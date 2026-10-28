"""Unit tests for Cloudflare Quick Tunnel discovery and process management."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.tunnel import (
    CloudflareQuickTunnel,
    extract_quick_tunnel_url,
    find_cloudflared_binary,
)


def test_extract_quick_tunnel_url() -> None:
    """Verify regex extraction of trycloudflare.com URLs from stdout/stderr lines."""
    raw_box = (
        "INF +--------------------------------------------------------------------------------------------+\n"
        "INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |\n"
        "INF |  https://brave-whale-swimming-fast.trycloudflare.com                                       |\n"
        "INF +--------------------------------------------------------------------------------------------+"
    )
    assert extract_quick_tunnel_url(raw_box) == "https://brave-whale-swimming-fast.trycloudflare.com"

    plain = "Tunnel URL: https://sample-quick-123.trycloudflare.com"
    assert extract_quick_tunnel_url(plain) == "https://sample-quick-123.trycloudflare.com"

    no_match = "INF Connecting to edge server..."
    assert extract_quick_tunnel_url(no_match) is None


def test_find_cloudflared_binary() -> None:
    """Verify locate logic returns executable path if present in PATH."""
    with patch("shutil.which", return_value="/usr/local/bin/cloudflared"):
        assert find_cloudflared_binary() == "/usr/local/bin/cloudflared"

    with patch("shutil.which", return_value=None):
        assert find_cloudflared_binary() is None


@pytest.mark.anyio
async def test_quick_tunnel_from_log_file(tmp_path: Path) -> None:
    """Verify discovering an existing tunnel URL directly from a log file without starting a subprocess."""
    log_file = tmp_path / "cloudflared.log"
    log_file.write_text(
        "INF Starting tunnel...\nINF Your quick Tunnel: https://existing-log-tunnel.trycloudflare.com\n",
        encoding="utf-8",
    )
    tunnel = CloudflareQuickTunnel(
        target_url="http://127.0.0.1:8080",
        log_file=log_file,
    )
    discovered = await tunnel.start()
    assert discovered == "https://existing-log-tunnel.trycloudflare.com"
    assert tunnel.tunnel_url == "https://existing-log-tunnel.trycloudflare.com"


@pytest.mark.anyio
async def test_quick_tunnel_binary_missing() -> None:
    """Verify behavior when cloudflared binary is not installed."""
    tunnel = CloudflareQuickTunnel(
        target_url="http://127.0.0.1:8080",
        cloudflared_path=None,
    )
    discovered = await tunnel.start()
    assert discovered is None
    assert tunnel.tunnel_url is None
    assert tunnel.is_running is False


@pytest.mark.anyio
async def test_quick_tunnel_async_subprocess_lifecycle() -> None:
    """Verify async subprocess execution, output reading, URL discovery, and clean stop."""
    mock_proc = MagicMock()
    mock_proc.pid = 98765
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    # Mock stdout stream
    mock_stdout = asyncio.StreamReader()
    mock_stdout.feed_data(b"INF Connecting to Cloudflare network...\n")
    mock_stdout.feed_eof()

    # Mock stderr stream with the quick tunnel announcement
    mock_stderr = asyncio.StreamReader()
    mock_stderr.feed_data(b"INF Requesting new quick Tunnel on trycloudflare.com...\n")
    mock_stderr.feed_data(b"INF | https://test-automated-mock.trycloudflare.com |\n")
    mock_stderr.feed_eof()

    mock_proc.stdout = mock_stdout
    mock_proc.stderr = mock_stderr

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        tunnel = CloudflareQuickTunnel(
            target_url="http://127.0.0.1:8080",
            cloudflared_path="/usr/bin/cloudflared",
        )
        discovered = await tunnel.start(timeout_seconds=5.0)

        assert discovered == "https://test-automated-mock.trycloudflare.com"
        assert tunnel.tunnel_url == "https://test-automated-mock.trycloudflare.com"
        assert tunnel.is_running is True
        assert tunnel.pid == 98765

        await tunnel.stop()
        mock_proc.terminate.assert_called_once()
        assert tunnel._proc is None


@pytest.mark.anyio
async def test_quick_tunnel_timeout() -> None:
    """Verify timeout behavior when tunnel URL is never emitted."""
    mock_proc = MagicMock()
    mock_proc.pid = 11111
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    mock_stdout = asyncio.StreamReader()
    mock_stdout.feed_data(b"INF Connecting...\n")
    mock_proc.stdout = mock_stdout

    mock_stderr = asyncio.StreamReader()
    mock_stderr.feed_data(b"INF Waiting for connection...\n")
    mock_proc.stderr = mock_stderr

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        tunnel = CloudflareQuickTunnel(
            target_url="http://127.0.0.1:8080",
            cloudflared_path="/usr/bin/cloudflared",
        )
        discovered = await tunnel.start(timeout_seconds=0.1)
        assert discovered is None

        await tunnel.stop()
