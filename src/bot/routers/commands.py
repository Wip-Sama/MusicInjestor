"""Standard Telegram command and persistent bottom-menu handlers (/start, /help, /status, /queue, /sso)."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from src.bot.keyboards.admin import (
    BTN_MAIN_INFO,
    BTN_MAIN_QUEUE,
    BTN_MAIN_SSO,
    build_guest_onboarding_keyboard,
    build_main_menu_keyboard,
    build_oidc_link_keyboard,
    build_queued_job_keyboard,
)
from src.config.settings import AppSettings
from src.core.audit import format_guest_access_denied_card, format_queued_job_card
from src.core.models import Permission, UserAccount, UserRole
from src.core.oidc import AuthentikOidcService
from src.core.ports import GovernanceRepositoryPort
from src.core.queue import IngestionQueueWorker
from src.core.rbac import has_permission

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


@commands_router.message(Command("start"))
async def handle_start(
    message: Message,
    current_user: UserAccount | None = None,
    oidc_service: AuthentikOidcService | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Intercept GUEST users with the onboarding screen or present the simple upload prompt & bottom menu."""
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


@commands_router.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Provide usage instructions and pipeline overview."""
    await message.answer(
        "<b>Available Commands:</b>\n"
        "/start - Show upload prompt &amp; persistent bottom navigation\n"
        "/admin - Open Administrator Control Panel (ADMIN / OWNER)\n"
        "/adduser <code>&lt;id&gt; [ROLE]</code> - Authorize or promote a Telegram user ID (ADMIN / OWNER)\n"
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
) -> None:
    """Report current operational, RBAC, OIDC, and queue status of the ingestion service."""
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
) -> None:
    """Display Authentik OIDC pairing status and provide an inline SSO login button."""
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
) -> None:
    """Display the user's queued jobs (or global queue for MODERATOR+)."""
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

    if not jobs:
        await message.answer(f"✅ <b>Ingestion Queue Empty</b> ({len(active_jobs)} active job(s) in flight).")
        return

    first_job = jobs[0]
    await message.answer(
        format_queued_job_card(first_job, position=1, active_job_filename=active_name),
        reply_markup=build_queued_job_keyboard(first_job.job_id),
    )
