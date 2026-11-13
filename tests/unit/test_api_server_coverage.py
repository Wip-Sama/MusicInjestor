"""Targeted coverage tests for src.bot.api_server."""

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from main import configure_logging
from src.bot.api_server import (
    build_bot_instance,
    check_local_bot_api_reachable,
    resolve_local_api_file_path,
)
from src.config.settings import AppSettings


def test_build_bot_instance() -> None:
    """Verify build_bot_instance returns a configured Bot instance."""
    settings = AppSettings(
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        telegram_use_local_api=True,
        telegram_local_api_url="http://localhost:8081",
    )
    bot = build_bot_instance(settings)
    assert bot.token == "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def test_resolve_local_api_file_path_wsl_colon_and_nested(tmp_path: Path) -> None:
    """Verify WSL colon directory resolution and host_dir fallback searches."""
    host_dir = tmp_path / "host_storage"
    host_dir.mkdir()
    work_dir = Path("/var/lib/telegram-bot-api")

    settings = AppSettings(
        telegram_bot_token="123456:TEST_TOKEN",
        telegram_local_api_host_dir=host_dir,
        telegram_local_api_work_dir=work_dir,
    )

    # 1. WSL colon resolution (e.g. D: replaced with D\uf03a)
    wsl_dir = host_dir / "token" / "D\uf03a"
    wsl_dir.mkdir(parents=True)
    wsl_file = wsl_dir / "music.flac"
    wsl_file.write_bytes(b"DATA")

    resolved = resolve_local_api_file_path("/var/lib/telegram-bot-api/token/D:/music.flac", settings)
    assert resolved == wsl_file

    # 2. ValueError relative candidate under host_dir
    direct_file = host_dir / "direct.mp3"
    direct_file.write_bytes(b"DIRECT")
    res_direct = resolve_local_api_file_path("direct.mp3", settings)
    assert res_direct == direct_file

    # 3. ValueError candidate inside subfolder of host_dir
    sub_dir = host_dir / "sub"
    sub_dir.mkdir()
    nested_file = sub_dir / "nested.mp3"
    nested_file.write_bytes(b"NESTED")
    res_nested = resolve_local_api_file_path("nested.mp3", settings)
    assert res_nested == nested_file

    # 4. OSError suppression during candidate search
    with patch.object(Path, "iterdir", side_effect=OSError("Disk failure")):
        fallback = resolve_local_api_file_path("missing.mp3", settings)
        assert fallback == Path("missing.mp3")


@pytest.mark.anyio
async def test_check_local_bot_api_reachable() -> None:
    """Verify check_local_bot_api_reachable succeeds on listening sockets and fails cleanly otherwise."""
    server = await asyncio.start_server(lambda r, w: w.close(), host="127.0.0.1", port=0)
    try:
        sock = server.sockets[0]
        port = sock.getsockname()[1]
        assert await check_local_bot_api_reachable(f"http://127.0.0.1:{port}", timeout_seconds=1.0) is True
    finally:
        server.close()
        await server.wait_closed()

    assert await check_local_bot_api_reachable("invalid-url", timeout_seconds=0.2) is False
    assert await check_local_bot_api_reachable("http://127.0.0.1:1", timeout_seconds=0.3) is False


def test_main_configure_logging_quiets_noisy_libraries() -> None:
    """Verify DEBUG logging suppresses verbose aiosqlite, httpcore, and hpack debug spam."""
    configure_logging("DEBUG")
    for lib_name in ("aiosqlite", "httpcore", "hpack"):
        assert logging.getLogger(lib_name).level == logging.WARNING
