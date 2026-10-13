"""Unit tests for the unified Telegram Mini App HTTP and API server (`BotWebServer`)."""

import json
import time
from pathlib import Path

import httpx
import pytest

from src.bot.webapp_server import BotWebServer
from src.core.rbac import GovernanceService
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository
from tests.unit.test_webapp_auth import TEST_BOT_TOKEN, _build_signed_init_data


async def _setup_test_server(
    tmp_path: Path,
    owner_telegram_ids: tuple[int, ...] = (),
    static_dir: Path | None = None,
    dev_mode: bool = False,
) -> tuple[BotWebServer, SqliteCatalogRepository]:
    """Helper bootstrapping SQLite persistence, GovernanceService, and BotWebServer on an ephemeral port."""
    db_manager = SqliteDatabaseManager(tmp_path / "webapp_test.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    gov_service = GovernanceService(repository=repo, owner_telegram_ids=owner_telegram_ids)

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov_service,
        coordinator=None,
        static_dir=static_dir,
        host="127.0.0.1",
        port=0,
        owner_telegram_ids=owner_telegram_ids,
        dev_mode=dev_mode,
    )
    await server.start()
    return server, repo


@pytest.mark.anyio
async def test_healthz_endpoint(tmp_path: Path) -> None:
    """Verify that GET /healthz returns status ok."""
    server, _ = await _setup_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/healthz")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["app"] == "musicDownloader"
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_get_me_unauthorized(tmp_path: Path) -> None:
    """Verify that requests missing TMA authorization headers return 401."""
    server, _ = await _setup_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/api/v1/app/auth/me")
            assert resp.status_code == 401
            data = resp.json()
            assert "error" in data
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_get_me_authorized_success(tmp_path: Path) -> None:
    """Verify that valid Authorization: tma <initData> successfully resolves the user profile."""
    server, _ = await _setup_test_server(tmp_path, owner_telegram_ids=(123456789,))
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        now = int(time.time())
        user_json = json.dumps(
            {"id": 123456789, "first_name": "Admin", "last_name": "Boss", "username": "adminboss"},
            separators=(",", ":"),
        )
        init_data = _build_signed_init_data(
            {
                "auth_date": str(now),
                "start_param": "track_99",
                "user": user_json,
            }
        )

        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"tma {init_data}"}
            resp = await client.get(f"{base_url}/api/v1/app/auth/me", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["telegram_id"] == 123456789
            assert data["role"] == "OWNER"
            assert data["display_name"] == "Admin Boss"
            assert data["username"] == "adminboss"
            assert data["is_owner"] is True
            assert data["start_param"] == "track_99"
            assert "SYSTEM_DESTRUCTIVE_WIPE" in data["permissions"]
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_static_spa_placeholder_fallback(tmp_path: Path) -> None:
    """Verify that unknown routes render the SPA fallback."""
    server, _ = await _setup_test_server(tmp_path, static_dir=tmp_path / "empty_dir")
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/songs/42")
            assert resp.status_code == 200
            assert "Music Ingestion Mini App" in resp.text
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_static_spa_serves_built_files(tmp_path: Path) -> None:
    """Verify that when index.html and assets exist, the server serves real files."""
    static_folder = tmp_path / "dist"
    static_folder.mkdir(parents=True)
    index_file = static_folder / "index.html"
    index_file.write_text("<!DOCTYPE html><html><body><h1>Built App</h1></body></html>", encoding="utf-8")
    asset_file = static_folder / "style.css"
    asset_file.write_text("body { color: red; }", encoding="utf-8")

    server, _ = await _setup_test_server(tmp_path, static_dir=static_folder)
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            # Direct asset request
            resp_asset = await client.get(f"{base_url}/style.css")
            assert resp_asset.status_code == 200
            assert "color: red" in resp_asset.text

            # SPA route fallback to index.html
            resp_spa = await client.get(f"{base_url}/catalog/view/123")
            assert resp_spa.status_code == 200
            assert "Built App" in resp_spa.text
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_dev_token_endpoint(tmp_path: Path) -> None:
    """Verify that GET /api/v1/app/dev/token yields a valid initData token when dev_mode is active."""
    # 1. When dev_mode is False, dev endpoint returns fallback (or 404)
    server_prod, _ = await _setup_test_server(tmp_path / "prod", dev_mode=False)
    try:
        base_url_prod = f"http://127.0.0.1:{server_prod.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url_prod}/api/v1/app/dev/token")
            # In SPA fallback, unknown paths return 200 html placeholder if no route matched
            assert "application/json" not in resp.headers.get("Content-Type", "")
    finally:
        await server_prod.stop()

    # 2. When dev_mode is True, dev endpoint returns a signed token usable with /api/v1/app/auth/me
    server_dev, _ = await _setup_test_server(tmp_path / "dev", owner_telegram_ids=(998877,), dev_mode=True)
    try:
        base_url_dev = f"http://127.0.0.1:{server_dev.bound_port}"
        async with httpx.AsyncClient() as client:
            resp_token = await client.get(f"{base_url_dev}/api/v1/app/dev/token")
            assert resp_token.status_code == 200
            data_token = resp_token.json()
            assert "init_data" in data_token
            dev_init_data = data_token["init_data"]

            # Use the acquired token to authenticate against /api/v1/app/auth/me
            headers = {"Authorization": f"tma {dev_init_data}"}
            resp_me = await client.get(f"{base_url_dev}/api/v1/app/auth/me", headers=headers)
            assert resp_me.status_code == 200
            data_me = resp_me.json()
            assert data_me["telegram_id"] == 998877
            assert data_me["role"] == "OWNER"
            assert data_me["is_owner"] is True
    finally:
        await server_dev.stop()
