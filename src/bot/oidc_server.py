"""Authentik OIDC HTTP callback and webhook ingress handlers (`/api/v1/auth/callback` & webhooks)."""

import asyncio
import contextlib
import hmac
import html
import logging

from aiogram import Bot
from aiohttp import web

from src.core.exceptions import OidcAuthenticationError, PermissionDeniedError
from src.core.oidc import AuthentikOidcService, OidcSyncOutcome

logger = logging.getLogger(__name__)


def format_oidc_pairing_message(outcome: OidcSyncOutcome) -> str:
    """Format the Telegram confirmation message sent upon successful Authentik OIDC pairing."""
    username = html.escape(outcome.user.authentik_username or outcome.user.display_name)
    return (
        "<b>✅ Authentik SSO Paired</b>\n"
        f"<blockquote>Account successfully paired with Authentik as <code>{username}</code> "
        f"(Role: <code>{outcome.new_role.value}</code>).</blockquote>\n"
        f"<i>Telegram ID:</i> <code>{outcome.user.telegram_id}</code>"
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


def build_oidc_web_app(coordinator: OidcCallbackCoordinator) -> web.Application:
    """Create an `aiohttp.web.Application` exposing OIDC callback and Authentik webhook endpoints."""
    app = web.Application()

    async def oauth_callback_handler(request: web.Request) -> web.Response:
        code = request.query.get("code", "").strip()
        state = request.query.get("state", "").strip()
        raw_msg_id = request.query.get("msg_id")
        prompt_msg_id = int(raw_msg_id) if raw_msg_id and raw_msg_id.isdigit() else None
        try:
            outcome, confirmation_text = await coordinator.handle_oauth_callback(
                code=code,
                state=state,
                prompt_message_id=prompt_msg_id,
            )
            html_body = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'><title>SSO Paired</title></head>"
                f"<body><h2>{confirmation_text}</h2><p>You may now return to Telegram.</p></body></html>"
            )
            return web.Response(
                text=html_body,
                content_type="text/html",
                status=200,
                headers={"X-Assigned-Role": outcome.new_role.value},
            )
        except OidcAuthenticationError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def authentik_webhook_handler(request: web.Request) -> web.Response:
        secret = (
            request.headers.get("X-Authentik-Secret")
            or request.headers.get("X-Webhook-Secret")
            or request.query.get("secret")
        )
        auth_header = request.headers.get("Authorization", "")
        if not secret and auth_header.lower().startswith("bearer "):
            secret = auth_header[7:].strip()

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body."}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "JSON payload must be an object."}, status=400)

        try:
            outcome = await coordinator.handle_authentik_webhook(
                payload=payload,
                provided_secret=secret,
            )
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except OidcAuthenticationError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        if outcome is None:
            return web.json_response({"status": "ignored", "reason": "user_not_found"}, status=200)
        return web.json_response(
            {
                "status": "synced",
                "telegram_id": outcome.user.telegram_id,
                "previous_role": outcome.previous_role.value,
                "new_role": outcome.new_role.value,
                "cancelled_jobs": outcome.cancelled_jobs,
            },
            status=200,
        )

    async def healthz_handler(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"}, status=200)

    app.router.add_get("/api/v1/auth/callback", oauth_callback_handler)
    app.router.add_post("/api/v1/webhooks/authentik", authentik_webhook_handler)
    app.router.add_get("/healthz", healthz_handler)
    return app


class OidcHttpServer:
    """Lifecycle wrapper around `aiohttp.web.AppRunner` and `TCPSite` for OIDC callbacks and webhooks."""

    def __init__(
        self,
        coordinator: OidcCallbackCoordinator,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self._app = build_oidc_web_app(coordinator)
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._bound_port: int = port

    @property
    def bound_port(self) -> int:
        """Return the active TCP listening port (resolved dynamically when `port=0`)."""
        return self._bound_port

    async def start(self) -> None:
        """Start the non-blocking aiohttp HTTP listener."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()
        server = getattr(self._site, "_server", None)
        sockets = getattr(server, "sockets", None) if server is not None else None
        if sockets:
            sockname = sockets[0].getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                self._bound_port = int(sockname[1])
        logger.info("Started OIDC HTTP server on http://%s:%d", self._host, self._bound_port)

    async def stop(self) -> None:
        """Gracefully stop and clean up the aiohttp HTTP listener."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None


async def run_hourly_oidc_sync_loop(
    oidc_service: AuthentikOidcService,
    interval_seconds: int = 3600,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background task that re-verifies all Authentik-linked users every `interval_seconds`."""
    effective_interval = max(1, interval_seconds)
    while stop_event is None or not stop_event.is_set():
        try:
            if stop_event is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=effective_interval)
                if stop_event.is_set():
                    break
            else:
                await asyncio.sleep(effective_interval)
            outcomes = await oidc_service.sync_all_linked_users()
            logger.info("Periodic OIDC sync completed (%d users checked).", len(outcomes))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Periodic OIDC sync iteration encountered an error: %s", exc)
