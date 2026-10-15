"""Unit tests for on-the-fly audio transcoding and the in-RAM transcode cache."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.core.exceptions import AudioTranscodingError
from src.core.transcoder import (
    AudioTranscodeCache,
    async_transcode_audio,
    default_transcode_cache,
    is_ffmpeg_available,
)


def test_is_ffmpeg_available_mock() -> None:
    """Verify is_ffmpeg_available checks shutil.which correctly."""
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        assert is_ffmpeg_available() is True

    with patch("shutil.which", return_value=None):
        assert is_ffmpeg_available() is False


@pytest.mark.anyio
async def test_audio_transcode_cache_lru_and_eviction(tmp_path: Path) -> None:
    """Verify AudioTranscodeCache stores buffers in RAM and evicts oldest entries."""
    cache = AudioTranscodeCache(max_items=2, max_bytes=100)

    f1 = tmp_path / "song1.flac"
    f2 = tmp_path / "song2.flac"
    f3 = tmp_path / "song3.flac"
    f1.write_bytes(b"FLAC1")
    f2.write_bytes(b"FLAC2")
    f3.write_bytes(b"FLAC3")

    # Put song 1 and 2
    await cache.put(f1, "opus", 128, b"OPUS_DATA_1")
    await cache.put(f2, "opus", 128, b"OPUS_DATA_2")
    assert cache.current_items == 2
    assert cache.current_bytes == len(b"OPUS_DATA_1") + len(b"OPUS_DATA_2")

    # Hit song 1
    hit1 = await cache.get(f1, "opus", 128)
    assert hit1 == b"OPUS_DATA_1"

    # Put song 3 -> should evict song 2 (least recently used)
    await cache.put(f3, "opus", 128, b"OPUS_DATA_3")
    assert cache.current_items == 2
    assert await cache.get(f1, "opus", 128) == b"OPUS_DATA_1"
    assert await cache.get(f2, "opus", 128) is None
    assert await cache.get(f3, "opus", 128) == b"OPUS_DATA_3"

    # Clear
    await cache.clear()
    assert cache.current_items == 0
    assert cache.current_bytes == 0


@pytest.mark.anyio
async def test_audio_transcode_cache_bytes_limit(tmp_path: Path) -> None:
    """Verify cache respects max_bytes limit."""
    cache = AudioTranscodeCache(max_items=10, max_bytes=1024 * 1024)
    file_path = tmp_path / "test.flac"
    file_path.write_bytes(b"FLAC")

    await cache.put(file_path, "opus", 128, b"X" * 1000)
    assert cache.current_bytes == 1000
    # Overwrite same key
    await cache.put(file_path, "opus", 128, b"Y" * 500)
    assert cache.current_bytes == 500


@pytest.mark.anyio
async def test_async_transcode_audio_validations(tmp_path: Path) -> None:
    """Verify validation errors for missing file, bad format, and missing ffmpeg."""
    non_existent = tmp_path / "ghost.flac"
    with pytest.raises(AudioTranscodingError, match="source file does not exist"):
        await async_transcode_audio(non_existent)

    real_file = tmp_path / "song.flac"
    real_file.write_bytes(b"FLAC")

    with pytest.raises(AudioTranscodingError, match="Unsupported target transcode format"):
        await async_transcode_audio(real_file, target_format="wav")

    with (
        patch("src.core.transcoder.is_ffmpeg_available", return_value=False),
        pytest.raises(AudioTranscodingError, match="ffmpeg binary 'ffmpeg' was not found"),
    ):
        await async_transcode_audio(real_file, target_format="opus")


@pytest.mark.anyio
async def test_async_transcode_audio_execution_flow(tmp_path: Path) -> None:
    """Verify subprocess execution for opus and mp3 transcoding."""
    real_file = tmp_path / "song.flac"
    real_file.write_bytes(b"FLAC_DATA")

    mock_process = AsyncMock()
    mock_process.returncode = 0
    mock_process.communicate.return_value = (b"OGG_OPUS_STREAM", b"")

    with (
        patch("src.core.transcoder.is_ffmpeg_available", return_value=True),
        patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec,
    ):
        # 1. Opus transcode
        out_opus = await async_transcode_audio(real_file, target_format="opus", bitrate_kbps=128)
        assert out_opus == b"OGG_OPUS_STREAM"
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        assert "libopus" in call_args
        assert "128k" in call_args

        # 2. MP3 transcode
        mock_process.communicate.return_value = (b"MP3_STREAM", b"")
        out_mp3 = await async_transcode_audio(real_file, target_format="mp3", bitrate_kbps=192)
        assert out_mp3 == b"MP3_STREAM"


@pytest.mark.anyio
async def test_async_transcode_audio_failure_cases(tmp_path: Path) -> None:
    """Verify handling when ffmpeg exits with error or empty stdout."""
    real_file = tmp_path / "corrupt.flac"
    real_file.write_bytes(b"GARBAGE")

    mock_process = AsyncMock()
    mock_process.returncode = 1
    mock_process.communicate.return_value = (b"", b"Invalid data found when processing input")

    with (
        patch("src.core.transcoder.is_ffmpeg_available", return_value=True),
        patch("asyncio.create_subprocess_exec", return_value=mock_process),
        pytest.raises(AudioTranscodingError, match="ffmpeg transcoding failed"),
    ):
        await async_transcode_audio(real_file, target_format="opus")

    # Empty stdout
    mock_process.returncode = 0
    mock_process.communicate.return_value = (b"", b"")
    with (
        patch("src.core.transcoder.is_ffmpeg_available", return_value=True),
        patch("asyncio.create_subprocess_exec", return_value=mock_process),
        pytest.raises(AudioTranscodingError, match="empty audio stream"),
    ):
        await async_transcode_audio(real_file, target_format="opus")

    # OSError on exec
    with (
        patch("src.core.transcoder.is_ffmpeg_available", return_value=True),
        patch("asyncio.create_subprocess_exec", side_effect=OSError("Exec failed")),
        pytest.raises(AudioTranscodingError, match="Failed to execute ffmpeg"),
    ):
        await async_transcode_audio(real_file, target_format="opus")


def test_default_transcode_cache_instance() -> None:
    """Verify global default transcode cache instance exists."""
    assert isinstance(default_transcode_cache, AudioTranscodeCache)
