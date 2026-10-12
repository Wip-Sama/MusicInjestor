"""Filesystem organizer, tag writer, cover art extractor/fetcher, and sidecar lyrics writer."""

import asyncio
import base64
import hashlib
import logging
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.flac import Picture
from mutagen.id3 import APIC, ID3, SYLT, TXXX, USLT, Encoding
from mutagen.mp4 import MP4Cover
from pathvalidate import sanitize_filename

from src.core.extractor import SUPPORTED_AUDIO_EXTENSIONS, find_best_cover_image
from src.core.metadata import _read_mutagen_metadata_sync, parse_filename_metadata
from src.core.models import (
    LibraryRescanReport,
    LibraryTrackRecord,
    LyricsData,
    TrackMetadata,
)
from src.core.ports import GovernanceRepositoryPort

logger = logging.getLogger(__name__)

_LRC_TIMESTAMP_RE = re.compile(r"\[(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")


def parse_lrc_timestamps(synced_lyrics: str) -> list[tuple[str, int]]:
    """Parse LRC timestamped lyrics into `(lyric_line, timestamp_ms)` pairs for ID3 `SYLT` frames."""
    entries: list[tuple[int, str]] = []
    for raw_line in synced_lyrics.splitlines():
        matches = list(_LRC_TIMESTAMP_RE.finditer(raw_line))
        if not matches:
            continue
        lyric_text = _LRC_TIMESTAMP_RE.sub("", raw_line).strip()
        for match in matches:
            hours = int(match.group(1)) if match.group(1) is not None else 0
            minutes = int(match.group(2))
            seconds = int(match.group(3))
            frac_raw = match.group(4) or "0"
            if len(frac_raw) == 1:
                millis = int(frac_raw) * 100
            elif len(frac_raw) == 2:
                millis = int(frac_raw) * 10
            else:
                millis = int(frac_raw[:3])
            total_ms = ((hours * 3600) + (minutes * 60) + seconds) * 1000 + millis
            entries.append((total_ms, lyric_text))

    entries.sort(key=lambda item: item[0])
    return [(text, ts_ms) for ts_ms, text in entries]


def _write_id3_lyrics_frames(id3_tags: object, primary_lyrics: str, lyrics: LyricsData) -> bool:
    """Attach `USLT`, `TXXX`, and `SYLT` frames to an ID3 tag container."""
    add_fn = getattr(id3_tags, "add", None)
    if not callable(add_fn):
        return False
    add_fn(USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=primary_lyrics))
    add_fn(TXXX(encoding=Encoding.UTF8, desc="LYRICS", text=[primary_lyrics]))
    if lyrics.has_synced and lyrics.synced_lyrics:
        add_fn(
            TXXX(
                encoding=Encoding.UTF8,
                desc="SYNCEDLYRICS",
                text=[lyrics.synced_lyrics],
            )
        )
        sylt_frames = parse_lrc_timestamps(lyrics.synced_lyrics)
        if sylt_frames:
            add_fn(
                SYLT(
                    encoding=Encoding.UTF8,
                    lang="eng",
                    format=2,
                    type=1,
                    desc="Synced",
                    text=sylt_frames,
                )
            )
    return True


def _extract_existing_lyrics_sync(audio_path: Path) -> LyricsData | None:
    """Extract pre-existing embedded lyrics from `audio_path` if external providers returned none."""
    if not audio_path.is_file():
        return None
    try:
        raw_audio = mutagen.File(audio_path)
    except Exception:
        return None
    if raw_audio is None:
        return None

    tags = getattr(raw_audio, "tags", None)
    if tags is None:
        return None

    synced: str | None = None
    plain: str | None = None

    getter = getattr(tags, "get", None)
    if callable(getter):
        for key in ("SYNCEDLYRICS", "syncedlyrics"):
            try:
                val = getter(key)
            except Exception:
                continue
            if isinstance(val, list) and val and str(val[0]).strip():
                synced = str(val[0]).strip()
                break
        for key in ("LYRICS", "lyrics", "UNSYNCEDLYRICS", "unsyncedlyrics", "\xa9lyr"):
            try:
                val = getter(key)
            except Exception:
                continue
            if isinstance(val, list) and val and str(val[0]).strip():
                text = str(val[0]).strip()
                if _LRC_TIMESTAMP_RE.search(text) and not synced:
                    synced = text
                elif not plain:
                    plain = text

    getall_fn = getattr(tags, "getall", None)
    if callable(getall_fn) and not (synced or plain):
        try:
            uslt_frames = getall_fn("USLT")
            if isinstance(uslt_frames, list) and uslt_frames:
                uslt_text = str(getattr(uslt_frames[0], "text", "") or "").strip()
                if uslt_text:
                    if _LRC_TIMESTAMP_RE.search(uslt_text):
                        synced = uslt_text
                    else:
                        plain = uslt_text
        except Exception:
            pass

    if not synced and not plain:
        return None
    return LyricsData(synced_lyrics=synced, plain_lyrics=plain)


def _embed_lyrics_into_audio_sync(audio_path: Path, lyrics: LyricsData) -> None:
    """Embed synchronized and/or plain lyrics directly into `audio_path` metadata tags."""
    primary_lyrics = lyrics.synced_lyrics or lyrics.plain_lyrics
    if not primary_lyrics:
        return

    suffix = audio_path.suffix.lower()
    raw_audio = None
    try:
        raw_audio = mutagen.File(audio_path)
    except Exception as exc:
        logger.debug("Could not open %s via mutagen.File for lyrics: %s", audio_path.name, exc)

    if raw_audio is None:
        if suffix == ".mp3":
            try:
                id3_tags = ID3(audio_path)
                if _write_id3_lyrics_frames(id3_tags, primary_lyrics, lyrics):
                    id3_tags.save(audio_path)
            except Exception as exc:
                logger.debug("Could not embed ID3 lyrics on %s: %s", audio_path.name, exc)
        return

    try:
        if getattr(raw_audio, "tags", None) is None and hasattr(raw_audio, "add_tags"):
            raw_audio.add_tags()
        tags = getattr(raw_audio, "tags", None)
        if tags is None:
            return

        if suffix in {".flac", ".ogg", ".opus"}:
            raw_audio["LYRICS"] = [primary_lyrics]
            if lyrics.has_synced and lyrics.synced_lyrics:
                raw_audio["SYNCEDLYRICS"] = [lyrics.synced_lyrics]
            raw_audio["UNSYNCEDLYRICS"] = [lyrics.plain_lyrics or primary_lyrics]
            raw_audio.save()
            return

        if suffix in {".m4a", ".alac"}:
            raw_audio["\xa9lyr"] = [primary_lyrics]
            raw_audio.save()
            return

        if (suffix in {".mp3", ".wav", ".dsf"} or hasattr(tags, "add")) and _write_id3_lyrics_frames(
            tags, primary_lyrics, lyrics
        ):
            raw_audio.save()
            return

        raw_audio["Lyrics"] = [primary_lyrics]
        raw_audio.save()
    except Exception as exc:
        logger.debug("Could not embed lyrics tag on %s: %s", audio_path.name, exc)


def _write_enriched_tags_sync(
    audio_path: Path,
    metadata: TrackMetadata,
    lyrics: LyricsData | None = None,
) -> bool:
    """Write canonical metadata and embedded lyrics into `audio_path` via `mutagen`."""
    easy_audio = None
    try:
        easy_audio = mutagen.File(audio_path, easy=True)
    except Exception as exc:
        logger.debug("mutagen.File(easy=True) failed for %s: %s", audio_path.name, exc)

    if easy_audio is None and audio_path.suffix.lower() == ".mp3":
        try:
            easy_audio = EasyID3(audio_path)
        except Exception as exc:
            logger.debug("EasyID3 fallback failed for %s: %s", audio_path.name, exc)

    if easy_audio is None:
        return False

    try:
        add_tags_fn = getattr(easy_audio, "add_tags", None)
        if getattr(easy_audio, "tags", None) is None and callable(add_tags_fn):
            add_tags_fn()

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
        if isinstance(easy_audio, EasyID3):
            easy_audio.save(audio_path)
        else:
            easy_audio.save()
    except Exception as exc:
        logger.debug("Could not save easy tags on %s: %s", audio_path.name, exc)
        return False

    if lyrics is not None and lyrics.has_any:
        _embed_lyrics_into_audio_sync(audio_path, lyrics)

    return True


def _extract_embedded_cover_bytes_sync(audio_path: Path) -> bytes | None:
    """Extract embedded cover art bytes (`Picture`, `APIC`, `covr`, or `metadata_block_picture`)."""
    if not audio_path.is_file():
        return None
    try:
        raw_audio = mutagen.File(audio_path)
    except Exception:
        return None
    if raw_audio is None:
        return None

    # 1. FLAC / Picture list
    pictures = getattr(raw_audio, "pictures", None)
    if isinstance(pictures, list) and pictures:
        for pic in pictures:
            data = getattr(pic, "data", None)
            if isinstance(data, (bytes, bytearray)) and data and getattr(pic, "type", 0) == 3:
                return bytes(data)
        first_data = getattr(pictures[0], "data", None)
        if isinstance(first_data, (bytes, bytearray)) and first_data:
            return bytes(first_data)

    tags = getattr(raw_audio, "tags", None)
    if tags is None:
        return None

    # 2. ID3 APIC frames
    getall_fn = getattr(tags, "getall", None)
    if callable(getall_fn):
        try:
            apic_frames = getall_fn("APIC")
            if isinstance(apic_frames, list):
                for frame in apic_frames:
                    frame_data = getattr(frame, "data", None)
                    if isinstance(frame_data, (bytes, bytearray)) and frame_data:
                        return bytes(frame_data)
        except Exception:
            pass

    if isinstance(tags, dict):
        for key, val in tags.items():
            if str(key).startswith("APIC"):
                frame_data = getattr(val, "data", None)
                if isinstance(frame_data, (bytes, bytearray)) and frame_data:
                    return bytes(frame_data)
                if isinstance(frame, APIC) if (frame := val) else False:
                    return bytes(frame.data)

    # 3. MP4 / M4A `covr` atom
    getter = getattr(tags, "get", None)
    if callable(getter):
        try:
            covr = getter("covr")
            if isinstance(covr, list) and covr and isinstance(covr[0], (bytes, bytearray)):
                return bytes(covr[0])
        except Exception:
            pass

        # 4. Ogg / Opus `metadata_block_picture`
        try:
            b64_pics = getter("metadata_block_picture")
            if isinstance(b64_pics, list) and b64_pics:
                raw_pic = Picture(base64.b64decode(str(b64_pics[0])))
                if raw_pic.data:
                    return bytes(raw_pic.data)
        except Exception:
            pass

    return None


class LibraryOrganizer:
    """Formats sanitized target paths, writes tags, extracts/fetches `cover.jpg`, and writes `.lrc`."""

    def __init__(
        self,
        library_root: Path,
        *,
        cover_art_base_url: str = "https://coverartarchive.org",
        transport: httpx.AsyncBaseTransport | None = None,
        enable_network: bool = False,
    ) -> None:
        self._library_root = library_root
        self._cover_art_base_url = cover_art_base_url.rstrip("/")
        self._transport = transport
        self._enable_network = enable_network

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

    async def _ensure_album_cover_art(
        self,
        source_audio_path: Path,
        target_album_dir: Path,
        metadata: TrackMetadata,
    ) -> Path | None:
        """Ensure `cover.jpg` exists in `target_album_dir` via archive image, embedded tags, or CAA."""
        cover_path = target_album_dir / "cover.jpg"
        if cover_path.is_file():
            return cover_path

        # 1. Check staged/sibling image files unpacked alongside the audio file
        staged_image = find_best_cover_image(source_audio_path.parent, recursive=False)
        if staged_image is not None and staged_image.is_file():
            await asyncio.to_thread(shutil.copy2, staged_image, cover_path)
            logger.info("Copied staged album cover art from %s to %s", staged_image.name, cover_path)
            return cover_path

        # 2. Extract embedded Picture / APIC / covr from the audio file
        embedded_bytes = await asyncio.to_thread(_extract_embedded_cover_bytes_sync, source_audio_path)
        if embedded_bytes:
            await asyncio.to_thread(cover_path.write_bytes, embedded_bytes)
            logger.info("Extracted embedded cover art from %s to %s", source_audio_path.name, cover_path)
            return cover_path

        # 3. Fetch missing cover art from MusicBrainz Cover Art Archive
        rel_mbid = (metadata.release_mbid or "").strip()
        if rel_mbid and (self._enable_network or self._transport is not None):
            if rel_mbid.startswith("release-group:"):
                rgid = rel_mbid.split(":", maxsplit=1)[1]
                url = f"{self._cover_art_base_url}/release-group/{rgid}/front"
            else:
                url = f"{self._cover_art_base_url}/release/{rel_mbid}/front"
            try:
                async with httpx.AsyncClient(
                    transport=self._transport,
                    follow_redirects=True,
                    timeout=10.0,
                ) as client:
                    response = await client.get(url)
                    if response.status_code == 200 and response.content:
                        await asyncio.to_thread(cover_path.write_bytes, response.content)
                        logger.info("Fetched cover art from Cover Art Archive (%s) to %s", url, cover_path)
                        return cover_path
            except Exception as exc:
                logger.debug("Cover Art Archive lookup failed for %s: %s", rel_mbid, exc)

        return None

    async def finalize_track(
        self,
        source_audio_path: Path,
        metadata: TrackMetadata,
        lyrics: LyricsData | None = None,
    ) -> Path:
        """Copy the audio file to its canonical library location, write tags, `cover.jpg`, and `.lrc`."""
        target_audio_path = self.build_target_audio_path(metadata, source_audio_path.suffix)
        target_audio_path.parent.mkdir(parents=True, exist_ok=True)

        effective_lyrics = lyrics
        if effective_lyrics is None or not effective_lyrics.has_any:
            effective_lyrics = await asyncio.to_thread(_extract_existing_lyrics_sync, source_audio_path)

        await asyncio.to_thread(shutil.copy2, source_audio_path, target_audio_path)
        await asyncio.to_thread(_write_enriched_tags_sync, target_audio_path, metadata, effective_lyrics)
        await self._ensure_album_cover_art(source_audio_path, target_audio_path.parent, metadata)
        logger.info("Finalized audio track at %s", target_audio_path)

        if effective_lyrics is not None and effective_lyrics.has_any:
            sidecar_text = effective_lyrics.synced_lyrics or effective_lyrics.plain_lyrics
            if sidecar_text:
                lrc_path = target_audio_path.with_suffix(".lrc")
                await asyncio.to_thread(
                    lrc_path.write_text,
                    sidecar_text,
                    "utf-8",
                )
                logger.info("Wrote sidecar lyrics to %s", lrc_path)

        return target_audio_path

    async def apply_tag_correction(
        self,
        audio_path: Path,
        metadata: TrackMetadata | None = None,
        *,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        year: int | None = None,
    ) -> bool:
        """Rewrite canonical or partial metadata tags on an already-ingested audio file in the library."""
        if not audio_path.is_file():
            return False
        if metadata is not None:
            return await asyncio.to_thread(_write_enriched_tags_sync, audio_path, metadata, None)
        return await asyncio.to_thread(
            _apply_partial_tag_correction_sync,
            audio_path,
            title,
            artist,
            album,
            year,
        )

    async def read_track_lyrics(self, audio_path: Path) -> LyricsData | None:
        """Read lyrics from the `.lrc` sidecar file if present, falling back to embedded audio tags."""
        return await asyncio.to_thread(_read_sidecar_or_embedded_lyrics_sync, audio_path)

    async def update_track_lyrics(self, audio_path: Path, lyrics_text: str) -> LyricsData:
        """Embed `lyrics_text` into `audio_path` tags and write the `.lrc` sidecar file."""
        cleaned = lyrics_text.strip()
        is_synced = bool(_LRC_TIMESTAMP_RE.search(cleaned))
        lyrics_obj = LyricsData(
            synced_lyrics=cleaned if is_synced else None,
            plain_lyrics=None if is_synced else cleaned,
        )
        if audio_path.is_file() and cleaned:
            await asyncio.to_thread(_embed_lyrics_into_audio_sync, audio_path, lyrics_obj)
            lrc_path = audio_path.with_suffix(".lrc")
            await asyncio.to_thread(lrc_path.write_text, cleaned, "utf-8")
        return lyrics_obj

    async def update_track_cover_art(
        self,
        audio_path: Path,
        cover_bytes: bytes,
        mime_type: str = "image/jpeg",
    ) -> Path:
        """Write `cover.jpg` in the album directory and embed the image into `audio_path` tags."""
        cover_path = audio_path.parent / "cover.jpg"
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(cover_path.write_bytes, cover_bytes)
        if audio_path.is_file():
            await asyncio.to_thread(_embed_cover_art_into_audio_sync, audio_path, cover_bytes, mime_type)
        return cover_path

    async def relocate_and_retag_track(
        self,
        current_path: Path,
        metadata: TrackMetadata,
        lyrics: LyricsData | None = None,
    ) -> Path:
        """Rewrite tags, move `current_path` + `.lrc` + `cover.jpg` if path changed, and clean empty dirs."""
        target_audio_path = self.build_target_audio_path(metadata, current_path.suffix)
        target_audio_path.parent.mkdir(parents=True, exist_ok=True)

        effective_lyrics = lyrics
        if effective_lyrics is None or not effective_lyrics.has_any:
            effective_lyrics = await self.read_track_lyrics(current_path)

        same_file = False
        try:
            same_file = target_audio_path.resolve() == current_path.resolve()
        except Exception:
            same_file = str(target_audio_path) == str(current_path)

        if not same_file and current_path.is_file():
            await asyncio.to_thread(shutil.copy2, current_path, target_audio_path)
            old_cover = current_path.parent / "cover.jpg"
            new_cover = target_audio_path.parent / "cover.jpg"
            if old_cover.is_file() and not new_cover.is_file():
                await asyncio.to_thread(shutil.copy2, old_cover, new_cover)

        if target_audio_path.is_file():
            await asyncio.to_thread(_write_enriched_tags_sync, target_audio_path, metadata, effective_lyrics)
            await self._ensure_album_cover_art(target_audio_path, target_audio_path.parent, metadata)

        if effective_lyrics is not None and effective_lyrics.has_any:
            sidecar_text = effective_lyrics.synced_lyrics or effective_lyrics.plain_lyrics
            if sidecar_text:
                lrc_path = target_audio_path.with_suffix(".lrc")
                await asyncio.to_thread(lrc_path.write_text, sidecar_text, "utf-8")

        if not same_file:
            await asyncio.to_thread(_cleanup_old_track_location_sync, current_path, self._library_root)

        return target_audio_path

    async def export_song_bundle(self, record: LibraryTrackRecord) -> dict[str, object]:
        """Build a portable JSON-serializable metadata & lyrics dictionary for `record`."""
        lyrics = await self.read_track_lyrics(record.storage_path)
        lyrics_text = (lyrics.synced_lyrics or lyrics.plain_lyrics) if lyrics is not None else None
        cover_exists = (record.storage_path.parent / "cover.jpg").is_file()
        return {
            "track_id": record.id,
            "title": record.title,
            "artist": record.artist,
            "album": record.album,
            "year": record.year,
            "track_number": record.track_number,
            "duration_seconds": record.duration_seconds,
            "storage_path": str(record.storage_path),
            "has_synced_lyrics": bool(lyrics.has_synced) if lyrics is not None else record.has_synced_lyrics,
            "has_cover_art": cover_exists,
            "lyrics": lyrics_text,
        }

    async def scan_library_filesystem(
        self,
        existing_records: list[LibraryTrackRecord],
    ) -> LibraryRescanReport:
        """Inspect `self._library_root` and compute a dry-run diff against `existing_records`."""
        return await asyncio.to_thread(
            _scan_library_filesystem_sync,
            self._library_root,
            existing_records,
        )

    async def apply_rescan_report(
        self,
        report: LibraryRescanReport,
        repository: GovernanceRepositoryPort,
    ) -> LibraryRescanReport:
        """Persist `added_tracks`, `updated_tracks`, and `removed_tracks` from `report` into SQLite."""
        for added in report.added_tracks:
            await repository.upsert_scanned_library_track(added)
        for updated in report.updated_tracks:
            await repository.upsert_scanned_library_track(updated)
        for removed in report.removed_tracks:
            await repository.delete_library_track(removed.id)
        return report

    async def reconcile_library_with_repository(
        self,
        repository: GovernanceRepositoryPort,
    ) -> LibraryRescanReport:
        """Scan `self._library_root` and reconcile all added, modified, moved, or deleted files in SQLite."""
        existing = await repository.list_library_tracks(limit=10000)
        report = await self.scan_library_filesystem(existing)
        if report.has_changes:
            await self.apply_rescan_report(report, repository)
        return report


def _read_sidecar_or_embedded_lyrics_sync(audio_path: Path) -> LyricsData | None:
    """Read `.lrc` sidecar file if present, falling back to embedded lyrics in `audio_path`."""
    lrc_path = audio_path.with_suffix(".lrc")
    if lrc_path.is_file():
        try:
            text = lrc_path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                if _LRC_TIMESTAMP_RE.search(text):
                    return LyricsData(synced_lyrics=text)
                return LyricsData(plain_lyrics=text)
        except Exception:
            pass
    return _extract_existing_lyrics_sync(audio_path)


def _embed_cover_art_into_audio_sync(
    audio_path: Path,
    cover_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> bool:
    """Embed `cover_bytes` as front cover art into FLAC, MP3/ID3, MP4/M4A, or Ogg/Opus files."""
    if not audio_path.is_file() or not cover_bytes:
        return False

    suffix = audio_path.suffix.lower()
    try:
        if suffix == ".mp3":
            try:
                id3 = ID3(audio_path)
            except Exception:
                id3 = ID3()
            delall_fn = getattr(id3, "delall", None)
            if callable(delall_fn):
                delall_fn("APIC")
            id3.add(
                APIC(
                    encoding=Encoding.UTF8,
                    mime=mime_type,
                    type=3,
                    desc="Front Cover",
                    data=cover_bytes,
                )
            )
            id3.save(audio_path)
            return True

        raw_audio = mutagen.File(audio_path)
        if raw_audio is None:
            return False

        add_pic_fn = getattr(raw_audio, "add_picture", None)
        if callable(add_pic_fn):
            clear_pics_fn = getattr(raw_audio, "clear_pictures", None)
            if callable(clear_pics_fn):
                clear_pics_fn()
            pic = Picture()
            pic.type = 3
            pic.mime = mime_type
            pic.desc = "Front Cover"
            pic.data = cover_bytes
            add_pic_fn(pic)
            raw_audio.save()
            return True

        raw_tags = getattr(raw_audio, "tags", None)
        if raw_tags is not None:
            if suffix in {".m4a", ".mp4"}:
                img_fmt = MP4Cover.FORMAT_PNG if "png" in mime_type.lower() else MP4Cover.FORMAT_JPEG
                raw_tags["covr"] = [MP4Cover(cover_bytes, imageformat=img_fmt)]
                raw_audio.save()
                return True

            pic = Picture()
            pic.type = 3
            pic.mime = mime_type
            pic.desc = "Front Cover"
            pic.data = cover_bytes
            b64_pic = base64.b64encode(pic.write()).decode("ascii")
            raw_tags["metadata_block_picture"] = [b64_pic]
            raw_audio.save()
            return True
    except Exception as exc:
        logger.debug("Could not embed cover art into %s: %s", audio_path.name, exc)
    return False


def _cleanup_old_track_location_sync(old_audio_path: Path, library_root: Path) -> None:
    """Remove `old_audio_path`, its `.lrc` sidecar, and prune empty album/artist folders."""
    old_lrc = old_audio_path.with_suffix(".lrc")
    if old_lrc.exists():
        old_lrc.unlink(missing_ok=True)
    if old_audio_path.exists():
        old_audio_path.unlink(missing_ok=True)

    old_album_dir = old_audio_path.parent
    old_artist_dir = old_album_dir.parent
    try:
        lib_resolved = library_root.resolve()
        if old_album_dir.is_dir() and old_album_dir.resolve() != lib_resolved:
            remaining_audio = [
                p for p in old_album_dir.iterdir() if p.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
            ]
            if not remaining_audio:
                shutil.rmtree(old_album_dir, ignore_errors=True)
        if (
            old_artist_dir.is_dir()
            and old_artist_dir.resolve() != lib_resolved
            and not any(old_artist_dir.iterdir())
        ):
            old_artist_dir.rmdir()
    except Exception:
        pass


def _compute_file_sha256_fingerprint_sync(audio_path: Path) -> str:
    """Compute a deterministic `sha256:<hex>` fingerprint for an audio file on disk."""
    digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _normalize_path_key(path: Path) -> str:
    """Return a normalized lowercase path string for cross-platform path comparisons."""
    try:
        return str(path.resolve()).lower()
    except Exception:
        return str(path).replace("\\", "/").lower()


def _extract_tail_library_key(path: Path) -> str:
    """Return normalized lowercase `<artist>/<album>/<filename>` tail for mount-point migration matching."""
    parts = [part.lower() for part in str(path).replace("\\", "/").split("/") if part]
    return "/".join(parts[-3:]) if len(parts) >= 3 else "/".join(parts)


def _scan_library_filesystem_sync(
    library_root: Path,
    existing_records: list[LibraryTrackRecord],
) -> LibraryRescanReport:
    """Synchronously scan `library_root` and classify added, updated, removed, and unchanged tracks."""
    if not library_root.exists():
        return LibraryRescanReport(removed_tracks=tuple(existing_records))

    discovered_files = sorted(
        p for p in library_root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
    )

    by_path: dict[str, LibraryTrackRecord] = {
        _normalize_path_key(rec.storage_path): rec for rec in existing_records
    }
    matched_ids: set[str] = set()
    unmatched_files: list[Path] = []

    added: list[LibraryTrackRecord] = []
    updated: list[LibraryTrackRecord] = []
    unchanged_count = 0

    for audio_file in discovered_files:
        key = _normalize_path_key(audio_file)
        existing = by_path.get(key)
        if existing is not None:
            matched_ids.add(existing.id)
            inspected = _inspect_library_audio_file_sync(audio_file, existing)
            if _has_record_changed(existing, inspected):
                updated.append(inspected)
            else:
                unchanged_count += 1
        else:
            unmatched_files.append(audio_file)

    remaining_records = [rec for rec in existing_records if rec.id not in matched_ids]
    remaining_by_fp: dict[str, LibraryTrackRecord] = {rec.fingerprint: rec for rec in remaining_records}
    remaining_by_tail: dict[str, LibraryTrackRecord] = {
        _extract_tail_library_key(rec.storage_path): rec for rec in remaining_records
    }

    for audio_file in unmatched_files:
        sha_fp = _compute_file_sha256_fingerprint_sync(audio_file)
        tail_key = _extract_tail_library_key(audio_file)
        moved_rec = remaining_by_tail.pop(tail_key, None)
        if moved_rec is not None:
            remaining_by_fp.pop(moved_rec.fingerprint, None)
        else:
            moved_rec = remaining_by_fp.pop(sha_fp, None)
            if moved_rec is not None:
                remaining_by_tail.pop(_extract_tail_library_key(moved_rec.storage_path), None)

        if moved_rec is None:
            # Also check if only 1 missing record has the exact same filename and duration
            fallback_meta = _read_mutagen_metadata_sync(
                audio_file,
                parse_filename_metadata(audio_file.stem),
            )
            for cand_rec in list(remaining_by_fp.values()):
                if (
                    cand_rec.title.lower() == fallback_meta.title.lower()
                    and cand_rec.artist.lower() == fallback_meta.artist.lower()
                ):
                    moved_rec = remaining_by_fp.pop(cand_rec.fingerprint, None)
                    remaining_by_tail.pop(_extract_tail_library_key(cand_rec.storage_path), None)
                    break

        if moved_rec is not None:
            matched_ids.add(moved_rec.id)
            inspected = _inspect_library_audio_file_sync(audio_file, moved_rec)
            updated.append(inspected)
        else:
            new_rec = _inspect_library_audio_file_sync(audio_file, None, precomputed_fp=sha_fp)
            added.append(new_rec)

    removed = [rec for rec in existing_records if rec.id not in matched_ids]
    return LibraryRescanReport(
        added_tracks=tuple(added),
        updated_tracks=tuple(updated),
        removed_tracks=tuple(removed),
        unchanged_count=unchanged_count,
    )


def _inspect_library_audio_file_sync(
    audio_path: Path,
    existing: LibraryTrackRecord | None,
    *,
    precomputed_fp: str | None = None,
) -> LibraryTrackRecord:
    """Read embedded tags and `.lrc` sidecar status from `audio_path` to build a `LibraryTrackRecord`."""
    fallback = parse_filename_metadata(audio_path.stem)
    # Infer artist/album from parent folders `<Artist>/<Album (Year)>/<track>` if tags are missing
    album_folder = audio_path.parent.name
    artist_folder = audio_path.parent.parent.name if audio_path.parent.parent != audio_path.parent else ""
    year_match = re.search(r"\((\d{4})\)\s*$", album_folder)
    folder_year = int(year_match.group(1)) if year_match else None
    clean_album_folder = re.sub(r"\s*\(\d{4}\)\s*$", "", album_folder).strip()

    meta = _read_mutagen_metadata_sync(audio_path, fallback)
    artist = (
        meta.artist
        if meta.artist and meta.artist != "Unknown Artist"
        else (artist_folder or "Unknown Artist")
    )
    album = meta.album if meta.album and meta.album != "Unknown Album" else (clean_album_folder or meta.title)
    year = meta.year or folder_year
    lyrics = _read_sidecar_or_embedded_lyrics_sync(audio_path)
    has_synced = bool(lyrics and (lyrics.has_synced or lyrics.has_any))

    return LibraryTrackRecord(
        id=existing.id if existing is not None else str(uuid.uuid4()),
        job_id=existing.job_id if existing is not None else None,
        uploaded_by=existing.uploaded_by if existing is not None else 0,
        storage_path=audio_path,
        fingerprint=(
            existing.fingerprint
            if existing is not None
            else (precomputed_fp or _compute_file_sha256_fingerprint_sync(audio_path))
        ),
        duration_seconds=float(
            meta.duration_seconds
            if meta.duration_seconds is not None
            else (existing.duration_seconds if existing is not None else 0.0)
        ),
        artist=artist,
        album=album,
        title=meta.title,
        year=year,
        track_number=meta.track_number or (existing.track_number if existing is not None else 1),
        has_synced_lyrics=has_synced,
        ingested_at=existing.ingested_at if existing is not None else datetime.now(UTC),
    )


def _has_record_changed(existing: LibraryTrackRecord, inspected: LibraryTrackRecord) -> bool:
    """Return True if a scanned track's path, metadata, or lyrics status differs from `existing`."""
    return (
        _normalize_path_key(existing.storage_path) != _normalize_path_key(inspected.storage_path)
        or existing.title != inspected.title
        or existing.artist != inspected.artist
        or existing.album != inspected.album
        or existing.year != inspected.year
        or existing.track_number != inspected.track_number
        or existing.has_synced_lyrics != inspected.has_synced_lyrics
    )


def _apply_partial_tag_correction_sync(
    audio_path: Path,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    year: int | None = None,
) -> bool:
    """Update non-None metadata fields (`title`, `artist`, `album`, `year`) on `audio_path` in-place."""
    try:
        audio = mutagen.File(audio_path, easy=True)
    except Exception as exc:
        logger.debug("Could not open %s for tag correction: %s", audio_path.name, exc)
        return False
    if audio is None:
        return False
    try:
        if getattr(audio, "tags", None) is None and hasattr(audio, "add_tags"):
            audio.add_tags()
        if title is not None:
            audio["title"] = [title]
        if artist is not None:
            audio["artist"] = [artist]
            audio["albumartist"] = [artist]
        if album is not None:
            audio["album"] = [album]
        if year is not None:
            audio["date"] = [str(year)]
        audio.save()
        return True
    except Exception as exc:
        logger.warning("Failed to apply tag correction to %s: %s", audio_path.name, exc)
        return False
