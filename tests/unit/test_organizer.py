"""Unit tests for LibraryOrganizer filesystem operations, tag writing, and rescan reconciliation."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.models import (
    LibraryTrackRecord,
    LyricsData,
    TrackMetadata,
)
from src.core.organizer import (
    LibraryOrganizer,
    _cleanup_old_track_location_sync,
    _embed_cover_art_into_audio_sync,
    _embed_lyrics_into_audio_sync,
    _extract_embedded_cover_bytes_sync,
    _extract_existing_lyrics_sync,
    _parse_umask_mode,
    _write_enriched_tags_sync,
    _write_id3_lyrics_frames,
    parse_lrc_timestamps,
)


def test_parse_lrc_timestamps() -> None:
    """Verify LRC timestamp parsing into milliseconds."""
    lrc = "[00:01.50]Line 1\n[01:05.000]Line 2\nInvalid line\n[00:10]Line 3"
    parsed = parse_lrc_timestamps(lrc)
    assert len(parsed) == 3
    assert parsed[0] == ("Line 1", 1500)
    assert parsed[1] == ("Line 3", 10000)
    assert parsed[2] == ("Line 2", 65000)


@pytest.mark.anyio
async def test_organizer_permissions_and_helpers(tmp_path: Path) -> None:
    """Verify permissions helper methods and directory walking."""
    lib = tmp_path / "lib"
    lib.mkdir()
    organizer = LibraryOrganizer(library_root=lib, umask="0022")

    assert organizer.file_mode >= 0o644
    assert organizer.dir_mode >= 0o755
    assert organizer.is_reconciling is False
    assert organizer.reconciliation_status == "idle"

    # Create test nested directory and file
    nested_dir = lib / "Artist" / "Album"
    nested_dir.mkdir(parents=True)
    test_file = nested_dir / "song.mp3"
    test_file.write_bytes(b"DATA")

    organizer.apply_file_permissions(test_file)
    organizer.apply_dir_permissions(nested_dir)
    fixed = await organizer.fix_library_permissions()
    assert fixed >= 2


@pytest.mark.anyio
async def test_organizer_lyrics_and_bundle(tmp_path: Path) -> None:
    """Verify update_track_lyrics, read_track_lyrics, and export_song_bundle."""
    lib = tmp_path / "lib"
    lib.mkdir()
    organizer = LibraryOrganizer(library_root=lib, enable_network=False)

    song_dir = lib / "Singer" / "Album"
    song_dir.mkdir(parents=True)
    audio_file = song_dir / "01. Singer - Track.mp3"
    audio_file.write_bytes(b"ID3" + b"\x00" * 100)

    # 1. Update lyrics (writes .lrc sidecar)
    await organizer.update_track_lyrics(audio_file, "[00:02.00]Test lyric line")
    sidecar = song_dir / "01. Singer - Track.lrc"
    assert sidecar.is_file()
    assert "[00:02.00]Test lyric line" in sidecar.read_text(encoding="utf-8")

    # 2. Read lyrics
    read_data = await organizer.read_track_lyrics(audio_file)
    assert read_data is not None
    assert "[00:02.00]Test lyric line" in read_data.synced_lyrics

    # 3. Export song bundle
    track_rec = LibraryTrackRecord(
        id="trk_exp1",
        job_id="job_exp",
        uploaded_by=100,
        storage_path=audio_file,
        fingerprint="fp123",
        duration_seconds=120,
        artist="Singer",
        album="Album",
        title="Track",
    )
    bundle = await organizer.export_song_bundle(track_rec)
    assert bundle["track_id"] == "trk_exp1"
    assert bundle["title"] == "Track"
    assert bundle["lyrics"] is not None
    assert "[00:02.00]" in bundle["lyrics"]


@pytest.mark.anyio
async def test_organizer_cover_art_update(tmp_path: Path) -> None:
    """Verify update_track_cover_art with raw bytes and cover image path."""
    lib = tmp_path / "lib"
    lib.mkdir()
    organizer = LibraryOrganizer(library_root=lib, enable_network=False)

    song_dir = lib / "Singer" / "Album"
    song_dir.mkdir(parents=True)
    audio_file = song_dir / "track.flac"
    audio_file.write_bytes(b"fLaC" + b"\x00" * 100)

    # Update with raw bytes
    cover_res = await organizer.update_track_cover_art(audio_file, b"JPEG_BYTES")
    assert cover_res == song_dir / "cover.jpg"
    assert cover_res.read_bytes() == b"JPEG_BYTES"


@pytest.mark.anyio
async def test_organizer_scan_and_reconciliation(tmp_path: Path) -> None:
    """Verify filesystem scanning, report generation, and repository reconciliation."""
    lib = tmp_path / "lib"
    lib.mkdir()
    organizer = LibraryOrganizer(library_root=lib, enable_network=False)

    artist_dir = lib / "Band" / "Record"
    artist_dir.mkdir(parents=True)
    audio1 = artist_dir / "01. Band - Song.mp3"
    audio1.write_bytes(b"ID3" + b"\x00" * 100)

    # Scan with empty catalog -> should discover 1 added track
    report = await organizer.scan_library_filesystem(existing_records=[])
    assert len(report.added_tracks) == 1
    assert report.added_tracks[0].title == "Song"
    assert report.has_changes is True

    # Apply rescan report to mock repository
    repo = AsyncMock()
    del repo.upsert_scanned_library_tracks_batch
    await organizer.apply_rescan_report(report, repo)
    repo.upsert_scanned_library_track.assert_called_once()

    # Reconcile full library with repository
    repo.list_library_tracks.return_value = []
    reconciled_report = await organizer.reconcile_library_with_repository(repo)
    assert len(reconciled_report.added_tracks) == 1


@pytest.mark.anyio
async def test_organizer_relocate_and_retag(tmp_path: Path) -> None:
    """Verify relocate_and_retag_track moves file and updates sidecars."""
    lib = tmp_path / "lib"
    lib.mkdir()
    organizer = LibraryOrganizer(library_root=lib, enable_network=False)

    old_dir = lib / "OldArtist" / "OldAlbum"
    old_dir.mkdir(parents=True)
    old_audio = old_dir / "01. OldTrack.mp3"
    old_audio.write_bytes(b"ID3" + b"\x00" * 100)
    old_lrc = old_dir / "01. OldTrack.lrc"
    old_lrc.write_text("[00:01.00]Old Lyrics", encoding="utf-8")
    old_cover = old_dir / "cover.jpg"
    old_cover.write_bytes(b"OLD_COVER")

    new_meta = TrackMetadata(
        title="NewTrack",
        artist="NewArtist",
        album="NewAlbum",
        year=2024,
        track_number=1,
    )

    new_audio_path = await organizer.relocate_and_retag_track(old_audio, new_meta)
    assert new_audio_path.is_file()
    assert "NewArtist" in str(new_audio_path)
    assert "NewAlbum" in str(new_audio_path)

    # Check sidecar LRC moved
    new_lrc = new_audio_path.with_suffix(".lrc")
    assert new_lrc.is_file()


def test_organizer_umask_parsing() -> None:
    """Verify umask string, int, and fallback parsing."""
    assert _parse_umask_mode(0o022) == 0o022
    assert _parse_umask_mode("022") == 0o022
    assert _parse_umask_mode("invalid") == 0o022


def test_organizer_embedded_lyrics_extraction(tmp_path: Path) -> None:
    """Verify extracting synced and unsynced lyrics from mutagen tags."""
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"DATA")

    with patch("mutagen.File") as mock_mut:
        fake_audio = MagicMock()
        fake_audio.tags = None
        mock_mut.return_value = fake_audio
        assert _extract_existing_lyrics_sync(audio) is None

        fake_audio.tags = {"SYNCEDLYRICS": ["[00:01.00]Line"]}
        res = _extract_existing_lyrics_sync(audio)
        assert res is not None
        assert res.synced_lyrics == "[00:01.00]Line"

        class FakeUSLT:
            text = "Plain lyrical text"

        fake_id3 = MagicMock()
        fake_id3.get.return_value = None
        fake_id3.getall.return_value = [FakeUSLT()]
        fake_audio.tags = fake_id3
        res_uslt = _extract_existing_lyrics_sync(audio)
        assert res_uslt is not None
        assert res_uslt.plain_lyrics == "Plain lyrical text"


def test_organizer_embedded_cover_extraction(tmp_path: Path) -> None:
    """Verify extracting cover bytes from APIC frames and covr atom."""
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"DATA")

    with patch("mutagen.File") as mock_mut:
        fake_audio = MagicMock()
        fake_audio.pictures = []
        fake_audio.tags = None
        mock_mut.return_value = fake_audio
        assert _extract_embedded_cover_bytes_sync(audio) is None

        class FakeAPIC:
            data = b"APIC_DATA_BYTES"

        fake_id3 = MagicMock()
        fake_id3.getall.return_value = [FakeAPIC()]
        fake_id3.get.return_value = None
        fake_audio.tags = fake_id3
        assert _extract_embedded_cover_bytes_sync(audio) == b"APIC_DATA_BYTES"

        fake_mp4 = MagicMock()
        fake_mp4.getall.side_effect = Exception("No ID3")
        fake_mp4.get.return_value = [b"MP4_COVR_BYTES"]
        fake_audio.tags = fake_mp4
        assert _extract_embedded_cover_bytes_sync(audio) == b"MP4_COVR_BYTES"


def test_organizer_cleanup_old_location(tmp_path: Path) -> None:
    """Verify removing old audio, lrc, and parent directories."""
    lib = tmp_path / "lib"
    art_dir = lib / "Artist" / "Album"
    art_dir.mkdir(parents=True)
    old_audio = art_dir / "track.mp3"
    old_audio.write_bytes(b"DATA")
    old_lrc = art_dir / "track.lrc"
    old_lrc.write_text("LRC", encoding="utf-8")

    _cleanup_old_track_location_sync(old_audio, lib)
    assert not old_audio.exists()
    assert not old_lrc.exists()
    assert not art_dir.exists()


def test_organizer_embed_cover_formats(tmp_path: Path) -> None:
    """Verify _embed_cover_art_into_audio_sync across FLAC, MP4, and OGG formats."""
    # 1. FLAC add_picture
    flac_file = tmp_path / "song.flac"
    flac_file.write_bytes(b"DATA")
    with patch("mutagen.File") as mock_mut:
        mock_flac = MagicMock()
        mock_mut.return_value = mock_flac
        assert _embed_cover_art_into_audio_sync(flac_file, b"JPEG_COVER_BYTES", "image/jpeg") is True
        mock_flac.add_picture.assert_called_once()
        mock_flac.save.assert_called_once()

    # 2. MP4 covr atom
    mp4_file = tmp_path / "song.m4a"
    mp4_file.write_bytes(b"DATA")
    with patch("mutagen.File") as mock_mut:
        mock_mp4 = MagicMock(spec=["tags", "save"])
        mock_mp4.tags = {}
        mock_mut.return_value = mock_mp4
        assert _embed_cover_art_into_audio_sync(mp4_file, b"PNG_COVER_BYTES", "image/png") is True
        assert "covr" in mock_mp4.tags
        mock_mp4.save.assert_called_once()

    # 3. OGG metadata_block_picture
    ogg_file = tmp_path / "song.ogg"
    ogg_file.write_bytes(b"DATA")
    with patch("mutagen.File") as mock_mut:
        mock_ogg = MagicMock(spec=["tags", "save"])
        mock_ogg.tags = {}
        mock_mut.return_value = mock_ogg
        with patch("mutagen.flac.Picture.write", return_value=b"PIC_DATA"):
            assert _embed_cover_art_into_audio_sync(ogg_file, b"JPEG_COVER_BYTES", "image/jpeg") is True
            assert "metadata_block_picture" in mock_ogg.tags
            mock_ogg.save.assert_called_once()


def test_organizer_embed_lyrics_formats(tmp_path: Path) -> None:
    """Verify _embed_lyrics_into_audio_sync across FLAC, MP4, and MP3 formats."""
    lyrics = LyricsData(synced_lyrics="[00:01.00]Line 1", plain_lyrics="Line 1")

    # 1. FLAC format
    flac_file = tmp_path / "song.flac"
    flac_file.write_bytes(b"DATA")
    with patch("mutagen.File") as mock_mut:
        mock_flac = MagicMock()
        mock_mut.return_value = mock_flac
        _embed_lyrics_into_audio_sync(flac_file, lyrics)
        mock_flac.__setitem__.assert_any_call("SYNCEDLYRICS", ["[00:01.00]Line 1"])
        mock_flac.save.assert_called_once()

    # 2. MP4 format
    mp4_file = tmp_path / "song.m4a"
    mp4_file.write_bytes(b"DATA")
    with patch("mutagen.File") as mock_mut:
        mock_mp4 = MagicMock()
        mock_mut.return_value = mock_mp4
        _embed_lyrics_into_audio_sync(mp4_file, lyrics)
        mock_mp4.__setitem__.assert_any_call("\xa9lyr", ["[00:01.00]Line 1"])
        mock_mp4.save.assert_called_once()


def test_organizer_write_enriched_tags_and_correction(tmp_path: Path) -> None:
    """Verify _write_enriched_tags_sync and apply_tag_correction."""
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"DATA")
    meta = TrackMetadata(
        title="Title",
        artist="Artist",
        album="Album",
        year=2023,
        track_number=1,
    )

    with patch("mutagen.File") as mock_mut:
        mock_audio = MagicMock()
        mock_mut.return_value = mock_audio
        assert _write_enriched_tags_sync(audio, meta) is True
        mock_audio.__setitem__.assert_any_call("title", ["Title"])
        mock_audio.__setitem__.assert_any_call("artist", ["Artist"])
        mock_audio.save.assert_called_once()

    organizer = LibraryOrganizer(library_root=tmp_path / "lib", enable_network=False)
    with patch("src.core.organizer._write_enriched_tags_sync", return_value=True):
        res = asyncio.run(organizer.apply_tag_correction(audio, metadata=meta))
        assert res is True


@pytest.mark.anyio
async def test_organizer_ensure_album_cover_art(tmp_path: Path) -> None:
    """Verify _ensure_album_cover_art with staged sibling image and Cover Art Archive."""
    # 1. Staged sibling image
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    audio = staged_dir / "song.flac"
    audio.write_bytes(b"DATA")
    art = staged_dir / "folder.jpg"
    art.write_bytes(b"STAGED_ART")

    lib = tmp_path / "lib"
    organizer = LibraryOrganizer(library_root=lib, enable_network=False)
    target_dir = lib / "Artist" / "Album"
    target_dir.mkdir(parents=True)

    meta = TrackMetadata(title="Song", artist="Artist", album="Album")
    cov = await organizer._ensure_album_cover_art(audio, target_dir, meta)
    assert cov is not None
    assert cov.read_bytes() == b"STAGED_ART"

    # 2. Cover art archive fetch via MockTransport
    def handle_caa(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"CAA_ART_BYTES")

    caa_transport = httpx.MockTransport(handle_caa)
    caa_org = LibraryOrganizer(library_root=lib, transport=caa_transport, enable_network=True)
    caa_audio = tmp_path / "solo.flac"
    caa_audio.write_bytes(b"DATA")
    caa_target = lib / "Artist2" / "Album2"
    caa_target.mkdir(parents=True)
    meta_caa = TrackMetadata(title="Song", artist="Artist", album="Album", release_mbid="mbid-12345")
    cov_caa = await caa_org._ensure_album_cover_art(caa_audio, caa_target, meta_caa)
    assert cov_caa is not None
    assert cov_caa.read_bytes() == b"CAA_ART_BYTES"


def test_organizer_write_id3_lyrics_frames() -> None:
    """Verify attaching USLT, TXXX, and SYLT frames to ID3 tags."""
    fake_id3 = MagicMock()
    lyrics = LyricsData(synced_lyrics="[00:01.00]Line 1", plain_lyrics="Line 1")
    assert _write_id3_lyrics_frames(fake_id3, "Line 1", lyrics) is True
    assert fake_id3.add.call_count >= 3
    assert _write_id3_lyrics_frames(None, "Line 1", lyrics) is False


@pytest.mark.anyio
async def test_organizer_partial_tag_correction(tmp_path: Path) -> None:
    """Verify partial tag correction through apply_tag_correction."""
    lib = tmp_path / "lib"
    organizer = LibraryOrganizer(library_root=lib, enable_network=False)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"DATA")

    with patch("src.core.organizer._apply_partial_tag_correction_sync", return_value=True):
        res = await organizer.apply_tag_correction(audio, title="New Title", artist="New Artist")
        assert res is True


@pytest.mark.anyio
async def test_organizer_rescan_detects_moved_track(tmp_path: Path) -> None:
    """Verify filesystem rescan detects moved tracks via tail library key matching."""
    lib = tmp_path / "lib"
    organizer = LibraryOrganizer(library_root=lib, enable_network=False)

    new_dir = lib / "Artist" / "Album"
    new_dir.mkdir(parents=True)
    new_audio = new_dir / "song.mp3"
    new_audio.write_bytes(b"DATA_SAME_HASH_123")

    old_path = tmp_path / "other" / "Artist" / "Album" / "song.mp3"
    rec = LibraryTrackRecord(
        id="trk_moved",
        job_id="job1",
        uploaded_by=100,
        storage_path=old_path,
        fingerprint="fp1",
        duration_seconds=120,
        artist="Artist",
        album="Album",
        title="song",
    )

    report = await organizer.scan_library_filesystem(existing_records=[rec])
    assert len(report.updated_tracks) == 1
    assert report.updated_tracks[0].id == "trk_moved"
    assert report.updated_tracks[0].storage_path == new_audio
