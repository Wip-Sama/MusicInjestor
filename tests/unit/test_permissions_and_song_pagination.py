"""Unit tests for file permission enforcement, library pagination, sorting, and keyboard navigation."""

import stat
from pathlib import Path

import pytest

from src.bot.keyboards.songs import (
    SORT_DESCRIPTIONS,
    SORT_LABELS,
    SongsPageCallback,
    SongsSortCallback,
    build_songs_search_menu_keyboard,
    build_songs_sort_keyboard,
)
from src.core.models import (
    LibraryTrackRecord,
)
from src.core.organizer import (
    LibraryOrganizer,
    _parse_umask_mode,
)
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository


def test_parse_umask_mode() -> None:
    """Verify umask string and int parsing with valid and fallback values."""
    assert _parse_umask_mode("0022") == 0o022
    assert _parse_umask_mode("022") == 0o022
    assert _parse_umask_mode("0002") == 0o002
    assert _parse_umask_mode("0") == 0
    assert _parse_umask_mode(0o022) == 0o022
    # Invalid fallback returns default 0o022
    assert _parse_umask_mode("invalid") == 0o022


@pytest.mark.anyio
async def test_file_and_dir_permissions_applied(tmp_path: Path) -> None:
    """Verify apply_file_permissions and apply_dir_permissions set readable permissions."""
    organizer = LibraryOrganizer(library_root=tmp_path, umask="0022", enable_network=False)

    test_dir = tmp_path / "Artist" / "Album"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "01 - Title.mp3"
    test_file.write_bytes(b"dummy audio content")

    organizer.apply_dir_permissions(test_dir)
    organizer.apply_file_permissions(test_file)

    # File and dir exist and are accessible
    assert test_file.is_file()
    assert test_dir.is_dir()
    mode = stat.S_IMODE(test_file.stat().st_mode)
    # File must at least have read permissions for user
    assert mode & stat.S_IREAD


@pytest.mark.anyio
async def test_fix_library_permissions_walks_and_repairs(tmp_path: Path) -> None:
    """Verify fix_library_permissions recursively updates files and directories."""
    organizer = LibraryOrganizer(library_root=tmp_path, umask="0022", enable_network=False)

    sub_dir = tmp_path / "Sub" / "Album"
    sub_dir.mkdir(parents=True, exist_ok=True)
    file1 = sub_dir / "track1.flac"
    file2 = sub_dir / "track2.mp3"
    sidecar = sub_dir / "track1.lrc"
    cover = sub_dir / "cover.jpg"

    file1.write_bytes(b"data1")
    file2.write_bytes(b"data2")
    sidecar.write_text("[00:01.00] test", encoding="utf-8")
    cover.write_bytes(b"fake image")

    repaired_count = await organizer.fix_library_permissions()
    assert repaired_count >= 1


@pytest.mark.anyio
async def test_repository_pagination_counting_and_sorting(tmp_path: Path) -> None:
    """Verify SqliteCatalogRepository pagination, count, and sorting by artist, title, album, year."""
    db_path = tmp_path / "test_repo.db"
    manager = SqliteDatabaseManager(db_path)
    await manager.initialize()
    repo = SqliteCatalogRepository(manager)

    # Initial count is 0
    assert await repo.count_library_tracks() == 0

    tracks_to_insert = [
        LibraryTrackRecord(
            id="trk-001",
            job_id="job-001",
            title="Bravo",
            artist="Charlie",
            album="Echo",
            year=2021,
            track_number=1,
            duration_seconds=180,
            storage_path=tmp_path / "Charlie" / "Echo" / "01 - Bravo.flac",
            fingerprint="FP_001",
            uploaded_by=123456,
        ),
        LibraryTrackRecord(
            id="trk-002",
            job_id="job-002",
            title="Alpha",
            artist="Delta",
            album="Foxtrot",
            year=2015,
            track_number=2,
            duration_seconds=200,
            storage_path=tmp_path / "Delta" / "Foxtrot" / "02 - Alpha.flac",
            fingerprint="FP_002",
            uploaded_by=123456,
        ),
        LibraryTrackRecord(
            id="trk-003",
            job_id="job-003",
            title="Charlie",
            artist="Alpha",
            album="AlphaAlbum",
            year=2024,
            track_number=3,
            duration_seconds=220,
            storage_path=tmp_path / "Alpha" / "AlphaAlbum" / "03 - Charlie.flac",
            fingerprint="FP_003",
            uploaded_by=123456,
        ),
    ]

    for trk in tracks_to_insert:
        await repo.upsert_scanned_library_track(trk)

    # Total count
    assert await repo.count_library_tracks() == 3

    # Count with search query
    assert await repo.count_library_tracks("Alpha") == 2  # matches trk-002 (title) and trk-003 (artist/album)
    assert await repo.count_library_tracks("Bravo") == 1
    assert await repo.count_library_tracks("Nonexistent") == 0

    # Pagination: limit=2, offset=0
    page1 = await repo.list_library_tracks(limit=2, offset=0, sort_by="artist")
    assert len(page1) == 2
    assert page1[0].artist == "Alpha"
    assert page1[1].artist == "Charlie"

    # Pagination: limit=2, offset=2
    page2 = await repo.list_library_tracks(limit=2, offset=2, sort_by="artist")
    assert len(page2) == 1
    assert page2[0].artist == "Delta"

    # Sorting: title asc
    by_title = await repo.list_library_tracks(limit=10, offset=0, sort_by="title")
    assert [t.title for t in by_title] == ["Alpha", "Bravo", "Charlie"]

    # Sorting: year desc
    by_year = await repo.list_library_tracks(limit=10, offset=0, sort_by="year_desc")
    assert [t.year for t in by_year] == [2024, 2021, 2015]

    # Sorting: album asc
    by_album = await repo.list_library_tracks(limit=10, offset=0, sort_by="album")
    assert [t.album for t in by_album] == ["AlphaAlbum", "Echo", "Foxtrot"]


def test_build_songs_sort_keyboard() -> None:
    """Verify build_songs_sort_keyboard produces buttons for all valid sorts with active indicators."""
    kb = build_songs_sort_keyboard(current_sort="title")
    assert kb.inline_keyboard

    # Must contain title with active mark
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    title_btn = next((b for b in buttons if SORT_DESCRIPTIONS["title"] in b.text), None)
    assert title_btn is not None
    assert title_btn.text.startswith("✅")

    recent_btn = next((b for b in buttons if SORT_DESCRIPTIONS["recent"] in b.text), None)
    assert recent_btn is not None
    assert not recent_btn.text.startswith("✅")


def test_build_songs_search_menu_keyboard_pagination(tmp_path: Path) -> None:
    """Verify pagination rows and controls in build_songs_search_menu_keyboard."""
    tracks = [
        LibraryTrackRecord(
            id=f"trk-{i:03d}",
            job_id=f"job-{i:03d}",
            title=f"Song {i}",
            artist=f"Artist {i}",
            album="Album",
            storage_path=tmp_path / f"{i}.mp3",
            fingerprint=f"FP_{i}",
            duration_seconds=180,
            uploaded_by=123456,
        )
        for i in range(1, 5)
    ]

    # Single page (total_pages=1): no pagination row
    kb_single = build_songs_search_menu_keyboard(
        tracks,
        page=1,
        total_pages=1,
        total_count=4,
        sort_by="recent",
    )
    texts_single = [btn.text for row in kb_single.inline_keyboard for btn in row]
    assert not any("📄" in t for t in texts_single)

    # Multi page: page 1 of 3
    kb_p1 = build_songs_search_menu_keyboard(
        tracks,
        page=1,
        total_pages=3,
        total_count=12,
        sort_by="recent",
    )
    texts_p1 = [btn.text for row in kb_p1.inline_keyboard for btn in row]
    assert "📄 1 / 3" in texts_p1
    assert any("Next ▶" in t for t in texts_p1)
    assert not any("◀ Prev" in t for t in texts_p1)

    # Multi page: page 2 of 3 (middle page)
    kb_p2 = build_songs_search_menu_keyboard(
        tracks,
        page=2,
        total_pages=3,
        total_count=12,
        sort_by="recent",
    )
    texts_p2 = [btn.text for row in kb_p2.inline_keyboard for btn in row]
    assert "📄 2 / 3" in texts_p2
    assert any("◀ Prev" in t for t in texts_p2)
    assert any("Next ▶" in t for t in texts_p2)

    # Multi page: page 3 of 3 (last page)
    kb_p3 = build_songs_search_menu_keyboard(
        tracks,
        page=3,
        total_pages=3,
        total_count=12,
        sort_by="recent",
    )
    texts_p3 = [btn.text for row in kb_p3.inline_keyboard for btn in row]
    assert "📄 3 / 3" in texts_p3
    assert any("◀ Prev" in t for t in texts_p3)
    assert not any("Next ▶" in t for t in texts_p3)


def test_sort_callback_and_constants() -> None:
    """Verify sort callback schema and label mappings."""
    cb = SongsSortCallback(sort="artist")
    assert cb.sort == "artist"

    cb_page = SongsPageCallback(action="page", page=2, sort="year")
    assert cb_page.action == "page"
    assert cb_page.page == 2
    assert cb_page.sort == "year"

    assert "recent" in SORT_LABELS
    assert "artist" in SORT_LABELS
    assert "title" in SORT_LABELS
    assert "year" in SORT_LABELS
    assert "album" in SORT_LABELS

    for key, label in SORT_LABELS.items():
        assert isinstance(label, str)
        assert key in SORT_DESCRIPTIONS
