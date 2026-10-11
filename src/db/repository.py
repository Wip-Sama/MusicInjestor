"""SQLite implementation of the core `CatalogRepositoryPort` protocol."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from src.core.models import (
    AudioFingerprint,
    DuplicateCheckResult,
    IngestionJob,
    JobState,
    MetadataCandidate,
    TrackMetadata,
)
from src.db.database import SqliteDatabaseManager


def _serialize_metadata(metadata: TrackMetadata) -> dict[str, object]:
    """Convert TrackMetadata dataclass into a JSON-serializable dictionary."""
    return asdict(metadata)


def _deserialize_metadata(data: dict[str, object]) -> TrackMetadata:
    """Reconstruct TrackMetadata from a deserialized JSON dictionary."""
    return TrackMetadata(
        title=str(data["title"]),
        artist=str(data["artist"]),
        album=str(data["album"]),
        album_artist=str(data["album_artist"]) if data.get("album_artist") is not None else None,
        year=int(str(data["year"])) if data.get("year") is not None else None,
        track_number=(int(str(data["track_number"])) if data.get("track_number") is not None else None),
        disc_number=int(str(data["disc_number"])) if data.get("disc_number") is not None else None,
        genre=str(data["genre"]) if data.get("genre") is not None else None,
        duration_seconds=(
            int(str(data["duration_seconds"])) if data.get("duration_seconds") is not None else None
        ),
        recording_mbid=(str(data["recording_mbid"]) if data.get("recording_mbid") is not None else None),
        release_mbid=str(data["release_mbid"]) if data.get("release_mbid") is not None else None,
    )


class SqliteCatalogRepository:
    """Concrete SQLite adapter implementing `CatalogRepositoryPort`."""

    def __init__(self, db_manager: SqliteDatabaseManager) -> None:
        self._db = db_manager

    async def check_duplicate(
        self,
        fingerprint: str,
        recording_mbid: str | None = None,
    ) -> DuplicateCheckResult:
        """Query `catalog_tracks` for an existing fingerprint or canonical MusicBrainz recording ID."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                "SELECT library_path FROM catalog_tracks WHERE fingerprint = ? LIMIT 1",
                (fingerprint,),
            )
            row = await cursor.fetchone()
            if row is not None:
                return DuplicateCheckResult(
                    is_duplicate=True,
                    existing_path=Path(str(row[0])),
                    match_reason="fingerprint",
                )

            if recording_mbid:
                mbid_cursor = await conn.execute(
                    "SELECT library_path FROM catalog_tracks WHERE recording_mbid = ? LIMIT 1",
                    (recording_mbid,),
                )
                mbid_row = await mbid_cursor.fetchone()
                if mbid_row is not None:
                    return DuplicateCheckResult(
                        is_duplicate=True,
                        existing_path=Path(str(mbid_row[0])),
                        match_reason="recording_mbid",
                    )

        return DuplicateCheckResult(is_duplicate=False)

    async def register_ingested_track(
        self,
        job_id: str,
        metadata: TrackMetadata,
        fingerprint: AudioFingerprint,
        library_path: Path,
    ) -> None:
        """Insert a finalized track and its fingerprint into `catalog_tracks`."""
        now_iso = datetime.now(UTC).isoformat()
        async with self._db.connect() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO catalog_tracks (
                    job_id, title, artist, album, album_artist, year,
                    track_number, disc_number, duration_seconds, fingerprint,
                    recording_mbid, release_mbid, library_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    metadata.title,
                    metadata.artist,
                    metadata.album,
                    metadata.album_artist,
                    metadata.year,
                    metadata.track_number,
                    metadata.disc_number,
                    fingerprint.duration_seconds,
                    fingerprint.fingerprint,
                    metadata.recording_mbid or fingerprint.acoustid_mbid,
                    metadata.release_mbid,
                    str(library_path),
                    now_iso,
                ),
            )
            await conn.commit()

    async def save_job(self, job: IngestionJob) -> None:
        """Upsert an `IngestionJob` state snapshot in `ingestion_jobs`."""
        candidates_payload = [
            {
                "metadata": _serialize_metadata(candidate.metadata),
                "confidence": candidate.confidence,
                "release_type": candidate.release_type,
                "disambiguation": candidate.disambiguation,
            }
            for candidate in job.candidates
        ]
        selected_json = (
            json.dumps(_serialize_metadata(job.selected_metadata))
            if job.selected_metadata is not None
            else None
        )
        fingerprint_json = json.dumps(asdict(job.fingerprint)) if job.fingerprint is not None else None

        async with self._db.connect() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO ingestion_jobs (
                    job_id, source_path, state, candidates_json,
                    selected_metadata_json, fingerprint_json,
                    final_path, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    str(job.source_path),
                    str(job.state),
                    json.dumps(candidates_payload),
                    selected_json,
                    fingerprint_json,
                    str(job.final_path) if job.final_path is not None else None,
                    job.error_message,
                    job.created_at.isoformat(),
                ),
            )
            await conn.commit()

    async def get_job(self, job_id: str) -> IngestionJob | None:
        """Load an `IngestionJob` by `job_id` from `ingestion_jobs`."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT job_id, source_path, state, candidates_json,
                       selected_metadata_json, fingerprint_json,
                       final_path, error_message, created_at
                FROM ingestion_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None

        raw_candidates = json.loads(str(row[3]))
        candidates = [
            MetadataCandidate(
                metadata=_deserialize_metadata(item["metadata"]),
                confidence=float(item["confidence"]),
                release_type=str(item.get("release_type", "Album")),
                disambiguation=str(item.get("disambiguation", "")),
            )
            for item in raw_candidates
        ]
        selected_meta = _deserialize_metadata(json.loads(str(row[4]))) if row[4] is not None else None
        fp_data = json.loads(str(row[5])) if row[5] is not None else None
        fingerprint = (
            AudioFingerprint(
                duration_seconds=int(fp_data["duration_seconds"]),
                fingerprint=str(fp_data["fingerprint"]),
                acoustid_mbid=(
                    str(fp_data["acoustid_mbid"]) if fp_data.get("acoustid_mbid") is not None else None
                ),
            )
            if fp_data is not None
            else None
        )

        return IngestionJob(
            job_id=str(row[0]),
            source_path=Path(str(row[1])),
            state=JobState(str(row[2])),
            candidates=candidates,
            selected_metadata=selected_meta,
            fingerprint=fingerprint,
            final_path=Path(str(row[6])) if row[6] is not None else None,
            error_message=str(row[7]) if row[7] is not None else None,
            created_at=datetime.fromisoformat(str(row[8])),
        )
