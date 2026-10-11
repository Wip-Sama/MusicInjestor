"""Authentik OIDC HTTP callback and webhook ingress handlers (`/api/v1/auth/callback` & webhooks)."""

import hmac
import logging

from aiogram import Bot

from src.core.exceptions import OidcAuthenticationError, PermissionDeniedError
from src.core.oidc import AuthentikOidcService, OidcSyncOutcome

logger = logging.getLogger(__name__)


def format_oidc_pairing_message(outcome: OidcSyncOutcome) -> str:
    """Format the Telegram confirmation message sent upon successful Authentik OIDC pairing."""
    username = outcome.user.authentik_username or outcome.user.display_name
    return (
        f"✅ Account successfully paired with Authentik as <b>{username}</b> "
        f"(Role: {outcome.new_role.value})."
    )


class OidcCallbackCoordinator:
    """Coordinates `/api/v1/auth/callback` code exchanges and `/api/v1/webhooks/authentik` events."""

    def __init__(
        self,
        oidc_service: AuthentikOidcService,
        bot: Bot | None = None,
        webhook_secret: str = "",
    ) -> None:
        self._oidc = oidc_service
        self._bot = bot
        self._webhook_secret = webhook_secret

    async def handle_oauth_callback(
        self,
        code: str,
        state: str,
        prompt_message_id: int | None = None,
        now_ts: int | None = None,
    ) -> tuple[OidcSyncOutcome, str]:
        """Process `GET /api/v1/auth/callback?code=<code>&state=<nonce>` and notify the user."""
        if not code or not state:
            raise OidcAuthenticationError("Missing required 'code' or 'state' query parameters.")

        outcome = await self._oidc.exchange_code_and_pair(
            code=code,
            state_nonce=state,
            now_ts=now_ts,
        )
        confirmation_text = format_oidc_pairing_message(outcome)

        if self._bot is not None:
            try:
                if prompt_message_id is not None:
                    await self._bot.edit_message_text(
                        chat_id=outcome.user.telegram_id,
                        message_id=prompt_message_id,
                        text=confirmation_text,
                    )
                else:
                    await self._bot.send_message(
                        chat_id=outcome.user.telegram_id,
                        text=confirmation_text,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to send OIDC confirmation message to user %s: %s",
                    outcome.user.telegram_id,
                    exc,
                )

        return outcome, confirmation_text

    async def handle_authentik_webhook(
        self,
        payload: dict[str, object],
        provided_secret: str | None = None,
    ) -> OidcSyncOutcome | None:
        """Process `POST /api/v1/webhooks/authentik` payload and synchronize the target user."""
        if self._webhook_secret and (
            not provided_secret or not hmac.compare_digest(provided_secret, self._webhook_secret)
        ):
            raise PermissionDeniedError("Invalid Authentik webhook secret.")

        event_type = str(payload.get("event_type") or payload.get("action") or "user_updated")
        authentik_sub = str(payload.get("sub") or payload.get("user_pk") or "")
        if not authentik_sub:
            raise OidcAuthenticationError("Webhook payload missing 'sub' or 'user_pk'.")

        raw_claims = payload.get("claims")
        claims = dict(raw_claims) if isinstance(raw_claims, dict) else None
        return await self._oidc.handle_webhook_event(
            event_type=event_type,
            authentik_sub=authentik_sub,
            claims=claims,
        )
