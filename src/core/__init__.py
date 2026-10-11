"""Pure domain and ingestion engine package.

Architectural Invariant:
This package MUST NOT import `aiogram`, `src.bot`, `src.db`, or `src.config`.
All external persistence and UI interactions occur through protocols defined in `src.core.ports`.
"""

from src.core.exceptions import (
    ArchiveExtractionError,
    DuplicateTrackError,
    FingerprintingError,
    IngestionError,
    UnsupportedAudioFormatError,
)
from src.core.models import (
    AudioFingerprint,
    DuplicateCheckResult,
    IngestionJob,
    IngestionResult,
    JobState,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
)
from src.core.pipeline import IngestionPipeline

__all__ = [
    "ArchiveExtractionError",
    "AudioFingerprint",
    "DuplicateCheckResult",
    "DuplicateTrackError",
    "FingerprintingError",
    "IngestionError",
    "IngestionJob",
    "IngestionPipeline",
    "IngestionResult",
    "JobState",
    "LyricsData",
    "MetadataCandidate",
    "TrackMetadata",
    "UnsupportedAudioFormatError",
]
