"""Domain-specific exceptions for the core ingestion engine."""

from pathlib import Path


class IngestionError(Exception):
    """Base exception for all errors inside the core music ingestion domain."""


class ArchiveExtractionError(IngestionError):
    """Raised when unpacking an archive (.zip, .7z, etc.) fails."""


class UnsupportedAudioFormatError(IngestionError):
    """Raised when a file is not a valid or supported audio container."""


class FingerprintingError(IngestionError):
    """Raised when `fpcalc` execution or AcoustID lookup fails."""


class DuplicateTrackError(IngestionError):
    """Raised when an audio file matches an existing fingerprint or MBID in the catalog."""

    def __init__(self, existing_path: Path, reason: str) -> None:
        super().__init__(f"Duplicate track detected ({reason}): {existing_path}")
        self.existing_path = existing_path
        self.reason = reason


class PermissionDeniedError(IngestionError):
    """Raised when a user attempts an action without sufficient RBAC permissions."""


class OidcAuthenticationError(IngestionError):
    """Raised when Authentik OIDC nonce verification, token exchange, or userinfo sync fails."""


class QueueConcurrencyError(IngestionError):
    """Raised when an invalid concurrency limit or queue state transition is requested."""


class AudioTranscodingError(IngestionError):
    """Raised when on-the-fly server-side audio transcoding fails."""
