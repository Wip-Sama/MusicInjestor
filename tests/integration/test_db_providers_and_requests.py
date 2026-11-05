"""Integration tests for SQLite persistence of provider servers and song requests."""

from pathlib import Path

import pytest

from src.core.models import (
    ProviderAudioProfiles,
    ProviderCapabilities,
    ProviderFeatures,
    ProviderServerRecord,
    SongRequestRecord,
    SongRequestStatus,
    SongRequestType,
    UserAccount,
    UserRole,
)
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository


@pytest.mark.anyio
async def test_sqlite_provider_servers_persistence(tmp_path: Path) -> None:
    db_file = tmp_path / "test_providers.db"
    db_manager = SqliteDatabaseManager(database_path=db_file)
    await db_manager.initialize()

    repo = SqliteCatalogRepository(db_manager=db_manager)

    # 1. Upsert provider server
    caps = ProviderCapabilities(
        provider_name="qobuz-worker",
        version="1.0.0",
        supported_sources=("spotify", "qobuz", "isrc"),
        audio_profiles=ProviderAudioProfiles(supported_codecs=("flac", "mp3")),
        features=ProviderFeatures(streaming=True),
    )
    server = ProviderServerRecord(
        id="qobuz-1",
        name="Qobuz DL",
        base_url="http://127.0.0.1:8001",
        api_token_encrypted="encrypted-secret",
        priority=5,
        is_enabled=True,
        capabilities=caps,
        last_health_status="healthy",
    )
    saved = await repo.upsert_provider_server(server)
    assert saved.id == "qobuz-1"

    # 2. Get provider server
    fetched = await repo.get_provider_server("qobuz-1")
    assert fetched is not None
    assert fetched.name == "Qobuz DL"
    assert fetched.base_url == "http://127.0.0.1:8001"
    assert fetched.capabilities is not None
    assert fetched.capabilities.provider_name == "qobuz-worker"
    assert "qobuz" in fetched.capabilities.supported_sources
    assert fetched.capabilities.features.streaming is True

    # 3. List provider servers
    server2 = ProviderServerRecord(
        id="deezer-1",
        name="Deezer DL",
        base_url="http://127.0.0.1:8002",
        priority=10,
        is_enabled=False,
    )
    await repo.upsert_provider_server(server2)

    all_servers = await repo.list_provider_servers(enabled_only=False)
    assert len(all_servers) == 2
    assert all_servers[0].id == "qobuz-1"  # Priority 5 before 10

    enabled_servers = await repo.list_provider_servers(enabled_only=True)
    assert len(enabled_servers) == 1
    assert enabled_servers[0].id == "qobuz-1"

    # 4. Delete provider server
    deleted = await repo.delete_provider_server("deezer-1")
    assert deleted is True
    assert await repo.get_provider_server("deezer-1") is None


@pytest.mark.anyio
async def test_sqlite_song_requests_lifecycle_persistence(tmp_path: Path) -> None:
    db_file = tmp_path / "test_requests.db"
    db_manager = SqliteDatabaseManager(database_path=db_file)
    await db_manager.initialize()

    repo = SqliteCatalogRepository(db_manager=db_manager)

    # Need user in users table for foreign key
    user = UserAccount(telegram_id=999, display_name="Test Listener", role=UserRole.LISTENER)
    await repo.upsert_user(user)

    admin = UserAccount(telegram_id=111, display_name="Admin", role=UserRole.ADMIN)
    await repo.upsert_user(admin)

    # 1. Create song request
    req = SongRequestRecord(
        id=0,
        telegram_id=999,
        request_type=SongRequestType.TRACK,
        track_title="Never Gonna Give You Up",
        artist_name="Rick Astley",
        album_name="Whenever You Need Somebody",
        identifiers={"isrc": "USUM71703861", "deezer_id": "3135556"},
        target_quality="lossless_hi_res",
        status=SongRequestStatus.PENDING_APPROVAL,
    )
    saved_req = await repo.create_song_request(req)
    assert saved_req.id > 0
    assert saved_req.track_title == "Never Gonna Give You Up"

    # 2. Retrieve song request
    fetched = await repo.get_song_request(saved_req.id)
    assert fetched is not None
    assert fetched.artist_name == "Rick Astley"
    assert fetched.identifiers.get("isrc") == "USUM71703861"
    assert fetched.status == SongRequestStatus.PENDING_APPROVAL

    # 3. List and count
    count_pending = await repo.count_song_requests(status=SongRequestStatus.PENDING_APPROVAL)
    assert count_pending == 1

    pending_list = await repo.list_song_requests(status=SongRequestStatus.PENDING_APPROVAL)
    assert len(pending_list) == 1
    assert pending_list[0].id == saved_req.id

    # 4. Resolve song request (Approve)
    resolved = await repo.resolve_song_request(
        request_id=saved_req.id,
        status=SongRequestStatus.APPROVED,
        reviewed_by=111,
    )
    assert resolved is not None
    assert resolved.status == SongRequestStatus.APPROVED
    assert resolved.reviewed_by == 111
    assert resolved.reviewed_at is not None

    # 5. Update job and track links
    updated = await repo.update_song_request_job_and_track(
        request_id=saved_req.id,
        job_id="job_abc123",
        status=SongRequestStatus.PROVISIONING,
    )
    assert updated is not None
    assert updated.job_id == "job_abc123"
    assert updated.status == SongRequestStatus.PROVISIONING
