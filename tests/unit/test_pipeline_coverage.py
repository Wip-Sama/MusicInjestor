"""Targeted coverage tests for IngestionPipeline edge cases and helpers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import (
    AudioFingerprint,
    DuplicateCheckResult,
    IngestionJob,
    JobState,
    MetadataCandidate,
    TrackMetadata,
)
from src.core.pipeline import (
    IngestionPipeline,
    _discover_staged_audio_files,
    _match_track_from_release,
)


def test_match_track_from_release_branches() -> None:
    """Verify title similarity and ordinal fallback in _match_track_from_release."""
    fallback = TrackMetadata(title="Fallback", artist="Artist", album="Album")
    rel_tracks = [
        TrackMetadata(title="First Song", artist="Singer", album="Album", track_number=1, disc_number=1),
        TrackMetadata(
            title="Second Song (Remix)", artist="Singer", album="Album", track_number=2, disc_number=1
        ),
    ]

    # Exact match by disc & track
    local1 = TrackMetadata(
        title="Unknown Title", artist="Unknown Artist", album="Album", track_number=1, disc_number=1
    )
    res1 = _match_track_from_release(local1, 1, rel_tracks, fallback)
    assert res1.title == "First Song"

    # Match by track_number only
    local2 = TrackMetadata(title="Unknown Title", artist="Unknown Artist", album="Album", track_number=2)
    res2 = _match_track_from_release(local2, 2, rel_tracks, fallback)
    assert res2.title == "Second Song (Remix)"

    # Title similarity match (>= 80%)
    local3 = TrackMetadata(title="First Song!", artist="Unknown Artist", album="Album")
    res3 = _match_track_from_release(local3, 99, rel_tracks, fallback)
    assert res3.title == "First Song"

    # Ordinal fallback
    local4 = TrackMetadata(title="Completely Different", artist="Artist", album="Album")
    res4 = _match_track_from_release(local4, 2, rel_tracks, fallback)
    assert res4.title == "Second Song (Remix)"


def test_resolve_staged_audio_paths_branches(tmp_path: Path) -> None:
    """Verify scratch directory path resolution fallbacks."""
    scratch = tmp_path / "storage" / "scratch" / "job_1"
    scratch.mkdir(parents=True)
    audio = scratch / "test.mp3"
    audio.write_bytes(b"DATA")

    job = IngestionJob(
        job_id="job_1",
        source_path=audio,
        scratch_dir=scratch,
    )
    found = _discover_staged_audio_files(job)
    assert len(found) == 1
    assert found[0] == audio

    # Fallback to source_path if scratch has no audio
    empty_scratch = tmp_path / "empty"
    empty_scratch.mkdir()
    dummy_source = tmp_path / "source.flac"
    dummy_source.write_bytes(b"FLAC")
    job_empty = IngestionJob(
        job_id="job_empty",
        source_path=dummy_source,
        scratch_dir=empty_scratch,
    )
    found_empty = _discover_staged_audio_files(job_empty)
    assert found_empty == [dummy_source]


@pytest.mark.anyio
async def test_pipeline_job_lifecycle_and_search_candidates(tmp_path: Path) -> None:
    """Verify cancel_job, search_manual_candidates, and duplicate check in complete_custom_ingestion."""
    extractor = AsyncMock()
    extractor.get_job_scratch_dir = MagicMock(return_value=tmp_path)
    fingerprinter = AsyncMock()
    metadata_svc = AsyncMock()
    lyrics_svc = AsyncMock()
    organizer = AsyncMock()
    catalog = AsyncMock()

    pipeline = IngestionPipeline(
        extractor=extractor,
        fingerprinter=fingerprinter,
        metadata_provider=metadata_svc,
        lyrics_provider=lyrics_svc,
        organizer=organizer,
        catalog_repository=catalog,
    )

    audio_file = tmp_path / "song.flac"
    audio_file.write_bytes(b"FLAC")
    job = IngestionJob(
        job_id="job_life",
        source_path=audio_file,
        scratch_dir=tmp_path,
        state=JobState.PENDING,
    )
    catalog.get_job.return_value = job

    # 1. Cancel job
    cancelled = await pipeline.cancel_job("job_life")
    assert cancelled is not None
    assert cancelled.state == JobState.CANCELLED
    catalog.save_job.assert_called()

    # 2. search_manual_candidates
    cand = MetadataCandidate(
        metadata=TrackMetadata(title="Query Song", artist="Query Artist", album="Query Album"),
        confidence=0.9,
    )
    metadata_svc.find_candidates.return_value = [cand]
    metadata_svc.read_local_metadata.return_value = TrackMetadata(
        title="Local", artist="Local", album="Local"
    )

    result = await pipeline.search_manual_candidates("job_life", "Query Artist - Query Song")
    assert result.requires_user_confirmation is True
    assert len(result.job.candidates) == 1

    # 3. Duplicate check rejection during complete_custom_ingestion
    job.fingerprint = AudioFingerprint(duration_seconds=180, fingerprint="FP123")
    pipeline._deduplicator = AsyncMock()
    pipeline._deduplicator.check_track.return_value = DuplicateCheckResult(
        is_duplicate=True,
        existing_path=tmp_path / "existing.flac",
        match_reason="fingerprint",
    )
    meta = TrackMetadata(title="Dup", artist="Dup", album="Dup")
    dup_res = await pipeline.complete_custom_ingestion("job_life", meta)
    assert dup_res.job.state == JobState.DUPLICATE_REJECTED


@pytest.mark.anyio
async def test_pipeline_prepare_for_review(tmp_path: Path) -> None:
    """Verify prepare_for_review stages candidates and sets confidence score."""
    extractor = AsyncMock()
    extractor.get_job_scratch_dir = MagicMock(return_value=tmp_path)
    fingerprinter = AsyncMock()
    fingerprinter.fingerprint_audio.return_value = AudioFingerprint(duration_seconds=120, fingerprint="FP1")
    metadata_svc = AsyncMock()
    lyrics_svc = AsyncMock()
    organizer = AsyncMock()
    catalog = AsyncMock()

    pipeline = IngestionPipeline(
        extractor=extractor,
        fingerprinter=fingerprinter,
        metadata_provider=metadata_svc,
        lyrics_provider=lyrics_svc,
        organizer=organizer,
        catalog_repository=catalog,
    )

    audio_file = tmp_path / "song.flac"
    audio_file.write_bytes(b"FLAC")
    job = IngestionJob(
        job_id="job_rev",
        source_path=audio_file,
        scratch_dir=tmp_path,
        state=JobState.PENDING,
    )
    catalog.get_job.return_value = job

    metadata_svc.read_local_metadata.return_value = TrackMetadata(
        title="Local Title", artist="Local Artist", album="Local Album"
    )
    cand = MetadataCandidate(
        metadata=TrackMetadata(title="Cand Title", artist="Cand Artist", album="Cand Album"),
        confidence=0.92,
    )
    metadata_svc.find_candidates.return_value = [cand]

    res = await pipeline.prepare_for_review("job_rev")
    assert res.requires_user_confirmation is True
    assert job.state == JobState.AWAITING_CONFIRMATION
    assert len(job.candidates) == 1
    assert job.confidence_score == 0.92
