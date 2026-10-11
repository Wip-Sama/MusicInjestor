"""Keyboard builders for Persistent Bottom Menu, Onboarding, Access Requests, Queue, and Admin Panel."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from src.core.models import Permission, UserAccount, UserRole
from src.core.rbac import has_permission, next_demotion_role, next_promotion_role

BTN_MAIN_QUEUE = "⏳ Queue"
BTN_MAIN_SSO = "🔐 Authentik SSO"
BTN_MAIN_ADMIN = "👮 Admin panel"
BTN_MAIN_INFO = "🤖 Bot information"


def build_guest_onboarding_keyboard(
    oidc_auth_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Create the guest onboarding keyboard with Authentik SSO deep-link and manual access request."""
    if oidc_auth_url:
        oidc_btn = InlineKeyboardButton(
            text="🔐 Link via Authentik",
            url=oidc_auth_url,
        )
    else:
        oidc_btn = InlineKeyboardButton(
            text="🔐 Link via Authentik",
            callback_data="access_req:oidc_disabled",
        )

    request_btn = InlineKeyboardButton(
        text="🙋 Request Access",
        callback_data="access_req:request",
    )
    return InlineKeyboardMarkup(inline_keyboard=[[oidc_btn, request_btn]])


def build_oidc_link_keyboard(oidc_auth_url: str | None = None) -> InlineKeyboardMarkup:
    """Create an inline button for linking or re-linking an Authentik OIDC account."""
    if oidc_auth_url:
        btn = InlineKeyboardButton(
            text="🔐 Open Authentik SSO Login",
            url=oidc_auth_url,
        )
    else:
        btn = InlineKeyboardButton(
            text="🔐 Link via Authentik",
            callback_data="access_req:oidc_disabled",
        )
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


def build_main_menu_keyboard(user: UserAccount) -> ReplyKeyboardMarkup:
    """Create the persistent bottom `ReplyKeyboardMarkup` dock under the Telegram text input field."""
    rows: list[list[KeyboardButton]] = [
        [
            KeyboardButton(text=BTN_MAIN_QUEUE),
            KeyboardButton(text=BTN_MAIN_SSO),
        ]
    ]
    if has_permission(user, Permission.MANAGE_USERS):
        rows.append(
            [
                KeyboardButton(text=BTN_MAIN_ADMIN),
                KeyboardButton(text=BTN_MAIN_INFO),
            ]
        )
    else:
        rows.append([KeyboardButton(text=BTN_MAIN_INFO)])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def build_access_request_approval_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Create the Admin Log Channel inline keyboard for approving or denying an access request."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Approve: Uploader",
                    callback_data=f"access_req:approve:{request_id}:{UserRole.UPLOADER.value}",
                ),
                InlineKeyboardButton(
                    text="Approve: Moderator",
                    callback_data=f"access_req:approve:{request_id}:{UserRole.MODERATOR.value}",
                ),
                InlineKeyboardButton(
                    text="❌ Deny",
                    callback_data=f"access_req:deny:{request_id}",
                ),
            ]
        ]
    )


def build_queued_job_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Create the in-chat cancel button for a queued ingestion job (Section 4.2)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel Job",
                    callback_data=f"queue:cancel:{job_id}",
                )
            ]
        ]
    )


def build_admin_dashboard_keyboard(
    queued_count: int,
    concurrency_limit: int,
) -> InlineKeyboardMarkup:
    """Create the Administrator Control Panel keyboard (`/admin`, Specification Section 7.1)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 User Management",
                    callback_data="admin:users",
                ),
                InlineKeyboardButton(
                    text=f"⏳ View Queue ({queued_count})",
                    callback_data="admin:queue",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Run Authentik Sync",
                    callback_data="admin:sync_all",
                ),
                InlineKeyboardButton(
                    text=f"⚙️ Concurrency: [{concurrency_limit}]",
                    callback_data="admin:concurrency",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📜 Audit Logs",
                    callback_data="admin:audit",
                ),
                InlineKeyboardButton(
                    text="❌ Close Panel",
                    callback_data="admin:close",
                ),
            ],
        ]
    )


def build_user_list_keyboard(
    users: list[UserAccount],
    pending_user_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    """Create an inline keyboard listing registered users and an option to add a user by ID."""
    pending_ids = pending_user_ids or set()
    rows: list[list[InlineKeyboardButton]] = []
    for user in users[:15]:
        is_pending = user.telegram_id in pending_ids
        if user.is_blocked:
            status_icon = "🚫"
        elif is_pending:
            status_icon = "🔔"
        else:
            status_icon = "👤"
        pending_suffix = " (Pending)" if is_pending else ""
        label = f"{status_icon} {user.mention_handle} [{user.role.value}]{pending_suffix}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"admin:user:{user.telegram_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Add / Authorize User by ID",
                callback_data="admin:add_user",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Control Panel",
                callback_data="admin:panel",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_user_detail_keyboard(target_user: UserAccount) -> InlineKeyboardMarkup:
    """Create the User Detail Management Sub-Screen keyboard (Specification Section 7.1)."""
    rows: list[list[InlineKeyboardButton]] = []

    role_buttons: list[InlineKeyboardButton] = []
    promo = next_promotion_role(target_user.role)
    if promo is not None:
        role_buttons.append(
            InlineKeyboardButton(
                text=f"⬆️ Promote to {promo.value.title()}",
                callback_data=f"admin:promote:{target_user.telegram_id}:{promo.value}",
            )
        )
    demo = next_demotion_role(target_user.role)
    if demo is not None:
        role_buttons.append(
            InlineKeyboardButton(
                text=f"⬇️ Demote to {demo.value.title()}",
                callback_data=f"admin:demote:{target_user.telegram_id}:{demo.value}",
            )
        )
    if role_buttons:
        rows.append(role_buttons)

    block_label = "✅ Unblock User" if target_user.is_blocked else "🚫 Block / Ban User"
    rows.append(
        [
            InlineKeyboardButton(
                text=block_label,
                callback_data=f"admin:block:{target_user.telegram_id}",
            ),
            InlineKeyboardButton(
                text="🔗 Force OIDC Re-sync",
                callback_data=f"admin:resync:{target_user.telegram_id}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to User List",
                callback_data="admin:users",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
