"""Unit tests for the ReleaseRadarService domain orchestrator."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.core.models import DiscoveredRelease, FollowedArtist
from src.core.ports import GovernanceRepositoryPort, ReleaseRadarProviderPort
from src.core.radar import ReleaseRadarService, is_release_recent


def test_is_release_recent() -> None:
    """Test date parsing and recency window checks."""
    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    assert is_release_recent(yesterday, max_days=60) is True

    old_date = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    assert is_release_recent(old_date, max_days=60) is False

    future_date = (now + timedelta(days=10)).strftime("%Y-%m-%d")
    assert is_release_recent(future_date, max_days=60) is True

    # Empty or year-only
    assert is_release_recent("", max_days=60) is True
    assert is_release_recent(str(now.year), max_days=60) is True
    assert is_release_recent("1995", max_days=60) is False


@pytest.mark.anyio
async def test_radar_service_follow_and_unfollow() -> None:
    """Test following and unfollowing an artist via ReleaseRadarService."""
    mock_provider = AsyncMock(spec=ReleaseRadarProviderPort)
    mock_provider.search_artist.return_value = "deezer_123"
    mock_provider.get_artist_recent_releases.return_value = [
        DiscoveredRelease(
            id="deezer:500",
            artist_name="Ado",
            title="Kura Kura",
            release_type="single",
            release_date="2026-02-01",
            preview_url="https://preview.mp3",
        )
    ]

    mock_repo = AsyncMock(spec=GovernanceRepositoryPort)
    mock_repo.follow_artist.return_value = FollowedArtist(
        id=1,
        telegram_id=999,
        artist_name="Ado",
        provider_artist_id="deezer_123",
    )
    mock_repo.unfollow_artist.return_value = True

    service = ReleaseRadarService(provider=mock_provider, repository=mock_repo)

    followed = await service.follow_artist(999, "Ado")
    assert followed.artist_name == "Ado"
    mock_provider.search_artist.assert_awaited_once_with("Ado")
    mock_repo.follow_artist.assert_awaited_once_with(
        telegram_id=999,
        artist_name="Ado",
        provider_artist_id="deezer_123",
    )
    mock_repo.upsert_discovered_releases.assert_awaited_once()

    unfollowed = await service.unfollow_artist(999, "Ado")
    assert unfollowed is True
    mock_repo.unfollow_artist.assert_awaited_once_with(999, "Ado")


@pytest.mark.anyio
async def test_radar_service_scan_monitored_artists() -> None:
    """Test scanning all monitored artists in library and follows."""
    mock_provider = AsyncMock(spec=ReleaseRadarProviderPort)
    mock_provider.search_artist.side_effect = lambda name: f"id_{name}"
    mock_provider.get_artist_recent_releases.return_value = [
        DiscoveredRelease(
            id="rel_1",
            artist_name="Artist A",
            title="Drop A",
            release_type="single",
            release_date=datetime.now(UTC).strftime("%Y-%m-%d"),
        )
    ]

    mock_repo = AsyncMock(spec=GovernanceRepositoryPort)
    mock_repo.list_all_monitored_artists.return_value = ["Artist A", "Artist B"]
    mock_repo.upsert_discovered_releases.side_effect = lambda rels: list(rels)

    service = ReleaseRadarService(provider=mock_provider, repository=mock_repo)
    discovered, notifications = await service.scan_monitored_artists(max_days_back=30)

    assert len(discovered) == 2
    assert mock_provider.search_artist.await_count == 2


@pytest.mark.anyio
async def test_radar_service_get_preview_audio_bytes() -> None:
    """Test downloading the 30-second audio stream bytes."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.content = b"ID3" + b"x" * 2000
    mock_client.get.return_value = mock_resp

    mock_provider = AsyncMock(spec=ReleaseRadarProviderPort)
    mock_repo = AsyncMock(spec=GovernanceRepositoryPort)

    service = ReleaseRadarService(
        provider=mock_provider,
        repository=mock_repo,
        http_client=mock_client,
    )

    data = await service.get_preview_audio_bytes("https://cdns.deezer.com/preview.mp3")
    assert data is not None
    assert len(data) > 2000

    # Invalid URL or failure
    assert await service.get_preview_audio_bytes("") is None
    mock_resp.status_code = 404
    assert await service.get_preview_audio_bytes("https://notfound.mp3") is None
