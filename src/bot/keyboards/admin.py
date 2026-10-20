"""Keyboard builders for Persistent Bottom Menu, Onboarding, Access Requests, Queue, and Admin Panel."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from src.core.models import (
    IngestionJob,
    JobState,
    LibraryTrackRecord,
    Permission,
    UserAccount,
    UserRole,
)
from src.core.rbac import has_permission, next_demotion_role, next_promotion_role

BTN_MAIN_SONGS = "🎵 Songs"
BTN_MAIN_QUEUE = "⏳ Queue"
BTN_MAIN_SSO = "🔐 Authentik SSO"
BTN_MAIN_ADMIN = "👮 Admin panel"
BTN_MAIN_INFO = "🤖 Bot information"

BTN_BACK_LABEL = "◀ Back"
BTN_HOME_LABEL = "🏠 Menu"

RESERVED_MENU_BUTTONS: frozenset[str] = frozenset(
    {
        BTN_MAIN_SONGS,
        BTN_MAIN_QUEUE,
        BTN_MAIN_SSO,
        BTN_MAIN_ADMIN,
        BTN_MAIN_INFO,
    }
)


class MenuCallback(CallbackData, prefix="menu"):
    """Callback schema for top-level single-screen navigation (`menu:<action>`)."""

    action: str


class HelpTopicCallback(CallbackData, prefix="help"):
    """Callback schema for viewing detailed module documentation (`help:<topic>`)."""

    topic: str


class AccessReqSimpleCallback(CallbackData, prefix="access_req"):
    """Callback schema for guest access request actions without parameters (`access_req:<action>`)."""

    action: str


class AccessReqDenyCallback(CallbackData, prefix="access_req"):
    """Callback schema for denying a pending access request (`access_req:deny:<request_id>`)."""

    action: str = "deny"
    request_id: int


class AccessReqApproveCallback(CallbackData, prefix="access_req"):
    """Callback schema for approving an access request (`access_req:approve:<request_id>:<role>`)."""

    action: str = "approve"
    request_id: int
    role: str


class QueueActionCallback(CallbackData, prefix="queue"):
    """Callback schema for viewing personal or global queue (`queue:<action>`)."""

    action: str


class QueueJobCallback(CallbackData, prefix="queue"):
    """Callback schema for controlling or inspecting a specific job (`queue:<action>:<job_id>`)."""

    action: str
    job_id: str


class QueueCancelCallback(CallbackData, prefix="queue"):
    """Callback schema for cancelling a queued job (`queue:cancel:<job_id>`)."""

    action: str = "cancel"
    job_id: str


class AdminActionCallback(CallbackData, prefix="admin"):
    """Callback schema for top-level `/admin` panel actions (`admin:<action>`)."""

    action: str


class AdminTargetCallback(CallbackData, prefix="admin"):
    """Callback schema for `/admin` actions targeting a user or request ID (`admin:<action>:<target_id>`)."""

    action: str
    target_id: int


class AdminRoleCallback(CallbackData, prefix="admin"):
    """Callback schema for role promotion/demotion (`admin:<action>:<target_id>:<role>`)."""

    action: str
    target_id: int
    role: str


class AdminJobCallback(CallbackData, prefix="admin"):
    """Callback schema for moderator resolution of a paused job (`admin:resolve_job:<job_id>`)."""

    action: str = "resolve_job"
    job_id: str


def format_toggle_label(label: str, is_active: bool) -> str:
    """Format a toggle button label using standard `✅ [Active]` vs `⬜ [Disabled]` states."""
    prefix = "✅ [Active]" if is_active else "⬜ [Disabled]"
    return f"{prefix} {label}"


def append_escape_row(
    builder: InlineKeyboardBuilder,
    *,
    back_callback: str,
    include_home: bool = False,
) -> InlineKeyboardBuilder:
    """Append the 2-tier escape navigation row (`◀ Back` for Level 1; `◀ Back` + `🏠 Menu` for Level >= 2)."""
    buttons: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text=BTN_BACK_LABEL,
            callback_data=back_callback,
        )
    ]
    if include_home:
        buttons.append(
            InlineKeyboardButton(
                text=BTN_HOME_LABEL,
                callback_data=MenuCallback(action="home").pack(),
            )
        )
    builder.row(*buttons)
    return builder


def build_home_menu_inline_keyboard(
    user: UserAccount | None = None,
    *,
    web_app_url: str | None = None,
    include_back: bool = False,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the root single-screen navigation inline keyboard for `/start` and `🏠 Menu`."""
    builder = InlineKeyboardBuilder()
    if web_app_url and web_app_url.strip():
        builder.button(text="🚀 Open Web App", web_app=WebAppInfo(url=web_app_url.strip()))
    builder.button(text="🎵 Songs", callback_data="songs:list")
    builder.button(text="📡 Radar", callback_data="radar:page:1")
    builder.button(text="⏳ Queue", callback_data=QueueActionCallback(action="mine").pack())
    builder.button(text="🔐 Authentik SSO", callback_data=MenuCallback(action="sso").pack())
    builder.button(text="🟢 Status", callback_data=MenuCallback(action="status").pack())
    if user is not None and has_permission(user, Permission.MANAGE_USERS):
        builder.button(text="👮 Admin Panel", callback_data=AdminActionCallback(action="panel").pack())
    builder.button(text="⚙️ Settings", callback_data=MenuCallback(action="settings").pack())
    builder.button(text="ℹ️ Help", callback_data=MenuCallback(action="help").pack())
    if web_app_url and web_app_url.strip():
        builder.adjust(1, 2)
    else:
        builder.adjust(2)
    if include_back:
        effective_back = back_callback or MenuCallback(action="home").pack()
        append_escape_row(
            builder,
            back_callback=effective_back,
            include_home=effective_back != MenuCallback(action="home").pack(),
        )
    return builder.as_markup()


def build_help_menu_keyboard(
    user: UserAccount | None = None,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the interactive Help & Documentation Hub keyboard (`help:<topic>` buttons)."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📥 Ingestion & Pipeline",
        callback_data=HelpTopicCallback(topic="ingestion").pack(),
    )
    builder.button(
        text="🎵 Songs & Editor",
        callback_data=HelpTopicCallback(topic="songs").pack(),
    )
    builder.button(
        text="⏳ Queue & Review",
        callback_data=HelpTopicCallback(topic="queue").pack(),
    )
    builder.button(
        text="🔐 Authentik SSO",
        callback_data=HelpTopicCallback(topic="sso").pack(),
    )
    builder.button(
        text="🟢 Status & ID Lookup",
        callback_data=HelpTopicCallback(topic="status").pack(),
    )
    if user is not None and has_permission(user, Permission.MANAGE_USERS):
        builder.button(
            text="👮 Admin Panel",
            callback_data=HelpTopicCallback(topic="admin").pack(),
        )
    builder.adjust(2)
    effective_back = back_callback or MenuCallback(action="home").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_help_topic_keyboard(
    topic: str,
    user: UserAccount | None = None,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build a Help Topic screen keyboard with a clearly labeled module launcher and `◀ Back` to Help."""
    builder = InlineKeyboardBuilder()
    launcher_map: dict[str, tuple[str, str]] = {
        "songs": ("🚀 Open Songs Browser", "songs:list"),
        "queue": ("🚀 Open Ingestion Queue", QueueActionCallback(action="mine").pack()),
        "sso": ("🚀 Open Authentik SSO", MenuCallback(action="sso").pack()),
        "status": ("🚀 Open System Status", MenuCallback(action="status").pack()),
    }
    if user is not None and has_permission(user, Permission.MANAGE_USERS):
        launcher_map["admin"] = ("🚀 Open Admin Panel", AdminActionCallback(action="panel").pack())

    if topic in launcher_map:
        btn_label, btn_cb = launcher_map[topic]
        builder.row(
            InlineKeyboardButton(
                text=btn_label,
                callback_data=btn_cb,
            )
        )

    effective_back = back_callback or MenuCallback(action="help").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=True,
    )
    return builder.as_markup()


def build_back_to_home_keyboard(
    *,
    back_callback: str | None = None,
    include_home: bool = False,
) -> InlineKeyboardMarkup:
    """Build an escape keyboard with `◀ Back` returning to `back_callback` (or Home)."""
    builder = InlineKeyboardBuilder()
    effective_back = back_callback or MenuCallback(action="home").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=include_home or effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_guest_onboarding_keyboard(
    oidc_auth_url: str | None = None,
    telegram_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Create the guest onboarding keyboard with Authentik SSO, manual access request, and ID copy button."""
    builder = InlineKeyboardBuilder()
    if oidc_auth_url:
        oidc_btn = InlineKeyboardButton(
            text="🔐 Link via Authentik",
            url=oidc_auth_url,
        )
    else:
        oidc_btn = InlineKeyboardButton(
            text="🔐 Link via Authentik",
            callback_data=AccessReqSimpleCallback(action="oidc_disabled").pack(),
        )

    request_btn = InlineKeyboardButton(
        text="🙋 Request Access",
        callback_data=AccessReqSimpleCallback(action="request").pack(),
    )
    builder.row(oidc_btn, request_btn)
    if telegram_id is not None:
        builder.row(
            InlineKeyboardButton(
                text=f"📋 Copy Telegram ID ({telegram_id})",
                copy_text=CopyTextButton(text=str(telegram_id)),
            )
        )
    return builder.as_markup()


def build_oidc_link_keyboard(
    oidc_auth_url: str | None = None,
    subject: str | None = None,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Create an inline keyboard for linking or inspecting an Authentik OIDC account."""
    builder = InlineKeyboardBuilder()
    if oidc_auth_url:
        builder.row(
            InlineKeyboardButton(
                text="🔐 Open Authentik SSO Login",
                url=oidc_auth_url,
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text="🔐 Link via Authentik",
                callback_data=AccessReqSimpleCallback(action="oidc_disabled").pack(),
            )
        )
    if subject:
        builder.row(
            InlineKeyboardButton(
                text=f"📋 Copy Subject ({subject[:12]})",
                copy_text=CopyTextButton(text=subject),
            ),
            InlineKeyboardButton(
                text="❌ Disconnect SSO",
                callback_data=AccessReqSimpleCallback(action="oidc_disconnect_self").pack(),
            ),
        )
    effective_back = back_callback or MenuCallback(action="home").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_main_menu_keyboard(user: UserAccount) -> ReplyKeyboardMarkup:
    """Create the persistent bottom `ReplyKeyboardMarkup` dock under the Telegram text input field."""
    builder = ReplyKeyboardBuilder()
    row1: list[KeyboardButton] = []
    if has_permission(user, Permission.VIEW_CATALOG):
        row1.append(KeyboardButton(text=BTN_MAIN_SONGS))
    if has_permission(user, Permission.SUBMIT_INGESTION):
        row1.append(KeyboardButton(text=BTN_MAIN_QUEUE))
    row1.append(KeyboardButton(text=BTN_MAIN_SSO))
    builder.row(*row1)
    if has_permission(user, Permission.MANAGE_USERS):
        builder.row(
            KeyboardButton(text=BTN_MAIN_ADMIN),
            KeyboardButton(text=BTN_MAIN_INFO),
        )
    else:
        builder.row(KeyboardButton(text=BTN_MAIN_INFO))

    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def build_access_request_approval_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Create the Admin Log Channel inline keyboard for approving or denying an access request."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Approve: Listener",
            callback_data=AccessReqApproveCallback(
                request_id=request_id,
                role=UserRole.LISTENER.value,
            ).pack(),
        ),
        InlineKeyboardButton(
            text="Approve: Uploader",
            callback_data=AccessReqApproveCallback(
                request_id=request_id,
                role=UserRole.UPLOADER.value,
            ).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="Approve: Moderator",
            callback_data=AccessReqApproveCallback(
                request_id=request_id,
                role=UserRole.MODERATOR.value,
            ).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Deny",
            callback_data=AccessReqDenyCallback(request_id=request_id).pack(),
        ),
    )
    return builder.as_markup()


def build_queued_job_keyboard(
    job_id: str,
    *,
    state: JobState = JobState.QUEUED,
    back_callback: str | None = None,
    include_home: bool = False,
    web_app_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Create the ordered control keyboard for a queued, paused, or inspected ingestion job.

    Row 1: Retrieval & Tagging Review (`📥 Get Queued File` | `🔎 Manual Review & Tag`)
    Row 2: Queue Execution Lifecycle (`⏸️ Pause / Hold` or `▶️ Unlock & Resume` | `❌ Cancel Job`)
    Row 3: Identifier Utility (`📋 Copy Job ID`)
    Row 4: Navigation (`◀ Back` | `🏠 Menu`)
    """
    builder = InlineKeyboardBuilder()
    if web_app_url and web_app_url.strip():
        builder.row(
            InlineKeyboardButton(
                text="🚀 View in Mini App",
                web_app=WebAppInfo(url=f"{web_app_url.strip().rstrip('/')}?startapp=job_{job_id}"),
            )
        )
    if state in {
        JobState.QUEUED,
        JobState.PROCESSING,
        JobState.PAUSED,
        JobState.AWAITING_CONFIRMATION,
        JobState.AWAITING_INPUT,
        JobState.EXTRACTING,
        JobState.FINGERPRINTING,
        JobState.ENRICHING_METADATA,
    }:
        # --- Row 1: File Retrieval & Interactive Review/Tagging ---
        builder.row(
            InlineKeyboardButton(
                text="📥 Get Queued File",
                callback_data=QueueJobCallback(action="get_file", job_id=job_id).pack(),
            ),
            InlineKeyboardButton(
                text="🔎 Manual Review & Tag",
                callback_data=QueueJobCallback(action="review", job_id=job_id).pack(),
            ),
        )
        # --- Row 2: Queue State Lifecycle (Hold/Resume & Cancel) ---
        if state == JobState.PAUSED:
            lifecycle_btn = InlineKeyboardButton(
                text="▶️ Unlock & Resume",
                callback_data=QueueJobCallback(action="unlock", job_id=job_id).pack(),
            )
        else:
            lifecycle_btn = InlineKeyboardButton(
                text="⏸️ Pause / Hold",
                callback_data=QueueJobCallback(action="pause", job_id=job_id).pack(),
            )
        builder.row(
            lifecycle_btn,
            InlineKeyboardButton(
                text="❌ Cancel Job",
                callback_data=QueueCancelCallback(job_id=job_id).pack(),
            ),
        )
        # --- Row 3: Utility ---
        builder.row(
            InlineKeyboardButton(
                text=f"📋 Copy Job ID ({job_id[:8]})",
                copy_text=CopyTextButton(text=job_id),
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text="📥 Get Queued File",
                callback_data=QueueJobCallback(action="get_file", job_id=job_id).pack(),
            ),
            InlineKeyboardButton(
                text=f"📋 Copy Job ID ({job_id[:8]})",
                copy_text=CopyTextButton(text=job_id),
            ),
        )

    effective_back = back_callback or MenuCallback(action="home").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=include_home or effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_batch_queued_keyboard(
    job_ids: list[str],
    *,
    web_app_url: str | None = None,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build a consolidated inline keyboard for a batch of queued files."""
    builder = InlineKeyboardBuilder()
    if web_app_url and web_app_url.strip():
        builder.row(
            InlineKeyboardButton(
                text="🚀 View in Mini App",
                web_app=WebAppInfo(url=f"{web_app_url.strip().rstrip('/')}?startapp=queue"),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="📋 View Queue",
            callback_data=QueueActionCallback(action="mine").pack(),
        ),
    )
    effective_back = back_callback or MenuCallback(action="home").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_admin_dashboard_keyboard(
    queued_count: int,
    concurrency_limit: int,
    pending_tags_count: int = 0,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Create the Administrator Control Panel keyboard (`/admin`, Specification Section 7.1)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="👥 User Management",
            callback_data=AdminActionCallback(action="users").pack(),
        ),
        InlineKeyboardButton(
            text=f"⏳ View Queue ({queued_count})",
            callback_data=AdminActionCallback(action="queue").pack(),
        ),
    )
    parallel_toggle = format_toggle_label(f"Concurrency: [{concurrency_limit}]", concurrency_limit > 1)
    builder.row(
        InlineKeyboardButton(
            text="🔄 Run Authentik Sync",
            callback_data=AdminActionCallback(action="sync_all").pack(),
        ),
        InlineKeyboardButton(
            text=parallel_toggle,
            callback_data=AdminActionCallback(action="concurrency").pack(),
        ),
    )
    if pending_tags_count > 0:
        builder.row(
            InlineKeyboardButton(
                text=f"🏷 Review Tag Edits ({pending_tags_count})",
                callback_data=AdminActionCallback(action="tags").pack(),
            )
        )
    effective_back = back_callback or MenuCallback(action="home").pack()
    builder.row(
        InlineKeyboardButton(
            text="📜 Audit Logs",
            callback_data=AdminActionCallback(action="audit").pack(),
        ),
        InlineKeyboardButton(
            text=BTN_BACK_LABEL,
            callback_data=effective_back,
        ),
        InlineKeyboardButton(
            text="❌ Close Panel",
            callback_data=AdminActionCallback(action="close").pack(),
        ),
    )
    return builder.as_markup()


def build_admin_subscreen_keyboard(
    back_callback: str = "admin:panel",
    *,
    include_home: bool = True,
) -> InlineKeyboardMarkup:
    """Create a 2-tier escape keyboard (`◀ Back` and `🏠 Menu`) for `/admin` sub-screens."""
    builder = InlineKeyboardBuilder()
    append_escape_row(builder, back_callback=back_callback, include_home=include_home)
    return builder.as_markup()


def build_tag_correction_review_keyboard(
    request_id: int,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Create an inline keyboard for moderators/admins to approve or reject a tag correction request."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Approve & Apply",
            callback_data=AdminTargetCallback(action="tag_approve", target_id=request_id).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Reject",
            callback_data=AdminTargetCallback(action="tag_reject", target_id=request_id).pack(),
        ),
    )
    effective_back = back_callback or AdminActionCallback(action="panel").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_paused_jobs_keyboard(
    paused_jobs: list[IngestionJob],
    *,
    queued_jobs: list[IngestionJob] | None = None,
    active_jobs: list[IngestionJob] | None = None,
    back_callback: str | None = None,
    include_home: bool | None = None,
    refresh_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Create the Queue Menu inline keyboard with 1 row per queue item (paused, queued, and active)."""
    builder = InlineKeyboardBuilder()
    seen_ids: set[str] = set()
    for job in paused_jobs[:10]:
        seen_ids.add(job.job_id)
        filename = job.original_filename or job.source_path.name
        if job.state == JobState.PAUSED:
            label = f"⏸️ Held: {filename} ({job.job_id[:8]})"
            cb = QueueJobCallback(action="view", job_id=job.job_id).pack()
        else:
            label = f"🔍 Resolve: {filename} ({job.job_id[:8]})"
            cb = AdminJobCallback(job_id=job.job_id).pack()
        builder.row(
            InlineKeyboardButton(
                text=label[:64],
                callback_data=cb,
            )
        )
    if queued_jobs:
        for idx, job in enumerate(queued_jobs[:10], start=1):
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            filename = job.original_filename or job.source_path.name
            label = f"⏳ #{idx}: {filename} ({job.job_id[:8]})"
            builder.row(
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=QueueJobCallback(action="view", job_id=job.job_id).pack(),
                )
            )
    if active_jobs:
        for job in active_jobs[:5]:
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            filename = job.original_filename or job.source_path.name
            label = f"⚙️ Active: {filename} ({job.job_id[:8]})"
            builder.row(
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=QueueJobCallback(action="view", job_id=job.job_id).pack(),
                )
            )
    if refresh_callback:
        builder.row(
            InlineKeyboardButton(
                text="🔄 Refresh Queue",
                callback_data=refresh_callback,
            )
        )
    effective_back = back_callback or AdminActionCallback(action="panel").pack()
    effective_home = (
        include_home if include_home is not None else (effective_back != MenuCallback(action="home").pack())
    )
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_home,
    )
    return builder.as_markup()


def build_user_list_keyboard(
    users: list[UserAccount],
    pending_user_ids: set[int] | None = None,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Create an inline keyboard listing registered users and an option to add a user by ID."""
    pending_ids = pending_user_ids or set()
    builder = InlineKeyboardBuilder()
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
        builder.row(
            InlineKeyboardButton(
                text=label[:64],
                callback_data=AdminTargetCallback(action="user", target_id=user.telegram_id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="➕ Add / Authorize User by ID",
            callback_data=AdminActionCallback(action="add_user").pack(),
        )
    )
    effective_back = back_callback or AdminActionCallback(action="panel").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_user_detail_keyboard(
    target_user: UserAccount,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Create the User Detail Management Sub-Screen keyboard (Specification Section 7.1)."""
    builder = InlineKeyboardBuilder()

    role_buttons: list[InlineKeyboardButton] = []
    promo = next_promotion_role(target_user.role)
    if promo is not None:
        role_buttons.append(
            InlineKeyboardButton(
                text=f"⬆️ Promote to {promo.value.title()}",
                callback_data=AdminRoleCallback(
                    action="promote",
                    target_id=target_user.telegram_id,
                    role=promo.value,
                ).pack(),
            )
        )
    demo = next_demotion_role(target_user.role)
    if demo is not None:
        role_buttons.append(
            InlineKeyboardButton(
                text=f"⬇️ Demote to {demo.value.title()}",
                callback_data=AdminRoleCallback(
                    action="demote",
                    target_id=target_user.telegram_id,
                    role=demo.value,
                ).pack(),
            )
        )
    if role_buttons:
        builder.row(*role_buttons)

    access_toggle_label = format_toggle_label("Account Access", not target_user.is_blocked)
    builder.row(
        InlineKeyboardButton(
            text=access_toggle_label,
            callback_data=AdminTargetCallback(action="block", target_id=target_user.telegram_id).pack(),
        ),
        InlineKeyboardButton(
            text="🔗 Force OIDC Re-sync",
            callback_data=AdminTargetCallback(action="resync", target_id=target_user.telegram_id).pack(),
        ),
    )
    if target_user.is_oidc_linked:
        override_label = (
            "🔓 Unlock Role (Sync OIDC)" if target_user.oidc_role_override else "🔒 Lock Role (Override OIDC)"
        )
        builder.row(
            InlineKeyboardButton(
                text=override_label,
                callback_data=AdminTargetCallback(
                    action="toggle_override",
                    target_id=target_user.telegram_id,
                ).pack(),
            ),
            InlineKeyboardButton(
                text="❌ Disconnect OIDC",
                callback_data=AdminTargetCallback(
                    action="oidc_disconnect",
                    target_id=target_user.telegram_id,
                ).pack(),
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text="📤 User Uploads",
            callback_data=AdminTargetCallback(
                action="user_uploads",
                target_id=target_user.telegram_id,
            ).pack(),
        ),
        InlineKeyboardButton(
            text="📝 User Changes & History",
            callback_data=AdminTargetCallback(
                action="user_changes",
                target_id=target_user.telegram_id,
            ).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"📋 Copy User ID ({target_user.telegram_id})",
            copy_text=CopyTextButton(text=str(target_user.telegram_id)),
        )
    )
    effective_back = back_callback or AdminActionCallback(action="users").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_user_uploads_keyboard(
    target_user_id: int,
    tracks: list[LibraryTrackRecord],
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build an inline keyboard listing a user's uploaded songs in chronological order."""
    builder = InlineKeyboardBuilder()
    for rec in tracks[:12]:
        sid = rec.id.strip()[:12]
        year_part = f" ({rec.year})" if rec.year else ""
        label = f"🎵 {rec.artist} - {rec.title} | {rec.album}{year_part}"
        if len(label) > 60:
            label = f"{label[:57]}..."
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"songs:view:{sid}",
            )
        )
    effective_back = back_callback or AdminTargetCallback(action="user", target_id=target_user_id).pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()
