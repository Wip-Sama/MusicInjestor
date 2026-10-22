"""High-level IngestionPipeline coordinating extraction, deduplication, enrichment, and filing."""

import contextlib
import hashlib
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from difflib import SequenceMatcher
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
    LyricsData,
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

StageCallback = Callable[[IngestionJob], Awaitable[None]]


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
    if top.confidence >= 0.98:
        return False
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


def _discover_staged_audio_files(
    job: IngestionJob,
    extractor: ArchiveExtractorPort | None = None,
) -> list[Path]:
    """Locate staged audio files in `job.scratch_dir`, `extractor` scratch root,
    or fall back to `job.source_path`.
    """
    candidate_dirs: list[Path] = []
    if job.scratch_dir is not None:
        candidate_dirs.append(job.scratch_dir)
        raw_scratch = str(job.scratch_dir).replace("\\", "/")
        if "/storage/scratch/" in raw_scratch:
            rel_scratch = raw_scratch.split("/storage/scratch/", 1)[1]
            candidate_dirs.append(Path("storage/scratch") / rel_scratch)
            candidate_dirs.append(Path("./storage/scratch") / rel_scratch)
            candidate_dirs.append(Path("/app/storage/scratch") / rel_scratch)
        with contextlib.suppress(Exception):
            candidate_dirs.append(Path(job.scratch_dir.name))

    if extractor is not None:
        get_dir_fn = getattr(extractor, "get_job_scratch_dir", None)
        if callable(get_dir_fn):
            with contextlib.suppress(Exception):
                res = get_dir_fn(job.job_id)
                if inspect.iscoroutine(res):
                    res.close()
                elif isinstance(res, Path):
                    candidate_dirs.append(res)
        scratch_root = getattr(extractor, "_scratch_root", None)
        if isinstance(scratch_root, Path):
            candidate_dirs.append(scratch_root / job.job_id)

    seen_dirs: set[Path] = set()
    for d in candidate_dirs:
        try:
            resolved_d = d.resolve() if d.is_dir() else d
        except Exception:
            resolved_d = d
        if resolved_d in seen_dirs:
            continue
        seen_dirs.add(resolved_d)
        if d.is_dir():
            found = sorted(
                path
                for path in d.rglob("*")
                if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
            )
            if found:
                return found

    if job.source_path.is_file():
        return [job.source_path]

    raw_src = str(job.source_path).replace("\\", "/")
    if "/telegram-bot-api/" in raw_src:
        rel_part = raw_src.split("/telegram-bot-api/", 1)[1]
        host_candidate = Path("./storage/telegram-bot-api") / Path(rel_part)
        if host_candidate.is_file():
            return [host_candidate]
        container_candidate = Path("/var/lib/telegram-bot-api") / Path(rel_part)
        if container_candidate.is_file():
            return [container_candidate]

    return [job.source_path]


def _match_track_from_release(
    local_meta: TrackMetadata,
    ordinal_index: int,
    release_tracks: list[TrackMetadata],
    fallback_release: TrackMetadata,
    known_duration: int | None = None,
) -> TrackMetadata:
    """Map a local track in a multi-track archive against the MusicBrainz release `medium-list`."""
    target_disc = local_meta.disc_number or fallback_release.disc_number or 1
    target_track_num = local_meta.track_number or ordinal_index

    matched_rel_track: TrackMetadata | None = None
    if release_tracks:
        # 1. Exact (disc_number, track_number) match
        for rel_trk in release_tracks:
            if (rel_trk.disc_number or 1) == target_disc and rel_trk.track_number == target_track_num:
                matched_rel_track = rel_trk
                break

        # 2. Match by track_number on single-disc or when local disc is unspecified
        if matched_rel_track is None:
            for rel_trk in release_tracks:
                if rel_trk.track_number == target_track_num:
                    matched_rel_track = rel_trk
                    break

        # 3. High title similarity match
        if matched_rel_track is None and local_meta.title not in {"", "Unknown Title"}:
            best_ratio = 0.0
            for rel_trk in release_tracks:
                ratio = SequenceMatcher(
                    None,
                    local_meta.title.strip().lower(),
                    rel_trk.title.strip().lower(),
                ).ratio()
                if ratio > best_ratio and ratio >= 0.80:
                    best_ratio = ratio
                    matched_rel_track = rel_trk

        # 4. Ordinal position fallback when track counts align
        if matched_rel_track is None and 1 <= ordinal_index <= len(release_tracks):
            matched_rel_track = release_tracks[ordinal_index - 1]

    if matched_rel_track is not None:
        resolved_artist = (
            matched_rel_track.artist
            if matched_rel_track.artist and matched_rel_track.artist != "Unknown Artist"
            else (local_meta.artist if local_meta.artist != "Unknown Artist" else fallback_release.artist)
        )
        return TrackMetadata(
            title=matched_rel_track.title or local_meta.title,
            artist=resolved_artist,
            album=fallback_release.album,
            album_artist=fallback_release.effective_album_artist,
            year=fallback_release.year or matched_rel_track.year,
            track_number=matched_rel_track.track_number or target_track_num,
            disc_number=matched_rel_track.disc_number or target_disc,
            genre=local_meta.genre or fallback_release.genre,
            duration_seconds=known_duration
            or local_meta.duration_seconds
            or matched_rel_track.duration_seconds,
            recording_mbid=matched_rel_track.recording_mbid or local_meta.recording_mbid,
            release_mbid=fallback_release.release_mbid,
            isrc=local_meta.isrc or matched_rel_track.isrc,
        )

    return TrackMetadata(
        title=local_meta.title,
        artist=(local_meta.artist if local_meta.artist != "Unknown Artist" else fallback_release.artist),
        album=fallback_release.album,
        album_artist=fallback_release.effective_album_artist,
        year=fallback_release.year,
        track_number=target_track_num,
        disc_number=target_disc,
        genre=local_meta.genre or fallback_release.genre,
        duration_seconds=known_duration or local_meta.duration_seconds,
        recording_mbid=local_meta.recording_mbid,
        release_mbid=fallback_release.release_mbid,
        isrc=local_meta.isrc,
    )


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

    async def _notify_stage(
        self,
        job: IngestionJob,
        stage_callback: StageCallback | None,
    ) -> None:
        """Persist `job` state and invoke `stage_callback` if registered."""
        await self._catalog.save_job(job)
        if stage_callback is not None:
            await stage_callback(job)

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

    async def get_job(self, job_id: str) -> IngestionJob | None:
        """Retrieve a persisted `IngestionJob` by its identifier."""
        return await self._catalog.get_job(job_id)

    async def _lookup_release_tracklist(
        self,
        selected_metadata: TrackMetadata,
    ) -> list[TrackMetadata]:
        """Fetch the release tracklist from `self._metadata` if `fetch_release_tracks` is supported."""
        if not selected_metadata.release_mbid:
            return []
        fetch_fn = getattr(self._metadata, "fetch_release_tracks", None)
        if not callable(fetch_fn):
            return []
        try:
            result = await fetch_fn(
                selected_metadata.release_mbid,
                prefer_english=selected_metadata.artist.isascii(),
            )
            if isinstance(result, list):
                return [item for item in result if isinstance(item, TrackMetadata)]
        except Exception as exc:
            logger.debug(
                "Release tracklist lookup failed for %s: %s",
                selected_metadata.release_mbid,
                exc,
            )
        return []

    async def prepare_for_review(self, job_id: str) -> IngestionResult:
        """Stage, fingerprint, and enrich candidates for `job_id` without waiting on queue slots."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown ingestion job: {job_id}"
            raise KeyError(msg)

        job.state = JobState.AWAITING_CONFIRMATION
        await self._catalog.save_job(job)

        if not job.candidates:
            discovered = _discover_staged_audio_files(job, extractor=self._extractor)
            if discovered and discovered[0].is_file():
                staged_files = discovered
                primary_audio = staged_files[0]
                job.scratch_dir = primary_audio.parent
                job.total_tracks = len(staged_files)
            elif job.source_path.is_file():
                staged_files = await self._extractor.extract_or_stage(job.source_path, job.job_id)
                primary_audio = staged_files[0]
                job.scratch_dir = primary_audio.parent
                job.total_tracks = len(staged_files)
            else:
                primary_audio = discovered[0] if discovered else job.source_path

            if primary_audio.is_file():
                raw_local_meta = await self._metadata.read_local_metadata(primary_audio)
                local_meta = _merge_original_filename_metadata(
                    raw_local_meta,
                    primary_audio.stem,
                    job.original_filename,
                )
                if job.fingerprint is None:
                    job.fingerprint = await self._compute_fingerprint_with_fallback(
                        primary_audio,
                        known_duration=local_meta.duration_seconds,
                    )
                candidates = await self._metadata.find_candidates(local_meta, job.fingerprint)
                job.candidates = candidates
                if candidates:
                    job.confidence_score = candidates[0].confidence
                    job.matched_release_mbid = candidates[0].metadata.release_mbid
                    job.selected_metadata = candidates[0].metadata
                else:
                    job.selected_metadata = local_meta
        elif job.selected_metadata is None:
            if job.candidates:
                job.selected_metadata = job.candidates[0].metadata
            else:
                stem = Path(job.original_filename or job.source_path.name).stem
                job.selected_metadata = parse_filename_metadata(stem)

        job.state = JobState.AWAITING_CONFIRMATION
        await self._catalog.save_job(job)
        return IngestionResult(
            job=job,
            requires_user_confirmation=True,
            tracks_ingested=0,
            lyrics_embedded_count=0,
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
        stage_callback: StageCallback | None = None,
        force_confirmation: bool = False,
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
        await self._notify_stage(job, stage_callback)
        staged_files = await self._extractor.extract_or_stage(source_path, resolved_job_id)
        primary_audio = staged_files[0]
        job.scratch_dir = primary_audio.parent
        job.total_tracks = len(staged_files)

        raw_local_meta = await self._metadata.read_local_metadata(primary_audio)
        local_meta = _merge_original_filename_metadata(
            raw_local_meta,
            primary_audio.stem,
            job.original_filename,
        )

        job.state = JobState.FINGERPRINTING
        await self._notify_stage(job, stage_callback)
        fingerprint = await self._compute_fingerprint_with_fallback(
            primary_audio,
            known_duration=local_meta.duration_seconds,
        )
        job.fingerprint = fingerprint

        dup_check = await self._deduplicator.check_track(fingerprint, metadata=local_meta)
        if dup_check.is_duplicate:
            job.state = JobState.DUPLICATE_REJECTED
            job.completed_at = datetime.now(UTC)
            job.final_path = dup_check.existing_path
            await self._catalog.save_job(job)
            await self._extractor.cleanup_job_scratch(resolved_job_id)
            return IngestionResult(
                job=job,
                requires_user_confirmation=False,
                duplicate=dup_check,
                tracks_ingested=0,
                lyrics_embedded_count=0,
            )

        persisted_midflight = await self._catalog.get_job(resolved_job_id)
        was_paused = persisted_midflight is not None and persisted_midflight.state == JobState.PAUSED
        if persisted_midflight is not None and persisted_midflight.state == JobState.CANCELLED:
            await self._extractor.cleanup_job_scratch(resolved_job_id)
            return IngestionResult(
                job=persisted_midflight,
                requires_user_confirmation=False,
                tracks_ingested=0,
                lyrics_embedded_count=0,
            )

        job.state = JobState.ENRICHING_METADATA
        if not was_paused:
            await self._notify_stage(job, stage_callback)
        candidates = await self._metadata.find_candidates(local_meta, fingerprint)
        job.candidates = candidates
        if candidates:
            job.confidence_score = candidates[0].confidence
            job.matched_release_mbid = candidates[0].metadata.release_mbid
            job.selected_metadata = candidates[0].metadata
            enriched_dup = await self._deduplicator.check_track(
                fingerprint,
                recording_mbid=candidates[0].metadata.recording_mbid,
                metadata=candidates[0].metadata,
            )
            if enriched_dup.is_duplicate:
                job.state = JobState.DUPLICATE_REJECTED
                job.completed_at = datetime.now(UTC)
                job.final_path = enriched_dup.existing_path
                await self._catalog.save_job(job)
                await self._extractor.cleanup_job_scratch(resolved_job_id)
                return IngestionResult(
                    job=job,
                    requires_user_confirmation=False,
                    duplicate=enriched_dup,
                    tracks_ingested=0,
                    lyrics_embedded_count=0,
                )
        else:
            job.selected_metadata = local_meta

        latest_persisted = await self._catalog.get_job(resolved_job_id)
        if latest_persisted is not None and latest_persisted.state in {
            JobState.PAUSED,
            JobState.AWAITING_CONFIRMATION,
            JobState.AWAITING_INPUT,
        }:
            was_paused = True

        if (
            force_confirmation
            or was_paused
            or _requires_confirmation(candidates, self._auto_approve_confidence)
        ):
            job.state = JobState.AWAITING_CONFIRMATION
            await self._catalog.save_job(job)
            logger.info(
                "Job %s requires interactive confirmation (%d candidate(s), forced=%s)",
                resolved_job_id,
                len(candidates),
                force_confirmation or was_paused,
            )
            return IngestionResult(
                job=job,
                requires_user_confirmation=True,
                tracks_ingested=0,
                lyrics_embedded_count=0,
            )

        selected = candidates[0].metadata
        return await self._complete_ingestion(
            job,
            primary_audio,
            selected,
            extra_audio_paths=staged_files[1:],
            stage_callback=stage_callback,
        )

    async def confirm_candidate(
        self,
        job_id: str,
        candidate_index: int,
        staged_audio_path: Path | None = None,
        stage_callback: StageCallback | None = None,
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
            discovered = _discover_staged_audio_files(job, extractor=self._extractor)
            if not discovered:
                msg = f"No staged audio files found for job {job_id}"
                raise FileNotFoundError(msg)
            primary_audio = discovered[0]
            extra_paths = discovered[1:]

        if not primary_audio.is_file():
            msg = f"Staged audio file does not exist for job {job_id}: {primary_audio}"
            raise FileNotFoundError(msg)

        job.total_tracks = 1 + len(extra_paths)
        chosen = job.candidates[candidate_index]
        job.confidence_score = chosen.confidence
        job.matched_release_mbid = chosen.metadata.release_mbid
        return await self._complete_ingestion(
            job,
            primary_audio,
            chosen.metadata,
            extra_audio_paths=extra_paths,
            stage_callback=stage_callback,
        )

    async def complete_custom_ingestion(
        self,
        job_id: str,
        metadata: TrackMetadata,
        *,
        custom_lyrics: LyricsData | None = None,
        custom_cover_bytes: bytes | None = None,
        custom_cover_mime: str = "image/jpeg",
        stage_callback: StageCallback | None = None,
    ) -> IngestionResult:
        """Finalize a reviewed job using custom/edited metadata, optional custom lyrics, and cover art."""
        job = await self._catalog.get_job(job_id)
        if job is None:
            msg = f"Unknown ingestion job: {job_id}"
            raise KeyError(msg)

        discovered = _discover_staged_audio_files(job, extractor=self._extractor)
        if (
            len(discovered) == 1
            and discovered[0] == job.source_path
            and job.scratch_dir is None
            and job.source_path.is_file()
        ):
            staged_files = await self._extractor.extract_or_stage(job.source_path, job.job_id)
            job.scratch_dir = staged_files[0].parent
            discovered = staged_files

        if not discovered:
            msg = f"No staged audio files found for job {job_id}"
            raise FileNotFoundError(msg)

        primary_audio = discovered[0]
        extra_paths = discovered[1:]
        if not primary_audio.is_file():
            msg = f"Staged audio file does not exist for job {job_id}: {primary_audio}"
            raise FileNotFoundError(msg)

        job.total_tracks = 1 + len(extra_paths)

        if job.fingerprint is None and primary_audio.is_file():
            job.fingerprint = await self._compute_fingerprint_with_fallback(
                primary_audio,
                known_duration=metadata.duration_seconds,
            )

        if custom_cover_bytes and primary_audio.parent.is_dir():
            (primary_audio.parent / "cover.jpg").write_bytes(custom_cover_bytes)

        result = await self._complete_ingestion(
            job,
            primary_audio,
            metadata,
            extra_audio_paths=extra_paths,
            stage_callback=stage_callback,
            custom_lyrics=custom_lyrics,
        )

        if custom_cover_bytes and result.job.final_path is not None and result.job.final_path.is_file():
            update_cover_fn = getattr(self._organizer, "update_track_cover_art", None)
            if callable(update_cover_fn):
                await update_cover_fn(
                    result.job.final_path,
                    custom_cover_bytes,
                    mime_type=custom_cover_mime,
                )

        return result

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

        discovered = _discover_staged_audio_files(job, extractor=self._extractor)
        if not discovered:
            msg = f"No staged audio files found for job {job_id}"
            raise FileNotFoundError(msg)

        job.total_tracks = len(discovered)
        primary_audio = discovered[0]
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
            job.selected_metadata = candidates[0].metadata
        else:
            job.selected_metadata = manual_meta
        await self._catalog.save_job(job)
        return IngestionResult(
            job=job,
            requires_user_confirmation=True,
            tracks_ingested=0,
            lyrics_embedded_count=0,
        )

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
        stage_callback: StageCallback | None = None,
        custom_lyrics: LyricsData | None = None,
    ) -> IngestionResult:
        """Execute stages 8–10: fetch release tracklist, lyrics, write tags & move file(s), cleanup."""
        try:
            extra_list = extra_audio_paths or []
            job.total_tracks = max(job.total_tracks, 1 + len(extra_list))
            release_tracks: list[TrackMetadata] = []
            if extra_list and selected_metadata.release_mbid:
                release_tracks = await self._lookup_release_tracklist(selected_metadata)

            primary_meta = selected_metadata
            if release_tracks and not primary_meta.recording_mbid:
                primary_meta = _match_track_from_release(
                    local_meta=primary_meta,
                    ordinal_index=1,
                    release_tracks=release_tracks,
                    fallback_release=selected_metadata,
                    known_duration=primary_meta.duration_seconds,
                )

            job.selected_metadata = primary_meta
            job.matched_release_mbid = primary_meta.release_mbid or job.matched_release_mbid

            final_dup = await self._deduplicator.check_track(
                job.fingerprint,
                recording_mbid=primary_meta.recording_mbid,
                metadata=primary_meta,
            )
            if final_dup.is_duplicate:
                job.state = JobState.DUPLICATE_REJECTED
                job.completed_at = datetime.now(UTC)
                job.final_path = final_dup.existing_path
                await self._catalog.save_job(job)
                await self._extractor.cleanup_job_scratch(job.job_id)
                return IngestionResult(
                    job=job,
                    requires_user_confirmation=False,
                    duplicate=final_dup,
                    tracks_ingested=0,
                    lyrics_embedded_count=0,
                )

            job.state = JobState.FETCHING_LYRICS
            await self._notify_stage(job, stage_callback)
            lyrics = (
                custom_lyrics if custom_lyrics is not None else await self._lyrics.fetch_lyrics(primary_meta)
            )
            lyrics_count = 1 if (lyrics and lyrics.has_any) else 0
            job.lyrics_matched_count = lyrics_count

            job.state = JobState.ORGANIZING
            await self._notify_stage(job, stage_callback)
            final_path = await self._organizer.finalize_track(
                source_audio_path=audio_path,
                metadata=primary_meta,
                lyrics=lyrics,
            )
            job.final_path = final_path
            tracks_written = 1
            job.processed_tracks = 1

            has_synced_primary = False
            if lyrics is not None and lyrics.has_synced:
                has_synced_primary = True
            else:
                read_fn = getattr(self._organizer, "read_track_lyrics", None)
                if callable(read_fn):
                    read_lyr = await read_fn(final_path)
                    has_synced_primary = bool(read_lyr and read_lyr.has_synced)

            if job.fingerprint is not None:
                try:
                    await self._catalog.register_ingested_track(
                        job_id=job.job_id,
                        metadata=primary_meta,
                        fingerprint=job.fingerprint,
                        library_path=final_path,
                        has_synced_lyrics=has_synced_primary,
                    )
                except TypeError:
                    await self._catalog.register_ingested_track(
                        job_id=job.job_id,
                        metadata=primary_meta,
                        fingerprint=job.fingerprint,
                        library_path=final_path,
                    )

            for index, extra_path in enumerate(extra_list, start=2):
                extra_local = await self._metadata.read_local_metadata(extra_path)
                extra_fp = await self._compute_fingerprint_with_fallback(
                    extra_path,
                    known_duration=extra_local.duration_seconds,
                )
                extra_meta = _match_track_from_release(
                    local_meta=extra_local,
                    ordinal_index=index,
                    release_tracks=release_tracks,
                    fallback_release=selected_metadata,
                    known_duration=extra_fp.duration_seconds or extra_local.duration_seconds,
                )
                if extra_meta.recording_mbid and not extra_fp.acoustid_mbid:
                    extra_fp = AudioFingerprint(
                        duration_seconds=extra_fp.duration_seconds,
                        fingerprint=extra_fp.fingerprint,
                        acoustid_mbid=extra_meta.recording_mbid,
                    )
                extra_dup = await self._deduplicator.check_track(
                    extra_fp,
                    recording_mbid=extra_meta.recording_mbid,
                    metadata=extra_meta,
                )
                if extra_dup.is_duplicate:
                    continue

                extra_lyrics = await self._lyrics.fetch_lyrics(extra_meta)
                if extra_lyrics and extra_lyrics.has_any:
                    lyrics_count += 1
                    job.lyrics_matched_count = lyrics_count

                extra_final = await self._organizer.finalize_track(
                    source_audio_path=extra_path,
                    metadata=extra_meta,
                    lyrics=extra_lyrics,
                )
                has_synced_extra = False
                if extra_lyrics is not None and extra_lyrics.has_synced:
                    has_synced_extra = True
                else:
                    read_fn = getattr(self._organizer, "read_track_lyrics", None)
                    if callable(read_fn):
                        read_extra_lyr = await read_fn(extra_final)
                        has_synced_extra = bool(read_extra_lyr and read_extra_lyr.has_synced)

                try:
                    await self._catalog.register_ingested_track(
                        job_id=job.job_id,
                        metadata=extra_meta,
                        fingerprint=extra_fp,
                        library_path=extra_final,
                        has_synced_lyrics=has_synced_extra,
                    )
                except TypeError:
                    await self._catalog.register_ingested_track(
                        job_id=job.job_id,
                        metadata=extra_meta,
                        fingerprint=extra_fp,
                        library_path=extra_final,
                    )
                tracks_written += 1
                job.processed_tracks = tracks_written

            job.state = JobState.COMPLETED
            job.completed_at = datetime.now(UTC)
            await self._notify_stage(job, stage_callback)

            await self._extractor.cleanup_job_scratch(job.job_id)
            return IngestionResult(
                job=job,
                requires_user_confirmation=False,
                lyrics_attached=lyrics_count > 0,
                tracks_ingested=tracks_written,
                lyrics_embedded_count=lyrics_count,
            )
        except Exception as exc:
            job.state = JobState.FAILED
            job.error_message = str(exc)
            job.completed_at = datetime.now(UTC)
            await self._catalog.save_job(job)
            with contextlib.suppress(Exception):
                await self._notify_stage(job, stage_callback)
            raise
