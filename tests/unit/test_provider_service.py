"""Unit tests for HttpMusicProviderClient and MultiProviderService."""

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.core.exceptions import (
    ProvisioningFailedError,
)
from src.core.models import (
    AvailableAudioQuality,
    ProviderServerRecord,
    TrackResolutionResult,
)
from src.core.provider import (
    HttpMusicProviderClient,
    MultiProviderService,
    _parse_audio_profiles,
    _parse_provider_features,
    _parse_quality_item,
    rank_quality,
)


def test_parse_helpers() -> None:
    prof = _parse_audio_profiles(
        {
            "audio_profiles": {
                "supported_codecs": ["FLAC", "MP3"],
                "min_bitrate_kbps": 320,
                "max_bitrate_kbps": 3000,
                "min_sample_rate_hz": 44100,
                "max_sample_rate_hz": 96000,
                "min_bit_depth": 16,
                "max_bit_depth": 24,
            }
        }
    )
    assert prof.supported_codecs == ("flac", "mp3")
    assert prof.max_bit_depth == 24
    assert prof.max_sample_rate_hz == 96000

    feat = _parse_provider_features(
        {
            "features": {
                "streaming": True,
                "stream_formats": ["audio/flac", "audio/mpeg"],
                "async_provisioning": True,
                "webhooks": True,
                "batch_provisioning": True,
            }
        }
    )
    assert feat.streaming is True
    assert feat.stream_formats == ("audio/flac", "audio/mpeg")
    assert feat.batch_provisioning is True

    q = _parse_quality_item(
        {
            "quality_id": "hi_res",
            "codec": "flac",
            "bit_depth": 24,
            "sample_rate_hz": 96000,
            "bitrate_kbps": 3000,
            "approx_file_size_bytes": 70000000,
        }
    )
    assert q.is_lossless is True
    assert "24-bit/96kHz" in q.display_badge


def test_rank_quality_ordering() -> None:
    q_24_96 = AvailableAudioQuality(
        quality_id="q1", codec="flac", bit_depth=24, sample_rate_hz=96000, bitrate_kbps=3000
    )
    q_16_44 = AvailableAudioQuality(
        quality_id="q2", codec="flac", bit_depth=16, sample_rate_hz=44100, bitrate_kbps=1411
    )
    q_mp3_320 = AvailableAudioQuality(
        quality_id="q3", codec="mp3", bit_depth=16, sample_rate_hz=44100, bitrate_kbps=320
    )
    q_mp3_128 = AvailableAudioQuality(
        quality_id="q4", codec="mp3", bit_depth=16, sample_rate_hz=44100, bitrate_kbps=128
    )

    ranked = sorted([q_mp3_128, q_24_96, q_mp3_320, q_16_44], key=rank_quality, reverse=True)
    assert ranked == [q_24_96, q_16_44, q_mp3_320, q_mp3_128]


@pytest.mark.anyio
async def test_http_provider_client_capabilities() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/capabilities"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "provider_name": "mock-qobuz",
                "version": "1.2.0",
                "supported_sources": ["spotify", "isrc", "qobuz"],
                "audio_profiles": {"supported_codecs": ["flac"]},
                "features": {"streaming": True},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = HttpMusicProviderClient(
            base_url="http://provider.local",
            api_token="test-token",
            client=client,
        )
        caps = await provider.get_capabilities()
        assert caps.provider_name == "mock-qobuz"
        assert caps.version == "1.2.0"
        assert "qobuz" in caps.supported_sources
        assert caps.features.streaming is True


@pytest.mark.anyio
async def test_http_provider_client_health() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "healthy", "version": "1.2.0", "active_jobs_count": 3})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = HttpMusicProviderClient(base_url="http://provider.local", client=client)
        health = await provider.check_health()
        assert health.status == "healthy"
        assert health.active_jobs_count == 3


@pytest.mark.anyio
async def test_http_provider_client_resolve_track() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/resolve"
        return httpx.Response(
            200,
            json={
                "matched": True,
                "provider_track_id": "trk_12345",
                "matched_source": "isrc",
                "metadata": {
                    "title": "Never Gonna Give You Up",
                    "artist": "Rick Astley",
                    "album": "Whenever You Need Somebody",
                    "duration_ms": 213000,
                    "isrc": "USUM71703861",
                },
                "available_qualities": [
                    {
                        "quality_id": "lossless_hi_res",
                        "codec": "flac",
                        "bit_depth": 24,
                        "sample_rate_hz": 96000,
                        "bitrate_kbps": 3000,
                    }
                ],
                "streaming_supported": True,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = HttpMusicProviderClient(base_url="http://provider.local", client=client)
        res = await provider.resolve_track(
            identifiers={"isrc": "USUM71703861"},
            fallback={"title": "Never Gonna Give You Up", "artist": "Rick Astley"},
        )
        assert res.matched is True
        assert res.provider_track_id == "trk_12345"
        assert res.metadata is not None
        assert res.metadata.title == "Never Gonna Give You Up"
        assert len(res.available_qualities) == 1
        assert res.available_qualities[0].bit_depth == 24
        assert res.streaming_supported is True


@pytest.mark.anyio
async def test_http_provider_client_provision_instant_and_async() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/provision":
            data = request.read().decode()
            if "instant" in data:
                return httpx.Response(
                    200,
                    json={
                        "status": "ready",
                        "download_url": "/api/v1/artifacts/track1.flac",
                        "checksum": {"hash": "abc123sha"},
                        "file_info": {
                            "format": "flac",
                            "size_bytes": 1024,
                            "sample_rate": 96000,
                            "bit_depth": 24,
                        },
                    },
                )
            return httpx.Response(
                202,
                json={
                    "status": "queued",
                    "job_id": "job_777",
                    "poll_url": "/api/v1/jobs/job_777",
                },
            )
        if request.url.path == "/api/v1/jobs/job_777":
            return httpx.Response(
                200,
                json={
                    "job_id": "job_777",
                    "status": "completed",
                    "progress_percent": 100.0,
                    "download_url": "http://provider.local/api/v1/artifacts/job_777.flac",
                    "checksum": {"hash": "def456sha"},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = HttpMusicProviderClient(base_url="http://provider.local", client=client)

        resp_instant = await provider.request_provision(provider_track_id="instant_trk")
        assert resp_instant.status == "ready"
        assert resp_instant.download_url == "http://provider.local/api/v1/artifacts/track1.flac"
        assert resp_instant.checksum_sha256 == "abc123sha"

        resp_async = await provider.request_provision(provider_track_id="slow_trk")
        assert resp_async.status == "queued"
        assert resp_async.job_id == "job_777"

        job_status = await provider.get_job_status("job_777")
        assert job_status.status == "completed"
        assert job_status.progress_percent == 100.0
        assert job_status.download_url == "http://provider.local/api/v1/artifacts/job_777.flac"


@pytest.mark.anyio
async def test_multi_provider_service_resolve_cascading_picks_best_quality() -> None:
    repo = AsyncMock()

    srv_deezer = ProviderServerRecord(
        id="deezer-worker",
        name="Deezer Worker",
        base_url="http://deezer.local",
        priority=20,
        is_enabled=True,
    )
    srv_qobuz = ProviderServerRecord(
        id="qobuz-worker",
        name="Qobuz Worker",
        base_url="http://qobuz.local",
        priority=10,
        is_enabled=True,
    )
    repo.list_provider_servers.return_value = [srv_deezer, srv_qobuz]

    # Qobuz provides 24-bit/96kHz FLAC
    client_qobuz = AsyncMock()
    client_qobuz.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="qobuz_trk_1",
        matched_source="isrc",
        available_qualities=(
            AvailableAudioQuality(
                quality_id="qobuz_hi_res", codec="flac", bit_depth=24, sample_rate_hz=96000, bitrate_kbps=3000
            ),
        ),
    )

    # Deezer provides 16-bit/44.1kHz FLAC
    client_deezer = AsyncMock()
    client_deezer.resolve_track.return_value = TrackResolutionResult(
        matched=True,
        provider_track_id="deezer_trk_1",
        matched_source="isrc",
        available_qualities=(
            AvailableAudioQuality(
                quality_id="deezer_cd", codec="flac", bit_depth=16, sample_rate_hz=44100, bitrate_kbps=1411
            ),
        ),
    )

    def client_factory(url: str, token: str | None) -> Any:
        if "qobuz" in url:
            return client_qobuz
        return client_deezer

    multi_svc = MultiProviderService(repository=repo, client_factory=client_factory)
    result = await multi_svc.resolve_track_cascading(identifiers={"isrc": "USUM71703861"})
    assert result is not None
    best_server, best_res, best_quality = result
    assert best_server.id == "qobuz-worker"
    assert best_quality is not None
    assert best_quality.bit_depth == 24
    assert best_quality.sample_rate_hz == 96000


@pytest.mark.anyio
async def test_download_file_with_checksum(tmp_path: Path) -> None:
    content = b"Audio stream bytes mock flac"
    expected_hash = hashlib.sha256(content).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    transport = httpx.MockTransport(handler)

    repo = AsyncMock()
    multi_svc = MultiProviderService(repository=repo)

    # Patch httpx.AsyncClient inside download_file_with_checksum
    target_file = tmp_path / "downloaded.flac"

    real_async_client = httpx.AsyncClient
    with patch("httpx.AsyncClient", side_effect=lambda **kw: real_async_client(transport=transport)):
        downloaded = await multi_svc.download_file_with_checksum(
            download_url="http://provider.local/audio.flac",
            target_path=target_file,
            expected_sha256=expected_hash,
        )
        assert downloaded.is_file()
        assert downloaded.read_bytes() == content

        # Checksum mismatch
        with pytest.raises(ProvisioningFailedError):
            await multi_svc.download_file_with_checksum(
                download_url="http://provider.local/audio.flac",
                target_path=tmp_path / "bad.flac",
                expected_sha256="wrong_checksum",
            )
