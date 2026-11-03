"""Pure domain logic for audio quality inspection, scoring, and upgrade evaluation."""

import logging
from dataclasses import dataclass
from pathlib import Path

import mutagen

logger = logging.getLogger(__name__)

LOSSLESS_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".wav", ".alac", ".aiff", ".aif", ".wv", ".ape", ".dsf", ".dff"}
)


@dataclass(frozen=True, slots=True)
class AudioQuality:
    """Technical audio quality attributes of an audio file."""

    format: str
    is_lossless: bool
    bitrate_kbps: int
    sample_rate: int = 44100
    bits_per_sample: int | None = None
    file_size_bytes: int = 0
    duration_seconds: float = 0.0

    @property
    def summary(self) -> str:
        """User-facing audio quality badge (e.g. 'FLAC (Lossless 24-bit/96kHz)' or 'MP3 (320 kbps)')."""
        fmt = self.format.upper()
        if self.is_lossless:
            details: list[str] = ["Lossless"]
            if self.bits_per_sample and self.sample_rate:
                khz = round(self.sample_rate / 1000, 1)
                khz_str = f"{int(khz)}" if khz.is_integer() else f"{khz}"
                details.append(f"{self.bits_per_sample}-bit/{khz_str}kHz")
            elif self.sample_rate:
                khz = round(self.sample_rate / 1000, 1)
                khz_str = f"{int(khz)}" if khz.is_integer() else f"{khz}"
                details.append(f"{khz_str}kHz")
            if self.bitrate_kbps > 0:
                details.append(f"{self.bitrate_kbps} kbps")
            return f"{fmt} ({', '.join(details)})"

        br = f"{self.bitrate_kbps} kbps" if self.bitrate_kbps > 0 else "Lossy"
        return f"{fmt} ({br})"


def inspect_audio_quality(
    audio_path: Path,
    known_duration: float | None = None,
) -> AudioQuality:
    """Inspect audio file attributes using mutagen and return an AudioQuality snapshot."""
    suffix = audio_path.suffix.lower()
    raw_fmt = suffix.removeprefix(".") or "audio"
    is_lossless = suffix in LOSSLESS_EXTENSIONS

    file_size = 0
    try:
        if audio_path.is_file():
            file_size = audio_path.stat().st_size
    except OSError:
        pass

    duration = known_duration or 0.0
    bitrate_kbps = 0
    sample_rate = 44100
    bits_per_sample: int | None = None

    try:
        audio = mutagen.File(audio_path)
        if audio is not None and getattr(audio, "info", None) is not None:
            info = audio.info
            length = getattr(info, "length", None)
            if isinstance(length, (int, float)) and length > 0:
                duration = float(length)

            sr = getattr(info, "sample_rate", None)
            if isinstance(sr, int) and sr > 0:
                sample_rate = sr

            bps = getattr(info, "bits_per_sample", None)
            if isinstance(bps, int) and bps > 0:
                bits_per_sample = bps
                is_lossless = True

            br = getattr(info, "bitrate", None)
            if isinstance(br, (int, float)) and br > 0:
                bitrate_kbps = int(round(br / 1000))
    except Exception as exc:
        logger.debug("Mutagen inspection failed for %s: %s", audio_path.name, exc)

    if bitrate_kbps <= 0 and duration > 0 and file_size > 0:
        bitrate_kbps = int(round((file_size * 8) / (duration * 1000)))

    return AudioQuality(
        format=raw_fmt,
        is_lossless=is_lossless,
        bitrate_kbps=bitrate_kbps,
        sample_rate=sample_rate,
        bits_per_sample=bits_per_sample,
        file_size_bytes=file_size,
        duration_seconds=duration,
    )


def is_audio_quality_upgrade(
    candidate: AudioQuality,
    current: AudioQuality,
) -> bool:
    """Determine whether `candidate` represents an audio quality upgrade over `current`.

    Rules:
    1. Lossless candidate vs Lossy current -> definite upgrade (e.g. FLAC replacing MP3).
    2. Lossy candidate vs Lossless current -> NOT an upgrade (downgrade).
    3. Both Lossless:
       - Higher bit depth (e.g. 24-bit vs 16-bit) -> upgrade.
       - Higher sample rate (e.g. 96kHz vs 44.1kHz, >2kHz diff) -> upgrade.
       - Significantly higher bitrate (>= +100 kbps) -> upgrade.
    4. Both Lossy:
       - Higher bitrate by at least 32 kbps (e.g. 320 kbps vs 256/192/128 kbps) -> upgrade.
       - Current is very low quality (<160 kbps) and candidate is >= 192 kbps -> upgrade.
    """
    # Rule 1: Lossless beats Lossy
    if candidate.is_lossless and not current.is_lossless:
        return True

    # Rule 2: Lossy cannot upgrade Lossless
    if not candidate.is_lossless and current.is_lossless:
        return False

    # Rule 3: Both are Lossless
    if candidate.is_lossless and current.is_lossless:
        # Check bit depth (e.g. 24-bit vs 16-bit)
        if (
            candidate.bits_per_sample is not None
            and current.bits_per_sample is not None
            and candidate.bits_per_sample > current.bits_per_sample
        ):
            return True

        # Check sample rate (e.g. 96000 vs 44100)
        if candidate.sample_rate > current.sample_rate + 2000:
            return True

        # Check bitrate if both are lossless
        return candidate.bitrate_kbps >= current.bitrate_kbps + 100

    # Rule 4: Both are Lossy
    if candidate.bitrate_kbps >= current.bitrate_kbps + 32:
        return True

    return bool(current.bitrate_kbps < 160 and candidate.bitrate_kbps >= 192)
