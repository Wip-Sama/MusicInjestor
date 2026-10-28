"""Unit tests for AlbumSearchService, gap analysis, and unified search."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.core.models import (
    DiscoveredRelease,
    LibraryTrackRecord,
    ReleaseTrackPreview,
)
from src.core.search import AlbumSearchService, _normalize_title


def _make_track(id: str, title: str, artist: str, album: str, track_number: int = 1) -> LibraryTrackRecord:
    return LibraryTrackRecord(
        id=id,
        job_id="job_1",
        uploaded_by=123,
        storage_path=Path(f"/music/{artist}/{album}/{title}.mp3"),
        fingerprint="fp123",
        duration_seconds=200.0,
        artist=artist,
        album=album,
        title=title,
        track_number=track_number,
    )


def test_normalize_title() -> None:
    assert _normalize_title("One More Time!!!") == "one more time"
    assert _normalize_title("Daft Punk - Discovery") == "daft punk  discovery"


@pytest.mark.anyio
async def test_album_gap_analysis_partial_ownership() -> None:
    mock_repo = AsyncMock()
    mock_provider = AsyncMock()

    # Library has track 1 and track 2
    lib_tracks = [
        _make_track("trk_1", "One More Time", "Daft Punk", "Discovery", 1),
        _make_track("trk_2", "Aerodynamic", "Daft Punk", "Discovery", 2),
    ]
    mock_repo.list_album_tracks.return_value = lib_tracks

    # Canonical Deezer release has 4 tracks
    canonical_tracks = [
        ReleaseTrackPreview(
            track_id="101",
            title="One More Time",
            preview_url="https://preview.deezer.com/101.mp3",
            duration_seconds=320,
            track_position=1,
        ),
        ReleaseTrackPreview(
            track_id="102",
            title="Aerodynamic",
            preview_url="https://preview.deezer.com/102.mp3",
            duration_seconds=207,
            track_position=2,
        ),
        ReleaseTrackPreview(
            track_id="103",
            title="Digital Love",
            preview_url="https://preview.deezer.com/103.mp3",
            duration_seconds=301,
            track_position=3,
        ),
        ReleaseTrackPreview(
            track_id="104",
            title="Harder, Better, Faster, Stronger",
            preview_url="https://preview.deezer.com/104.mp3",
            duration_seconds=224,
            track_position=4,
        ),
    ]
    mock_provider.get_release_track_previews.return_value = canonical_tracks

    service = AlbumSearchService(catalog_repository=mock_repo, radar_provider=mock_provider)
    gap = await service.analyze_album_gap(
        artist="Daft Punk",
        album="Discovery",
        provider_release_id="302127",
    )

    assert gap.total_tracks == 4
    assert gap.owned_tracks_count == 2
    assert gap.missing_tracks_count == 2
    assert gap.coverage_percent == 50
    assert gap.is_complete is False

    # Track 1 & 2 owned in library
    assert gap.tracks[0].is_owned is True
    assert gap.tracks[0].library_track_id == "trk_1"
    assert gap.tracks[1].is_owned is True
    assert gap.tracks[1].library_track_id == "trk_2"

    # Track 3 & 4 missing with preview URLs
    assert gap.tracks[2].is_owned is False
    assert gap.tracks[2].library_track_id is None
    assert gap.tracks[2].preview_url == "https://preview.deezer.com/103.mp3"
    assert gap.tracks[3].is_owned is False
    assert gap.tracks[3].preview_url == "https://preview.deezer.com/104.mp3"


@pytest.mark.anyio
async def test_album_gap_analysis_fallback_to_library_only() -> None:
    mock_repo = AsyncMock()
    mock_provider = AsyncMock()

    # No provider match or offline
    mock_provider.search_albums.return_value = []
    mock_provider.get_release_track_previews.return_value = []

    lib_tracks = [
        _make_track("trk_a", "Local Song A", "Indie Band", "Self Titled", 1),
        _make_track("trk_b", "Local Song B", "Indie Band", "Self Titled", 2),
    ]
    mock_repo.list_album_tracks.return_value = lib_tracks

    service = AlbumSearchService(catalog_repository=mock_repo, radar_provider=mock_provider)
    gap = await service.analyze_album_gap(artist="Indie Band", album="Self Titled")

    assert gap.total_tracks == 2
    assert gap.owned_tracks_count == 2
    assert gap.missing_tracks_count == 0
    assert gap.coverage_percent == 100
    assert gap.is_complete is True


@pytest.mark.anyio
async def test_search_library_only_scope() -> None:
    mock_repo = AsyncMock()
    mock_provider = AsyncMock()

    mock_repo.search_library_tracks.return_value = [
        _make_track("trk_1", "Get Lucky", "Daft Punk", "Random Access Memories", 8)
    ]
    mock_repo.search_library_albums.return_value = [("Daft Punk", "Random Access Memories", 1, 2013)]
    mock_repo.list_album_tracks.return_value = [
        _make_track("trk_1", "Get Lucky", "Daft Punk", "Random Access Memories", 8)
    ]
    mock_provider.search_albums.return_value = []
    mock_provider.get_release_track_previews.return_value = []

    service = AlbumSearchService(catalog_repository=mock_repo, radar_provider=mock_provider)
    result = await service.search(query="Daft Punk", scope="library")

    assert result.scope == "library"
    assert len(result.tracks) == 1
    assert result.tracks[0].title == "Get Lucky"
    assert result.tracks[0].in_library is True

    assert len(result.albums) == 1
    assert result.albums[0].title == "Random Access Memories"


@pytest.mark.anyio
async def test_search_global_scope_with_reconciliation_and_previews() -> None:
    mock_repo = AsyncMock()
    mock_provider = AsyncMock()

    # In library
    mock_repo.search_library_tracks.return_value = [
        _make_track("trk_owned", "One More Time", "Daft Punk", "Discovery", 1)
    ]
    mock_repo.search_library_albums.return_value = []
    mock_repo.list_album_tracks.return_value = [
        _make_track("trk_owned", "One More Time", "Daft Punk", "Discovery", 1)
    ]

    # In Deezer: 1 album, 2 tracks (1 owned, 1 not owned)
    mock_provider.search_albums.return_value = [
        DiscoveredRelease(
            id="deezer:302127",
            artist_name="Daft Punk",
            title="Discovery",
            release_type="album",
            release_date="2001-03-12",
            cover_url="https://cover.jpg",
            preview_url=None,
            external_url="https://deezer.com/302127",
            provider="deezer",
            provider_release_id="302127",
            track_count=14,
            in_library=False,
            created_at=datetime.now(UTC),
        )
    ]
    mock_provider.search_tracks.return_value = [
        ReleaseTrackPreview(
            track_id="101",
            title="One More Time",
            preview_url="https://preview1.mp3",
            duration_seconds=320,
            track_position=1,
        ),
        ReleaseTrackPreview(
            track_id="102",
            title="Digital Love",
            preview_url="https://preview2.mp3",
            duration_seconds=301,
            track_position=3,
        ),
    ]
    mock_provider.get_release_track_previews.return_value = [
        ReleaseTrackPreview(
            track_id="101",
            title="One More Time",
            preview_url="https://preview1.mp3",
            duration_seconds=320,
            track_position=1,
        ),
        ReleaseTrackPreview(
            track_id="102",
            title="Digital Love",
            preview_url="https://preview2.mp3",
            duration_seconds=301,
            track_position=2,
        ),
    ]

    service = AlbumSearchService(catalog_repository=mock_repo, radar_provider=mock_provider)
    result = await service.search(query="Discovery", scope="global")

    assert result.scope == "global"
    assert len(result.albums) == 1
    assert result.albums[0].title == "Discovery"
    assert result.albums[0].owned_tracks_count == 1
    assert result.albums[0].missing_tracks_count == 1

    # Tracks: One More Time owned, Digital Love missing with preview URL
    assert any(t.title == "One More Time" and t.in_library is True for t in result.tracks)
    assert any(
        t.title == "Digital Love" and t.in_library is False and t.preview_url == "https://preview2.mp3"
        for t in result.tracks
    )
