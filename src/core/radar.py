"""Core domain service orchestrating artist monitoring, release discovery, and notifications."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from src.core.models import DiscoveredRelease, FollowedArtist, ReleaseNotification, ReleaseTrackPreview
from src.core.ports import GovernanceRepositoryPort, ReleaseRadarProviderPort

logger = logging.getLogger(__name__)


def is_release_recent(release_date_str: str, max_days: int = 60) -> bool:
    """Check whether an ISO release date string (YYYY-MM-DD) falls within `max_days` or is in the future."""
    cleaned = release_date_str.strip()
    if not cleaned or len(cleaned) < 4:
        return True

    # If only year is known (e.g. "2026")
    if len(cleaned) == 4 and cleaned.isdigit():
        return int(cleaned) >= (datetime.now(UTC).year - 1)

    try:
        dt = datetime.fromisoformat(cleaned[:10]).replace(tzinfo=UTC)
        threshold = datetime.now(UTC) - timedelta(days=max_days)
        return dt >= threshold
    except Exception:
        return True


class ReleaseRadarService:
    """Monitors library and followed artists, discovers newly published releases, and manages user feeds."""

    def __init__(
        self,
        provider: ReleaseRadarProviderPort,
        repository: GovernanceRepositoryPort,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._http_client = http_client

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Return the injected HTTP client or construct an ephemeral one."""
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient(timeout=10.0)

    async def follow_artist(
        self,
        telegram_id: int,
        artist_name: str,
    ) -> FollowedArtist:
        """Follow an artist for `telegram_id`, resolving provider ID and discovering immediate releases."""
        cleaned_name = artist_name.strip()
        provider_id = await self._provider.search_artist(cleaned_name)
        followed = await self._repository.follow_artist(
            telegram_id=telegram_id,
            artist_name=cleaned_name,
            provider_artist_id=provider_id,
        )

        if provider_id:
            try:
                releases = await self._provider.get_artist_recent_releases(
                    provider_artist_id=provider_id,
                    artist_name=cleaned_name,
                    limit=10,
                )
                if releases:
                    await self._repository.upsert_discovered_releases(releases)
            except Exception as exc:
                logger.warning(
                    "Could not fetch immediate releases for newly followed artist %s: %s", cleaned_name, exc
                )

        return followed

    async def unfollow_artist(self, telegram_id: int, artist_name: str) -> bool:
        """Unfollow an artist for `telegram_id`."""
        return await self._repository.unfollow_artist(telegram_id, artist_name)

    async def list_user_follows(self, telegram_id: int) -> list[FollowedArtist]:
        """Return all artists followed by the given user."""
        return await self._repository.list_user_followed_artists(telegram_id)

    async def is_artist_followed(self, telegram_id: int, artist_name: str) -> bool:
        """Check whether `telegram_id` follows `artist_name`."""
        return await self._repository.is_artist_followed(telegram_id, artist_name)

    async def scan_monitored_artists(
        self,
        max_days_back: int = 60,
        concurrency_limit: int = 3,
    ) -> tuple[list[DiscoveredRelease], list[ReleaseNotification]]:
        """Scan all monitored artists (library artists + followed artists) and generate notifications."""
        monitored_artists = await self._repository.list_all_monitored_artists()
        if not monitored_artists:
            logger.info("No monitored artists found in library or follows to scan.")
            return [], []

        logger.info("Starting Release Radar scan across %d monitored artists...", len(monitored_artists))

        discovered_releases: list[DiscoveredRelease] = []
        new_notifications: list[ReleaseNotification] = []
        sem = asyncio.Semaphore(max(1, concurrency_limit))

        async def _scan_single_artist(name: str) -> list[DiscoveredRelease]:
            async with sem:
                try:
                    provider_id = await self._provider.search_artist(name)
                    if not provider_id:
                        return []
                    releases = await self._provider.get_artist_recent_releases(
                        provider_artist_id=provider_id,
                        artist_name=name,
                        limit=10,
                    )
                    recent = [
                        r for r in releases if is_release_recent(r.release_date, max_days=max_days_back)
                    ]
                    if recent:
                        return await self._repository.upsert_discovered_releases(recent)
                    return []
                except Exception as exc:
                    logger.warning("Error scanning releases for artist '%s': %s", name, exc)
                    return []

        tasks = [_scan_single_artist(art) for art in monitored_artists]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in batch_results:
            if isinstance(res, list):
                discovered_releases.extend(res)

        logger.info(
            "Release Radar scan complete: processed %d artists, discovered %d recent releases.",
            len(monitored_artists),
            len(discovered_releases),
        )

        return discovered_releases, new_notifications

    async def get_release_track_previews(
        self,
        provider_release_id: str,
    ) -> list[ReleaseTrackPreview]:
        """Fetch all track previews for a release from the provider."""
        return await self._provider.get_release_track_previews(provider_release_id)

    async def get_preview_audio_bytes(self, preview_url: str) -> bytes | None:
        """Download 30-second audio stream bytes for Telegram chat playback."""
        clean_url = preview_url.strip()
        if not clean_url or not clean_url.startswith("http"):
            return None

        try:
            client = await self._get_http_client()
            resp = await client.get(clean_url, timeout=12.0)
            if resp.status_code == 200 and len(resp.content) > 1024:
                return resp.content
            return None
        except Exception as exc:
            logger.warning("Failed to download audio preview from %s: %s", clean_url, exc)
            return None
