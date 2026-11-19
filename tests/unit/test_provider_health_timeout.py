"""Unit tests for provider health check live-verification timeout fallback."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.core.provider import HttpMusicProviderClient


@pytest.mark.asyncio
async def test_check_health_verify_timeout_falls_back_to_degraded() -> None:
    """If /health?verify=true times out but basic /health responds, status is degraded not unreachable."""
    fallback_resp = MagicMock()
    fallback_resp.raise_for_status = MagicMock()
    fallback_resp.json.return_value = {
        "status": "ok",
        "version": "0.1.9",
        "upstream_dependencies": {"youtube": "ready"},
        "active_jobs_count": 0,
    }

    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.is_closed = False
    mock_http.get = AsyncMock(side_effect=[httpx.ReadTimeout(""), fallback_resp])

    client = HttpMusicProviderClient(base_url="http://music-provider:8000", client=mock_http)
    health = await client.check_health(verify=True)

    assert health.status == "degraded"
    assert health.upstream_dependencies["note"] == "Live upstream verification timed out"
    assert mock_http.get.await_count == 2


@pytest.mark.asyncio
async def test_check_health_unreachable_populates_non_empty_error_detail() -> None:
    """When both verify and fallback fail with an empty-message timeout, error uses exception class name."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.is_closed = False
    mock_http.get = AsyncMock(side_effect=httpx.ConnectTimeout(""))

    client = HttpMusicProviderClient(base_url="http://music-provider:8000", client=mock_http)
    health = await client.check_health(verify=False)

    assert health.status == "unreachable"
    assert health.upstream_dependencies["error"] == "ConnectTimeout"
