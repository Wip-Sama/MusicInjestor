"""Pure domain models for audio tracks, fingerprints, metadata candidates, and ingestion jobs."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class JobState(StrEnum):
    """Lifecycle states of an ingestion job across pipeline stages."""

    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    FINGERPRINTING = "FINGERPRINTING"
    ENRICHING_METADATA = "ENRICHING_METADATA"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    FETCHING_LYRICS = "FETCHING_LYRICS"
    ORGANIZING = "ORGANIZING"
    COMPLETED = "COMPLETED"
    DUPLICATE_REJECTED = "DUPLICATE_REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


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
            f"{self.metadata.artist} - {self.metadata.title} | "
            f"{self.metadata.album}{year_part}{extra} ({pct}%)"
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
    """Mutable aggregate tracking an ingestion job through the pipeline."""

    job_id: str
    source_path: Path
    state: JobState = JobState.PENDING
    candidates: list[MetadataCandidate] = field(default_factory=list)
    selected_metadata: TrackMetadata | None = None
    fingerprint: AudioFingerprint | None = None
    final_path: Path | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Final or intermediate outcome returned by the IngestionPipeline."""

    job: IngestionJob
    requires_user_confirmation: bool = False
    duplicate: DuplicateCheckResult | None = None
    lyrics_attached: bool = False
