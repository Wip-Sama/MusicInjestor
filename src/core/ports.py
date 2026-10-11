"""Hexagonal Architecture Ports (Protocols) decoupling `src/core` from infrastructure and UI."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from src.core.models import (
    AccessRequestRecord,
    AccessRequestStatus,
    AudioFingerprint,
    AuditLogEntry,
    DuplicateCheckResult,
    IngestionJob,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
    UserAccount,
    UserRole,
)


@runtime_checkable
class CatalogRepositoryPort(Protocol):
    """Persistence port for deduplication lookups and ingestion job state tracking."""

    async def check_duplicate(
        self,
        fingerprint: str,
        recording_mbid: str | None = None,
    ) -> DuplicateCheckResult:
        """Check whether a fingerprint or MusicBrainz recording ID already exists in the library."""
        ...

    async def register_ingested_track(
        self,
        job_id: str,
        metadata: TrackMetadata,
        fingerprint: AudioFingerprint,
        library_path: Path,
    ) -> None:
        """Persist a newly ingested track and its fingerprint into the library catalog."""
        ...

    async def save_job(self, job: IngestionJob) -> None:
        """Insert or update an ingestion job state in persistent storage."""
        ...

    async def get_job(self, job_id: str) -> IngestionJob | None:
        """Retrieve an ingestion job by ID."""
        ...


@runtime_checkable
class GovernanceRepositoryPort(Protocol):
    """Persistence port for RBAC users, Authentik OIDC state, access requests, queue, and audit logs."""

    async def get_user(self, telegram_id: int) -> UserAccount | None:
        """Retrieve a user account by Telegram numeric ID."""
        ...

    async def get_user_by_authentik_sub(self, authentik_sub: str) -> UserAccount | None:
        """Retrieve a user account by Authentik OIDC subject UUID."""
        ...

    async def upsert_user(self, user: UserAccount) -> UserAccount:
        """Insert or update a user account record."""
        ...

    async def list_users(self, role: UserRole | None = None) -> list[UserAccount]:
        """List all registered users, optionally filtered by role."""
        ...

    async def list_oidc_linked_users(self) -> list[UserAccount]:
        """List all non-blocked users linked to an Authentik OIDC subject."""
        ...

    async def count_user_uploads(self, telegram_id: int) -> tuple[int, int]:
        """Return `(albums_count, tracks_count)` uploaded by the given user."""
        ...

    async def create_access_request(self, telegram_id: int) -> AccessRequestRecord:
        """Create a new PENDING access request for a user."""
        ...

    async def get_pending_access_request(self, telegram_id: int) -> AccessRequestRecord | None:
        """Return an existing PENDING access request for `telegram_id`, if any."""
        ...

    async def get_access_request_by_id(self, request_id: int) -> AccessRequestRecord | None:
        """Retrieve an access request record by its primary key ID."""
        ...

    async def list_pending_access_requests(self) -> list[AccessRequestRecord]:
        """Return all currently PENDING access requests ordered by creation time."""
        ...

    async def resolve_access_request(
        self,
        request_id: int,
        status: AccessRequestStatus,
        reviewed_by: int,
    ) -> AccessRequestRecord | None:
        """Mark an access request as APPROVED or REJECTED by `reviewed_by`."""
        ...

    async def append_audit_log(self, entry: AuditLogEntry) -> AuditLogEntry:
        """Persist an immutable audit log record and return the stored entry with its ID."""
        ...

    async def list_audit_logs(self, limit: int = 20) -> list[AuditLogEntry]:
        """Retrieve the most recent audit log records ordered newest-first."""
        ...

    async def list_queued_jobs(self, user_id: int | None = None) -> list[IngestionJob]:
        """Return jobs in QUEUED state ordered FIFO by `created_at`."""
        ...

    async def list_active_jobs(self) -> list[IngestionJob]:
        """Return jobs currently actively processing or awaiting disambiguation."""
        ...

    async def cancel_queued_jobs_for_user(self, user_id: int) -> int:
        """Cancel all QUEUED jobs belonging to `user_id` and return the count cancelled."""
        ...


@runtime_checkable
class AdminLogNotifierPort(Protocol):
    """Port for dispatching formatted operational and security events to the Admin Log Channel."""

    async def send_access_request_card(
        self,
        user: UserAccount,
        request: AccessRequestRecord,
    ) -> None:
        """Post an interactive access request card to the Admin Log Channel."""
        ...

    async def send_role_updated_audit(
        self,
        target_user: UserAccount,
        actor: UserAccount,
        previous_role: UserRole,
        new_role: UserRole,
        reason: str,
    ) -> None:
        """Post Event A (`USER AUDIT: ROLE_UPDATED`) to the Admin Log Channel."""
        ...

    async def send_oidc_sync_audit(
        self,
        target_user: UserAccount,
        group_changes: str,
        new_role: UserRole,
        cancelled_jobs_count: int,
    ) -> None:
        """Post Event B (`OIDC AUDIT: ROLE_SYNCHRONIZED`) to the Admin Log Channel."""
        ...

    async def send_ingest_success_audit(
        self,
        job: IngestionJob,
        uploader: UserAccount,
        metadata: TrackMetadata,
        tracks_written: int,
        audio_format: str,
        lyrics_embedded: int,
        duration_seconds: float,
    ) -> None:
        """Post Event C (`INGEST AUDIT: SUCCESS`) to the Admin Log Channel."""
        ...

    async def send_duplicate_rejected_audit(
        self,
        job: IngestionJob,
        uploader: UserAccount,
        detected_track: str,
        conflict_path: Path,
        match_source: str,
    ) -> None:
        """Post Event D (`INGEST AUDIT: DUPLICATE_REJECTED`) to the Admin Log Channel."""
        ...


@runtime_checkable
class ArchiveExtractorPort(Protocol):
    """Port for unpacking archives (.zip, .7z) or staging single audio files."""

    async def extract_or_stage(self, source_path: Path, job_id: str) -> list[Path]:
        """Extract an archive or stage an audio file into a job-specific scratch directory."""
        ...

    async def cleanup_job_scratch(self, job_id: str) -> None:
        """Remove ephemeral scratch files for a finished or failed job."""
        ...


@runtime_checkable
class FingerprinterPort(Protocol):
    """Port for computing acoustic fingerprints and optional AcoustID lookups."""

    async def compute_fingerprint(self, audio_path: Path) -> AudioFingerprint:
        """Generate a Chromaprint fingerprint and duration for the given audio file."""
        ...


@runtime_checkable
class MetadataProviderPort(Protocol):
    """Port for inspecting local tags and querying MusicBrainz for candidate matches."""

    async def read_local_metadata(self, audio_path: Path) -> TrackMetadata:
        """Extract existing embedded tags from the local audio file."""
        ...

    async def find_candidates(
        self,
        local_metadata: TrackMetadata,
        fingerprint: AudioFingerprint | None = None,
    ) -> list[MetadataCandidate]:
        """Query canonical metadata sources and return ranked candidates with confidence scores."""
        ...


@runtime_checkable
class LyricsProviderPort(Protocol):
    """Port for retrieving synced or plain lyrics from external providers (e.g., LRCLIB)."""

    async def fetch_lyrics(self, metadata: TrackMetadata) -> LyricsData | None:
        """Fetch synchronized or plain lyrics matching the track metadata."""
        ...


@runtime_checkable
class StorageOrganizerPort(Protocol):
    """Port for writing final audio tags, sidecar lyrics, and moving files into the library."""

    def build_target_audio_path(self, metadata: TrackMetadata, extension: str) -> Path:
        """Compute the sanitized destination path inside the music library."""
        ...

    async def finalize_track(
        self,
        source_audio_path: Path,
        metadata: TrackMetadata,
        lyrics: LyricsData | None = None,
    ) -> Path:
        """Write metadata tags, optionally write `.lrc` sidecar, and move track to target storage."""
        ...
