"""Standard Telegram command and bottom-menu handlers (/start, /help, /status, /queue, /sso, /fixtags)."""

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from src.bot.keyboards.admin import (
    BTN_MAIN_INFO,
    BTN_MAIN_QUEUE,
    BTN_MAIN_SSO,
    build_guest_onboarding_keyboard,
    build_main_menu_keyboard,
    build_oidc_link_keyboard,
    build_paused_jobs_keyboard,
    build_queued_job_keyboard,
    build_tag_correction_review_keyboard,
)
from src.config.settings import AppSettings
from src.core.audit import format_guest_access_denied_card, format_queued_job_card
from src.core.exceptions import PermissionDeniedError
from src.core.models import JobState, Permission, UserAccount, UserRole
from src.core.oidc import AuthentikOidcService
from src.core.ports import GovernanceRepositoryPort
from src.core.queue import IngestionQueueWorker
from src.core.rbac import GovernanceService, has_permission

commands_router = Router(name="commands")


def _resolve_oidc_url(
    user: UserAccount | None,
    oidc_service: AuthentikOidcService | None,
    settings: AppSettings | None,
) -> str | None:
    """Build an Authentik OAuth2 authorization URL for `user` if OIDC is enabled."""
    if user is not None and oidc_service is not None and (settings is None or settings.oidc_enabled):
        url, _ = oidc_service.build_authorization_url(user.telegram_id)
        return url
    return None


def _parse_tag_correction_spec(
    raw_spec: str,
) -> tuple[str | None, str | None, str | None, int | None, str]:
    """Parse `title=... | artist=... | album=... | year=... | reason=...` tag correction fields."""
    proposed_title: str | None = None
    proposed_artist: str | None = None
    proposed_album: str | None = None
    proposed_year: int | None = None
    reason = ""

    segments = [seg.strip() for seg in raw_spec.split("|") if seg.strip()]
    for seg in segments:
        sep = "=" if "=" in seg else (":" if ":" in seg else None)
        if sep is not None:
            key, val = seg.split(sep, maxsplit=1)
            norm_key = key.strip().lower()
            clean_val = val.strip()
            if not clean_val:
                continue
            if norm_key in {"title", "track", "t"}:
                proposed_title = clean_val
            elif norm_key in {"artist", "a"}:
                proposed_artist = clean_val
            elif norm_key in {"album", "b"}:
                proposed_album = clean_val
            elif norm_key in {"year", "date", "y"} and clean_val.isdigit():
                proposed_year = int(clean_val)
            elif norm_key in {"reason", "note", "r"}:
                reason = clean_val
        elif " - " in seg and proposed_artist is None and proposed_title is None:
            artist_part, title_part = seg.split(" - ", maxsplit=1)
            proposed_artist = artist_part.strip() or None
            proposed_title = title_part.strip() or None
        elif proposed_title is None:
            proposed_title = seg

    return proposed_title, proposed_artist, proposed_album, proposed_year, reason


@commands_router.message(Command("start"))
async def handle_start(
    message: Message,
    current_user: UserAccount | None = None,
    oidc_service: AuthentikOidcService | None = None,
    settings: AppSettings | None = None,
    state: FSMContext | None = None,
) -> None:
    """Intercept GUEST users with the onboarding screen or present the simple upload prompt & bottom menu."""
    if state is not None:
        await state.clear()

    if current_user is not None and current_user.role == UserRole.GUEST:
        oidc_url = _resolve_oidc_url(current_user, oidc_service, settings)
        await message.answer(
            format_guest_access_denied_card(current_user.telegram_id),
            reply_markup=build_guest_onboarding_keyboard(oidc_url),
        )
        return

    role_badge = f" (Role: <b>{current_user.role.value}</b>)" if current_user is not None else ""
    reply_kb = build_main_menu_keyboard(current_user) if current_user is not None else None
    await message.answer(
        f"<b>Automated Music Ingestion Bot</b>{role_badge}\n"
        "Send an audio track (<code>.flac</code>, <code>.mp3</code>, <code>.m4a</code>) "
        "or an archive (<code>.zip</code>, <code>.7z</code>) up to 2 GB.",
        reply_markup=reply_kb,
    )


@commands_router.message(Command("cancel"))
async def handle_cancel_command(
    message: Message,
    state: FSMContext | None = None,
) -> None:
    """Exit any active FSM input or confirmation prompt and return to the default idle state."""
    if state is not None:
        await state.clear()
    await message.answer(
        "🛑 <b>Active prompt cancelled.</b>\n"
        "You can now send an audio/archive file to ingest or use <code>/songs</code> to browse the library."
    )


@commands_router.message(Command("help"))
async def handle_help(
    message: Message,
    state: FSMContext | None = None,
) -> None:
    """Provide usage instructions and pipeline overview."""
    if state is not None:
        await state.clear()
    await message.answer(
        "<b>Available Commands:</b>\n"
        "/start - Show upload prompt &amp; persistent bottom navigation\n"
        "/songs - Browse, search, download, edit tags/cover/lyrics, and export/import songs\n"
        "/rescan - Scan library folder and reconcile changes with database\n"
        "/cancel - Exit any active input or confirmation prompt\n"
        "/admin - Open Administrator Control Panel (MODERATOR / ADMIN / OWNER)\n"
        "/adduser <code>&lt;id&gt; [ROLE]</code> - Authorize or promote a Telegram user ID (ADMIN / OWNER)\n"
        "/fixtags <code>&lt;track_id&gt; title=... | artist=...</code> - Request post-ingestion tag edits\n"
        "/queue - View active and queued ingestion jobs\n"
        "/sso - Link or inspect Authentik SSO pairing\n"
        "/status - Check local Bot API, RBAC &amp; queue status\n"
        "/help - Show usage instructions"
    )


@commands_router.message(Command("status"))
@commands_router.message(F.text == BTN_MAIN_INFO)
async def handle_status(
    message: Message,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    state: FSMContext | None = None,
) -> None:
    """Report current operational, RBAC, OIDC, and queue status of the ingestion service."""
    if state is not None:
        await state.clear()

    if current_user is None or governance_repository is None:
        await message.answer("Status: <b>Online</b> (Local Bot API &amp; SQLite WAL ready).")
        return

    queued_jobs = await governance_repository.list_queued_jobs()
    active_jobs = await governance_repository.list_active_jobs()
    concurrency = queue_worker.concurrency_limit if queue_worker is not None else 1
    oidc_label = (
        f"Linked (<code>{current_user.authentik_username or current_user.authentik_sub}</code>)"
        if current_user.is_oidc_linked
        else "Not Linked"
    )
    reply_kb = build_main_menu_keyboard(current_user) if current_user.role != UserRole.GUEST else None
    await message.answer(
        "🟢 <b>System &amp; Session Status</b>\n"
        "────────────────────────\n"
        f"• Your Telegram ID: <code>{current_user.telegram_id}</code>\n"
        f"• Assigned Role: <b>{current_user.role.value}</b>\n"
        f"• Authentik OIDC: {oidc_label}\n"
        f"• Queue Concurrency: <b>{concurrency}</b>\n"
        f"• Global Queue: <b>{len(active_jobs)}</b> Active | <b>{len(queued_jobs)}</b> Queued",
        reply_markup=reply_kb,
    )


@commands_router.message(Command("sso"))
@commands_router.message(F.text == BTN_MAIN_SSO)
async def handle_sso_command(
    message: Message,
    current_user: UserAccount | None = None,
    oidc_service: AuthentikOidcService | None = None,
    settings: AppSettings | None = None,
    state: FSMContext | None = None,
) -> None:
    """Display Authentik OIDC pairing status and provide an inline SSO login button."""
    if state is not None:
        await state.clear()

    if current_user is None:
        await message.answer("Session unavailable.")
        return

    oidc_url = _resolve_oidc_url(current_user, oidc_service, settings)
    linked_status = (
        f"Linked as <code>{current_user.authentik_username or current_user.authentik_sub}</code>"
        if current_user.is_oidc_linked
        else "Not Linked"
    )
    await message.answer(
        "🔐 <b>Authentik OIDC Single Sign-On</b>\n"
        f"• Status: <b>{linked_status}</b>\n"
        f"• Current Role: <b>{current_user.role.value}</b>",
        reply_markup=build_oidc_link_keyboard(oidc_url),
    )


@commands_router.message(Command("queue"))
@commands_router.message(F.text == BTN_MAIN_QUEUE)
async def handle_queue_command(
    message: Message,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    state: FSMContext | None = None,
) -> None:
    """Display the user's queued jobs (or global queue and paused disambiguation jobs for MODERATOR+)."""
    if state is not None:
        await state.clear()

    if current_user is None or not has_permission(current_user, Permission.VIEW_OWN_JOBS):
        await message.answer("🔒 You need at least <b>UPLOADER</b> permissions to view the queue.")
        return

    if governance_repository is None:
        await message.answer("Queue repository is not initialized.")
        return

    can_view_global = has_permission(current_user, Permission.VIEW_GLOBAL_QUEUE)
    jobs = (
        await governance_repository.list_queued_jobs()
        if can_view_global
        else await governance_repository.list_queued_jobs(user_id=current_user.telegram_id)
    )
    active_jobs = await governance_repository.list_active_jobs()
    active_name = active_jobs[0].original_filename if active_jobs else None
    paused_jobs = [
        j
        for j in active_jobs
        if j.state in {JobState.AWAITING_CONFIRMATION, JobState.AWAITING_INPUT}
        and (
            has_permission(current_user, Permission.RESOLVE_OTHERS_JOBS)
            or j.user_id == current_user.telegram_id
        )
    ]

    if not jobs:
        if paused_jobs:
            await message.answer(
                f"⚠️ <b>{len(paused_jobs)} Job(s) Awaiting Disambiguation</b>\n"
                "Select a paused job below to resolve its MusicBrainz release:",
                reply_markup=build_paused_jobs_keyboard(paused_jobs),
            )
            return
        await message.answer(f"✅ <b>Ingestion Queue Empty</b> ({len(active_jobs)} active job(s) in flight).")
        return

    first_job = jobs[0]
    await message.answer(
        format_queued_job_card(first_job, position=1, active_job_filename=active_name),
        reply_markup=build_queued_job_keyboard(first_job.job_id),
    )


@commands_router.message(Command("fixtags"))
async def handle_fixtags_command(
    message: Message,
    current_user: UserAccount | None = None,
    governance_service: GovernanceService | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    state: FSMContext | None = None,
) -> None:
    """Submit a post-ingestion tag correction request (`REQUEST_TAG_CORRECTION`)."""
    if state is not None:
        await state.clear()
    if current_user is None or not has_permission(current_user, Permission.REQUEST_TAG_CORRECTION):
        await message.answer(
            "🔒 You need at least <b>UPLOADER</b> permissions to request post-ingestion tag corrections."
        )
        return

    if governance_service is None or governance_repository is None:
        await message.answer("Tag correction service is not initialized.")
        return

    raw_text = (message.text or "").strip()
    parts = raw_text.split(maxsplit=2)
    if len(parts) < 3:
        recent_tracks = await governance_repository.list_library_tracks(limit=10)
        lines = [
            "🏷 <b>Post-Ingestion Tag Correction</b>",
            (
                "Usage: <code>/fixtags &lt;track_id&gt; "
                "title=New Title | artist=New Artist | album=New Album | year=2024</code>"
            ),
        ]
        if recent_tracks:
            lines.append("\n<b>Recent Library Tracks:</b>")
            for trk in recent_tracks:
                lines.append(
                    f"• <code>{html.escape(trk.id)}</code> — {html.escape(trk.artist)} - "
                    f"<b>{html.escape(trk.title)}</b> (<i>{html.escape(trk.album)}</i>)"
                )
        await message.answer("\n".join(lines))
        return

    track_id = parts[1].strip()
    title, artist, album, year, reason = _parse_tag_correction_spec(parts[2])
    if title is None and artist is None and album is None and year is None:
        await message.answer(
            "⚠️ Please specify at least one tag field to update "
            "(e.g. <code>title=... | artist=... | album=... | year=2024</code>)."
        )
        return

    try:
        req, track = await governance_service.request_tag_correction(
            actor=current_user,
            track_id=track_id,
            proposed_title=title,
            proposed_artist=artist,
            proposed_album=album,
            proposed_year=year,
            reason=reason,
        )
    except (KeyError, PermissionDeniedError) as exc:
        await message.answer(f"⚠️ Could not create tag correction request: {html.escape(str(exc))}")
        return

    can_approve = has_permission(current_user, Permission.APPROVE_TAG_CORRECTIONS)
    kb = build_tag_correction_review_keyboard(req.id or 0) if can_approve and req.id is not None else None
    changes: list[str] = []
    if title is not None:
        changes.append(f"• Title: <code>{html.escape(track.title)}</code> → <b>{html.escape(title)}</b>")
    if artist is not None:
        changes.append(f"• Artist: <code>{html.escape(track.artist)}</code> → <b>{html.escape(artist)}</b>")
    if album is not None:
        changes.append(f"• Album: <code>{html.escape(track.album)}</code> → <b>{html.escape(album)}</b>")
    if year is not None:
        changes.append(f"• Year: <code>{track.year or '-'}</code> → <b>{year}</b>")

    await message.answer(
        f"✅ <b>Tag Correction Request #{req.id} Submitted</b>\n"
        f"Track ID: <code>{html.escape(track.id)}</code>\n" + "\n".join(changes),
        reply_markup=kb,
    )
