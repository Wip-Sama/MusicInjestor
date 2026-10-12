"""Handlers for `/admin` dashboard, `/adduser`, manual access requests (`access_req:*`), and queue actions."""

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.keyboards.admin import (
    BTN_MAIN_ADMIN,
    RESERVED_MENU_BUTTONS,
    build_admin_dashboard_keyboard,
    build_main_menu_keyboard,
    build_paused_jobs_keyboard,
    build_queued_job_keyboard,
    build_tag_correction_review_keyboard,
    build_user_detail_keyboard,
    build_user_list_keyboard,
)
from src.bot.keyboards.verification import build_verification_keyboard
from src.bot.routers.files import format_ingestion_summary
from src.bot.states import AdminStates, IngestionStates
from src.config.settings import AppSettings
from src.core.audit import (
    format_admin_dashboard_card,
    format_queued_job_card,
    format_user_detail_card,
)
from src.core.exceptions import PermissionDeniedError
from src.core.models import IngestionResult, JobState, Permission, UserAccount, UserRole
from src.core.oidc import AuthentikOidcService
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.ports import GovernanceRepositoryPort
from src.core.queue import IngestionQueueWorker
from src.core.rbac import GovernanceService, has_permission

logger = logging.getLogger(__name__)

admin_router = Router(name="admin")


def parse_access_request_callback(data: str) -> tuple[str, int | None, UserRole | None]:
    """Parse callback strings of the form `access_req:<action>[:<request_id>[:<role>]]`."""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "access_req":
        msg = f"Malformed access_req callback data: {data}"
        raise ValueError(msg)

    action = parts[1]
    if action in {"request", "oidc_disabled"}:
        return action, None, None
    if action == "deny" and len(parts) == 3:
        return action, int(parts[2]), None
    if action == "approve" and len(parts) == 4:
        return action, int(parts[2]), UserRole(parts[3])

    msg = f"Unsupported access_req callback structure: {data}"
    raise ValueError(msg)


def parse_admin_callback(data: str) -> tuple[str, int | None, UserRole | None]:
    """Parse callback strings of the form `admin:<action>[:<target_id>[:<role>]]`."""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "admin":
        msg = f"Malformed admin callback data: {data}"
        raise ValueError(msg)

    action = parts[1]
    if action in {
        "panel",
        "users",
        "add_user",
        "sync_all",
        "concurrency",
        "queue",
        "audit",
        "tags",
        "wipe",
        "close",
    }:
        return action, None, None
    if action in {"user", "block", "resync", "tag_approve", "tag_reject"} and len(parts) == 3:
        return action, int(parts[2]), None
    if action == "resolve_job" and len(parts) == 3:
        return action, None, None
    if action in {"promote", "demote"} and len(parts) == 4:
        return action, int(parts[2]), UserRole(parts[3])

    msg = f"Unsupported admin callback structure: {data}"
    raise ValueError(msg)


async def _render_dashboard_payload(
    governance_repo: GovernanceRepositoryPort,
    queue_worker: IngestionQueueWorker | None,
    settings: AppSettings | None,
) -> tuple[str, int, int]:
    """Build the formatted Administrator Control Panel text and return `(text, queued_count, limit)`."""
    users = await governance_repo.list_users()
    active_users = sum(1 for u in users if not u.is_blocked and u.role != UserRole.GUEST)
    pending_reqs = await governance_repo.list_pending_access_requests()
    active_jobs = await governance_repo.list_active_jobs()
    queued_jobs = await governance_repo.list_queued_jobs()
    limit = (
        queue_worker.concurrency_limit
        if queue_worker is not None
        else (settings.queue_concurrency_limit if settings is not None else 1)
    )
    node_name = settings.node_name if settings is not None else "Proxmox LXC 104"
    text = format_admin_dashboard_card(
        node_name=node_name,
        concurrency_limit=limit,
        active_users_count=active_users,
        pending_requests_count=len(pending_reqs),
        active_jobs_count=len(active_jobs),
        queued_jobs_count=len(queued_jobs),
    )
    return text, len(queued_jobs), limit


async def _authorize_user_by_spec(
    raw_spec: str,
    actor: UserAccount,
    governance_service: GovernanceService,
    governance_repository: GovernanceRepositoryPort,
) -> UserAccount:
    """Parse `<telegram_id> [ROLE]` (defaulting to UPLOADER), create/load user, and assign role."""
    tokens = raw_spec.strip().split()
    if not tokens:
        raise ValueError("Please provide a numeric Telegram user ID.")
    target_id = int(tokens[0])
    target_role = UserRole(tokens[1].upper()) if len(tokens) > 1 else UserRole.UPLOADER

    existing = await governance_repository.get_user(target_id)
    if existing is None:
        await governance_service.ensure_user(
            telegram_id=target_id,
            display_name=f"User {target_id}",
        )
    return await governance_service.change_user_role(
        actor=actor,
        target_telegram_id=target_id,
        new_role=target_role,
        reason="Manual Admin Authorization by Telegram ID",
    )


@admin_router.message(Command("admin"))
@admin_router.message(F.text == BTN_MAIN_ADMIN)
async def handle_admin_command(
    message: Message,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    settings: AppSettings | None = None,
    state: FSMContext | None = None,
) -> None:
    """Open the interactive Administrator Control Panel (`/admin`, Specification Section 7.1)."""
    if state is not None:
        await state.clear()

    if current_user is None or not has_permission(current_user, Permission.MANAGE_USERS):
        await message.answer("🔒 Administrator privileges are required to access <code>/admin</code>.")
        return

    if governance_repository is None:
        await message.answer("⚙️ Admin panel is unavailable (governance repository not wired).")
        return

    text, queued_count, limit = await _render_dashboard_payload(
        governance_repository,
        queue_worker,
        settings,
    )
    await message.answer(
        text,
        reply_markup=build_admin_dashboard_keyboard(queued_count, limit),
    )


@admin_router.message(Command("adduser"))
async def handle_adduser_command(
    message: Message,
    current_user: UserAccount | None = None,
    governance_service: GovernanceService | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    state: FSMContext | None = None,
) -> None:
    """Authorize or promote a user by Telegram ID: `/adduser <telegram_id> [UPLOADER|MODERATOR|ADMIN]`."""
    if state is not None:
        await state.clear()

    if current_user is None or not has_permission(current_user, Permission.MANAGE_USERS):
        await message.answer("🔒 Administrator privileges are required to use <code>/adduser</code>.")
        return
    if governance_service is None or governance_repository is None:
        await message.answer("Governance service is not initialized.")
        return

    raw_text = (message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "ℹ️ <b>Usage:</b> <code>/adduser &lt;telegram_id&gt; [UPLOADER|MODERATOR|ADMIN]</code>\n"
            "Example: <code>/adduser 987654321 UPLOADER</code>"
        )
        return

    try:
        updated = await _authorize_user_by_spec(
            parts[1],
            current_user,
            governance_service,
            governance_repository,
        )
    except (ValueError, PermissionDeniedError) as exc:
        await message.answer(f"⚠️ Could not authorize user: {exc}")
        return

    albums_count, tracks_count = await governance_repository.count_user_uploads(updated.telegram_id)
    await message.answer(
        f"✅ Authorized <code>{updated.telegram_id}</code> as <b>{updated.role.value}</b>.\n\n"
        + format_user_detail_card(updated, albums_count, tracks_count),
        reply_markup=build_user_detail_keyboard(updated),
    )


@admin_router.message(
    AdminStates.awaiting_new_user_id,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(RESERVED_MENU_BUTTONS),
)
async def handle_new_user_id_input(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    governance_service: GovernanceService | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
) -> None:
    """Process interactive `<telegram_id> [ROLE]` reply after clicking `[ ➕ Add User by ID ]`."""
    await state.clear()
    if (
        current_user is None
        or governance_service is None
        or governance_repository is None
        or not has_permission(current_user, Permission.MANAGE_USERS)
    ):
        await message.answer("🔒 Session expired or insufficient privileges.")
        return

    try:
        updated = await _authorize_user_by_spec(
            message.text or "",
            current_user,
            governance_service,
            governance_repository,
        )
    except (ValueError, PermissionDeniedError) as exc:
        await message.answer(
            f"⚠️ Invalid input ({exc}).\n"
            "Use <code>/adduser &lt;telegram_id&gt; [UPLOADER|MODERATOR|ADMIN]</code> to try again."
        )
        return

    albums_count, tracks_count = await governance_repository.count_user_uploads(updated.telegram_id)
    await message.answer(
        f"✅ User <code>{updated.telegram_id}</code> updated to <b>{updated.role.value}</b>.\n\n"
        + format_user_detail_card(updated, albums_count, tracks_count),
        reply_markup=build_user_detail_keyboard(updated),
    )


@admin_router.callback_query(F.data.startswith("access_req:"))
async def handle_access_request_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    governance_service: GovernanceService | None = None,
) -> None:
    """Process guest `[ 🙋 Request Access ]` clicks and Admin Channel `[ Approve / Deny ]` actions."""
    if not callback.data or current_user is None or governance_service is None:
        await callback.answer("Action unavailable.", show_alert=True)
        return

    action, request_id, role = parse_access_request_callback(callback.data)

    if action == "oidc_disabled":
        await callback.answer(
            "ℹ️ Authentik OIDC SSO is not enabled in .env (OIDC_ENABLED=false).",
            show_alert=True,
        )
        return

    if action == "request":
        if current_user.role != UserRole.GUEST:
            await callback.answer(f"You already hold {current_user.role.value} access.", show_alert=True)
            return
        _, created_new = await governance_service.submit_access_request(current_user)
        if created_new:
            await callback.answer("✅ Access request sent to administrators!", show_alert=True)
        else:
            await callback.answer(
                "⏳ Your access request is already pending administrator review.",
                show_alert=True,
            )
        return

    if action == "approve" and request_id is not None and role is not None:
        try:
            approved_user = await governance_service.approve_access_request(
                actor=current_user,
                request_id=request_id,
                granted_role=role,
            )
        except PermissionDeniedError:
            await callback.answer("🔒 Admin privileges required to approve requests.", show_alert=True)
            return

        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"✅ <b>Access Request #{request_id} Approved</b>\n"
                f"• User: {approved_user.mention_handle} (ID: <code>{approved_user.telegram_id}</code>)\n"
                f"• Assigned Role: <b>{role.value}</b>\n"
                f"• Approved by: {current_user.mention_handle}"
            )
        if callback.bot is not None:
            try:
                await callback.bot.send_message(
                    chat_id=approved_user.telegram_id,
                    text=(
                        f"🎉 Your access request has been approved! You now have {role.value} permissions."
                    ),
                    reply_markup=build_main_menu_keyboard(approved_user),
                )
            except Exception as exc:
                logger.warning("Could not DM approved user %s: %s", approved_user.telegram_id, exc)
        await callback.answer(f"Approved as {role.value}.")
        return

    if action == "deny" and request_id is not None:
        try:
            denied_req = await governance_service.deny_access_request(
                actor=current_user,
                request_id=request_id,
            )
        except PermissionDeniedError:
            await callback.answer("🔒 Admin privileges required to deny requests.", show_alert=True)
            return

        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"❌ <b>Access Request #{request_id} Denied</b>\n"
                f"• User ID: <code>{denied_req.telegram_id}</code>\n"
                f"• Reviewed by: {current_user.mention_handle}"
            )
        await callback.answer("Access request denied.")


@admin_router.callback_query(F.data.startswith("admin:"))
async def handle_admin_dashboard_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    governance_service: GovernanceService | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    oidc_service: AuthentikOidcService | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
    state: FSMContext | None = None,
) -> None:
    """Handle interactive button navigation inside the `/admin` Control Panel."""
    if not callback.data or current_user is None or governance_repository is None:
        await callback.answer("Admin panel unavailable.", show_alert=True)
        return

    action, target_id, target_role = parse_admin_callback(callback.data)
    msg = callback.message if isinstance(callback.message, Message) else None

    if action == "resolve_job":
        job_id = callback.data.split(":", maxsplit=2)[2]
        if pipeline is None:
            await callback.answer("Pipeline unavailable.", show_alert=True)
            return
        target_job = await pipeline.get_job(job_id)
        if target_job is None:
            await callback.answer("Job not found.", show_alert=True)
            return
        can_resolve = (
            has_permission(current_user, Permission.RESOLVE_OTHERS_JOBS)
            if target_job.user_id != current_user.telegram_id
            else has_permission(current_user, Permission.RESOLVE_OWN_DISAMBIGUATION)
        )
        if not can_resolve:
            await callback.answer(
                "🔒 Moderator privileges required to resolve another user's job.",
                show_alert=True,
            )
            return
        if state is not None:
            await state.set_state(IngestionStates.awaiting_confirmation)
            await state.update_data(job_id=target_job.job_id)
        summary = format_ingestion_summary(IngestionResult(job=target_job, requires_user_confirmation=True))
        kb = build_verification_keyboard(target_job.job_id, target_job.candidates)
        if msg is not None:
            await msg.edit_text(summary, reply_markup=kb)
        await callback.answer(f"Opened disambiguation for job {target_job.job_id}.")
        return

    if action in {"tags", "tag_approve", "tag_reject"}:
        if not has_permission(current_user, Permission.APPROVE_TAG_CORRECTIONS):
            await callback.answer(
                "🔒 Moderator privileges required to review tag corrections.",
                show_alert=True,
            )
            return
        if action == "tags":
            pending_tags = await governance_repository.list_pending_tag_correction_requests()
            if not pending_tags:
                await callback.answer("✅ No pending tag correction requests.", show_alert=True)
                return
            first_req = pending_tags[0]
            track = await governance_repository.get_library_track(first_req.track_id)
            track_desc = (
                f"{html.escape(track.artist)} - <b>{html.escape(track.title)}</b> "
                f"(<i>{html.escape(track.album)}</i>)"
                if track is not None
                else f"<code>{html.escape(first_req.track_id)}</code>"
            )
            lines = [
                f"🏷 <b>Pending Tag Correction #{first_req.id}</b> ({len(pending_tags)} pending)",
                f"• Track: {track_desc}",
                f"• Requested by: <code>{first_req.requested_by}</code>",
            ]
            if first_req.proposed_title:
                lines.append(f"• Proposed Title: <b>{html.escape(first_req.proposed_title)}</b>")
            if first_req.proposed_artist:
                lines.append(f"• Proposed Artist: <b>{html.escape(first_req.proposed_artist)}</b>")
            if first_req.proposed_album:
                lines.append(f"• Proposed Album: <b>{html.escape(first_req.proposed_album)}</b>")
            if first_req.proposed_year:
                lines.append(f"• Proposed Year: <b>{first_req.proposed_year}</b>")
            if first_req.reason:
                lines.append(f"• Reason: <i>{html.escape(first_req.reason)}</i>")
            if msg is not None and first_req.id is not None:
                await msg.edit_text(
                    "\n".join(lines),
                    reply_markup=build_tag_correction_review_keyboard(first_req.id),
                )
            await callback.answer()
            return

        if governance_service is None or target_id is None:
            await callback.answer("Governance service unavailable.", show_alert=True)
            return

        if action == "tag_approve":
            try:
                resolved, updated_track = await governance_service.approve_tag_correction(
                    actor=current_user,
                    request_id=target_id,
                )
            except (KeyError, PermissionDeniedError) as exc:
                await callback.answer(str(exc), show_alert=True)
                return

            if updated_track.storage_path.is_file():
                organizer = LibraryOrganizer(updated_track.storage_path.parent)
                await organizer.apply_tag_correction(
                    audio_path=updated_track.storage_path,
                    title=resolved.proposed_title,
                    artist=resolved.proposed_artist,
                    album=resolved.proposed_album,
                    year=resolved.proposed_year,
                )

            if msg is not None:
                artist_esc = html.escape(updated_track.artist)
                title_esc = html.escape(updated_track.title)
                await msg.edit_text(
                    f"✅ <b>Tag Correction #{target_id} Approved &amp; Applied</b>\n"
                    f"• Track: {artist_esc} - <b>{title_esc}</b>\n"
                    f"• Album: <i>{html.escape(updated_track.album)}</i>"
                )
            await callback.answer("Tag correction approved and written to file!")
            return

        if action == "tag_reject":
            try:
                await governance_service.reject_tag_correction(
                    actor=current_user,
                    request_id=target_id,
                )
            except (KeyError, PermissionDeniedError) as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            if msg is not None:
                await msg.edit_text(f"❌ <b>Tag Correction #{target_id} Rejected</b>")
            await callback.answer("Tag correction rejected.")
            return

    if action == "wipe":
        if governance_service is None:
            await callback.answer("Governance service unavailable.", show_alert=True)
            return
        try:
            removed = await governance_service.wipe_library_catalog(current_user)
        except PermissionDeniedError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if msg is not None:
            await msg.edit_text(
                f"🧨 <b>System Catalog Wipe Executed</b>\nRemoved <b>{removed}</b> catalog/track records."
            )
        await callback.answer("Catalog wiped.")
        return

    if not has_permission(current_user, Permission.MANAGE_USERS):
        await callback.answer("🔒 Administrator privileges required.", show_alert=True)
        return

    if action == "close":
        if state is not None:
            await state.clear()
        if msg is not None:
            await msg.delete()
        await callback.answer("Control panel closed.")
        return

    if action == "panel":
        if state is not None:
            await state.clear()
        text, queued_count, limit = await _render_dashboard_payload(
            governance_repository,
            queue_worker,
            settings,
        )
        if msg is not None:
            await msg.edit_text(
                text,
                reply_markup=build_admin_dashboard_keyboard(queued_count, limit),
            )
        await callback.answer()
        return

    if action == "users":
        if state is not None:
            await state.clear()
        users = await governance_repository.list_users()
        pending_reqs = await governance_repository.list_pending_access_requests()
        pending_user_ids = {req.telegram_id for req in pending_reqs}
        header = (
            f"👥 <b>User Management</b> ({len(users)} registered, {len(pending_reqs)} pending)\n"
            "Select a user below or click <b>➕ Add / Authorize User by ID</b>:"
        )
        if msg is not None:
            await msg.edit_text(
                header,
                reply_markup=build_user_list_keyboard(users, pending_user_ids=pending_user_ids),
            )
        await callback.answer()
        return

    if action == "add_user":
        if state is not None:
            await state.set_state(AdminStates.awaiting_new_user_id)
        if msg is not None:
            await msg.answer(
                "➕ <b>Authorize or Promote User by Telegram ID</b>\n"
                "Reply with the user's numeric Telegram ID and optional role:\n"
                "<code>&lt;telegram_id&gt; [UPLOADER|MODERATOR|ADMIN]</code>\n\n"
                "Example: <code>987654321 UPLOADER</code>"
            )
        await callback.answer()
        return

    if action == "user" and target_id is not None:
        target_user = await governance_repository.get_user(target_id)
        if target_user is None:
            await callback.answer("User not found.", show_alert=True)
            return
        albums_count, tracks_count = await governance_repository.count_user_uploads(target_id)
        if msg is not None:
            await msg.edit_text(
                format_user_detail_card(target_user, albums_count, tracks_count),
                reply_markup=build_user_detail_keyboard(target_user),
            )
        await callback.answer()
        return

    if action in {"promote", "demote"} and target_id is not None and target_role is not None:
        if governance_service is None:
            await callback.answer("Governance service unavailable.", show_alert=True)
            return
        updated_user = await governance_service.change_user_role(
            actor=current_user,
            target_telegram_id=target_id,
            new_role=target_role,
            reason=f"Manual {action.title()} via Admin Panel",
        )
        albums_count, tracks_count = await governance_repository.count_user_uploads(target_id)
        if msg is not None:
            await msg.edit_text(
                format_user_detail_card(updated_user, albums_count, tracks_count),
                reply_markup=build_user_detail_keyboard(updated_user),
            )
        await callback.answer(f"Role updated to {target_role.value}.")
        return

    if action == "block" and target_id is not None and governance_service is not None:
        existing = await governance_repository.get_user(target_id)
        if existing is None:
            await callback.answer("User not found.", show_alert=True)
            return
        try:
            updated = await governance_service.set_user_blocked(
                actor=current_user,
                target_telegram_id=target_id,
                is_blocked=not existing.is_blocked,
            )
        except PermissionDeniedError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        albums_count, tracks_count = await governance_repository.count_user_uploads(target_id)
        if msg is not None:
            await msg.edit_text(
                format_user_detail_card(updated, albums_count, tracks_count),
                reply_markup=build_user_detail_keyboard(updated),
            )
        status_word = "blocked" if updated.is_blocked else "unblocked"
        await callback.answer(f"User {status_word}.")
        return

    if action == "resync" and target_id is not None:
        target_user = await governance_repository.get_user(target_id)
        if target_user is None or not target_user.is_oidc_linked or oidc_service is None:
            await callback.answer("User is not linked to Authentik OIDC.", show_alert=True)
            return
        outcome = await oidc_service.refresh_user_from_idp(target_user)
        albums_count, tracks_count = await governance_repository.count_user_uploads(target_id)
        if msg is not None:
            await msg.edit_text(
                format_user_detail_card(outcome.user, albums_count, tracks_count),
                reply_markup=build_user_detail_keyboard(outcome.user),
            )
        await callback.answer(f"OIDC synchronized (Role: {outcome.new_role.value}).")
        return

    if action == "sync_all":
        if oidc_service is None:
            await callback.answer("OIDC service is not configured.", show_alert=True)
            return
        outcomes = await oidc_service.sync_all_linked_users()
        await callback.answer(f"🔄 Synchronized {len(outcomes)} Authentik account(s).", show_alert=True)
        return

    if action == "concurrency" and queue_worker is not None:
        next_limit = (queue_worker.concurrency_limit % 4) + 1
        await queue_worker.set_concurrency_limit(current_user, next_limit)
        text, queued_count, limit = await _render_dashboard_payload(
            governance_repository,
            queue_worker,
            settings,
        )
        if msg is not None:
            await msg.edit_text(
                text,
                reply_markup=build_admin_dashboard_keyboard(queued_count, limit),
            )
        await callback.answer(f"Concurrency limit set to {limit}.")
        return

    if action == "queue":
        queued_jobs = await governance_repository.list_queued_jobs()
        active_jobs = await governance_repository.list_active_jobs()
        paused_jobs = [
            j for j in active_jobs if j.state in {JobState.AWAITING_CONFIRMATION, JobState.AWAITING_INPUT}
        ]
        lines = [f"⏳ <b>Global Ingestion Queue</b> ({len(active_jobs)} active, {len(queued_jobs)} queued)"]
        if not queued_jobs and not active_jobs:
            lines.append("• No jobs currently in queue.")
        for idx, job in enumerate(queued_jobs[:10], start=1):
            lines.append(
                f"#{idx} • <code>{job.original_filename or job.source_path.name}</code> "
                f"(User <code>{job.user_id}</code>)"
            )
        for job in paused_jobs[:5]:
            lines.append(
                f"⚠️ Paused: <code>{job.original_filename or job.source_path.name}</code> "
                f"(Job <code>{job.job_id}</code>, User <code>{job.user_id}</code>)"
            )
        if msg is not None:
            text, queued_count, limit = await _render_dashboard_payload(
                governance_repository,
                queue_worker,
                settings,
            )
            kb = (
                build_paused_jobs_keyboard(paused_jobs)
                if paused_jobs
                else build_admin_dashboard_keyboard(queued_count, limit)
            )
            await msg.edit_text(
                "\n".join(lines) + "\n\n" + text,
                reply_markup=kb,
            )
        await callback.answer()
        return

    if action == "audit":
        logs = await governance_repository.list_audit_logs(limit=10)
        lines = ["📜 <b>Recent Audit Logs</b>"]
        if not logs:
            lines.append("• No audit events recorded yet.")
        for entry in logs:
            lines.append(
                f"• <b>{entry.action}</b> by <code>{entry.actor_id}</code> "
                f"→ <code>{entry.target_entity or '-'}</code>"
            )
        if msg is not None:
            _, queued_count, limit = await _render_dashboard_payload(
                governance_repository,
                queue_worker,
                settings,
            )
            await msg.edit_text(
                "\n".join(lines),
                reply_markup=build_admin_dashboard_keyboard(queued_count, limit),
            )
        await callback.answer()


@admin_router.message(Command("wipe"))
async def handle_wipe_command(
    message: Message,
    current_user: UserAccount | None = None,
    governance_service: GovernanceService | None = None,
    state: FSMContext | None = None,
) -> None:
    """Execute a destructive library catalog wipe (`SYSTEM_DESTRUCTIVE_WIPE`, OWNER only)."""
    if state is not None:
        await state.clear()

    if current_user is None or not has_permission(current_user, Permission.SYSTEM_DESTRUCTIVE_WIPE):
        await message.answer("🔒 Only the system <b>OWNER</b> can execute <code>/wipe</code>.")
        return
    if governance_service is None:
        await message.answer("Governance service is not initialized.")
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() != "CONFIRM":
        await message.answer(
            "⚠️ <b>Destructive Action Warning</b>\n"
            "Send <code>/wipe CONFIRM</code> to permanently clear all catalog tracks and job records."
        )
        return

    removed = await governance_service.wipe_library_catalog(current_user)
    await message.answer(
        f"🧨 <b>System Catalog Wipe Executed</b>\nRemoved <b>{removed}</b> catalog/track records."
    )


@admin_router.callback_query(F.data.in_({"queue:mine", "queue:global"}))
async def handle_queue_view_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
) -> None:
    """Show the user's queued jobs or global queue when clicked from `/start`."""
    if current_user is None or governance_repository is None:
        await callback.answer("Queue unavailable.", show_alert=True)
        return

    is_global = callback.data == "queue:global" and has_permission(
        current_user,
        Permission.VIEW_GLOBAL_QUEUE,
    )
    jobs = (
        await governance_repository.list_queued_jobs()
        if is_global
        else await governance_repository.list_queued_jobs(user_id=current_user.telegram_id)
    )
    active_jobs = await governance_repository.list_active_jobs()
    active_name = active_jobs[0].original_filename if active_jobs else None

    if not jobs:
        await callback.answer(
            f"Queue is empty ({len(active_jobs)} active job(s)).",
            show_alert=True,
        )
        return

    first_job = jobs[0]
    if isinstance(callback.message, Message):
        await callback.message.answer(
            format_queued_job_card(first_job, position=1, active_job_filename=active_name),
            reply_markup=build_queued_job_keyboard(first_job.job_id),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("queue:cancel:"))
async def handle_queue_cancel_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    state: FSMContext | None = None,
) -> None:
    """Cancel a queued job when the uploader or a moderator clicks `[ ❌ Cancel Job ]`."""
    if not callback.data or current_user is None or queue_worker is None:
        await callback.answer("Queue cancellation unavailable.", show_alert=True)
        return

    job_id = callback.data.split(":", maxsplit=2)[2]
    try:
        cancelled_job = await queue_worker.cancel_job(job_id, current_user)
    except PermissionDeniedError:
        await callback.answer("🔒 You do not have permission to cancel this job.", show_alert=True)
        return
    except KeyError:
        await callback.answer("Job not found.", show_alert=True)
        return

    if state is not None:
        await state.clear()

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"🛑 <b>Job Cancelled</b>\nPayload: <code>{cancelled_job.original_filename}</code>"
        )
    await callback.answer("Job cancelled.")
