"""Planned bulk metadata and lyrics re-search service with rate-throttled execution."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import MusicBrainzMetadataService
from src.core.models import AuditAction, AuditLogEntry, LibraryTrackRecord, TrackMetadata
from src.core.organizer import LibraryOrganizer
from src.core.ports import GovernanceRepositoryPort
from src.core.throttler import ApiThrottler, default_api_throttler

logger = logging.getLogger(__name__)

BulkProgressCallback = Callable[[int, int, str, str], Any]


@dataclass(frozen=True)
class BulkResearchPlan:
    """Preview and estimated resource footprint for planned bulk metadata research."""

    track_ids: list[str]
    total_tracks: int
    missing_lyrics_count: int
    missing_mbid_count: int
    estimated_seconds: int
    search_metadata: bool = True
    search_lyrics: bool = True
    auto_apply: bool = True


@dataclass
class BulkResearchReport:
    """Execution summary report produced after running a bulk research plan."""

    total_tracks: int
    applied_count: int = 0
    needs_review_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def is_success(self) -> bool:
        """Return True if at least one track was successfully enriched and no fatal failures occurred."""
        return self.applied_count > 0 or self.needs_review_count > 0


@dataclass
class BulkResearchProgress:
    """Live snapshot of an actively running bulk re-search job."""

    total_tracks: int
    current_index: int = 0
    current_track: str = ""
    status: str = "running"
    actor_id: int = 0
    actor_name: str = "System"
    started_at: float = field(default_factory=time.time)
    applied_count: int = 0
    needs_review_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0

    @property
    def percent(self) -> int:
        """Percentage of tracks processed so far (0..100)."""
        if self.total_tracks <= 0:
            return 0
        return min(100, int((self.current_index / self.total_tracks) * 100))


_active_bulk_research: BulkResearchProgress | None = None


def get_active_bulk_research() -> BulkResearchProgress | None:
    """Return the currently executing bulk research job progress, or None if idle."""
    return _active_bulk_research


def set_active_bulk_research(progress: BulkResearchProgress | None) -> None:
    """Set or clear the active bulk research job progress."""
    global _active_bulk_research
    _active_bulk_research = progress


class BulkResearchService:
    """Orchestrates planned bulk metadata and lyrics re-search with polite API throttling."""

    def __init__(
        self,
        catalog_repository: GovernanceRepositoryPort,
        metadata_service: MusicBrainzMetadataService,
        lyrics_client: LrclibLyricsClient,
        organizer: LibraryOrganizer,
        throttler: ApiThrottler | None = None,
    ) -> None:
        self._repo = catalog_repository
        self._metadata = metadata_service
        self._lyrics = lyrics_client
        self._organizer = organizer
        self._throttler = throttler or default_api_throttler
        self._cancel_requested = False

    def cancel(self) -> None:
        """Request cooperative cancellation of an active bulk research run."""
        self._cancel_requested = True

    async def plan_bulk_research(
        self,
        track_ids: list[str] | None = None,
        *,
        filter_mode: str = "all",
        search_metadata: bool = True,
        search_lyrics: bool = True,
        auto_apply: bool = True,
    ) -> BulkResearchPlan:
        """Analyze selected tracks and return a structured dry-run plan with timing estimates."""
        tracks: list[LibraryTrackRecord] = []
        if track_ids:
            for tid in track_ids:
                record = await self._repo.get_library_track(tid)
                if record is not None and record.storage_path.is_file():
                    tracks.append(record)
        else:
            all_records = await self._repo.list_library_tracks(limit=10000, offset=0)
            for record in all_records:
                if not record.storage_path.is_file():
                    continue
                if filter_mode == "missing_lyrics" and record.has_synced_lyrics:
                    continue
                if filter_mode == "missing_mbid" and record.year is not None:
                    continue
                tracks.append(record)

        missing_lyrics = sum(1 for t in tracks if not t.has_synced_lyrics)
        missing_mbid = sum(1 for t in tracks if t.year is None)

        # Estimate duration respecting rate limiters:
        # MusicBrainz: 1.0s, LRCLIB: ~0.35s
        sec_per_track = 0.0
        if search_metadata:
            sec_per_track += 1.0
        if search_lyrics:
            sec_per_track += 0.35
        sec_per_track = max(0.5, sec_per_track)

        estimated_seconds = int(round(len(tracks) * sec_per_track))

        return BulkResearchPlan(
            track_ids=[t.id for t in tracks],
            total_tracks=len(tracks),
            missing_lyrics_count=missing_lyrics,
            missing_mbid_count=missing_mbid,
            estimated_seconds=estimated_seconds,
            search_metadata=search_metadata,
            search_lyrics=search_lyrics,
            auto_apply=auto_apply,
        )

    async def execute_bulk_research(
        self,
        plan: BulkResearchPlan,
        *,
        actor_id: int | None = None,
        actor_name: str = "System",
        progress_callback: BulkProgressCallback | None = None,
    ) -> BulkResearchReport:
        """Execute a bulk research plan, honoring rate limiters and recording audit logs."""
        self._cancel_requested = False
        start_time = time.monotonic()
        report = BulkResearchReport(total_tracks=len(plan.track_ids))

        active_progress = BulkResearchProgress(
            total_tracks=len(plan.track_ids),
            current_index=0,
            current_track="Initializing...",
            status="running",
            actor_id=actor_id or 0,
            actor_name=actor_name,
            started_at=time.time(),
        )
        set_active_bulk_research(active_progress)

        try:
            for idx, track_id in enumerate(plan.track_ids, start=1):
                if self._cancel_requested:
                    logger.info(
                        "Bulk research cancelled cooperatively at track %d/%d",
                        idx,
                        len(plan.track_ids),
                    )
                    active_progress.status = "cancelled"
                    break

                record = await self._repo.get_library_track(track_id)
                if record is None or not record.storage_path.is_file():
                    report.skipped_count += 1
                    continue

                track_label = f"{record.artist} - {record.title}"
                active_progress.current_index = idx
                active_progress.current_track = track_label
                active_progress.applied_count = report.applied_count
                active_progress.needs_review_count = report.needs_review_count
                active_progress.skipped_count = report.skipped_count
                active_progress.failed_count = report.failed_count

                if progress_callback:
                    try:
                        res = progress_callback(idx, len(plan.track_ids), track_label, "searching")
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as exc:
                        logger.debug("Progress callback error: %s", exc)

                try:
                    base_meta = TrackMetadata(
                        title=record.title,
                        artist=record.artist,
                        album=record.album,
                        year=record.year,
                        track_number=record.track_number or 1,
                        duration_seconds=int(round(record.duration_seconds)),
                    )

                    best_meta = base_meta
                    if plan.search_metadata:
                        seed_meta = await self._metadata.read_local_metadata(record.storage_path)
                        candidates = await self._metadata.find_candidates(seed_meta)
                        if candidates:
                            best_meta = candidates[0].metadata

                    fetched_lyrics_text: str | None = None
                    if plan.search_lyrics:
                        fetched_lrc = await self._lyrics.fetch_lyrics(best_meta)
                        if fetched_lrc is not None:
                            fetched_lyrics_text = fetched_lrc.synced_lyrics or fetched_lrc.plain_lyrics

                    effective_year = best_meta.year or record.year
                    effective_meta = (
                        TrackMetadata(
                            title=best_meta.title,
                            artist=best_meta.artist,
                            album=best_meta.album,
                            album_artist=best_meta.album_artist,
                            year=effective_year,
                            track_number=best_meta.track_number,
                            disc_number=best_meta.disc_number,
                            genre=best_meta.genre,
                            duration_seconds=best_meta.duration_seconds,
                            recording_mbid=best_meta.recording_mbid,
                            release_mbid=best_meta.release_mbid,
                            isrc=best_meta.isrc,
                        )
                        if best_meta.year is None
                        else best_meta
                    )
                    has_meta_change = (
                        effective_meta.title != record.title
                        or effective_meta.artist != record.artist
                        or effective_meta.album != record.album
                        or (effective_meta.year is not None and effective_meta.year != record.year)
                    )
                    has_lyrics_change = fetched_lyrics_text is not None and not record.has_synced_lyrics

                    if not has_meta_change and not has_lyrics_change:
                        report.skipped_count += 1
                        report.details.append(
                            {
                                "track_id": record.id,
                                "title": record.title,
                                "status": "unchanged",
                            }
                        )
                        continue

                    if plan.auto_apply:
                        # Apply changes to audio file and sidecars
                        new_path = record.storage_path
                        if has_meta_change:
                            new_path = await self._organizer.relocate_and_retag_track(
                                current_path=record.storage_path,
                                metadata=effective_meta,
                            )

                        if fetched_lyrics_text:
                            await self._organizer.update_track_lyrics(new_path, fetched_lyrics_text)

                        # Update SQLite catalog
                        updated_record = LibraryTrackRecord(
                            id=record.id,
                            job_id=record.job_id,
                            uploaded_by=record.uploaded_by,
                            storage_path=new_path,
                            fingerprint=record.fingerprint,
                            duration_seconds=record.duration_seconds,
                            artist=effective_meta.artist,
                            album=effective_meta.album,
                            title=effective_meta.title,
                            year=effective_meta.year,
                            track_number=effective_meta.track_number or record.track_number,
                            has_synced_lyrics=bool(fetched_lyrics_text or record.has_synced_lyrics),
                            ingested_at=record.ingested_at,
                            updated_at=record.updated_at,
                            modified_by=actor_id,
                            modification_count=record.modification_count + 1,
                        )
                        await self._repo.upsert_scanned_library_track(updated_record)

                        # Record audit entry
                        await self._repo.append_audit_log(
                            AuditLogEntry(
                                actor_id=actor_id or 0,
                                action=AuditAction.BULK_METADATA_ENRICHED,
                                target_entity=record.id,
                                details={
                                    "actor_name": actor_name,
                                    "old_title": record.title,
                                    "new_title": best_meta.title,
                                    "old_artist": record.artist,
                                    "new_artist": best_meta.artist,
                                    "lyrics_added": bool(fetched_lyrics_text),
                                },
                            )
                        )
                        report.applied_count += 1
                        report.details.append(
                            {
                                "track_id": record.id,
                                "title": best_meta.title,
                                "status": "applied",
                            }
                        )
                    else:
                        report.needs_review_count += 1
                        report.details.append(
                            {
                                "track_id": record.id,
                                "title": effective_meta.title,
                                "status": "needs_review",
                                "proposed_meta": {
                                    "title": effective_meta.title,
                                    "artist": effective_meta.artist,
                                    "album": effective_meta.album,
                                    "year": effective_meta.year,
                                    "track_number": effective_meta.track_number or record.track_number or 1,
                                    "recording_mbid": effective_meta.recording_mbid,
                                    "release_mbid": effective_meta.release_mbid,
                                },
                                "proposed_lyrics": fetched_lyrics_text,
                            }
                        )
                except Exception as exc:
                    logger.warning("Bulk research failed for %s: %s", record.id, exc)
                    report.failed_count += 1
                    report.details.append(
                        {
                            "track_id": record.id,
                            "title": record.title,
                            "status": "error",
                            "error": str(exc),
                        }
                    )

            report.elapsed_seconds = round(time.monotonic() - start_time, 2)
            if progress_callback:
                try:
                    res = progress_callback(
                        len(plan.track_ids),
                        len(plan.track_ids),
                        "All done",
                        "completed",
                    )
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

            logger.info(
                "Bulk research finished: %d applied, %d needs review, %d skipped, %d failed in %.2fs",
                report.applied_count,
                report.needs_review_count,
                report.skipped_count,
                report.failed_count,
                report.elapsed_seconds,
            )
            return report
        finally:
            set_active_bulk_research(None)
