"""High-level IngestionPipeline coordinating extraction, deduplication, enrichment, and filing."""

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from src.core.deduplicator import DeduplicatorService
from src.core.exceptions import FingerprintingError
from src.core.extractor import SUPPORTED_AUDIO_EXTENSIONS
from src.core.metadata import parse_filename_metadata, parse_manual_search_query
from src.core.models import (
    AudioFingerprint,
    IngestionJob,
    IngestionResult,
    JobState,
    MetadataCandidate,
    SourceType,
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


def _compute_sha256_digest(file_path: Path) -> str:
    """Compute a SHA-256 hex digest for fallback fingerprinting when `fpcalc` is absent."""
    digest = hashlib.sha256()
    if file_path.is_file():
        with file_path.open("rb") as handle:
            while chunk := handle.read(65536):
                digest.update(chunk)
    return digest.hexdigest()


def _merge_original_filename_metadata(
    local_meta: TrackMetadata,
    staged_stem: str,
    original_filename: str,
) -> TrackMetadata:
    """Supplement `local_meta` with artist/title parsed from `original_filename` when tags are absent."""
    if not original_filename:
        return local_meta
    orig_stem = Path(original_filename).stem
    if not orig_stem or orig_stem == staged_stem:
        return local_meta

    parsed = parse_filename_metadata(orig_stem)
    artist = local_meta.artist if local_meta.artist != "Unknown Artist" else parsed.artist
    title = parsed.title if local_meta.title == staged_stem and parsed.title else local_meta.title
    track_num = local_meta.track_number or parsed.track_number
    disc_num = local_meta.disc_number or parsed.disc_number
    return TrackMetadata(
        title=title,
        artist=artist,
        album=local_meta.album,
        album_artist=local_meta.album_artist,
        year=local_meta.year,
        track_number=track_num,
        disc_number=disc_num,
        genre=local_meta.genre,
        duration_seconds=local_meta.duration_seconds,
        recording_mbid=local_meta.recording_mbid,
        release_mbid=local_meta.release_mbid,
        isrc=local_meta.isrc,
    )


def _discover_staged_audio_files(job: IngestionJob) -> list[Path]:
    """Locate staged audio files in `job.scratch_dir` or fall back to `job.source_path`."""
    if job.scratch_dir is not None and job.scratch_dir.is_dir():
        found = sorted(
            path
            for path in job.scratch_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
        )
        if found:
            return found
    return [job.source_path]


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

    async def _compute_fingerprint_with_fallback(
        self,
        audio_path: Path,
        known_duration: int | None = None,
    ) -> AudioFingerprint:
        """Compute Chromaprint via `self._fingerprinter`, falling back to SHA-256 if `fpcalc` is absent."""
        try:
            return await self._fingerprinter.compute_fingerprint(audio_path)
        except FingerprintingError as exc:
            logger.warning(
                "Chromaprint unavailable for %s (%s); using SHA-256 fallback fingerprint",
                audio_path.name,
                exc,
            )
            sha256_hex = _compute_sha256_digest(audio_path)
            return AudioFingerprint(
                duration_seconds=known_duration or 0,
                fingerprint=f"sha256:{sha256_hex}",
                acoustid_mbid=None,
            )

    async def process_file(
        self,
        source_path: Path,
        job_id: str | None = None,
        *,
        user_id: int | None = None,
        original_filename: str = "",
        file_size_bytes: int = 0,
        source_type: SourceType = SourceType.DIRECT_AUDIO,
        sha256_hash: str | None = None,
        started_at: datetime | None = None,
    ) -> IngestionResult:
        """Run stages 1–7, and continue through 8–10 if confidence meets the auto-approve threshold."""
        resolved_job_id = job_id or uuid.uuid4().hex[:12]
        job = IngestionJob(
            job_id=resolved_job_id,
            source_path=source_path,
            user_id=user_id,
            original_filename=original_filename or source_path.name,
            file_size_bytes=file_size_bytes or (source_path.stat().st_size if source_path.is_file() else 0),
            source_type=source_type,
            sha256_hash=sha256_hash,
            started_at=started_at or datetime.now(UTC),
        )
        await self._catalog.save_job(job)

        job.state = JobState.EXTRACTING
        await self._catalog.save_job(job)
        staged_files = await self._extractor.extract_or_stage(source_path, resolved_job_id)
        primary_audio = staged_files[0]
        job.scratch_dir = primary_audio.parent

        raw_local_meta = await self._metadata.read_local_metadata(primary_audio)
        local_meta = _merge_original_filename_metadata(
            raw_local_meta,
            primary_audio.stem,
            job.original_filename,
        )

        job.state = JobState.FINGERPRINTING
        await self._catalog.save_job(job)
        fingerprint = await self._compute_fingerprint_with_fallback(
            primary_audio,
            known_duration=local_meta.duration_seconds,
        )
        job.fingerprint = fingerprint

        dup_check = await self._deduplicator.check_track(fingerprint)
        if dup_check.is_duplicate:
            job.state = JobState.DUPLICATE_REJECTED
            job.completed_at = datetime.now(UTC)
            await self._catalog.save_job(job)
            await self._extractor.cleanup_job_scratch(resolved_job_id)
            return IngestionResult(
                job=job,
                requires_user_confirmation=False,
                duplicate=dup_check,
            )

        job.state = JobState.ENRICHING_METADATA
        await self._catalog.save_job(job)
        candidates = await self._metadata.find_candidates(local_meta, fingerprint)
        job.candidates = candidates
        if candidates:
            job.confidence_score = candidates[0].confidence
            job.matched_release_mbid = candidates[0].metadata.release_mbid

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
        return await self._complete_ingestion(
            job,
            primary_audio,
            selected,
            extra_audio_paths=staged_files[1:],
        )

    async def confirm_candidate(
        self,
        job_id: str,
        candidate_index: int,
        staged_audio_path: Path | None = None,
    ) -> IngestionResult:
        """Resume a job in AWAITING_CONFIRMATION after the user selects a candidate match."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown ingestion job: {job_id}"
            raise KeyError(msg)
        if not 0 <= candidate_index < len(job.candidates):
            msg = f"Candidate index {candidate_index} out of range for job {job_id}"
            raise IndexError(msg)

        if staged_audio_path is not None:
            primary_audio = staged_audio_path
            extra_paths: list[Path] = []
        else:
            discovered = _discover_staged_audio_files(job)
            primary_audio = discovered[0]
            extra_paths = discovered[1:]

        chosen = job.candidates[candidate_index]
        job.confidence_score = chosen.confidence
        job.matched_release_mbid = chosen.metadata.release_mbid
        return await self._complete_ingestion(
            job,
            primary_audio,
            chosen.metadata,
            extra_audio_paths=extra_paths,
        )

    async def search_manual_candidates(
        self,
        job_id: str,
        query: str,
    ) -> IngestionResult:
        """Re-query candidates using a manual query, MusicBrainz URL/MBID, ISRC, or field override."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown ingestion job: {job_id}"
            raise KeyError(msg)

        primary_audio = _discover_staged_audio_files(job)[0]
        raw_local_meta = await self._metadata.read_local_metadata(primary_audio)
        base_meta = _merge_original_filename_metadata(
            raw_local_meta,
            primary_audio.stem,
            job.original_filename,
        )
        if base_meta.duration_seconds is None and job.fingerprint is not None:
            base_meta = TrackMetadata(
                title=base_meta.title,
                artist=base_meta.artist,
                album=base_meta.album,
                album_artist=base_meta.album_artist,
                year=base_meta.year,
                track_number=base_meta.track_number,
                disc_number=base_meta.disc_number,
                genre=base_meta.genre,
                duration_seconds=job.fingerprint.duration_seconds,
                recording_mbid=base_meta.recording_mbid,
                release_mbid=base_meta.release_mbid,
                isrc=base_meta.isrc,
            )

        manual_meta = parse_manual_search_query(query, base_metadata=base_meta)
        manual_fp = (
            AudioFingerprint(
                duration_seconds=job.fingerprint.duration_seconds,
                fingerprint=job.fingerprint.fingerprint,
                acoustid_mbid=None,
            )
            if job.fingerprint is not None
            else None
        )
        candidates = await self._metadata.find_candidates(manual_meta, manual_fp)
        job.candidates = candidates
        job.state = JobState.AWAITING_CONFIRMATION
        if candidates:
            job.confidence_score = candidates[0].confidence
            job.matched_release_mbid = candidates[0].metadata.release_mbid
        await self._catalog.save_job(job)
        return IngestionResult(job=job, requires_user_confirmation=True)

    async def cancel_job(self, job_id: str) -> IngestionJob:
        """Cancel an in-flight or awaiting-confirmation job and clean up its scratch directory."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown ingestion job: {job_id}"
            raise KeyError(msg)
        job.state = JobState.CANCELLED
        job.completed_at = datetime.now(UTC)
        await self._catalog.save_job(job)
        await self._extractor.cleanup_job_scratch(job_id)
        return job

    async def _complete_ingestion(
        self,
        job: IngestionJob,
        audio_path: Path,
        selected_metadata: TrackMetadata,
        extra_audio_paths: list[Path] | None = None,
    ) -> IngestionResult:
        """Execute stages 8–10: fetch lyrics, write tags & move file(s), register catalog, cleanup."""
        job.selected_metadata = selected_metadata
        job.matched_release_mbid = selected_metadata.release_mbid or job.matched_release_mbid
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

        if job.fingerprint is not None:
            await self._catalog.register_ingested_track(
                job_id=job.job_id,
                metadata=selected_metadata,
                fingerprint=job.fingerprint,
                library_path=final_path,
            )

        any_lyrics = bool(lyrics and lyrics.has_any)
        for index, extra_path in enumerate(extra_audio_paths or [], start=2):
            extra_local = await self._metadata.read_local_metadata(extra_path)
            extra_fp = await self._compute_fingerprint_with_fallback(
                extra_path,
                known_duration=extra_local.duration_seconds,
            )
            extra_dup = await self._deduplicator.check_track(extra_fp)
            if extra_dup.is_duplicate:
                continue
            extra_meta = TrackMetadata(
                title=extra_local.title,
                artist=(
                    extra_local.artist if extra_local.artist != "Unknown Artist" else selected_metadata.artist
                ),
                album=selected_metadata.album,
                album_artist=selected_metadata.effective_album_artist,
                year=selected_metadata.year,
                track_number=extra_local.track_number or index,
                disc_number=extra_local.disc_number or selected_metadata.disc_number or 1,
                genre=extra_local.genre or selected_metadata.genre,
                duration_seconds=extra_fp.duration_seconds or extra_local.duration_seconds,
                recording_mbid=extra_local.recording_mbid,
                release_mbid=selected_metadata.release_mbid,
            )
            extra_lyrics = await self._lyrics.fetch_lyrics(extra_meta)
            extra_final = await self._organizer.finalize_track(
                source_audio_path=extra_path,
                metadata=extra_meta,
                lyrics=extra_lyrics,
            )
            await self._catalog.register_ingested_track(
                job_id=job.job_id,
                metadata=extra_meta,
                fingerprint=extra_fp,
                library_path=extra_final,
            )
            if extra_lyrics and extra_lyrics.has_any:
                any_lyrics = True

        job.state = JobState.COMPLETED
        job.completed_at = datetime.now(UTC)
        await self._catalog.save_job(job)

        await self._extractor.cleanup_job_scratch(job.job_id)
        return IngestionResult(
            job=job,
            requires_user_confirmation=False,
            lyrics_attached=any_lyrics,
        )
