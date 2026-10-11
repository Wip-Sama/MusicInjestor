"""Integration tests for SQLite WAL persistence (`src.db`)."""

import asyncio
from pathlib import Path

import pytest

from src.core.models import (
    AudioFingerprint,
    IngestionJob,
    JobState,
    MetadataCandidate,
    TrackMetadata,
)
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
        )

        by_fp = await repo.check_duplicate("FP_KID_A_01", None)
        assert by_fp.is_duplicate is True
        assert by_fp.match_reason == "fingerprint"
        assert by_fp.existing_path == final_file

        by_mbid = await repo.check_duplicate("FP_DIFFERENT_ENCODING", "mbid-kid-a-01")
        assert by_mbid.is_duplicate is True
        assert by_mbid.match_reason == "recording_mbid"
        assert by_mbid.existing_path == final_file

    asyncio.run(_scenario())


@pytest.mark.integration
def test_sqlite_job_persistence_roundtrip(tmp_path: Path) -> None:
    """Verify `IngestionJob` state, candidates, and fingerprint round-trip through SQLite."""
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

    asyncio.run(_scenario())
