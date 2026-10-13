"""Standard Telegram command, bottom-menu, Help guide topics, and root `menu:*` callback handlers."""

import html

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from src.bot.keyboards.admin import (
    BTN_MAIN_INFO,
    BTN_MAIN_QUEUE,
    BTN_MAIN_SSO,
    RESERVED_MENU_BUTTONS,
    MenuCallback,
    build_back_to_home_keyboard,
    build_guest_onboarding_keyboard,
    build_help_menu_keyboard,
    build_help_topic_keyboard,
    build_home_menu_inline_keyboard,
    build_main_menu_keyboard,
    build_oidc_link_keyboard,
    build_paused_jobs_keyboard,
    build_queued_job_keyboard,
    build_tag_correction_review_keyboard,
)
from src.bot.keyboards.songs import build_song_action_keyboard
from src.bot.routers.songs import format_song_detail_card
from src.bot.ui import (
    ack_callback,
    format_screen,
    reset_fsm_state,
    resolve_back_callback,
    safe_answer,
    safe_edit_text,
)
from src.config.settings import AppSettings
from src.core.audit import (
    format_guest_access_denied_card,
    format_job_inspector_card,
)
from src.core.exceptions import PermissionDeniedError
from src.core.models import JobState, Permission, UserAccount, UserRole
from src.core.oidc import AuthentikOidcService
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.ports import CatalogRepositoryPort, GovernanceRepositoryPort
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


def _render_home_card(current_user: UserAccount | None) -> str:
    """Format the 3-tier HTML Home screen card."""
    role_str = current_user.role.value if current_user is not None else "AUTHORIZED"
    footers = [f"<i>Role:</i> <code>{role_str}</code> • <i>🧭 Home</i>"]
    if current_user is not None:
        footers = [
            f"<i>Telegram ID:</i> <code>{current_user.telegram_id}</code> • "
            f"<i>Role:</i> <code>{role_str}</code> • <i>🧭 Home</i>"
        ]
    return format_screen(
        title="🎵 Automated Music Ingestion Bot",
        context_lines=[
            "Send an audio track (<code>.flac</code>, <code>.mp3</code>, <code>.m4a</code>) "
            "or an archive (<code>.zip</code>, <code>.7z</code>) up to 2 GB.",
        ],
        footer_lines=footers,
    )


def _render_help_card() -> str:
    """Format the 3-tier HTML Help & Capabilities screen with topic guide selector."""
    return format_screen(
        title="ℹ️ Music Ingestion Gateway Guide",
        context_lines=[
            "• 📥 Direct Ingestion: Drop any lossless/lossy track or multi-track archive up to 2 GB.",
            "• 🧬 AcoustID &amp; MusicBrainz: Automatic fingerprinting, English/Romaji normalization, "
            "and cover art.",
            "• 📝 Synchronized Lyrics: Embedded audio tags plus sidecar <code>.lrc</code> files via LRCLIB.",
            "• 🧪 Safe Library Editor: Interactive metadata, cover, lyrics, and folder rescan "
            "with dry-run previews.",
        ],
        footer_lines=[
            "<i>Select a guide topic below to learn how each module works</i> • <i>🧭 Home › Help</i>"
        ],
        expandable=True,
    )


def _render_help_topic_card(topic: str) -> str:
    """Format an in-place documentation card explaining how a specific bot module works."""
    if topic == "ingestion":
        return format_screen(
            title="📖 Help Guide — Audio &amp; Archive Ingestion",
            context_lines=[
                "• <b>Supported Formats:</b> Send single audio files (<code>.flac</code>, <code>.mp3</code>, "
                "<code>.m4a</code>, <code>.ogg</code>, <code>.opus</code>, <code>.wav</code>) or multi-track "
                "archives (<code>.zip</code>, <code>.7z</code>) up to <b>2 GB</b>.",
                "• <b>10-Stage Automated Pipeline:</b> Unpacks to scratch storage, computes Chromaprint "
                "(<code>fpcalc</code>) fingerprint, checks duplicates, queries AcoustID + MusicBrainz + "
                "Genius + Last.fm, normalizes English/Romaji voice-actor (<code>CV</code>) credits, fetches "
                "LRCLIB synced lyrics and Cover Art Archive <code>cover.jpg</code>, writes embedded tags "
                "+ <code>.lrc</code> sidecar, and registers the track in SQLite.",
                "• <b>Auto-Approval vs. Review:</b> Matches with <code>&gt;= 98%</code> confidence ingest "
                "automatically; lower-confidence or ambiguous releases pause for interactive selection or "
                "full-pipeline manual tagging.",
                "• <b>Quick ID Lookup:</b> Paste any <b>Job ID</b> or <b>Track ID</b> directly in chat to "
                "open its interactive Inspector Card.",
            ],
            footer_lines=["<i>🧭 Home › Help › Ingestion Pipeline</i>"],
            expandable=True,
        )
    if topic == "songs":
        return format_screen(
            title="📖 Help Guide — Songs Library &amp; Editor",
            context_lines=[
                "• <b>Live Library Browser:</b> Tap <code>🎵 Songs</code> to browse recent tracks or type "
                "any title, artist, or album in chat to filter results immediately.",
                "• <b>Retrieval &amp; Provenance:</b> Inside a Song Card, use <code>📥 Get Song File</code> "
                "(instant forwarding/cached audio + <code>.lrc</code>), <code>💿 Get Full Album</code>, "
                "<code>📤 Export Data</code>, or <code>📜 Song History</code> to inspect who uploaded the "
                "song, when, and every subsequent edit.",
                "• <b>Guided Full-Pipeline Editor:</b> Edit <code>Title</code>, <code>Artist</code>, "
                "<code>Album</code>, <code>Year</code>, <code>Track #</code>, replace "
                "<code>🖼️ Cover Art</code> (photo, file, or URL), update <code>📝 Lyrics</code> "
                "(text or <code>.lrc</code>), run <code>🔎 Auto Re-Search</code>, or "
                "<code>📥 Import Data</code> from JSON.",
                "• <b>Mandatory Dry-Run Policy:</b> Every edit renders a <code>Before ➡️ After</code> and "
                "<code>Old Path ➡️ New Path</code> preview requiring explicit "
                "<code>✅ Confirm &amp; Apply Changes</code>.",
            ],
            footer_lines=["<i>🧭 Home › Help › Songs Library</i>"],
            expandable=True,
        )
    if topic == "queue":
        return format_screen(
            title="📖 Help Guide — Queue &amp; Manual Review",
            context_lines=[
                "• <b>Queue Overview:</b> Tap <code>⏳ Queue</code> to see all active, paused/held, and "
                "waiting jobs. Each button row represents a single queue item — tap any item to open its "
                "<b>Job Inspector</b>.",
                "• <b>Download Queued File:</b> Tap <code>📥 Get Queued File</code> on any queued or paused "
                "job to listen to the staged audio or download the archive before tagging.",
                "• <b>Full-Pipeline Manual Tag &amp; Review:</b> Tap <code>🔎 Manual Review &amp; Tag</code> "
                "to inspect candidates or open <code>✏️ Manual Tag &amp; Editor</code> — giving you the "
                "exact same full pipeline (Title, Artist, Album, Year, Track #, Cover Art, Lyrics, and "
                "Auto Re-Search with Dry-Run Preview) used when uploading and editing songs.",
                "• <b>Pause / Unlock / Cancel:</b> Hold a job with <code>⏸️ Pause / Hold</code> so the worker "
                "skips it while you review, resume with <code>▶️ Unlock &amp; Resume</code>, or abort with "
                "<code>❌ Cancel Job</code>.",
            ],
            footer_lines=["<i>🧭 Home › Help › Queue &amp; Review</i>"],
            expandable=True,
        )
    if topic == "sso":
        return format_screen(
            title="📖 Help Guide — Authentik SSO &amp; Roles",
            context_lines=[
                "• <b>Single Sign-On Linking:</b> Tap <code>🔐 Authentik SSO</code> and open the "
                "OAuth2 login link to bind your Telegram account to your Authentik identity.",
                "• <b>5-Tier Role Matrix:</b> Roles (<code>GUEST</code>, <code>UPLOADER</code>, "
                "<code>MODERATOR</code>, <code>ADMIN</code>, <code>OWNER</code>) govern upload access, "
                "global queue visibility, disambiguation of other users' jobs, and user management.",
                "• <b>Automatic Synchronization:</b> Linked accounts automatically refresh group claims "
                "every 15 minutes on activity and hourly in the background.",
            ],
            footer_lines=["<i>🧭 Home › Help › Authentik SSO</i>"],
            expandable=True,
        )
    if topic == "status":
        return format_screen(
            title="📖 Help Guide — System &amp; Session Status",
            context_lines=[
                "• <b>Session Diagnostics:</b> Displays your active RBAC role, Authentik OIDC link status, "
                "and Telegram ID.",
                "• <b>Pipeline Telemetry:</b> Shows the current worker concurrency limit (<code>1..4</code>) "
                "and real-time counts of active and queued ingestion jobs.",
            ],
            footer_lines=["<i>🧭 Home › Help › Status</i>"],
        )
    if topic == "admin":
        return format_screen(
            title="📖 Help Guide — Admin Control Panel",
            context_lines=[
                "• <b>User Management:</b> Authorize users by Telegram ID, promote/demote roles, block "
                "accounts, and inspect per-user <code>📤 User Uploads</code> and chronological "
                "<code>📝 User Changes &amp; History</code>.",
                "• <b>Queue &amp; Concurrency:</b> Inspect the global ingestion queue, resolve paused jobs "
                "across all users, and cycle worker concurrency between <code>1</code> and <code>4</code>.",
                "• <b>Governance &amp; Audit:</b> Review pending tag correction requests, trigger global "
                "OIDC claim sync, and inspect the system-wide audit trail.",
            ],
            footer_lines=["<i>🧭 Home › Help › Admin Panel</i>"],
            expandable=True,
        )
    return _render_help_card()


async def _render_status_card(
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort | None,
    queue_worker: IngestionQueueWorker | None,
    organizer: LibraryOrganizer | None = None,
) -> str:
    """Format the 3-tier HTML System & Session Status screen."""
    if current_user is None or governance_repository is None:
        return format_screen(
            title="🟢 System &amp; Session Status",
            context_lines=["Status: Online (Local Bot API &amp; SQLite WAL ready)."],
            footer_lines=["<i>🧭 Home › Status</i>"],
        )

    queued_jobs = await governance_repository.list_queued_jobs()
    active_jobs = await governance_repository.list_active_jobs()
    concurrency = queue_worker.concurrency_limit if queue_worker is not None else 1
    oidc_ident = html.escape(current_user.authentik_username or current_user.authentik_sub or "")
    oidc_label = f"Linked (<code>{oidc_ident}</code>)" if current_user.is_oidc_linked else "Not Linked"
    sync_status = (
        f"🟡 In Progress ({html.escape(organizer.reconciliation_status)})"
        if organizer is not None and organizer.is_reconciling
        else "🟢 Ready"
    )
    return format_screen(
        title="🟢 System &amp; Session Status",
        context_lines=[
            f"• Assigned Role: <code>{current_user.role.value}</code>",
            f"• Authentik OIDC: {oidc_label}",
            f"• Queue Concurrency: <code>{concurrency}</code>",
            f"• Global Queue: <code>{len(active_jobs)}</code> Active | "
            f"<code>{len(queued_jobs)}</code> Queued",
            f"• Library Sync: {sync_status}",
        ],
        footer_lines=[
            f"<i>Your Telegram ID:</i> <code>{current_user.telegram_id}</code> • <i>🧭 Home › Status</i>"
        ],
    )


def _render_sso_card(current_user: UserAccount) -> str:
    """Format the 3-tier HTML Authentik OIDC Single Sign-On screen."""
    oidc_ident = html.escape(current_user.authentik_username or current_user.authentik_sub or "")
    linked_status = f"Linked as <code>{oidc_ident}</code>" if current_user.is_oidc_linked else "Not Linked"
    return format_screen(
        title="🔐 Authentik OIDC Single Sign-On",
        context_lines=[
            f"• Status: {linked_status}",
            f"• Current Role: <code>{current_user.role.value}</code>",
        ],
        footer_lines=[f"<i>Telegram ID:</i> <code>{current_user.telegram_id}</code> • <i>🧭 Home › SSO</i>"],
    )


async def render_queue_overview_screen(
    current_user: UserAccount,
    governance_repository: GovernanceRepositoryPort,
    *,
    back_callback: str = "menu:home",
    include_home: bool = False,
    refresh_callback: str | None = "queue:mine",
    breadcrumb: str = "🧭 Home › Queue",
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the structured Queue Overview card and per-item selector keyboard."""
    can_view_global = has_permission(current_user, Permission.VIEW_GLOBAL_QUEUE)
    queued_jobs = (
        await governance_repository.list_queued_jobs()
        if can_view_global
        else await governance_repository.list_queued_jobs(user_id=current_user.telegram_id)
    )
    all_active_jobs = await governance_repository.list_active_jobs()
    paused_states = {JobState.PAUSED, JobState.AWAITING_CONFIRMATION, JobState.AWAITING_INPUT}
    paused_jobs = [
        j
        for j in all_active_jobs
        if j.state in paused_states
        and (
            has_permission(current_user, Permission.RESOLVE_OTHERS_JOBS)
            or j.user_id == current_user.telegram_id
        )
    ]
    running_jobs = [
        j
        for j in all_active_jobs
        if j.state not in paused_states and (can_view_global or j.user_id == current_user.telegram_id)
    ]

    if not queued_jobs and not paused_jobs and not running_jobs:
        text = format_screen(
            title="✅ Ingestion Queue Empty",
            context_lines=[f"<code>{len(all_active_jobs)}</code> active job(s) in flight."],
            footer_lines=[f"<i>{breadcrumb}</i>"],
        )
        kb = build_paused_jobs_keyboard(
            [],
            queued_jobs=[],
            active_jobs=[],
            back_callback=back_callback,
            include_home=include_home,
            refresh_callback=refresh_callback,
        )
        return text, kb

    lines: list[str] = [
        f"• Running: <code>{len(running_jobs)}</code> | "
        f"Paused / Review: <code>{len(paused_jobs)}</code> | "
        f"Queued: <code>{len(queued_jobs)}</code>",
    ]
    for job in running_jobs[:4]:
        fname = html.escape(job.original_filename or job.source_path.name)
        lines.append(
            f"⚙️ Active: <code>{fname}</code> "
            f"(<code>{html.escape(job.state.value)}</code> • <code>{html.escape(job.job_id[:8])}</code>)"
        )
    for job in paused_jobs[:6]:
        fname = html.escape(job.original_filename or job.source_path.name)
        badge = "⏸️ Held" if job.state == JobState.PAUSED else "🔍 Awaiting Disambiguation"
        lines.append(f"{badge}: <code>{fname}</code> (Job <code>{html.escape(job.job_id[:8])}</code>)")
    for idx, job in enumerate(queued_jobs[:8], start=1):
        fname = html.escape(job.original_filename or job.source_path.name)
        lines.append(f"⏳ #{idx}: <code>{fname}</code> (Job <code>{html.escape(job.job_id[:8])}</code>)")
    lines.append("Tap a queue item below to inspect, download the file, pause/unlock, or manually tag:")

    has_disambiguation = any(
        j.state in {JobState.AWAITING_CONFIRMATION, JobState.AWAITING_INPUT} for j in paused_jobs
    )
    if has_disambiguation and not queued_jobs:
        title = f"⚠️ {len(paused_jobs)} Job(s) Awaiting Disambiguation / Review"
    else:
        title = (
            f"⏳ Ingestion Queue ({len(running_jobs)} active, "
            f"{len(paused_jobs)} paused, {len(queued_jobs)} queued)"
        )

    text = format_screen(
        title=title,
        context_lines=lines,
        footer_lines=[f"<i>{breadcrumb}</i>"],
        expandable=len(lines) > 5,
    )
    kb = build_paused_jobs_keyboard(
        paused_jobs,
        queued_jobs=queued_jobs,
        active_jobs=running_jobs,
        back_callback=back_callback,
        include_home=include_home,
        refresh_callback=refresh_callback,
    )
    return text, kb


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
    """Intercept GUEST users with the onboarding screen or present the upload prompt & bottom menu."""
    await reset_fsm_state(state, clear_nav=True)
    await resolve_back_callback(state, None, "menu:home", reset_to_home=True)

    if current_user is not None and current_user.role == UserRole.GUEST:
        oidc_url = _resolve_oidc_url(current_user, oidc_service, settings)
        await safe_answer(
            message,
            format_guest_access_denied_card(current_user.telegram_id),
            reply_markup=build_guest_onboarding_keyboard(
                oidc_url,
                telegram_id=current_user.telegram_id,
            ),
        )
        return

    reply_kb = build_main_menu_keyboard(current_user) if current_user is not None else None
    await message.answer(
        _render_home_card(current_user),
        reply_markup=reply_kb,
    )


@commands_router.message(Command("cancel"))
async def handle_cancel_command(
    message: Message,
    current_user: UserAccount | None = None,
    state: FSMContext | None = None,
) -> None:
    """Exit any active FSM input or confirmation prompt and return to the default idle state."""
    await reset_fsm_state(state, clear_nav=True)
    await resolve_back_callback(state, None, "menu:home", reset_to_home=True)
    await message.answer(
        format_screen(
            title="🛑 Active prompt cancelled",
            context_lines=[
                "Your interactive session has been reset. "
                "Drop an audio/archive file to ingest or select a destination below.",
            ],
            footer_lines=["<i>🧭 Home</i>"],
        ),
        reply_markup=build_home_menu_inline_keyboard(current_user),
    )


@commands_router.message(Command("help"))
async def handle_help(
    message: Message,
    current_user: UserAccount | None = None,
    state: FSMContext | None = None,
) -> None:
    """Provide interactive Help topic buttons explaining how each bot module works."""
    await reset_fsm_state(state)
    back_cb = await resolve_back_callback(
        state,
        None,
        "menu:help",
        default_back="menu:home",
        reset_to_home=True,
    )
    await message.answer(
        _render_help_card(),
        reply_markup=build_help_menu_keyboard(current_user, back_callback=back_cb),
    )


@commands_router.callback_query(F.data.startswith("help:"))
async def handle_help_topic_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    state: FSMContext | None = None,
) -> None:
    """Render in-place help documentation for the selected module (`help:<topic>`)."""
    await ack_callback(callback)
    await reset_fsm_state(state)
    raw_data = callback.data or "help:ingestion"
    topic = raw_data.split(":", maxsplit=1)[1] if ":" in raw_data else "ingestion"
    back_cb = await resolve_back_callback(
        state,
        callback.message,
        f"help:{topic}",
        default_back="menu:help",
    )
    await safe_edit_text(
        callback.message,
        _render_help_topic_card(topic),
        reply_markup=build_help_topic_keyboard(topic, current_user, back_callback=back_cb),
    )


@commands_router.message(Command("status"))
@commands_router.message(F.text == BTN_MAIN_INFO)
async def handle_status(
    message: Message,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    organizer: LibraryOrganizer | None = None,
    state: FSMContext | None = None,
) -> None:
    """Report current operational, RBAC, OIDC, and queue status of the ingestion service."""
    await reset_fsm_state(state)
    await resolve_back_callback(
        state,
        None,
        "menu:status",
        default_back="menu:home",
        reset_to_home=True,
    )

    text = await _render_status_card(current_user, governance_repository, queue_worker, organizer=organizer)
    if current_user is None or governance_repository is None:
        await message.answer(text, reply_markup=build_back_to_home_keyboard())
        return

    reply_kb = build_main_menu_keyboard(current_user) if current_user.role != UserRole.GUEST else None
    await message.answer(text, reply_markup=reply_kb)


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
    await reset_fsm_state(state)
    back_cb = await resolve_back_callback(
        state,
        None,
        "menu:sso",
        default_back="menu:home",
        reset_to_home=True,
    )

    if current_user is None:
        await message.answer(
            format_screen(
                title="⚠️ Session Unavailable",
                context_lines=["Unable to resolve user session context."],
                footer_lines=["<i>🧭 Home › SSO</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
        )
        return

    oidc_url = _resolve_oidc_url(current_user, oidc_service, settings)
    await safe_answer(
        message,
        _render_sso_card(current_user),
        reply_markup=build_oidc_link_keyboard(
            oidc_url,
            subject=current_user.authentik_sub,
            back_callback=back_cb,
        ),
    )


@commands_router.callback_query(F.data.startswith("menu:"))
async def handle_menu_navigation_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    organizer: LibraryOrganizer | None = None,
    oidc_service: AuthentikOidcService | None = None,
    settings: AppSettings | None = None,
    state: FSMContext | None = None,
) -> None:
    """Mutate the current screen in-place when the user taps `🏠 Menu`, `◀ Back`, Status, SSO, or Help."""
    await ack_callback(callback)
    await reset_fsm_state(state)

    raw_data = callback.data or "menu:home"
    action = raw_data.split(":", maxsplit=1)[1] if ":" in raw_data else "home"

    if current_user is not None and current_user.role == UserRole.GUEST:
        oidc_url = _resolve_oidc_url(current_user, oidc_service, settings)
        await safe_edit_text(
            callback.message,
            format_guest_access_denied_card(current_user.telegram_id),
            reply_markup=build_guest_onboarding_keyboard(
                oidc_url,
                telegram_id=current_user.telegram_id,
            ),
        )
        return

    if action == "status":
        back_cb = await resolve_back_callback(
            state,
            callback.message,
            "menu:status",
            default_back="menu:home",
        )
        text = await _render_status_card(
            current_user,
            governance_repository,
            queue_worker,
            organizer=organizer,
        )
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
        )
        return

    if action == "sso" and current_user is not None:
        back_cb = await resolve_back_callback(
            state,
            callback.message,
            "menu:sso",
            default_back="menu:home",
        )
        oidc_url = _resolve_oidc_url(current_user, oidc_service, settings)
        await safe_edit_text(
            callback.message,
            _render_sso_card(current_user),
            reply_markup=build_oidc_link_keyboard(
                oidc_url,
                subject=current_user.authentik_sub,
                back_callback=back_cb,
            ),
        )
        return

    if action == "help":
        back_cb = await resolve_back_callback(
            state,
            callback.message,
            "menu:help",
            default_back="menu:home",
        )
        await safe_edit_text(
            callback.message,
            _render_help_card(),
            reply_markup=build_help_menu_keyboard(current_user, back_callback=back_cb),
        )
        return

    await resolve_back_callback(state, callback.message, "menu:home", reset_to_home=True)
    await safe_edit_text(
        callback.message,
        _render_home_card(current_user),
        reply_markup=build_home_menu_inline_keyboard(current_user),
    )


@commands_router.message(Command("queue"))
@commands_router.message(F.text == BTN_MAIN_QUEUE)
async def handle_queue_command(
    message: Message,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    state: FSMContext | None = None,
) -> None:
    """Display the structured Queue Overview with 1 row per queue item."""
    await reset_fsm_state(state)

    if current_user is None or not has_permission(current_user, Permission.VIEW_OWN_JOBS):
        await message.answer(
            format_screen(
                title="🔒 Permission Required",
                context_lines=["You need at least <code>UPLOADER</code> permissions to view the queue."],
                footer_lines=["<i>🧭 Home › Queue</i>"],
            )
        )
        return

    if governance_repository is None:
        await message.answer(
            format_screen(
                title="⚙️ Queue Unavailable",
                context_lines=["Queue repository is not initialized."],
                footer_lines=["<i>🧭 Home › Queue</i>"],
            )
        )
        return

    back_cb = await resolve_back_callback(
        state,
        None,
        "queue:mine",
        default_back=MenuCallback(action="home").pack(),
        reset_to_home=True,
    )
    text, kb = await render_queue_overview_screen(
        current_user,
        governance_repository,
        back_callback=back_cb,
        include_home=back_cb != MenuCallback(action="home").pack(),
        refresh_callback="queue:mine",
    )
    await safe_answer(message, text, reply_markup=kb)


async def _inspect_id_query(
    message: Message,
    raw_query: str,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort | None,
    catalog_repository: CatalogRepositoryPort | None,
    pipeline: IngestionPipeline | None,
    queue_worker: IngestionQueueWorker | None,
    state: FSMContext | None = None,
) -> bool:
    """Look up `raw_query` as a Job ID or Track ID and reply with an interactive Inspector Card if matched."""
    query = raw_query.strip()
    if not query:
        return False

    target_job = None
    if catalog_repository is not None:
        target_job = await catalog_repository.get_job(query)
    if target_job is None and pipeline is not None:
        target_job = await pipeline.get_job(query)

    if target_job is not None:
        if current_user is not None and target_job.user_id not in {None, current_user.telegram_id}:
            can_view_others = has_permission(current_user, Permission.VIEW_GLOBAL_QUEUE) or has_permission(
                current_user, Permission.RESOLVE_OTHERS_JOBS
            )
            if not can_view_others:
                await message.answer(
                    format_screen(
                        title="🔒 Permission Denied",
                        context_lines=["You do not have permission to inspect another user's job."],
                        footer_lines=["<i>🧭 Home › Queue › Job Inspector</i>"],
                    ),
                    reply_markup=build_back_to_home_keyboard(),
                )
                return True

        uploader_lbl: str | None = None
        if target_job.user_id and governance_repository is not None:
            u_obj = await governance_repository.get_user(target_job.user_id)
            uploader_lbl = (
                f"{u_obj.mention_handle} ({u_obj.telegram_id})"
                if u_obj is not None
                else f"User {target_job.user_id}"
            )
        pos = await queue_worker.get_queue_position(target_job.job_id) if queue_worker is not None else 0
        back_cb = await resolve_back_callback(
            state,
            None,
            f"queue:view:{target_job.job_id}",
            default_back="queue:mine",
            reset_to_home=True,
        )
        await safe_answer(
            message,
            format_job_inspector_card(target_job, uploader_label=uploader_lbl, position=pos),
            reply_markup=build_queued_job_keyboard(
                target_job.job_id,
                state=target_job.state,
                back_callback=back_cb,
                include_home=back_cb != "menu:home",
            ),
        )
        return True

    if governance_repository is not None:
        track = await governance_repository.get_library_track(query)
        if track is not None:
            has_lrc = track.storage_path.with_suffix(".lrc").is_file()
            has_cover = (track.storage_path.parent / "cover.jpg").is_file()
            up_lbl: str | None = None
            if track.uploaded_by > 0:
                u_obj = await governance_repository.get_user(track.uploaded_by)
                up_lbl = (
                    f"{u_obj.mention_handle} ({u_obj.telegram_id})"
                    if u_obj is not None
                    else f"User {track.uploaded_by}"
                )
            mod_lbl: str | None = None
            if track.modified_by and track.modified_by > 0:
                m_obj = await governance_repository.get_user(track.modified_by)
                mod_lbl = (
                    f"{m_obj.mention_handle} ({m_obj.telegram_id})"
                    if m_obj is not None
                    else f"User {track.modified_by}"
                )
            back_cb = await resolve_back_callback(
                state,
                None,
                f"songs:view:{track.id[:12]}",
                default_back="songs:list",
                reset_to_home=True,
            )
            await safe_answer(
                message,
                format_song_detail_card(
                    track,
                    has_lrc_file=has_lrc,
                    has_cover_file=has_cover,
                    uploader_label=up_lbl,
                    modifier_label=mod_lbl,
                ),
                reply_markup=build_song_action_keyboard(track.id, back_callback=back_cb),
            )
            return True

    return False


@commands_router.message(Command("job"))
async def handle_job_command(
    message: Message,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    catalog_repository: CatalogRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    state: FSMContext | None = None,
) -> None:
    """Inspect and control a specific job by ID (`/job <job_id>`)."""
    await reset_fsm_state(state)

    if current_user is not None and current_user.role == UserRole.GUEST:
        await message.answer(
            format_screen(
                title="🔒 Permission Required",
                context_lines=["You need at least <code>UPLOADER</code> permissions to inspect jobs."],
                footer_lines=["<i>🧭 Home › Queue › Job Inspector</i>"],
            )
        )
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            format_screen(
                title="🔎 Inspect Job by ID",
                context_lines=[
                    "Paste a <b>Job ID</b> (or Track ID) directly in chat, or use "
                    "<code>/job &lt;job_id&gt;</code> to open its interactive control panel."
                ],
                footer_lines=["<i>🧭 Home › Queue › Job Inspector</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(),
        )
        return

    matched = await _inspect_id_query(
        message=message,
        raw_query=parts[1],
        current_user=current_user,
        governance_repository=governance_repository,
        catalog_repository=catalog_repository,
        pipeline=pipeline,
        queue_worker=queue_worker,
        state=state,
    )
    if not matched:
        await message.answer(
            format_screen(
                title="⚠️ Job Not Found",
                context_lines=[
                    "No ingestion job or track found matching ID "
                    f"<code>{html.escape(parts[1].strip())}</code>."
                ],
                footer_lines=["<i>🧭 Home › Queue › Job Inspector</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(),
        )


@commands_router.message(
    StateFilter(None),
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(RESERVED_MENU_BUTTONS),
)
async def handle_idle_id_lookup_text(
    message: Message,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    catalog_repository: CatalogRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    state: FSMContext | None = None,
) -> None:
    """Allow pasting a Job ID or Track ID in idle chat to open its interactive Inspector Card."""
    if current_user is not None and current_user.role == UserRole.GUEST:
        return
    raw_text = (message.text or "").strip()
    if len(raw_text) < 4 or " " in raw_text:
        return
    await _inspect_id_query(
        message=message,
        raw_query=raw_text,
        current_user=current_user,
        governance_repository=governance_repository,
        catalog_repository=catalog_repository,
        pipeline=pipeline,
        queue_worker=queue_worker,
        state=state,
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
            format_screen(
                title="🔒 Permission Required",
                context_lines=[
                    "You need at least <code>UPLOADER</code> permissions to request tag corrections."
                ],
                footer_lines=["<i>🧭 Home › Tag Corrections</i>"],
            )
        )
        return

    if governance_service is None or governance_repository is None:
        await message.answer(
            format_screen(
                title="⚙️ Service Unavailable",
                context_lines=["Tag correction service is not initialized."],
                footer_lines=["<i>🧭 Home › Tag Corrections</i>"],
            )
        )
        return

    raw_text = (message.text or "").strip()
    parts = raw_text.split(maxsplit=2)
    if len(parts) < 3:
        recent_tracks = await governance_repository.list_library_tracks(limit=10)
        context_lines = [
            "Format: <code>&lt;track_id&gt; title=New Title | artist=New Artist | "
            "album=New Album | year=2024</code>",
        ]
        if recent_tracks:
            context_lines.append("")
            context_lines.append("Recent Library Tracks:")
            for trk in recent_tracks:
                context_lines.append(
                    f"• <code>{html.escape(trk.id)}</code> — {html.escape(trk.artist)} - "
                    f"{html.escape(trk.title)} (<i>{html.escape(trk.album)}</i>)"
                )
        await message.answer(
            format_screen(
                title="🏷 Post-Ingestion Tag Correction",
                context_lines=context_lines,
                footer_lines=["<i>🧭 Home › Tag Corrections</i>"],
                expandable=bool(recent_tracks),
            ),
            reply_markup=build_back_to_home_keyboard(),
        )
        return

    track_id = parts[1].strip()
    title, artist, album, year, reason = _parse_tag_correction_spec(parts[2])
    if title is None and artist is None and album is None and year is None:
        await message.answer(
            format_screen(
                title="⚠️ Missing Tag Fields",
                context_lines=[
                    "Please specify at least one tag field to update "
                    "(e.g. <code>title=... | artist=... | album=... | year=2024</code>)."
                ],
                footer_lines=[f"<i>Track ID:</i> <code>{html.escape(track_id)}</code>"],
            )
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
        await message.answer(
            format_screen(
                title="⚠️ Tag Correction Rejected",
                context_lines=[f"Could not create tag correction request: {html.escape(str(exc))}"],
                footer_lines=[f"<i>Track ID:</i> <code>{html.escape(track_id)}</code>"],
            )
        )
        return

    can_approve = has_permission(current_user, Permission.APPROVE_TAG_CORRECTIONS)
    kb = build_tag_correction_review_keyboard(req.id or 0) if can_approve and req.id is not None else None
    changes: list[str] = []
    if title is not None:
        changes.append(
            f"• Title: <code>{html.escape(track.title)}</code> → <code>{html.escape(title)}</code>"
        )
    if artist is not None:
        changes.append(
            f"• Artist: <code>{html.escape(track.artist)}</code> → <code>{html.escape(artist)}</code>"
        )
    if album is not None:
        changes.append(
            f"• Album: <code>{html.escape(track.album)}</code> → <code>{html.escape(album)}</code>"
        )
    if year is not None:
        changes.append(f"• Year: <code>{track.year or '-'}</code> → <code>{year}</code>")

    await message.answer(
        format_screen(
            title=f"✅ Tag Correction Request #{req.id} Submitted",
            context_lines=changes,
            footer_lines=[f"<i>Track ID:</i> <code>{html.escape(track.id)}</code>"],
        ),
        reply_markup=kb,
    )
