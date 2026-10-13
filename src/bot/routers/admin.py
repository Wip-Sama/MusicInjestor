"""Handlers for `/admin` dashboard, `/adduser`, manual access requests (`access_req:*`), and queue actions."""

import html
import inspect
import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from src.bot.keyboards.admin import (
    BTN_MAIN_ADMIN,
    RESERVED_MENU_BUTTONS,
    build_admin_dashboard_keyboard,
    build_admin_subscreen_keyboard,
    build_back_to_home_keyboard,
    build_main_menu_keyboard,
    build_queued_job_keyboard,
    build_tag_correction_review_keyboard,
    build_user_detail_keyboard,
    build_user_list_keyboard,
    build_user_uploads_keyboard,
)
from src.bot.keyboards.verification import build_verification_keyboard
from src.bot.routers.commands import render_queue_overview_screen
from src.bot.routers.files import format_ingestion_summary
from src.bot.states import AdminStates, IngestionStates
from src.bot.ui import (
    ack_callback,
    format_loading_card,
    format_screen,
    reset_fsm_state,
    resolve_back_callback,
    safe_answer,
    safe_edit_text,
    send_chat_action_safe,
)
from src.config.settings import AppSettings
from src.core.audit import (
    format_admin_dashboard_card,
    format_job_inspector_card,
    format_queued_job_card,
    format_user_detail_card,
)
from src.core.exceptions import PermissionDeniedError
from src.core.extractor import SUPPORTED_AUDIO_EXTENSIONS
from src.core.models import (
    IngestionJob,
    IngestionResult,
    JobState,
    Permission,
    UserAccount,
    UserRole,
)
from src.core.oidc import AuthentikOidcService
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.ports import CatalogRepositoryPort, GovernanceRepositoryPort
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
    if (
        action
        in {
            "user",
            "block",
            "resync",
            "tag_approve",
            "tag_reject",
            "user_uploads",
            "user_changes",
        }
        and len(parts) == 3
    ):
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
    await reset_fsm_state(state)
    back_cb = await resolve_back_callback(
        state,
        None,
        "admin:panel",
        default_back="menu:home",
        reset_to_home=True,
    )

    if current_user is None or not has_permission(current_user, Permission.MANAGE_USERS):
        await message.answer(
            format_screen(
                title="🔒 Administrator Privileges Required",
                context_lines=["You do not have permission to access the Administrator Control Panel."],
                footer_lines=["<i>🧭 Home › Admin Panel</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
        )
        return

    if governance_repository is None:
        await message.answer(
            format_screen(
                title="⚙️ Admin Panel Unavailable",
                context_lines=["Governance repository is not wired."],
                footer_lines=["<i>🧭 Home › Admin Panel</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
        )
        return

    text, queued_count, limit = await _render_dashboard_payload(
        governance_repository,
        queue_worker,
        settings,
    )
    await message.answer(
        text,
        reply_markup=build_admin_dashboard_keyboard(
            queued_count,
            limit,
            back_callback=back_cb,
        ),
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
    await reset_fsm_state(state)

    if current_user is None or not has_permission(current_user, Permission.MANAGE_USERS):
        await message.answer(
            format_screen(
                title="🔒 Administrator Privileges Required",
                context_lines=["You do not have permission to authorize users."],
                footer_lines=["<i>🧭 Home › Admin › Authorize User</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(),
        )
        return
    if governance_service is None or governance_repository is None:
        await message.answer(
            format_screen(
                title="⚙️ Governance Unavailable",
                context_lines=["Governance service is not initialized."],
                footer_lines=["<i>🧭 Home › Admin</i>"],
            )
        )
        return

    raw_text = (message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            format_screen(
                title="ℹ️ Authorize User by Telegram ID",
                context_lines=[
                    "Format: <code>&lt;telegram_id&gt; [UPLOADER|MODERATOR|ADMIN]</code>",
                    "Example: <code>987654321 UPLOADER</code>",
                ],
                footer_lines=["<i>🧭 Home › Admin › Users › Add User</i>"],
            ),
            reply_markup=build_admin_subscreen_keyboard(back_callback="admin:users"),
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
        await message.answer(
            format_screen(
                title="⚠️ Authorization Failed",
                context_lines=[f"Could not authorize user: {html.escape(str(exc))}"],
                footer_lines=["<i>🧭 Home › Admin › Users</i>"],
            ),
            reply_markup=build_admin_subscreen_keyboard(back_callback="admin:users"),
        )
        return

    albums_count, tracks_count = await governance_repository.count_user_uploads(updated.telegram_id)
    await safe_answer(
        message,
        format_user_detail_card(updated, albums_count, tracks_count),
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
        await message.answer(
            format_screen(
                title="🔒 Session Expired",
                context_lines=["Session expired or insufficient privileges."],
                footer_lines=["<i>🧭 Home › Admin</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(),
        )
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
            format_screen(
                title="⚠️ Invalid User Specification",
                context_lines=[
                    f"Reason: {html.escape(str(exc))}",
                    "Expected format: <code>&lt;telegram_id&gt; [UPLOADER|MODERATOR|ADMIN]</code>",
                ],
                footer_lines=["<i>🧭 Home › Admin › Users › Add User</i>"],
            ),
            reply_markup=build_admin_subscreen_keyboard(back_callback="admin:users"),
        )
        return

    albums_count, tracks_count = await governance_repository.count_user_uploads(updated.telegram_id)
    await safe_answer(
        message,
        format_user_detail_card(updated, albums_count, tracks_count),
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
        await ack_callback(callback, "Action unavailable.", show_alert=True)
        return

    action, request_id, role = parse_access_request_callback(callback.data)

    if action == "oidc_disabled":
        await ack_callback(
            callback,
            "ℹ️ Authentik OIDC SSO is not enabled in .env (OIDC_ENABLED=false).",
            show_alert=True,
        )
        return

    if action == "request":
        if current_user.role != UserRole.GUEST:
            await ack_callback(
                callback,
                f"You already hold {current_user.role.value} access.",
                show_alert=True,
            )
            return
        _, created_new = await governance_service.submit_access_request(current_user)
        if created_new:
            await ack_callback(callback, "✅ Access request sent to administrators!", show_alert=True)
        else:
            await ack_callback(
                callback,
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
            await ack_callback(
                callback,
                "🔒 Admin privileges required to approve requests.",
                show_alert=True,
            )
            return

        await ack_callback(callback, f"Approved as {role.value}.")
        await safe_edit_text(
            callback.message,
            format_screen(
                title=f"✅ Access Request #{request_id} Approved",
                context_lines=[
                    f"• User: {html.escape(approved_user.mention_handle)} "
                    f"(ID: <code>{approved_user.telegram_id}</code>)",
                    f"• Assigned Role: <code>{role.value}</code>",
                    f"• Approved by: {html.escape(current_user.mention_handle)}",
                ],
                footer_lines=[f"<i>Request ID:</i> <code>{request_id}</code>"],
            ),
        )
        if callback.bot is not None:
            try:
                await callback.bot.send_message(
                    chat_id=approved_user.telegram_id,
                    text=format_screen(
                        title="🎉 Access Request Approved",
                        context_lines=[
                            f"Your access request has been approved! "
                            f"You now have <code>{role.value}</code> permissions."
                        ],
                        footer_lines=[f"<i>Telegram ID:</i> <code>{approved_user.telegram_id}</code>"],
                    ),
                    reply_markup=build_main_menu_keyboard(approved_user),
                )
            except Exception as exc:
                logger.warning("Could not DM approved user %s: %s", approved_user.telegram_id, exc)
        return

    if action == "deny" and request_id is not None:
        try:
            denied_req = await governance_service.deny_access_request(
                actor=current_user,
                request_id=request_id,
            )
        except PermissionDeniedError:
            await ack_callback(
                callback,
                "🔒 Admin privileges required to deny requests.",
                show_alert=True,
            )
            return

        await ack_callback(callback, "Access request denied.")
        await safe_edit_text(
            callback.message,
            format_screen(
                title=f"❌ Access Request #{request_id} Denied",
                context_lines=[
                    f"• User ID: <code>{denied_req.telegram_id}</code>",
                    f"• Reviewed by: {html.escape(current_user.mention_handle)}",
                ],
                footer_lines=[f"<i>Request ID:</i> <code>{request_id}</code>"],
            ),
        )


async def _deliver_queued_job_file(
    msg: Message | object | None,
    job: IngestionJob,
) -> bool:
    """Send the queued job's staged audio or archive file to `msg` so the user/moderator can listen/review."""
    if msg is None:
        return False

    candidate_path = job.source_path
    if not candidate_path.is_file() and job.scratch_dir and job.scratch_dir.is_dir():
        for staged in sorted(job.scratch_dir.iterdir()):
            if staged.is_file() and staged.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                candidate_path = staged
                break
    if not candidate_path.is_file():
        return False

    payload_name = html.escape(job.original_filename or candidate_path.name)
    caption = (
        f"<b>📥 Queued File for Review: {payload_name}</b>\n"
        f"<blockquote>• Job ID: <code>{html.escape(job.job_id)}</code>\n"
        f"• State: <code>{html.escape(job.state.value)}</code></blockquote>"
    )
    suffix = candidate_path.suffix.lower()
    answer_audio_fn = getattr(msg, "answer_audio", None)
    answer_doc_fn = getattr(msg, "answer_document", None)

    if suffix in SUPPORTED_AUDIO_EXTENSIONS and callable(answer_audio_fn):
        meta = job.selected_metadata or (job.candidates[0].metadata if job.candidates else None)
        title = meta.title if meta is not None else candidate_path.stem
        performer = meta.artist if meta is not None else "Unknown Artist"
        res = answer_audio_fn(
            FSInputFile(candidate_path),
            title=title,
            performer=performer,
            caption=caption,
        )
        if inspect.isawaitable(res):
            await res
        return True

    if callable(answer_doc_fn):
        res = answer_doc_fn(
            FSInputFile(candidate_path),
            caption=caption,
        )
        if inspect.isawaitable(res):
            await res
        return True
    return False


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
    """Handle interactive button navigation inside the `/admin` Control Panel in-place."""
    if not callback.data or current_user is None or governance_repository is None:
        await ack_callback(callback, "Admin panel unavailable.", show_alert=True)
        return

    action, target_id, target_role = parse_admin_callback(callback.data)
    msg = callback.message

    if action == "resolve_job":
        job_id = callback.data.split(":", maxsplit=2)[2]
        if pipeline is None:
            await ack_callback(callback, "Pipeline unavailable.", show_alert=True)
            return
        target_job = await pipeline.get_job(job_id)
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        can_resolve = (
            has_permission(current_user, Permission.RESOLVE_OTHERS_JOBS)
            if target_job.user_id != current_user.telegram_id
            else has_permission(current_user, Permission.RESOLVE_OWN_DISAMBIGUATION)
        )
        if not can_resolve:
            await ack_callback(
                callback,
                "🔒 Moderator privileges required to resolve another user's job.",
                show_alert=True,
            )
            return
        await ack_callback(callback, f"Opened disambiguation for job {target_job.job_id}.")
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:review:{target_job.job_id}",
            default_back="admin:queue",
        )
        if state is not None:
            await state.set_state(IngestionStates.awaiting_confirmation)
            await state.update_data(job_id=target_job.job_id)
        summary = format_ingestion_summary(IngestionResult(job=target_job, requires_user_confirmation=True))
        kb = build_verification_keyboard(
            target_job.job_id,
            target_job.candidates,
            back_callback=back_cb,
        )
        await safe_edit_text(msg, summary, reply_markup=kb)
        return

    if action in {"tags", "tag_approve", "tag_reject"}:
        if not has_permission(current_user, Permission.APPROVE_TAG_CORRECTIONS):
            await ack_callback(
                callback,
                "🔒 Moderator privileges required to review tag corrections.",
                show_alert=True,
            )
            return
        if action == "tags":
            pending_tags = await governance_repository.list_pending_tag_correction_requests()
            if not pending_tags:
                await ack_callback(callback, "✅ No pending tag correction requests.", show_alert=True)
                return
            await ack_callback(callback)
            first_req = pending_tags[0]
            track = await governance_repository.get_library_track(first_req.track_id)
            track_desc = (
                f"{html.escape(track.artist)} - <code>{html.escape(track.title)}</code> "
                f"(<i>{html.escape(track.album)}</i>)"
                if track is not None
                else f"<code>{html.escape(first_req.track_id)}</code>"
            )
            lines = [
                f"• Track: {track_desc}",
                f"• Requested by: <code>{first_req.requested_by}</code>",
            ]
            if first_req.proposed_title:
                lines.append(f"• Proposed Title: <code>{html.escape(first_req.proposed_title)}</code>")
            if first_req.proposed_artist:
                lines.append(f"• Proposed Artist: <code>{html.escape(first_req.proposed_artist)}</code>")
            if first_req.proposed_album:
                lines.append(f"• Proposed Album: <code>{html.escape(first_req.proposed_album)}</code>")
            if first_req.proposed_year:
                lines.append(f"• Proposed Year: <code>{first_req.proposed_year}</code>")
            if first_req.reason:
                lines.append(f"• Reason: <i>{html.escape(first_req.reason)}</i>")
            if first_req.id is not None:
                await safe_edit_text(
                    msg,
                    format_screen(
                        title=f"🏷 Pending Tag Correction #{first_req.id} ({len(pending_tags)} pending)",
                        context_lines=lines,
                        footer_lines=["<i>🧭 Home › Admin › Tag Corrections</i>"],
                    ),
                    reply_markup=build_tag_correction_review_keyboard(first_req.id),
                )
            return

        if governance_service is None or target_id is None:
            await ack_callback(callback, "Governance service unavailable.", show_alert=True)
            return

        if action == "tag_approve":
            try:
                resolved, updated_track = await governance_service.approve_tag_correction(
                    actor=current_user,
                    request_id=target_id,
                )
            except (KeyError, PermissionDeniedError) as exc:
                await ack_callback(callback, str(exc), show_alert=True)
                return

            await ack_callback(callback, "Tag correction approved and written to file!")
            await send_chat_action_safe(callback)
            await safe_edit_text(
                msg,
                format_loading_card(
                    "Applying Tag Correction",
                    "Writing updated metadata tags to audio file...",
                    "🧭 Home › Admin › Tag Corrections",
                ),
            )

            if updated_track.storage_path.is_file():
                organizer = LibraryOrganizer(updated_track.storage_path.parent)
                await organizer.apply_tag_correction(
                    audio_path=updated_track.storage_path,
                    title=resolved.proposed_title,
                    artist=resolved.proposed_artist,
                    album=resolved.proposed_album,
                    year=resolved.proposed_year,
                )

            artist_esc = html.escape(updated_track.artist)
            title_esc = html.escape(updated_track.title)
            back_cb = "admin:panel" if has_permission(current_user, Permission.MANAGE_USERS) else "menu:home"
            await safe_edit_text(
                msg,
                format_screen(
                    title=f"✅ Tag Correction #{target_id} Approved &amp; Applied",
                    context_lines=[
                        f"• Track: {artist_esc} - <code>{title_esc}</code>",
                        f"• Album: <i>{html.escape(updated_track.album)}</i>",
                    ],
                    footer_lines=[
                        f"<i>Track ID:</i> <code>{html.escape(updated_track.id)}</code> • "
                        "<i>🧭 Home › Admin › Tag Corrections</i>"
                    ],
                ),
                reply_markup=build_admin_subscreen_keyboard(
                    back_callback=back_cb,
                    include_home=back_cb != "menu:home",
                ),
            )
            return

        if action == "tag_reject":
            try:
                await governance_service.reject_tag_correction(
                    actor=current_user,
                    request_id=target_id,
                )
            except (KeyError, PermissionDeniedError) as exc:
                await ack_callback(callback, str(exc), show_alert=True)
                return
            await ack_callback(callback, "Tag correction rejected.")
            back_cb = "admin:panel" if has_permission(current_user, Permission.MANAGE_USERS) else "menu:home"
            await safe_edit_text(
                msg,
                format_screen(
                    title=f"❌ Tag Correction #{target_id} Rejected",
                    context_lines=["The tag correction request was declined."],
                    footer_lines=["<i>🧭 Home › Admin › Tag Corrections</i>"],
                ),
                reply_markup=build_admin_subscreen_keyboard(
                    back_callback=back_cb,
                    include_home=back_cb != "menu:home",
                ),
            )
            return

    if action == "wipe":
        if governance_service is None:
            await ack_callback(callback, "Governance service unavailable.", show_alert=True)
            return
        try:
            removed = await governance_service.wipe_library_catalog(current_user)
        except PermissionDeniedError as exc:
            await ack_callback(callback, str(exc), show_alert=True)
            return
        await ack_callback(callback, "Catalog wiped.")
        await safe_edit_text(
            msg,
            format_screen(
                title="🧨 System Catalog Wipe Executed",
                context_lines=[f"Removed <code>{removed}</code> catalog/track records."],
                footer_lines=["<i>🧭 Home › Admin › System Wipe</i>"],
            ),
            reply_markup=build_admin_subscreen_keyboard(back_callback="admin:panel"),
        )
        return

    if not has_permission(current_user, Permission.MANAGE_USERS):
        await ack_callback(callback, "🔒 Administrator privileges required.", show_alert=True)
        return

    if action == "close":
        await ack_callback(callback, "Control panel closed.")
        await reset_fsm_state(state, clear_nav=True)
        delete_fn = getattr(msg, "delete", None)
        if callable(delete_fn):
            try:
                del_res = delete_fn()
                if inspect.isawaitable(del_res):
                    await del_res
            except Exception as exc:
                logger.debug("Message delete on admin:close failed, editing to Home: %s", exc)
                await safe_edit_text(
                    msg,
                    format_screen(
                        title="✅ Control Panel Closed",
                        context_lines=["Use the menu below to navigate."],
                        footer_lines=["<i>🧭 Home</i>"],
                    ),
                    reply_markup=build_back_to_home_keyboard(),
                )
        return

    if action == "panel":
        await ack_callback(callback)
        await reset_fsm_state(state)
        back_cb = await resolve_back_callback(
            state,
            msg,
            "admin:panel",
            default_back="menu:home",
        )
        text, queued_count, limit = await _render_dashboard_payload(
            governance_repository,
            queue_worker,
            settings,
        )
        await safe_edit_text(
            msg,
            text,
            reply_markup=build_admin_dashboard_keyboard(
                queued_count,
                limit,
                back_callback=back_cb,
            ),
        )
        return

    if action == "users":
        await ack_callback(callback)
        await reset_fsm_state(state)
        back_cb = await resolve_back_callback(
            state,
            msg,
            "admin:users",
            default_back="admin:panel",
        )
        users = await governance_repository.list_users()
        pending_reqs = await governance_repository.list_pending_access_requests()
        pending_user_ids = {req.telegram_id for req in pending_reqs}
        header = format_screen(
            title=f"👥 User Management ({len(users)} registered, {len(pending_reqs)} pending)",
            context_lines=["Select a user below or tap ➕ Add / Authorize User by ID:"],
            footer_lines=["<i>🧭 Home › Admin › Users</i>"],
        )
        await safe_edit_text(
            msg,
            header,
            reply_markup=build_user_list_keyboard(
                users,
                pending_user_ids=pending_user_ids,
                back_callback=back_cb,
            ),
        )
        return

    if action == "add_user":
        await ack_callback(callback)
        if state is not None:
            await state.set_state(AdminStates.awaiting_new_user_id)
        await safe_edit_text(
            msg,
            format_screen(
                title="➕ Authorize or Promote User by Telegram ID",
                context_lines=[
                    "Reply with the user's numeric Telegram ID and optional role:",
                    "<code>&lt;telegram_id&gt; [UPLOADER|MODERATOR|ADMIN]</code>",
                    "Example: <code>987654321 UPLOADER</code>",
                ],
                footer_lines=["<i>🧭 Home › Admin › Users › Add User</i>"],
            ),
            reply_markup=build_admin_subscreen_keyboard(back_callback="admin:users"),
        )
        return

    if action == "user" and target_id is not None:
        target_user = await governance_repository.get_user(target_id)
        if target_user is None:
            await ack_callback(callback, "User not found.", show_alert=True)
            return
        await ack_callback(callback)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"admin:user:{target_id}",
            default_back="admin:users",
        )
        albums_count, tracks_count = await governance_repository.count_user_uploads(target_id)
        user_logs = await governance_repository.list_user_audit_logs(target_id, limit=100)
        await safe_edit_text(
            msg,
            format_user_detail_card(
                target_user,
                albums_count,
                tracks_count,
                changes_count=len(user_logs),
            ),
            reply_markup=build_user_detail_keyboard(target_user, back_callback=back_cb),
        )
        return

    if action == "user_uploads" and target_id is not None:
        target_user = await governance_repository.get_user(target_id)
        if target_user is None:
            await ack_callback(callback, "User not found.", show_alert=True)
            return
        await ack_callback(callback)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"admin:user_uploads:{target_id}",
            default_back=f"admin:user:{target_id}",
        )
        tracks = await governance_repository.list_user_uploaded_tracks(target_id, limit=50)
        lines: list[str] = []
        if not tracks:
            lines.append("• No uploaded tracks recorded for this user yet.")
        for idx, rec in enumerate(tracks[:15], start=1):
            ts = rec.ingested_at.strftime("%Y-%m-%d %H:%M UTC")
            mod_badge = f" • ✏️ {rec.modification_count} edit(s)" if rec.modification_count > 0 else ""
            lines.append(
                f"#{idx} • <code>[{ts}]</code> <b>{html.escape(rec.artist)} — "
                f"{html.escape(rec.title)}</b> (<i>{html.escape(rec.album)}</i>){mod_badge}"
            )
        await safe_edit_text(
            msg,
            format_screen(
                title=f"📤 Uploaded Songs — {html.escape(target_user.mention_handle)} ({len(tracks)})",
                context_lines=lines,
                footer_lines=[
                    f"<i>User ID:</i> <code>{target_user.telegram_id}</code> • "
                    "<i>🧭 Home › Admin › Users › Uploads (Chronological)</i>"
                ],
                expandable=len(lines) > 3,
            ),
            reply_markup=build_user_uploads_keyboard(target_id, tracks, back_callback=back_cb),
        )
        return

    if action == "user_changes" and target_id is not None:
        target_user = await governance_repository.get_user(target_id)
        if target_user is None:
            await ack_callback(callback, "User not found.", show_alert=True)
            return
        await ack_callback(callback)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"admin:user_changes:{target_id}",
            default_back=f"admin:user:{target_id}",
        )
        logs = await governance_repository.list_user_audit_logs(target_id, limit=50)
        lines = []
        if not logs:
            lines.append("• No uploads, changes, or audit events recorded for this user yet.")
        for entry in logs[:25]:
            ts = entry.created_at.strftime("%Y-%m-%d %H:%M UTC")
            action_name = str(getattr(entry.action, "value", entry.action))
            target_label = html.escape(entry.target_entity or "-")
            detail_parts: list[str] = []
            if entry.details:
                for k in ("filename", "artist", "title", "album", "action_type", "new_role"):
                    if k in entry.details and entry.details[k] is not None:
                        detail_parts.append(f"{k}={entry.details[k]}")
            detail_str = f" ({html.escape(', '.join(detail_parts))})" if detail_parts else ""
            lines.append(
                f"• <code>[{ts}]</code> <b>{html.escape(action_name)}</b> "
                f"→ <code>{target_label}</code>{detail_str}"
            )
        await safe_edit_text(
            msg,
            format_screen(
                title=f"📝 Chronological Activity &amp; Changes — {html.escape(target_user.mention_handle)}",
                context_lines=lines,
                footer_lines=[
                    f"<i>User ID:</i> <code>{target_user.telegram_id}</code> • "
                    "<i>🧭 Home › Admin › Users › History</i>"
                ],
                expandable=True,
            ),
            reply_markup=build_admin_subscreen_keyboard(back_callback=back_cb),
        )
        return

    if action in {"promote", "demote"} and target_id is not None and target_role is not None:
        if governance_service is None:
            await ack_callback(callback, "Governance service unavailable.", show_alert=True)
            return
        await ack_callback(callback, f"Role updated to {target_role.value}.")
        updated_user = await governance_service.change_user_role(
            actor=current_user,
            target_telegram_id=target_id,
            new_role=target_role,
            reason=f"Manual {action.title()} via Admin Panel",
        )
        albums_count, tracks_count = await governance_repository.count_user_uploads(target_id)
        user_logs = await governance_repository.list_user_audit_logs(target_id, limit=100)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"admin:user:{target_id}",
            default_back="admin:users",
        )
        await safe_edit_text(
            msg,
            format_user_detail_card(
                updated_user,
                albums_count,
                tracks_count,
                changes_count=len(user_logs),
            ),
            reply_markup=build_user_detail_keyboard(updated_user, back_callback=back_cb),
        )
        return

    if action == "block" and target_id is not None and governance_service is not None:
        existing = await governance_repository.get_user(target_id)
        if existing is None:
            await ack_callback(callback, "User not found.", show_alert=True)
            return
        try:
            updated = await governance_service.set_user_blocked(
                actor=current_user,
                target_telegram_id=target_id,
                is_blocked=not existing.is_blocked,
            )
        except PermissionDeniedError as exc:
            await ack_callback(callback, str(exc), show_alert=True)
            return
        status_word = "blocked" if updated.is_blocked else "unblocked"
        await ack_callback(callback, f"User {status_word}.")
        albums_count, tracks_count = await governance_repository.count_user_uploads(target_id)
        user_logs = await governance_repository.list_user_audit_logs(target_id, limit=100)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"admin:user:{target_id}",
            default_back="admin:users",
        )
        await safe_edit_text(
            msg,
            format_user_detail_card(
                updated,
                albums_count,
                tracks_count,
                changes_count=len(user_logs),
            ),
            reply_markup=build_user_detail_keyboard(updated, back_callback=back_cb),
        )
        return

    if action == "resync" and target_id is not None:
        target_user = await governance_repository.get_user(target_id)
        if target_user is None or not target_user.is_oidc_linked or oidc_service is None:
            await ack_callback(callback, "User is not linked to Authentik OIDC.", show_alert=True)
            return
        await send_chat_action_safe(callback)
        await safe_edit_text(
            msg,
            format_loading_card(
                "Synchronizing OIDC Claims",
                f"Querying Authentik Identity Provider for user <code>{target_id}</code>...",
                "🧭 Home › Admin › Users › Detail",
            ),
        )
        outcome = await oidc_service.refresh_user_from_idp(target_user)
        await ack_callback(callback, f"OIDC synchronized (Role: {outcome.new_role.value}).")
        albums_count, tracks_count = await governance_repository.count_user_uploads(target_id)
        user_logs = await governance_repository.list_user_audit_logs(target_id, limit=100)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"admin:user:{target_id}",
            default_back="admin:users",
        )
        await safe_edit_text(
            msg,
            format_user_detail_card(
                outcome.user,
                albums_count,
                tracks_count,
                changes_count=len(user_logs),
            ),
            reply_markup=build_user_detail_keyboard(outcome.user, back_callback=back_cb),
        )
        return

    if action == "sync_all":
        if oidc_service is None:
            await ack_callback(callback, "OIDC service is not configured.", show_alert=True)
            return
        await send_chat_action_safe(callback)
        await safe_edit_text(
            msg,
            format_loading_card(
                "Running Authentik OIDC Sync",
                "Refreshing group claims across all linked accounts...",
                "🧭 Home › Admin Panel",
            ),
        )
        outcomes = await oidc_service.sync_all_linked_users()
        await ack_callback(
            callback, f"🔄 Synchronized {len(outcomes)} Authentik account(s).", show_alert=True
        )
        back_cb = await resolve_back_callback(
            state,
            msg,
            "admin:panel",
            default_back="menu:home",
        )
        text, queued_count, limit = await _render_dashboard_payload(
            governance_repository,
            queue_worker,
            settings,
        )
        await safe_edit_text(
            msg,
            text,
            reply_markup=build_admin_dashboard_keyboard(
                queued_count,
                limit,
                back_callback=back_cb,
            ),
        )
        return

    if action == "concurrency" and queue_worker is not None:
        next_limit = (queue_worker.concurrency_limit % 4) + 1
        await queue_worker.set_concurrency_limit(current_user, next_limit)
        await ack_callback(callback, f"Concurrency limit set to {next_limit}.")
        back_cb = await resolve_back_callback(
            state,
            msg,
            "admin:panel",
            default_back="menu:home",
        )
        text, queued_count, limit = await _render_dashboard_payload(
            governance_repository,
            queue_worker,
            settings,
        )
        await safe_edit_text(
            msg,
            text,
            reply_markup=build_admin_dashboard_keyboard(
                queued_count,
                limit,
                back_callback=back_cb,
            ),
        )
        return

    if action == "queue":
        await ack_callback(callback)
        back_cb = await resolve_back_callback(
            state,
            msg,
            "admin:queue",
            default_back="admin:panel",
        )
        text, kb = await render_queue_overview_screen(
            current_user,
            governance_repository,
            back_callback=back_cb,
            include_home=True,
            refresh_callback="admin:queue",
            breadcrumb="🧭 Home › Admin › Queue",
        )
        await safe_edit_text(msg, text, reply_markup=kb)
        return

    if action == "audit":
        await ack_callback(callback)
        back_cb = await resolve_back_callback(
            state,
            msg,
            "admin:audit",
            default_back="admin:panel",
        )
        logs = await governance_repository.list_audit_logs(limit=10)
        lines = []
        if not logs:
            lines.append("• No audit events recorded yet.")
        for entry in logs:
            lines.append(
                f"• <code>{html.escape(entry.action)}</code> by <code>{entry.actor_id}</code> "
                f"→ <code>{html.escape(entry.target_entity or '-')}</code>"
            )
        await safe_edit_text(
            msg,
            format_screen(
                title="📜 Recent Audit Logs",
                context_lines=lines,
                footer_lines=["<i>🧭 Home › Admin › Audit Logs</i>"],
                expandable=True,
            ),
            reply_markup=build_admin_subscreen_keyboard(back_callback=back_cb),
        )


@admin_router.message(Command("wipe"))
async def handle_wipe_command(
    message: Message,
    current_user: UserAccount | None = None,
    governance_service: GovernanceService | None = None,
    state: FSMContext | None = None,
) -> None:
    """Execute a destructive library catalog wipe (`SYSTEM_DESTRUCTIVE_WIPE`, OWNER only)."""
    await reset_fsm_state(state)

    if current_user is None or not has_permission(current_user, Permission.SYSTEM_DESTRUCTIVE_WIPE):
        await message.answer(
            format_screen(
                title="🔒 Owner Privileges Required",
                context_lines=["Only the system <code>OWNER</code> can execute a catalog wipe."],
                footer_lines=["<i>🧭 Home › Admin › System Wipe</i>"],
            )
        )
        return
    if governance_service is None:
        await message.answer(
            format_screen(
                title="⚙️ Governance Unavailable",
                context_lines=["Governance service is not initialized."],
                footer_lines=["<i>🧭 Home › Admin</i>"],
            )
        )
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() != "CONFIRM":
        await message.answer(
            format_screen(
                title="⚠️ Destructive Action Warning",
                context_lines=[
                    "Confirmation token <code>CONFIRM</code> is required to permanently clear "
                    "all catalog tracks and job records."
                ],
                footer_lines=["<i>🧭 Home › Admin › System Wipe</i>"],
            )
        )
        return

    removed = await governance_service.wipe_library_catalog(current_user)
    await message.answer(
        format_screen(
            title="🧨 System Catalog Wipe Executed",
            context_lines=[f"Removed <code>{removed}</code> catalog/track records."],
            footer_lines=["<i>🧭 Home › Admin › System Wipe</i>"],
        )
    )


@admin_router.callback_query(F.data.in_({"queue:mine", "queue:global"}))
async def handle_queue_view_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    state: FSMContext | None = None,
) -> None:
    """Mutate screen in-place to show the structured Queue Overview (`queue:mine` or `queue:global`)."""
    if current_user is None or governance_repository is None:
        await ack_callback(callback, "Queue unavailable.", show_alert=True)
        return

    await ack_callback(callback)
    await reset_fsm_state(state)
    route = callback.data or "queue:mine"
    back_cb = await resolve_back_callback(
        state,
        callback.message,
        route,
        default_back="menu:home",
    )
    text, kb = await render_queue_overview_screen(
        current_user,
        governance_repository,
        back_callback=back_cb,
        include_home=back_cb != "menu:home",
        refresh_callback=route,
    )
    await safe_edit_text(callback.message, text, reply_markup=kb)


@admin_router.callback_query(
    F.data.startswith(
        (
            "queue:pause:",
            "queue:unlock:",
            "queue:review:",
            "queue:view:",
            "queue:get_file:",
        )
    )
)
async def handle_queue_job_control_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    pipeline: IngestionPipeline | None = None,
    catalog_repository: CatalogRepositoryPort | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    state: FSMContext | None = None,
) -> None:
    """Pause, unlock, download queued file, manually review, or inspect a queued/paused job."""
    if not callback.data or current_user is None:
        await ack_callback(callback, "Queue control unavailable.", show_alert=True)
        return

    parts = callback.data.split(":", maxsplit=2)
    if len(parts) < 3:
        await ack_callback(callback, "Malformed job action.", show_alert=True)
        return

    action = parts[1]
    job_id = parts[2]
    msg = callback.message

    if action == "get_file":
        target_job = None
        if pipeline is not None:
            target_job = await pipeline.get_job(job_id)
        if target_job is None and catalog_repository is not None:
            target_job = await catalog_repository.get_job(job_id)
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        if target_job.user_id not in {None, current_user.telegram_id}:
            can_access_others = has_permission(current_user, Permission.VIEW_GLOBAL_QUEUE) or has_permission(
                current_user, Permission.RESOLVE_OTHERS_JOBS
            )
            if not can_access_others:
                await ack_callback(
                    callback,
                    "🔒 You do not have permission to download another user's queued file.",
                    show_alert=True,
                )
                return

        await send_chat_action_safe(callback, ChatAction.UPLOAD_DOCUMENT)
        delivered = await _deliver_queued_job_file(msg, target_job)
        if not delivered:
            await ack_callback(
                callback,
                "⚠️ Queued file is no longer available on disk.",
                show_alert=True,
            )
            return
        await ack_callback(callback, "📥 Sent queued file for review.")
        return

    if action == "view":
        target_job = None
        if pipeline is not None:
            target_job = await pipeline.get_job(job_id)
        elif catalog_repository is not None:
            target_job = await catalog_repository.get_job(job_id)
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        await ack_callback(callback)
        uploader_lbl: str | None = None
        if target_job.user_id and governance_repository is not None:
            u_obj = await governance_repository.get_user(target_job.user_id)
            uploader_lbl = (
                f"{u_obj.mention_handle} ({u_obj.telegram_id})"
                if u_obj is not None
                else f"User {target_job.user_id}"
            )
        pos = await queue_worker.get_queue_position(target_job.job_id) if queue_worker is not None else 0
        default_back = (
            "admin:queue" if has_permission(current_user, Permission.MANAGE_USERS) else "queue:mine"
        )
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:view:{target_job.job_id}",
            default_back=default_back,
        )
        await safe_edit_text(
            msg,
            format_job_inspector_card(target_job, uploader_label=uploader_lbl, position=pos),
            reply_markup=build_queued_job_keyboard(
                target_job.job_id,
                state=target_job.state,
                back_callback=back_cb,
                include_home=back_cb != "menu:home",
            ),
        )
        return

    if action == "pause":
        if queue_worker is None:
            await ack_callback(callback, "Queue worker unavailable.", show_alert=True)
            return
        try:
            paused_job = await queue_worker.pause_job(job_id, current_user)
        except PermissionDeniedError:
            await ack_callback(callback, "🔒 You do not have permission to pause this job.", show_alert=True)
            return
        except KeyError:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        await ack_callback(callback, "⏸️ Job paused for manual review.")
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:view:{paused_job.job_id}",
            default_back="queue:mine",
        )
        await safe_edit_text(
            msg,
            format_queued_job_card(paused_job, position=0),
            reply_markup=build_queued_job_keyboard(
                paused_job.job_id,
                state=JobState.PAUSED,
                back_callback=back_cb,
                include_home=back_cb != "menu:home",
            ),
        )
        return

    if action == "unlock":
        if queue_worker is None:
            await ack_callback(callback, "Queue worker unavailable.", show_alert=True)
            return
        try:
            unlocked_job = await queue_worker.unlock_job(job_id, current_user)
        except PermissionDeniedError:
            await ack_callback(callback, "🔒 You do not have permission to unlock this job.", show_alert=True)
            return
        except KeyError:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        pos = max(1, await queue_worker.get_queue_position(unlocked_job.job_id))
        await ack_callback(callback, "▶️ Job unlocked and returned to queue.")
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:view:{unlocked_job.job_id}",
            default_back="queue:mine",
        )
        await safe_edit_text(
            msg,
            format_queued_job_card(unlocked_job, position=pos),
            reply_markup=build_queued_job_keyboard(
                unlocked_job.job_id,
                state=JobState.QUEUED,
                back_callback=back_cb,
                include_home=back_cb != "menu:home",
            ),
        )
        return

    if action == "review":
        target_job = None
        if pipeline is not None:
            target_job = await pipeline.get_job(job_id)
        elif catalog_repository is not None:
            target_job = await catalog_repository.get_job(job_id)
        if target_job is None and queue_worker is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return

        if target_job is not None:
            can_resolve = (
                has_permission(current_user, Permission.RESOLVE_OTHERS_JOBS)
                if target_job.user_id != current_user.telegram_id
                else has_permission(current_user, Permission.RESOLVE_OWN_DISAMBIGUATION)
            )
            if not can_resolve:
                await ack_callback(
                    callback,
                    "🔒 You do not have permission to review this job.",
                    show_alert=True,
                )
                return

        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:review:{job_id}",
            default_back=f"queue:view:{job_id}",
        )

        if (
            queue_worker is not None
            and target_job is not None
            and not target_job.candidates
            and target_job.source_path.exists()
        ):
            await ack_callback(callback, "🔎 Analyzing file for manual review...")
            await send_chat_action_safe(callback)
            try:
                result = await queue_worker.review_job_manually(target_job.job_id, current_user)
            except (PermissionDeniedError, KeyError) as exc:
                await ack_callback(callback, str(exc), show_alert=True)
                return
            if result is not None:
                if state is not None:
                    await state.set_state(IngestionStates.awaiting_confirmation)
                    await state.update_data(job_id=result.job.job_id)
                await safe_edit_text(
                    msg,
                    format_ingestion_summary(result),
                    reply_markup=build_verification_keyboard(
                        result.job.job_id,
                        result.job.candidates,
                        back_callback=back_cb,
                    ),
                )
                return

        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        target_job.state = JobState.AWAITING_CONFIRMATION
        if catalog_repository is not None:
            await catalog_repository.save_job(target_job)
        await ack_callback(callback, f"🔎 Opened manual review for {target_job.job_id[:8]}.")
        if state is not None:
            await state.set_state(IngestionStates.awaiting_confirmation)
            await state.update_data(job_id=target_job.job_id)
        summary = format_ingestion_summary(IngestionResult(job=target_job, requires_user_confirmation=True))
        await safe_edit_text(
            msg,
            summary,
            reply_markup=build_verification_keyboard(
                target_job.job_id,
                target_job.candidates,
                back_callback=back_cb,
            ),
        )
        return


@admin_router.callback_query(F.data.startswith("queue:cancel:"))
async def handle_queue_cancel_callback(
    callback: CallbackQuery,
    current_user: UserAccount | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    state: FSMContext | None = None,
) -> None:
    """Cancel a queued job when the uploader or a moderator clicks `[ ❌ Cancel Job ]`."""
    if not callback.data or current_user is None or queue_worker is None:
        await ack_callback(callback, "Queue cancellation unavailable.", show_alert=True)
        return

    job_id = callback.data.split(":", maxsplit=2)[2]
    try:
        cancelled_job = await queue_worker.cancel_job(job_id, current_user)
    except PermissionDeniedError:
        await ack_callback(callback, "🔒 You do not have permission to cancel this job.", show_alert=True)
        return
    except KeyError:
        await ack_callback(callback, "Job not found.", show_alert=True)
        return

    await ack_callback(callback, "Job cancelled.")
    back_cb = await resolve_back_callback(
        state,
        callback.message,
        f"queue:cancel:{job_id}",
        default_back="queue:mine",
    )
    await reset_fsm_state(state)

    payload_name = html.escape(cancelled_job.original_filename or cancelled_job.source_path.name)
    await safe_edit_text(
        callback.message,
        format_screen(
            title="🛑 Job Cancelled",
            context_lines=[f"Payload: <code>{payload_name}</code>"],
            footer_lines=[f"<i>Job ID:</i> <code>{html.escape(cancelled_job.job_id)}</code>"],
        ),
        reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
    )
