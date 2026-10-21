"""Unit tests for `src.core.pipeline.IngestionPipeline` using in-memory test doubles."""

import asyncio
from pathlib import Path

import pytest

from src.core.exceptions import FingerprintingError
from src.core.models import (
    AudioFingerprint,
    DuplicateCheckResult,
    IngestionJob,
    JobState,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
)
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline


class InMemoryCatalogRepository:
    """In-memory implementation of `CatalogRepositoryPort` for fast unit testing."""

    def __init__(self) -> None:
        self.jobs: dict[str, IngestionJob] = {}
        self.tracks_by_fp: dict[str, Path] = {}

    async def check_duplicate(
        self,
        fingerprint: str,
        recording_mbid: str | None = None,
        metadata: TrackMetadata | None = None,
    ) -> DuplicateCheckResult:
        await asyncio.sleep(0)
        if fingerprint in self.tracks_by_fp:
            return DuplicateCheckResult(
                is_duplicate=True,
                existing_path=self.tracks_by_fp[fingerprint],
                match_reason="fingerprint",
            )
        return DuplicateCheckResult(is_duplicate=False)

    async def register_ingested_track(
        self,
        job_id: str,
        metadata: TrackMetadata,
        fingerprint: AudioFingerprint,
        library_path: Path,
        has_synced_lyrics: bool = False,
    ) -> None:
        await asyncio.sleep(0)
        self.tracks_by_fp[fingerprint.fingerprint] = library_path

    async def save_job(self, job: IngestionJob) -> None:
        await asyncio.sleep(0)
        self.jobs[job.job_id] = job

    async def get_job(self, job_id: str) -> IngestionJob | None:
        await asyncio.sleep(0)
        return self.jobs.get(job_id)


class StubExtractor:
    """Stub `ArchiveExtractorPort` returning the source path directly."""

    def __init__(self) -> None:
        self.cleaned_jobs: list[str] = []

    async def extract_or_stage(self, source_path: Path, job_id: str) -> list[Path]:
        await asyncio.sleep(0)
        return [source_path]

    async def cleanup_job_scratch(self, job_id: str) -> None:
        await asyncio.sleep(0)
        self.cleaned_jobs.append(job_id)


class StubFingerprinter:
    """Stub `FingerprinterPort` returning a deterministic fingerprint."""

    def __init__(self, fp_hash: str = "AQADtEmyJJFStB==") -> None:
        self._fp_hash = fp_hash

    async def compute_fingerprint(self, audio_path: Path) -> AudioFingerprint:
        await asyncio.sleep(0)
        return AudioFingerprint(
            duration_seconds=215,
            fingerprint=self._fp_hash,
            acoustid_mbid="mbid-1234",
        )


class StubMetadataProvider:
    """Stub `MetadataProviderPort` returning configurable candidates."""

    def __init__(self, candidates: list[MetadataCandidate]) -> None:
        self._candidates = candidates

    async def read_local_metadata(self, audio_path: Path) -> TrackMetadata:
        await asyncio.sleep(0)
        return TrackMetadata(title="Test Song", artist="Test Artist", album="Test Album")

    async def find_candidates(
        self,
        local_metadata: TrackMetadata,
        fingerprint: AudioFingerprint | None = None,
    ) -> list[MetadataCandidate]:
        await asyncio.sleep(0)
        return self._candidates


class StubLyricsProvider:
    """Stub `LyricsProviderPort` returning synced lyrics."""

    async def fetch_lyrics(self, metadata: TrackMetadata) -> LyricsData | None:
        await asyncio.sleep(0)
        return LyricsData(synced_lyrics="[00:01.00] Synced test line")


def test_pipeline_auto_approves_high_confidence_and_rejects_subsequent_duplicate(
    tmp_path: Path,
) -> None:
    """Verify full auto-approved pipeline execution followed by duplicate rejection."""
    source_file = tmp_path / "song.flac"
    source_file.write_bytes(b"fLaC-data")

    catalog = InMemoryCatalogRepository()
    extractor = StubExtractor()
    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Starlight",
                artist="Muse",
                album="Black Holes and Revelations",
                year=2006,
                track_number=2,
            ),
            confidence=0.96,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=extractor,
        fingerprinter=StubFingerprinter("FP_STARLIGHT"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=catalog,
        auto_approve_confidence=0.90,
    )

    first_result = asyncio.run(pipeline.process_file(source_file, job_id="job_auto"))
    assert not first_result.requires_user_confirmation
    assert first_result.job.state == JobState.COMPLETED
    assert first_result.lyrics_attached is True
    assert first_result.job.final_path is not None
    assert first_result.job.final_path.is_file()
    assert "job_auto" in extractor.cleaned_jobs

    second_result = asyncio.run(pipeline.process_file(source_file, job_id="job_dup"))
    assert second_result.job.state == JobState.DUPLICATE_REJECTED
    assert second_result.duplicate is not None
    assert second_result.duplicate.is_duplicate is True
    assert second_result.duplicate.existing_path == first_result.job.final_path


def test_pipeline_halts_for_ambiguous_candidates_and_resumes_on_confirmation(
    tmp_path: Path,
) -> None:
    """Verify low-confidence candidates pause at `AWAITING_CONFIRMATION` until confirmed."""
    source_file = tmp_path / "ambiguous.flac"
    source_file.write_bytes(b"fLaC-ambiguous")

    catalog = InMemoryCatalogRepository()
    extractor = StubExtractor()
    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Song",
                artist="Artist",
                album="Standard Edition",
                year=2020,
                track_number=1,
            ),
            confidence=0.78,
        ),
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Song",
                artist="Artist",
                album="Deluxe Edition",
                year=2021,
                track_number=1,
            ),
            confidence=0.75,
            disambiguation="Deluxe",
        ),
    ]
    pipeline = IngestionPipeline(
        extractor=extractor,
        fingerprinter=StubFingerprinter("FP_AMBIGUOUS"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=catalog,
        auto_approve_confidence=0.90,
    )

    paused_result = asyncio.run(pipeline.process_file(source_file, job_id="job_confirm"))
    assert paused_result.requires_user_confirmation is True
    assert paused_result.job.state == JobState.AWAITING_CONFIRMATION
    assert len(paused_result.job.candidates) == 2

    confirmed_result = asyncio.run(
        pipeline.confirm_candidate(
            job_id="job_confirm",
            candidate_index=1,
            staged_audio_path=source_file,
        )
    )
    assert confirmed_result.job.state == JobState.COMPLETED
    assert confirmed_result.job.selected_metadata is not None
    assert confirmed_result.job.selected_metadata.album == "Deluxe Edition"
    assert confirmed_result.job.final_path is not None
    assert confirmed_result.job.final_path.is_file()


def test_pipeline_sha256_fallback_manual_search_and_multi_track_batch(
    tmp_path: Path,
) -> None:
    """Verify SHA-256 fallback when `fpcalc` fails, manual search, and multi-track archive batch filing."""
    track_one = tmp_path / "01 - First Song.flac"
    track_two = tmp_path / "02 - Second Song.flac"
    track_one.write_bytes(b"fLaC-track-1")
    track_two.write_bytes(b"fLaC-track-2")

    class BatchStubExtractor(StubExtractor):
        async def extract_or_stage(self, source_path: Path, job_id: str) -> list[Path]:
            await asyncio.sleep(0)
            return [track_one, track_two]

    class FailingFingerprinter:
        async def compute_fingerprint(self, audio_path: Path) -> AudioFingerprint:
            await asyncio.sleep(0)
            msg = "fpcalc not found"
            raise FingerprintingError(msg)

    catalog = InMemoryCatalogRepository()
    extractor = BatchStubExtractor()
    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="First Song",
                artist="Emilia (CV: Rie Takahashi)",
                album="Stay Alive",
                year=2016,
                track_number=1,
            ),
            confidence=0.95,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=extractor,
        fingerprinter=FailingFingerprinter(),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=catalog,
        auto_approve_confidence=0.90,
    )

    batch_result = asyncio.run(
        pipeline.process_file(
            track_one,
            job_id="job_batch",
            original_filename="01 - EMILIA(CV:RIE TAKAHASHI) – Stay Alive.flac",
        )
    )
    assert batch_result.job.state == JobState.COMPLETED
    assert len(catalog.tracks_by_fp) == 2

    # Test manual candidate search and job cancellation
    low_conf_pipeline = IngestionPipeline(
        extractor=extractor,
        fingerprinter=StubFingerprinter("FP_MANUAL"),
        metadata_provider=StubMetadataProvider(
            [
                MetadataCandidate(
                    metadata=TrackMetadata(title="Stay Alive", artist="Emilia", album="Stay Alive"),
                    confidence=0.72,
                )
            ]
        ),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=catalog,
        auto_approve_confidence=0.90,
    )
    paused = asyncio.run(low_conf_pipeline.process_file(track_one, job_id="job_manual"))
    assert paused.job.state == JobState.AWAITING_CONFIRMATION

    manual_res = asyncio.run(
        low_conf_pipeline.search_manual_candidates(
            "job_manual",
            "Emilia (CV: Rie Takahashi) - Stay Alive | Stay Alive",
        )
    )
    assert manual_res.requires_user_confirmation is True
    assert manual_res.job.state == JobState.AWAITING_CONFIRMATION

    cancelled = asyncio.run(low_conf_pipeline.cancel_job("job_manual"))
    assert cancelled.state == JobState.CANCELLED
    assert "job_manual" in extractor.cleaned_jobs


def test_pipeline_multi_track_archive_release_tracklist_mapping_and_stage_callbacks(
    tmp_path: Path,
) -> None:
    """Verify multi-track archives map each track against `fetch_release_tracks` and emit stage callbacks."""
    track_one = tmp_path / "01 - Track_01.flac"
    track_two = tmp_path / "02 - Track_02.flac"
    track_one.write_bytes(b"fLaC-multi-1")
    track_two.write_bytes(b"fLaC-multi-2")

    class MultiExtractor(StubExtractor):
        async def extract_or_stage(self, source_path: Path, job_id: str) -> list[Path]:
            await asyncio.sleep(0)
            return [track_one, track_two]

    class ReleaseMetadataProvider(StubMetadataProvider):
        async def read_local_metadata(self, audio_path: Path) -> TrackMetadata:
            await asyncio.sleep(0)
            idx = 1 if "01" in audio_path.name else 2
            return TrackMetadata(
                title=f"Local {idx}",
                artist="Local Artist",
                album="Local Album",
                track_number=idx,
            )

        async def fetch_release_tracks(
            self,
            release_mbid: str,
            prefer_english: bool = False,
        ) -> list[TrackMetadata]:
            await asyncio.sleep(0)
            assert release_mbid == "ca344c5b-9c69-4283-8383-e7c4c026d571"
            assert prefer_english is True
            return [
                TrackMetadata(
                    title="Stay Alive",
                    artist="Emilia (CV: Rie Takahashi)",
                    album="Stay Alive",
                    album_artist="Emilia (CV: Rie Takahashi)",
                    year=2016,
                    track_number=1,
                    disc_number=1,
                    recording_mbid="rec-mbid-track-1",
                    release_mbid=release_mbid,
                ),
                TrackMetadata(
                    title="Wishing",
                    artist="Rem (CV: Inori Minase)",
                    album="Stay Alive",
                    album_artist="Emilia (CV: Rie Takahashi)",
                    year=2016,
                    track_number=2,
                    disc_number=1,
                    recording_mbid="rec-mbid-track-2",
                    release_mbid=release_mbid,
                ),
            ]

    recorded_tracks: list[TrackMetadata] = []

    class TrackingCatalog(InMemoryCatalogRepository):
        async def register_ingested_track(
            self,
            job_id: str,
            metadata: TrackMetadata,
            fingerprint: AudioFingerprint,
            library_path: Path,
            has_synced_lyrics: bool = False,
        ) -> None:
            await super().register_ingested_track(
                job_id,
                metadata,
                fingerprint,
                library_path,
                has_synced_lyrics=has_synced_lyrics,
            )
            recorded_tracks.append(metadata)

    catalog = TrackingCatalog()
    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Stay Alive",
                artist="Emilia (CV: Rie Takahashi)",
                album="Stay Alive",
                album_artist="Emilia (CV: Rie Takahashi)",
                year=2016,
                track_number=1,
                disc_number=1,
                recording_mbid="rec-mbid-track-1",
                release_mbid="ca344c5b-9c69-4283-8383-e7c4c026d571",
            ),
            confidence=0.98,
        )
    ]

    class PerFileFingerprinter:
        async def compute_fingerprint(self, audio_path: Path) -> AudioFingerprint:
            await asyncio.sleep(0)
            return AudioFingerprint(
                duration_seconds=215,
                fingerprint=f"FP_{audio_path.stem}",
                acoustid_mbid=None,
            )

    pipeline = IngestionPipeline(
        extractor=MultiExtractor(),
        fingerprinter=PerFileFingerprinter(),
        metadata_provider=ReleaseMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=catalog,
        auto_approve_confidence=0.90,
    )

    stages_seen: list[JobState] = []

    async def _on_stage(job: IngestionJob) -> None:
        stages_seen.append(job.state)

    result = asyncio.run(
        pipeline.process_file(
            track_one,
            job_id="job_release_map",
            stage_callback=_on_stage,
        )
    )
    assert result.job.state == JobState.COMPLETED
    assert result.tracks_ingested == 2
    assert result.lyrics_embedded_count == 2
    assert len(recorded_tracks) == 2
    assert recorded_tracks[0].title == "Stay Alive"
    assert recorded_tracks[0].recording_mbid == "rec-mbid-track-1"
    assert recorded_tracks[1].title == "Wishing"
    assert recorded_tracks[1].artist == "Rem (CV: Inori Minase)"
    assert recorded_tracks[1].recording_mbid == "rec-mbid-track-2"
    assert JobState.EXTRACTING in stages_seen
    assert JobState.FINGERPRINTING in stages_seen
    assert JobState.ENRICHING_METADATA in stages_seen
    assert JobState.FETCHING_LYRICS in stages_seen
    assert JobState.ORGANIZING in stages_seen
    assert JobState.COMPLETED in stages_seen


def test_confirm_candidate_handles_failure_and_notifies_stage(tmp_path: Path) -> None:
    """Verify that failure during _complete_ingestion marks the job FAILED and notifies stage_callback."""
    source_file = tmp_path / "track.flac"
    source_file.write_bytes(b"dummy-audio-content")

    catalog = InMemoryCatalogRepository()
    extractor = StubExtractor()
    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(title="Song", artist="Artist", album="Album", year=2024, track_number=1),
            confidence=0.70,
        )
    ]

    class FailingOrganizer:
        def build_target_audio_path(self, metadata: TrackMetadata, extension: str) -> Path:
            return Path("/tmp") / f"{metadata.title}{extension}"

        async def finalize_track(
            self,
            source_audio_path: Path,
            metadata: TrackMetadata,
            lyrics: LyricsData | None = None,
        ) -> Path:
            await asyncio.sleep(0)
            msg = "Disk write failed or file missing"
            raise FileNotFoundError(msg)

    pipeline = IngestionPipeline(
        extractor=extractor,
        fingerprinter=StubFingerprinter("FP_FAIL"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=FailingOrganizer(),
        catalog_repository=catalog,
        auto_approve_confidence=0.90,
    )

    paused = asyncio.run(pipeline.process_file(source_file, job_id="job_fail_test"))
    assert paused.job.state == JobState.AWAITING_CONFIRMATION

    stages_seen: list[JobState] = []

    async def _on_stage(job: IngestionJob) -> None:
        stages_seen.append(job.state)

    with pytest.raises(FileNotFoundError, match="Disk write failed"):
        asyncio.run(
            pipeline.confirm_candidate(
                job_id="job_fail_test",
                candidate_index=0,
                staged_audio_path=source_file,
                stage_callback=_on_stage,
            )
        )

    saved_job = asyncio.run(catalog.get_job("job_fail_test"))
    assert saved_job is not None
    assert saved_job.state == JobState.FAILED
    assert "Disk write failed" in (saved_job.error_message or "")
    assert saved_job.completed_at is not None
    assert JobState.FETCHING_LYRICS in stages_seen
    assert JobState.ORGANIZING in stages_seen
    assert JobState.FAILED in stages_seen


def test_confirm_candidate_raises_file_not_found_when_missing_staged(tmp_path: Path) -> None:
    """Verify that confirm_candidate raises FileNotFoundError when staged file does not exist on disk."""
    non_existent = tmp_path / "scratch" / "job_ghost" / "missing.flac"
    catalog = InMemoryCatalogRepository()
    job = IngestionJob(
        job_id="job_ghost",
        source_path=non_existent,
        scratch_dir=non_existent.parent,
        candidates=[
            MetadataCandidate(
                metadata=TrackMetadata(title="Ghost", artist="Ghost", album="Ghost"),
                confidence=0.5,
            )
        ],
        state=JobState.AWAITING_CONFIRMATION,
    )
    asyncio.run(catalog.save_job(job))

    pipeline = IngestionPipeline(
        extractor=StubExtractor(),
        fingerprinter=StubFingerprinter("FP_GHOST"),
        metadata_provider=StubMetadataProvider([]),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=catalog,
    )

    with pytest.raises(FileNotFoundError, match="Staged audio file does not exist"):
        asyncio.run(pipeline.confirm_candidate("job_ghost", candidate_index=0))
