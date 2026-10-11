"""Unit tests for core domain models, metadata similarity, extractor staging, and organizer."""

import asyncio
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import mutagen
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
from src.core.metadata import (
    MusicBrainzMetadataService,
    compute_metadata_similarity,
    extract_artist_search_variants,
    parse_filename_metadata,
    parse_manual_search_query,
)
from src.core.models import AudioFingerprint, LyricsData, MetadataCandidate, TrackMetadata
from src.core.organizer import LibraryOrganizer

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
    assert emilia_meta.artist == "EMILIA(CV:RIE TAKAHASHI)"
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
    assert top.metadata.artist == "Emilia (CV: Rie Takahashi)"
    assert top.metadata.title == "Stay Alive"
    assert top.metadata.album == "Stay Alive"
    assert top.metadata.year == 2016
    assert top.metadata.release_mbid == "mb-rel-2016"
    assert top.confidence >= 0.90
    assert "Emilia (CV: Rie Takahashi) - Stay Alive | Stay Alive (2016)" in top.display_label


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
    assert "RIE TAKAHASHI" in variants
    assert "EMILIA" in variants

    compact_variants = extract_artist_search_variants("EMILIA(CVRIE TAKAHASHI)")
    assert "RIE TAKAHASHI" in compact_variants
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
    assert top.metadata.artist == "Emilia (CV: Rie Takahashi)"
    assert top.metadata.track_number == 1
    assert top.confidence >= 0.95
    assert top.display_label.startswith("[")
    # Also offers the original Japanese script candidate
    assert any(c.metadata.artist == "エミリア(CV:高橋李依)" for c in candidates)


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
