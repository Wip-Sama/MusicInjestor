"""Unit tests for planned bulk metadata and lyrics re-search service."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.bulk_research import (
    BulkResearchPlan,
    BulkResearchProgress,
    BulkResearchService,
    get_active_bulk_research,
)
from src.core.models import (
    LibraryTrackRecord,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
)


@pytest.mark.anyio
async def test_bulk_research_planning(tmp_path: Path) -> None:
    """Verify plan_bulk_research calculates correct counts and estimated durations."""
    repo = AsyncMock()
    metadata_svc = AsyncMock()
    lyrics_client = AsyncMock()
    organizer = MagicMock()

    f1 = tmp_path / "song1.mp3"
    f2 = tmp_path / "song2.mp3"
    f1.write_bytes(b"DATA")
    f2.write_bytes(b"DATA")

    t1 = LibraryTrackRecord(
        id="trk_1",
        job_id="job_1",
        uploaded_by=123,
        storage_path=f1,
        fingerprint="fp1",
        duration_seconds=180,
        artist="Artist 1",
        album="Album 1",
        title="Title 1",
        year=2020,
        has_synced_lyrics=True,
    )
    t2 = LibraryTrackRecord(
        id="trk_2",
        job_id="job_2",
        uploaded_by=123,
        storage_path=f2,
        fingerprint="fp2",
        duration_seconds=200,
        artist="Artist 2",
        album="Album 2",
        title="Title 2",
        year=None,
        has_synced_lyrics=False,
    )

    repo.list_library_tracks.return_value = [t1, t2]
    repo.get_library_track.side_effect = lambda tid: (
        t1 if tid == "trk_1" else (t2 if tid == "trk_2" else None)
    )

    service = BulkResearchService(
        catalog_repository=repo,
        metadata_service=metadata_svc,
        lyrics_client=lyrics_client,
        organizer=organizer,
    )

    # 1. Plan for all tracks
    plan_all = await service.plan_bulk_research(filter_mode="all")
    assert plan_all.total_tracks == 2
    assert plan_all.missing_lyrics_count == 1
    assert plan_all.missing_mbid_count == 1
    assert plan_all.estimated_seconds >= 2

    # 2. Plan with specific track IDs
    plan_ids = await service.plan_bulk_research(track_ids=["trk_1"])
    assert plan_ids.total_tracks == 1
    assert plan_ids.missing_lyrics_count == 0

    # 3. Plan with missing_lyrics filter
    plan_lyrics = await service.plan_bulk_research(filter_mode="missing_lyrics")
    assert plan_lyrics.total_tracks == 1
    assert plan_lyrics.track_ids == ["trk_2"]

    # 4. Plan with missing_mbid filter
    plan_mbid = await service.plan_bulk_research(filter_mode="missing_mbid")
    assert plan_mbid.total_tracks == 1
    assert plan_mbid.track_ids == ["trk_2"]


@pytest.mark.anyio
async def test_bulk_research_execution_flow(tmp_path: Path) -> None:
    """Verify execute_bulk_research applies changes, triggers callbacks, and reports results."""
    repo = AsyncMock()
    metadata_svc = AsyncMock()
    lyrics_client = AsyncMock()
    organizer = AsyncMock()

    f1 = tmp_path / "song1.mp3"
    f2 = tmp_path / "song2.mp3"
    f1.write_bytes(b"DATA1")
    f2.write_bytes(b"DATA2")

    t1 = LibraryTrackRecord(
        id="trk_1",
        job_id="job_1",
        uploaded_by=123,
        storage_path=f1,
        fingerprint="fp1",
        duration_seconds=180,
        artist="Old Artist",
        album="Album 1",
        title="Old Title",
        has_synced_lyrics=False,
    )
    t2 = LibraryTrackRecord(
        id="trk_2",
        job_id="job_2",
        uploaded_by=123,
        storage_path=f2,
        fingerprint="fp2",
        duration_seconds=200,
        artist="Same Artist",
        album="Album 2",
        title="Same Title",
        year=2020,
        has_synced_lyrics=True,
    )

    repo.get_library_track.side_effect = lambda tid: (
        t1 if tid == "trk_1" else (t2 if tid == "trk_2" else None)
    )

    # Candidate for track 1
    enriched_meta = TrackMetadata(
        title="New Enriched Title",
        artist="New Enriched Artist",
        album="Enriched Album",
        recording_mbid="new-mbid",
    )
    cand1 = MetadataCandidate(metadata=enriched_meta, confidence=0.99, release_type="Album")
    cand2 = MetadataCandidate(
        metadata=TrackMetadata(title="Same Title", artist="Same Artist", album="Album 2", year=2020),
        confidence=0.99,
        release_type="Album",
    )

    metadata_svc.read_local_metadata.side_effect = lambda path: (
        enriched_meta if "song1" in str(path) else cand2.metadata
    )
    metadata_svc.find_candidates.side_effect = lambda meta: [cand1] if meta.title != "Same Title" else [cand2]

    # Lyrics for track 1 only
    lyrics_client.fetch_lyrics.side_effect = lambda meta: (
        LyricsData(synced_lyrics="[00:01.00] Hello world") if meta.title == "New Enriched Title" else None
    )

    organizer.relocate_and_retag_track.return_value = f1

    service = BulkResearchService(
        catalog_repository=repo,
        metadata_service=metadata_svc,
        lyrics_client=lyrics_client,
        organizer=organizer,
    )

    plan = BulkResearchPlan(
        track_ids=["trk_1", "trk_2"],
        total_tracks=2,
        missing_lyrics_count=1,
        missing_mbid_count=1,
        estimated_seconds=3,
        auto_apply=True,
    )

    progress_events = []

    async def on_progress(cur: int, tot: int, name: str, status: str) -> None:
        progress_events.append((cur, tot, name, status))

    report = await service.execute_bulk_research(
        plan,
        actor_id=12345,
        actor_name="TestAdmin",
        progress_callback=on_progress,
    )

    assert report.is_success is True
    assert report.applied_count == 1
    assert report.skipped_count == 1  # trk_2 was unchanged
    assert report.failed_count == 0
    assert len(progress_events) >= 2
    organizer.relocate_and_retag_track.assert_called_once()
    organizer.update_track_lyrics.assert_called_once()
    repo.upsert_scanned_library_track.assert_called_once()
    repo.append_audit_log.assert_called_once()


@pytest.mark.anyio
async def test_bulk_research_cancel_and_errors(tmp_path: Path) -> None:
    """Verify cooperative cancellation and error resilience during bulk research."""
    repo = AsyncMock()
    metadata_svc = AsyncMock()
    lyrics_client = AsyncMock()
    organizer = AsyncMock()

    f1 = tmp_path / "song1.mp3"
    f1.write_bytes(b"DATA")
    t1 = LibraryTrackRecord(
        id="trk_1",
        job_id="job_1",
        uploaded_by=123,
        storage_path=f1,
        fingerprint="fp1",
        duration_seconds=180,
        artist="Artist",
        album="Album",
        title="Title",
    )
    repo.get_library_track.return_value = t1
    metadata_svc.find_candidates.side_effect = RuntimeError("External API failure")

    service = BulkResearchService(
        catalog_repository=repo,
        metadata_service=metadata_svc,
        lyrics_client=lyrics_client,
        organizer=organizer,
    )

    plan = BulkResearchPlan(
        track_ids=["trk_1"],
        total_tracks=1,
        missing_lyrics_count=0,
        missing_mbid_count=0,
        estimated_seconds=1,
    )

    report = await service.execute_bulk_research(plan)
    assert report.failed_count == 1
    assert report.applied_count == 0

    # Test cancel
    service.cancel()
    assert service._cancel_requested is True
    cancelled_report = await service.execute_bulk_research(plan)
    # Cancelled before processing
    assert cancelled_report.applied_count == 0


@pytest.mark.anyio
async def test_bulk_research_active_progress_tracking(tmp_path: Path) -> None:
    """Verify that active bulk research progress is set during execution and reset after completion."""
    progress = BulkResearchProgress(total_tracks=10, current_index=5)
    assert progress.percent == 50

    repo = AsyncMock()
    metadata_svc = AsyncMock()
    lyrics_client = AsyncMock()
    organizer = AsyncMock()

    f1 = tmp_path / "track1.mp3"
    f1.write_bytes(b"DATA")
    t1 = LibraryTrackRecord(
        id="trk_1",
        job_id="job_1",
        uploaded_by=123,
        storage_path=f1,
        fingerprint="fp1",
        duration_seconds=180,
        artist="Artist",
        album="Album",
        title="Title",
    )
    repo.get_library_track.return_value = t1
    metadata_svc.read_local_metadata.return_value = TrackMetadata(
        title="Title", artist="Artist", album="Album", duration_seconds=180
    )
    metadata_svc.find_candidates.return_value = []
    lyrics_client.fetch_lyrics.return_value = None

    service = BulkResearchService(
        catalog_repository=repo,
        metadata_service=metadata_svc,
        lyrics_client=lyrics_client,
        organizer=organizer,
    )

    plan = BulkResearchPlan(
        track_ids=["trk_1"],
        total_tracks=1,
        missing_lyrics_count=0,
        missing_mbid_count=0,
        estimated_seconds=1,
    )

    observed_active: list[BulkResearchProgress | None] = []

    def _progress_cb(current: int, total: int, label: str, phase: str) -> None:
        observed_active.append(get_active_bulk_research())

    # Pre-condition: idle
    assert get_active_bulk_research() is None

    await service.execute_bulk_research(
        plan,
        actor_id=456,
        actor_name="TestAdmin",
        progress_callback=_progress_cb,
    )

    # Post-condition: guaranteed cleanup back to None
    assert get_active_bulk_research() is None
    assert len(observed_active) > 0
    first = observed_active[0]
    assert first is not None
    assert first.actor_id == 456
    assert first.actor_name == "TestAdmin"
    assert first.total_tracks == 1
