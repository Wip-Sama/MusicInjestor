"""Filesystem organizer and sidecar lyrics writer for finalized music library assets."""

import asyncio
import logging
import shutil
from pathlib import Path

import mutagen
from pathvalidate import sanitize_filename

from src.core.models import LyricsData, TrackMetadata

logger = logging.getLogger(__name__)


def _write_enriched_tags_sync(
    audio_path: Path,
    metadata: TrackMetadata,
    lyrics: LyricsData | None,
) -> bool:
    """Write canonical metadata and embedded lyrics into `audio_path` via `mutagen`."""
    try:
        easy_audio = mutagen.File(audio_path, easy=True)
    except Exception as exc:
        logger.debug("Skipping tag write for %s (unrecognized stream: %s)", audio_path.name, exc)
        return False

    if easy_audio is None:
        return False

    try:
        if getattr(easy_audio, "tags", None) is None and hasattr(easy_audio, "add_tags"):
            easy_audio.add_tags()

        tag_pairs: tuple[tuple[str, str | None], ...] = (
            ("title", metadata.title),
            ("artist", metadata.artist),
            ("album", metadata.album),
            ("albumartist", metadata.effective_album_artist),
            ("date", str(metadata.year) if metadata.year is not None else None),
            ("tracknumber", str(metadata.track_number) if metadata.track_number is not None else None),
            ("discnumber", str(metadata.disc_number) if metadata.disc_number is not None else None),
            ("genre", metadata.genre),
            ("musicbrainz_trackid", metadata.recording_mbid),
            ("musicbrainz_albumid", metadata.release_mbid),
        )
        for key, value in tag_pairs:
            if value:
                try:
                    easy_audio[key] = [value]
                except Exception:
                    continue
        easy_audio.save()
    except Exception as exc:
        logger.debug("Could not save easy tags on %s: %s", audio_path.name, exc)
        return False

    if lyrics is not None and lyrics.has_any:
        lyrics_text = lyrics.synced_lyrics or lyrics.plain_lyrics
        if lyrics_text:
            try:
                raw_audio = mutagen.File(audio_path)
                if raw_audio is not None and getattr(raw_audio, "tags", None) is not None:
                    suffix = audio_path.suffix.lower()
                    if suffix in {".flac", ".ogg", ".opus"}:
                        raw_audio["LYRICS"] = [lyrics_text]
                        raw_audio.save()
                    elif suffix in {".m4a", ".alac"}:
                        raw_audio["\xa9lyr"] = [lyrics_text]
                        raw_audio.save()
            except Exception as exc:
                logger.debug("Could not embed lyrics tag on %s: %s", audio_path.name, exc)

    return True


class LibraryOrganizer:
    """Formats sanitized target paths, writes enriched tags & `.lrc` sidecars, and files audio tracks."""

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
        """Copy the audio file to its canonical library location, write tags, and write synced `.lrc`."""
        target_audio_path = self.build_target_audio_path(metadata, source_audio_path.suffix)
        target_audio_path.parent.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(shutil.copy2, source_audio_path, target_audio_path)
        await asyncio.to_thread(_write_enriched_tags_sync, target_audio_path, metadata, lyrics)
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
