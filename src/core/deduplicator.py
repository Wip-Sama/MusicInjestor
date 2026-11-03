"""Pure domain logic for duplicate detection before metadata enrichment and filing."""

import logging
from pathlib import Path

from src.core.models import AudioFingerprint, DuplicateCheckResult, TrackMetadata
from src.core.ports import CatalogRepositoryPort
from src.core.quality import inspect_audio_quality, is_audio_quality_upgrade

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
        audio_path: Path | None = None,
    ) -> DuplicateCheckResult:
        """Evaluate whether a track already exists by fingerprint, MBID, or metadata,
        and evaluate if incoming audio is an upgrade.
        """
        fp_str = fingerprint.fingerprint if fingerprint is not None else ""
        effective_mbid = recording_mbid or (fingerprint.acoustid_mbid if fingerprint is not None else None)
        result = await self._catalog.check_duplicate(
            fingerprint=fp_str,
            recording_mbid=effective_mbid,
            metadata=metadata,
        )
        if result.is_duplicate:
            is_upgrade = False
            new_quality = None
            existing_quality = None
            if audio_path is not None and result.existing_path is not None:
                new_quality = inspect_audio_quality(audio_path)
                existing_quality = inspect_audio_quality(result.existing_path)
                is_upgrade = is_audio_quality_upgrade(new_quality, existing_quality)

            logger.info(
                "Duplicate track detected via %s at existing path %s (upgrade_available=%s)",
                result.match_reason,
                result.existing_path,
                is_upgrade,
            )
            return DuplicateCheckResult(
                is_duplicate=True,
                existing_path=result.existing_path,
                match_reason=result.match_reason,
                is_upgrade=is_upgrade,
                existing_quality=existing_quality,
                new_quality=new_quality,
            )
        return result
