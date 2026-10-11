"""Unit tests for `src.core.pipeline.IngestionPipeline` using in-memory test doubles."""

import asyncio
from pathlib import Path

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
