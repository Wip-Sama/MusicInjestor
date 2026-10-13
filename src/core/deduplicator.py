"""Pure domain logic for duplicate detection before metadata enrichment and filing."""

import logging

from src.core.models import AudioFingerprint, DuplicateCheckResult, TrackMetadata
from src.core.ports import CatalogRepositoryPort

logger = logging.getLogger(__name__)


class DeduplicatorService:
    """Coordinates fingerprint, MusicBrainz ID, and metadata duplicate checks against the catalog port."""

    def __init__(self, catalog_repository: CatalogRepositoryPort) -> None:
        self._catalog = catalog_repository

    async def check_track(
        self,
        fingerprint: AudioFingerprint | None = None,
        recording_mbid: str | None = None,
        metadata: TrackMetadata | None = None,
    ) -> DuplicateCheckResult:
        """Evaluate whether a track already exists by fingerprint, MBID, or metadata similarity."""
        fp_str = fingerprint.fingerprint if fingerprint is not None else ""
        effective_mbid = recording_mbid or (fingerprint.acoustid_mbid if fingerprint is not None else None)
        result = await self._catalog.check_duplicate(
            fingerprint=fp_str,
            recording_mbid=effective_mbid,
            metadata=metadata,
        )
        if result.is_duplicate:
            logger.info(
                "Duplicate track detected via %s at existing path %s",
                result.match_reason,
                result.existing_path,
            )
        return result
