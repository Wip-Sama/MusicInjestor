"""Pure domain logic for duplicate detection before metadata enrichment and filing."""

import logging

from src.core.models import AudioFingerprint, DuplicateCheckResult
from src.core.ports import CatalogRepositoryPort

logger = logging.getLogger(__name__)


class DeduplicatorService:
    """Coordinates fingerprint and MusicBrainz ID duplicate checks against the catalog port."""

    def __init__(self, catalog_repository: CatalogRepositoryPort) -> None:
        self._catalog = catalog_repository

    async def check_track(
        self,
        fingerprint: AudioFingerprint,
        recording_mbid: str | None = None,
    ) -> DuplicateCheckResult:
        """Evaluate whether a track already exists in the catalog by fingerprint or MBID."""
        effective_mbid = recording_mbid or fingerprint.acoustid_mbid
        result = await self._catalog.check_duplicate(
            fingerprint=fingerprint.fingerprint,
            recording_mbid=effective_mbid,
        )
        if result.is_duplicate:
            logger.info(
                "Duplicate track detected via %s at existing path %s",
                result.match_reason,
                result.existing_path,
            )
        return result
