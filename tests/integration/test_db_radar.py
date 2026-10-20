"""Integration tests for Release Radar persistence in SQLite WAL database."""

from pathlib import Path

import pytest

from src.core.models import DiscoveredRelease, LibraryTrackRecord, ReleaseNotification, UserAccount, UserRole
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository


@pytest.mark.integration
@pytest.mark.anyio
async def test_sqlite_radar_follows_and_releases(tmp_path: Path) -> None:
    """Test following artists, upserting discovered releases, and notifications."""
    db_path = tmp_path / "radar_test.db"
    db_manager = SqliteDatabaseManager(db_path)
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)

    # 1. Create a user
    user = UserAccount(telegram_id=12345, display_name="Radar Fan", role=UserRole.LISTENER)
    await repo.upsert_user(user)

    # 2. Follow an artist
    followed = await repo.follow_artist(
        telegram_id=12345,
        artist_name="Dua Lipa",
        provider_artist_id="deezer_dua",
    )
    assert followed.artist_name == "Dua Lipa"
    assert followed.provider_artist_id == "deezer_dua"
    assert await repo.is_artist_followed(12345, "Dua Lipa") is True
    assert await repo.is_artist_followed(12345, "dua lipa") is True
    assert await repo.is_artist_followed(12345, "Other Artist") is False

    user_follows = await repo.list_user_followed_artists(12345)
    assert len(user_follows) == 1
    assert user_follows[0].artist_name == "Dua Lipa"

    # 3. Insert a track in library_tracks to verify in_library detection
    track_file = tmp_path / "song.mp3"
    track_file.write_bytes(b"dummy audio")
    lib_track = LibraryTrackRecord(
        id="t1",
        job_id=None,
        uploaded_by=12345,
        storage_path=track_file,
        fingerprint="fp1",
        duration_seconds=180.0,
        artist="Dua Lipa",
        album="Future Nostalgia",
        title="Levitating",
    )
    await repo.upsert_scanned_library_track(lib_track)

    # 4. Upsert discovered releases
    rel1 = DiscoveredRelease(
        id="deezer:101",
        artist_name="Dua Lipa",
        title="Future Nostalgia",
        release_type="album",
        release_date="2020-03-27",
        cover_url="https://cover.jpg",
        preview_url="https://preview.mp3",
    )
    rel2 = DiscoveredRelease(
        id="deezer:102",
        artist_name="Dua Lipa",
        title="Radical Optimism",
        release_type="album",
        release_date="2024-05-03",
        cover_url="https://cover2.jpg",
        preview_url="https://preview2.mp3",
    )

    persisted = await repo.upsert_discovered_releases([rel1, rel2])
    assert len(persisted) == 2
    # rel1 should have in_library == True because Future Nostalgia exists in library_tracks!
    assert persisted[0].in_library is True
    # rel2 is not in library
    assert persisted[1].in_library is False

    # 5. List releases and get by ID
    all_rels = await repo.list_discovered_releases()
    assert len(all_rels) == 2
    assert all_rels[0].id == "deezer:102"  # Newest date first (2024 vs 2020)

    artist_rels = await repo.list_discovered_releases(artist_name="dua lipa")
    assert len(artist_rels) == 2

    retrieved = await repo.get_discovered_release("deezer:101")
    assert retrieved is not None
    assert retrieved.title == "Future Nostalgia"

    # 6. Monitored artists: should include Dua Lipa
    monitored = await repo.list_all_monitored_artists()
    assert "Dua Lipa" in monitored

    # 7. Release notifications
    notif = ReleaseNotification(
        id=0,
        telegram_id=12345,
        release_id="deezer:102",
    )
    await repo.create_release_notifications([notif])

    user_notifs = await repo.list_user_release_notifications(12345, unread_only=True)
    assert len(user_notifs) == 1
    assert user_notifs[0][0].release_id == "deezer:102"
    assert user_notifs[0][1].title == "Radical Optimism"

    # Mark as read
    notif_id = user_notifs[0][0].id
    assert await repo.mark_notification_read(12345, notif_id) is True
    assert len(await repo.list_user_release_notifications(12345, unread_only=True)) == 0

    # 8. Unfollow artist
    assert await repo.unfollow_artist(12345, "Dua Lipa") is True
    assert await repo.is_artist_followed(12345, "Dua Lipa") is False
