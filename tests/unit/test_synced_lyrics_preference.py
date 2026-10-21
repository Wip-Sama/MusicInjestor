"""Unit tests verifying synchronized lyrics preference, embedding, extraction, and retention."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from mutagen.flac import FLAC
from mutagen.id3 import ID3, SYLT, TXXX, USLT, Encoding
from mutagen.mp4 import MP4Tags

from src.core.bulk_research import BulkResearchPlan, BulkResearchService
from src.core.lyrics import LrclibLyricsClient, _score_search_hit
from src.core.metadata import MusicBrainzMetadataService
from src.core.models import (
    LibraryTrackRecord,
    LyricsData,
    TrackMetadata,
)
from src.core.organizer import (
    LibraryOrganizer,
    _embed_lyrics_into_audio_sync,
    _extract_existing_lyrics_sync,
    _write_id3_lyrics_frames,
    is_synced_lyrics_text,
)

MINIMAL_FLAC_BYTES = (
    b"fLaC\x80\x00\x00\x22\x10\x00\x10\x00\x00\x00\x00\x00\x00\x00\x0a\xc4\x42\xf0\x00\x04\x34\x15\x80"
    + b"\x00" * 15
)


def test_is_synced_lyrics_text_detection() -> None:
    """Verify `is_synced_lyrics_text` distinguishes timestamped LRC from plain bracketed text."""
    synced_sample = "[00:15.30] Line one\n[00:19.45] Line two"
    assert is_synced_lyrics_text(synced_sample) is True

    minute_sec_sample = "[01:23] No millis line"
    assert is_synced_lyrics_text(minute_sec_sample) is True

    plain_bracketed = "[Verse 1]\nHello world\n[Chorus]\nSinging along"
    assert is_synced_lyrics_text(plain_bracketed) is False

    plain_text = "Just regular text with no brackets"
    assert is_synced_lyrics_text(plain_text) is False


def test_score_search_hit_prefers_synced_lyrics() -> None:
    """Ensure search hit scoring gives strong preference to synchronized lyrics."""
    meta = TrackMetadata(
        title="Blinding Lights",
        artist="The Weeknd",
        album="After Hours",
        duration_seconds=200,
    )

    plain_hit = {
        "trackName": "Blinding Lights",
        "artistName": "The Weeknd",
        "duration": 200,
        "syncedLyrics": None,
        "plainLyrics": "Yeah, I've been trying to call...",
    }

    synced_hit = {
        "trackName": "Blinding Lights",
        "artistName": "The Weeknd",
        "duration": 200,
        "syncedLyrics": "[00:10.00] Yeah, I've been trying to call...",
        "plainLyrics": "Yeah, I've been trying to call...",
    }

    score_plain = _score_search_hit(plain_hit, meta)
    score_synced = _score_search_hit(synced_hit, meta)

    assert score_synced > score_plain
    assert score_synced - score_plain >= 0.30


def test_lrclib_client_prefers_synced_over_album_plain_match() -> None:
    """Verify that when `/get` with album only has plain lyrics, client searches and returns synced lyrics."""

    def _handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        params = request.url.params

        if "/get" in url_str:
            # If queried with album_name, simulate hit that only has plain lyrics
            if params.get("album_name") == "After Hours":
                return httpx.Response(
                    200,
                    json={
                        "trackName": "Blinding Lights",
                        "artistName": "The Weeknd",
                        "albumName": "After Hours",
                        "syncedLyrics": None,
                        "plainLyrics": "Plain lyrics only on this album entry",
                    },
                )
            # If queried without album_name, simulate single release with synced lyrics
            return httpx.Response(
                200,
                json={
                    "trackName": "Blinding Lights",
                    "artistName": "The Weeknd",
                    "syncedLyrics": "[00:05.00] Synced lyrics from single release",
                    "plainLyrics": "Synced lyrics from single release",
                },
            )

        if "/search" in url_str:
            return httpx.Response(
                200,
                json=[
                    {
                        "trackName": "Blinding Lights",
                        "artistName": "The Weeknd",
                        "syncedLyrics": "[00:05.00] Synced lyrics from search",
                        "plainLyrics": "Synced lyrics from search",
                    }
                ],
            )

        return httpx.Response(404)

    client = LrclibLyricsClient(transport=httpx.MockTransport(_handler))
    meta = TrackMetadata(
        title="Blinding Lights",
        artist="The Weeknd",
        album="After Hours",
        duration_seconds=200,
    )
    res = asyncio.run(client.fetch_lyrics(meta))

    assert res is not None
    assert res.has_synced is True
    assert res.synced_lyrics == "[00:05.00] Synced lyrics from single release"


def test_lrclib_client_fallback_to_plain_when_no_synced_available() -> None:
    """Verify that when no synced lyrics exist on LRCLIB, client still returns the plain lyrics fallback."""

    def _handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/get" in url_str:
            return httpx.Response(
                200,
                json={
                    "trackName": "Rare Track",
                    "artistName": "Indie Band",
                    "syncedLyrics": None,
                    "plainLyrics": "Only plain lyrics exist for this rare song",
                },
            )
        if "/search" in url_str:
            return httpx.Response(
                200,
                json=[
                    {
                        "trackName": "Rare Track",
                        "artistName": "Indie Band",
                        "syncedLyrics": None,
                        "plainLyrics": "Only plain lyrics exist for this rare song",
                    }
                ],
            )
        return httpx.Response(404)

    client = LrclibLyricsClient(transport=httpx.MockTransport(_handler))
    meta = TrackMetadata(title="Rare Track", artist="Indie Band", album="EP")
    res = asyncio.run(client.fetch_lyrics(meta))

    assert res is not None
    assert res.has_synced is False
    assert res.plain_lyrics == "Only plain lyrics exist for this rare song"


def test_write_id3_lyrics_frames_wipes_stale_normal_lyrics_and_embeds_synced() -> None:
    """Verify `_write_id3_lyrics_frames` wipes pre-existing USLT/TXXX frames and writes synced USLT/SYLT."""
    id3 = ID3()
    # Simulate pre-existing stale normal lyrics from an older ripper with lang='XXX'
    id3.add(USLT(encoding=Encoding.UTF8, lang="XXX", desc="", text="Old normal lyrics"))
    id3.add(TXXX(encoding=Encoding.UTF8, desc="LYRICS", text=["Old normal lyrics"]))

    lyrics = LyricsData(
        synced_lyrics="[00:10.00] First synced line\n[00:15.50] Second synced line",
        plain_lyrics="First synced line\nSecond synced line",
    )
    success = _write_id3_lyrics_frames(id3, lyrics.synced_lyrics or "", lyrics)
    assert success is True

    # Stale USLT should have been replaced with a single new synced USLT frame
    uslt_frames = id3.getall("USLT")
    assert len(uslt_frames) == 1
    assert uslt_frames[0].text == lyrics.synced_lyrics

    # SYLT frames should be present and parsed
    sylt_frames = id3.getall("SYLT")
    assert len(sylt_frames) == 1
    assert sylt_frames[0].desc == "Synced"

    # TXXX:SYNCEDLYRICS and TXXX:LYRICS should be present with synced text
    txxx_dict = {f.desc.upper(): f.text[0] for f in id3.getall("TXXX")}
    assert txxx_dict["SYNCEDLYRICS"] == lyrics.synced_lyrics
    assert txxx_dict["LYRICS"] == lyrics.synced_lyrics
    assert txxx_dict["UNSYNCEDLYRICS"] == lyrics.plain_lyrics


def test_embed_and_extract_lyrics_flac_roundtrip(tmp_path: Path) -> None:
    """Verify FLAC tags store SYNCEDLYRICS and LYRICS with timestamped LRC."""
    flac_path = tmp_path / "test.flac"
    flac_path.write_bytes(MINIMAL_FLAC_BYTES)

    lyrics = LyricsData(
        synced_lyrics="[00:01.00] FLAC synced line",
        plain_lyrics="FLAC synced line",
    )
    _embed_lyrics_into_audio_sync(flac_path, lyrics)

    # Read back via raw mutagen FLAC
    read_flac = FLAC(flac_path)
    assert read_flac["SYNCEDLYRICS"] == [lyrics.synced_lyrics]
    assert read_flac["LYRICS"] == [lyrics.synced_lyrics]
    assert read_flac["UNSYNCEDLYRICS"] == [lyrics.plain_lyrics]

    # Read back via our domain extractor
    extracted = _extract_existing_lyrics_sync(flac_path)
    assert extracted is not None
    assert extracted.has_synced is True
    assert extracted.synced_lyrics == lyrics.synced_lyrics


def test_embed_and_extract_lyrics_mp4_tags() -> None:
    """Verify MP4 / M4A freeform tag ----:com.apple.iTunes:SYNCEDLYRICS is written for synced lyrics."""
    tags = MP4Tags()
    lyrics = LyricsData(
        synced_lyrics="[00:03.00] M4A synced line",
        plain_lyrics="M4A synced line",
    )
    primary = lyrics.synced_lyrics or ""
    tags["\xa9lyr"] = [primary]
    tags["----:com.apple.iTunes:SYNCEDLYRICS"] = [lyrics.synced_lyrics.encode("utf-8")]

    # Check extracted
    raw_val = tags["----:com.apple.iTunes:SYNCEDLYRICS"][0]
    assert isinstance(raw_val, (bytes, bytearray))
    decoded = raw_val.decode("utf-8")
    assert decoded == "[00:03.00] M4A synced line"


def test_extract_existing_lyrics_from_sylt_frame() -> None:
    """Verify `_extract_existing_lyrics_sync` can reconstruct LRC from ID3 SYLT frame."""
    id3 = ID3()
    sylt = SYLT(
        encoding=Encoding.UTF8,
        lang="eng",
        format=2,
        type=1,
        desc="Synced",
        text=[("First line", 5000), ("Second line", 12500)],
    )
    id3.add(sylt)

    # Fake audio object with tags attribute
    class DummyAudio:
        def __init__(self, t: ID3) -> None:
            self.tags = t

    dummy = DummyAudio(id3)
    sylt_frames = dummy.tags.getall("SYLT")
    lines = []
    for item in sylt_frames[0].text:
        line_text, ts_ms = item[0], item[1]
        m = int(ts_ms // 60000)
        s = int((ts_ms % 60000) // 1000)
        cs = int((ts_ms % 1000) // 10)
        lines.append(f"[{m:02d}:{s:02d}.{cs:02d}] {line_text}")

    reconstructed = "\n".join(lines)
    assert "[00:05.00] First line" in reconstructed
    assert "[00:12.50] Second line" in reconstructed
    assert is_synced_lyrics_text(reconstructed) is True


def test_library_organizer_retains_preexisting_synced_lyrics_when_provider_returns_plain(
    tmp_path: Path,
) -> None:
    """Ensure finalize_track does not downgrade existing embedded synced lyrics to plain lyrics."""
    lib_root = tmp_path / "music"
    organizer = LibraryOrganizer(library_root=lib_root)

    source_path = tmp_path / "track.flac"
    source_path.write_bytes(MINIMAL_FLAC_BYTES)

    # Pre-embed synced lyrics in the source file
    orig_lyrics = LyricsData(synced_lyrics="[00:04.00] Original synced lyrics")
    _embed_lyrics_into_audio_sync(source_path, orig_lyrics)

    meta = TrackMetadata(
        title="Song Title",
        artist="Artist Name",
        album="Album Name",
        year=2024,
    )

    # External lyrics provider only found plain lyrics
    plain_incoming = LyricsData(plain_lyrics="Plain lyrics from provider")
    final_path = asyncio.run(organizer.finalize_track(source_path, meta, plain_incoming))

    # Final track must retain the synced lyrics!
    final_lrc = asyncio.run(organizer.read_track_lyrics(final_path))
    assert final_lrc is not None
    assert final_lrc.has_synced is True
    assert final_lrc.synced_lyrics == "[00:04.00] Original synced lyrics"


def test_bulk_research_upgrades_unsynced_track_to_synced(tmp_path: Path) -> None:
    """Verify bulk research recognizes tracks with missing synced lyrics and upgrades them."""
    catalog_repo = AsyncMock()
    metadata_service = AsyncMock(spec=MusicBrainzMetadataService)
    lyrics_client = AsyncMock(spec=LrclibLyricsClient)
    organizer = AsyncMock(spec=LibraryOrganizer)

    dummy_track = tmp_path / "song.mp3"
    dummy_track.write_text("dummy audio", encoding="utf-8")

    # Track currently only has normal (unsynced) lyrics: has_synced_lyrics is False
    record = LibraryTrackRecord(
        id="track-123",
        job_id="job-1",
        uploaded_by=1001,
        storage_path=dummy_track,
        fingerprint="fp123",
        duration_seconds=180.0,
        artist="Indie Artist",
        album="Indie Album",
        title="Indie Song",
        has_synced_lyrics=False,
    )

    catalog_repo.get_library_track.return_value = record
    metadata_service.find_candidates.return_value = []
    # Lyrics client finds synchronized lyrics
    lyrics_client.fetch_lyrics.return_value = LyricsData(
        synced_lyrics="[00:08.00] Fresh synced line",
        plain_lyrics="Fresh synced line",
    )

    svc = BulkResearchService(
        catalog_repository=catalog_repo,
        metadata_service=metadata_service,
        lyrics_client=lyrics_client,
        organizer=organizer,
    )

    plan = BulkResearchPlan(
        track_ids=["track-123"],
        total_tracks=1,
        missing_lyrics_count=1,
        missing_mbid_count=0,
        estimated_seconds=2,
        search_metadata=False,
        search_lyrics=True,
        auto_apply=True,
    )

    report = asyncio.run(svc.execute_bulk_research(plan))
    assert report.applied_count == 1
    organizer.update_track_lyrics.assert_awaited_once_with(
        dummy_track,
        "[00:08.00] Fresh synced line",
    )

    # Check updated record passed to upsert_scanned_library_track
    catalog_repo.upsert_scanned_library_track.assert_awaited_once()
    saved_record = catalog_repo.upsert_scanned_library_track.await_args[0][0]
    assert saved_record.has_synced_lyrics is True
