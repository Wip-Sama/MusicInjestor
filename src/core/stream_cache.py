"""Short-lived TTL + size-bounded ephemeral disk cache for streaming provider audio."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class EphemeralStreamCache:
    """Stores streamed audio from transient streaming providers outside the archival library.

    Entries are strictly short-lived: evicted automatically after `ttl_seconds` and
    least-recently-used evicted once total disk footprint exceeds `max_bytes`.
    """

    def __init__(
        self,
        cache_dir: Path,
        ttl_seconds: int = 3600,
        max_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self._cache_dir = cache_dir
        self._ttl_seconds = max(60, ttl_seconds)
        self._max_bytes = max(1024 * 1024, max_bytes)
        self._lock = asyncio.Lock()

    @property
    def cache_dir(self) -> Path:
        """Return configured root directory for temporary stream cache files."""
        return self._cache_dir

    def _entry_stem(self, provider_id: str, track_id: str, quality: str | None = None) -> str:
        raw = f"{provider_id}\0{track_id}\0{(quality or 'default').lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _audio_path(self, stem: str) -> Path:
        return self._cache_dir / f"{stem}.audio"

    def _meta_path(self, stem: str) -> Path:
        return self._cache_dir / f"{stem}.meta.json"

    def temp_part_path(self, provider_id: str, track_id: str, quality: str | None = None) -> Path:
        """Return staging `.part` path for writing an in-flight stream before commit."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        stem = self._entry_stem(provider_id, track_id, quality)
        return self._cache_dir / f"{stem}.{os.getpid()}.{time.monotonic_ns()}.part"

    async def get_cached_entry(
        self,
        provider_id: str,
        track_id: str,
        quality: str | None = None,
    ) -> tuple[Path, str] | None:
        """Return `(audio_path, content_type)` if cached within TTL, else None."""
        stem = self._entry_stem(provider_id, track_id, quality)
        audio_path = self._audio_path(stem)
        meta_path = self._meta_path(stem)

        async with self._lock:
            if not audio_path.is_file():
                return None

            try:
                stat = audio_path.stat()
            except OSError:
                return None

            if (time.time() - stat.st_mtime) > self._ttl_seconds or stat.st_size <= 0:
                with contextlib.suppress(OSError):
                    audio_path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    meta_path.unlink(missing_ok=True)
                return None

            content_type = "audio/mpeg"
            if meta_path.is_file():
                with contextlib.suppress(Exception):
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    content_type = str(meta.get("content_type") or "audio/mpeg")

            # Preserve initial write mtime for strict hard TTL expiration of ephemeral streams
            return audio_path, content_type

    async def commit_stream_file(
        self,
        part_path: Path,
        provider_id: str,
        track_id: str,
        quality: str | None = None,
        content_type: str = "audio/mpeg",
    ) -> Path | None:
        """Promote a completed `.part` buffer into the ephemeral cache if within size limits."""
        if not part_path.is_file():
            return None

        try:
            size = part_path.stat().st_size
        except OSError:
            return None

        if size <= 0 or size > self._max_bytes:
            with contextlib.suppress(OSError):
                part_path.unlink(missing_ok=True)
            return None

        stem = self._entry_stem(provider_id, track_id, quality)
        target_audio = self._audio_path(stem)
        target_meta = self._meta_path(stem)

        async with self._lock:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            os.replace(part_path, target_audio)
            with contextlib.suppress(OSError):
                target_meta.write_text(
                    json.dumps(
                        {
                            "provider_id": provider_id,
                            "track_id": track_id,
                            "quality": quality or "default",
                            "content_type": content_type,
                            "cached_at": time.time(),
                        }
                    ),
                    encoding="utf-8",
                )
            await self._evict_locked()

        logger.debug(
            "Cached ephemeral stream (%d bytes, %s) for %s/%s at %s",
            size,
            content_type,
            provider_id,
            track_id,
            target_audio.name,
        )
        return target_audio

    async def _evict_locked(self) -> int:
        """Evict expired entries first, then oldest entries until total size <= max_bytes."""
        if not self._cache_dir.is_dir():
            return 0

        now = time.time()
        removed = 0
        survivors: list[tuple[float, int, Path]] = []

        for entry in list(self._cache_dir.iterdir()):
            if not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue

            age = now - stat.st_mtime
            if entry.suffix == ".part":
                if age > 300.0:
                    with contextlib.suppress(OSError):
                        entry.unlink(missing_ok=True)
                        removed += 1
                continue

            if entry.suffix == ".audio":
                if age > self._ttl_seconds:
                    with contextlib.suppress(OSError):
                        entry.unlink(missing_ok=True)
                        self._cache_dir.joinpath(f"{entry.stem}.meta.json").unlink(missing_ok=True)
                        removed += 1
                else:
                    survivors.append((stat.st_mtime, stat.st_size, entry))
            elif entry.name.endswith(".meta.json"):
                audio_sibling = self._cache_dir / f"{entry.name.removesuffix('.meta.json')}.audio"
                if not audio_sibling.exists():
                    with contextlib.suppress(OSError):
                        entry.unlink(missing_ok=True)

        total_bytes = sum(item[1] for item in survivors)
        if total_bytes > self._max_bytes:
            survivors.sort(key=lambda item: item[0])  # Oldest first
            for _, size, audio_file in survivors:
                if total_bytes <= self._max_bytes:
                    break
                with contextlib.suppress(OSError):
                    audio_file.unlink(missing_ok=True)
                    self._cache_dir.joinpath(f"{audio_file.stem}.meta.json").unlink(missing_ok=True)
                    total_bytes -= size
                    removed += 1

        return removed

    async def evict_expired(self) -> int:
        """Purge all TTL-expired cached streams and orphaned temporary `.part` buffers."""
        async with self._lock:
            return await self._evict_locked()

    async def clear(self) -> int:
        """Remove all entries from the ephemeral stream cache directory."""
        async with self._lock:
            if not self._cache_dir.is_dir():
                return 0
            count = 0
            for entry in list(self._cache_dir.iterdir()):
                if entry.is_file():
                    with contextlib.suppress(OSError):
                        entry.unlink(missing_ok=True)
                        count += 1
            return count
