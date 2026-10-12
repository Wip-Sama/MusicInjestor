"""Unit tests for core domain models, metadata similarity, extractor staging, and organizer."""

import asyncio
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import mutagen
import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3

from src.core.exceptions import (
    ArchiveExtractionError,
    DuplicateTrackError,
    FingerprintingError,
    UnsupportedAudioFormatError,
)
from src.core.extractor import SevenZipArchiveExtractor, find_best_cover_image
from src.core.fingerprint import ChromaprintFingerprinter
from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import (
    MusicBrainzMetadataService,
    compute_metadata_similarity,
    extract_artist_search_variants,
    extract_bilingual_english,
    extract_title_search_variants,
    normalize_primary_artist,
    parse_filename_metadata,
    parse_manual_search_query,
)
from src.core.models import AudioFingerprint, LyricsData, MetadataCandidate, TrackMetadata
from src.core.organizer import LibraryOrganizer, parse_lrc_timestamps

MINIMAL_FLAC_BYTES = (
    b"fLaC\x80\x00\x00\x22"
    b"\x10\x00\x10\x00\x00\x00\x00\x00\x00\x00\x0a\xc4\x42\xf0\x00\x04\x34\x15\x80" + b"\x00" * 15
)


def test_lrclib_lyrics_client_200_and_404() -> None:
    """Verify `LrclibLyricsClient` parses 200 JSON payloads, tries CV variants, and handles 404."""

    def _handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "NotFound" in url_str:
            return httpx.Response(status_code=404)
        if "EMILIA" in url_str:
            # Simulate LRCLIB 404 on full character credit, succeeding on extracted CV "Rie Takahashi"
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
                title="Stay Alive",
                artist="EMILIA(CV:RIE TAKAHASHI)",
                album="Stay Alive",
                duration_seconds=267,
            )
        )
    )
    assert found is not None
    assert found.has_synced is True
    assert "Around the world" in (found.synced_lyrics or "")

    missing = asyncio.run(client.fetch_lyrics(TrackMetadata(title="NotFound", artist="Nobody", album="None")))
    assert missing is None


def test_track_metadata_and_candidate_formatting() -> None:
    """Verify `effective_album_artist` fallback and `[XX%]` prefix at start of `display_label`."""
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
    assert candidate.display_label.startswith("[94%] Radiohead - Paranoid Android")
    assert "OK Computer (1997) [Remastered]" in candidate.display_label


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


def test_extractor_unpacks_zip_archive_with_stdlib_fallback(tmp_path: Path) -> None:
    """Verify `.zip` archives unpack via Python stdlib `zipfile` when `7z` is absent on PATH."""
    zip_path = tmp_path / "album.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("01 - First Track.flac", MINIMAL_FLAC_BYTES)
        zf.writestr("02 - Second Track.flac", MINIMAL_FLAC_BYTES)
        zf.writestr("cover.jpg", b"jpeg-bytes")

    extractor = SevenZipArchiveExtractor(
        scratch_root=tmp_path / "scratch",
        sevenzip_binary="nonexistent_7z_binary_xyz",
    )
    extracted = asyncio.run(extractor.extract_or_stage(zip_path, "job_zip"))
    assert len(extracted) == 2
    assert extracted[0].name == "01 - First Track.flac"
    assert extracted[1].name == "02 - Second Track.flac"


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


def test_fingerprinter_acoustid_lookup_with_mock_subprocess_and_http(tmp_path: Path) -> None:
    """Verify `ChromaprintFingerprinter` parses `fpcalc -json` and queries AcoustID v2 when key is set."""
    audio = tmp_path / "Stay Alive.flac"
    audio.write_bytes(MINIMAL_FLAC_BYTES)

    def _acoustid_handler(request: httpx.Request) -> httpx.Response:
        assert "client=test-acoustid-key" in str(request.url)
        return httpx.Response(
            status_code=200,
            json={
                "status": "ok",
                "results": [
                    {
                        "id": "acoustid-1",
                        "score": 0.98,
                        "recordings": [{"id": "rec-mbid-stay-alive"}],
                    }
                ],
            },
        )

    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(b'{"duration": 267.4, "fingerprint": "AQADtEmyJJFStB=="}', b"")
    )

    fp_service = ChromaprintFingerprinter(
        fpcalc_binary="fpcalc",
        acoustid_api_key="test-acoustid-key",
        transport=httpx.MockTransport(_acoustid_handler),
    )

    async def _run() -> AudioFingerprint:
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            return await fp_service.compute_fingerprint(audio)

    result = asyncio.run(_run())
    assert result.duration_seconds == 267
    assert result.fingerprint == "AQADtEmyJJFStB=="
    assert result.acoustid_mbid == "rec-mbid-stay-alive"


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

    emilia_meta = parse_filename_metadata("01 - EMILIA(CV:RIE TAKAHASHI) – Stay Alive")
    assert emilia_meta.track_number == 1
    assert emilia_meta.artist == "Rie Takahashi"
    assert emilia_meta.title == "Stay Alive"

    disc_meta = parse_filename_metadata("2-05 - Artist Name — Song Title")
    assert disc_meta.disc_number == 2
    assert disc_meta.track_number == 5
    assert disc_meta.artist == "Artist Name"
    assert disc_meta.title == "Song Title"


def test_mutagen_tag_reading_and_musicbrainz_candidate_ranking(tmp_path: Path) -> None:
    """Verify `MusicBrainzMetadataService` reads embedded FLAC tags via `mutagen` and ranks MB releases."""
    flac_file = tmp_path / "file_0.flac"
    flac_file.write_bytes(MINIMAL_FLAC_BYTES)
    easy = mutagen.File(flac_file, easy=True)
    assert easy is not None
    easy["title"] = ["Stay Alive"]
    easy["artist"] = ["EMILIA(CV:RIE TAKAHASHI)"]
    easy["album"] = ["Stay Alive"]
    easy["date"] = ["2016-08-24"]
    easy["tracknumber"] = ["2/4"]
    easy["discnumber"] = ["1/1"]
    easy["genre"] = ["Anime"]
    easy.save()

    def _fake_mb_search(
        meta: TrackMetadata,
        fp: AudioFingerprint | None,
    ) -> list[dict[str, object]]:
        return [
            {
                "id": "mb-rec-stay-alive-1",
                "title": "Stay Alive",
                "artist-credit-phrase": "Emilia (CV: Rie Takahashi)",
                "length": "267000",
                "ext:score": "100",
                "release-list": [
                    {
                        "id": "mb-rel-2016",
                        "title": "Stay Alive",
                        "date": "2016-08-24",
                        "release-group": {"primary-type": "Single"},
                        "medium-list": [
                            {
                                "position": "1",
                                "track-list": [{"number": "2"}],
                            }
                        ],
                    },
                    {
                        "id": "mb-rel-ost",
                        "title": "Re:Zero Original Soundtrack",
                        "date": "2016-10-26",
                        "disambiguation": "Limited Edition",
                        "release-group": {"primary-type": "Album"},
                    },
                ],
            }
        ]

    service = MusicBrainzMetadataService(search_fn=_fake_mb_search)
    local_meta = asyncio.run(service.read_local_metadata(flac_file))
    assert local_meta.title == "Stay Alive"
    assert local_meta.artist == "EMILIA(CV:RIE TAKAHASHI)"
    assert local_meta.album == "Stay Alive"
    assert local_meta.year == 2016
    assert local_meta.track_number == 2
    assert local_meta.disc_number == 1
    assert local_meta.genre == "Anime"
    assert local_meta.duration_seconds == 6

    candidates = asyncio.run(
        service.find_candidates(
            local_meta,
            AudioFingerprint(duration_seconds=267, fingerprint="FP_STAY_ALIVE"),
        )
    )
    assert len(candidates) == 2
    top = candidates[0]
    assert top.metadata.artist == "Rie Takahashi"
    assert top.metadata.title == "Stay Alive"
    assert top.metadata.album == "Stay Alive"
    assert top.metadata.year == 2016
    assert top.metadata.release_mbid == "mb-rel-2016"
    assert top.confidence >= 0.90
    assert "Rie Takahashi - Stay Alive | Stay Alive (2016)" in top.display_label


def test_library_organizer_sanitizes_paths_writes_mutagen_tags_and_lrc(tmp_path: Path) -> None:
    """Verify `LibraryOrganizer` sanitizes paths, writes enriched `mutagen` tags, and writes `.lrc`."""
    organizer = LibraryOrganizer(library_root=tmp_path / "library")
    meta = TrackMetadata(
        title="Stay Alive",
        artist="Emilia (CV: Rie Takahashi)",
        album="Stay Alive",
        album_artist="Emilia (CV: Rie Takahashi)",
        year=2016,
        track_number=1,
        disc_number=1,
        genre="Anime",
        recording_mbid="rec-mbid-1",
        release_mbid="rel-mbid-1",
    )
    target_path = organizer.build_target_audio_path(meta, ".FLAC")
    assert target_path.suffix == ".flac"
    assert target_path.parent.name == "Stay Alive (2016)"
    assert target_path.parent.parent.name == "Emilia (CV_ Rie Takahashi)"
    assert target_path.name == "01 - Stay Alive.flac"

    source_file = tmp_path / "raw.flac"
    source_file.write_bytes(MINIMAL_FLAC_BYTES)
    lyrics = LyricsData(synced_lyrics="[00:00.50] I stay alive")

    final_path = asyncio.run(organizer.finalize_track(source_file, meta, lyrics))
    assert final_path.is_file()
    lrc_file = final_path.with_suffix(".lrc")
    assert lrc_file.is_file()
    assert "I stay alive" in lrc_file.read_text(encoding="utf-8")

    # Verify enriched tags and embedded LYRICS were written into the FLAC file by mutagen
    written_easy = mutagen.File(final_path, easy=True)
    assert written_easy is not None
    assert written_easy["title"] == ["Stay Alive"]
    assert written_easy["artist"] == ["Emilia (CV: Rie Takahashi)"]
    assert written_easy["album"] == ["Stay Alive"]
    assert written_easy["date"] == ["2016"]
    assert written_easy["tracknumber"] == ["1"]

    written_raw = mutagen.File(final_path)
    assert written_raw is not None
    assert written_raw["LYRICS"] == ["[00:00.50] I stay alive"]


def test_cv_artist_variants_and_manual_search_url_mbid_parsing() -> None:
    """Verify `extract_artist_search_variants` and `parse_manual_search_query` for URLs, MBIDs, and CVs."""
    variants = extract_artist_search_variants("EMILIA(CV:RIE TAKAHASHI)")
    assert "EMILIA(CV:RIE TAKAHASHI)" in variants
    assert "Rie Takahashi" in variants
    assert "EMILIA" in variants

    compact_variants = extract_artist_search_variants("EMILIA(CVRIE TAKAHASHI)")
    assert "Rie Takahashi" in compact_variants
    assert "EMILIA" in compact_variants

    jp_variants = extract_artist_search_variants("エミリア（CV：高橋李依）")
    assert "高橋李依" in jp_variants
    assert "エミリア" in jp_variants

    base = TrackMetadata(
        title="Stay Alive",
        artist="EMILIA(CV:RIE TAKAHASHI)",
        album="TVアニメ「Re:ゼロから始める異世界生活」後期エンディングテーマ「Stay Alive」",
        year=2016,
        track_number=1,
        isrc="JPD731600777",
    )

    # 1. MusicBrainz Release URL
    rel_meta = parse_manual_search_query(
        "https://musicbrainz.org/release/ca344c5b-9c69-4283-8383-e7c4c026d571",
        base_metadata=base,
    )
    assert rel_meta.release_mbid == "ca344c5b-9c69-4283-8383-e7c4c026d571"
    assert rel_meta.title == "Stay Alive"

    # 2. MusicBrainz Recording URL
    rec_meta = parse_manual_search_query(
        "https://musicbrainz.org/recording/38b27632-eaa9-4598-bfd1-2a973636d09f",
        base_metadata=base,
    )
    assert rec_meta.recording_mbid == "38b27632-eaa9-4598-bfd1-2a973636d09f"

    # 3. Bare MBID UUID
    bare_uuid = parse_manual_search_query(
        "ca344c5b-9c69-4283-8383-e7c4c026d571",
        base_metadata=base,
    )
    assert bare_uuid.release_mbid == "ca344c5b-9c69-4283-8383-e7c4c026d571"

    # 4. ISRC code
    isrc_meta = parse_manual_search_query("isrc:jpd73-16-00777", base_metadata=base)
    assert isrc_meta.isrc == "JPD731600777"

    # 5. Single voice-actor name overrides artist while keeping known track title
    cv_only_meta = parse_manual_search_query("Rie Takahashi", base_metadata=base)
    assert cv_only_meta.artist == "Rie Takahashi"
    assert cv_only_meta.title == "Stay Alive"
    assert cv_only_meta.year == 2016
    assert cv_only_meta.track_number == 1

    # 6. Key-value override
    kv_meta = parse_manual_search_query("artist: Rie Takahashi | album: Stay Alive", base_metadata=base)
    assert kv_meta.artist == "Rie Takahashi"
    assert kv_meta.album == "Stay Alive"
    assert kv_meta.title == "Stay Alive"


def test_musicbrainz_english_alias_reconstruction_and_compilation_demotion() -> None:
    """Verify Japanese recordings reconstruct English aliases and rank original Singles above Compilations."""
    local_meta = TrackMetadata(
        title="Stay Alive",
        artist="EMILIA(CV:RIE TAKAHASHI)",
        album="TVアニメ「Re:ゼロから始める異世界生活」後期エンディングテーマ「Stay Alive」",
        year=2016,
        track_number=1,
        isrc="JPD731600777",
    )

    # Simulate MusicBrainz returning 5 compilation releases before the actual 2016 Single at index 5
    compilation_releases: list[dict[str, object]] = [
        {
            "id": f"comp-rel-{idx}",
            "title": f"HIGE DRIVER BEST Vol. {idx}",
            "date": "2020-09-30",
            "release-group": {
                "primary-type": "Album",
                "secondary-type-list": ["Compilation"],
            },
            "medium-list": [{"position": "1", "track-list": [{"number": "9"}]}],
        }
        for idx in range(5)
    ]
    canonical_single: dict[str, object] = {
        "id": "ca344c5b-9c69-4283-8383-e7c4c026d571",
        "title": "Stay Alive",
        "date": "2016-08-24",
        "release-group": {"primary-type": "Single"},
        "medium-list": [{"position": "1", "track-list": [{"number": "1"}]}],
    }

    def _fake_mb_search(
        meta: TrackMetadata,
        fp: AudioFingerprint | None,
    ) -> list[dict[str, object]]:
        return [
            {
                "id": "38b27632-eaa9-4598-bfd1-2a973636d09f",
                "title": "Stay Alive",
                "artist-credit-phrase": "エミリア(CV:高橋李依)",
                "artist-credit": [
                    {
                        "name": "エミリア",
                        "artist": {
                            "id": "e6f2753b-7552-40b2-a1a7-e0f5aa6c1f2e",
                            "name": "エミリア",
                            "sort-name": "Emilia",
                            "alias-list": [{"alias": "Emilia", "locale": "en", "primary": "primary"}],
                        },
                    },
                    "(CV:",
                    {
                        "name": "高橋李依",
                        "artist": {
                            "id": "10b02e83-6050-4682-abe9-fe8032a1074a",
                            "name": "高橋李依",
                            "sort-name": "Takahashi, Rie",
                            "alias-list": [{"alias": "Rie Takahashi", "locale": "en", "primary": "primary"}],
                        },
                    },
                    ")",
                ],
                "length": "267466",
                "ext:score": "100",
                "release-list": [*compilation_releases, canonical_single],
            }
        ]

    service = MusicBrainzMetadataService(search_fn=_fake_mb_search)
    candidates = asyncio.run(service.find_candidates(local_meta))
    assert candidates
    top = candidates[0]
    assert top.metadata.release_mbid == "ca344c5b-9c69-4283-8383-e7c4c026d571"
    assert top.metadata.album == "Stay Alive"
    assert top.metadata.artist == "Rie Takahashi"
    assert top.metadata.track_number == 1
    assert top.confidence >= 0.95
    assert top.display_label.startswith("[")
    # Also offers the original Japanese voice actor script candidate
    assert any(c.metadata.artist == "高橋李依" for c in candidates)


def test_musicbrainz_release_mbid_lookup_and_artist_arid_resolution() -> None:
    """Verify `get_release_by_id` URL resolution and `search_artists` -> `arid` fallback."""
    service = MusicBrainzMetadataService(enable_network=True)

    # 1. Direct release MBID lookup (e.g. user pasted https://musicbrainz.org/release/ca344c5b-...)
    rel_payload = {
        "release": {
            "id": "ca344c5b-9c69-4283-8383-e7c4c026d571",
            "title": "Stay Alive",
            "date": "2016-08-24",
            "artist-credit-phrase": "Emilia (CV: Rie Takahashi)",
            "release-group": {"primary-type": "Single"},
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [
                        {
                            "number": "1",
                            "title": "Stay Alive",
                            "length": "267000",
                            "recording": {
                                "id": "38b27632-eaa9-4598-bfd1-2a973636d09f",
                                "title": "Stay Alive",
                            },
                        }
                    ],
                }
            ],
        }
    }
    rel_query = parse_manual_search_query(
        "https://musicbrainz.org/release/ca344c5b-9c69-4283-8383-e7c4c026d571",
        base_metadata=TrackMetadata(
            title="Stay Alive",
            artist="EMILIA(CV:RIE TAKAHASHI)",
            album="Unknown Album",
            track_number=1,
        ),
    )
    with patch("musicbrainzngs.get_release_by_id", return_value=rel_payload) as mock_rel:
        rel_candidates = asyncio.run(service.find_candidates(rel_query))
        mock_rel.assert_called_once()
        assert rel_candidates
        assert rel_candidates[0].metadata.release_mbid == "ca344c5b-9c69-4283-8383-e7c4c026d571"
        assert rel_candidates[0].metadata.recording_mbid == "38b27632-eaa9-4598-bfd1-2a973636d09f"

    # 2. Artist alias -> arid fallback when direct artist-credit search returns empty
    def _fake_search_recordings(**kwargs: object) -> dict[str, object]:
        q = str(kwargs.get("query", ""))
        if "arid:10b02e83-6050-4682-abe9-fe8032a1074a" in q:
            return {
                "recording-list": [
                    {
                        "id": "38b27632-eaa9-4598-bfd1-2a973636d09f",
                        "title": "Stay Alive",
                        "artist-credit-phrase": "Emilia (CV: Rie Takahashi)",
                        "ext:score": "100",
                        "release-list": [
                            {
                                "id": "ca344c5b-9c69-4283-8383-e7c4c026d571",
                                "title": "Stay Alive",
                                "date": "2016-08-24",
                            }
                        ],
                    }
                ]
            }
        return {"recording-list": []}

    fake_artists = {
        "artist-list": [
            {
                "id": "10b02e83-6050-4682-abe9-fe8032a1074a",
                "name": "高橋李依",
                "sort-name": "Takahashi, Rie",
            }
        ]
    }
    with (
        patch("musicbrainzngs.search_recordings", side_effect=_fake_search_recordings),
        patch("musicbrainzngs.search_artists", return_value=fake_artists),
    ):
        arid_candidates = asyncio.run(
            service.find_candidates(
                TrackMetadata(
                    title="Stay Alive",
                    artist="EMILIA(CV:RIE TAKAHASHI)",
                    album="Stay Alive",
                )
            )
        )
        assert arid_candidates
        assert arid_candidates[0].metadata.recording_mbid == "38b27632-eaa9-4598-bfd1-2a973636d09f"


def test_synced_lyrics_embedding_and_cover_art_extraction_and_fetching(tmp_path: Path) -> None:
    """Verify synced lyrics tags (FLAC & MP3 SYLT), cover.jpg extraction/CAA fetch, and tag corrections."""
    lrc_text = "[00:01.50] First line\n[00:04.250] Second line\n[01:02:03.00] Late line"
    parsed_lines = parse_lrc_timestamps(lrc_text)
    assert parsed_lines == [
        ("First line", 1500),
        ("Second line", 4250),
        ("Late line", 3723000),
    ]

    # 1. FLAC with embedded Picture + synced & plain lyrics -> writes SYNCEDLYRICS and cover.jpg
    flac_src = tmp_path / "embedded_art.flac"
    flac_src.write_bytes(MINIMAL_FLAC_BYTES)
    flac_obj = FLAC(flac_src)
    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.data = b"\xff\xd8\xff\xe0JFIF-embedded-cover-bytes"
    flac_obj.add_picture(pic)
    flac_obj.save()

    organizer = LibraryOrganizer(library_root=tmp_path / "lib")
    meta = TrackMetadata(
        title="Stay Alive",
        artist="Emilia (CV: Rie Takahashi)",
        album="Stay Alive",
        year=2016,
        track_number=1,
    )
    lyrics = LyricsData(
        synced_lyrics="[00:01.50] I stay alive\n[00:05.00] In the dark",
        plain_lyrics="I stay alive\nIn the dark",
    )
    final_flac = asyncio.run(organizer.finalize_track(flac_src, meta, lyrics))
    assert final_flac.is_file()
    cover_jpg = final_flac.parent / "cover.jpg"
    assert cover_jpg.is_file()
    assert cover_jpg.read_bytes() == b"\xff\xd8\xff\xe0JFIF-embedded-cover-bytes"

    written_flac = FLAC(final_flac)
    assert written_flac["SYNCEDLYRICS"] == [lyrics.synced_lyrics]
    assert written_flac["LYRICS"] == [lyrics.synced_lyrics]
    assert written_flac["UNSYNCEDLYRICS"] == [lyrics.plain_lyrics]

    # Apply post-ingestion tag correction on the finalized FLAC file
    updated_ok = asyncio.run(
        organizer.apply_tag_correction(
            audio_path=final_flac,
            title="Stay Alive (TV Size)",
            artist="Emilia",
            album="Re:Zero ED2",
            year=2017,
        )
    )
    assert updated_ok is True
    corrected_easy = mutagen.File(final_flac, easy=True)
    assert corrected_easy is not None
    assert corrected_easy["title"] == ["Stay Alive (TV Size)"]
    assert corrected_easy["artist"] == ["Emilia"]
    assert corrected_easy["album"] == ["Re:Zero ED2"]
    assert corrected_easy["date"] == ["2017"]

    # 2. MP3 ID3 SYLT/USLT/TXXX synced lyrics embedding + Cover Art Archive HTTP fallback
    mp3_src = tmp_path / "song.mp3"
    # Minimal valid MPEG1 Layer3 frame header so mutagen.id3 can attach an ID3 header
    mp3_src.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 413)
    id3_init = ID3()
    id3_init.save(mp3_src)

    def _caa_handler(request: httpx.Request) -> httpx.Response:
        if "coverartarchive.org/release/ca344c5b-9c69-4283-8383-e7c4c026d571/front" in str(request.url):
            return httpx.Response(status_code=200, content=b"\xff\xd8\xff\xe0CAA-COVER-ART")
        return httpx.Response(status_code=404)

    caa_organizer = LibraryOrganizer(
        library_root=tmp_path / "lib_caa",
        transport=httpx.MockTransport(_caa_handler),
    )
    mp3_meta = TrackMetadata(
        title="Wishing",
        artist="Rem (CV: Inori Minase)",
        album="Wishing",
        year=2016,
        track_number=2,
        release_mbid="ca344c5b-9c69-4283-8383-e7c4c026d571",
    )
    final_mp3 = asyncio.run(caa_organizer.finalize_track(mp3_src, mp3_meta, lyrics))
    assert final_mp3.is_file()
    caa_cover = final_mp3.parent / "cover.jpg"
    assert caa_cover.is_file()
    assert caa_cover.read_bytes() == b"\xff\xd8\xff\xe0CAA-COVER-ART"

    written_id3 = ID3(final_mp3)
    sylt_frames = written_id3.getall("SYLT")
    assert len(sylt_frames) == 1
    assert sylt_frames[0].text == [("I stay alive", 1500), ("In the dark", 5000)]
    uslt_frames = written_id3.getall("USLT")
    assert len(uslt_frames) == 1

    # 3. Staged cover image in archive/scratch directory takes priority
    scratch_dir = tmp_path / "scratch_cover"
    scratch_dir.mkdir()
    staged_flac = scratch_dir / "01 - Track.flac"
    staged_flac.write_bytes(MINIMAL_FLAC_BYTES)
    staged_cover = scratch_dir / "front.jpg"
    staged_cover.write_bytes(b"\xff\xd8\xff\xe0STAGED-FRONT-JPG")
    assert find_best_cover_image(scratch_dir) == staged_cover

    staged_meta = TrackMetadata(title="Track", artist="Artist", album="Staged Album", year=2024)
    final_staged = asyncio.run(organizer.finalize_track(staged_flac, staged_meta))
    assert (final_staged.parent / "cover.jpg").read_bytes() == b"\xff\xd8\xff\xe0STAGED-FRONT-JPG"


def test_fetch_release_tracks_medium_list_mapping() -> None:
    """Verify `MusicBrainzMetadataService.fetch_release_tracks` maps all tracks from `medium-list`."""
    release_payload: dict[str, object] = {
        "id": "ca344c5b-9c69-4283-8383-e7c4c026d571",
        "title": "Stay Alive",
        "date": "2016-08-24",
        "artist-credit-phrase": "エミリア(CV:高橋李依)",
        "artist-credit": [
            {
                "name": "エミリア",
                "artist": {
                    "name": "エミリア",
                    "sort-name": "Emilia",
                    "alias-list": [{"alias": "Emilia", "locale": "en", "primary": "primary"}],
                },
            },
            "(CV:",
            {
                "name": "高橋李依",
                "artist": {
                    "name": "高橋李依",
                    "sort-name": "Takahashi, Rie",
                    "alias-list": [{"alias": "Rie Takahashi", "locale": "en", "primary": "primary"}],
                },
            },
            ")",
        ],
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {
                        "number": "1",
                        "title": "Stay Alive",
                        "length": "267000",
                        "recording": {
                            "id": "rec-mbid-1",
                            "title": "Stay Alive",
                        },
                    },
                    {
                        "number": "2",
                        "title": "Wishing",
                        "length": "360000",
                        "recording": {
                            "id": "rec-mbid-2",
                            "title": "Wishing",
                            "artist-credit-phrase": "Rem (CV: Inori Minase)",
                        },
                    },
                ],
            }
        ],
    }

    service = MusicBrainzMetadataService(release_lookup_fn=lambda _mbid: release_payload)
    tracks = asyncio.run(
        service.fetch_release_tracks(
            "ca344c5b-9c69-4283-8383-e7c4c026d571",
            prefer_english=True,
        )
    )
    assert len(tracks) == 2
    assert tracks[0].track_number == 1
    assert tracks[0].title == "Stay Alive"
    assert tracks[0].artist == "Rie Takahashi"
    assert tracks[0].recording_mbid == "rec-mbid-1"
    assert tracks[0].duration_seconds == 267
    assert tracks[1].track_number == 2
    assert tracks[1].title == "Wishing"
    assert tracks[1].artist == "Inori Minase"
    assert tracks[1].recording_mbid == "rec-mbid-2"


def test_primary_artist_character_stripping_and_bilingual_genius_lastfm_enrichment(
    tmp_path: Path,
) -> None:
    """Verify `normalize_primary_artist`, `extract_bilingual_english`, Genius/Last.fm, and LRCLIB search."""
    # 1. Collaboration & Character/CV artist normalization
    assert normalize_primary_artist("Rkomi con Elodie") == "Rkomi"
    assert normalize_primary_artist("Rkomi feat. Elodie") == "Rkomi"
    assert normalize_primary_artist("Yotsugi Ononoki(Saori Hayami)") == "Saori Hayami"
    assert normalize_primary_artist("Ononoki Yotsugi (CV: Hayami Saori)") == "Hayami Saori"
    assert normalize_primary_artist("EMILIA(CV:RIE TAKAHASHI)") == "Rie Takahashi"
    assert (
        normalize_primary_artist("斧乃木余接(CV.早見沙織) (Yotsugi Ononoki (CV. Saori Hayami))")
        == "Saori Hayami"
    )
    assert normalize_primary_artist("ClariS") == "ClariS"

    # 2. Bilingual & OP/ED title extraction
    assert extract_bilingual_english("オレンジミント (Orange Mint)") == "Orange Mint"
    assert extract_bilingual_english("歌物語 2 (Uta Monogatari 2)") == "Uta Monogatari 2"
    assert extract_title_search_variants("Tsukimonogatari OP Orange Mint")[0] == "Orange Mint"
    assert extract_title_search_variants("Tsukimonogatari ED border")[0] == "border"

    # 3. MusicBrainz Character type stripping + legal-name alias avoidance + Genius/Last.fm enrichment
    def _mb_orange_mint_and_rkomi(
        meta: TrackMetadata,
        fp: AudioFingerprint | None,
    ) -> list[dict[str, object]]:
        if "coda" in meta.title.lower():
            return [
                {
                    "id": "265a1eca-4f5a-4bee-b441-bc0994ca2d9c",
                    "title": "La coda del diavolo",
                    "artist-credit-phrase": "Rkomi con Elodie",
                    "artist-credit": [
                        {
                            "artist": {
                                "name": "Rkomi",
                                "type": "Person",
                                "alias-list": [{"alias": "Mirko Manuele Martorana", "type": "Legal name"}],
                            }
                        },
                        " con ",
                        {
                            "artist": {
                                "name": "Elodie",
                                "type": "Person",
                                "alias-list": [{"alias": "Elodie Di Patrizi", "type": "Legal name"}],
                            }
                        },
                    ],
                    "ext:score": "100",
                    "release-list": [
                        {
                            "id": "07547216-8ea5-4f98-afb5-1aae1dba3b24",
                            "title": "Taxi Driver",
                            "date": "2021-11-30",
                            "medium-list": [{"position": "1", "track-list": [{"number": "2"}]}],
                        }
                    ],
                }
            ]
        return [
            {
                "id": "37d2ca56-6f5e-455c-b93a-e880d5f3bc1f",
                "title": "オレンジミント",
                "artist-credit-phrase": "斧乃木余接(早見沙織)",
                "artist-credit": [
                    {
                        "artist": {
                            "type": "Character",
                            "name": "斧乃木余接",
                            "alias-list": [
                                {"alias": "Yotsugi Ononoki", "locale": "en", "primary": "primary"}
                            ],
                        }
                    },
                    "(",
                    {
                        "artist": {
                            "type": "Person",
                            "name": "早見沙織",
                            "sort-name": "Hayami, Saori",
                            "alias-list": [{"alias": "Saori Hayami", "locale": "en", "primary": "primary"}],
                        }
                    },
                    ")",
                ],
                "ext:score": "100",
                "release-list": [
                    {
                        "id": "3e1c38ff-7ced-4517-b10d-713f42896c6f",
                        "title": "オレンジミント",
                        "date": "2015-05-07",
                        "medium-list": [{"position": "1", "track-list": [{"number": "1"}]}],
                    }
                ],
            }
        ]

    def _http_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "genius.com" in url and "/songs/" in url:
            return httpx.Response(
                status_code=200,
                json={
                    "response": {
                        "song": {
                            "album": {"name": "オレンジミント (Orange Mint)"},
                            "release_date_components": {"year": 2015},
                        }
                    }
                },
            )
        if "genius.com" in url:
            return httpx.Response(
                status_code=200,
                json={
                    "response": {
                        "sections": [
                            {
                                "hits": [
                                    {
                                        "result": {
                                            "id": 6528618,
                                            "title": "オレンジミント (Orange Mint)",
                                            "primary_artist": {
                                                "name": (
                                                    "斧乃木余接(CV.早見沙織) "
                                                    "(Yotsugi Ononoki (CV. Saori Hayami))"
                                                )
                                            },
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
        if "audioscrobbler.com" in url:
            return httpx.Response(
                status_code=200,
                json={
                    "results": {
                        "trackmatches": {"track": [{"name": "Orange Mint", "artist": "Saori Hayami"}]}
                    }
                },
            )
        if "lrclib.net/api/get" in url:
            return httpx.Response(status_code=404)
        if "lrclib.net/api/search" in url:
            return httpx.Response(
                status_code=200,
                json=[
                    {
                        "trackName": "Orange Mint",
                        "artistName": "斧乃木余接(早見沙織)",
                        "duration": 275,
                        "syncedLyrics": "[00:01.00] Mint lyrics",
                        "plainLyrics": "Mint lyrics",
                        "instrumental": False,
                    }
                ],
            )
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(_http_handler)
    service = MusicBrainzMetadataService(
        lastfm_api_key="test-lfm-key",
        search_fn=_mb_orange_mint_and_rkomi,
        http_transport=transport,
    )

    # Test Rkomi con Elodie -> Rkomi (no legal name alias Mirko Manuele Martorana)
    rkomi_cands = asyncio.run(
        service.find_candidates(
            TrackMetadata(title="La coda del diavolo", artist="Rkomi con Elodie", album="Taxi Driver")
        )
    )
    assert len(rkomi_cands) == 1
    assert rkomi_cands[0].metadata.artist == "Rkomi"
    assert rkomi_cands[0].metadata.album_artist == "Rkomi"
    assert rkomi_cands[0].confidence == 1.0

    # Test Yotsugi Ononoki(Saori Hayami) -> Saori Hayami - Orange Mint
    om_cands = asyncio.run(
        service.find_candidates(
            TrackMetadata(
                title="オレンジミント",
                artist="Yotsugi Ononoki(Saori Hayami)",
                album="オレンジミント",
            )
        )
    )
    assert om_cands
    assert om_cands[0].metadata.artist == "Saori Hayami"
    assert om_cands[0].metadata.title == "Orange Mint"
    assert om_cands[0].metadata.album == "Orange Mint"

    # 4. LRCLIB `/search` fallback when `/get` returns 404
    lrc_client = LrclibLyricsClient(transport=transport)
    om_lyrics = asyncio.run(
        lrc_client.fetch_lyrics(
            TrackMetadata(
                title="Orange Mint",
                artist="Saori Hayami",
                album="Orange Mint",
                duration_seconds=275,
            )
        )
    )
    assert om_lyrics is not None
    assert om_lyrics.has_synced is True
    assert "Mint lyrics" in (om_lyrics.synced_lyrics or "")

    # 5. Plain lyrics & pre-existing embedded lyrics write `.lrc` sidecar
    organizer = LibraryOrganizer(library_root=tmp_path / "lib_lrc")
    src_flac = tmp_path / "plain.flac"
    src_flac.write_bytes(MINIMAL_FLAC_BYTES)
    plain_only = LyricsData(plain_lyrics="Plain unsynced lyrics line")
    final_plain = asyncio.run(
        organizer.finalize_track(
            src_flac,
            TrackMetadata(title="La coda del diavolo", artist="Rkomi", album="Taxi Driver", year=2021),
            plain_only,
        )
    )
    assert final_plain.with_suffix(".lrc").is_file()
    assert final_plain.with_suffix(".lrc").read_text(encoding="utf-8") == "Plain unsynced lyrics line"
