"""Deezer API provider implementing `ReleaseRadarProviderPort` for new music releases and previews."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

import httpx

from src.core.models import DiscoveredRelease
from src.core.ports import ReleaseRadarProviderPort

logger = logging.getLogger(__name__)

DEEZER_API_BASE: str = "https://api.deezer.com"
DEFAULT_TIMEOUT_SECONDS: float = 10.0


class DeezerReleaseProvider(ReleaseRadarProviderPort):
    """Fetches artist releases, album tracklists, and 30-second audio previews from Deezer."""

    def __init__(
        self,
        base_url: str = DEEZER_API_BASE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the injected HTTP client or construct a reusable instance."""
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=self._timeout_seconds)

    async def search_artist(self, artist_name: str) -> str | None:
        """Search Deezer for the given artist name and return the canonical Deezer artist ID."""
        cleaned_name = artist_name.strip()
        if not cleaned_name:
            return None

        url = f"{self._base_url}/search/artist?q={quote_plus(cleaned_name)}"
        try:
            client = await self._get_client()
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(
                    "Deezer artist search returned HTTP %d for '%s'", resp.status_code, cleaned_name
                )
                return None

            payload: dict[str, Any] = resp.json()
            data: list[dict[str, Any]] = payload.get("data", [])
            if not data:
                return None

            # Prefer exact case-insensitive match if present
            target_lower = cleaned_name.lower()
            for candidate in data:
                candidate_name = str(candidate.get("name", "")).strip().lower()
                if candidate_name == target_lower:
                    return str(candidate.get("id"))

            # Fall back to top result
            first_id = data[0].get("id")
            return str(first_id) if first_id is not None else None
        except Exception as exc:
            logger.warning("Failed to search Deezer for artist '%s': %s", cleaned_name, exc)
            return None

    async def get_release_preview_track(
        self,
        provider_release_id: str,
    ) -> tuple[str, str] | None:
        """Retrieve `(track_title, preview_mp3_url)` for the specified Deezer album/single ID."""
        cleaned_id = provider_release_id.strip()
        if not cleaned_id:
            return None

        url = f"{self._base_url}/album/{quote_plus(cleaned_id)}/tracks"
        try:
            client = await self._get_client()
            resp = await client.get(url)
            if resp.status_code != 200:
                return None

            payload: dict[str, Any] = resp.json()
            tracks: list[dict[str, Any]] = payload.get("data", [])
            for track in tracks:
                preview = str(track.get("preview", "")).strip()
                title = str(track.get("title", "")).strip()
                if preview and preview.startswith("http"):
                    return title or "Preview Track", preview

            return None
        except Exception as exc:
            logger.warning("Failed to fetch Deezer preview for album %s: %s", cleaned_id, exc)
            return None

    async def get_artist_recent_releases(
        self,
        provider_artist_id: str,
        artist_name: str,
        limit: int = 25,
    ) -> list[DiscoveredRelease]:
        """Fetch chronologically recent albums, EPs, and singles for the given artist ID."""
        cleaned_id = provider_artist_id.strip()
        if not cleaned_id:
            return []

        url = f"{self._base_url}/artist/{quote_plus(cleaned_id)}/albums?limit={limit}&order=RELEASE_DATE_DESC"
        try:
            client = await self._get_client()
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(
                    "Deezer artist releases returned HTTP %d for ID %s", resp.status_code, cleaned_id
                )
                return []

            payload: dict[str, Any] = resp.json()
            albums_data: list[dict[str, Any]] = payload.get("data", [])
            if not albums_data:
                return []

            results: list[DiscoveredRelease] = []
            sem = asyncio.Semaphore(4)

            async def _populate_preview(album_id: str) -> tuple[str, str] | None:
                async with sem:
                    return await self.get_release_preview_track(album_id)

            # Gather preview tracks for the top recent releases (limit to first 10 for performance)
            preview_tasks: list[asyncio.Task[tuple[str, str] | None]] = []
            for item in albums_data[:10]:
                alb_id = str(item.get("id", "")).strip()
                if alb_id:
                    preview_tasks.append(asyncio.create_task(_populate_preview(alb_id)))
                else:
                    preview_tasks.append(asyncio.create_task(asyncio.sleep(0, result=None)))

            preview_results = await asyncio.gather(*preview_tasks, return_exceptions=True)

            for idx, item in enumerate(albums_data):
                alb_id = str(item.get("id", "")).strip()
                if not alb_id:
                    continue

                title = str(item.get("title", "")).strip()
                record_type = str(item.get("record_type", "album")).lower()
                release_date = str(item.get("release_date", "")).strip()
                cover_url = (
                    str(
                        item.get("cover_xl") or item.get("cover_big") or item.get("cover_medium") or ""
                    ).strip()
                    or None
                )
                external_url = str(item.get("link", "")).strip() or f"https://www.deezer.com/album/{alb_id}"
                track_count = int(item.get("nb_tracks") or 1)

                preview_url: str | None = None
                if idx < len(preview_results):
                    res = preview_results[idx]
                    if isinstance(res, tuple) and bool(res[1]):
                        preview_url = res[1]

                results.append(
                    DiscoveredRelease(
                        id=f"deezer:{alb_id}",
                        artist_name=artist_name,
                        title=title,
                        release_type=record_type,
                        release_date=release_date,
                        cover_url=cover_url,
                        preview_url=preview_url,
                        external_url=external_url,
                        provider="deezer",
                        provider_release_id=alb_id,
                        track_count=track_count,
                        in_library=False,
                        created_at=datetime.now(UTC),
                    )
                )

            return results
        except Exception as exc:
            logger.warning(
                "Failed to fetch Deezer releases for artist %s (%s): %s", artist_name, cleaned_id, exc
            )
            return []
