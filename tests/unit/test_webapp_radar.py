"""Unit tests for the Release Radar Web Mini App API endpoints."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from src.bot.webapp_server import BotWebServer
from src.core.models import (
    DiscoveredRelease,
    RadarScanProgress,
    ReleaseTrackPreview,
    UserAccount,
    UserRole,
)
from src.core.radar import ReleaseRadarService
from src.core.rbac import GovernanceService
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository
from tests.unit.test_webapp_auth import TEST_BOT_TOKEN, _build_signed_init_data


@pytest.mark.anyio
async def test_webapp_radar_endpoints(tmp_path: Path) -> None:
    """Test /api/v1/app/radar/releases, follows, follow, and scan endpoints."""
    db_manager = SqliteDatabaseManager(tmp_path / "webapp_radar.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)

    user = UserAccount(telegram_id=98765, display_name="Radar User", role=UserRole.UPLOADER)
    await repo.upsert_user(user)

    # Insert release
    rel = DiscoveredRelease(
        id="deezer:999",
        artist_name="Fujii Kaze",
        title="Michi Teyu Ku",
        release_type="single",
        release_date="2024-03-15",
        preview_url="https://preview.mp3",
    )
    await repo.upsert_discovered_releases([rel])

    mock_radar = AsyncMock(spec=ReleaseRadarService)
    mock_radar.scan_monitored_artists.return_value = ([rel], [])
    mock_radar.get_release_track_previews.return_value = [
        ReleaseTrackPreview(
            track_id="t_1",
            title="Michi Teyu Ku",
            preview_url="https://preview.mp3",
            duration_seconds=30,
            track_position=1,
        )
    ]

    gov_service = GovernanceService(repository=repo)
    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov_service,
        catalog_repository=repo,
        radar_service=mock_radar,
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    port = server.bound_port
    base_url = f"http://127.0.0.1:{port}"

    now = int(time.time())
    user_payload = {
        "id": 98765,
        "first_name": "Radar",
        "last_name": "User",
        "username": "radaruser",
    }
    raw_params = {
        "auth_date": str(now),
        "user": json.dumps(user_payload, separators=(",", ":")),
    }
    headers = {
        "Authorization": f"tma {_build_signed_init_data(raw_params)}",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. GET /api/v1/app/radar/releases
            resp = await client.get(f"{base_url}/api/v1/app/radar/releases", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert len(data["items"]) == 1
            assert data["items"][0]["title"] == "Michi Teyu Ku"
            assert data["items"][0]["is_followed"] is False
            assert "external_links" in data["items"][0]
            assert "primary_platform" in data["items"][0]
            assert "primary_url" in data["items"][0]

            # 1b. GET /api/v1/app/radar/release/deezer:999/tracks
            tracks_resp = await client.get(
                f"{base_url}/api/v1/app/radar/release/deezer:999/tracks", headers=headers
            )
            assert tracks_resp.status_code == 200
            t_data = tracks_resp.json()
            assert t_data["release_id"] == "deezer:999"
            assert len(t_data["tracks"]) == 1
            assert t_data["tracks"][0]["title"] == "Michi Teyu Ku"
            assert t_data["tracks"][0]["preview_url"] == "https://preview.mp3"

            # 2. POST /api/v1/app/radar/follow
            follow_resp = await client.post(
                f"{base_url}/api/v1/app/radar/follow",
                headers=headers,
                json={"artist_name": "Fujii Kaze", "follow": True},
            )
            assert follow_resp.status_code == 200
            assert follow_resp.json()["ok"] is True
            mock_radar.follow_artist.assert_awaited_once_with(98765, "Fujii Kaze")

            # 3. GET /api/v1/app/radar/follows
            follows_resp = await client.get(f"{base_url}/api/v1/app/radar/follows", headers=headers)
            assert follows_resp.status_code == 200
            assert "items" in follows_resp.json()

            # 4. POST /api/v1/app/radar/scan
            scan_resp = await client.post(f"{base_url}/api/v1/app/radar/scan", headers=headers)
            assert scan_resp.status_code == 202
            assert scan_resp.json()["ok"] is True
            assert scan_resp.json()["is_scanning"] is True
            await asyncio.sleep(0.05)
            mock_radar.scan_monitored_artists.assert_awaited_once()

            # 5. GET /api/v1/app/radar/status
            status_resp = await client.get(f"{base_url}/api/v1/app/radar/status", headers=headers)
            assert status_resp.status_code == 200
            status_data = status_resp.json()
            assert "is_scanning" in status_data
            assert "progress" in status_data

            # 6. Test concurrent scan rejection (409)
            mock_radar.is_scanning = True
            mock_radar.progress = RadarScanProgress(
                is_scanning=True,
                current_artist="Fujii Kaze",
                processed_artists=1,
                total_artists=5,
                percent=20,
            )
            busy_scan_resp = await client.post(f"{base_url}/api/v1/app/radar/scan", headers=headers)
            assert busy_scan_resp.status_code == 409
            assert busy_scan_resp.json()["already_scanning"] is True

            # 7. GET /api/v1/app/artists/{artist_name}
            artist_resp = await client.get(f"{base_url}/api/v1/app/artists/Fujii%20Kaze", headers=headers)
            assert artist_resp.status_code == 200
            artist_data = artist_resp.json()
            assert artist_data["artist_name"] == "Fujii Kaze"
            assert "library_tracks" in artist_data
            assert "discovered_releases" in artist_data
            assert len(artist_data["discovered_releases"]) == 1
            assert artist_data["stats"]["releases_discovered"] == 1

    finally:
        await server.stop()
