"""Hexagonal Architecture Ports (Protocols) decoupling `src/core` from infrastructure and UI."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from src.core.models import (
    AudioFingerprint,
    DuplicateCheckResult,
    IngestionJob,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
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
