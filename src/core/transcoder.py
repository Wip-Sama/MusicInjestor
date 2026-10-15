"""On-the-fly server-side audio transcoding and in-RAM LRU cache."""

import asyncio
import collections
import logging
import shutil
from pathlib import Path

from src.core.exceptions import AudioTranscodingError

logger = logging.getLogger(__name__)

SUPPORTED_TRANSCODE_FORMATS: frozenset[str] = frozenset({"opus", "mp3", "ogg"})


def is_ffmpeg_available(ffmpeg_bin: str = "ffmpeg") -> bool:
    """Return True if ffmpeg executable is available on PATH."""
    return shutil.which(ffmpeg_bin) is not None


class AudioTranscodeCache:
    """In-RAM Least-Recently-Used (LRU) cache for transcoded audio buffers."""

    def __init__(self, max_items: int = 16, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._max_items = max(1, max_items)
        self._max_bytes = max(1024 * 1024, max_bytes)
        self._cache: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._current_bytes = 0
        self._lock = asyncio.Lock()

    @property
    def current_items(self) -> int:
        """Current number of cached audio tracks in RAM."""
        return len(self._cache)

    @property
    def current_bytes(self) -> int:
        """Current total memory footprint of cached audio in bytes."""
        return self._current_bytes

    def _make_key(self, path: Path, target_format: str, bitrate_kbps: int) -> str:
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        return f"{path.resolve()}:{target_format.lower()}:{bitrate_kbps}:{mtime}"

    async def get(self, path: Path, target_format: str, bitrate_kbps: int) -> bytes | None:
        """Retrieve transcoded audio from RAM cache, refreshing LRU recency."""
        key = self._make_key(path, target_format, bitrate_kbps)
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                logger.debug("Transcode RAM cache hit for %s (%s)", path.name, target_format)
                return self._cache[key]
        return None

    async def put(self, path: Path, target_format: str, bitrate_kbps: int, data: bytes) -> None:
        """Store transcoded audio bytes in RAM cache, evicting oldest items when limits exceeded."""
        key = self._make_key(path, target_format, bitrate_kbps)
        data_len = len(data)
        async with self._lock:
            if key in self._cache:
                self._current_bytes -= len(self._cache[key])
                del self._cache[key]

            # Evict if exceeding max items or bytes
            while self._cache and (
                len(self._cache) >= self._max_items or (self._current_bytes + data_len) > self._max_bytes
            ):
                _, evicted_data = self._cache.popitem(last=False)
                self._current_bytes -= len(evicted_data)

            self._cache[key] = data
            self._current_bytes += data_len
            logger.debug(
                "Stored %d bytes in transcode RAM cache for %s (%s) [total: %d bytes across %d items]",
                data_len,
                path.name,
                target_format,
                self._current_bytes,
                len(self._cache),
            )

    async def clear(self) -> None:
        """Evict all cached audio buffers from RAM."""
        async with self._lock:
            self._cache.clear()
            self._current_bytes = 0


async def async_transcode_audio(
    input_path: Path,
    target_format: str = "opus",
    bitrate_kbps: int = 128,
    ffmpeg_bin: str = "ffmpeg",
) -> bytes:
    """Transcode an audio file into Opus or MP3 in RAM using ffmpeg.

    Returns the transcoded audio as raw bytes.
    """
    if not input_path.is_file():
        msg = f"Audio source file does not exist: {input_path}"
        raise AudioTranscodingError(msg)

    fmt = target_format.strip().lower()
    if fmt not in SUPPORTED_TRANSCODE_FORMATS:
        supported = sorted(SUPPORTED_TRANSCODE_FORMATS)
        msg = f"Unsupported target transcode format: '{target_format}'. Supported: {supported}"
        raise AudioTranscodingError(msg)

    if not is_ffmpeg_available(ffmpeg_bin):
        msg = f"ffmpeg binary '{ffmpeg_bin}' was not found on PATH."
        raise AudioTranscodingError(msg)

    # Build ffmpeg command line arguments
    # -vn: strip video/cover streams to keep payload purely audio
    cmd: list[str] = [
        ffmpeg_bin,
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(input_path),
        "-vn",
    ]

    if fmt in {"opus", "ogg"}:
        cmd.extend(["-c:a", "libopus", "-b:a", f"{bitrate_kbps}k", "-f", "ogg", "pipe:1"])
    elif fmt == "mp3":
        cmd.extend(["-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", "-f", "mp3", "pipe:1"])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
    except OSError as exc:
        msg = f"Failed to execute ffmpeg for {input_path.name}: {exc}"
        raise AudioTranscodingError(msg) from exc

    if process.returncode != 0:
        err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
        msg = f"ffmpeg transcoding failed for {input_path.name} (exit code {process.returncode}): {err_msg}"
        raise AudioTranscodingError(msg)

    if not stdout_bytes:
        msg = f"ffmpeg produced empty audio stream for {input_path.name}"
        raise AudioTranscodingError(msg)

    logger.info(
        "Transcoded %s -> %s (%d kbps): produced %d bytes in RAM",
        input_path.name,
        fmt,
        bitrate_kbps,
        len(stdout_bytes),
    )
    return stdout_bytes


default_transcode_cache: AudioTranscodeCache = AudioTranscodeCache()
