"""HTTP client, multi-provider cascading registry, and acquisition orchestration service."""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from src.core.exceptions import (
    ProviderError,
    ProviderResolutionError,
    ProviderUnavailableError,
    ProvisioningFailedError,
)
from src.core.models import (
    AlbumResolutionResult,
    AlbumTrackResolution,
    AvailableAudioQuality,
    Permission,
    ProviderAudioProfiles,
    ProviderCapabilities,
    ProviderFeatures,
    ProviderHealth,
    ProviderServerRecord,
    ProvisionJobStatus,
    ProvisionResponse,
    SongRequestRecord,
    SongRequestStatus,
    SongRequestType,
    TrackMetadata,
    TrackResolutionResult,
    UserAccount,
    UserRole,
)
from src.core.ports import (
    AdminLogNotifierPort,
    GovernanceRepositoryPort,
    MusicProviderPort,
)
from src.core.rbac import require_permission

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_TIMEOUT_SECONDS: float = 15.0
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS: float = 180.0


def _parse_audio_profiles(data: dict[str, Any]) -> ProviderAudioProfiles:
    """Safely parse ProviderAudioProfiles from capabilities dictionary."""
    prof = data.get("audio_profiles") or {}
    codecs_raw = prof.get("supported_codecs")
    codecs = (
        tuple(str(c).lower() for c in codecs_raw)
        if isinstance(codecs_raw, (list, tuple))
        else ("flac", "mp3")
    )
    return ProviderAudioProfiles(
        supported_codecs=codecs,
        min_bitrate_kbps=int(prof.get("min_bitrate_kbps") or 128),
        max_bitrate_kbps=int(prof.get("max_bitrate_kbps") or 9216),
        min_sample_rate_hz=int(prof.get("min_sample_rate_hz") or 44100),
        max_sample_rate_hz=int(prof.get("max_sample_rate_hz") or 192000),
        min_bit_depth=int(prof.get("min_bit_depth") or 16),
        max_bit_depth=int(prof.get("max_bit_depth") or 24),
    )


def _parse_provider_features(data: dict[str, Any]) -> ProviderFeatures:
    """Safely parse ProviderFeatures from capabilities dictionary."""
    feat = data.get("features") or {}
    stream_fmts_raw = feat.get("stream_formats")
    stream_fmts = (
        tuple(str(f) for f in stream_fmts_raw)
        if isinstance(stream_fmts_raw, (list, tuple))
        else ("audio/mpeg",)
    )
    return ProviderFeatures(
        streaming=bool(feat.get("streaming", False)),
        stream_formats=stream_fmts,
        async_provisioning=bool(feat.get("async_provisioning", True)),
        webhooks=bool(feat.get("webhooks", False)),
        batch_provisioning=bool(feat.get("batch_provisioning", False)),
    )


def _parse_quality_item(item: dict[str, Any]) -> AvailableAudioQuality:
    """Parse single AvailableAudioQuality item."""
    return AvailableAudioQuality(
        quality_id=str(item.get("quality_id") or "standard"),
        codec=str(item.get("codec") or "mp3").lower(),
        bit_depth=int(item.get("bit_depth") or 16),
        sample_rate_hz=int(item.get("sample_rate_hz") or 44100),
        bitrate_kbps=int(item.get("bitrate_kbps") or 320),
        approx_file_size_bytes=int(item.get("approx_file_size_bytes") or 0),
    )


def rank_quality(quality: AvailableAudioQuality) -> tuple[int, int, int, int]:
    """Score an audio quality for sorting (higher is better).

    Priority:
    1. Lossless (1) vs Lossy (0)
    2. Bit depth (24 vs 16)
    3. Sample rate (e.g. 96000 vs 44100)
    4. Bitrate in kbps
    """
    is_lossless = 1 if quality.is_lossless else 0
    return (is_lossless, quality.bit_depth, quality.sample_rate_hz, quality.bitrate_kbps)


class HttpMusicProviderClient(MusicProviderPort):
    """Production HTTP client for communicating with an external music provider microservice."""

    def __init__(
        self,
        base_url: str,
        api_token: str | None = None,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token.strip() if api_token and api_token.strip() else None
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    def _build_headers(self) -> dict[str, str]:
        """Construct HTTP headers including optional Bearer token authentication."""
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        return headers

    def get_auth_headers(self) -> dict[str, str]:
        """Return authentication headers suitable for streaming/downloading artifacts."""
        if self._api_token:
            return {"Authorization": f"Bearer {self._api_token}"}
        return {}

    def resolve_artifact_url(self, download_url: str) -> str:
        """Resolve relative artifact/download URLs against this provider client's base URL."""
        cleaned = download_url.strip()
        if cleaned.startswith(("http://", "https://")):
            return cleaned
        return urljoin(f"{self._base_url}/", cleaned.lstrip("/"))

    async def _get_client(self) -> httpx.AsyncClient:
        """Return injected or lazily initialized HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        """Close underlying HTTP client if owned."""
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_capabilities(self) -> ProviderCapabilities:
        """Fetch provider capabilities from `GET /api/v1/capabilities`."""
        client = await self._get_client()
        url = f"{self._base_url}/api/v1/capabilities"
        try:
            resp = await client.get(url, headers=self._build_headers())
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch capabilities from %s: %s", url, exc)
            raise ProviderUnavailableError(f"Provider capabilities unreachable at {url}: {exc}") from exc

        sources_raw = data.get("supported_sources") or ()
        sources = tuple(str(s).lower() for s in sources_raw)
        return ProviderCapabilities(
            provider_name=str(data.get("provider_name") or "unknown-provider"),
            version=str(data.get("version") or "1.0.0"),
            supported_sources=sources,
            audio_profiles=_parse_audio_profiles(data),
            features=_parse_provider_features(data),
        )

    async def check_health(self, verify: bool = False) -> ProviderHealth:
        """Check provider service status from `GET /api/v1/health`."""
        client = await self._get_client()
        url = f"{self._base_url}/api/v1/health"
        params = {"verify": "true"} if verify else None
        try:
            resp = await client.get(url, params=params, headers=self._build_headers())
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Health check failed for provider %s: %s", url, exc)
            return ProviderHealth(status="unreachable", upstream_dependencies={"error": str(exc)})

        return ProviderHealth(
            status=str(data.get("status") or "ok"),
            version=str(data.get("version") or ""),
            upstream_dependencies=dict(data.get("upstream_dependencies") or {}),
            active_jobs_count=int(data.get("active_jobs_count") or 0),
        )

    async def resolve_track(
        self,
        identifiers: dict[str, str],
        fallback: dict[str, object] | None = None,
    ) -> TrackResolutionResult:
        """Query availability and audio quality for a track via `POST /api/v1/resolve`."""
        client = await self._get_client()
        url = f"{self._base_url}/api/v1/resolve"
        payload: dict[str, Any] = {
            "item_type": "track",
            "identifiers": identifiers,
            "metadata_fallback": fallback or {},
        }
        try:
            resp = await client.post(url, json=payload, headers=self._build_headers())
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("Resolution failed on provider %s: %s", url, exc)
            raise ProviderResolutionError(f"Provider track resolution failed at {url}: {exc}") from exc

        matched = bool(data.get("matched", False))
        if not matched:
            return TrackResolutionResult(matched=False)

        meta_dict = data.get("metadata") or {}
        track_meta: TrackMetadata | None = None
        if meta_dict and "title" in meta_dict and "artist" in meta_dict:
            dur_ms = meta_dict.get("duration_ms")
            dur_sec = int(round(dur_ms / 1000)) if isinstance(dur_ms, (int, float)) and dur_ms > 0 else None
            track_meta = TrackMetadata(
                title=str(meta_dict.get("title") or ""),
                artist=str(meta_dict.get("artist") or ""),
                album=str(meta_dict.get("album") or ""),
                duration_seconds=dur_sec,
                isrc=str(meta_dict.get("isrc") or "") or None,
            )

        qualities_raw = data.get("available_qualities") or ()
        qualities = tuple(_parse_quality_item(q) for q in qualities_raw if isinstance(q, dict))

        return TrackResolutionResult(
            matched=True,
            provider_track_id=str(data.get("provider_track_id") or "") or None,
            matched_source=str(data.get("matched_source") or "") or None,
            metadata=track_meta,
            available_qualities=qualities,
            streaming_supported=bool(data.get("streaming_supported", False)),
        )

    async def resolve_album(
        self,
        identifiers: dict[str, str],
        fallback: dict[str, object] | None = None,
    ) -> AlbumResolutionResult:
        """Query availability and audio quality for an album via `POST /api/v1/resolve`."""
        client = await self._get_client()
        url = f"{self._base_url}/api/v1/resolve"
        payload: dict[str, Any] = {
            "item_type": "album",
            "identifiers": identifiers,
            "metadata_fallback": fallback or {},
        }
        try:
            resp = await client.post(url, json=payload, headers=self._build_headers())
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("Album resolution failed on provider %s: %s", url, exc)
            raise ProviderResolutionError(f"Provider album resolution failed at {url}: {exc}") from exc

        matched = bool(data.get("matched", False))
        if not matched:
            return AlbumResolutionResult(matched=False)

        tracks_raw = data.get("tracks") or ()
        tracks: list[AlbumTrackResolution] = []
        for idx, t in enumerate(tracks_raw, start=1):
            if isinstance(t, dict):
                t_qualities = tuple(_parse_quality_item(q) for q in (t.get("available_qualities") or ()))
                dur_ms = t.get("duration_ms") or 0
                dur_sec = int(round(dur_ms / 1000)) if dur_ms > 0 else int(t.get("duration_seconds") or 0)
                tracks.append(
                    AlbumTrackResolution(
                        position=int(t.get("position") or idx),
                        title=str(t.get("title") or f"Track {idx}"),
                        provider_track_id=str(t.get("provider_track_id") or "") or None,
                        duration_seconds=dur_sec,
                        matched=bool(t.get("matched", True)),
                        available_qualities=t_qualities,
                    )
                )

        album_qualities_raw = data.get("available_qualities") or ()
        album_qualities = tuple(_parse_quality_item(q) for q in album_qualities_raw if isinstance(q, dict))

        return AlbumResolutionResult(
            matched=True,
            provider_album_id=str(data.get("provider_album_id") or "") or None,
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
            total_tracks=int(data.get("total_tracks") or len(tracks)),
            matched_tracks_count=int(
                data.get("matched_tracks_count") or len([t for t in tracks if t.matched])
            ),
            tracks=tuple(tracks),
            available_qualities=album_qualities,
        )

    async def request_provision(
        self,
        provider_track_id: str,
        target_quality: str | None = None,
        callback_url: str | None = None,
        priority: str = "normal",
    ) -> ProvisionResponse:
        """Trigger track provisioning on `POST /api/v1/provision`."""
        client = await self._get_client()
        url = f"{self._base_url}/api/v1/provision"
        payload: dict[str, Any] = {
            "provider_track_id": provider_track_id,
            "target_quality": target_quality or "best",
            "priority": priority,
        }
        if callback_url:
            payload["callback_url"] = callback_url

        try:
            resp = await client.post(url, json=payload, headers=self._build_headers())
            if resp.status_code not in (200, 202):
                resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("Provisioning failed on provider %s: %s", url, exc)
            raise ProvisioningFailedError(f"Provider provisioning request failed at {url}: {exc}") from exc

        checksum_dict = data.get("checksum") or {}
        file_info = data.get("file_info") or {}

        # Resolve relative download URL against base_url if necessary
        download_url = data.get("download_url")
        if download_url and isinstance(download_url, str) and download_url.startswith("/"):
            download_url = urljoin(f"{self._base_url}/", download_url.lstrip("/"))

        return ProvisionResponse(
            status=str(data.get("status") or ("ready" if resp.status_code == 200 else "queued")),
            download_url=download_url,
            job_id=str(data.get("job_id") or "") or None,
            poll_url=str(data.get("poll_url") or "") or None,
            expires_at=str(data.get("expires_at") or "") or None,
            checksum_sha256=str(checksum_dict.get("hash") or "") or None,
            file_format=str(file_info.get("format") or "") or None,
            file_size_bytes=int(file_info.get("size_bytes") or 0),
            sample_rate=int(file_info.get("sample_rate") or 44100),
            bit_depth=int(file_info.get("bit_depth") or 16),
            bitrate_kbps=int(file_info.get("bitrate_kbps") or 0),
            error_message=str(data.get("error") or "") or None,
        )

    async def request_batch_provision(
        self,
        provider_album_id: str,
        track_ids: Sequence[str],
        target_quality: str | None = None,
        callback_url: str | None = None,
        priority: str = "normal",
    ) -> ProvisionResponse:
        """Trigger batch album provisioning on `POST /api/v1/provision`."""
        client = await self._get_client()
        url = f"{self._base_url}/api/v1/provision"
        payload: dict[str, Any] = {
            "provider_album_id": provider_album_id,
            "track_ids": list(track_ids),
            "target_quality": target_quality or "best",
            "priority": priority,
        }
        if callback_url:
            payload["callback_url"] = callback_url

        try:
            resp = await client.post(url, json=payload, headers=self._build_headers())
            if resp.status_code not in (200, 202):
                resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("Batch provisioning failed on provider %s: %s", url, exc)
            raise ProvisioningFailedError(f"Provider batch provisioning failed at {url}: {exc}") from exc

        download_url = data.get("download_url")
        if download_url and isinstance(download_url, str) and download_url.startswith("/"):
            download_url = urljoin(f"{self._base_url}/", download_url.lstrip("/"))

        return ProvisionResponse(
            status=str(data.get("status") or ("ready" if resp.status_code == 200 else "queued")),
            download_url=download_url,
            job_id=str(data.get("job_id") or "") or None,
            poll_url=str(data.get("poll_url") or "") or None,
            expires_at=str(data.get("expires_at") or "") or None,
            file_format=str((data.get("file_info") or {}).get("format") or "zip"),
            error_message=str(data.get("error") or "") or None,
        )

    async def get_job_status(self, job_id: str) -> ProvisionJobStatus:
        """Query async job progress from `GET /api/v1/jobs/{job_id}`."""
        client = await self._get_client()
        url = f"{self._base_url}/api/v1/jobs/{job_id}"
        try:
            resp = await client.get(url, headers=self._build_headers())
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("Job status check failed on provider %s: %s", url, exc)
            raise ProviderError(f"Failed to check job {job_id} at {url}: {exc}") from exc

        checksum_dict = data.get("checksum") or {}
        file_info = data.get("file_info") or {}
        download_url = data.get("download_url")
        if download_url and isinstance(download_url, str) and download_url.startswith("/"):
            download_url = urljoin(f"{self._base_url}/", download_url.lstrip("/"))

        return ProvisionJobStatus(
            job_id=str(data.get("job_id") or job_id),
            status=str(data.get("status") or "queued"),
            progress_percent=float(data.get("progress_percent") or 0.0),
            download_url=download_url,
            expires_at=str(data.get("expires_at") or "") or None,
            checksum_sha256=str(checksum_dict.get("hash") or "") or None,
            file_format=str(file_info.get("format") or "") or None,
            file_size_bytes=int(file_info.get("size_bytes") or 0),
            error=str(data.get("error") or "") or None,
        )

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel in-progress provisioning job on `DELETE /api/v1/jobs/{job_id}`."""
        client = await self._get_client()
        url = f"{self._base_url}/api/v1/jobs/{job_id}"
        try:
            resp = await client.delete(url, headers=self._build_headers())
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.warning("Failed to cancel job %s on %s: %s", job_id, url, exc)
            return False

    def get_stream_url(self, provider_track_id: str, quality: str | None = None) -> str:
        """Build streaming audio endpoint URL for a track."""
        suffix = f"?quality={quality}" if quality else ""
        return f"{self._base_url}/api/v1/stream/{provider_track_id}{suffix}"


ArtifactReadyCallback = Callable[
    [Path, SongRequestRecord, ProviderServerRecord],
    Awaitable[str | None],
]
RequesterNotifyCallback = Callable[[int, str], Awaitable[None]]


class MultiProviderService:
    """Orchestrates multi-provider discovery, fallback cascading, quality ranking, and provisioning."""

    def __init__(
        self,
        repository: GovernanceRepositoryPort,
        notifier: AdminLogNotifierPort | None = None,
        client_factory: Any = None,
        *,
        staging_dir: Path | None = None,
        auto_provision_uploader_requests: bool = True,
        download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        on_artifact_ready: ArtifactReadyCallback | None = None,
        notify_requester: RequesterNotifyCallback | None = None,
    ) -> None:
        self._repository = repository
        self._notifier = notifier
        self._client_factory = client_factory or (
            lambda url, token: HttpMusicProviderClient(base_url=url, api_token=token)
        )
        self._clients: dict[str, tuple[str, str | None, MusicProviderPort]] = {}
        self._staging_dir = staging_dir
        self._auto_provision_uploader_requests = auto_provision_uploader_requests
        self._download_timeout_seconds = download_timeout_seconds
        self._on_artifact_ready = on_artifact_ready
        self._notify_requester = notify_requester
        self._background_tasks: set[asyncio.Task[None]] = set()

    def get_provider_client(self, server: ProviderServerRecord) -> MusicProviderPort:
        """Return or instantiate a cached MusicProviderPort client for a server record."""
        cached = self._clients.get(server.id)
        if cached is not None and cached[0] == server.base_url and cached[1] == server.api_token_encrypted:
            return cached[2]
        client = self._client_factory(server.base_url, server.api_token_encrypted)
        self._clients[server.id] = (server.base_url, server.api_token_encrypted, client)
        return client

    async def handshake_and_update(
        self,
        server: ProviderServerRecord,
        verify: bool = False,
    ) -> ProviderServerRecord:
        """Execute handshake and health check, updating persisted record with live capabilities."""
        client = self.get_provider_client(server)
        health = (
            await client.check_health(verify=verify)
            if verify and isinstance(client, HttpMusicProviderClient)
            else await client.check_health()
        )
        caps: ProviderCapabilities | None = None
        if health.status in ("ok", "healthy", "ready", "degraded"):
            try:
                caps = await client.get_capabilities()
            except Exception as exc:
                logger.warning("Handshake capabilities failed for %s: %s", server.name, exc)

        updated = ProviderServerRecord(
            id=server.id,
            name=server.name,
            base_url=server.base_url,
            api_token_encrypted=server.api_token_encrypted,
            priority=server.priority,
            is_enabled=server.is_enabled,
            capabilities=caps or server.capabilities,
            last_health_status=health.status,
            last_health_checked_at=datetime.now(UTC),
            created_at=server.created_at,
        )
        await self._repository.upsert_provider_server(updated)
        return updated

    async def resolve_track_cascading(
        self,
        identifiers: dict[str, str],
        fallback: Mapping[str, object] | None = None,
    ) -> tuple[ProviderServerRecord, TrackResolutionResult, AvailableAudioQuality | None] | None:
        """Resolve a track across all active providers concurrently, ranking by audio quality."""
        servers = await self._repository.list_provider_servers(enabled_only=True)
        if not servers:
            logger.info("No enabled provider servers registered.")
            return None

        # Filter providers whose capabilities match any available identifier keys (if known)
        candidate_servers: list[ProviderServerRecord] = []
        for s in servers:
            if s.capabilities and s.capabilities.supported_sources:
                supported = set(s.capabilities.supported_sources)
                # Check if any identifier or generic match is supported
                has_common = any(k.removesuffix("_id") in supported for k in identifiers)
                if has_common or "musicbrainz" in supported or "isrc" in supported:
                    candidate_servers.append(s)
            else:
                candidate_servers.append(s)

        if not candidate_servers:
            candidate_servers = servers

        async def _query_server(
            srv: ProviderServerRecord,
        ) -> tuple[ProviderServerRecord, TrackResolutionResult] | None:
            try:
                client = self.get_provider_client(srv)
                fallback_dict = dict(fallback) if fallback is not None else None
                res = await client.resolve_track(identifiers, fallback_dict)
                if res.matched and res.provider_track_id:
                    return srv, res
            except Exception as exc:
                logger.debug("Provider %s failed to resolve track: %s", srv.name, exc)
            return None

        tasks = [_query_server(s) for s in candidate_servers]
        raw_results = await asyncio.gather(*tasks, return_exceptions=False)
        valid = [r for r in raw_results if r is not None]
        if not valid:
            return None

        # Rank valid providers by highest available audio quality
        scored_candidates: list[
            tuple[
                ProviderServerRecord,
                TrackResolutionResult,
                AvailableAudioQuality | None,
                tuple[int, int, int, int],
            ]
        ] = []
        for srv, res in valid:
            best_quality: AvailableAudioQuality | None = None
            best_score = (0, 0, 0, 0)
            if res.available_qualities:
                sorted_qualities = sorted(res.available_qualities, key=rank_quality, reverse=True)
                best_quality = sorted_qualities[0]
                best_score = rank_quality(best_quality)
            scored_candidates.append((srv, res, best_quality, best_score))

        # Sort: highest quality score first, then server priority ascending (lower number = higher priority)
        scored_candidates.sort(key=lambda item: (item[3], -item[0].priority), reverse=True)
        best_srv, best_res, best_q, _ = scored_candidates[0]
        return best_srv, best_res, best_q

    async def download_file_with_checksum(
        self,
        download_url: str,
        target_path: Path,
        expected_sha256: str | None = None,
        timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        headers: dict[str, str] | None = None,
    ) -> Path:
        """Stream download file from URL to target_path and verify SHA-256."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()

        async with (
            httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client,
            client.stream("GET", download_url, headers=headers) as resp,
        ):
            resp.raise_for_status()
            with target_path.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
                    sha.update(chunk)

        computed = sha.hexdigest().lower()
        if expected_sha256 and expected_sha256.strip():
            expected = expected_sha256.strip().lower()
            if computed != expected:
                target_path.unlink(missing_ok=True)
                raise ProvisioningFailedError(
                    f"Downloaded file checksum mismatch: expected {expected}, got {computed}"
                )

        return target_path

    async def poll_provision_completion(
        self,
        client: MusicProviderPort,
        job_id: str,
        timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        poll_interval: float = 2.0,
    ) -> ProvisionJobStatus:
        """Poll job status until completed or timed out."""
        elapsed = 0.0
        while elapsed < timeout_seconds:
            status = await client.get_job_status(job_id)
            if status.status in ("completed", "ready"):
                return status
            if status.status == "failed":
                raise ProvisioningFailedError(f"Provider job failed: {status.error or 'unknown error'}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise ProvisioningFailedError(f"Provider job {job_id} timed out after {timeout_seconds} seconds")

    async def submit_song_request(
        self,
        user: UserAccount,
        request_type: SongRequestType,
        track_title: str,
        artist_name: str,
        album_name: str = "",
        identifiers: dict[str, str] | None = None,
        target_quality: str | None = None,
    ) -> tuple[SongRequestRecord, bool]:
        """Submit a song/album request.

        Returns `(record, is_auto_approved)`.
        - If user has `SUBMIT_INGESTION`: auto-approved (`APPROVED`), returns True.
        - If user only has `REQUEST_SONG`: placed in `PENDING_APPROVAL`, returns False.
        """
        require_permission(user, Permission.REQUEST_SONG)

        is_uploader_or_above = user.role.has_at_least(UserRole.UPLOADER)
        initial_status = (
            SongRequestStatus.APPROVED if is_uploader_or_above else SongRequestStatus.PENDING_APPROVAL
        )

        record = SongRequestRecord(
            id=0,
            telegram_id=user.telegram_id,
            request_type=request_type,
            track_title=track_title.strip(),
            artist_name=artist_name.strip(),
            album_name=album_name.strip(),
            identifiers=identifiers or {},
            target_quality=target_quality,
            status=initial_status,
            reviewed_by=user.telegram_id if is_uploader_or_above else None,
            reviewed_at=datetime.now(UTC) if is_uploader_or_above else None,
        )
        saved = await self._repository.create_song_request(record)

        if not is_uploader_or_above and self._notifier is not None:
            try:
                await self._notifier.send_song_request_card(saved, user)
            except Exception as exc:
                logger.error("Failed to post song request card to admin channel: %s", exc)
        elif (
            is_uploader_or_above
            and self._auto_provision_uploader_requests
            and self._on_artifact_ready is not None
        ):
            self.schedule_provision_request(saved.id)

        return saved, is_uploader_or_above

    async def resolve_song_request_by_moderator(
        self,
        request_id: int,
        reviewer: UserAccount,
        approved: bool,
        rejection_reason: str = "",
    ) -> SongRequestRecord | None:
        """Approve or reject a pending song request by a moderator/admin."""
        require_permission(reviewer, Permission.APPROVE_SONG_REQUEST)
        new_status = SongRequestStatus.APPROVED if approved else SongRequestStatus.REJECTED
        updated = await self._repository.resolve_song_request(
            request_id=request_id,
            status=new_status,
            reviewed_by=reviewer.telegram_id,
            rejection_reason=rejection_reason,
        )
        if updated and self._notifier is not None:
            try:
                await self._notifier.send_song_request_resolved_audit(updated, reviewer, new_status)
            except Exception as exc:
                logger.error("Failed to post song request resolution audit: %s", exc)
        if updated and approved and self._on_artifact_ready is not None:
            self.schedule_provision_request(updated.id)
        return updated

    def schedule_provision_request(self, request_id: int) -> None:
        """Launch asynchronous background provisioning for an approved song request."""
        task = asyncio.create_task(self._safe_provision_request(request_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _safe_provision_request(self, request_id: int) -> None:
        """Wrapper executing provisioning while logging unhandled failures."""
        try:
            await self.execute_provision_request(request_id)
        except Exception as exc:
            logger.error("Unhandled error provisioning song request #%d: %s", request_id, exc)

    async def execute_provision_request(self, request_id: int) -> SongRequestRecord | None:
        """Resolve, provision, download, verify, and enqueue an approved song request."""
        record = await self._repository.get_song_request(request_id)
        if record is None:
            return None

        fallback = {
            "title": record.track_title,
            "artist": record.artist_name,
            "album": record.album_name,
        }
        try:
            resolved = await self.resolve_track_cascading(
                identifiers=record.identifiers,
                fallback=fallback,
            )
            if resolved is None:
                msg = "No configured music provider matched this request."
                failed = await self._repository.update_song_request_job_and_track(
                    request_id,
                    status=SongRequestStatus.FAILED,
                    error_message=msg,
                )
                await self._notify_user_safely(
                    record.telegram_id,
                    f"❌ Provisioning failed for <b>{record.track_title}</b>: {msg}",
                )
                return failed

            server, track_res, best_quality = resolved
            if not track_res.provider_track_id:
                raise ProvisioningFailedError("Provider resolved track without a provider_track_id.")
            client = self.get_provider_client(server)
            chosen_quality = record.target_quality or (best_quality.quality_id if best_quality else None)

            await self._repository.update_song_request_job_and_track(
                request_id,
                status=SongRequestStatus.PROVISIONING,
                provider_id=server.id,
            )

            prov_resp = await client.request_provision(
                provider_track_id=track_res.provider_track_id,
                target_quality=chosen_quality,
            )

            download_url = prov_resp.download_url
            expected_sha = prov_resp.checksum_sha256
            if not download_url and prov_resp.job_id:
                job_status = await self.poll_provision_completion(
                    client,
                    prov_resp.job_id,
                    timeout_seconds=self._download_timeout_seconds,
                )
                download_url = job_status.download_url
                expected_sha = expected_sha or job_status.checksum_sha256

            if not download_url:
                raise ProvisioningFailedError("Provider completed without supplying a download URL.")

            if isinstance(client, HttpMusicProviderClient):
                full_url = client.resolve_artifact_url(download_url)
                auth_headers = client.get_auth_headers()
            else:
                full_url = (
                    download_url
                    if download_url.startswith(("http://", "https://"))
                    else urljoin(f"{server.base_url.rstrip('/')}/", download_url.lstrip("/"))
                )
                auth_headers = (
                    {"Authorization": f"Bearer {server.api_token_encrypted}"}
                    if server.api_token_encrypted
                    else {}
                )

            parsed_path = urlparse(full_url).path
            ext = Path(parsed_path).suffix or ".flac"
            safe_stem = f"req_{record.id}_{record.artist_name}_{record.track_title}"
            cleaned_stem = "".join(c if c.isalnum() or c in " -_()" else "_" for c in safe_stem).strip()[:80]
            base_dir = self._staging_dir or (Path("storage") / "scratch" / "provider_staging")
            target_path = base_dir / f"{cleaned_stem}{ext}"

            downloaded = await self.download_file_with_checksum(
                download_url=full_url,
                target_path=target_path,
                expected_sha256=expected_sha,
                timeout_seconds=self._download_timeout_seconds,
                headers=auth_headers,
            )

            job_id: str | None = None
            if self._on_artifact_ready is not None:
                job_id = await self._on_artifact_ready(downloaded, record, server)

            updated = await self._repository.update_song_request_job_and_track(
                request_id,
                job_id=job_id,
                status=SongRequestStatus.COMPLETED,
                provider_id=server.id,
            )
            await self._notify_user_safely(
                record.telegram_id,
                f"✅ <b>Provider Acquisition Ready</b>\n"
                f"Downloaded <b>{record.track_title}</b> — <code>{record.artist_name}</code> "
                f"via <i>{server.name}</i> and queued for library ingestion.",
            )
            return updated
        except Exception as exc:
            logger.warning("Provisioning failed for request #%d: %s", request_id, exc)
            failed = await self._repository.update_song_request_job_and_track(
                request_id,
                status=SongRequestStatus.FAILED,
                error_message=str(exc)[:300],
            )
            await self._notify_user_safely(
                record.telegram_id,
                f"❌ <b>Provisioning Failed (#{request_id})</b>\n"
                f"Track: <b>{record.track_title}</b>\n"
                f"Reason: <code>{str(exc)[:180]}</code>",
            )
            return failed

    async def _notify_user_safely(self, telegram_id: int, message: str) -> None:
        """Dispatch user status notifications cleanly without propagating errors."""
        if self._notify_requester is None or telegram_id <= 0:
            return
        try:
            await self._notify_requester(telegram_id, message)
        except Exception as exc:
            logger.debug("Failed to notify requester %d: %s", telegram_id, exc)
