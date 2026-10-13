"""Integration tests for SQLite WAL persistence (`src.db`)."""

import asyncio
from pathlib import Path

import pytest

from main import run_periodic_library_rescan_loop
from src.core.models import (
    AccessRequestStatus,
    AudioFingerprint,
    AuditAction,
    AuditLogEntry,
    IngestionJob,
    JobState,
    LibraryTrackRecord,
    MetadataCandidate,
    SourceType,
    TrackMetadata,
    UserAccount,
    UserRole,
)
from src.core.organizer import LibraryOrganizer
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository


@pytest.mark.integration
def test_sqlite_wal_initialization_and_duplicate_queries(tmp_path: Path) -> None:
    """Verify SQLite initializes in WAL mode and enforces fingerprint/MBID duplicate lookups."""
    db_path = tmp_path / "catalog.db"
    manager = SqliteDatabaseManager(db_path)
    repo = SqliteCatalogRepository(manager)

    async def _scenario() -> None:
        await manager.initialize()
        assert manager.database_path == db_path

        async with manager.connect() as conn:
            cursor = await conn.execute("PRAGMA journal_mode;")
            row = await cursor.fetchone()
            assert row is not None
            assert str(row[0]).lower() == "wal"

        empty_check = await repo.check_duplicate("FP_UNIQUE_1", "mbid-unique-1")
        assert empty_check.is_duplicate is False

        meta = TrackMetadata(
            title="Everything In Its Right Place",
            artist="Radiohead",
            album="Kid A",
            year=2000,
            track_number=1,
            recording_mbid="mbid-kid-a-01",
        )
        fp = AudioFingerprint(
            duration_seconds=251,
            fingerprint="FP_KID_A_01",
            acoustid_mbid="mbid-kid-a-01",
        )
        final_file = tmp_path / "library" / "Radiohead" / "Kid A (2000)" / "01.flac"

        await repo.register_ingested_track(
            job_id="job_kida",
            metadata=meta,
            fingerprint=fp,
            library_path=final_file,
            uploaded_by=987654321,
            has_synced_lyrics=True,
        )

        by_fp = await repo.check_duplicate("FP_KID_A_01", None)
        assert by_fp.is_duplicate is True
        assert by_fp.match_reason == "fingerprint"
        assert by_fp.existing_path == final_file

        by_mbid = await repo.check_duplicate("FP_DIFFERENT_ENCODING", "mbid-kid-a-01")
        assert by_mbid.is_duplicate is True
        assert by_mbid.match_reason == "recording_mbid"
        assert by_mbid.existing_path == final_file

        albums_count, tracks_count = await repo.count_user_uploads(987654321)
        assert albums_count == 1
        assert tracks_count == 1

    asyncio.run(_scenario())


@pytest.mark.integration
def test_sqlite_job_persistence_roundtrip(tmp_path: Path) -> None:
    """Verify `IngestionJob` state, candidates, fingerprint, and queue fields round-trip through SQLite."""
    db_path = tmp_path / "jobs.db"
    manager = SqliteDatabaseManager(db_path)
    repo = SqliteCatalogRepository(manager)

    async def _scenario() -> None:
        await manager.initialize()
        assert await repo.get_job("nonexistent") is None

        candidate_meta = TrackMetadata(
            title="Veridis Quo",
            artist="Daft Punk",
            album="Discovery",
            year=2001,
            track_number=11,
        )
        job = IngestionJob(
            job_id="job_discovery",
            source_path=tmp_path / "raw.flac",
            state=JobState.AWAITING_CONFIRMATION,
            candidates=[
                MetadataCandidate(
                    metadata=candidate_meta,
                    confidence=0.88,
                    disambiguation="Original Release",
                )
            ],
            selected_metadata=candidate_meta,
            fingerprint=AudioFingerprint(duration_seconds=345, fingerprint="FP_VERIDIS"),
            final_path=tmp_path / "final.flac",
            user_id=123456,
            source_type=SourceType.ARCHIVE_ZIP,
            original_filename="Discovery.zip",
            file_size_bytes=102400,
            confidence_score=0.88,
        )
        await repo.save_job(job)

        loaded = await repo.get_job("job_discovery")
        assert loaded is not None
        assert loaded.job_id == "job_discovery"
        assert loaded.state == JobState.AWAITING_CONFIRMATION
        assert len(loaded.candidates) == 1
        assert loaded.candidates[0].metadata.title == "Veridis Quo"
        assert loaded.selected_metadata is not None
        assert loaded.selected_metadata.album == "Discovery"
        assert loaded.fingerprint is not None
        assert loaded.fingerprint.fingerprint == "FP_VERIDIS"
        assert loaded.user_id == 123456
        assert loaded.source_type == SourceType.ARCHIVE_ZIP
        assert loaded.original_filename == "Discovery.zip"

        active_jobs = await repo.list_active_jobs()
        assert len(active_jobs) == 1
        assert active_jobs[0].job_id == "job_discovery"

    asyncio.run(_scenario())


@pytest.mark.integration
def test_sqlite_users_access_requests_and_audit_logs_persistence(tmp_path: Path) -> None:
    """Verify SQLite persistence for `users`, `access_requests`, and `audit_logs` tables."""
    db_path = tmp_path / "governance.db"
    manager = SqliteDatabaseManager(db_path)
    repo = SqliteCatalogRepository(manager)

    async def _scenario() -> None:
        await manager.initialize()
        user = UserAccount(
            telegram_id=11223344,
            display_name="Jane Smith",
            username="jane_smith",
            role=UserRole.GUEST,
            authentik_sub="sub-uuid-jane",
            authentik_username="jane",
            authentik_groups=("music-uploaders",),
        )
        await repo.upsert_user(user)

        by_id = await repo.get_user(11223344)
        assert by_id is not None
        assert by_id.authentik_sub == "sub-uuid-jane"
        assert by_id.authentik_groups == ("music-uploaders",)

        by_sub = await repo.get_user_by_authentik_sub("sub-uuid-jane")
        assert by_sub is not None
        assert by_sub.telegram_id == 11223344

        linked = await repo.list_oidc_linked_users()
        assert len(linked) == 1

        req = await repo.create_access_request(11223344)
        pending = await repo.get_pending_access_request(11223344)
        assert pending is not None
        assert pending.id == req.id

        all_pending = await repo.list_pending_access_requests()
        assert len(all_pending) == 1

        resolved = await repo.resolve_access_request(
            request_id=req.id,
            status=AccessRequestStatus.APPROVED,
            reviewed_by=11223344,
        )
        assert resolved is not None
        assert resolved.status == AccessRequestStatus.APPROVED
        assert await repo.get_pending_access_request(11223344) is None

        saved_log = await repo.append_audit_log(
            AuditLogEntry(
                actor_id=11223344,
                action=AuditAction.ROLE_UPDATED,
                target_entity="user:11223344",
                details={"new_role": "UPLOADER"},
            )
        )
        assert saved_log.id is not None
        logs = await repo.list_audit_logs(limit=5)
        assert len(logs) == 1
        assert logs[0].details["new_role"] == "UPLOADER"

    asyncio.run(_scenario())


@pytest.mark.integration
def test_sqlite_legacy_ingestion_jobs_schema_migration(tmp_path: Path) -> None:
    """Verify an existing pre-M3.5 `ingestion_jobs` table without `status` migrates cleanly."""
    db_path = tmp_path / "legacy.db"
    manager = SqliteDatabaseManager(db_path)
    repo = SqliteCatalogRepository(manager)

    async def _scenario() -> None:
        async with manager.connect() as conn:
            await conn.execute(
                """
                CREATE TABLE ingestion_jobs (
                    job_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    candidates_json TEXT NOT NULL,
                    selected_metadata_json TEXT,
                    fingerprint_json TEXT,
                    final_path TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            await conn.execute(
                """
                INSERT INTO ingestion_jobs (
                    job_id, source_path, state, candidates_json, created_at
                ) VALUES ('legacy_1', '/tmp/a.flac', 'COMPLETED', '[]', '2026-10-11T12:00:00+00:00');
                """
            )
            await conn.commit()

        await manager.initialize()
        loaded = await repo.get_job("legacy_1")
        assert loaded is not None
        assert loaded.job_id == "legacy_1"
        assert loaded.state == JobState.COMPLETED

    asyncio.run(_scenario())


@pytest.mark.integration
def test_sqlite_upsert_scanned_library_tracks_batch_and_reconciliation(tmp_path: Path) -> None:
    """Verify batch track upserts, LibraryOrganizer reconciliation, and background startup loop."""
    db_path = tmp_path / "rescan.db"
    manager = SqliteDatabaseManager(db_path)
    repo = SqliteCatalogRepository(manager)
    library_dir = tmp_path / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    organizer = LibraryOrganizer(library_root=library_dir)

    async def _scenario() -> None:
        await manager.initialize()

        # 1. Test batch upsert
        rec1 = LibraryTrackRecord(
            id="track-batch-1",
            job_id="job-batch-1",
            uploaded_by=1001,
            storage_path=library_dir / "Artist1" / "Album1 (2020)" / "01 - Track1.mp3",
            fingerprint="fp-batch-1",
            duration_seconds=180.0,
            artist="Artist1",
            album="Album1",
            title="Track1",
            year=2020,
            track_number=1,
            has_synced_lyrics=False,
        )
        rec2 = LibraryTrackRecord(
            id="track-batch-2",
            job_id="job-batch-2",
            uploaded_by=1002,
            storage_path=library_dir / "Artist2" / "Album2 (2021)" / "02 - Track2.mp3",
            fingerprint="fp-batch-2",
            duration_seconds=210.0,
            artist="Artist2",
            album="Album2",
            title="Track2",
            year=2021,
            track_number=2,
            has_synced_lyrics=True,
        )
        await repo.upsert_scanned_library_tracks_batch([rec1, rec2])

        tracks = await repo.list_library_tracks(limit=10)
        assert len(tracks) == 2
        assert {t.id for t in tracks} == {"track-batch-1", "track-batch-2"}

        # 2. Test empty batch does not fail
        await repo.upsert_scanned_library_tracks_batch([])

        # 3. Test background rescan loop with run_on_startup=True
        stop_event = asyncio.Event()
        stop_event.set()  # Stop immediately after startup phase
        await run_periodic_library_rescan_loop(
            organizer=organizer,
            repository=repo,
            interval_seconds=10,
            stop_event=stop_event,
            run_on_startup=True,
        )

    asyncio.run(_scenario())


@pytest.mark.integration
def test_sqlite_metadata_similarity_deduplication_and_lockdown(tmp_path: Path) -> None:
    """Verify that metadata similarity detects duplicate songs across differing paths and tags."""
    db_path = tmp_path / "similarity_test.db"
    manager = SqliteDatabaseManager(db_path)
    repo = SqliteCatalogRepository(manager)
    organizer = LibraryOrganizer(tmp_path / "library")

    async def _scenario() -> None:
        await manager.initialize()

        # 1. Existing scanned song in library with folder and filename variation
        existing_path = Path(
            "/mnt/music/library/Rkomi/Rkomi - TAXI DRIVER/02 Rkomi - LA CODA DEL DIAVOLO.flac"
        )
        rec = LibraryTrackRecord(
            id="track-rkomi-01",
            job_id="job-rkomi-01",
            uploaded_by=1001,
            storage_path=existing_path,
            fingerprint="fp_rkomi_rip1",
            duration_seconds=185.0,
            artist="Rkomi",
            album="Rkomi - TAXI DRIVER",
            title="02 Rkomi - LA CODA DEL DIAVOLO",
            year=2021,
            track_number=2,
            has_synced_lyrics=True,
        )
        await repo.upsert_scanned_library_tracks_batch([rec])

        # 2. Incoming track with different rip fingerprint, cleaned title, and year tag
        incoming_meta = TrackMetadata(
            artist="Rkomi",
            album="Taxi Driver (2021)",
            title="02 - La coda del diavolo",
            year=2021,
            track_number=2,
            duration_seconds=185,
        )
        dup_result = await repo.check_duplicate(
            fingerprint="fp_different_rip_hash_2",
            recording_mbid=None,
            metadata=incoming_meta,
        )
        assert dup_result.is_duplicate is True
        assert dup_result.match_reason == "similarity"
        assert dup_result.existing_path == existing_path

        # 3. Completely different song by the same artist is NOT a duplicate
        different_song_meta = TrackMetadata(
            artist="Rkomi",
            album="Taxi Driver (2021)",
            title="Partire da te",
            year=2021,
            track_number=1,
            duration_seconds=225,
        )
        non_dup = await repo.check_duplicate(
            fingerprint="fp_different_song_hash_3",
            recording_mbid=None,
            metadata=different_song_meta,
        )
        assert non_dup.is_duplicate is False

        # 4. Verify organizer lockdown state prevents concurrent reconciliation
        assert organizer.is_reconciling is False
        assert organizer.reconciliation_status == "idle"

        organizer._is_reconciling = True
        organizer._reconciliation_status = "Scanning disk files..."
        assert organizer.is_reconciling is True
        assert organizer.reconciliation_status == "Scanning disk files..."

        # Re-entrant call while reconciling is safely skipped
        report = await organizer.reconcile_library_with_repository(repo)
        assert report.has_changes is False
        assert len(report.added_tracks) == 0

        organizer._is_reconciling = False

    asyncio.run(_scenario())
