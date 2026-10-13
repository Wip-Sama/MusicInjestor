import hashlib
import hmac
import html
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

from src.bot.auth import TelegramInitDataValidationError, validate_telegram_init_data
from src.bot.oidc_server import OidcCallbackCoordinator
from src.core.exceptions import OidcAuthenticationError, PermissionDeniedError
from src.core.models import UserAccount
from src.core.rbac import ROLE_PERMISSIONS, GovernanceService

logger = logging.getLogger(__name__)


def extract_raw_init_data_from_request(request: web.Request) -> str:
    """Extract raw `initData` query string from HTTP authorization headers or query parameters."""
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        parts = auth_header.split(" ", maxsplit=1)
        if len(parts) == 2 and parts[0].lower() in {"tma", "bearer"}:
            return parts[1].strip()

    custom_header = request.headers.get("X-Telegram-Init-Data", "").strip()
    if custom_header:
        return custom_header

    return request.query.get("tgWebAppData", "").strip()


async def authenticate_tma_request(
    request: web.Request,
    bot_token: str,
    governance_service: GovernanceService,
) -> tuple[UserAccount, dict[str, Any]]:
    """Validate `initData` from the incoming request and resolve the corresponding `UserAccount`."""
    raw_init_data = extract_raw_init_data_from_request(request)
    if not raw_init_data:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "Missing Telegram Mini App initData authorization header."}),
            content_type="application/json",
        )

    try:
        validated_data = validate_telegram_init_data(raw_init_data, bot_token=bot_token)
    except TelegramInitDataValidationError as exc:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json",
        ) from exc

    raw_user = validated_data.get("user")
    if not isinstance(raw_user, dict) or "id" not in raw_user:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "initData does not contain a valid user object."}),
            content_type="application/json",
        )

    try:
        telegram_id = int(raw_user["id"])
    except (ValueError, TypeError) as exc:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "Malformed Telegram user ID in initData."}),
            content_type="application/json",
        ) from exc

    first_name = str(raw_user.get("first_name", "")).strip()
    last_name = str(raw_user.get("last_name", "")).strip()
    display_name = f"{first_name} {last_name}".strip() or f"User {telegram_id}"
    username = raw_user.get("username")
    clean_username = str(username).strip() if username else None

    user = await governance_service.ensure_user(
        telegram_id=telegram_id,
        display_name=display_name,
        username=clean_username,
    )
    return user, validated_data


def build_webapp_api(
    bot_token: str,
    governance_service: GovernanceService,
    coordinator: OidcCallbackCoordinator | None = None,
    static_dir: Path | None = None,
    owner_telegram_ids: tuple[int, ...] = (),
    dev_mode: bool = False,
) -> web.Application:
    """Construct an `aiohttp.web.Application` exposing TMA API, OIDC callbacks, and static assets."""
    app = web.Application()

    # 1. Healthcheck endpoint
    async def handle_healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "app": "musicDownloader", "version": "0.1.0"})

    app.router.add_get("/healthz", handle_healthz)

    # 1.1 Local development token endpoint (only available when dev_mode is active)
    if dev_mode:

        async def handle_dev_token(_request: web.Request) -> web.Response:
            now = int(time.time())
            owner_id = owner_telegram_ids[0] if owner_telegram_ids else 10000001
            user_json = json.dumps(
                {
                    "id": owner_id,
                    "first_name": "Local",
                    "last_name": "Developer",
                    "username": "localdev",
                },
                separators=(",", ":"),
            )
            params = {
                "auth_date": str(now),
                "query_id": "AAH_local_dev_token",
                "user": user_json,
            }
            data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
            secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
            calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
            init_data = f"{urlencode(params)}&hash={calc_hash}"
            return web.json_response({"init_data": init_data, "dev_mode": True})

        app.router.add_get("/api/v1/app/dev/token", handle_dev_token)

    # 2. Telegram Mini App Auth & Profile endpoint
    async def handle_get_me(request: web.Request) -> web.Response:
        user, validated_data = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        permissions = [p.value for p in ROLE_PERMISSIONS.get(user.role, frozenset())]
        is_owner = user.role.value == "OWNER" or user.telegram_id in owner_telegram_ids

        response_payload = {
            "telegram_id": user.telegram_id,
            "role": user.role.value,
            "display_name": user.display_name,
            "username": user.username,
            "authentik_linked": bool(user.authentik_username),
            "authentik_username": user.authentik_username,
            "permissions": permissions,
            "is_owner": is_owner,
            "start_param": validated_data.get("start_param", ""),
        }
        return web.json_response(response_payload)

    app.router.add_get("/api/v1/app/auth/me", handle_get_me)

    # 3. Optional OIDC routes (consolidated from oidc_server)
    if coordinator is not None:

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
            auth_hdr = request.headers.get("Authorization", "")
            if not secret and auth_hdr.lower().startswith("bearer "):
                secret = auth_hdr[7:].strip()

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

        app.router.add_get("/api/v1/auth/callback", oauth_callback_handler)
        app.router.add_post("/api/v1/webhooks/authentik", authentik_webhook_handler)

    # 4. SPA Static Files & Fallback Handler
    resolved_static = static_dir.resolve() if static_dir else None

    async def handle_spa_fallback(request: web.Request) -> web.StreamResponse:
        rel_path = request.path.lstrip("/")
        if resolved_static and resolved_static.is_dir():
            target_file = (resolved_static / rel_path).resolve()
            if target_file.is_file() and str(target_file).startswith(str(resolved_static)):
                return web.FileResponse(target_file)

            index_html = resolved_static / "index.html"
            if index_html.is_file():
                return web.FileResponse(index_html)

        placeholder_html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Music Ingestion Mini App</title>"
            "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
            "<style>body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;"
            "display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;"
            "background: #1c1c1e; color: #f2f2f7; text-align: center; padding: 20px; }</style>"
            "</head><body>"
            "<div><h2>🎵 Telegram Music Mini App</h2>"
            "<p>API Server is operational. Frontend build in progress or static directory not found.</p>"
            f"<p><code>Status: OK (Path: {html.escape(request.path)})</code></p></div>"
            "</body></html>"
        )
        return web.Response(text=placeholder_html, content_type="text/html", status=200)

    app.router.add_get("/{tail:.*}", handle_spa_fallback)

    return app


class BotWebServer:
    """Lifecycle wrapper for running the unified `aiohttp.web.Application` listener."""

    def __init__(
        self,
        bot_token: str,
        governance_service: GovernanceService,
        coordinator: OidcCallbackCoordinator | None = None,
        static_dir: Path | None = None,
        host: str = "0.0.0.0",
        port: int = 8080,
        owner_telegram_ids: tuple[int, ...] = (),
        dev_mode: bool = False,
    ) -> None:
        self._bot_token = bot_token
        self._governance_service = governance_service
        self._coordinator = coordinator
        self._static_dir = static_dir
        self._host = host
        self._port = port
        self._owner_telegram_ids = owner_telegram_ids
        self._dev_mode = dev_mode

        self._app = build_webapp_api(
            bot_token=bot_token,
            governance_service=governance_service,
            coordinator=coordinator,
            static_dir=static_dir,
            owner_telegram_ids=owner_telegram_ids,
            dev_mode=dev_mode,
        )
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._bound_port: int = port

    @property
    def bound_port(self) -> int:
        """Return the active listening TCP port (useful when `port=0`)."""
        return self._bound_port

    async def start(self) -> None:
        """Initialize and start the unified HTTP web server."""
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

        logger.info("Started Unified Web & API Server on http://%s:%d", self._host, self._bound_port)

    async def stop(self) -> None:
        """Gracefully stop and clean up the HTTP runner."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
