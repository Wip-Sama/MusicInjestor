"""SQLite implementation of `CatalogRepositoryPort` and `GovernanceRepositoryPort`."""

import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from src.core.metadata import (
    _best_album_similarity,
    _best_artist_similarity,
    _best_title_similarity,
    normalize_primary_artist,
    parse_filename_metadata,
)
from src.core.models import (
    AccessRequestRecord,
    AccessRequestStatus,
    AudioFingerprint,
    AuditLogEntry,
    DuplicateCheckResult,
    IngestionJob,
    JobState,
    LibraryTrackRecord,
    MetadataCandidate,
    SourceType,
    TagCorrectionRequest,
    TagCorrectionStatus,
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
        isrc=str(data["isrc"]) if data.get("isrc") is not None else None,
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
        metadata: TrackMetadata | None = None,
    ) -> DuplicateCheckResult:
        """Query `catalog_tracks` and `library_tracks` for existing fingerprint, MBID, or similarity."""
        async with self._db.connect() as conn:
            if fingerprint:
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

            if metadata is not None and (metadata.title or metadata.artist):
                norm_artist = normalize_primary_artist(metadata.artist).strip()
                norm_title = metadata.title.strip()
                norm_album = metadata.album.strip() if metadata.album else ""
                src_duration = (
                    float(metadata.duration_seconds)
                    if metadata.duration_seconds is not None and metadata.duration_seconds > 0
                    else None
                )

                candidate_rows: list[Sequence[object]] = []
                seen_paths: set[str] = set()

                if norm_artist and norm_artist.lower() != "unknown artist":
                    artist_cur = await conn.execute(
                        """
                        SELECT storage_path, artist, album, title, duration_seconds
                        FROM library_tracks
                        WHERE LOWER(artist) LIKE ? OR LOWER(storage_path) LIKE ?
                        LIMIT 500
                        """,
                        (f"%{norm_artist.lower()}%", f"%{norm_artist.lower()}%"),
                    )
                    for r in await artist_cur.fetchall():
                        path_str = str(r[0])
                        if path_str not in seen_paths:
                            seen_paths.add(path_str)
                            candidate_rows.append(r)

                parsed_title = parse_filename_metadata(norm_title).title
                title_token = (
                    parsed_title if parsed_title and parsed_title.lower() != "unknown title" else norm_title
                )
                if title_token and title_token.lower() != "unknown title" and len(title_token) >= 4:
                    title_cur = await conn.execute(
                        """
                        SELECT storage_path, artist, album, title, duration_seconds
                        FROM library_tracks
                        WHERE LOWER(title) LIKE ?
                        LIMIT 500
                        """,
                        (f"%{title_token.lower()}%",),
                    )
                    for r in await title_cur.fetchall():
                        path_str = str(r[0])
                        if path_str not in seen_paths:
                            seen_paths.add(path_str)
                            candidate_rows.append(r)

                if not candidate_rows:
                    count_cur = await conn.execute("SELECT COUNT(*) FROM library_tracks")
                    count_row = await count_cur.fetchone()
                    total_tracks = int(count_row[0]) if count_row is not None else 0
                    if total_tracks <= 2500:
                        all_cur = await conn.execute(
                            """
                            SELECT storage_path, artist, album, title, duration_seconds
                            FROM library_tracks
                            LIMIT 2500
                            """
                        )
                        for r in await all_cur.fetchall():
                            path_str = str(r[0])
                            if path_str not in seen_paths:
                                seen_paths.add(path_str)
                                candidate_rows.append(r)

                for row in candidate_rows:
                    cand_path = Path(str(row[0]))
                    cand_artist = str(row[1]) if row[1] is not None else ""
                    cand_album = str(row[2]) if row[2] is not None else ""
                    cand_title = str(row[3]) if row[3] is not None else ""
                    cand_duration = (
                        float(str(row[4])) if row[4] is not None and float(str(row[4])) > 0 else None
                    )

                    cand_artist_val = cand_artist
                    if not cand_artist_val or cand_artist_val.lower() == "unknown artist":
                        cand_artist_val = cand_path.parent.parent.name
                    artist_sim = _best_artist_similarity(metadata.artist, cand_artist_val)

                    title_sim = _best_title_similarity(metadata.title, cand_title)
                    if cand_path.stem:
                        stem_sim = _best_title_similarity(metadata.title, cand_path.stem)
                        title_sim = max(title_sim, stem_sim)

                    duration_mismatch = False
                    if src_duration is not None and cand_duration is not None:
                        diff = abs(src_duration - cand_duration)
                        if diff > 18.0:
                            duration_mismatch = True

                    if duration_mismatch:
                        continue

                    album_sim = 0.0
                    if norm_album and cand_album:
                        album_sim = _best_album_similarity(norm_album, cand_album)

                    is_dup = (
                        (artist_sim >= 0.85 and title_sim >= 0.85)
                        or (title_sim >= 0.95 and artist_sim >= 0.75)
                        or (album_sim >= 0.85 and title_sim >= 0.85 and artist_sim >= 0.70)
                    )

                    if is_dup:
                        return DuplicateCheckResult(
                            is_duplicate=True,
                            existing_path=cand_path,
                            match_reason="similarity",
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
            resolved_user_id = job.user_id
            if resolved_user_id is None:
                cur_u = await conn.execute(
                    "SELECT user_id FROM ingestion_jobs WHERE job_id = ? LIMIT 1",
                    (job.job_id,),
                )
                row_u = await cur_u.fetchone()
                if row_u is not None and row_u[0] is not None:
                    resolved_user_id = int(str(row_u[0]))

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
                    resolved_user_id,
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
        """Load an `IngestionJob` by `job_id` (or prefix) from `ingestion_jobs`."""
        clean_id = job_id.strip()
        if not clean_id:
            return None
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
                WHERE job_id = ? OR job_id LIKE ?
                ORDER BY CASE WHEN job_id = ? THEN 0 ELSE 1 END, created_at DESC
                LIMIT 1
                """,
                (clean_id, f"{clean_id}%", clean_id),
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
                   OR job_id IN (SELECT job_id FROM ingestion_jobs WHERE user_id = ?)
                """,
                (telegram_id, telegram_id),
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
        return [_row_to_audit_log(tuple(row)) for row in rows]

    async def list_user_audit_logs(self, actor_id: int, limit: int = 50) -> list[AuditLogEntry]:
        """Retrieve audit log records performed by `actor_id` ordered chronologically newest-first."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, actor_id, action, target_entity, details_json, created_at
                FROM audit_logs
                WHERE actor_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (actor_id, limit),
            )
            rows = await cursor.fetchall()
        return [_row_to_audit_log(tuple(row)) for row in rows]

    async def list_entity_audit_logs(self, target_entity: str, limit: int = 50) -> list[AuditLogEntry]:
        """Retrieve audit log records targeting `target_entity` ordered chronologically newest-first."""
        clean_t = target_entity.strip()
        if not clean_t:
            return []
        short_prefix = clean_t[:12]
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, actor_id, action, target_entity, details_json, created_at
                FROM audit_logs
                WHERE target_entity = ?
                   OR target_entity = ?
                   OR target_entity = ?
                   OR target_entity LIKE ?
                   OR target_entity LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    clean_t,
                    f"track:{clean_t}",
                    f"job:{clean_t}",
                    f"{short_prefix}%",
                    f"track:{short_prefix}%",
                    limit,
                ),
            )
            rows = await cursor.fetchall()
        return [_row_to_audit_log(tuple(row)) for row in rows]

    async def list_user_uploaded_tracks(
        self,
        telegram_id: int,
        limit: int = 50,
    ) -> list[LibraryTrackRecord]:
        """List library tracks uploaded by `telegram_id` ordered chronologically newest-first."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"""
                SELECT {_LIBRARY_TRACK_COLUMNS}
                FROM library_tracks
                WHERE uploaded_by = ?
                   OR job_id IN (SELECT job_id FROM ingestion_jobs WHERE user_id = ?)
                ORDER BY ingested_at DESC
                LIMIT ?
                """,
                (telegram_id, telegram_id, limit),
            )
            rows = await cursor.fetchall()
        return [_row_to_library_track(tuple(r)) for r in rows]

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

    async def list_paused_jobs(self, user_id: int | None = None) -> list[IngestionJob]:
        """Return jobs in PAUSED state ordered FIFO by `created_at`."""
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
                    WHERE state = 'PAUSED' AND user_id = ?
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
                    WHERE state = 'PAUSED'
                    ORDER BY created_at ASC
                    """
                )
            rows = await cursor.fetchall()
        return [_row_to_job(tuple(r)) for r in rows]

    async def list_active_jobs(self) -> list[IngestionJob]:
        """Return jobs currently actively processing, paused, or awaiting disambiguation."""
        active_states = (
            JobState.PAUSED.value,
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
        """Cancel all QUEUED or PAUSED jobs belonging to `user_id` and return the count cancelled."""
        now_iso = datetime.now(UTC).isoformat()
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                UPDATE ingestion_jobs
                SET state = 'CANCELLED', status = 'CANCELLED', completed_at = ?
                WHERE user_id = ? AND state IN ('QUEUED', 'PAUSED')
                """,
                (now_iso, user_id),
            )
            await conn.commit()
            return int(cursor.rowcount or 0)

    async def get_library_track(self, track_id: str) -> LibraryTrackRecord | None:
        """Retrieve an ingested library track record by its primary key ID (or prefix)."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"""
                SELECT {_LIBRARY_TRACK_COLUMNS}
                FROM library_tracks
                WHERE id = ? OR id LIKE ? OR job_id = ? OR job_id LIKE ?
                ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, ingested_at DESC
                LIMIT 1
                """,
                (track_id, f"{track_id}%", track_id, f"{track_id}%", track_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
        return _row_to_library_track(tuple(row))

    async def list_library_tracks(self, limit: int = 20) -> list[LibraryTrackRecord]:
        """List recently ingested library tracks ordered newest-first."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"""
                SELECT {_LIBRARY_TRACK_COLUMNS}
                FROM library_tracks
                ORDER BY ingested_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [_row_to_library_track(tuple(r)) for r in rows]

    async def search_library_tracks(
        self,
        query: str,
        limit: int = 20,
    ) -> list[LibraryTrackRecord]:
        """Search library tracks by title, artist, album, or ID/job_id substring (case-insensitive)."""
        clean_q = query.strip()
        if not clean_q:
            return await self.list_library_tracks(limit=limit)
        pattern = f"%{clean_q}%"
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"""
                SELECT {_LIBRARY_TRACK_COLUMNS}
                FROM library_tracks
                WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? OR id LIKE ? OR job_id LIKE ?
                ORDER BY artist ASC, album ASC, COALESCE(track_number, 1) ASC
                LIMIT ?
                """,
                (pattern, pattern, pattern, f"{clean_q}%", f"{clean_q}%", limit),
            )
            rows = await cursor.fetchall()
        return [_row_to_library_track(tuple(r)) for r in rows]

    async def list_album_tracks(
        self,
        artist: str,
        album: str,
    ) -> list[LibraryTrackRecord]:
        """Return all library tracks matching `artist` and `album` ordered by track number."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                f"""
                SELECT {_LIBRARY_TRACK_COLUMNS}
                FROM library_tracks
                WHERE lower(artist) = lower(?) AND lower(album) = lower(?)
                ORDER BY COALESCE(track_number, 1) ASC, title ASC
                """,
                (artist.strip(), album.strip()),
            )
            rows = await cursor.fetchall()
        return [_row_to_library_track(tuple(r)) for r in rows]

    async def create_tag_correction_request(
        self,
        request: TagCorrectionRequest,
    ) -> TagCorrectionRequest:
        """Create a new PENDING post-ingestion tag correction request."""
        now_dt = datetime.now(UTC)
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO tag_correction_requests (
                    track_id, requested_by, proposed_title, proposed_artist,
                    proposed_album, proposed_year, reason, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.track_id,
                    request.requested_by,
                    request.proposed_title,
                    request.proposed_artist,
                    request.proposed_album,
                    request.proposed_year,
                    request.reason,
                    TagCorrectionStatus.PENDING.value,
                    now_dt.isoformat(),
                ),
            )
            await conn.commit()
            row_id = int(cursor.lastrowid or 0)
        return TagCorrectionRequest(
            id=row_id,
            track_id=request.track_id,
            requested_by=request.requested_by,
            proposed_title=request.proposed_title,
            proposed_artist=request.proposed_artist,
            proposed_album=request.proposed_album,
            proposed_year=request.proposed_year,
            reason=request.reason,
            status=TagCorrectionStatus.PENDING,
            created_at=now_dt,
        )

    async def get_tag_correction_request(
        self,
        request_id: int,
    ) -> TagCorrectionRequest | None:
        """Retrieve a tag correction request by ID."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, track_id, requested_by, proposed_title, proposed_artist,
                       proposed_album, proposed_year, reason, status, reviewed_by,
                       reviewed_at, created_at
                FROM tag_correction_requests
                WHERE id = ?
                """,
                (request_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
        return _row_to_tag_correction(tuple(row))

    async def list_pending_tag_correction_requests(self) -> list[TagCorrectionRequest]:
        """List all PENDING tag correction requests ordered FIFO."""
        async with self._db.connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, track_id, requested_by, proposed_title, proposed_artist,
                       proposed_album, proposed_year, reason, status, reviewed_by,
                       reviewed_at, created_at
                FROM tag_correction_requests
                WHERE status = 'PENDING'
                ORDER BY id ASC
                """
            )
            rows = await cursor.fetchall()
        return [_row_to_tag_correction(tuple(r)) for r in rows]

    async def resolve_tag_correction_request(
        self,
        request_id: int,
        status: TagCorrectionStatus,
        reviewed_by: int,
    ) -> TagCorrectionRequest | None:
        """Mark a tag correction request as APPROVED or REJECTED."""
        now_dt = datetime.now(UTC)
        async with self._db.connect() as conn:
            await conn.execute(
                """
                UPDATE tag_correction_requests
                SET status = ?, reviewed_by = ?, reviewed_at = ?
                WHERE id = ?
                """,
                (status.value, reviewed_by, now_dt.isoformat(), request_id),
            )
            await conn.commit()
        return await self.get_tag_correction_request(request_id)

    async def update_library_track_metadata(
        self,
        track_id: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        year: int | None = None,
        modified_by: int | None = None,
    ) -> LibraryTrackRecord | None:
        """Update stored metadata fields on a library track after an approved tag correction."""
        return await self.update_library_track_full(
            track_id,
            title=title,
            artist=artist,
            album=album,
            year=year,
            modified_by=modified_by,
        )

    async def update_library_track_full(
        self,
        track_id: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        year: int | None = None,
        track_number: int | None = None,
        storage_path: Path | None = None,
        has_synced_lyrics: bool | None = None,
        recording_mbid: str | None = None,
        release_mbid: str | None = None,
        modified_by: int | None = None,
        increment_modification: bool = True,
    ) -> LibraryTrackRecord | None:
        """Update metadata, storage path, lyrics flag, MBIDs, and modification audit fields for a track."""
        existing = await self.get_library_track(track_id)
        if existing is None:
            return None

        new_title = title if title is not None else existing.title
        new_artist = artist if artist is not None else existing.artist
        new_album = album if album is not None else existing.album
        new_year = year if year is not None else existing.year
        new_track_num = track_number if track_number is not None else existing.track_number
        new_path = str(storage_path) if storage_path is not None else str(existing.storage_path)
        new_lyrics_flag = (
            1 if (has_synced_lyrics if has_synced_lyrics is not None else existing.has_synced_lyrics) else 0
        )
        now_iso = (
            datetime.now(UTC).isoformat()
            if increment_modification
            else (existing.updated_at.isoformat() if existing.updated_at else None)
        )
        new_mod_by = modified_by if modified_by is not None else existing.modified_by
        new_mod_count = (
            existing.modification_count + 1 if increment_modification else existing.modification_count
        )

        async with self._db.connect() as conn:
            await conn.execute(
                """
                UPDATE library_tracks
                SET title = ?, artist = ?, album = ?, year = ?,
                    track_number = ?, storage_path = ?, has_synced_lyrics = ?,
                    updated_at = ?, modified_by = ?, modification_count = ?,
                    telegram_file_id = NULL, source_chat_id = NULL, source_message_id = NULL
                WHERE id = ?
                """,
                (
                    new_title,
                    new_artist,
                    new_album,
                    new_year,
                    new_track_num,
                    new_path,
                    new_lyrics_flag,
                    now_iso,
                    new_mod_by,
                    new_mod_count,
                    existing.id,
                ),
            )
            await conn.execute(
                """
                UPDATE catalog_tracks
                SET title = ?, artist = ?, album = ?, album_artist = ?,
                    year = ?, track_number = ?, library_path = ?,
                    recording_mbid = COALESCE(?, recording_mbid),
                    release_mbid = COALESCE(?, release_mbid)
                WHERE fingerprint = ?
                """,
                (
                    new_title,
                    new_artist,
                    new_album,
                    new_artist,
                    new_year,
                    new_track_num,
                    new_path,
                    recording_mbid,
                    release_mbid,
                    existing.fingerprint,
                ),
            )
            if existing.job_id and storage_path is not None:
                await conn.execute(
                    """
                    UPDATE ingestion_jobs
                    SET final_path = ?
                    WHERE job_id = ?
                    """,
                    (new_path, existing.job_id),
                )
            await conn.commit()
        return await self.get_library_track(existing.id)

    async def cache_track_telegram_file(
        self,
        track_id: str,
        *,
        telegram_file_id: str | None = None,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
    ) -> None:
        """Persist Telegram delivery cache identifiers (`file_id`, `chat_id`, `message_id`) for a track."""
        existing = await self.get_library_track(track_id)
        if existing is None:
            return
        async with self._db.connect() as conn:
            await conn.execute(
                """
                UPDATE library_tracks
                SET telegram_file_id = COALESCE(?, telegram_file_id),
                    source_chat_id = COALESCE(?, source_chat_id),
                    source_message_id = COALESCE(?, source_message_id)
                WHERE id = ?
                """,
                (
                    telegram_file_id,
                    source_chat_id,
                    source_message_id,
                    existing.id,
                ),
            )
            await conn.commit()

    async def upsert_scanned_library_tracks_batch(
        self,
        records: Sequence[LibraryTrackRecord],
        batch_size: int = 200,
    ) -> None:
        """Batch insert or update library tracks discovered or reconciled during a filesystem rescan."""
        if not records:
            return
        async with self._db.connect() as conn:
            for i in range(0, len(records), batch_size):
                batch = records[i : i + batch_size]
                for record in batch:
                    now_iso = record.ingested_at.isoformat()
                    job_id = record.job_id or f"rescan-{record.id[:12]}"
                    resolved_uploader = record.uploaded_by
                    if resolved_uploader == 0:
                        job_cur = await conn.execute(
                            """
                            SELECT user_id FROM ingestion_jobs
                            WHERE (job_id = ? OR final_path = ?)
                              AND user_id IS NOT NULL AND user_id > 0
                            LIMIT 1
                            """,
                            (job_id, str(record.storage_path)),
                        )
                        job_row = await job_cur.fetchone()
                        if job_row is not None and job_row[0] is not None:
                            resolved_uploader = int(str(job_row[0]))

                    await conn.execute(
                        """
                        INSERT INTO library_tracks (
                            id, job_id, uploaded_by, storage_path, fingerprint,
                            duration_seconds, artist, album, title, year,
                            track_number, has_synced_lyrics, ingested_at,
                            updated_at, modified_by, modification_count,
                            telegram_file_id, source_chat_id, source_message_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            uploaded_by = CASE
                                WHEN library_tracks.uploaded_by > 0 THEN library_tracks.uploaded_by
                                ELSE excluded.uploaded_by
                            END,
                            storage_path = excluded.storage_path,
                            fingerprint = excluded.fingerprint,
                            duration_seconds = excluded.duration_seconds,
                            artist = excluded.artist,
                            album = excluded.album,
                            title = excluded.title,
                            year = excluded.year,
                            track_number = excluded.track_number,
                            has_synced_lyrics = excluded.has_synced_lyrics
                        """,
                        (
                            record.id,
                            job_id,
                            resolved_uploader,
                            str(record.storage_path),
                            record.fingerprint,
                            record.duration_seconds,
                            record.artist,
                            record.album,
                            record.title,
                            record.year,
                            record.track_number,
                            1 if record.has_synced_lyrics else 0,
                            now_iso,
                            record.updated_at.isoformat() if record.updated_at else None,
                            record.modified_by,
                            record.modification_count,
                            record.telegram_file_id,
                            record.source_chat_id,
                            record.source_message_id,
                        ),
                    )
                    await conn.execute(
                        """
                        INSERT INTO catalog_tracks (
                            job_id, title, artist, album, album_artist,
                            year, track_number, disc_number, duration_seconds,
                            fingerprint, library_path, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(fingerprint) DO UPDATE SET
                            title = excluded.title,
                            artist = excluded.artist,
                            album = excluded.album,
                            album_artist = excluded.album_artist,
                            year = excluded.year,
                            track_number = excluded.track_number,
                            library_path = excluded.library_path
                        """,
                        (
                            job_id,
                            record.title,
                            record.artist,
                            record.album,
                            record.artist,
                            record.year,
                            record.track_number or 1,
                            1,
                            int(round(record.duration_seconds)),
                            record.fingerprint,
                            str(record.storage_path),
                            now_iso,
                        ),
                    )
                await conn.commit()

    async def upsert_scanned_library_track(
        self,
        record: LibraryTrackRecord,
    ) -> LibraryTrackRecord:
        """Insert or update a library track discovered or reconciled during a filesystem rescan."""
        await self.upsert_scanned_library_tracks_batch([record])
        return (await self.get_library_track(record.id)) or record

    async def delete_library_track(self, track_id: str) -> bool:
        """Delete a single library track record by ID when its file was removed from disk."""
        existing = await self.get_library_track(track_id)
        if existing is None:
            return False
        async with self._db.connect() as conn:
            cur = await conn.execute(
                "DELETE FROM library_tracks WHERE id = ?",
                (existing.id,),
            )
            await conn.execute(
                "DELETE FROM catalog_tracks WHERE fingerprint = ?",
                (existing.fingerprint,),
            )
            await conn.commit()
            return bool(cur.rowcount and cur.rowcount > 0)

    async def wipe_library_catalog(self) -> int:
        """Delete all catalog track, job, and tag correction records (OWNER destructive wipe)."""
        async with self._db.connect() as conn:
            cur_lib = await conn.execute("DELETE FROM library_tracks")
            cur_cat = await conn.execute("DELETE FROM catalog_tracks")
            await conn.execute("DELETE FROM tag_correction_requests")
            await conn.execute("DELETE FROM ingestion_jobs")
            await conn.commit()
            return int((cur_lib.rowcount or 0) + (cur_cat.rowcount or 0))


_LIBRARY_TRACK_COLUMNS = (
    "id, job_id, uploaded_by, storage_path, fingerprint, "
    "duration_seconds, artist, album, title, year, "
    "track_number, has_synced_lyrics, ingested_at, "
    "updated_at, modified_by, modification_count, "
    "telegram_file_id, source_chat_id, source_message_id"
)


def _row_to_audit_log(row: tuple[object, ...]) -> AuditLogEntry:
    """Map an `audit_logs` table row into an `AuditLogEntry`."""
    raw_details = str(row[4]) if row[4] is not None else "{}"
    try:
        details = json.loads(raw_details)
    except Exception:
        details = {}
    return AuditLogEntry(
        id=int(str(row[0])),
        actor_id=int(str(row[1])),
        action=str(row[2]),
        target_entity=str(row[3]) if row[3] is not None else None,
        details=details if isinstance(details, dict) else {},
        created_at=_parse_iso_datetime(row[5]) or datetime.now(UTC),
    )


def _row_to_library_track(row: tuple[object, ...]) -> LibraryTrackRecord:
    """Map a `library_tracks` table row into a `LibraryTrackRecord`."""
    return LibraryTrackRecord(
        id=str(row[0]),
        job_id=str(row[1]) if row[1] is not None else None,
        uploaded_by=int(str(row[2])) if row[2] is not None else 0,
        storage_path=Path(str(row[3])),
        fingerprint=str(row[4]),
        duration_seconds=float(str(row[5])),
        artist=str(row[6]),
        album=str(row[7]),
        title=str(row[8]),
        year=int(str(row[9])) if row[9] is not None else None,
        track_number=int(str(row[10])) if row[10] is not None else None,
        has_synced_lyrics=bool(int(str(row[11]))) if row[11] is not None else False,
        ingested_at=_parse_iso_datetime(row[12]) or datetime.now(UTC),
        updated_at=_parse_iso_datetime(row[13]) if len(row) > 13 else None,
        modified_by=int(str(row[14])) if len(row) > 14 and row[14] is not None else None,
        modification_count=int(str(row[15])) if len(row) > 15 and row[15] is not None else 0,
        telegram_file_id=str(row[16]) if len(row) > 16 and row[16] is not None else None,
        source_chat_id=int(str(row[17])) if len(row) > 17 and row[17] is not None else None,
        source_message_id=int(str(row[18])) if len(row) > 18 and row[18] is not None else None,
    )


def _row_to_tag_correction(row: tuple[object, ...]) -> TagCorrectionRequest:
    """Map a `tag_correction_requests` table row into a `TagCorrectionRequest`."""
    return TagCorrectionRequest(
        id=int(str(row[0])),
        track_id=str(row[1]),
        requested_by=int(str(row[2])),
        proposed_title=str(row[3]) if row[3] is not None else None,
        proposed_artist=str(row[4]) if row[4] is not None else None,
        proposed_album=str(row[5]) if row[5] is not None else None,
        proposed_year=int(str(row[6])) if row[6] is not None else None,
        reason=str(row[7]) if row[7] is not None else "",
        status=TagCorrectionStatus(str(row[8])),
        reviewed_by=int(str(row[9])) if row[9] is not None else None,
        reviewed_at=_parse_iso_datetime(row[10]),
        created_at=_parse_iso_datetime(row[11]) or datetime.now(UTC),
    )
