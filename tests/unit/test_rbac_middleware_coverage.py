"""Targeted coverage tests for RbacMiddleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from src.bot.middlewares.rbac import RbacMiddleware, _extract_telegram_user
from src.core.models import UserAccount, UserRole


@pytest.mark.anyio
async def test_extract_telegram_user_non_message() -> None:
    """Verify _extract_telegram_user returns None for unsupported Telegram objects."""
    assert _extract_telegram_user(TelegramObject()) is None


def test_build_oidc_url_for_user() -> None:
    """Verify build_oidc_url_for_user builds auth URL when enabled."""
    mock_oidc = MagicMock()
    mock_oidc.build_authorization_url.return_value = ("https://auth.example.com", "nonce")

    mw_enabled = RbacMiddleware(governance_service=AsyncMock(), oidc_service=mock_oidc, oidc_enabled=True)
    assert mw_enabled.build_oidc_url_for_user(12345) == "https://auth.example.com"

    mw_disabled = RbacMiddleware(governance_service=AsyncMock(), oidc_service=mock_oidc, oidc_enabled=False)
    assert mw_disabled.build_oidc_url_for_user(12345) is None


@pytest.mark.anyio
async def test_rbac_middleware_no_telegram_user() -> None:
    """Verify handler is invoked directly when event has no User."""
    handler = AsyncMock(return_value="OK")
    mw = RbacMiddleware(governance_service=AsyncMock())
    result = await mw(handler, TelegramObject(), {})
    assert result == "OK"
    handler.assert_called_once()


@pytest.mark.anyio
async def test_rbac_middleware_blocked_callback_query() -> None:
    """Verify blocked user callback query is rejected with alert."""
    gov = AsyncMock()
    blocked_user = UserAccount(telegram_id=999, display_name="Blocked", role=UserRole.GUEST, is_blocked=True)
    gov.ensure_user.return_value = blocked_user

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=999, is_bot=False, first_name="Blocked")
    cb.answer = AsyncMock()

    handler = AsyncMock()
    mw = RbacMiddleware(governance_service=gov)
    res = await mw(handler, cb, {})
    assert res is None
    cb.answer.assert_called_once_with("🚫 Your account has been blocked.", show_alert=True)
    handler.assert_not_called()


@pytest.mark.anyio
async def test_rbac_middleware_media_upload_oidc_refresh_and_block() -> None:
    """Verify media upload triggers OIDC refresh and blocks deactivated user."""
    gov = AsyncMock()
    user_initial = UserAccount(
        telegram_id=111,
        display_name="User",
        role=UserRole.UPLOADER,
        is_blocked=False,
        authentik_sub="sub-111",
    )
    user_deactivated = UserAccount(
        telegram_id=111,
        display_name="User",
        role=UserRole.GUEST,
        is_blocked=True,
        authentik_sub="sub-111",
    )

    gov.ensure_user.return_value = user_initial

    mock_oidc = AsyncMock()
    mock_oidc.refresh_user_if_stale.return_value = user_deactivated

    msg = MagicMock(spec=Message)
    msg.from_user = User(id=111, is_bot=False, first_name="User")
    msg.audio = MagicMock()
    msg.document = None
    msg.answer = AsyncMock()

    handler = AsyncMock()
    mw = RbacMiddleware(governance_service=gov, oidc_service=mock_oidc, oidc_enabled=True)

    res = await mw(handler, msg, {})
    assert res is None
    mock_oidc.refresh_user_if_stale.assert_called_once_with(111)
    msg.answer.assert_called_once()
    assert "Access Denied" in msg.answer.call_args[0][0]

    # Test OIDC refresh exception is handled safely
    mock_oidc.refresh_user_if_stale.side_effect = RuntimeError("Network error")
    msg.answer.reset_mock()
    gov.ensure_user.return_value = UserAccount(
        telegram_id=222,
        display_name="User2",
        role=UserRole.UPLOADER,
        authentik_sub="sub-222",
    )
    msg.from_user = User(id=222, is_bot=False, first_name="User2")
    await mw(handler, msg, {})
    handler.assert_called_once()
