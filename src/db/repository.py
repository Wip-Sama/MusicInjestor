"""SQLite implementation of `CatalogRepositoryPort` and `GovernanceRepositoryPort`."""

import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from src.core.models import (
    AccessRequestRecord,
    AccessRequestStatus,
    AudioFingerprint,
    AuditLogEntry,
    DuplicateCheckResult,
    IngestionJob,
    JobState,
    MetadataCandidate,
    SourceType,
    TrackMetadata,
    UserAccount,
    UserRole,
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


def _parse_iso_datetime(value: object) -> datetime | None:
    """Parse an optional ISO-8601 timestamp string into a UTC-aware `datetime`."""
    if value is None or str(value).strip() == "":
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _row_to_user(row: tuple[object, ...]) -> UserAccount:
    """Map a `users` table row into a `UserAccount` domain object."""
    raw_groups = str(row[7]) if row[7] is not None else "[]"
    try:
        groups_list = json.loads(raw_groups)
        groups = tuple(str(g) for g in groups_list) if isinstance(groups_list, list) else ()
    except Exception:
        groups = ()

    created_dt = _parse_iso_datetime(row[12]) or datetime.now(UTC)
    updated_dt = _parse_iso_datetime(row[13]) or created_dt

    return UserAccount(
        telegram_id=int(str(row[0])),
        username=str(row[1]) if row[1] is not None else None,
        display_name=str(row[2]),
        role=UserRole(str(row[3])),
        is_blocked=bool(int(str(row[4]))),
        authentik_sub=str(row[5]) if row[5] is not None else None,
        authentik_username=str(row[6]) if row[6] is not None else None,
        authentik_groups=groups,
        access_token_encrypted=str(row[8]) if row[8] is not None else None,
        refresh_token_encrypted=str(row[9]) if row[9] is not None else None,
        token_expires_at=_parse_iso_datetime(row[10]),
        last_oidc_sync=_parse_iso_datetime(row[11]),
        created_at=created_dt,
        updated_at=updated_dt,
    )


def _row_to_access_request(row: tuple[object, ...]) -> AccessRequestRecord:
    """Map an `access_requests` table row into an `AccessRequestRecord`."""
    created_dt = _parse_iso_datetime(row[5]) or datetime.now(UTC)
    return AccessRequestRecord(
        id=int(str(row[0])),
        telegram_id=int(str(row[1])),
        status=AccessRequestStatus(str(row[2])),
        reviewed_by=int(str(row[3])) if row[3] is not None else None,
        reviewed_at=_parse_iso_datetime(row[4]),
        created_at=created_dt,
    )


def _row_to_job(row: tuple[object, ...]) -> IngestionJob:
    """Map an `ingestion_jobs` table row into an `IngestionJob` domain aggregate."""
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
    created_dt = _parse_iso_datetime(row[8]) or datetime.now(UTC)

    return IngestionJob(
        job_id=str(row[0]),
        source_path=Path(str(row[1])),
        state=JobState(str(row[2])),
        candidates=candidates,
        selected_metadata=selected_meta,
        fingerprint=fingerprint,
        final_path=Path(str(row[6])) if row[6] is not None else None,
        error_message=str(row[7]) if row[7] is not None else None,
        created_at=created_dt,
        user_id=int(str(row[9])) if row[9] is not None else None,
        source_type=SourceType(str(row[10])) if row[10] is not None else SourceType.DIRECT_AUDIO,
        original_filename=str(row[11]) if row[11] is not None else "",
        file_size_bytes=int(str(row[12])) if row[12] is not None else 0,
        scratch_dir=Path(str(row[13])) if row[13] else None,
        sha256_hash=str(row[14]) if row[14] is not None else None,
        confidence_score=float(str(row[15])) if row[15] is not None else None,
        matched_release_mbid=str(row[16]) if row[16] is not None else None,
        started_at=_parse_iso_datetime(row[17]),
        completed_at=_parse_iso_datetime(row[18]),
    )


class SqliteCatalogRepository:
    """Concrete SQLite adapter implementing both `CatalogRepositoryPort` and `GovernanceRepositoryPort`."""

    def __init__(self, db_manager: SqliteDatabaseManager) -> None:
        self._db = db_manager

    # -------------------------------------------------------------------------
    # CatalogRepositoryPort implementation
    # -------------------------------------------------------------------------

    async def check_duplicate(
        self,
        fingerprint: str,
        recording_mbid: str | None = None,
    ) -> DuplicateCheckResult:
        """Query `catalog_tracks` and `library_tracks` for an existing fingerprint or recording MBID."""
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

            lib_cursor = await conn.execute(
                "SELECT storage_path FROM library_tracks WHERE fingerprint = ? LIMIT 1",
                (fingerprint,),
            )
            lib_row = await lib_cursor.fetchone()
            if lib_row is not None:
                return DuplicateCheckResult(
                    is_duplicate=True,
                    existing_path=Path(str(lib_row[0])),
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
        uploaded_by: int = 0,
        has_synced_lyrics: bool = False,
    ) -> None:
        """Insert a finalized track and its fingerprint into `catalog_tracks` and `library_tracks`."""
        now_iso = datetime.now(UTC).isoformat()
        track_id = metadata.recording_mbid or fingerprint.acoustid_mbid or str(uuid.uuid4())
        resolved_uploader = uploaded_by
        async with self._db.connect() as conn:
            if resolved_uploader == 0:
                job_cur = await conn.execute(
                    "SELECT user_id FROM ingestion_jobs WHERE job_id = ? LIMIT 1",
                    (job_id,),
                )
                job_row = await job_cur.fetchone()
                if job_row is not None and job_row[0] is not None:
                    resolved_uploader = int(str(job_row[0]))

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
            await conn.execute(
                """
                INSERT OR REPLACE INTO library_tracks (
                    id, job_id, uploaded_by, storage_path, fingerprint,
                    duration_seconds, artist, album, title, year,
                    track_number, has_synced_lyrics, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track_id,
                    job_id,
                    resolved_uploader,
                    str(library_path),
                    fingerprint.fingerprint,
                    float(fingerprint.duration_seconds),
                    metadata.artist,
                    metadata.album,
                    metadata.title,
                    metadata.year,
                    metadata.track_number,
                    1 if has_synced_lyrics else 0,
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
                    job_id, id, user_id, state, status, source_type,
                    source_path, original_filename, file_size_bytes, scratch_dir,
                    sha256_hash, confidence_score, matched_release_mbid,
                    candidates_json, selected_metadata_json, fingerprint_json,
                    final_path, error_message, created_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.job_id,
                    job.user_id,
                    str(job.state),
                    str(job.state),
                    str(job.source_type),
                    str(job.source_path),
                    job.original_filename or job.source_path.name,
                    job.file_size_bytes,
                    str(job.scratch_dir) if job.scratch_dir is not None else "",
                    job.sha256_hash,
                    job.confidence_score,
                    job.matched_release_mbid,
                    json.dumps(candidates_payload),
                    selected_json,
                    fingerprint_json,
                    str(job.final_path) if job.final_path is not None else None,
                    job.error_message,
                    job.created_at.isoformat(),
                    job.started_at.isoformat() if job.started_at is not None else None,
                    job.completed_at.isoformat() if job.completed_at is not None else None,
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
                       final_path, error_message, created_at,
                       user_id, source_type, original_filename,
                       file_size_bytes, scratch_dir, sha256_hash,
                       confidence_score, matched_release_mbid,
                       started_at, completed_at
                FROM ingestion_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None

        return _row_to_job(tuple(row))

    # -------------------------------------------------------------------------
    # GovernanceRepositoryPort implementation
    # -------------------------------------------------------------------------

    async def get_user(self, telegram_id: int) -> UserAccount | None:
        """Retrieve a user account by Telegram numeric ID."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT telegram_id, username, display_name, role, is_blocked,
                       authentik_sub, authentik_username, authentik_groups,
                       access_token_encrypted, refresh_token_encrypted,
                       token_expires_at, last_oidc_sync, created_at, updated_at
                FROM users
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
        return _row_to_user(tuple(row))

    async def get_user_by_authentik_sub(self, authentik_sub: str) -> UserAccount | None:
        """Retrieve a user account by Authentik OIDC subject UUID."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT telegram_id, username, display_name, role, is_blocked,
                       authentik_sub, authentik_username, authentik_groups,
                       access_token_encrypted, refresh_token_encrypted,
                       token_expires_at, last_oidc_sync, created_at, updated_at
                FROM users
                WHERE authentik_sub = ?
                """,
                (authentik_sub,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
        return _row_to_user(tuple(row))

    async def upsert_user(self, user: UserAccount) -> UserAccount:
        """Insert or update a user account record in `users`."""
        groups_json = json.dumps(list(user.authentik_groups))
        async with self._db.connect() as conn:
            await conn.execute(
                """
                INSERT INTO users (
                    telegram_id, username, display_name, role, is_blocked,
                    authentik_sub, authentik_username, authentik_groups,
                    access_token_encrypted, refresh_token_encrypted,
                    token_expires_at, last_oidc_sync, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name,
                    role = excluded.role,
                    is_blocked = excluded.is_blocked,
                    authentik_sub = excluded.authentik_sub,
                    authentik_username = excluded.authentik_username,
                    authentik_groups = excluded.authentik_groups,
                    access_token_encrypted = excluded.access_token_encrypted,
                    refresh_token_encrypted = excluded.refresh_token_encrypted,
                    token_expires_at = excluded.token_expires_at,
                    last_oidc_sync = excluded.last_oidc_sync,
                    updated_at = excluded.updated_at
                """,
                (
                    user.telegram_id,
                    user.username,
                    user.display_name,
                    user.role.value,
                    1 if user.is_blocked else 0,
                    user.authentik_sub,
                    user.authentik_username,
                    groups_json,
                    user.access_token_encrypted,
                    user.refresh_token_encrypted,
                    user.token_expires_at.isoformat() if user.token_expires_at else None,
                    user.last_oidc_sync.isoformat() if user.last_oidc_sync else None,
                    user.created_at.isoformat(),
                    user.updated_at.isoformat(),
                ),
            )
            await conn.commit()
        return user

    async def list_users(self, role: UserRole | None = None) -> list[UserAccount]:
        """List all registered users ordered by `telegram_id`, optionally filtered by role."""
        async with self._db.connect() as conn:
            if role is not None:
                cursor = await conn.execute(
                    """
                    SELECT telegram_id, username, display_name, role, is_blocked,
                           authentik_sub, authentik_username, authentik_groups,
                           access_token_encrypted, refresh_token_encrypted,
                           token_expires_at, last_oidc_sync, created_at, updated_at
                    FROM users
                    WHERE role = ?
                    ORDER BY telegram_id ASC
                    """,
                    (role.value,),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT telegram_id, username, display_name, role, is_blocked,
                           authentik_sub, authentik_username, authentik_groups,
                           access_token_encrypted, refresh_token_encrypted,
                           token_expires_at, last_oidc_sync, created_at, updated_at
                    FROM users
                    ORDER BY telegram_id ASC
                    """
                )
            rows = await cursor.fetchall()
        return [_row_to_user(tuple(r)) for r in rows]

    async def list_oidc_linked_users(self) -> list[UserAccount]:
        """List all active (non-blocked) users linked to an Authentik OIDC subject."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT telegram_id, username, display_name, role, is_blocked,
                       authentik_sub, authentik_username, authentik_groups,
                       access_token_encrypted, refresh_token_encrypted,
                       token_expires_at, last_oidc_sync, created_at, updated_at
                FROM users
                WHERE authentik_sub IS NOT NULL AND is_blocked = 0
                ORDER BY telegram_id ASC
                """
            )
            rows = await cursor.fetchall()
        return [_row_to_user(tuple(r)) for r in rows]

    async def count_user_uploads(self, telegram_id: int) -> tuple[int, int]:
        """Return `(albums_count, tracks_count)` uploaded by `telegram_id`."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT COUNT(DISTINCT album), COUNT(*)
                FROM library_tracks
                WHERE uploaded_by = ?
                """,
                (telegram_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return 0, 0
            return int(row[0] or 0), int(row[1] or 0)

    async def create_access_request(self, telegram_id: int) -> AccessRequestRecord:
        """Create a new PENDING access request for `telegram_id`."""
        now_dt = datetime.now(UTC)
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO access_requests (telegram_id, status, created_at)
                VALUES (?, ?, ?)
                """,
                (telegram_id, AccessRequestStatus.PENDING.value, now_dt.isoformat()),
            )
            await conn.commit()
            request_id = int(cursor.lastrowid or 0)
        return AccessRequestRecord(
            id=request_id,
            telegram_id=telegram_id,
            status=AccessRequestStatus.PENDING,
            created_at=now_dt,
        )

    async def get_pending_access_request(self, telegram_id: int) -> AccessRequestRecord | None:
        """Return an existing PENDING access request for `telegram_id`, if any."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, telegram_id, status, reviewed_by, reviewed_at, created_at
                FROM access_requests
                WHERE telegram_id = ? AND status = 'PENDING'
                ORDER BY id DESC
                LIMIT 1
                """,
                (telegram_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
        return _row_to_access_request(tuple(row))

    async def get_access_request_by_id(self, request_id: int) -> AccessRequestRecord | None:
        """Retrieve an access request record by its primary key ID."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, telegram_id, status, reviewed_by, reviewed_at, created_at
                FROM access_requests
                WHERE id = ?
                """,
                (request_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
        return _row_to_access_request(tuple(row))

    async def list_pending_access_requests(self) -> list[AccessRequestRecord]:
        """Return all currently PENDING access requests ordered by creation time."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, telegram_id, status, reviewed_by, reviewed_at, created_at
                FROM access_requests
                WHERE status = 'PENDING'
                ORDER BY id ASC
                """
            )
            rows = await cursor.fetchall()
        return [_row_to_access_request(tuple(r)) for r in rows]

    async def resolve_access_request(
        self,
        request_id: int,
        status: AccessRequestStatus,
        reviewed_by: int,
    ) -> AccessRequestRecord | None:
        """Mark an access request as APPROVED or REJECTED by `reviewed_by`."""
        now_dt = datetime.now(UTC)
        async with self._db.connect() as conn:
            await conn.execute(
                """
                UPDATE access_requests
                SET status = ?, reviewed_by = ?, reviewed_at = ?
                WHERE id = ?
                """,
                (status.value, reviewed_by, now_dt.isoformat(), request_id),
            )
            await conn.commit()
        return await self.get_access_request_by_id(request_id)

    async def append_audit_log(self, entry: AuditLogEntry) -> AuditLogEntry:
        """Persist an immutable audit log record and return the stored entry with its ID."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO audit_logs (actor_id, action, target_entity, details_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entry.actor_id,
                    str(entry.action),
                    entry.target_entity,
                    json.dumps(entry.details),
                    entry.created_at.isoformat(),
                ),
            )
            await conn.commit()
            row_id = int(cursor.lastrowid or 0)
        return AuditLogEntry(
            id=row_id,
            actor_id=entry.actor_id,
            action=entry.action,
            target_entity=entry.target_entity,
            details=entry.details,
            created_at=entry.created_at,
        )

    async def list_audit_logs(self, limit: int = 20) -> list[AuditLogEntry]:
        """Retrieve the most recent audit log records ordered newest-first."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, actor_id, action, target_entity, details_json, created_at
                FROM audit_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()

        entries: list[AuditLogEntry] = []
        for row in rows:
            raw_details = str(row[4]) if row[4] is not None else "{}"
            try:
                details = json.loads(raw_details)
            except Exception:
                details = {}
            entries.append(
                AuditLogEntry(
                    id=int(str(row[0])),
                    actor_id=int(str(row[1])),
                    action=str(row[2]),
                    target_entity=str(row[3]) if row[3] is not None else None,
                    details=details if isinstance(details, dict) else {},
                    created_at=_parse_iso_datetime(row[5]) or datetime.now(UTC),
                )
            )
        return entries

    async def list_queued_jobs(self, user_id: int | None = None) -> list[IngestionJob]:
        """Return jobs in QUEUED state ordered FIFO by `created_at`."""
        async with self._db.connect() as conn:
            if user_id is not None:
                cursor = await conn.execute(
                    """
                    SELECT job_id, source_path, state, candidates_json,
                           selected_metadata_json, fingerprint_json,
                           final_path, error_message, created_at,
                           user_id, source_type, original_filename,
                           file_size_bytes, scratch_dir, sha256_hash,
                           confidence_score, matched_release_mbid,
                           started_at, completed_at
                    FROM ingestion_jobs
                    WHERE state = 'QUEUED' AND user_id = ?
                    ORDER BY created_at ASC
                    """,
                    (user_id,),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT job_id, source_path, state, candidates_json,
                           selected_metadata_json, fingerprint_json,
                           final_path, error_message, created_at,
                           user_id, source_type, original_filename,
                           file_size_bytes, scratch_dir, sha256_hash,
                           confidence_score, matched_release_mbid,
                           started_at, completed_at
                    FROM ingestion_jobs
                    WHERE state = 'QUEUED'
                    ORDER BY created_at ASC
                    """
                )
            rows = await cursor.fetchall()
        return [_row_to_job(tuple(r)) for r in rows]

    async def list_active_jobs(self) -> list[IngestionJob]:
        """Return jobs currently actively processing or awaiting disambiguation."""
        active_states = (
            JobState.PROCESSING.value,
            JobState.EXTRACTING.value,
            JobState.FINGERPRINTING.value,
            JobState.ENRICHING_METADATA.value,
            JobState.AWAITING_CONFIRMATION.value,
            JobState.AWAITING_INPUT.value,
            JobState.FETCHING_LYRICS.value,
            JobState.ORGANIZING.value,
        )
        placeholders = ", ".join("?" for _ in active_states)
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"""
                SELECT job_id, source_path, state, candidates_json,
                       selected_metadata_json, fingerprint_json,
                       final_path, error_message, created_at,
                       user_id, source_type, original_filename,
                       file_size_bytes, scratch_dir, sha256_hash,
                       confidence_score, matched_release_mbid,
                       started_at, completed_at
                FROM ingestion_jobs
                WHERE state IN ({placeholders})
                ORDER BY created_at ASC
                """,
                active_states,
            )
            rows = await cursor.fetchall()
        return [_row_to_job(tuple(r)) for r in rows]

    async def cancel_queued_jobs_for_user(self, user_id: int) -> int:
        """Cancel all QUEUED jobs belonging to `user_id` and return the count cancelled."""
        now_iso = datetime.now(UTC).isoformat()
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                UPDATE ingestion_jobs
                SET state = 'CANCELLED', status = 'CANCELLED', completed_at = ?
                WHERE user_id = ? AND state = 'QUEUED'
                """,
                (now_iso, user_id),
            )
            await conn.commit()
            return int(cursor.rowcount or 0)
