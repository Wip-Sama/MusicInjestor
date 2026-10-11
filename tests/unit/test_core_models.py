"""Unit tests for core domain models, metadata similarity, extractor staging, and organizer."""

import asyncio
from pathlib import Path

import httpx
import pytest

from src.core.exceptions import (
    ArchiveExtractionError,
    DuplicateTrackError,
    FingerprintingError,
    UnsupportedAudioFormatError,
)
from src.core.extractor import SevenZipArchiveExtractor
from src.core.fingerprint import ChromaprintFingerprinter
from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import MusicBrainzMetadataService, compute_metadata_similarity
from src.core.models import LyricsData, MetadataCandidate, TrackMetadata
from src.core.organizer import LibraryOrganizer


def test_lrclib_lyrics_client_200_and_404() -> None:
    """Verify `LrclibLyricsClient` parses 200 JSON payloads and returns None on 404."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if "NotFound" in str(request.url):
            return httpx.Response(status_code=404)
        return httpx.Response(
            status_code=200,
            json={
                "syncedLyrics": "[00:02.00] Around the world",
                "plainLyrics": "Around the world",
                "instrumental": False,
            },
        )

    client = LrclibLyricsClient(transport=httpx.MockTransport(_handler))
    found = asyncio.run(
        client.fetch_lyrics(
            TrackMetadata(
                title="Around the World",
                artist="Daft Punk",
                album="Homework",
                duration_seconds=429,
            )
        )
    )
    assert found is not None
    assert found.has_synced is True
    assert "Around the world" in (found.synced_lyrics or "")

    missing = asyncio.run(client.fetch_lyrics(TrackMetadata(title="NotFound", artist="Nobody", album="None")))
    assert missing is None


def test_track_metadata_and_candidate_formatting() -> None:
    """Verify `effective_album_artist` fallback and `display_label` formatting."""
    meta = TrackMetadata(
        title="Paranoid Android",
        artist="Radiohead",
        album="OK Computer",
        year=1997,
    )
    assert meta.effective_album_artist == "Radiohead"

    meta_with_album_artist = TrackMetadata(
        title="Track 1",
        artist="Feat Artist",
        album="Compilation",
        album_artist="Various Artists",
    )
    assert meta_with_album_artist.effective_album_artist == "Various Artists"

    candidate = MetadataCandidate(
        metadata=meta,
        confidence=0.942,
        disambiguation="Remastered",
    )
    assert "Radiohead - Paranoid Android" in candidate.display_label
    assert "OK Computer (1997) [Remastered] (94%)" in candidate.display_label


def test_lyrics_data_properties() -> None:
    """Verify `has_synced` and `has_any` flags on `LyricsData`."""
    empty = LyricsData()
    assert not empty.has_synced
    assert not empty.has_any

    plain_only = LyricsData(plain_lyrics="Hello world")
    assert not plain_only.has_synced
    assert plain_only.has_any

    synced = LyricsData(synced_lyrics="[00:01.00] Hello world")
    assert synced.has_synced
    assert synced.has_any


def test_compute_metadata_similarity() -> None:
    """Verify exact matches return 1.0 and distinct tracks return lower confidence."""
    track_a = TrackMetadata(title="Time", artist="Pink Floyd", album="The Dark Side of the Moon")
    track_b = TrackMetadata(title="Time", artist="Pink Floyd", album="The Dark Side of the Moon")
    track_c = TrackMetadata(title="Money", artist="Someone Else", album="Another Album")

    assert compute_metadata_similarity(track_a, track_b) == 1.0
    assert compute_metadata_similarity(track_a, track_c) < 0.4


def test_duplicate_track_error_attributes() -> None:
    """Verify `DuplicateTrackError` stores existing path and reason."""
    err = DuplicateTrackError(existing_path=Path("/music/song.flac"), reason="fingerprint")
    assert err.existing_path == Path("/music/song.flac")
    assert err.reason == "fingerprint"
    assert "fingerprint" in str(err)


def test_extractor_stages_single_audio_and_cleans_up(tmp_path: Path) -> None:
    """Verify staging a single `.flac` file and cleaning up the scratch folder."""
    source_audio = tmp_path / "Artist - Song.flac"
    source_audio.write_bytes(b"fLaC-fake-header")

    extractor = SevenZipArchiveExtractor(scratch_root=tmp_path / "scratch")
    staged_files = asyncio.run(extractor.extract_or_stage(source_audio, "job001"))

    assert len(staged_files) == 1
    assert staged_files[0].is_file()
    assert staged_files[0].name == "Artist - Song.flac"

    asyncio.run(extractor.cleanup_job_scratch("job001"))
    assert not extractor.get_job_scratch_dir("job001").exists()


def test_extractor_rejects_missing_or_unsupported_files(tmp_path: Path) -> None:
    """Verify missing files raise `ArchiveExtractionError` and invalid extensions fail."""
    extractor = SevenZipArchiveExtractor(scratch_root=tmp_path / "scratch")

    with pytest.raises(ArchiveExtractionError):
        asyncio.run(extractor.extract_or_stage(tmp_path / "missing.flac", "job404"))

    bad_file = tmp_path / "notes.txt"
    bad_file.write_text("not audio", encoding="utf-8")
    with pytest.raises(UnsupportedAudioFormatError):
        asyncio.run(extractor.extract_or_stage(bad_file, "job405"))


def test_fingerprinter_missing_file_or_binary_raises(tmp_path: Path) -> None:
    """Verify `ChromaprintFingerprinter` raises `FingerprintingError` when file or binary is absent."""
    fp = ChromaprintFingerprinter(fpcalc_binary="nonexistent_fpcalc_binary_xyz")
    with pytest.raises(FingerprintingError):
        asyncio.run(fp.compute_fingerprint(tmp_path / "missing.flac"))

    audio = tmp_path / "sample.flac"
    audio.write_bytes(b"fLaC")
    with pytest.raises(FingerprintingError, match="was not found on PATH"):
        asyncio.run(fp.compute_fingerprint(audio))


def test_metadata_service_parses_artist_title_from_stem(tmp_path: Path) -> None:
    """Verify `MusicBrainzMetadataService` parses `Artist - Title` filenames and generates candidates."""
    service = MusicBrainzMetadataService()
    audio_path = tmp_path / "Daft Punk - Digital Love.flac"
    local_meta = asyncio.run(service.read_local_metadata(audio_path))
    assert local_meta.artist == "Daft Punk"
    assert local_meta.title == "Digital Love"

    candidates = asyncio.run(service.find_candidates(local_meta))
    assert len(candidates) == 1
    assert candidates[0].confidence >= 0.90


def test_library_organizer_sanitizes_paths_and_writes_lrc(tmp_path: Path) -> None:
    """Verify `LibraryOrganizer` sanitizes unsafe characters and writes `.lrc` sidecars."""
    organizer = LibraryOrganizer(library_root=tmp_path / "library")
    meta = TrackMetadata(
        title="Weird: Title? *Test*",
        artist="AC/DC",
        album="Back in Black",
        year=1980,
        track_number=3,
    )
    target_path = organizer.build_target_audio_path(meta, ".FLAC")
    assert target_path.suffix == ".flac"
    assert target_path.parent.name == "Back in Black (1980)"
    assert target_path.name.startswith("03 - ")

    source_file = tmp_path / "raw.flac"
    source_file.write_bytes(b"fLaC-audio-bytes")
    lyrics = LyricsData(synced_lyrics="[00:00.50] Back in black")

    final_path = asyncio.run(organizer.finalize_track(source_file, meta, lyrics))
    assert final_path.is_file()
    lrc_file = final_path.with_suffix(".lrc")
    assert lrc_file.is_file()
    assert "Back in black" in lrc_file.read_text(encoding="utf-8")
