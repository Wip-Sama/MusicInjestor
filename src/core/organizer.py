"""Filesystem organizer and sidecar lyrics writer for finalized music library assets."""

import asyncio
import logging
import shutil
from pathlib import Path

from pathvalidate import sanitize_filename

from src.core.models import LyricsData, TrackMetadata

logger = logging.getLogger(__name__)


class LibraryOrganizer:
    """Formats sanitized target paths, writes `.lrc` sidecars, and moves finalized audio files."""

    def __init__(self, library_root: Path) -> None:
        self._library_root = library_root

    def build_target_audio_path(self, metadata: TrackMetadata, extension: str) -> Path:
        """Construct `/music/{Album Artist}/{Album} ({Year})/{Track:02d} - {Title}.{ext}`."""
        normalized_ext = extension if extension.startswith(".") else f".{extension}"
        safe_artist = sanitize_filename(
            metadata.effective_album_artist,
            replacement_text="_",
        ).strip()
        album_label = f"{metadata.album} ({metadata.year})" if metadata.year is not None else metadata.album
        safe_album = sanitize_filename(album_label, replacement_text="_").strip()
        safe_title = sanitize_filename(metadata.title, replacement_text="_").strip()

        track_num = metadata.track_number if metadata.track_number is not None else 1
        filename = f"{track_num:02d} - {safe_title}{normalized_ext.lower()}"
        return self._library_root / safe_artist / safe_album / filename

    async def finalize_track(
        self,
        source_audio_path: Path,
        metadata: TrackMetadata,
        lyrics: LyricsData | None = None,
    ) -> Path:
        """Move the audio file to its canonical library location and write synced `.lrc` if present."""
        target_audio_path = self.build_target_audio_path(metadata, source_audio_path.suffix)
        target_audio_path.parent.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(shutil.copy2, source_audio_path, target_audio_path)
        logger.info("Finalized audio track at %s", target_audio_path)

        if lyrics is not None and lyrics.has_synced and lyrics.synced_lyrics is not None:
            lrc_path = target_audio_path.with_suffix(".lrc")
            await asyncio.to_thread(
                lrc_path.write_text,
                lyrics.synced_lyrics,
                "utf-8",
            )
            logger.info("Wrote synchronized sidecar lyrics to %s", lrc_path)

        return target_audio_path
