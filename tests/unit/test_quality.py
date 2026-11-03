"""Unit tests for src/core/quality.py (audio quality inspection, summary badges, and upgrade evaluation)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.quality import (
    AudioQuality,
    inspect_audio_quality,
    is_audio_quality_upgrade,
)


def test_audio_quality_summary_lossless():
    """Verify summary badge formatting for lossless formats."""
    q_flac_hi_res = AudioQuality(
        format="flac",
        is_lossless=True,
        bitrate_kbps=1500,
        sample_rate=96000,
        bits_per_sample=24,
    )
    assert q_flac_hi_res.summary == "FLAC (Lossless, 24-bit/96kHz, 1500 kbps)"

    q_flac_cd = AudioQuality(
        format="flac",
        is_lossless=True,
        bitrate_kbps=900,
        sample_rate=44100,
        bits_per_sample=16,
    )
    assert q_flac_cd.summary == "FLAC (Lossless, 16-bit/44.1kHz, 900 kbps)"


def test_audio_quality_summary_lossy():
    """Verify summary badge formatting for lossy formats."""
    q_mp3_320 = AudioQuality(
        format="mp3",
        is_lossless=False,
        bitrate_kbps=320,
        sample_rate=44100,
    )
    assert q_mp3_320.summary == "MP3 (320 kbps)"

    q_opus_128 = AudioQuality(
        format="opus",
        is_lossless=False,
        bitrate_kbps=128,
        sample_rate=48000,
    )
    assert q_opus_128.summary == "OPUS (128 kbps)"


def test_is_audio_quality_upgrade_lossless_beats_lossy():
    """Rule 1: Lossless format always beats lossy format."""
    mp3 = AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=320)
    flac = AudioQuality(format="flac", is_lossless=True, bitrate_kbps=800)

    assert is_audio_quality_upgrade(candidate=flac, current=mp3) is True


def test_is_audio_quality_upgrade_lossy_never_upgrades_lossless():
    """Rule 2: Lossy format never upgrades a lossless format."""
    mp3 = AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=320)
    flac = AudioQuality(format="flac", is_lossless=True, bitrate_kbps=800)

    assert is_audio_quality_upgrade(candidate=mp3, current=flac) is False


def test_is_audio_quality_upgrade_lossless_bit_depth_and_sample_rate():
    """Rule 3: Lossless upgrades when candidate has higher bit depth or sample rate."""
    cd_flac = AudioQuality(
        format="flac",
        is_lossless=True,
        bitrate_kbps=800,
        sample_rate=44100,
        bits_per_sample=16,
    )
    hi_res_bit_depth = AudioQuality(
        format="flac",
        is_lossless=True,
        bitrate_kbps=850,
        sample_rate=44100,
        bits_per_sample=24,
    )
    assert is_audio_quality_upgrade(candidate=hi_res_bit_depth, current=cd_flac) is True
    assert is_audio_quality_upgrade(candidate=cd_flac, current=hi_res_bit_depth) is False

    hi_res_sample_rate = AudioQuality(
        format="flac",
        is_lossless=True,
        bitrate_kbps=850,
        sample_rate=96000,
        bits_per_sample=16,
    )
    assert is_audio_quality_upgrade(candidate=hi_res_sample_rate, current=cd_flac) is True


def test_is_audio_quality_upgrade_lossless_bitrate():
    """Rule 3: Lossless upgrades when candidate has >= 100 kbps higher bitrate."""
    low_flac = AudioQuality(
        format="flac",
        is_lossless=True,
        bitrate_kbps=700,
        sample_rate=44100,
        bits_per_sample=16,
    )
    high_flac = AudioQuality(
        format="flac",
        is_lossless=True,
        bitrate_kbps=850,
        sample_rate=44100,
        bits_per_sample=16,
    )
    assert is_audio_quality_upgrade(candidate=high_flac, current=low_flac) is True

    same_flac = AudioQuality(
        format="flac",
        is_lossless=True,
        bitrate_kbps=750,
        sample_rate=44100,
        bits_per_sample=16,
    )
    assert is_audio_quality_upgrade(candidate=same_flac, current=low_flac) is False


def test_is_audio_quality_upgrade_lossy_bitrate():
    """Rule 4: Lossy upgrades when candidate has >= 32 kbps higher bitrate, or lifts low quality."""
    mp3_128 = AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=128)
    mp3_192 = AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=192)
    mp3_320 = AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=320)

    # 320 over 128/192
    assert is_audio_quality_upgrade(candidate=mp3_320, current=mp3_128) is True
    assert is_audio_quality_upgrade(candidate=mp3_320, current=mp3_192) is True

    # 128 over 320 is downgrade
    assert is_audio_quality_upgrade(candidate=mp3_128, current=mp3_320) is False

    # Marginal difference (< 32 kbps)
    mp3_310 = AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=310)
    assert is_audio_quality_upgrade(candidate=mp3_320, current=mp3_310) is False


def test_inspect_audio_quality_with_mocked_mutagen(tmp_path: Path):
    """Verify inspect_audio_quality extracts mutagen attributes and file size."""
    test_audio = tmp_path / "song.flac"
    test_audio.write_bytes(b"dummy audio content" * 100)

    mock_mutagen_file = MagicMock()
    mock_mutagen_file.info.length = 180.0
    mock_mutagen_file.info.sample_rate = 96000
    mock_mutagen_file.info.bits_per_sample = 24
    mock_mutagen_file.info.bitrate = 1440000

    with patch("src.core.quality.mutagen.File", return_value=mock_mutagen_file):
        quality = inspect_audio_quality(test_audio)

    assert quality.format == "flac"
    assert quality.is_lossless is True
    assert quality.bits_per_sample == 24
    assert quality.sample_rate == 96000
    assert quality.bitrate_kbps == 1440
    assert quality.duration_seconds == 180.0
    assert quality.file_size_bytes == test_audio.stat().st_size
