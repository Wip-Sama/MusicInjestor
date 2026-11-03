"""Unit tests for quality upgrade duplicate handling in IngestionPipeline."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import (
    AudioFingerprint,
    DuplicateCheckResult,
    IngestionJob,
    JobState,
    TrackMetadata,
)
from src.core.pipeline import IngestionPipeline
from src.core.quality import AudioQuality


@pytest.fixture
def mock_dependencies():
    """Build mock ports for IngestionPipeline testing."""
    extractor = MagicMock()
    fingerprinter = MagicMock()
    metadata_provider = MagicMock()
    lyrics_provider = MagicMock()
    organizer = MagicMock()
    catalog = MagicMock()

    extractor.cleanup_job_scratch = AsyncMock()
    catalog.save_job = AsyncMock()
    catalog.replace_library_track = AsyncMock()
    catalog.get_user_preferences = AsyncMock()
    fingerprinter.compute_fingerprint = AsyncMock(
        return_value=AudioFingerprint(fingerprint="fp123", duration_seconds=120.0)
    )
    lyrics_provider.fetch_lyrics = AsyncMock(return_value=None)
    organizer.finalize_track = AsyncMock()

    pipeline = IngestionPipeline(
        extractor=extractor,
        fingerprinter=fingerprinter,
        metadata_provider=metadata_provider,
        lyrics_provider=lyrics_provider,
        organizer=organizer,
        catalog_repository=catalog,
    )
    return pipeline, extractor, catalog, organizer


@pytest.mark.anyio
async def test_duplicate_check_pauses_for_upgrade_when_auto_upgrade_disabled(mock_dependencies, tmp_path):
    """When a duplicate is an upgrade and auto_upgrade is disabled, pause in AWAITING_CONFIRMATION."""
    pipeline, extractor, catalog, organizer = mock_dependencies

    # Setup dummy audio file
    dummy_audio = tmp_path / "incoming.flac"
    dummy_audio.write_bytes(b"FLAC DATA" * 10)

    job = IngestionJob(
        job_id="test-job-upgrade",
        user_id=12345,
        source_path=dummy_audio,
        original_filename="incoming.flac",
        scratch_dir=tmp_path / "scratch",
    )

    existing_path = tmp_path / "existing.mp3"
    existing_path.write_bytes(b"MP3 DATA" * 5)

    dup_result = DuplicateCheckResult(
        is_duplicate=True,
        existing_path=existing_path,
        match_reason="fingerprint",
        is_upgrade=True,
        existing_quality=AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=128),
        new_quality=AudioQuality(format="flac", is_lossless=True, bitrate_kbps=800),
    )

    # User preferences: auto_upgrade_quality = False
    mock_prefs = MagicMock()
    mock_prefs.auto_upgrade_quality = False
    catalog.get_user_preferences.return_value = mock_prefs

    result = await pipeline._handle_duplicate_check(job, dup_result, dummy_audio, job.job_id)

    assert result.requires_user_confirmation is True
    assert result.duplicate.is_upgrade is True
    assert job.is_upgrade is True
    assert job.upgrade_for_path == existing_path
    assert job.state == JobState.AWAITING_CONFIRMATION
    # Scratch directory should NOT be cleaned up because audio is needed if confirmed
    extractor.cleanup_job_scratch.assert_not_called()
    catalog.save_job.assert_called_once_with(job)


@pytest.mark.anyio
async def test_duplicate_check_auto_upgrades_when_preference_enabled(mock_dependencies, tmp_path):
    """When a duplicate is an upgrade and auto_upgrade is enabled,
    execute_upgrade is triggered immediately.
    """
    pipeline, extractor, catalog, organizer = mock_dependencies

    dummy_audio = tmp_path / "incoming.flac"
    dummy_audio.write_bytes(b"FLAC DATA" * 10)

    job = IngestionJob(
        job_id="test-job-auto",
        user_id=12345,
        source_path=dummy_audio,
        original_filename="incoming.flac",
        scratch_dir=tmp_path / "scratch",
    )

    existing_path = tmp_path / "existing.mp3"
    existing_path.write_bytes(b"MP3 DATA" * 5)

    dup_result = DuplicateCheckResult(
        is_duplicate=True,
        existing_path=existing_path,
        match_reason="fingerprint",
        is_upgrade=True,
        existing_quality=AudioQuality(format="mp3", is_lossless=False, bitrate_kbps=128),
        new_quality=AudioQuality(format="flac", is_lossless=True, bitrate_kbps=800),
    )

    # User preferences: auto_upgrade_quality = True
    mock_prefs = MagicMock()
    mock_prefs.auto_upgrade_quality = True
    catalog.get_user_preferences.return_value = mock_prefs

    # Mock execute_upgrade
    expected_result = MagicMock()
    pipeline.execute_upgrade = AsyncMock(return_value=expected_result)

    result = await pipeline._handle_duplicate_check(job, dup_result, dummy_audio, job.job_id)

    assert result is expected_result
    pipeline.execute_upgrade.assert_called_once_with(job.job_id, stage_callback=None)


@pytest.mark.anyio
async def test_execute_upgrade_replaces_track_and_completes(mock_dependencies, tmp_path):
    """execute_upgrade moves file, updates DB via replace_library_track, cleans scratch, and completes job."""
    pipeline, extractor, catalog, organizer = mock_dependencies

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    incoming_audio = staging_dir / "new_song.flac"
    incoming_audio.write_bytes(b"NEW FLAC AUDIO" * 50)

    library_dir = tmp_path / "library"
    library_dir.mkdir()
    old_audio = library_dir / "old_song.mp3"
    old_audio.write_bytes(b"OLD MP3 AUDIO" * 20)

    target_audio = library_dir / "old_song.flac"

    job = IngestionJob(
        job_id="test-exec-upgrade",
        user_id=999,
        source_path=incoming_audio,
        original_filename="new_song.flac",
        scratch_dir=staging_dir,
        is_upgrade=True,
        upgrade_for_path=old_audio,
        selected_metadata=TrackMetadata(title="Upgraded Song", artist="Great Artist", album="Great Album"),
        fingerprint=AudioFingerprint(fingerprint="fp123", duration_seconds=120.0),
    )

    catalog.get_job = AsyncMock(return_value=job)
    extractor.list_extracted_audio_files = AsyncMock(return_value=[incoming_audio])
    organizer.finalize_track = AsyncMock(return_value=target_audio)

    await pipeline.execute_upgrade("test-exec-upgrade")

    assert job.state == JobState.COMPLETED
    assert job.final_path == target_audio
    catalog.replace_library_track.assert_called_once()
    assert not old_audio.exists()  # Old file was removed
    extractor.cleanup_job_scratch.assert_called_once_with("test-exec-upgrade")
    catalog.save_job.assert_called()
