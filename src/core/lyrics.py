"""LRCLIB REST API client for retrieving synchronized and plain lyrics with `/search` fallback."""

import logging

import httpx

from src.core.metadata import (
    _best_artist_similarity,
    _best_title_similarity,
    extract_artist_search_variants,
    extract_title_search_variants,
)
from src.core.models import LyricsData, TrackMetadata

logger = logging.getLogger(__name__)


def _is_instrumental_title(title: str) -> bool:
    """Return True if `title` explicitly indicates an instrumental or off-vocal track."""
    lowered = title.lower()
    return any(
        kw in lowered for kw in ("instrumental", "off vocal", "off-vocal", "inst.", "karaoke", "カラオケ")
    )


def _score_search_hit(hit: dict[str, object], metadata: TrackMetadata) -> float:
    """Score an LRCLIB `/api/search` hit against `metadata`, preferring synced lyrics and duration match."""
    hit_title = str(hit.get("trackName") or hit.get("name") or "").strip()
    hit_artist = str(hit.get("artistName") or "").strip()
    if not hit_title:
        return 0.0

    want_instrumental = _is_instrumental_title(metadata.title)
    hit_is_inst = bool(hit.get("instrumental", False)) or _is_instrumental_title(hit_title)
    if hit_is_inst and not want_instrumental:
        return 0.0

    synced = str(hit.get("syncedLyrics") or "").strip()
    plain = str(hit.get("plainLyrics") or "").strip()
    if not synced and not plain:
        return 0.0

    title_sim = _best_title_similarity(metadata.title, hit_title)
    artist_sim = _best_artist_similarity(metadata.artist, hit_artist) if hit_artist else 0.0

    duration_bonus = 0.0
    duration_diff: float | None = None
    raw_dur = hit.get("duration")
    if metadata.duration_seconds and isinstance(raw_dur, (int, float)) and raw_dur > 0:
        duration_diff = abs(float(raw_dur) - float(metadata.duration_seconds))
        if duration_diff <= 3.0:
            duration_bonus = 0.30
        elif duration_diff <= 7.0:
            duration_bonus = 0.15
        elif duration_diff > 25.0:
            duration_bonus = -0.25

    # Cross-script match (e.g. English `Orange Mint` vs Japanese `オレンジミント` on LRCLIB)
    # when the primary artist matches and track duration matches within 2.5 seconds
    if (
        title_sim < 0.40
        and artist_sim >= 0.85
        and metadata.title.isascii() != hit_title.isascii()
        and duration_diff is not None
        and duration_diff <= 2.5
    ):
        title_sim = 0.85

    if title_sim < 0.40:
        return 0.0

    synced_bonus = 0.12 if synced else 0.0
    score = (0.50 * title_sim) + (0.35 * artist_sim) + duration_bonus + synced_bonus
    return round(score, 4)


def _payload_to_lyrics_data(payload: dict[str, object]) -> LyricsData | None:
    """Convert an LRCLIB track dictionary into a `LyricsData` domain object if non-empty."""
    synced = payload.get("syncedLyrics")
    plain = payload.get("plainLyrics")
    instrumental = bool(payload.get("instrumental", False))
    lyrics_obj = LyricsData(
        synced_lyrics=str(synced).strip() if synced and str(synced).strip() else None,
        plain_lyrics=str(plain).strip() if plain and str(plain).strip() else None,
        instrumental=instrumental,
    )
    return lyrics_obj if lyrics_obj.has_any else None


class LrclibLyricsClient:
    """Fetches timestamped `.lrc` or plain lyrics from the LRCLIB API (`/get` + `/search` fallback)."""

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
        """Query `/get` on LRCLIB using artist/title variants, falling back to `/search` on 404."""
        artist_variants = extract_artist_search_variants(metadata.artist) or [metadata.artist]
        title_variants = extract_title_search_variants(metadata.title) or [metadata.title]
        get_url = f"{self._base_url}/get"
        search_url = f"{self._base_url}/search"

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                # 1. Exact `/get` lookup across title & artist variants
                for title_candidate in title_variants[:2]:
                    for artist_candidate in artist_variants[:4]:
                        params: dict[str, str | int] = {
                            "track_name": title_candidate,
                            "artist_name": artist_candidate,
                            "album_name": metadata.album,
                        }
                        if metadata.duration_seconds is not None:
                            params["duration"] = metadata.duration_seconds

                        response = await client.get(get_url, params=params)
                        if response.status_code != 200:
                            continue
                        raw_json = response.json()
                        if isinstance(raw_json, dict):
                            found = _payload_to_lyrics_data(raw_json)
                            if found is not None:
                                return found

                # 2. Fallback to `/search` (tolerates album title mismatches and Japanese/Romaji scripts)
                search_queries: list[dict[str, str]] = []
                for title_candidate in title_variants[:2]:
                    for artist_candidate in artist_variants[:2]:
                        if artist_candidate and artist_candidate != "Unknown Artist":
                            search_queries.append(
                                {
                                    "track_name": title_candidate,
                                    "artist_name": artist_candidate,
                                }
                            )
                            search_queries.append({"q": f"{artist_candidate} {title_candidate}"})
                    search_queries.append({"q": title_candidate})
                if (
                    artist_variants
                    and artist_variants[0] != "Unknown Artist"
                    and metadata.duration_seconds is not None
                ):
                    search_queries.append({"q": artist_variants[0]})

                best_hit: dict[str, object] | None = None
                best_score = 0.0
                for q_params in search_queries:
                    resp = await client.get(search_url, params=q_params)
                    if resp.status_code != 200:
                        continue
                    items = resp.json()
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        score = _score_search_hit(item, metadata)
                        if score > best_score and score >= 0.45:
                            best_score = score
                            best_hit = item
                    # Stop early if we found a high-confidence synced match
                    if best_hit is not None and best_score >= 0.80 and best_hit.get("syncedLyrics"):
                        break

                if best_hit is not None:
                    found = _payload_to_lyrics_data(best_hit)
                    if found is not None:
                        return found

                logger.debug(
                    "No lyrics found on LRCLIB for %s - %s",
                    metadata.artist,
                    metadata.title,
                )
                return None
        except httpx.HTTPError as exc:
            logger.warning("LRCLIB request failed for %s: %s", metadata.title, exc)
            return None
