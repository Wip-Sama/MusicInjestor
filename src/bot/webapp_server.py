import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import mutagen
from aiohttp import BodyPartReader, ClientSession, WSMsgType, web
from mutagen.mp4 import MP4Cover

from src.bot.auth import TelegramInitDataValidationError, validate_telegram_init_data
from src.bot.oidc_server import OidcCallbackCoordinator
from src.core.bulk_research import (
    BulkResearchProgress,
    BulkResearchService,
    get_active_bulk_research,
)
from src.core.exceptions import OidcAuthenticationError, PermissionDeniedError
from src.core.extractor import SUPPORTED_ARCHIVE_EXTENSIONS, SUPPORTED_AUDIO_EXTENSIONS
from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import MusicBrainzMetadataService, parse_manual_search_query
from src.core.models import (
    AccessRequestRecord,
    AccessRequestStatus,
    AuditLogEntry,
    IngestionJob,
    JobState,
    LyricsData,
    Permission,
    TrackMetadata,
    UserAccount,
    UserPreferences,
    UserRole,
)
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.ports import GovernanceRepositoryPort
from src.core.queue import IngestionQueueWorker
from src.core.rbac import (
    ROLE_PERMISSIONS,
    GovernanceService,
    has_permission,
    require_permission,
)
from src.core.transcoder import (
    async_transcode_audio,
    default_transcode_cache,
    is_ffmpeg_available,
)

logger = logging.getLogger(__name__)

_LRC_TIMESTAMP_RE: re.Pattern[str] = re.compile(r"\[(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")


class QueueEventBus:
    """In-memory pub-sub event bus broadcasting live ingestion queue updates to SSE clients."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Add an event subscriber queue."""
        self._subscribers.add(queue)

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an event subscriber queue."""
        self._subscribers.discard(queue)

    async def publish(self, event: dict[str, Any]) -> None:
        """Broadcast an event payload to all active subscriber queues."""
        for sub in list(self._subscribers):
            with contextlib.suppress(Exception):
                sub.put_nowait(event)


def serialize_bulk_progress(progress: BulkResearchProgress | None) -> dict[str, Any] | None:
    """Serialize an active BulkResearchProgress snapshot into a JSON-safe dictionary for Mini App clients."""
    if progress is None:
        return None
    return {
        "total_tracks": progress.total_tracks,
        "current_index": progress.current_index,
        "current_track": progress.current_track,
        "status": progress.status,
        "actor_id": progress.actor_id,
        "actor_name": progress.actor_name,
        "started_at": progress.started_at,
        "applied_count": progress.applied_count,
        "needs_review_count": progress.needs_review_count,
        "failed_count": progress.failed_count,
        "skipped_count": progress.skipped_count,
        "percent": progress.percent,
    }


def serialize_job(job: IngestionJob, queue_position: int = 0) -> dict[str, Any]:
    """Serialize an IngestionJob domain model into a JSON-safe dictionary for Mini App clients."""
    selected = None
    if job.selected_metadata is not None:
        selected = {
            "title": job.selected_metadata.title,
            "artist": job.selected_metadata.artist,
            "album": job.selected_metadata.album,
            "year": job.selected_metadata.year,
            "track_number": job.selected_metadata.track_number,
            "disc_number": job.selected_metadata.disc_number,
            "genre": job.selected_metadata.genre,
            "duration_seconds": job.selected_metadata.duration_seconds,
            "recording_mbid": job.selected_metadata.recording_mbid,
            "release_mbid": job.selected_metadata.release_mbid,
        }

    candidates = [
        {
            "index": idx,
            "confidence": cand.confidence,
            "release_type": cand.release_type,
            "disambiguation": cand.disambiguation,
            "title": cand.metadata.title,
            "artist": cand.metadata.artist,
            "album": cand.metadata.album,
            "year": cand.metadata.year,
            "track_number": cand.metadata.track_number,
            "release_mbid": cand.metadata.release_mbid,
            "recording_mbid": cand.metadata.recording_mbid,
        }
        for idx, cand in enumerate(job.candidates)
    ]

    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "source_type": job.source_type.value,
        "original_filename": job.original_filename or job.source_path.name,
        "file_size_bytes": job.file_size_bytes,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_message": job.error_message,
        "confidence_score": job.confidence_score,
        "matched_release_mbid": job.matched_release_mbid,
        "final_path": str(job.final_path) if job.final_path else None,
        "user_id": job.user_id,
        "queue_position": queue_position,
        "total_tracks": job.total_tracks,
        "processed_tracks": job.processed_tracks,
        "lyrics_matched_count": job.lyrics_matched_count,
        "selected_metadata": selected,
        "candidates": candidates,
    }


DEFAULT_COVER_SVG: str = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="200" height="200">'
    '<rect width="200" height="200" fill="#2c2c2e"/>'
    '<circle cx="100" cy="100" r="45" fill="#3a3a3c"/>'
    '<path d="M92 75 v42 a14 14 0 1 0 10 13.4 V90 l25 -6 v25 a14 14 0 1 0 10 13.4 V75 Z" fill="#0a84ff"/>'
    "</svg>"
)


_COVER_CACHE: dict[str, tuple[bytes, str]] = {}
_COVER_CACHE_MAX_ENTRIES = 256


def extract_cover_bytes(audio_path: Path) -> tuple[bytes, str] | None:
    """Extract embedded cover image bytes and mime type from audio file if available.

    Uses an in-RAM cache to avoid repeated disk reads and mutagen decoding. When embedded
    artwork is discovered inside FLAC/MP3 files, it also opportunistically writes a
    sidecar `cover.jpg` next to the audio file so subsequent reads bypass decoding entirely.
    """
    if not audio_path.is_file():
        return None

    try:
        mtime = audio_path.stat().st_mtime_ns
    except OSError:
        mtime = 0

    cache_key = f"{audio_path.resolve()}:{mtime}"
    if cache_key in _COVER_CACHE:
        return _COVER_CACHE[cache_key]

    cover_file = audio_path.parent / "cover.jpg"
    if cover_file.is_file():
        try:
            data = cover_file.read_bytes(), "image/jpeg"
            if len(_COVER_CACHE) >= _COVER_CACHE_MAX_ENTRIES:
                _COVER_CACHE.pop(next(iter(_COVER_CACHE)))
            _COVER_CACHE[cache_key] = data
            return data
        except Exception:
            pass

    png_cover = audio_path.parent / "cover.png"
    if png_cover.is_file():
        try:
            data = png_cover.read_bytes(), "image/png"
            if len(_COVER_CACHE) >= _COVER_CACHE_MAX_ENTRIES:
                _COVER_CACHE.pop(next(iter(_COVER_CACHE)))
            _COVER_CACHE[cache_key] = data
            return data
        except Exception:
            pass

    try:
        f = mutagen.File(audio_path)
        if f is None:
            return None

        result: tuple[bytes, str] | None = None

        # FLAC pictures
        pictures = getattr(f, "pictures", None)
        if pictures:
            mime = (getattr(pictures[0], "mime", None) or "").strip() or "image/jpeg"
            result = bytes(pictures[0].data), mime

        # MP3 ID3 tags
        if result is None:
            tags = getattr(f, "tags", None)
            if tags:
                for tag in tags.values():
                    if getattr(tag, "FrameID", None) == "APIC":
                        result = bytes(tag.data), getattr(tag, "mime", "image/jpeg")
                        break

        # MP4 covr atoms
        if result is None and hasattr(f, "tags") and f.tags:
            covr = f.tags.get("covr")
            if covr and len(covr) > 0:
                img_data = bytes(covr[0])
                fmt = getattr(covr[0], "imageformat", None)
                mime = "image/png" if fmt == MP4Cover.FORMAT_PNG else "image/jpeg"
                result = img_data, mime

        if result is not None:
            # Cache in RAM
            if len(_COVER_CACHE) >= _COVER_CACHE_MAX_ENTRIES:
                _COVER_CACHE.pop(next(iter(_COVER_CACHE)))
            _COVER_CACHE[cache_key] = result

            # Write-through sidecar cache to speed up all future accesses
            try:
                target_ext = ".png" if result[1] == "image/png" else ".jpg"
                target_sidecar = audio_path.parent / f"cover{target_ext}"
                if not target_sidecar.exists():
                    target_sidecar.write_bytes(result[0])
            except Exception:
                pass

            return result
    except Exception:
        pass

    return None


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
    catalog_repository: GovernanceRepositoryPort | None = None,
    organizer: LibraryOrganizer | None = None,
    metadata_service: MusicBrainzMetadataService | None = None,
    lyrics_client: LrclibLyricsClient | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    pipeline: IngestionPipeline | None = None,
    scratch_dir: Path | None = None,
    event_bus: QueueEventBus | None = None,
) -> web.Application:
    """Construct an `aiohttp.web.Application` exposing TMA API, OIDC callbacks, and static assets."""
    app = web.Application()
    bus = event_bus or QueueEventBus()
    app["event_bus"] = bus

    if queue_worker is not None:

        async def _on_worker_progress(job: IngestionJob, result: Any = None) -> None:
            event_type = "stage_changed"
            if job.state == JobState.AWAITING_CONFIRMATION:
                event_type = "confirmation_required"
            elif job.state == JobState.COMPLETED:
                event_type = "job_completed"
            elif job.state == JobState.FAILED:
                event_type = "job_failed"
            elif job.state in {JobState.PAUSED, JobState.CANCELLED}:
                event_type = "job_state_changed"

            await bus.publish(
                {
                    "type": event_type,
                    "job_id": job.job_id,
                    "user_id": job.user_id,
                    "state": job.state.value,
                    "job": serialize_job(job),
                }
            )

        queue_worker.add_progress_callback(_on_worker_progress)

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
            "authentik_linked": user.is_oidc_linked,
            "authentik_username": user.authentik_username,
            "oidc_role_override": user.oidc_role_override,
            "permissions": permissions,
            "is_owner": is_owner,
            "start_param": validated_data.get("start_param", ""),
        }
        return web.json_response(response_payload)

    app.router.add_get("/api/v1/app/auth/me", handle_get_me)

    async def handle_post_auth_oidc_disconnect(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if governance_service is None:
            return web.json_response({"error": "Governance service unavailable."}, status=503)
        if not user.is_oidc_linked:
            return web.json_response({"error": "Account is not linked to Authentik OIDC."}, status=400)
        updated = await governance_service.unlink_user_oidc(
            actor=user,
            target_telegram_id=user.telegram_id,
        )
        return web.json_response(
            {
                "success": True,
                "message": "Authentik OIDC account unlinked successfully.",
                "telegram_id": updated.telegram_id,
                "authentik_linked": updated.is_oidc_linked,
            }
        )

    app.router.add_post("/api/v1/app/auth/oidc/disconnect", handle_post_auth_oidc_disconnect)

    # 2.05 User Settings & Preferences
    async def handle_get_user_settings(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if governance_service is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Governance service unavailable."}),
                content_type="application/json",
            )
        prefs = await governance_service._repo.get_user_preferences(user.telegram_id)
        return web.json_response(
            {
                "telegram_id": prefs.telegram_id,
                "stream_format": prefs.stream_format,
                "stream_bitrate": prefs.stream_bitrate,
                "auto_lyrics_search": prefs.auto_lyrics_search,
                "theme": prefs.theme,
            }
        )

    async def handle_update_user_settings(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if governance_service is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Governance service unavailable."}),
                content_type="application/json",
            )
        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Invalid JSON body."}),
                content_type="application/json",
            ) from exc

        current = await governance_service._repo.get_user_preferences(user.telegram_id)
        new_format = str(body.get("stream_format") or current.stream_format).strip().lower()
        if new_format not in {"auto", "original", "opus", "mp3"}:
            new_format = current.stream_format

        new_bitrate = current.stream_bitrate
        with contextlib.suppress(Exception):
            raw_br = int(body.get("stream_bitrate", current.stream_bitrate))
            if raw_br in {64, 96, 128, 160, 192, 256, 320}:
                new_bitrate = raw_br

        new_auto_lyrics = (
            bool(body["auto_lyrics_search"]) if "auto_lyrics_search" in body else current.auto_lyrics_search
        )
        new_theme = str(body.get("theme") or current.theme).strip().lower()
        if new_theme not in {"system", "dark", "light"}:
            new_theme = current.theme

        updated = UserPreferences(
            telegram_id=user.telegram_id,
            stream_format=new_format,
            stream_bitrate=new_bitrate,
            auto_lyrics_search=new_auto_lyrics,
            theme=new_theme,
        )
        saved = await governance_service._repo.update_user_preferences(updated)
        return web.json_response(
            {
                "telegram_id": saved.telegram_id,
                "stream_format": saved.stream_format,
                "stream_bitrate": saved.stream_bitrate,
                "auto_lyrics_search": saved.auto_lyrics_search,
                "theme": saved.theme,
            }
        )

    app.router.add_get("/api/v1/app/user/settings", handle_get_user_settings)
    app.router.add_post("/api/v1/app/user/settings", handle_update_user_settings)

    # 2.1 Catalog track list with search, sorting & pagination
    async def handle_get_tracks(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.VIEW_CATALOG):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Catalog access restricted. Permission VIEW_CATALOG is required."}),
                content_type="application/json",
            )
        if catalog_repository is None:
            return web.json_response(
                {
                    "items": [],
                    "total": 0,
                    "page": 1,
                    "per_page": 20,
                    "total_pages": 0,
                }
            )

        query = request.query.get("query", "").strip()
        sort_by = request.query.get("sort_by", "recent").strip().lower()
        if sort_by not in {"recent", "artist", "title", "album", "year"}:
            sort_by = "recent"

        try:
            page = max(1, int(request.query.get("page", "1")))
        except ValueError:
            page = 1

        try:
            per_page = min(100, max(1, int(request.query.get("per_page", "20"))))
        except ValueError:
            per_page = 20

        offset = (page - 1) * per_page

        if query:
            records = await catalog_repository.search_library_tracks(
                query=query,
                limit=per_page,
                offset=offset,
                sort_by=sort_by,
            )
            total = await catalog_repository.count_library_tracks(query=query)
        else:
            records = await catalog_repository.list_library_tracks(
                limit=per_page,
                offset=offset,
                sort_by=sort_by,
            )
            total = await catalog_repository.count_library_tracks(query="")

        total_pages = (total + per_page - 1) // per_page if total > 0 else 0

        items = []
        for r in records:
            ext = r.storage_path.suffix.lstrip(".").lower()
            items.append(
                {
                    "id": r.id,
                    "title": r.title,
                    "artist": r.artist,
                    "album": r.album,
                    "year": r.year,
                    "track_number": r.track_number,
                    "duration_seconds": r.duration_seconds,
                    "has_synced_lyrics": r.has_synced_lyrics,
                    "format": ext or "audio",
                    "audio_url": f"/api/v1/app/tracks/{r.id}/audio",
                    "cover_url": f"/api/v1/app/tracks/{r.id}/cover",
                }
            )

        return web.json_response(
            {
                "items": items,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
            }
        )

    app.router.add_get("/api/v1/app/tracks", handle_get_tracks)

    # 2.2 Single track details
    async def handle_get_track(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.VIEW_CATALOG):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Catalog access restricted. Permission VIEW_CATALOG is required."}),
                content_type="application/json",
            )
        if catalog_repository is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Catalog repository unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Track not found."}),
                content_type="application/json",
            )

        ext = record.storage_path.suffix.lstrip(".").lower()
        file_size = record.storage_path.stat().st_size if record.storage_path.is_file() else 0
        bitrate_kbps = None
        if file_size > 0 and record.duration_seconds > 0:
            bitrate_kbps = int((file_size * 8) / (record.duration_seconds * 1000))

        return web.json_response(
            {
                "id": record.id,
                "title": record.title,
                "artist": record.artist,
                "album": record.album,
                "year": record.year,
                "track_number": record.track_number,
                "duration_seconds": record.duration_seconds,
                "has_synced_lyrics": record.has_synced_lyrics,
                "format": ext or "audio",
                "file_size": file_size,
                "bitrate_kbps": bitrate_kbps,
                "audio_url": f"/api/v1/app/tracks/{record.id}/audio",
                "cover_url": f"/api/v1/app/tracks/{record.id}/cover",
            }
        )

    app.router.add_get("/api/v1/app/tracks/{track_id}", handle_get_track)

    # 2.3 Album cover art streaming
    async def handle_get_cover(request: web.Request) -> web.StreamResponse:
        raw_token = extract_raw_init_data_from_request(request)
        if raw_token:
            with contextlib.suppress(Exception):
                user, _ = await authenticate_tma_request(
                    request=request,
                    bot_token=bot_token,
                    governance_service=governance_service,
                )
                if not has_permission(user, Permission.VIEW_CATALOG):
                    return web.Response(text=DEFAULT_COVER_SVG, content_type="image/svg+xml")

        if catalog_repository is None:
            return web.Response(text=DEFAULT_COVER_SVG, content_type="image/svg+xml")

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            return web.Response(text=DEFAULT_COVER_SVG, content_type="image/svg+xml")

        cover_art = extract_cover_bytes(record.storage_path)
        if cover_art is not None:
            img_bytes, mime_type = cover_art
            etag = f'"{hashlib.md5(img_bytes[:512] + str(len(img_bytes)).encode()).hexdigest()}"'
            if request.headers.get("If-None-Match") == etag:
                return web.Response(
                    status=304,
                    headers={
                        "ETag": etag,
                        "Cache-Control": "public, max-age=604800, immutable",
                        "Access-Control-Allow-Origin": "*",
                    },
                )
            return web.Response(
                body=img_bytes,
                content_type=mime_type,
                headers={
                    "ETag": etag,
                    "Cache-Control": "public, max-age=604800, immutable",
                    "Access-Control-Allow-Origin": "*",
                },
            )

        return web.Response(text=DEFAULT_COVER_SVG, content_type="image/svg+xml")

    app.router.add_get("/api/v1/app/tracks/{track_id}/cover", handle_get_cover)

    # 2.4 Audio streaming with HTTP 206 Partial Content range support & on-the-fly RAM transcoding
    async def handle_get_audio(request: web.Request) -> web.StreamResponse:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.VIEW_CATALOG):
            raise web.HTTPForbidden(
                text=json.dumps(
                    {"error": "Audio streaming restricted. Permission VIEW_CATALOG is required."}
                ),
                content_type="application/json",
            )

        if catalog_repository is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Catalog repository unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None or not record.storage_path.is_file():
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Audio track file not found on disk."}),
                content_type="application/json",
            )

        ext = record.storage_path.suffix.lower()
        req_format = request.query.get("format", "auto").strip().lower()
        bitrate_kbps = 128
        with contextlib.suppress(Exception):
            bitrate_kbps = int(request.query.get("bitrate", "128"))

        if req_format == "auto" and governance_service and user:
            with contextlib.suppress(Exception):
                user_prefs = await governance_service._repo.get_user_preferences(user.telegram_id)
                if user_prefs.stream_format != "auto":
                    req_format = user_prefs.stream_format
                    bitrate_kbps = user_prefs.stream_bitrate

        want_transcode = False
        if req_format not in {"original", "direct", "raw"}:
            want_transcode = req_format in {"opus", "mp3"} or (req_format == "auto" and ext == ".flac")

        if want_transcode and is_ffmpeg_available():
            target_fmt = "mp3" if req_format == "mp3" else "opus"
            target_mime = "audio/mpeg" if target_fmt == "mp3" else "audio/ogg; codecs=opus"
            transcoded_data = await default_transcode_cache.get(record.storage_path, target_fmt, bitrate_kbps)
            if transcoded_data is None:
                try:
                    transcoded_data = await async_transcode_audio(
                        record.storage_path,
                        target_format=target_fmt,
                        bitrate_kbps=bitrate_kbps,
                    )
                    await default_transcode_cache.put(
                        record.storage_path, target_fmt, bitrate_kbps, transcoded_data
                    )
                except Exception as exc:
                    logger.warning(
                        "On-the-fly transcoding failed for %s (%s): %s; streaming original",
                        record.id,
                        target_fmt,
                        exc,
                    )
                    transcoded_data = None

            if transcoded_data is not None:
                total_len = len(transcoded_data)
                range_header = request.headers.get("Range", "").strip()
                if range_header.startswith("bytes="):
                    try:
                        range_spec = range_header[6:].split("-", 1)
                        start = int(range_spec[0]) if range_spec[0] else 0
                        end = int(range_spec[1]) if len(range_spec) > 1 and range_spec[1] else total_len - 1
                        start = max(0, min(start, total_len - 1))
                        end = max(start, min(end, total_len - 1))
                        slice_bytes = transcoded_data[start : end + 1]
                        return web.Response(
                            status=206,
                            body=slice_bytes,
                            headers={
                                "Content-Type": target_mime,
                                "Content-Range": f"bytes {start}-{end}/{total_len}",
                                "Content-Length": str(len(slice_bytes)),
                                "Accept-Ranges": "bytes",
                                "Cache-Control": "public, max-age=86400",
                                "Access-Control-Allow-Origin": "*",
                            },
                        )
                    except Exception:
                        pass

                return web.Response(
                    status=200,
                    body=transcoded_data,
                    headers={
                        "Content-Type": target_mime,
                        "Content-Length": str(total_len),
                        "Accept-Ranges": "bytes",
                        "Cache-Control": "public, max-age=86400",
                        "Access-Control-Allow-Origin": "*",
                    },
                )

        content_type = "audio/mpeg"
        if ext == ".flac":
            content_type = "audio/flac"
        elif ext in {".m4a", ".mp4"}:
            content_type = "audio/mp4"
        elif ext == ".ogg":
            content_type = "audio/ogg"
        elif ext == ".opus":
            content_type = "audio/opus"

        resp = web.FileResponse(
            record.storage_path,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
        resp.content_type = content_type
        return resp

    app.router.add_get("/api/v1/app/tracks/{track_id}/audio", handle_get_audio)

    # 2.5 Track lyrics endpoint (raw and synced LRC)
    async def handle_get_lyrics(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.VIEW_CATALOG):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Catalog access restricted. Permission VIEW_CATALOG is required."}),
                content_type="application/json",
            )
        if catalog_repository is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Catalog repository unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Track not found."}),
                content_type="application/json",
            )

        synced_lyrics = None
        plain_lyrics = None
        has_synced = False

        if organizer is not None:
            lyrics_obj = await organizer.read_track_lyrics(record.storage_path)
            if lyrics_obj is not None:
                synced_lyrics = lyrics_obj.synced_lyrics
                plain_lyrics = lyrics_obj.plain_lyrics
                has_synced = lyrics_obj.has_synced

        return web.json_response(
            {
                "track_id": record.id,
                "has_synced": has_synced,
                "synced_lyrics": synced_lyrics,
                "plain_lyrics": plain_lyrics,
            }
        )

    app.router.add_get("/api/v1/app/tracks/{track_id}/lyrics", handle_get_lyrics)

    # 2.6 Propose song edit (dry-run preview calculating diff and target filesystem path)
    async def handle_propose_edit(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.REQUEST_TAG_CORRECTION):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Permission denied: UPLOADER role or above required."}),
                content_type="application/json",
            )
        if catalog_repository is None or organizer is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Catalog repository or library organizer unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Track not found."}),
                content_type="application/json",
            )

        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Invalid JSON body."}),
                content_type="application/json",
            ) from exc

        proposal_data = body.get("proposal", body) if isinstance(body.get("proposal"), dict) else body
        action_type = str(proposal_data.get("action_type") or "metadata").lower()

        new_title = str(proposal_data.get("title") or proposal_data.get("new_title") or record.title).strip()
        new_artist = str(
            proposal_data.get("artist") or proposal_data.get("new_artist") or record.artist
        ).strip()
        new_album = str(proposal_data.get("album") or proposal_data.get("new_album") or record.album).strip()
        if action_type in {"lyrics", "cover"}:
            new_year = record.year
        elif "year" in proposal_data or "new_year" in proposal_data:
            raw_year = proposal_data.get("year") if "year" in proposal_data else proposal_data.get("new_year")
            try:
                new_year = int(raw_year) if raw_year is not None and str(raw_year).strip() else None
            except ValueError, TypeError:
                new_year = record.year
        else:
            new_year = record.year
        raw_trk = (
            proposal_data.get("track_number")
            if "track_number" in proposal_data
            else proposal_data.get("new_track_number")
        )
        try:
            new_trk = (
                int(raw_trk) if raw_trk is not None and str(raw_trk).strip() else (record.track_number or 1)
            )
        except ValueError, TypeError:
            new_trk = record.track_number or 1

        new_lyrics = proposal_data.get("lyrics") or proposal_data.get("new_lyrics")
        new_cover_hex = proposal_data.get("cover_hex") or proposal_data.get("new_cover_hex")
        new_cover_mime = str(
            proposal_data.get("cover_mime") or proposal_data.get("new_cover_mime") or "image/jpeg"
        )
        new_rec_mbid = proposal_data.get("recording_mbid") or proposal_data.get("new_recording_mbid")
        new_rel_mbid = proposal_data.get("release_mbid") or proposal_data.get("new_release_mbid")

        target_meta = TrackMetadata(
            title=new_title,
            artist=new_artist,
            album=new_album,
            album_artist=new_artist,
            year=new_year,
            track_number=new_trk,
            duration_seconds=int(round(record.duration_seconds)),
            recording_mbid=str(new_rec_mbid) if new_rec_mbid else None,
            release_mbid=str(new_rel_mbid) if new_rel_mbid else None,
        )
        suffix = record.storage_path.suffix or ".flac"
        new_path = organizer.build_target_audio_path(target_meta, suffix)

        diff_list = [
            {
                "field": "title",
                "label": "Title",
                "old": record.title,
                "new": new_title,
                "changed": record.title != new_title,
            },
            {
                "field": "artist",
                "label": "Artist",
                "old": record.artist,
                "new": new_artist,
                "changed": record.artist != new_artist,
            },
            {
                "field": "album",
                "label": "Album",
                "old": record.album,
                "new": new_album,
                "changed": record.album != new_album,
            },
            {
                "field": "year",
                "label": "Year",
                "old": record.year,
                "new": new_year,
                "changed": record.year != new_year,
            },
            {
                "field": "track_number",
                "label": "Track #",
                "old": record.track_number or 1,
                "new": new_trk,
                "changed": (record.track_number or 1) != new_trk,
            },
        ]

        path_changed = (
            str(record.storage_path).replace("\\", "/").lower() != str(new_path).replace("\\", "/").lower()
        )
        has_cover_change = bool(new_cover_hex)
        has_lyrics_change = new_lyrics is not None

        return web.json_response(
            {
                "track_id": record.id,
                "action_type": action_type,
                "diff": diff_list,
                "old_path": str(record.storage_path),
                "new_path": str(new_path),
                "path_changed": path_changed,
                "has_lyrics_change": has_lyrics_change,
                "has_cover_change": has_cover_change,
                "proposal": {
                    "track_id": record.id,
                    "action_type": action_type,
                    "new_title": new_title,
                    "new_artist": new_artist,
                    "new_album": new_album,
                    "new_year": new_year,
                    "new_track_number": new_trk,
                    "new_lyrics": new_lyrics,
                    "new_cover_hex": new_cover_hex,
                    "new_cover_mime": new_cover_mime,
                    "new_recording_mbid": str(new_rec_mbid) if new_rec_mbid else None,
                    "new_release_mbid": str(new_rel_mbid) if new_rel_mbid else None,
                },
            }
        )

    app.router.add_post("/api/v1/app/tracks/{track_id}/propose-edit", handle_propose_edit)

    # 2.7 Apply song edit (retagging, relocating file, embedding lyrics/cover, and database commit)
    async def handle_apply_edit(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.REQUEST_TAG_CORRECTION):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Permission denied: UPLOADER role or above required."}),
                content_type="application/json",
            )
        if catalog_repository is None or organizer is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Catalog repository or library organizer unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Track not found."}),
                content_type="application/json",
            )

        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Invalid JSON body."}),
                content_type="application/json",
            ) from exc

        proposal_data = body.get("proposal", body) if isinstance(body.get("proposal"), dict) else body

        new_title = str(proposal_data.get("new_title") or proposal_data.get("title") or record.title).strip()
        new_artist = str(
            proposal_data.get("new_artist") or proposal_data.get("artist") or record.artist
        ).strip()
        new_album = str(proposal_data.get("new_album") or proposal_data.get("album") or record.album).strip()
        raw_year = proposal_data.get("new_year") if "new_year" in proposal_data else proposal_data.get("year")
        try:
            new_year = int(raw_year) if raw_year is not None and str(raw_year).strip() else None
        except ValueError, TypeError:
            new_year = record.year
        raw_trk = (
            proposal_data.get("new_track_number")
            if "new_track_number" in proposal_data
            else proposal_data.get("track_number")
        )
        try:
            new_trk = (
                int(raw_trk) if raw_trk is not None and str(raw_trk).strip() else (record.track_number or 1)
            )
        except ValueError, TypeError:
            new_trk = record.track_number or 1

        new_lyrics = proposal_data.get("new_lyrics") or proposal_data.get("lyrics")
        new_cover_hex = proposal_data.get("new_cover_hex") or proposal_data.get("cover_hex")
        new_cover_mime = str(
            proposal_data.get("new_cover_mime") or proposal_data.get("cover_mime") or "image/jpeg"
        )
        new_rec_mbid = proposal_data.get("new_recording_mbid") or proposal_data.get("recording_mbid")
        new_rel_mbid = proposal_data.get("new_release_mbid") or proposal_data.get("release_mbid")

        accepted_fields = body.get("accepted_fields")
        if accepted_fields is None and isinstance(proposal_data, dict):
            accepted_fields = proposal_data.get("accepted_fields")
        if isinstance(accepted_fields, list):
            accepted_set = set(accepted_fields)
            if "title" not in accepted_set:
                new_title = record.title
            if "artist" not in accepted_set:
                new_artist = record.artist
            if "album" not in accepted_set:
                new_album = record.album
            if "year" not in accepted_set:
                new_year = record.year
            if "track_number" not in accepted_set:
                new_trk = record.track_number or 1
            if "lyrics" not in accepted_set:
                new_lyrics = None
            if "cover" not in accepted_set:
                new_cover_hex = None

        lyrics_obj = None
        if isinstance(new_lyrics, str) and new_lyrics.strip():
            cleaned = new_lyrics.strip()
            is_synced = bool(_LRC_TIMESTAMP_RE.search(cleaned))
            lyrics_obj = LyricsData(
                synced_lyrics=cleaned if is_synced else None,
                plain_lyrics=None if is_synced else cleaned,
            )

        updated_meta = TrackMetadata(
            title=new_title,
            artist=new_artist,
            album=new_album,
            album_artist=new_artist,
            year=new_year,
            track_number=new_trk,
            duration_seconds=int(round(record.duration_seconds)),
            recording_mbid=str(new_rec_mbid) if new_rec_mbid else None,
            release_mbid=str(new_rel_mbid) if new_rel_mbid else None,
        )

        new_path = await organizer.relocate_and_retag_track(
            record.storage_path,
            updated_meta,
            lyrics_obj,
        )

        if isinstance(new_cover_hex, str) and new_cover_hex.strip():
            try:
                cover_bytes = bytes.fromhex(new_cover_hex.strip())
                await organizer.update_track_cover_art(new_path, cover_bytes, mime_type=new_cover_mime)
            except Exception as exc:
                logger.warning("Failed to apply cover art hex for %s: %s", record.id, exc)

        final_lyrics = await organizer.read_track_lyrics(new_path)
        has_synced_flag = bool(final_lyrics and (final_lyrics.has_synced or final_lyrics.has_any))

        updated_record = await catalog_repository.update_library_track_full(
            record.id,
            title=new_title,
            artist=new_artist,
            album=new_album,
            year=new_year,
            track_number=new_trk,
            storage_path=new_path,
            has_synced_lyrics=has_synced_flag,
            recording_mbid=updated_meta.recording_mbid,
            release_mbid=updated_meta.release_mbid,
            modified_by=user.telegram_id,
        )
        await catalog_repository.append_audit_log(
            AuditLogEntry(
                actor_id=user.telegram_id,
                action="SONG_UPDATED_MINIAPP",
                target_entity=record.id,
                details={
                    "action_type": str(proposal_data.get("action_type") or "studio"),
                    "title": new_title,
                    "artist": new_artist,
                    "album": new_album,
                    "year": new_year,
                    "storage_path": str(new_path),
                },
            )
        )
        effective = updated_record or record
        ext = effective.storage_path.suffix.lstrip(".").lower()
        return web.json_response(
            {
                "status": "applied",
                "track": {
                    "id": effective.id,
                    "title": effective.title,
                    "artist": effective.artist,
                    "album": effective.album,
                    "year": effective.year,
                    "track_number": effective.track_number,
                    "duration_seconds": effective.duration_seconds,
                    "has_synced_lyrics": effective.has_synced_lyrics,
                    "format": ext or "audio",
                    "audio_url": f"/api/v1/app/tracks/{effective.id}/audio",
                    "cover_url": f"/api/v1/app/tracks/{effective.id}/cover?t={int(time.time())}",
                },
            }
        )

    app.router.add_post("/api/v1/app/tracks/{track_id}/apply-edit", handle_apply_edit)

    # 2.8 Cover art upload / preview endpoint
    async def handle_post_cover(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.REQUEST_TAG_CORRECTION):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Permission denied: UPLOADER role or above required."}),
                content_type="application/json",
            )
        if catalog_repository is None or organizer is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Catalog repository or library organizer unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Track not found."}),
                content_type="application/json",
            )

        img_bytes: bytes = b""
        mime_type = "image/jpeg"
        should_apply = request.query.get("apply", "false").lower() in {"true", "1", "yes"}

        content_type = request.content_type.lower()
        if "multipart" in content_type:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if isinstance(part, BodyPartReader) and part.name in {"file", "image", "cover"}:
                    img_bytes = await part.read()
                    if part.headers.get("Content-Type"):
                        mime_type = part.headers["Content-Type"].split(";")[0].strip()
                    break
        else:
            try:
                body = await request.json()
            except Exception as exc:
                raise web.HTTPBadRequest(
                    text=json.dumps(
                        {"error": "Invalid payload format. Expected multipart/form-data or JSON."}
                    ),
                    content_type="application/json",
                ) from exc
            should_apply = should_apply or bool(body.get("apply", False))
            raw_hex = body.get("cover_hex")
            raw_b64 = body.get("image_base64") or body.get("image") or ""
            if isinstance(raw_hex, str) and raw_hex:
                img_bytes = bytes.fromhex(raw_hex)
            elif isinstance(raw_b64, str) and raw_b64.startswith("data:"):
                meta_part, data_part = raw_b64.split(",", 1)
                img_bytes = base64.b64decode(data_part)
                if ";base64" in meta_part:
                    mime_type = meta_part.split(";")[0].replace("data:", "").strip()
            elif isinstance(raw_b64, str) and raw_b64:
                img_bytes = base64.b64decode(raw_b64)
            if body.get("mime_type"):
                mime_type = str(body["mime_type"]).strip()

        if not img_bytes:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "No image data provided."}),
                content_type="application/json",
            )
        if len(img_bytes) > 10 * 1024 * 1024:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Image file too large (max 10MB)."}),
                content_type="application/json",
            )

        if should_apply:
            await organizer.update_track_cover_art(record.storage_path, img_bytes, mime_type=mime_type)
            await catalog_repository.append_audit_log(
                AuditLogEntry(
                    actor_id=user.telegram_id,
                    action="SONG_COVER_UPDATED_MINIAPP",
                    target_entity=record.id,
                    details={"storage_path": str(record.storage_path), "bytes": len(img_bytes)},
                )
            )
            return web.json_response(
                {
                    "status": "applied",
                    "cover_url": f"/api/v1/app/tracks/{record.id}/cover?t={int(time.time())}",
                }
            )

        return web.json_response(
            {
                "status": "preview",
                "cover_hex": img_bytes.hex(),
                "mime_type": mime_type,
                "size_bytes": len(img_bytes),
            }
        )

    app.router.add_post("/api/v1/app/tracks/{track_id}/cover", handle_post_cover)

    # 2.9 Synced lyrics update / preview endpoint
    async def handle_post_lyrics(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.REQUEST_TAG_CORRECTION):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Permission denied: UPLOADER role or above required."}),
                content_type="application/json",
            )
        if catalog_repository is None or organizer is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Catalog repository or library organizer unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Track not found."}),
                content_type="application/json",
            )

        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Invalid JSON body."}),
                content_type="application/json",
            ) from exc

        lyrics_text = str(body.get("lyrics", "")).strip()
        should_apply = bool(body.get("apply", False)) or request.query.get("apply", "false").lower() in {
            "true",
            "1",
        }
        is_synced = bool(_LRC_TIMESTAMP_RE.search(lyrics_text))

        if should_apply:
            await organizer.update_track_lyrics(record.storage_path, lyrics_text)
            await catalog_repository.update_library_track_full(
                record.id,
                has_synced_lyrics=is_synced,
                modified_by=user.telegram_id,
            )
            await catalog_repository.append_audit_log(
                AuditLogEntry(
                    actor_id=user.telegram_id,
                    action="SONG_LYRICS_UPDATED_MINIAPP",
                    target_entity=record.id,
                    details={"storage_path": str(record.storage_path), "is_synced": is_synced},
                )
            )
            return web.json_response(
                {
                    "status": "applied",
                    "has_synced": is_synced,
                    "lyrics": lyrics_text,
                }
            )

        old_lyrics = await organizer.read_track_lyrics(record.storage_path)
        old_text = (old_lyrics.synced_lyrics or old_lyrics.plain_lyrics) if old_lyrics else None
        return web.json_response(
            {
                "status": "preview",
                "old_has_synced": old_lyrics.has_synced if old_lyrics else False,
                "new_has_synced": is_synced,
                "old_lyrics": old_text,
                "new_lyrics": lyrics_text,
                "changed": old_text != lyrics_text,
            }
        )

    app.router.add_post("/api/v1/app/tracks/{track_id}/lyrics", handle_post_lyrics)

    # 2.10 Auto re-search & enrichment endpoint (MusicBrainz, Genius, Last.fm & LRCLIB)
    async def handle_auto_enrich(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.REQUEST_TAG_CORRECTION):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Permission denied: UPLOADER role or above required."}),
                content_type="application/json",
            )
        if catalog_repository is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Catalog repository unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Track not found."}),
                content_type="application/json",
            )

        query = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                query = str(body.get("query", "")).strip()
        except Exception:
            pass

        base_meta = TrackMetadata(
            title=record.title,
            artist=record.artist,
            album=record.album,
            year=record.year,
            track_number=record.track_number or 1,
            duration_seconds=int(round(record.duration_seconds)),
        )

        if metadata_service is None:
            return web.json_response(
                {
                    "candidates": [],
                    "best": {
                        "title": record.title,
                        "artist": record.artist,
                        "album": record.album,
                        "year": record.year,
                        "track_number": record.track_number or 1,
                    },
                    "fetched_lyrics": None,
                }
            )

        if query:
            seed_meta = parse_manual_search_query(query, base_meta)
        else:
            seed_meta = (
                await metadata_service.read_local_metadata(record.storage_path)
                if record.storage_path.is_file()
                else base_meta
            )

        candidates = await metadata_service.find_candidates(seed_meta)
        best = candidates[0].metadata if candidates else seed_meta

        lyrics_text = None
        if lyrics_client is not None:
            try:
                fetched_lrc = await lyrics_client.fetch_lyrics(best)
                if fetched_lrc is not None:
                    lyrics_text = fetched_lrc.synced_lyrics or fetched_lrc.plain_lyrics
            except Exception as exc:
                logger.debug("Failed to fetch lyrics during auto-enrich: %s", exc)

        candidate_items = []
        for c in candidates[:8]:
            pct = round(c.confidence * 100)
            candidate_items.append(
                {
                    "label": c.display_label,
                    "confidence_pct": pct,
                    "title": c.metadata.title,
                    "artist": c.metadata.artist,
                    "album": c.metadata.album,
                    "year": c.metadata.year,
                    "track_number": c.metadata.track_number,
                    "recording_mbid": c.metadata.recording_mbid,
                    "release_mbid": c.metadata.release_mbid,
                }
            )

        return web.json_response(
            {
                "candidates": candidate_items,
                "best": {
                    "title": best.title,
                    "artist": best.artist,
                    "album": best.album,
                    "year": best.year,
                    "track_number": best.track_number or record.track_number or 1,
                    "recording_mbid": best.recording_mbid,
                    "release_mbid": best.release_mbid,
                },
                "fetched_lyrics": lyrics_text,
            }
        )

    app.router.add_post("/api/v1/app/tracks/{track_id}/auto-enrich", handle_auto_enrich)

    # 2.11 Auto lyrics research endpoint (LRCLIB)
    async def handle_auto_lyrics(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.REQUEST_TAG_CORRECTION):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Permission denied: UPLOADER role or above required."}),
                content_type="application/json",
            )
        if catalog_repository is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Catalog repository unavailable."}),
                content_type="application/json",
            )
        if lyrics_client is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Lyrics client unavailable."}),
                content_type="application/json",
            )

        track_id = request.match_info.get("track_id", "").strip()
        record = await catalog_repository.get_library_track(track_id)
        if record is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "Track not found."}),
                content_type="application/json",
            )

        query = ""
        override_artist = ""
        override_title = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                query = str(body.get("query", "")).strip()
                override_artist = str(body.get("artist", "")).strip()
                override_title = str(body.get("title", "")).strip()
        except Exception:
            pass

        artist_to_search = override_artist or record.artist
        title_to_search = override_title or record.title

        if query:
            if " - " in query:
                parts = query.split(" - ", maxsplit=1)
                artist_to_search = parts[0].strip() or artist_to_search
                title_to_search = parts[1].strip() or title_to_search
            else:
                title_to_search = query

        search_meta = TrackMetadata(
            title=title_to_search,
            artist=artist_to_search,
            album=record.album,
            year=record.year,
            track_number=record.track_number or 1,
            duration_seconds=int(round(record.duration_seconds)),
        )

        try:
            fetched_lrc = await lyrics_client.fetch_lyrics(search_meta)
        except Exception as exc:
            logger.debug("Failed to auto-search lyrics for track %s: %s", record.id, exc)
            fetched_lrc = None

        if fetched_lrc is not None and (fetched_lrc.synced_lyrics or fetched_lrc.plain_lyrics):
            raw_text = fetched_lrc.synced_lyrics or fetched_lrc.plain_lyrics or ""
            is_synced = bool(fetched_lrc.synced_lyrics)
            lines = [line for line in raw_text.splitlines() if line.strip()]
            return web.json_response(
                {
                    "found": True,
                    "lyrics": raw_text,
                    "synced": is_synced,
                    "line_count": len(lines),
                    "artist": search_meta.artist,
                    "title": search_meta.title,
                }
            )

        return web.json_response(
            {
                "found": False,
                "lyrics": None,
                "synced": False,
                "line_count": 0,
                "artist": search_meta.artist,
                "title": search_meta.title,
                "message": "No lyrics found on LRCLIB.",
            }
        )

    app.router.add_post("/api/v1/app/tracks/{track_id}/auto-lyrics", handle_auto_lyrics)

    # 2.12 Planned bulk metadata & lyrics re-search endpoints
    async def handle_bulk_research_plan(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.REQUEST_TAG_CORRECTION):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Permission denied: UPLOADER role or above required."}),
                content_type="application/json",
            )
        if (
            catalog_repository is None
            or metadata_service is None
            or lyrics_client is None
            or organizer is None
        ):
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Bulk research services unavailable."}),
                content_type="application/json",
            )

        body: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed

        raw_ids = body.get("track_ids")
        track_ids = [str(t).strip() for t in raw_ids if str(t).strip()] if isinstance(raw_ids, list) else None
        filter_mode = str(body.get("filter_mode", "all")).strip()
        search_metadata = bool(body.get("search_metadata", True))
        search_lyrics = bool(body.get("search_lyrics", True))
        auto_apply = bool(body.get("auto_apply", True))

        bulk_service = BulkResearchService(
            catalog_repository=catalog_repository,
            metadata_service=metadata_service,
            lyrics_client=lyrics_client,
            organizer=organizer,
        )

        plan = await bulk_service.plan_bulk_research(
            track_ids=track_ids,
            filter_mode=filter_mode,
            search_metadata=search_metadata,
            search_lyrics=search_lyrics,
            auto_apply=auto_apply,
        )

        return web.json_response(
            {
                "track_ids": plan.track_ids,
                "total_tracks": plan.total_tracks,
                "missing_lyrics_count": plan.missing_lyrics_count,
                "missing_mbid_count": plan.missing_mbid_count,
                "estimated_seconds": plan.estimated_seconds,
                "search_metadata": plan.search_metadata,
                "search_lyrics": plan.search_lyrics,
                "auto_apply": plan.auto_apply,
            }
        )

    app.router.add_post("/api/v1/app/tracks/bulk-research/plan", handle_bulk_research_plan)

    async def handle_bulk_research_execute(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.REQUEST_TAG_CORRECTION):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Permission denied: UPLOADER role or above required."}),
                content_type="application/json",
            )
        if (
            catalog_repository is None
            or metadata_service is None
            or lyrics_client is None
            or organizer is None
        ):
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Bulk research services unavailable."}),
                content_type="application/json",
            )

        body: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed

        raw_ids = body.get("track_ids")
        track_ids = [str(t).strip() for t in raw_ids if str(t).strip()] if isinstance(raw_ids, list) else None
        filter_mode = str(body.get("filter_mode", "all")).strip()
        search_metadata = bool(body.get("search_metadata", True))
        search_lyrics = bool(body.get("search_lyrics", True))
        auto_apply = bool(body.get("auto_apply", True))

        bulk_service = BulkResearchService(
            catalog_repository=catalog_repository,
            metadata_service=metadata_service,
            lyrics_client=lyrics_client,
            organizer=organizer,
        )

        plan = await bulk_service.plan_bulk_research(
            track_ids=track_ids,
            filter_mode=filter_mode,
            search_metadata=search_metadata,
            search_lyrics=search_lyrics,
            auto_apply=auto_apply,
        )

        bus: QueueEventBus | None = app.get("event_bus")

        async def _on_bulk_progress(current: int, total: int, label: str, status: str) -> None:
            active_info = get_active_bulk_research()
            if active_info and bus:
                await bus.publish(
                    {
                        "type": "bulk_research_progress",
                        "data": serialize_bulk_progress(active_info),
                        "bulk_research": serialize_bulk_progress(active_info),
                    }
                )

        try:
            report = await bulk_service.execute_bulk_research(
                plan,
                actor_id=user.telegram_id,
                actor_name=user.display_name,
                progress_callback=_on_bulk_progress,
            )
        finally:
            if bus:
                await bus.publish(
                    {
                        "type": "bulk_research_progress",
                        "data": None,
                        "bulk_research": None,
                    }
                )

        return web.json_response(
            {
                "total_tracks": report.total_tracks,
                "applied_count": report.applied_count,
                "needs_review_count": report.needs_review_count,
                "skipped_count": report.skipped_count,
                "failed_count": report.failed_count,
                "elapsed_seconds": report.elapsed_seconds,
                "details": report.details,
            }
        )

    app.router.add_post("/api/v1/app/tracks/bulk-research/execute", handle_bulk_research_execute)

    # 2.8 Ingestion Hub & Live Queue Board Endpoints (Phase 4)
    async def handle_ingest_upload(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.SUBMIT_INGESTION):
            raise web.HTTPForbidden(
                text=json.dumps(
                    {"error": "Your role does not have permission to submit files for ingestion."}
                ),
                content_type="application/json",
            )
        if queue_worker is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Ingestion queue worker is unavailable."}),
                content_type="application/json",
            )

        if not request.content_type.startswith("multipart/"):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Expected multipart/form-data upload payload."}),
                content_type="application/json",
            )

        reader = await request.multipart()
        saved_file_path: Path | None = None
        original_filename: str | None = None
        file_bytes_written = 0

        scratch_base = scratch_dir or Path("storage/scratch")
        scratch_sub = scratch_base / str(uuid.uuid4())
        scratch_sub.mkdir(parents=True, exist_ok=True)

        while True:
            part = await reader.next()
            if part is None:
                break
            if not isinstance(part, BodyPartReader):
                continue

            raw_filename = part.filename or ""
            clean_filename = Path(raw_filename).name.strip()
            if not clean_filename:
                continue

            suffix = Path(clean_filename).suffix.lower()
            if suffix not in SUPPORTED_AUDIO_EXTENSIONS and suffix not in SUPPORTED_ARCHIVE_EXTENSIONS:
                shutil.rmtree(scratch_sub, ignore_errors=True)
                raise web.HTTPBadRequest(
                    text=json.dumps(
                        {
                            "error": f"Unsupported file format '{suffix}'. Supported audio: "
                            f"{', '.join(sorted(SUPPORTED_AUDIO_EXTENSIONS))}; archives: "
                            f"{', '.join(sorted(SUPPORTED_ARCHIVE_EXTENSIONS))}."
                        }
                    ),
                    content_type="application/json",
                )

            original_filename = clean_filename
            saved_file_path = scratch_sub / clean_filename
            with saved_file_path.open("wb") as handle:
                while chunk := await part.read_chunk():
                    handle.write(chunk)
                    file_bytes_written += len(chunk)
            break

        if saved_file_path is None or not saved_file_path.is_file():
            shutil.rmtree(scratch_sub, ignore_errors=True)
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "No valid audio or archive file found in multipart upload."}),
                content_type="application/json",
            )

        job, pos = await queue_worker.enqueue_job(
            user=user,
            source_path=saved_file_path,
            original_filename=original_filename,
            file_size_bytes=file_bytes_written,
        )

        asyncio.create_task(queue_worker.execute_job(job.job_id))

        await bus.publish(
            {
                "type": "job_enqueued",
                "job_id": job.job_id,
                "user_id": user.telegram_id,
                "state": job.state.value,
                "job": serialize_job(job, pos),
            }
        )

        return web.Response(
            text=json.dumps({"success": True, "job": serialize_job(job, pos)}),
            content_type="application/json",
            status=201,
        )

    app.router.add_post("/api/v1/app/ingest/upload", handle_ingest_upload)

    async def handle_get_queue(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        can_view_own = has_permission(user, Permission.VIEW_OWN_JOBS)
        can_view_global = has_permission(user, Permission.VIEW_GLOBAL_QUEUE)

        if not can_view_own and not can_view_global:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Insufficient permissions to view ingestion queue."}),
                content_type="application/json",
            )

        if catalog_repository is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Catalog repository is unavailable."}),
                content_type="application/json",
            )

        scope_mine = request.query.get("mine", "").lower() in {"1", "true", "yes"} or not can_view_global
        target_user_id = user.telegram_id if scope_mine else None

        queued_jobs = await catalog_repository.list_queued_jobs(user_id=target_user_id)
        paused_jobs = await catalog_repository.list_paused_jobs(user_id=target_user_id)
        all_active = await catalog_repository.list_active_jobs()
        if scope_mine:
            all_active = [j for j in all_active if j.user_id == user.telegram_id]

        recent_jobs = await catalog_repository.list_jobs(user_id=target_user_id, limit=30)

        seen_ids: set[str] = set()
        combined_jobs: list[IngestionJob] = []
        pos_map: dict[str, int] = {j.job_id: idx for idx, j in enumerate(queued_jobs, start=1)}

        for j in all_active:
            if j.job_id not in seen_ids:
                seen_ids.add(j.job_id)
                combined_jobs.append(j)

        for j in queued_jobs:
            if j.job_id not in seen_ids:
                seen_ids.add(j.job_id)
                combined_jobs.append(j)

        for j in paused_jobs:
            if j.job_id not in seen_ids:
                seen_ids.add(j.job_id)
                combined_jobs.append(j)

        for j in recent_jobs:
            if j.job_id not in seen_ids:
                seen_ids.add(j.job_id)
                combined_jobs.append(j)

        concurrency = queue_worker.concurrency_limit if queue_worker else 1
        active_cnt = queue_worker.active_count if queue_worker else 0

        payload = {
            "concurrency_limit": concurrency,
            "active_count": active_cnt,
            "queued_count": len(queued_jobs),
            "paused_count": len(paused_jobs),
            "can_view_global": can_view_global,
            "scope": "mine" if scope_mine else "global",
            "active_bulk_research": serialize_bulk_progress(get_active_bulk_research()),
            "jobs": [serialize_job(j, pos_map.get(j.job_id, 0)) for j in combined_jobs],
        }
        return web.Response(
            text=json.dumps(payload),
            content_type="application/json",
            status=200,
        )

    app.router.add_get("/api/v1/app/queue", handle_get_queue)

    async def handle_queue_action(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        job_id = request.match_info.get("job_id", "").strip()
        if not job_id:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Missing job_id."}),
                content_type="application/json",
            )

        if catalog_repository is None or queue_worker is None:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Queue service is unavailable."}),
                content_type="application/json",
            )

        job = await catalog_repository.get_job(job_id)
        if job is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": f"Job '{job_id}' not found."}),
                content_type="application/json",
            )

        try:
            data = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": f"Invalid JSON payload: {exc}"}),
                content_type="application/json",
            ) from exc

        action = str(data.get("action", "")).strip().lower()

        if action == "pause":
            updated_job = await queue_worker.pause_job(job_id, actor=user)
            await bus.publish(
                {
                    "type": "job_state_changed",
                    "job_id": updated_job.job_id,
                    "user_id": updated_job.user_id,
                    "state": updated_job.state.value,
                    "job": serialize_job(updated_job),
                }
            )
            return web.Response(
                text=json.dumps({"success": True, "job": serialize_job(updated_job)}),
                content_type="application/json",
                status=200,
            )

        elif action in {"resume", "unlock"}:
            updated_job = await queue_worker.unlock_job(job_id, actor=user)
            if updated_job.state == JobState.QUEUED:
                asyncio.create_task(queue_worker.execute_job(updated_job.job_id))
            await bus.publish(
                {
                    "type": "job_state_changed",
                    "job_id": updated_job.job_id,
                    "user_id": updated_job.user_id,
                    "state": updated_job.state.value,
                    "job": serialize_job(updated_job),
                }
            )
            return web.Response(
                text=json.dumps({"success": True, "job": serialize_job(updated_job)}),
                content_type="application/json",
                status=200,
            )

        elif action == "cancel":
            updated_job = await queue_worker.cancel_job(job_id, actor=user)
            await bus.publish(
                {
                    "type": "job_state_changed",
                    "job_id": updated_job.job_id,
                    "user_id": updated_job.user_id,
                    "state": updated_job.state.value,
                    "job": serialize_job(updated_job),
                }
            )
            return web.Response(
                text=json.dumps({"success": True, "job": serialize_job(updated_job)}),
                content_type="application/json",
                status=200,
            )

        elif action == "select_candidate":
            if pipeline is None:
                raise web.HTTPServiceUnavailable(
                    text=json.dumps({"error": "Ingestion pipeline unavailable."}),
                    content_type="application/json",
                )

            is_owner = job.user_id is not None and job.user_id == user.telegram_id
            if is_owner:
                require_permission(user, Permission.RESOLVE_OWN_DISAMBIGUATION)
            else:
                require_permission(user, Permission.RESOLVE_OTHERS_JOBS)

            candidate_idx_raw = data.get("candidate_index")
            if candidate_idx_raw is None or not isinstance(candidate_idx_raw, int):
                raise web.HTTPBadRequest(
                    text=json.dumps({"error": "Missing or invalid integer 'candidate_index'."}),
                    content_type="application/json",
                )

            async def _publish_stage(staged_job: IngestionJob) -> None:
                await bus.publish(
                    {
                        "type": "stage_changed",
                        "job_id": staged_job.job_id,
                        "user_id": staged_job.user_id,
                        "state": staged_job.state.value,
                        "stage": staged_job.state.value,
                        "processed_tracks": staged_job.processed_tracks,
                        "total_tracks": staged_job.total_tracks,
                        "job": serialize_job(staged_job),
                    }
                )

            try:
                result = await pipeline.confirm_candidate(
                    job_id=job.job_id,
                    candidate_index=candidate_idx_raw,
                    stage_callback=_publish_stage,
                )
            except (KeyError, IndexError) as exc:
                raise web.HTTPBadRequest(
                    text=json.dumps({"error": f"Failed to confirm candidate: {exc}"}),
                    content_type="application/json",
                ) from exc
            except Exception as exc:
                logger.error("Pipeline confirmation failed for job %s: %s", job.job_id, exc, exc_info=True)
                failed_job = await catalog_repository.get_job(job.job_id) or job
                await bus.publish(
                    {
                        "type": "job_failed",
                        "job_id": job.job_id,
                        "user_id": job.user_id,
                        "state": JobState.FAILED.value,
                        "error": str(exc),
                        "job": serialize_job(failed_job),
                    }
                )
                raise web.HTTPInternalServerError(
                    text=json.dumps({"error": f"Ingestion failed: {exc}"}),
                    content_type="application/json",
                ) from exc

            await queue_worker.record_confirmed_completion(result)
            await bus.publish(
                {
                    "type": "job_completed",
                    "job_id": result.job.job_id,
                    "user_id": result.job.user_id,
                    "state": result.job.state.value,
                    "job": serialize_job(result.job),
                }
            )
            return web.Response(
                text=json.dumps({"success": True, "job": serialize_job(result.job)}),
                content_type="application/json",
                status=200,
            )

        elif action == "manual_search":
            if pipeline is None:
                raise web.HTTPServiceUnavailable(
                    text=json.dumps({"error": "Ingestion pipeline unavailable."}),
                    content_type="application/json",
                )
            is_owner = job.user_id is not None and job.user_id == user.telegram_id
            if is_owner:
                require_permission(user, Permission.RESOLVE_OWN_DISAMBIGUATION)
            else:
                require_permission(user, Permission.RESOLVE_OTHERS_JOBS)

            query = str(data.get("query", "")).strip()
            if not query:
                raise web.HTTPBadRequest(
                    text=json.dumps({"error": "Missing 'query' string for manual search."}),
                    content_type="application/json",
                )

            try:
                result = await pipeline.search_manual_candidates(job_id=job.job_id, query=query)
            except Exception as exc:
                raise web.HTTPBadRequest(
                    text=json.dumps({"error": f"Manual search failed: {exc}"}),
                    content_type="application/json",
                ) from exc

            await bus.publish(
                {
                    "type": "confirmation_required",
                    "job_id": result.job.job_id,
                    "user_id": result.job.user_id,
                    "state": result.job.state.value,
                    "job": serialize_job(result.job),
                }
            )
            return web.Response(
                text=json.dumps({"success": True, "job": serialize_job(result.job)}),
                content_type="application/json",
                status=200,
            )

        elif action in {"apply_edit", "apply_custom_metadata"}:
            if pipeline is None:
                raise web.HTTPServiceUnavailable(
                    text=json.dumps({"error": "Ingestion pipeline unavailable."}),
                    content_type="application/json",
                )
            is_owner = job.user_id is not None and job.user_id == user.telegram_id
            if is_owner:
                require_permission(user, Permission.RESOLVE_OWN_DISAMBIGUATION)
            else:
                require_permission(user, Permission.RESOLVE_OTHERS_JOBS)

            seed_meta = job.selected_metadata or (
                job.candidates[0].metadata
                if job.candidates
                else TrackMetadata(title="Unknown Title", artist="Unknown Artist", album="Unknown Album")
            )
            title = str(data.get("title") or seed_meta.title).strip()
            artist = str(data.get("artist") or seed_meta.artist).strip()
            album = str(data.get("album") or seed_meta.album).strip()
            raw_yr = data.get("year")
            year = int(raw_yr) if raw_yr is not None and str(raw_yr).strip().isdigit() else seed_meta.year
            raw_trk = data.get("track_number")
            track_number = (
                int(raw_trk)
                if raw_trk is not None and str(raw_trk).strip().isdigit()
                else (seed_meta.track_number or 1)
            )
            rec_mbid = (
                str(data.get("recording_mbid")).strip()
                if data.get("recording_mbid")
                else seed_meta.recording_mbid
            )
            rel_mbid = (
                str(data.get("release_mbid")).strip() if data.get("release_mbid") else seed_meta.release_mbid
            )

            custom_meta = TrackMetadata(
                title=title or "Unknown Title",
                artist=artist or "Unknown Artist",
                album=album or "Unknown Album",
                album_artist=artist or "Unknown Artist",
                year=year,
                track_number=track_number,
                duration_seconds=seed_meta.duration_seconds,
                recording_mbid=rec_mbid,
                release_mbid=rel_mbid,
            )

            lyrics_obj: LyricsData | None = None
            raw_lyr = data.get("lyrics")
            if isinstance(raw_lyr, str) and raw_lyr.strip():
                clean_lyr = raw_lyr.strip()
                is_synced = "[" in clean_lyr and "]" in clean_lyr
                lyrics_obj = LyricsData(
                    synced_lyrics=clean_lyr if is_synced else None,
                    plain_lyrics=None if is_synced else clean_lyr,
                )

            cover_bytes: bytes | None = None
            cover_b64 = data.get("cover_base64")
            if isinstance(cover_b64, str) and cover_b64.strip():
                try:
                    cover_bytes = base64.b64decode(cover_b64.strip())
                except Exception as b64_err:
                    logger.debug("Failed to decode cover_base64 for job %s: %s", job.job_id, b64_err)
            cover_mime = str(data.get("cover_mime") or "image/jpeg")

            async def _publish_custom_stage(staged_job: IngestionJob) -> None:
                await bus.publish(
                    {
                        "type": "stage_changed",
                        "job_id": staged_job.job_id,
                        "user_id": staged_job.user_id,
                        "state": staged_job.state.value,
                        "stage": staged_job.state.value,
                        "processed_tracks": staged_job.processed_tracks,
                        "total_tracks": staged_job.total_tracks,
                        "job": serialize_job(staged_job),
                    }
                )

            try:
                result = await pipeline.complete_custom_ingestion(
                    job_id=job.job_id,
                    metadata=custom_meta,
                    custom_lyrics=lyrics_obj,
                    custom_cover_bytes=cover_bytes,
                    custom_cover_mime=cover_mime,
                    stage_callback=_publish_custom_stage,
                )
            except Exception as exc:
                logger.error("Custom ingestion failed for job %s: %s", job.job_id, exc, exc_info=True)
                failed_job = await catalog_repository.get_job(job.job_id) or job
                await bus.publish(
                    {
                        "type": "job_failed",
                        "job_id": job.job_id,
                        "user_id": job.user_id,
                        "state": JobState.FAILED.value,
                        "error": str(exc),
                        "job": serialize_job(failed_job),
                    }
                )
                raise web.HTTPInternalServerError(
                    text=json.dumps({"error": f"Custom ingestion failed: {exc}"}),
                    content_type="application/json",
                ) from exc

            if queue_worker is not None:
                await queue_worker.record_confirmed_completion(result)
            await bus.publish(
                {
                    "type": "job_completed",
                    "job_id": result.job.job_id,
                    "user_id": result.job.user_id,
                    "state": result.job.state.value,
                    "job": serialize_job(result.job),
                }
            )
            return web.Response(
                text=json.dumps({"success": True, "job": serialize_job(result.job)}),
                content_type="application/json",
                status=200,
            )

        else:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": f"Unknown action '{action}'."}),
                content_type="application/json",
            )

    app.router.add_post("/api/v1/app/queue/{job_id}/action", handle_queue_action)

    async def handle_queue_events_sse(request: web.Request) -> web.StreamResponse:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        can_view_global = has_permission(user, Permission.VIEW_GLOBAL_QUEUE)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        init_payload = json.dumps({"status": "connected", "user_id": user.telegram_id})
        await response.write(f"event: connected\ndata: {init_payload}\n\n".encode())

        sub_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        bus.subscribe(sub_queue)

        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(sub_queue.get(), timeout=15.0)
                    event_user_id = event_data.get("user_id")
                    if can_view_global or event_user_id is None or event_user_id == user.telegram_id:
                        event_type = event_data.get("type", "update")
                        payload = json.dumps(event_data)
                        await response.write(f"event: {event_type}\ndata: {payload}\n\n".encode())
                except TimeoutError:
                    await response.write(b": ping\n\n")
        except asyncio.CancelledError, ConnectionResetError:
            pass
        finally:
            bus.unsubscribe(sub_queue)

        return response

    app.router.add_get("/api/v1/app/queue/events", handle_queue_events_sse)

    # 2.7 User Access Request endpoints (for GUEST accounts requesting catalog access)
    async def handle_post_access_request(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        if not has_permission(user, Permission.SUBMIT_ACCESS_REQUEST):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "User is not permitted to submit access requests."}),
                content_type="application/json",
            )
        try:
            req_record, _ = await governance_service.submit_access_request(user)
            return web.json_response(
                {
                    "success": True,
                    "request_id": req_record.id,
                    "status": req_record.status.value,
                    "message": "Access request submitted to administrators.",
                }
            )
        except Exception as exc:
            return web.json_response(
                {"success": False, "error": str(exc)},
                status=400,
            )

    app.router.add_post("/api/v1/app/access-request", handle_post_access_request)

    async def handle_get_access_request(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        repo = getattr(governance_service, "_repo", None)
        if repo is None:
            return web.json_response({"has_pending": False, "request": None})
        pending = await repo.get_pending_access_request(user.telegram_id)
        if pending is None:
            return web.json_response({"has_pending": False, "request": None})
        return web.json_response(
            {
                "has_pending": True,
                "request": {
                    "id": pending.id,
                    "status": pending.status.value,
                    "created_at": pending.created_at.isoformat() if pending.created_at else None,
                },
            }
        )

    app.router.add_get("/api/v1/app/access-request", handle_get_access_request)

    # 2.8 Governance & User Administration endpoints
    def serialize_admin_user(
        target_user: UserAccount,
        uploads: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        return {
            "telegram_id": target_user.telegram_id,
            "role": target_user.role.value,
            "display_name": target_user.display_name,
            "username": target_user.username,
            "is_blocked": target_user.is_blocked,
            "authentik_linked": target_user.is_oidc_linked,
            "authentik_username": target_user.authentik_username,
            "authentik_groups": list(target_user.authentik_groups),
            "oidc_role_override": target_user.oidc_role_override,
            "created_at": target_user.created_at.isoformat() if target_user.created_at else None,
            "updated_at": target_user.updated_at.isoformat() if target_user.updated_at else None,
            "uploads_count": {
                "albums": uploads[0] if uploads else 0,
                "tracks": uploads[1] if uploads else 0,
            },
        }

    async def handle_get_admin_users(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        try:
            require_permission(user, Permission.MANAGE_USERS)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        role_filter = request.query.get("role", "").strip().upper()
        search_query = request.query.get("query", "").strip().lower()

        target_role: UserRole | None = None
        if role_filter and role_filter in UserRole.__members__:
            target_role = UserRole(role_filter)

        repo = getattr(governance_service, "_repo", None)
        if repo is None:
            return web.json_response({"users": []})

        all_users = await repo.list_users(role=target_role)
        results: list[dict[str, Any]] = []
        for u in all_users:
            if search_query:
                name_match = search_query in u.display_name.lower()
                username_match = bool(u.username and search_query in u.username.lower())
                id_match = search_query in str(u.telegram_id)
                if not (name_match or username_match or id_match):
                    continue
            uploads = await repo.count_user_uploads(u.telegram_id)
            results.append(serialize_admin_user(u, uploads))

        return web.json_response({"users": results})

    app.router.add_get("/api/v1/app/admin/users", handle_get_admin_users)

    async def handle_post_admin_user_role(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        try:
            require_permission(user, Permission.MANAGE_USERS)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        raw_user_id = request.match_info.get("user_id", "").strip()
        try:
            target_telegram_id = int(raw_user_id)
        except ValueError:
            return web.json_response({"error": "Invalid user_id parameter."}, status=400)

        if target_telegram_id == user.telegram_id:
            return web.json_response({"error": "You cannot change your own role."}, status=400)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Malformed JSON payload."}, status=400)

        role_str = str(data.get("role", "")).strip().upper()
        if role_str not in UserRole.__members__:
            return web.json_response({"error": f"Invalid role '{role_str}'."}, status=400)

        new_role = UserRole(role_str)
        reason = str(data.get("reason", "Role updated via Mini App")).strip()

        try:
            updated_user = await governance_service.change_user_role(
                actor=user,
                target_telegram_id=target_telegram_id,
                new_role=new_role,
                reason=reason,
            )
        except KeyError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        repo = getattr(governance_service, "_repo", None)
        uploads = await repo.count_user_uploads(updated_user.telegram_id) if repo else (0, 0)
        return web.json_response({"user": serialize_admin_user(updated_user, uploads)})

    app.router.add_post("/api/v1/app/admin/users/{user_id}/role", handle_post_admin_user_role)

    async def handle_post_admin_user_block(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        try:
            require_permission(user, Permission.MANAGE_USERS)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        raw_user_id = request.match_info.get("user_id", "").strip()
        try:
            target_telegram_id = int(raw_user_id)
        except ValueError:
            return web.json_response({"error": "Invalid user_id parameter."}, status=400)

        if target_telegram_id == user.telegram_id:
            return web.json_response({"error": "You cannot block your own account."}, status=400)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Malformed JSON payload."}, status=400)

        is_blocked = bool(data.get("is_blocked", False))
        reason = str(data.get("reason", "Block status updated via Mini App")).strip()

        try:
            updated_user = await governance_service.set_user_blocked(
                actor=user,
                target_telegram_id=target_telegram_id,
                is_blocked=is_blocked,
                reason=reason,
            )
        except KeyError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        repo = getattr(governance_service, "_repo", None)
        uploads = await repo.count_user_uploads(updated_user.telegram_id) if repo else (0, 0)
        return web.json_response({"user": serialize_admin_user(updated_user, uploads)})

    app.router.add_post("/api/v1/app/admin/users/{user_id}/block", handle_post_admin_user_block)

    async def handle_post_admin_user_oidc_disconnect(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        try:
            require_permission(user, Permission.MANAGE_USERS)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        raw_user_id = request.match_info.get("user_id", "").strip()
        try:
            target_telegram_id = int(raw_user_id)
        except ValueError:
            return web.json_response({"error": "Invalid user_id parameter."}, status=400)

        if governance_service is None:
            return web.json_response({"error": "Governance service unavailable."}, status=503)

        try:
            updated = await governance_service.unlink_user_oidc(
                actor=user,
                target_telegram_id=target_telegram_id,
            )
        except KeyError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        repo = getattr(governance_service, "_repo", None)
        uploads = await repo.count_user_uploads(target_telegram_id) if repo else (0, 0)
        return web.json_response(
            {
                "success": True,
                "message": "User Authentik OIDC account unlinked.",
                "user": serialize_admin_user(updated, uploads),
            }
        )

    app.router.add_post(
        "/api/v1/app/admin/users/{user_id}/oidc/disconnect",
        handle_post_admin_user_oidc_disconnect,
    )

    async def handle_post_admin_user_oidc_override(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        try:
            require_permission(user, Permission.MANAGE_USERS)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        raw_user_id = request.match_info.get("user_id", "").strip()
        try:
            target_telegram_id = int(raw_user_id)
        except ValueError:
            return web.json_response({"error": "Invalid user_id parameter."}, status=400)

        if governance_service is None:
            return web.json_response({"error": "Governance service unavailable."}, status=503)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Malformed JSON payload."}, status=400)

        override = bool(data.get("override", True))
        try:
            updated = await governance_service.set_user_oidc_override(
                actor=user,
                target_telegram_id=target_telegram_id,
                override=override,
            )
        except KeyError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        repo = getattr(governance_service, "_repo", None)
        uploads = await repo.count_user_uploads(target_telegram_id) if repo else (0, 0)
        return web.json_response(
            {
                "success": True,
                "message": f"OIDC role override set to {override}.",
                "user": serialize_admin_user(updated, uploads),
            }
        )

    app.router.add_post(
        "/api/v1/app/admin/users/{user_id}/oidc/override",
        handle_post_admin_user_oidc_override,
    )

    async def handle_get_admin_access_requests(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        try:
            require_permission(user, Permission.MANAGE_USERS)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        repo = getattr(governance_service, "_repo", None)
        if repo is None:
            return web.json_response({"requests": []})

        pending_requests: list[AccessRequestRecord] = await repo.list_pending_access_requests()
        results: list[dict[str, Any]] = []
        for req in pending_requests:
            req_user = await repo.get_user(req.telegram_id)
            results.append(
                {
                    "id": req.id,
                    "telegram_id": req.telegram_id,
                    "display_name": req_user.display_name if req_user else f"User {req.telegram_id}",
                    "username": req_user.username if req_user else None,
                    "status": req.status.value,
                    "created_at": req.created_at.isoformat() if req.created_at else None,
                }
            )

        return web.json_response({"requests": results})

    app.router.add_get("/api/v1/app/admin/access-requests", handle_get_admin_access_requests)

    async def handle_post_admin_access_request_resolve(request: web.Request) -> web.Response:
        user, _ = await authenticate_tma_request(
            request=request,
            bot_token=bot_token,
            governance_service=governance_service,
        )
        try:
            require_permission(user, Permission.MANAGE_USERS)
        except PermissionDeniedError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        raw_req_id = request.match_info.get("request_id", "").strip()
        try:
            request_id = int(raw_req_id)
        except ValueError:
            return web.json_response({"error": "Invalid request_id parameter."}, status=400)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Malformed JSON payload."}, status=400)

        action = str(data.get("action", "")).strip().lower()
        if action == "approve":
            granted_role_str = str(data.get("role", "")).strip().upper()
            granted_role = (
                UserRole(granted_role_str) if granted_role_str in UserRole.__members__ else UserRole.UPLOADER
            )
            try:
                resolved_user = await governance_service.approve_access_request(
                    actor=user,
                    request_id=request_id,
                    granted_role=granted_role,
                )
                repo = getattr(governance_service, "_repo", None)
                uploads = await repo.count_user_uploads(resolved_user.telegram_id) if repo else (0, 0)
                return web.json_response(
                    {
                        "request_id": request_id,
                        "status": "APPROVED",
                        "user": serialize_admin_user(resolved_user, uploads),
                    }
                )
            except KeyError as exc:
                return web.json_response({"error": str(exc)}, status=404)
        elif action == "deny":
            try:
                resolved_req: AccessRequestRecord = await governance_service.deny_access_request(
                    actor=user,
                    request_id=request_id,
                )
                status_val: AccessRequestStatus = resolved_req.status
                return web.json_response(
                    {
                        "request_id": request_id,
                        "status": status_val.value,
                    }
                )
            except KeyError as exc:
                return web.json_response({"error": str(exc)}, status=404)
        else:
            return web.json_response({"error": "Action must be 'approve' or 'deny'."}, status=400)

    app.router.add_post(
        "/api/v1/app/admin/access-requests/{request_id}/resolve",
        handle_post_admin_access_request_resolve,
    )

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

    # 4. SPA Static Files & Fallback Handler (with Vite dev proxy support in dev_mode)
    resolved_static = static_dir.resolve() if static_dir else None

    async def _check_vite_running() -> bool:
        if not dev_mode:
            return False
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 5173), timeout=0.15)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _proxy_to_vite(request: web.Request) -> web.StreamResponse:
        target_url = f"http://127.0.0.1:5173{request.rel_url}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}}

        # Handle Vite HMR WebSockets
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws_server = web.WebSocketResponse()
            await ws_server.prepare(request)
            async with (
                ClientSession() as session,
                session.ws_connect(target_url, headers=headers) as ws_client,
            ):

                async def forward_to_client() -> None:
                    async for msg in ws_client:
                        if msg.type == WSMsgType.TEXT:
                            await ws_server.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_server.send_bytes(msg.data)
                        elif msg.type == WSMsgType.CLOSE:
                            await ws_server.close()
                            break

                async def forward_to_server() -> None:
                    async for msg in ws_server:
                        if msg.type == WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_client.send_bytes(msg.data)
                        elif msg.type == WSMsgType.CLOSE:
                            await ws_client.close()
                            break

                await asyncio.gather(forward_to_client(), forward_to_server(), return_exceptions=True)
            return ws_server

        # Handle regular HTTP asset/page requests
        body = await request.read()
        async with (
            ClientSession() as session,
            session.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=body,
                allow_redirects=False,
            ) as upstream_resp,
        ):
            resp_headers = {
                k: v
                for k, v in upstream_resp.headers.items()
                if k.lower() not in {"transfer-encoding", "content-encoding", "content-length"}
            }
            resp_body = await upstream_resp.read()
            return web.Response(
                body=resp_body,
                status=upstream_resp.status,
                headers=resp_headers,
            )

    async def handle_spa_fallback(request: web.Request) -> web.StreamResponse:
        if dev_mode and await _check_vite_running():
            try:
                return await _proxy_to_vite(request)
            except Exception as exc:
                logger.debug(
                    "Vite dev proxy request to %s failed: %s; falling back to static files.",
                    request.rel_url,
                    exc,
                )

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

    app.router.add_route("*", "/{tail:.*}", handle_spa_fallback)

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
        catalog_repository: GovernanceRepositoryPort | None = None,
        organizer: LibraryOrganizer | None = None,
        metadata_service: MusicBrainzMetadataService | None = None,
        lyrics_client: LrclibLyricsClient | None = None,
        queue_worker: IngestionQueueWorker | None = None,
        pipeline: IngestionPipeline | None = None,
        scratch_dir: Path | None = None,
        event_bus: QueueEventBus | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._governance_service = governance_service
        self._coordinator = coordinator
        self._static_dir = static_dir
        self._host = host
        self._port = port
        self._owner_telegram_ids = owner_telegram_ids
        self._dev_mode = dev_mode
        self._catalog_repository = catalog_repository
        self._organizer = organizer
        self._metadata_service = metadata_service
        self._lyrics_client = lyrics_client
        self._queue_worker = queue_worker
        self._pipeline = pipeline
        self._scratch_dir = scratch_dir
        self._event_bus = event_bus

        self._app = build_webapp_api(
            bot_token=bot_token,
            governance_service=governance_service,
            coordinator=coordinator,
            static_dir=static_dir,
            owner_telegram_ids=owner_telegram_ids,
            dev_mode=dev_mode,
            catalog_repository=catalog_repository,
            organizer=organizer,
            metadata_service=metadata_service,
            lyrics_client=lyrics_client,
            queue_worker=queue_worker,
            pipeline=pipeline,
            scratch_dir=scratch_dir,
            event_bus=event_bus,
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
