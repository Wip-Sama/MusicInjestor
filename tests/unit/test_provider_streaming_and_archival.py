"""Unit tests for streaming vs archival provider routing, quality gates, and ephemeral stream caching."""

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiohttp import web

from src.bot.webapp_server import BotWebServer
from src.core.deezer import DeezerReleaseProvider
from src.core.lyrics import LrclibLyricsClient
from src.core.models import (
    AvailableAudioQuality,
    LyricsData,
    ProviderAudioProfiles,
    ProviderCapabilities,
    ProviderFeatures,
    ProviderHealth,
    ProviderServerRecord,
    ProvisionResponse,
    SongRequestRecord,
    SongRequestStatus,
    SongRequestType,
    TrackMetadata,
    TrackResolutionResult,
    UserAccount,
    UserRole,
)
from src.core.provider import HttpMusicProviderClient, MultiProviderService, available_quality_meets_level
from src.core.quality import AudioQuality, audio_quality_meets_level, meets_archival_quality
from src.core.rbac import GovernanceService
from src.core.stream_cache import EphemeralStreamCache
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository
from tests.unit.test_webapp_auth import TEST_BOT_TOKEN, _build_signed_init_data


def test_archival_quality_gate_tiers() -> None:
    """Verify minimum archival quality levels reject lossy audio unless explicitly relaxed."""
    opus_160 = AvailableAudioQuality(
        quality_id="opus_stream", codec="opus", bit_depth=16, sample_rate_hz=48000, bitrate_kbps=160
    )
    mp3_320 = AvailableAudioQuality(
        quality_id="lossy_high", codec="mp3", bit_depth=16, sample_rate_hz=44100, bitrate_kbps=320
    )
    flac_cd = AvailableAudioQuality(
        quality_id="lossless_cd", codec="flac", bit_depth=16, sample_rate_hz=44100, bitrate_kbps=1411
    )
    flac_hires = AvailableAudioQuality(
        quality_id="lossless_hi_res",
        codec="flac",
        bit_depth=24,
        sample_rate_hz=96000,
        bitrate_kbps=3000,
    )

    # Tier helper
    assert not available_quality_meets_level(opus_160, "lossless")
    assert not available_quality_meets_level(mp3_320, "lossless")
    assert available_quality_meets_level(flac_cd, "lossless")
    assert not available_quality_meets_level(flac_cd, "hires")
    assert available_quality_meets_level(flac_hires, "hires")
    assert available_quality_meets_level(mp3_320, "lossy_high")
    assert not available_quality_meets_level(opus_160, "lossy_high")
    assert available_quality_meets_level(opus_160, "any")

    # Downloaded file AudioQuality inspection helper
    assert not meets_archival_quality(
        is_lossless=False, bit_depth=16, sample_rate_hz=44100, bitrate_kbps=192, min_level="lossless"
    )
    assert meets_archival_quality(
        is_lossless=True, bit_depth=16, sample_rate_hz=44100, bitrate_kbps=1411, min_level="lossless"
    )
    assert not audio_quality_meets_level(
        AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=320), "lossless"
    )
    assert audio_quality_meets_level(
        AudioQuality(
            format="flac",
            is_lossless=True,
            bits_per_sample=24,
            sample_rate=96000,
            bitrate_kbps=3000,
        ),
        "hires",
    )


@pytest.mark.anyio
async def test_ephemeral_stream_cache_ttl_and_lru_eviction(tmp_path: Path) -> None:
    """Verify EphemeralStreamCache serves hits, expires entries past TTL, and evicts oldest LRU bytes."""
    cache_dir = tmp_path / "cache" / "streams"
    cache = EphemeralStreamCache(cache_dir=cache_dir, ttl_seconds=60, max_bytes=1024 * 1024)

    part_a = cache.temp_part_path("yt-1", "trk_1", "opus")
    part_a.write_bytes(b"A" * 2048)
    saved_a = await cache.commit_stream_file(
        part_a, "yt-1", "trk_1", quality="opus", content_type="audio/webm"
    )
    assert saved_a is not None
    assert saved_a.is_file()

    hit = await cache.get_cached_entry("yt-1", "trk_1", "opus")
    assert hit is not None
    assert hit[0].read_bytes() == b"A" * 2048
    assert hit[1] == "audio/webm"

    # Expire via old mtime
    expired_time = time.time() - 120
    os.utime(hit[0], (expired_time, expired_time))
    assert await cache.get_cached_entry("yt-1", "trk_1", "opus") is None
    assert not saved_a.exists()

    # Eviction check when exceeding max_bytes
    bounded = EphemeralStreamCache(cache_dir=cache_dir, ttl_seconds=3600, max_bytes=1024 * 1024)
    # Write first entry (600KB) and backdate slightly so it is older LRU
    part1 = bounded.temp_part_path("yt-1", "old_trk", "opus")
    part1.write_bytes(b"1" * (600 * 1024))
    path1 = await bounded.commit_stream_file(part1, "yt-1", "old_trk", "opus")
    assert path1 is not None
    older_ts = time.time() - 30
    os.utime(path1, (older_ts, older_ts))

    # Write second entry (600KB -> total 1.2MB > 1MB limit), triggering LRU eviction of old_trk
    part2 = bounded.temp_part_path("yt-1", "new_trk", "opus")
    part2.write_bytes(b"2" * (600 * 1024))
    path2 = await bounded.commit_stream_file(part2, "yt-1", "new_trk", "opus")
    assert path2 is not None
    assert await bounded.get_cached_entry("yt-1", "old_trk", "opus") is None
    assert await bounded.get_cached_entry("yt-1", "new_trk", "opus") is not None


@pytest.mark.anyio
async def test_multi_provider_cascading_and_archival_post_download_gate(tmp_path: Path) -> None:
    """Verify streaming prioritizes fast priority order and archival rejects lossy providers & files."""
    db = SqliteDatabaseManager(tmp_path / "providers_cascade.db")
    await db.initialize()
    repo = SqliteCatalogRepository(db)

    yt_srv = ProviderServerRecord(
        id="yt-stream",
        name="YouTube Fast",
        base_url="http://yt.local:8086",
        priority=5,
        is_enabled=True,
        allow_streaming=True,
        allow_archiving=False,
        capabilities=ProviderCapabilities(
            provider_name="youtube-stream",
            version="1.0.0",
            supported_sources=("youtube",),
            features=ProviderFeatures(streaming=True, provisioning=False),
        ),
    )
    lossy_arch = ProviderServerRecord(
        id="lossy-arch",
        name="Lossy MP3 Node",
        base_url="http://lossy.local:8087",
        priority=8,
        is_enabled=True,
        allow_streaming=False,
        allow_archiving=True,
        capabilities=ProviderCapabilities(
            provider_name="lossy-dl",
            version="1.0.0",
            supported_sources=("deezer",),
            features=ProviderFeatures(streaming=False, provisioning=True),
        ),
    )
    flac_srv = ProviderServerRecord(
        id="flac-arch",
        name="SpotiFLAC Archiver",
        base_url="http://flac.local:8085",
        priority=10,
        is_enabled=True,
        allow_streaming=False,
        allow_archiving=True,
        min_archival_quality="lossless",
        capabilities=ProviderCapabilities(
            provider_name="spotiflac",
            version="1.0.0",
            supported_sources=("spotify", "isrc", "deezer"),
            features=ProviderFeatures(streaming=False, provisioning=True),
        ),
    )
    for s in (yt_srv, lossy_arch, flac_srv):
        await repo.upsert_provider_server(s)

    yt_client = AsyncMock()
    yt_client.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="yt_abc123",
        matched_source="youtube",
        streaming_supported=True,
        available_qualities=(
            AvailableAudioQuality(
                quality_id="opus_160", codec="opus", bit_depth=16, sample_rate_hz=48000, bitrate_kbps=160
            ),
        ),
    )

    lossy_client = AsyncMock()
    lossy_client.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="mp3_trk",
        matched_source="deezer",
        streaming_supported=False,
        available_qualities=(
            AvailableAudioQuality(
                quality_id="mp3_320", codec="mp3", bit_depth=16, sample_rate_hz=44100, bitrate_kbps=320
            ),
        ),
    )

    flac_client = AsyncMock()
    flac_client.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="flac_trk",
        matched_source="isrc",
        streaming_supported=False,
        available_qualities=(
            AvailableAudioQuality(
                quality_id="lossless_hi_res",
                codec="flac",
                bit_depth=24,
                sample_rate_hz=96000,
                bitrate_kbps=3000,
            ),
        ),
    )

    artifact_ready_cb = AsyncMock()
    service = MultiProviderService(
        repository=repo,
        staging_dir=tmp_path / "staging",
        on_artifact_ready=artifact_ready_cb,
        min_archival_quality="lossless",
    )
    clients_by_id = {
        "yt-stream": yt_client,
        "lossy-arch": lossy_client,
        "flac-arch": flac_client,
    }
    service.get_provider_client = lambda srv: clients_by_id[srv.id]  # type: ignore[method-assign]

    # 1. Streaming lookup selects YouTube Fast (priority 5) without touching archival nodes
    stream_match = await service.resolve_track_cascading(
        identifiers={},
        fallback={"title": "Demo Song", "artist": "Demo Artist"},
        purpose="stream",
    )
    assert stream_match is not None
    assert stream_match[0].id == "yt-stream"
    assert stream_match[1].provider_track_id == "yt_abc123"
    flac_client.resolve_track.assert_not_awaited()

    # 2. Archival lookup skips yt-stream (allow_archiving=False) & lossy-arch (MP3 < lossless)
    archive_match = await service.resolve_track_cascading(
        identifiers={"isrc": "US123"},
        fallback={"title": "Demo Song", "artist": "Demo Artist"},
        purpose="archive",
    )
    assert archive_match is not None
    assert archive_match[0].id == "flac-arch"
    assert archive_match[2] is not None
    assert archive_match[2].codec == "flac"

    # 3. Post-download archival quality check rejects deceitful server that returns a lossy .mp3 file
    user = UserAccount(telegram_id=501, display_name="Uploader", role=UserRole.UPLOADER)
    await repo.upsert_user(user)
    req_record = await repo.create_song_request(
        SongRequestRecord(
            id=0,
            telegram_id=501,
            request_type=SongRequestType.TRACK,
            track_title="Disguised Lossy",
            artist_name="Demo Artist",
            status=SongRequestStatus.APPROVED,
        )
    )

    # Make flac_client falsely download an .mp3 file despite claiming FLAC
    flac_client.request_provision.return_value = ProvisionResponse(
        status="ready",
        job_id="job_deceit",
        download_url="/api/v1/artifacts/deceit.mp3",
    )

    async def _fake_download(
        download_url: str,
        target_path: Path,
        expected_sha256: str | None = None,
        timeout_seconds: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"ID3-fake-mp3-payload")
        return target_path

    service.download_file_with_checksum = AsyncMock(side_effect=_fake_download)  # type: ignore[method-assign]

    final_req = await service.execute_provision_request(req_record.id)
    assert final_req is not None
    assert final_req.status == SongRequestStatus.FAILED
    assert final_req.error_message is not None
    assert "archival quality" in final_req.error_message
    artifact_ready_cb.assert_not_awaited()
    assert list((tmp_path / "staging").glob("*.mp3")) == []


@pytest.mark.anyio
async def test_webapp_stream_cache_and_provider_resolve_endpoints(tmp_path: Path) -> None:
    """Verify WebApp status roles, /providers/resolve, HTTP 501 gate, and stream cache HIT/MISS."""
    upstream_hits: list[str] = []
    upstream_payload = b"0123456789abcdef-audio-bytes"

    async def handle_fake_stream(request: web.Request) -> web.Response:
        upstream_hits.append(request.match_info["tid"])
        return web.Response(body=upstream_payload, content_type="audio/webm")

    fake_app = web.Application()
    fake_app.router.add_get("/api/v1/stream/{tid}", handle_fake_stream)
    fake_runner = web.AppRunner(fake_app)
    await fake_runner.setup()
    fake_site = web.TCPSite(fake_runner, host="127.0.0.1", port=0)
    await fake_site.start()
    fake_sockets = getattr(getattr(fake_site, "_server", None), "sockets", None) or []
    fake_port = int(fake_sockets[0].getsockname()[1])
    fake_base_url = f"http://127.0.0.1:{fake_port}"

    db_manager = SqliteDatabaseManager(tmp_path / "webapp_providers.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    gov_service = GovernanceService(repository=repo, owner_telegram_ids=(9001,))
    await repo.upsert_user(UserAccount(telegram_id=9001, display_name="Owner", role=UserRole.OWNER))

    yt_srv = ProviderServerRecord(
        id="yt-1",
        name="YouTube Stream",
        base_url=fake_base_url,
        priority=5,
        is_enabled=True,
        allow_streaming=True,
        allow_archiving=False,
        capabilities=ProviderCapabilities(
            provider_name="youtube-stream",
            version="1.0.0",
            supported_sources=("youtube",),
            audio_profiles=ProviderAudioProfiles(supported_codecs=("opus",)),
            features=ProviderFeatures(streaming=True, provisioning=False),
        ),
    )
    flac_srv = ProviderServerRecord(
        id="flac-1",
        name="SpotiFLAC Archiver",
        base_url=fake_base_url,
        priority=10,
        is_enabled=True,
        allow_streaming=False,
        allow_archiving=True,
        min_archival_quality="lossless",
        capabilities=ProviderCapabilities(
            provider_name="spotiflac",
            version="1.0.0",
            supported_sources=("isrc",),
            features=ProviderFeatures(streaming=False, provisioning=True),
        ),
    )
    await repo.upsert_provider_server(yt_srv)
    await repo.upsert_provider_server(flac_srv)

    stream_cache = EphemeralStreamCache(
        cache_dir=tmp_path / "cache" / "streams",
        ttl_seconds=3600,
        max_bytes=10 * 1024 * 1024,
    )
    ps = MultiProviderService(repository=repo, min_archival_quality="lossless")
    yt_mock_client: Any = AsyncMock()
    yt_mock_client.get_stream_url = lambda tid, quality=None: f"{fake_base_url}/api/v1/stream/{tid}"
    yt_mock_client.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="yt_xyz",
        matched_source="youtube",
        streaming_supported=True,
        available_qualities=(
            AvailableAudioQuality(quality_id="opus_stream", codec="opus", bitrate_kbps=160),
        ),
    )
    ps.get_provider_client = lambda srv: yt_mock_client  # type: ignore[method-assign]

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov_service,
        host="127.0.0.1",
        port=0,
        owner_telegram_ids=(9001,),
        catalog_repository=repo,
        provider_service=ps,
        stream_cache=stream_cache,
    )
    await server.start()

    user_json = json.dumps({"id": 9001, "first_name": "Owner", "username": "owner"}, separators=(",", ":"))
    token = _build_signed_init_data({"auth_date": str(int(time.time())), "user": user_json})
    headers = {"Authorization": f"tma {token}"}
    base_url = f"http://127.0.0.1:{server.bound_port}"

    try:
        async with httpx.AsyncClient() as client:
            status_resp = await client.get(f"{base_url}/api/v1/app/providers/status", headers=headers)
            assert status_resp.status_code == 200
            status_data = status_resp.json()
            assert status_data["has_streaming_providers"] is True
            assert status_data["has_archival_providers"] is True

            resolve_resp = await client.post(
                f"{base_url}/api/v1/app/providers/resolve",
                headers=headers,
                json={"title": "Demo", "artist": "Artist", "purpose": "stream"},
            )
            assert resolve_resp.status_code == 200
            resolve_data = resolve_resp.json()
            assert resolve_data["matched"] is True
            assert resolve_data["provider_id"] == "yt-1"
            assert resolve_data["stream_url"] == "/api/v1/app/stream/provider/yt-1/yt_xyz"

            # Streaming on archival-only provider fails with 501
            denied_stream = await client.get(
                f"{base_url}/api/v1/app/stream/provider/flac-1/yt_xyz",
                headers=headers,
            )
            assert denied_stream.status_code == 501

            # 1st stream -> cache MISS, proxies from fake upstream server and stores in EphemeralStreamCache
            s1 = await client.get(
                f"{base_url}/api/v1/app/stream/provider/yt-1/yt_xyz",
                headers=headers,
            )
            assert s1.status_code == 200
            assert s1.headers.get("X-Stream-Cache") == "MISS"
            assert s1.content == upstream_payload
            assert len(upstream_hits) == 1

            # 2nd stream -> cache HIT with Range support and zero new upstream hits
            s2 = await client.get(
                f"{base_url}/api/v1/app/stream/provider/yt-1/yt_xyz",
                headers={**headers, "Range": "bytes=0-3"},
            )
            assert s2.status_code == 206
            assert s2.headers.get("X-Stream-Cache") == "HIT"
            assert s2.content == b"0123"
            assert len(upstream_hits) == 1
    finally:
        await server.stop()
        await fake_runner.cleanup()


@pytest.mark.anyio
async def test_single_dual_capability_provider_auto_discovery_and_routing(tmp_path: Path) -> None:
    """Verify a single provider advertising streaming + provisioning drives both roles automatically."""
    db = SqliteDatabaseManager(tmp_path / "dual_provider.db")
    await db.initialize()
    repo = SqliteCatalogRepository(db)

    gov_service = GovernanceService(
        repository=repo,
        owner_telegram_ids=(9002,),
    )
    await repo.upsert_user(
        UserAccount(telegram_id=9002, role=UserRole.OWNER, username="owner", display_name="Owner")
    )

    dual_caps = ProviderCapabilities(
        provider_name="composite-node",
        version="1.0.0",
        supported_sources=("spotify", "deezer", "youtube", "isrc"),
        audio_profiles=ProviderAudioProfiles(
            supported_codecs=("flac", "opus", "aac"),
            min_bit_depth=16,
            max_bit_depth=24,
        ),
        features=ProviderFeatures(streaming=True, provisioning=True),
    )

    fake_client: Any = AsyncMock()
    fake_client.check_health.return_value = ProviderHealth(status="healthy")
    fake_client.get_capabilities.return_value = dual_caps
    fake_client.get_stream_url = HttpMusicProviderClient("http://composite.local:8000").get_stream_url

    ps = MultiProviderService(
        repository=repo,
        client_factory=lambda url, token: fake_client,
        min_archival_quality="lossless",
    )

    # Register by URL with default open flags (`allow_streaming=True, allow_archiving=True`)
    registered = await ps.handshake_and_update(
        ProviderServerRecord(
            id="dual-1",
            name="Composite Node",
            base_url="http://composite.local:8000",
            priority=10,
            is_enabled=True,
        )
    )
    assert registered.supports_streaming is True
    assert registered.supports_archiving is True
    assert (
        fake_client.get_stream_url(
            "spoti_7",
            metadata_hints={"title": "A", "artist": "B"},
        )
        == "http://composite.local:8000/api/v1/stream/spoti_7?title=A&artist=B"
    )

    # Lossy YouTube fallback works for streaming, while archival quality gate still blocks it
    fake_client.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="yt_fallback",
        matched_source="youtube",
        streaming_supported=True,
        available_qualities=(
            AvailableAudioQuality(quality_id="opus_stream", codec="opus", bitrate_kbps=160),
        ),
    )
    stream_res = await ps.resolve_track_cascading(
        identifiers={}, fallback={"title": "Fallback Song", "artist": "Artist"}, purpose="stream"
    )
    assert stream_res is not None
    assert stream_res[0].id == "dual-1"
    assert stream_res[1].provider_track_id == "yt_fallback"

    arch_blocked = await ps.resolve_track_cascading(
        identifiers={}, fallback={"title": "Fallback Song", "artist": "Artist"}, purpose="archive"
    )
    assert arch_blocked is None

    # Lossless resolution succeeds for BOTH archival and streaming
    fake_client.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="spoti_lossless",
        matched_source="spotify",
        streaming_supported=True,
        available_qualities=(
            AvailableAudioQuality(
                quality_id="lossless_cd", codec="flac", bit_depth=16, sample_rate_hz=44100, bitrate_kbps=1411
            ),
        ),
    )
    arch_ok = await ps.resolve_track_cascading(
        identifiers={"isrc": "US123"}, fallback={"title": "HI-RES", "artist": "Artist"}, purpose="archive"
    )
    assert arch_ok is not None
    assert arch_ok[1].provider_track_id == "spoti_lossless"

    # WebApp /providers/status lights up both flags from this single provider
    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov_service,
        host="127.0.0.1",
        port=0,
        owner_telegram_ids=(9002,),
        catalog_repository=repo,
        provider_service=ps,
    )
    await server.start()
    try:
        user_json = json.dumps(
            {"id": 9002, "first_name": "Owner", "username": "owner"}, separators=(",", ":")
        )
        token = _build_signed_init_data({"auth_date": str(int(time.time())), "user": user_json})
        headers = {"Authorization": f"tma {token}"}
        async with httpx.AsyncClient() as client:
            status_resp = await client.get(
                f"http://127.0.0.1:{server.bound_port}/api/v1/app/providers/status",
                headers=headers,
            )
            assert status_resp.status_code == 200
            data = status_resp.json()
            assert data["enabled_count"] == 1
            assert data["has_streaming_providers"] is True
            assert data["has_archival_providers"] is True
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_stream_enrichment_cover_lyrics_and_resolve_metadata(tmp_path: Path) -> None:
    """Verify streamed resolve metadata, Deezer cover enrichment, and LRCLIB synced lyrics endpoint."""
    # 1. Verify HttpMusicProviderClient parses metadata.cover_url
    mock_provider_transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "matched": True,
                "provider_track_id": "yt_abc123",
                "matched_source": "youtube",
                "streaming_supported": True,
                "metadata": {
                    "title": "Trasparente",
                    "artist": "Emanuele Aloia",
                    "album": "Trasparente (Single)",
                    "duration_ms": 168000,
                    "isrc": "IT1234567890",
                    "cover_url": "https://img.example.com/cover.jpg",
                },
                "available_qualities": [{"quality_id": "opus_stream", "codec": "opus", "bitrate_kbps": 160}],
            },
        )
    )
    async with httpx.AsyncClient(transport=mock_provider_transport) as mock_http:
        client = HttpMusicProviderClient(base_url="http://provider.local:8000", client=mock_http)
        resolved = await client.resolve_track(identifiers={}, fallback={"title": "Trasparente"})
        assert resolved.matched is True
        assert resolved.cover_url == "https://img.example.com/cover.jpg"
        assert resolved.metadata is not None
        assert resolved.metadata.album == "Trasparente (Single)"

    # 2. Verify DeezerReleaseProvider.search_track_enrichment extracts high-res cover & canonical tags
    deezer_transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 918273,
                        "title": "Trasparente",
                        "duration": 168,
                        "track_position": 1,
                        "link": "https://www.deezer.com/track/918273",
                        "artist": {"name": "Emanuele Aloia"},
                        "album": {
                            "title": "Sindache",
                            "cover_xl": "https://cdn-images.dzcdn.net/images/cover/xyz/1000x1000.jpg",
                        },
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient(transport=deezer_transport) as dz_http:
        dz_provider = DeezerReleaseProvider(client=dz_http)
        dz_info = await dz_provider.search_track_enrichment(title="Trasparente", artist="Emanuele Aloia")
        assert dz_info is not None
        assert dz_info["album"] == "Sindache"
        assert dz_info["cover_url"] == "https://cdn-images.dzcdn.net/images/cover/xyz/1000x1000.jpg"
        assert dz_info["deezer_id"] == "918273"

    # 3. Verify WebApp /providers/resolve and /streams/enrichment endpoints end-to-end
    db_mgr = SqliteDatabaseManager(tmp_path / "enrichment.db")
    await db_mgr.initialize()
    repo = SqliteCatalogRepository(db_mgr)
    gov_service = GovernanceService(repository=repo, owner_telegram_ids=(9003,))
    await repo.upsert_user(
        UserAccount(telegram_id=9003, role=UserRole.OWNER, username="owner", display_name="Owner")
    )

    fake_prov_client: Any = AsyncMock()
    fake_prov_client.check_health.return_value = ProviderHealth(status="healthy")
    fake_prov_client.get_capabilities.return_value = ProviderCapabilities(
        provider_name="composite",
        version="1.0.0",
        supported_sources=("spotify", "youtube"),
        audio_profiles=ProviderAudioProfiles(supported_codecs=("flac", "opus")),
        features=ProviderFeatures(streaming=True, provisioning=True),
    )
    fake_prov_client.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="yt_trasparente",
        matched_source="youtube",
        streaming_supported=True,
        cover_url="https://img.youtube.com/vi/abc/hqdefault.jpg",
        metadata=TrackMetadata(
            title="Trasparente",
            artist="Emanuele Aloia",
            album="",
            duration_seconds=168,
            isrc="IT1234567890",
        ),
        available_qualities=(
            AvailableAudioQuality(quality_id="opus_stream", codec="opus", bitrate_kbps=160),
        ),
    )
    ps = MultiProviderService(repository=repo, client_factory=lambda u, t: fake_prov_client)
    await ps.handshake_and_update(
        ProviderServerRecord(
            id="comp-1",
            name="Composite",
            base_url="http://comp.local:8000",
            priority=10,
            is_enabled=True,
        )
    )

    mock_radar: Any = MagicMock()
    mock_radar._provider = MagicMock()
    mock_radar._provider.search_track_enrichment = AsyncMock(
        return_value={
            "deezer_id": "918273",
            "title": "Trasparente",
            "artist": "Emanuele Aloia",
            "album": "Trasparente",
            "cover_url": "https://cdn-images.dzcdn.net/images/cover/xyz/1000x1000.jpg",
            "duration_seconds": 168,
            "track_number": 1,
            "external_url": "https://www.deezer.com/track/918273",
        }
    )

    mock_lyrics: Any = AsyncMock(spec=LrclibLyricsClient)
    mock_lyrics.fetch_lyrics = AsyncMock(
        return_value=LyricsData(
            synced_lyrics="[00:05.00]Trasparente come vetro\n[00:10.00]Sotto questo cielo",
            plain_lyrics="Trasparente come vetro\nSotto questo cielo",
        )
    )

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov_service,
        host="127.0.0.1",
        port=0,
        owner_telegram_ids=(9003,),
        catalog_repository=repo,
        provider_service=ps,
        radar_service=mock_radar,
        lyrics_client=mock_lyrics,
    )
    await server.start()
    try:
        user_json = json.dumps(
            {"id": 9003, "first_name": "Owner", "username": "owner"}, separators=(",", ":")
        )
        token = _build_signed_init_data({"auth_date": str(int(time.time())), "user": user_json})
        headers = {"Authorization": f"tma {token}"}
        async with httpx.AsyncClient() as http:
            resolve_resp = await http.post(
                f"http://127.0.0.1:{server.bound_port}/api/v1/app/providers/resolve",
                json={"title": "Trasparente", "artist": "Emanuele Aloia", "purpose": "stream"},
                headers=headers,
            )
            assert resolve_resp.status_code == 200
            r_data = resolve_resp.json()
            assert r_data["stream_url"] == "/api/v1/app/stream/provider/comp-1/yt_trasparente"
            assert r_data["cover_url"] == "https://img.youtube.com/vi/abc/hqdefault.jpg"
            assert r_data["metadata"]["title"] == "Trasparente"
            assert r_data["metadata"]["isrc"] == "IT1234567890"

            enrich_resp = await http.get(
                f"http://127.0.0.1:{server.bound_port}/api/v1/app/streams/enrichment",
                params={"title": "Trasparente", "artist": "Emanuele Aloia", "duration": "168"},
                headers=headers,
            )
            assert enrich_resp.status_code == 200
            e_data = enrich_resp.json()
            assert e_data["found"] is True
            assert e_data["cover_url"] == "https://cdn-images.dzcdn.net/images/cover/xyz/1000x1000.jpg"
            assert e_data["album"] == "Trasparente"
            assert e_data["has_synced"] is True
            assert "[00:05.00]Trasparente" in e_data["synced_lyrics"]
            assert mock_lyrics.fetch_lyrics.await_count == 1

            # Second identical lookup must hit the in-memory cache cleanly
            enrich_cached = await http.get(
                f"http://127.0.0.1:{server.bound_port}/api/v1/app/streams/enrichment",
                params={"title": "Trasparente", "artist": "Emanuele Aloia", "duration": "168"},
                headers=headers,
            )
            assert enrich_cached.status_code == 200
            assert mock_lyrics.fetch_lyrics.await_count == 1
    finally:
        await server.stop()
