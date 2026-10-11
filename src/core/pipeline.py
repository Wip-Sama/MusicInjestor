"""High-level IngestionPipeline coordinating extraction, deduplication, enrichment, and filing."""

import logging
import uuid
from pathlib import Path

from src.core.deduplicator import DeduplicatorService
from src.core.models import (
    IngestionJob,
    IngestionResult,
    JobState,
    MetadataCandidate,
    TrackMetadata,
)
from src.core.ports import (
    ArchiveExtractorPort,
    CatalogRepositoryPort,
    FingerprinterPort,
    LyricsProviderPort,
    MetadataProviderPort,
    StorageOrganizerPort,
)

logger = logging.getLogger(__name__)


def _requires_confirmation(
    candidates: list[MetadataCandidate],
    threshold: float,
) -> bool:
    """Determine if candidates are ambiguous or fall below the auto-approval confidence threshold."""
    if not candidates:
        return True
    top = candidates[0]
    if top.confidence < threshold:
        return True
    if len(candidates) > 1:
        runner_up = candidates[1]
        if runner_up.confidence >= threshold and abs(top.confidence - runner_up.confidence) < 0.05:
            return True
    return False


class IngestionPipeline:
    """Pure domain orchestrator driving audio files through the verification and storage stages."""

    def __init__(
        self,
        extractor: ArchiveExtractorPort,
        fingerprinter: FingerprinterPort,
        metadata_provider: MetadataProviderPort,
        lyrics_provider: LyricsProviderPort,
        organizer: StorageOrganizerPort,
        catalog_repository: CatalogRepositoryPort,
        auto_approve_confidence: float = 0.90,
    ) -> None:
        self._extractor = extractor
        self._fingerprinter = fingerprinter
        self._metadata = metadata_provider
        self._lyrics = lyrics_provider
        self._organizer = organizer
        self._catalog = catalog_repository
        self._deduplicator = DeduplicatorService(catalog_repository)
        self._auto_approve_confidence = auto_approve_confidence

    async def process_file(
        self,
        source_path: Path,
        job_id: str | None = None,
    ) -> IngestionResult:
        """Run stages 1–7, and continue through 8–10 if confidence meets the auto-approve threshold."""
        resolved_job_id = job_id or uuid.uuid4().hex[:12]
        job = IngestionJob(job_id=resolved_job_id, source_path=source_path)
        await self._catalog.save_job(job)

        job.state = JobState.EXTRACTING
        await self._catalog.save_job(job)
        staged_files = await self._extractor.extract_or_stage(source_path, resolved_job_id)
        primary_audio = staged_files[0]

        job.state = JobState.FINGERPRINTING
        await self._catalog.save_job(job)
        fingerprint = await self._fingerprinter.compute_fingerprint(primary_audio)
        job.fingerprint = fingerprint

        dup_check = await self._deduplicator.check_track(fingerprint)
        if dup_check.is_duplicate:
            job.state = JobState.DUPLICATE_REJECTED
            await self._catalog.save_job(job)
            await self._extractor.cleanup_job_scratch(resolved_job_id)
            return IngestionResult(
                job=job,
                requires_user_confirmation=False,
                duplicate=dup_check,
            )

        job.state = JobState.ENRICHING_METADATA
        await self._catalog.save_job(job)
        local_meta = await self._metadata.read_local_metadata(primary_audio)
        candidates = await self._metadata.find_candidates(local_meta, fingerprint)
        job.candidates = candidates

        if _requires_confirmation(candidates, self._auto_approve_confidence):
            job.state = JobState.AWAITING_CONFIRMATION
            await self._catalog.save_job(job)
            logger.info(
                "Job %s requires interactive confirmation (%d candidate(s))",
                resolved_job_id,
                len(candidates),
            )
            return IngestionResult(
                job=job,
                requires_user_confirmation=True,
            )

        selected = candidates[0].metadata
        return await self._complete_ingestion(job, primary_audio, selected)

    async def confirm_candidate(
        self,
        job_id: str,
        candidate_index: int,
        staged_audio_path: Path,
    ) -> IngestionResult:
        """Resume a job in AWAITING_CONFIRMATION after the user selects a candidate match."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown ingestion job: {job_id}"
            raise KeyError(msg)
        if not 0 <= candidate_index < len(job.candidates):
            msg = f"Candidate index {candidate_index} out of range for job {job_id}"
            raise IndexError(msg)

        selected = job.candidates[candidate_index].metadata
        return await self._complete_ingestion(job, staged_audio_path, selected)

    async def _complete_ingestion(
        self,
        job: IngestionJob,
        audio_path: Path,
        selected_metadata: TrackMetadata,
    ) -> IngestionResult:
        """Execute stages 8–10: fetch lyrics, write tags & move file, register catalog, cleanup."""
        job.selected_metadata = selected_metadata
        job.state = JobState.FETCHING_LYRICS
        await self._catalog.save_job(job)
        lyrics = await self._lyrics.fetch_lyrics(selected_metadata)

        job.state = JobState.ORGANIZING
        await self._catalog.save_job(job)
        final_path = await self._organizer.finalize_track(
            source_audio_path=audio_path,
            metadata=selected_metadata,
            lyrics=lyrics,
        )
        job.final_path = final_path
        job.state = JobState.COMPLETED
        await self._catalog.save_job(job)

        if job.fingerprint is not None:
            await self._catalog.register_ingested_track(
                job_id=job.job_id,
                metadata=selected_metadata,
                fingerprint=job.fingerprint,
                library_path=final_path,
            )

        await self._extractor.cleanup_job_scratch(job.job_id)
        return IngestionResult(
            job=job,
            requires_user_confirmation=False,
            lyrics_attached=bool(lyrics and lyrics.has_any),
        )
