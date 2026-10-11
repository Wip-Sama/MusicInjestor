"""LRCLIB REST API client for retrieving synchronized and plain lyrics."""

import logging

import httpx

from src.core.models import LyricsData, TrackMetadata

logger = logging.getLogger(__name__)


class LrclibLyricsClient:
    """Fetches timestamped `.lrc` or plain lyrics from the LRCLIB API."""

    def __init__(
        self,
        base_url: str = "https://lrclib.net/api",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def fetch_lyrics(self, metadata: TrackMetadata) -> LyricsData | None:
        """Query `/get` on LRCLIB using track title, artist, album, and duration."""
        params: dict[str, str | int] = {
            "track_name": metadata.title,
            "artist_name": metadata.artist,
            "album_name": metadata.album,
        }
        if metadata.duration_seconds is not None:
            params["duration"] = metadata.duration_seconds

        url = f"{self._base_url}/get"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.get(url, params=params)
                if response.status_code == 404:
                    logger.debug(
                        "No lyrics found on LRCLIB for %s - %s",
                        metadata.artist,
                        metadata.title,
                    )
                    return None
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            logger.warning("LRCLIB request failed for %s: %s", metadata.title, exc)
            return None

        synced = payload.get("syncedLyrics")
        plain = payload.get("plainLyrics")
        instrumental = bool(payload.get("instrumental", False))
        return LyricsData(
            synced_lyrics=str(synced) if synced else None,
            plain_lyrics=str(plain) if plain else None,
            instrumental=instrumental,
        )
