"""Aiogram 3.x outer middleware enforcing RBAC permissions, guest gating, and OIDC staleness refresh."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from src.bot.keyboards.admin import build_guest_onboarding_keyboard
from src.bot.ui import safe_answer
from src.core.audit import format_guest_access_denied_card
from src.core.models import Permission, UserAccount
from src.core.oidc import AuthentikOidcService
from src.core.rbac import GovernanceService, has_permission

logger = logging.getLogger(__name__)


def _extract_telegram_user(event: TelegramObject) -> User | None:
    """Extract the `aiogram.types.User` from a `Message` or `CallbackQuery`."""
    if isinstance(event, (Message, CallbackQuery)):
        return event.from_user
    return None


def _is_media_upload(event: TelegramObject) -> bool:
    """Return True if `event` is a `Message` carrying an audio track or archive document."""
    return isinstance(event, Message) and (event.audio is not None or event.document is not None)


class RbacMiddleware(BaseMiddleware):
    """Enforces user onboarding, blocked-account rejection, OIDC 15m sync, and guest upload gating."""

    def __init__(
        self,
        governance_service: GovernanceService,
        oidc_service: AuthentikOidcService | None = None,
        oidc_enabled: bool = False,
    ) -> None:
        self._governance = governance_service
        self._oidc = oidc_service
        self._oidc_enabled = oidc_enabled

    def build_oidc_url_for_user(self, telegram_id: int) -> str | None:
        """Return an Authentik OAuth2 deep-link URL if OIDC is enabled and configured."""
        if self._oidc_enabled and self._oidc is not None:
            url, _ = self._oidc.build_authorization_url(telegram_id)
            return url
        return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Resolve `current_user`, enforce blocklist and upload RBAC, then invoke `handler`."""
        tg_user = _extract_telegram_user(event)
        if tg_user is None:
            return await handler(event, data)

        display_name = tg_user.full_name or f"User {tg_user.id}"
        user: UserAccount = await self._governance.ensure_user(
            telegram_id=tg_user.id,
            display_name=display_name,
            username=tg_user.username,
        )
        data["current_user"] = user

        if user.is_blocked:
            logger.info("Blocked user %s attempted interaction; rejecting.", user.telegram_id)
            if isinstance(event, Message):
                await event.answer(
                    "<b>🚫 Access Denied</b>\n"
                    "<blockquote>Your account has been blocked by an administrator.</blockquote>\n"
                    f"<i>Telegram ID:</i> <code>{user.telegram_id}</code>"
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("🚫 Your account has been blocked.", show_alert=True)
            return None

        if _is_media_upload(event):
            if user.is_oidc_linked and self._oidc is not None:
                try:
                    user = await self._oidc.refresh_user_if_stale(user.telegram_id)
                    data["current_user"] = user
                except Exception as exc:
                    logger.warning("OIDC staleness refresh failed for user %s: %s", user.telegram_id, exc)

            if user.is_blocked:
                if isinstance(event, Message):
                    await event.answer(
                        "<b>🚫 Access Denied</b>\n"
                        "<blockquote>Your account has been deactivated.</blockquote>\n"
                        f"<i>Telegram ID:</i> <code>{user.telegram_id}</code>"
                    )
                return None

            if not has_permission(user, Permission.SUBMIT_INGESTION):
                logger.info(
                    "Discarding unauthorized media upload from guest user %s",
                    user.telegram_id,
                )
                if isinstance(event, Message):
                    oidc_url = self.build_oidc_url_for_user(user.telegram_id)
                    await safe_answer(
                        event,
                        format_guest_access_denied_card(user.telegram_id),
                        reply_markup=build_guest_onboarding_keyboard(
                            oidc_url,
                            telegram_id=user.telegram_id,
                        ),
                    )
                return None

        return await handler(event, data)
