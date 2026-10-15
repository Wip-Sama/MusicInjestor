"""Pure domain models for audio tracks, fingerprints, metadata candidates, and ingestion jobs."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class UserRole(StrEnum):
    """Six-tier Role-Based Access Control (RBAC) hierarchy."""

    GUEST = "GUEST"
    LISTENER = "LISTENER"
    UPLOADER = "UPLOADER"
    MODERATOR = "MODERATOR"
    ADMIN = "ADMIN"
    OWNER = "OWNER"

    @property
    def rank(self) -> int:
        """Numeric hierarchy level used for role comparisons."""
        ranks: dict[UserRole, int] = {
            UserRole.GUEST: 0,
            UserRole.LISTENER: 1,
            UserRole.UPLOADER: 2,
            UserRole.MODERATOR: 3,
            UserRole.ADMIN: 4,
            UserRole.OWNER: 5,
        }
        return ranks[self]

    def has_at_least(self, minimum: UserRole) -> bool:
        """Return True if this role's rank is greater than or equal to `minimum`."""
        return self.rank >= minimum.rank


class Permission(StrEnum):
    """Granular RBAC capabilities defined in Specification Section 3.1."""

    BASIC_COMMANDS = "BASIC_COMMANDS"
    LINK_OIDC = "LINK_OIDC"
    SUBMIT_ACCESS_REQUEST = "SUBMIT_ACCESS_REQUEST"
    VIEW_CATALOG = "VIEW_CATALOG"
    SUBMIT_INGESTION = "SUBMIT_INGESTION"
    VIEW_OWN_JOBS = "VIEW_OWN_JOBS"
    RESOLVE_OWN_DISAMBIGUATION = "RESOLVE_OWN_DISAMBIGUATION"
    REQUEST_TAG_CORRECTION = "REQUEST_TAG_CORRECTION"
    VIEW_GLOBAL_QUEUE = "VIEW_GLOBAL_QUEUE"
    RESOLVE_OTHERS_JOBS = "RESOLVE_OTHERS_JOBS"
    APPROVE_TAG_CORRECTIONS = "APPROVE_TAG_CORRECTIONS"
    MANAGE_USERS = "MANAGE_USERS"
    REVIEW_AUDIT_LOGS = "REVIEW_AUDIT_LOGS"
    CHANGE_CONCURRENCY_LIMIT = "CHANGE_CONCURRENCY_LIMIT"
    SYSTEM_DESTRUCTIVE_WIPE = "SYSTEM_DESTRUCTIVE_WIPE"


class AccessRequestStatus(StrEnum):
    """Lifecycle states of a manual guest access request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class TagCorrectionStatus(StrEnum):
    """Lifecycle states of a library track tag correction request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SourceType(StrEnum):
    """Ingestion payload origin types."""

    ARCHIVE_ZIP = "ARCHIVE_ZIP"
    ARCHIVE_7Z = "ARCHIVE_7Z"
    DIRECT_AUDIO = "DIRECT_AUDIO"
    URL = "URL"


class AuditAction(StrEnum):
    """Standardized audit log action identifiers."""

    ROLE_UPDATED = "ROLE_UPDATED"
    OIDC_SYNC = "OIDC_SYNC"
    ACCESS_REQUESTED = "ACCESS_REQUESTED"
    ACCESS_DENIED = "ACCESS_DENIED"
    USER_BLOCKED = "USER_BLOCKED"
    USER_UNBLOCKED = "USER_UNBLOCKED"
    INGEST_QUEUED = "INGEST_QUEUED"
    INGEST_SUCCESS = "INGEST_SUCCESS"
    DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
    CONCURRENCY_UPDATED = "CONCURRENCY_UPDATED"
    TAG_CORRECTION_REQUESTED = "TAG_CORRECTION_REQUESTED"
    TAG_CORRECTION_APPROVED = "TAG_CORRECTION_APPROVED"
    TAG_CORRECTION_REJECTED = "TAG_CORRECTION_REJECTED"
    OWNERSHIP_TRANSFERRED = "OWNERSHIP_TRANSFERRED"
    SYSTEM_WIPE_EXECUTED = "SYSTEM_WIPE_EXECUTED"
    JOB_PAUSED = "JOB_PAUSED"
    JOB_UNLOCKED = "JOB_UNLOCKED"
    BULK_METADATA_ENRICHED = "BULK_METADATA_ENRICHED"


class JobState(StrEnum):
    """Lifecycle states of an ingestion job across queue and pipeline stages."""

    QUEUED = "QUEUED"
    PAUSED = "PAUSED"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    EXTRACTING = "EXTRACTING"
    FINGERPRINTING = "FINGERPRINTING"
    ENRICHING_METADATA = "ENRICHING_METADATA"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    AWAITING_INPUT = "AWAITING_INPUT"
    FETCHING_LYRICS = "FETCHING_LYRICS"
    ORGANIZING = "ORGANIZING"
    COMPLETED = "COMPLETED"
    DUPLICATE_REJECTED = "DUPLICATE_REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass(slots=True)
class UserAccount:
    """Domain entity representing a Telegram user and optional Authentik OIDC federation state."""

    telegram_id: int
    display_name: str
    username: str | None = None
    role: UserRole = UserRole.GUEST
    is_blocked: bool = False
    authentik_sub: str | None = None
    authentik_username: str | None = None
    authentik_groups: tuple[str, ...] = ()
    access_token_encrypted: str | None = None
    refresh_token_encrypted: str | None = None
    token_expires_at: datetime | None = None
    last_oidc_sync: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_oidc_linked(self) -> bool:
        """Return True if this account is paired with an Authentik OIDC subject."""
        return bool(self.authentik_sub and self.authentik_sub.strip())

    @property
    def mention_handle(self) -> str:
        """Return `@username` if available, falling back to `display_name`."""
        if self.username and self.username.strip():
            clean = self.username.strip().lstrip("@")
            return f"@{clean}"
        return self.display_name


@dataclass(frozen=True, slots=True)
class AccessRequestRecord:
    """Manual access request submitted by a GUEST user."""

    id: int
    telegram_id: int
    status: AccessRequestStatus = AccessRequestStatus.PENDING
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class LibraryTrackRecord:
    """Persisted record of an ingested track inside `library_tracks`."""

    id: str
    job_id: str | None
    uploaded_by: int
    storage_path: Path
    fingerprint: str
    duration_seconds: float
    artist: str
    album: str
    title: str
    year: int | None = None
    track_number: int | None = None
    has_synced_lyrics: bool = False
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
    modified_by: int | None = None
    modification_count: int = 0
    telegram_file_id: str | None = None
    source_chat_id: int | None = None
    source_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class TagCorrectionRequest:
    """Request submitted by an uploader/moderator to correct metadata tags on an ingested track."""

    track_id: str
    requested_by: int
    proposed_title: str | None = None
    proposed_artist: str | None = None
    proposed_album: str | None = None
    proposed_year: int | None = None
    reason: str = ""
    status: TagCorrectionStatus = TagCorrectionStatus.PENDING
    id: int | None = None
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class LibraryRescanReport:
    """Summary of filesystem-to-database reconciliation for `storage/library`."""

    added_tracks: tuple[LibraryTrackRecord, ...] = ()
    updated_tracks: tuple[LibraryTrackRecord, ...] = ()
    removed_tracks: tuple[LibraryTrackRecord, ...] = ()
    unchanged_count: int = 0

    @property
    def has_changes(self) -> bool:
        """Return True if any tracks were added, updated, or removed."""
        return bool(self.added_tracks or self.updated_tracks or self.removed_tracks)


@dataclass(frozen=True, slots=True)
class SongEditProposal:
    """Dry-run preview payload for a proposed song metadata, cover, or lyrics change."""

    track_id: str
    action_type: str
    new_title: str | None = None
    new_artist: str | None = None
    new_album: str | None = None
    new_year: int | None = None
    new_track_number: int | None = None
    new_lyrics: str | None = None
    new_cover_source_path: str | None = None
    new_recording_mbid: str | None = None
    new_release_mbid: str | None = None


@dataclass(frozen=True, slots=True)
class AuditLogEntry:
    """Immutable audit log record persisted in SQLite and optionally routed to Admin Log Channel."""

    actor_id: int
    action: str
    target_entity: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class AudioFingerprint:
    """Acoustic fingerprint computed via Chromaprint (`fpcalc`)."""

    duration_seconds: int
    fingerprint: str
    acoustid_mbid: str | None = None


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    """Normalized canonical track metadata used for tagging and path organization."""

    title: str
    artist: str
    album: str
    album_artist: str | None = None
    year: int | None = None
    track_number: int | None = None
    disc_number: int | None = None
    genre: str | None = None
    duration_seconds: int | None = None
    recording_mbid: str | None = None
    release_mbid: str | None = None
    isrc: str | None = None

    @property
    def effective_album_artist(self) -> str:
        """Return album_artist if present, falling back to track artist."""
        if self.album_artist and self.album_artist.strip():
            return self.album_artist.strip()
        return self.artist.strip()


@dataclass(frozen=True, slots=True)
class MetadataCandidate:
    """Candidate metadata match returned from MusicBrainz with confidence score."""

    metadata: TrackMetadata
    confidence: float
    release_type: str = "Album"
    disambiguation: str = ""

    @property
    def display_label(self) -> str:
        """Human-readable label suitable for UI selection buttons or logs."""
        year_part = f" ({self.metadata.year})" if self.metadata.year else ""
        extra = f" [{self.disambiguation}]" if self.disambiguation else ""
        pct = round(self.confidence * 100)
        return (
            f"[{pct}%] {self.metadata.artist} - {self.metadata.title} | "
            f"{self.metadata.album}{year_part}{extra}"
        )


@dataclass(frozen=True, slots=True)
class LyricsData:
    """Synchronized and plain-text lyrics fetched from LRCLIB."""

    synced_lyrics: str | None = None
    plain_lyrics: str | None = None
    instrumental: bool = False

    @property
    def has_synced(self) -> bool:
        """Check whether timestamped LRC lyrics are available."""
        return bool(self.synced_lyrics and self.synced_lyrics.strip())

    @property
    def has_any(self) -> bool:
        """Check whether either synced or plain lyrics are available."""
        return self.has_synced or bool(self.plain_lyrics and self.plain_lyrics.strip())


@dataclass(frozen=True, slots=True)
class DuplicateCheckResult:
    """Outcome of checking a track against the existing library catalog."""

    is_duplicate: bool
    existing_path: Path | None = None
    match_reason: str | None = None


@dataclass(slots=True)
class IngestionJob:
    """Mutable aggregate tracking an ingestion job through the queue and pipeline."""

    job_id: str
    source_path: Path
    state: JobState = JobState.PENDING
    candidates: list[MetadataCandidate] = field(default_factory=list)
    selected_metadata: TrackMetadata | None = None
    fingerprint: AudioFingerprint | None = None
    final_path: Path | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    user_id: int | None = None
    source_type: SourceType = SourceType.DIRECT_AUDIO
    original_filename: str = ""
    file_size_bytes: int = 0
    scratch_dir: Path | None = None
    sha256_hash: str | None = None
    confidence_score: float | None = None
    matched_release_mbid: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    total_tracks: int = 1
    processed_tracks: int = 0
    lyrics_matched_count: int = 0


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Final or intermediate outcome returned by the IngestionPipeline."""

    job: IngestionJob
    requires_user_confirmation: bool = False
    duplicate: DuplicateCheckResult | None = None
    lyrics_attached: bool = False
    tracks_ingested: int = 1
    lyrics_embedded_count: int = 0
