"""Serialized and runtime-configurable async Ingestion Queue Engine (Specification Section 4)."""

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from src.core.exceptions import PermissionDeniedError, QueueConcurrencyError
from src.core.models import (
    AuditAction,
    AuditLogEntry,
    IngestionJob,
    IngestionResult,
    JobState,
    Permission,
    SourceType,
    UserAccount,
)
from src.core.pipeline import IngestionPipeline
from src.core.ports import (
    AdminLogNotifierPort,
    CatalogRepositoryPort,
    GovernanceRepositoryPort,
)
from src.core.rbac import has_permission, require_permission

logger = logging.getLogger(__name__)

MIN_CONCURRENCY_LIMIT = 1
MAX_CONCURRENCY_LIMIT = 4

ProgressCallback = Callable[[IngestionJob, IngestionResult | None], Awaitable[None]]


def infer_source_type(file_path: Path) -> SourceType:
    """Infer `SourceType` from a file extension."""
    suffix = file_path.suffix.lower()
    if suffix == ".zip":
        return SourceType.ARCHIVE_ZIP
    if suffix == ".7z":
        return SourceType.ARCHIVE_7Z
    return SourceType.DIRECT_AUDIO


def compute_file_sha256(file_path: Path) -> str | None:
    """Compute SHA-256 hex digest for an existing local file."""
    if not file_path.is_file():
        return None
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


class IngestionQueueWorker:
    """FIFO SQLite-backed job queue with runtime-adjustable concurrency (1..4)."""

    def __init__(
        self,
        pipeline: IngestionPipeline,
        catalog_repository: CatalogRepositoryPort,
        governance_repository: GovernanceRepositoryPort,
        notifier: AdminLogNotifierPort | None = None,
        concurrency_limit: int = 1,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        if not MIN_CONCURRENCY_LIMIT <= concurrency_limit <= MAX_CONCURRENCY_LIMIT:
            msg = f"concurrency_limit must be between {MIN_CONCURRENCY_LIMIT} and {MAX_CONCURRENCY_LIMIT}"
            raise QueueConcurrencyError(msg)

        self._pipeline = pipeline
        self._catalog = catalog_repository
        self._governance = governance_repository
        self._notifier = notifier
        self._concurrency_limit = concurrency_limit
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self._active_count = 0
        self._condition = asyncio.Condition()
        self._wake_event = asyncio.Event()
        self._processing_job_ids: set[str] = set()
        self._progress_callbacks: list[ProgressCallback] = (
            [progress_callback] if progress_callback is not None else []
        )

    @property
    def concurrency_limit(self) -> int:
        """Return the current runtime concurrency limit (1..4)."""
        return self._concurrency_limit

    @property
    def active_count(self) -> int:
        """Return the number of jobs currently executing inside the worker engine."""
        return self._active_count

    def add_progress_callback(self, callback: ProgressCallback) -> None:
        """Register a subscriber callback invoked whenever a job changes stage or completes."""
        self._progress_callbacks.append(callback)

    async def set_concurrency_limit(
        self,
        actor: UserAccount,
        new_limit: int,
    ) -> int:
        """Update the runtime concurrency limit between 1 and 4 (requires ADMIN+)."""
        require_permission(actor, Permission.CHANGE_CONCURRENCY_LIMIT)
        if not MIN_CONCURRENCY_LIMIT <= new_limit <= MAX_CONCURRENCY_LIMIT:
            msg = f"Concurrency limit must be between {MIN_CONCURRENCY_LIMIT} and {MAX_CONCURRENCY_LIMIT}"
            raise QueueConcurrencyError(msg)

        previous = self._concurrency_limit
        async with self._condition:
            self._concurrency_limit = new_limit
            self._semaphore = asyncio.Semaphore(new_limit)
            self._condition.notify_all()

        await self._governance.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.CONCURRENCY_UPDATED,
                target_entity="system:queue",
                details={"previous_limit": previous, "new_limit": new_limit},
            )
        )
        return self._concurrency_limit

    async def enqueue_job(
        self,
        user: UserAccount,
        source_path: Path,
        original_filename: str | None = None,
        job_id: str | None = None,
        file_size_bytes: int | None = None,
    ) -> tuple[IngestionJob, int]:
        """Persist a new job in `QUEUED` state and return `(job, queue_position)`."""
        require_permission(user, Permission.SUBMIT_INGESTION)
        resolved_id = job_id or str(uuid.uuid4())
        resolved_name = original_filename or source_path.name
        disk_size = source_path.stat().st_size if source_path.is_file() else 0
        size_bytes = file_size_bytes if file_size_bytes is not None and file_size_bytes > 0 else disk_size
        sha256_hex = compute_file_sha256(source_path)

        job = IngestionJob(
            job_id=resolved_id,
            source_path=source_path,
            state=JobState.QUEUED,
            created_at=datetime.now(UTC),
            user_id=user.telegram_id,
            source_type=infer_source_type(Path(resolved_name)),
            original_filename=resolved_name,
            file_size_bytes=size_bytes,
            scratch_dir=source_path.parent,
            sha256_hash=sha256_hex,
        )
        await self._catalog.save_job(job)
        await self._governance.append_audit_log(
            AuditLogEntry(
                actor_id=user.telegram_id,
                action=AuditAction.INGEST_QUEUED,
                target_entity=f"job:{job.job_id}",
                details={"filename": resolved_name, "size_bytes": size_bytes},
            )
        )
        position = await self.get_queue_position(job.job_id)
        self._wake_event.set()
        return job, position

    async def get_queue_position(self, job_id: str) -> int:
        """Return 1-indexed FIFO position of `job_id` among `QUEUED` jobs (or 0 if not queued)."""
        queued = await self._governance.list_queued_jobs()
        for index, item in enumerate(queued, start=1):
            if item.job_id == job_id:
                return index
        return 0

    def _authorize_job_control(self, job: IngestionJob, actor: UserAccount) -> None:
        """Verify that `actor` is allowed to control `job` (owner or moderator+)."""
        is_owner_of_job = job.user_id is not None and job.user_id == actor.telegram_id
        if is_owner_of_job:
            require_permission(actor, Permission.VIEW_OWN_JOBS)
        elif not has_permission(actor, Permission.RESOLVE_OTHERS_JOBS):
            raise PermissionDeniedError(
                f"User {actor.telegram_id} cannot modify job {job.job_id} owned by {job.user_id}."
            )

    async def cancel_job(self, job_id: str, actor: UserAccount) -> IngestionJob:
        """Cancel a `QUEUED`, `PAUSED`, or `AWAITING_CONFIRMATION` job if permitted."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown job {job_id}"
            raise KeyError(msg)

        self._authorize_job_control(job, actor)
        return await self._pipeline.cancel_job(job.job_id)

    async def pause_job(self, job_id: str, actor: UserAccount) -> IngestionJob:
        """Pause/hold a pending job so it awaits manual review or explicit unlock."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown job {job_id}"
            raise KeyError(msg)

        self._authorize_job_control(job, actor)
        job.state = JobState.PAUSED
        await self._catalog.save_job(job)
        await self._governance.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.JOB_PAUSED,
                target_entity=f"job:{job.job_id}",
                details={"filename": job.original_filename or job.source_path.name},
            )
        )
        return job

    async def unlock_job(self, job_id: str, actor: UserAccount) -> IngestionJob:
        """Unlock a `PAUSED` job back to `QUEUED` so it can be processed."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown job {job_id}"
            raise KeyError(msg)

        self._authorize_job_control(job, actor)
        job.state = JobState.QUEUED
        await self._catalog.save_job(job)
        await self._governance.append_audit_log(
            AuditLogEntry(
                actor_id=actor.telegram_id,
                action=AuditAction.JOB_UNLOCKED,
                target_entity=f"job:{job.job_id}",
                details={"filename": job.original_filename or job.source_path.name},
            )
        )
        self._wake_event.set()
        return job

    async def review_job_manually(
        self,
        job_id: str,
        actor: UserAccount,
        progress_callback: ProgressCallback | None = None,
    ) -> IngestionResult | None:
        """Prepare and transition a queued/paused job into `AWAITING_CONFIRMATION` for manual review."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown job {job_id}"
            raise KeyError(msg)

        self._authorize_job_control(job, actor)
        result = await self._pipeline.prepare_for_review(job.job_id)
        await self._dispatch_progress(result.job, result, progress_callback)
        return result

    async def process_next_queued_job(self) -> IngestionResult | None:
        """Acquire a concurrency slot, pop the oldest `QUEUED` job in FIFO order, and execute it."""
        queued_jobs = await self._governance.list_queued_jobs()
        if not queued_jobs:
            return None

        target_job = None
        for j in queued_jobs:
            if j.job_id not in self._processing_job_ids and j.state == JobState.QUEUED:
                target_job = j
                break
        if target_job is None:
            return None

        self._processing_job_ids.add(target_job.job_id)
        try:
            return await self.execute_job(target_job.job_id)
        finally:
            self._processing_job_ids.discard(target_job.job_id)

    async def run_worker_loop(self, stop_event: asyncio.Event | None = None) -> None:
        """Autonomous background loop popping oldest QUEUED jobs up to concurrency limit."""
        effective_stop = stop_event or asyncio.Event()
        self._wake_event.set()
        logger.info(
            "IngestionQueueWorker background loop started (concurrency_limit=%d)",
            self._concurrency_limit,
        )
        while not effective_stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), timeout=2.0)
            self._wake_event.clear()
            if effective_stop.is_set():
                break

            while self._active_count < self._concurrency_limit and not effective_stop.is_set():
                queued_jobs = await self._governance.list_queued_jobs()
                next_job = None
                for j in queued_jobs:
                    if j.job_id not in self._processing_job_ids and j.state == JobState.QUEUED:
                        next_job = j
                        break
                if next_job is None:
                    break

                self._processing_job_ids.add(next_job.job_id)
                asyncio.create_task(self._safe_execute_job(next_job.job_id))

    async def _safe_execute_job(self, job_id: str) -> None:
        """Helper to run a queued job inside the worker loop and ensure wake_event triggers afterwards."""
        try:
            await self.execute_job(job_id)
        except Exception as exc:
            logger.error("Background queue worker error on job %s: %s", job_id, exc)
        finally:
            self._processing_job_ids.discard(job_id)
            self._wake_event.set()

    async def _dispatch_progress(
        self,
        job: IngestionJob,
        result: IngestionResult | None,
        call_callback: ProgressCallback | None = None,
    ) -> None:
        """Invoke worker-level and per-call progress callbacks."""
        for cb in self._progress_callbacks:
            try:
                await cb(job, result)
            except Exception as exc:
                logger.warning("Queue progress subscriber failed: %s", exc)
        if call_callback is not None:
            await call_callback(job, result)

    async def execute_job(
        self,
        job_id: str,
        progress_callback: ProgressCallback | None = None,
        force_confirmation: bool = False,
    ) -> IngestionResult | None:
        """Execute a specific queued job under the worker concurrency semaphore."""
        async with self._condition:
            while self._active_count >= self._concurrency_limit:
                await self._condition.wait()
            self._active_count += 1

        try:
            job = await self._catalog.get_job(job_id)
            if job is None or job.state == JobState.CANCELLED:
                return None
            if job.state == JobState.PAUSED and not force_confirmation:
                return None

            if not job.source_path.is_file():
                logger.error("Job %s source file not found on disk: %s", job.job_id, job.source_path)
                job.state = JobState.FAILED
                job.error_message = f"Source audio file not found on disk: {job.source_path}"
                job.completed_at = datetime.now(UTC)
                await self._catalog.save_job(job)
                await self._dispatch_progress(job, None, progress_callback)
                return None

            started_clock = time.monotonic()
            job.state = JobState.PROCESSING
            job.started_at = datetime.now(UTC)
            await self._catalog.save_job(job)

            await self._dispatch_progress(job, None, progress_callback)

            async def _on_stage(updated_job: IngestionJob) -> None:
                await self._dispatch_progress(updated_job, None, progress_callback)

            try:
                result = await self._pipeline.process_file(
                    source_path=job.source_path,
                    job_id=job.job_id,
                    user_id=job.user_id,
                    original_filename=job.original_filename,
                    file_size_bytes=job.file_size_bytes,
                    source_type=job.source_type,
                    sha256_hash=job.sha256_hash,
                    started_at=job.started_at,
                    stage_callback=_on_stage,
                    force_confirmation=force_confirmation,
                )
            except Exception as exc:
                failed_job = await self._catalog.get_job(job.job_id) or job
                failed_job.state = JobState.FAILED
                failed_job.error_message = str(exc)
                failed_job.completed_at = datetime.now(UTC)
                await self._catalog.save_job(failed_job)
                raise

            elapsed = max(0.1, time.monotonic() - started_clock)
            await self._emit_completion_events(result, elapsed)

            await self._dispatch_progress(result.job, result, progress_callback)

            return result
        finally:
            self._processing_job_ids.discard(job_id)
            async with self._condition:
                self._active_count -= 1
                self._condition.notify_all()
            self._wake_event.set()

    async def record_confirmed_completion(
        self,
        result: IngestionResult,
        duration_seconds: float = 1.0,
    ) -> None:
        """Emit audit log and Admin Channel events when an awaiting-confirmation job finishes."""
        await self._emit_completion_events(result, duration_seconds)
        for cb in self._progress_callbacks:
            try:
                await cb(result.job, result)
            except Exception as exc:
                logger.warning("Queue progress subscriber failed on confirmed completion: %s", exc)

    async def recover_pending_jobs(self) -> list[IngestionJob]:
        """Reset any interrupted in-flight jobs back to `QUEUED` on startup and return the FIFO queue."""
        active = await self._governance.list_active_jobs()
        for job in active:
            if job.state not in {JobState.AWAITING_CONFIRMATION, JobState.AWAITING_INPUT, JobState.PAUSED}:
                job.state = JobState.QUEUED
                await self._catalog.save_job(job)
        self._wake_event.set()
        return await self._governance.list_queued_jobs()

    async def _emit_completion_events(
        self,
        result: IngestionResult,
        duration_seconds: float,
    ) -> None:
        """Record audit log entries and dispatch Admin Log Channel alerts for completed/duplicate jobs."""
        job = result.job
        uploader_id = job.user_id or 0
        uploader = (await self._governance.get_user(uploader_id) if uploader_id else None) or UserAccount(
            telegram_id=uploader_id, display_name=f"User {uploader_id}"
        )

        if result.duplicate is not None and result.duplicate.is_duplicate:
            conflict_path = result.duplicate.existing_path or Path("unknown")
            match_reason = (
                "Exact AcoustID Fingerprint Hash Match"
                if result.duplicate.match_reason == "fingerprint"
                else "MusicBrainz Recording MBID Match"
            )
            detected_label = job.original_filename or job.source_path.stem
            await self._governance.append_audit_log(
                AuditLogEntry(
                    actor_id=uploader.telegram_id,
                    action=AuditAction.DUPLICATE_BLOCKED,
                    target_entity=f"job:{job.job_id}",
                    details={
                        "conflict_path": str(conflict_path),
                        "match_reason": result.duplicate.match_reason,
                    },
                )
            )
            if self._notifier is not None:
                await self._notifier.send_duplicate_rejected_audit(
                    job=job,
                    uploader=uploader,
                    detected_track=detected_label,
                    conflict_path=conflict_path,
                    match_source=match_reason,
                )
            return

        if job.state == JobState.COMPLETED and job.selected_metadata is not None:
            meta = job.selected_metadata
            ext = (job.final_path.suffix.lstrip(".").upper() if job.final_path else "FLAC") or "FLAC"
            tracks_written = max(1, result.tracks_ingested)
            lyrics_embedded = (
                result.lyrics_embedded_count
                if result.lyrics_embedded_count > 0
                else (1 if result.lyrics_attached else 0)
            )
            await self._governance.append_audit_log(
                AuditLogEntry(
                    actor_id=uploader.telegram_id,
                    action=AuditAction.INGEST_SUCCESS,
                    target_entity=f"job:{job.job_id}",
                    details={
                        "album": meta.album,
                        "artist": meta.artist,
                        "title": meta.title,
                        "final_path": str(job.final_path),
                        "tracks_written": tracks_written,
                        "duration_seconds": round(duration_seconds, 2),
                    },
                )
            )
            if self._notifier is not None:
                await self._notifier.send_ingest_success_audit(
                    job=job,
                    uploader=uploader,
                    metadata=meta,
                    tracks_written=tracks_written,
                    audio_format=ext,
                    lyrics_embedded=lyrics_embedded,
                    duration_seconds=duration_seconds,
                )
