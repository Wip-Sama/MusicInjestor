"""Metadata inspection and MusicBrainz confidence scoring service."""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path

import musicbrainzngs
import mutagen

from src.core.models import AudioFingerprint, MetadataCandidate, TrackMetadata

logger = logging.getLogger(__name__)

_LEADING_TRACK_RE = re.compile(r"^(?:(\d{1,2})-)?(\d{1,2})\s*(?:[-–—.]\s*|\s+)(.+)$")
_ARTIST_TITLE_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_LEADING_INT_RE = re.compile(r"^\s*(\d+)")
_LUCENE_SPECIAL_RE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
_ALNUM_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)

_CV_SEGMENT_RE = re.compile(
    r"(?P<char>[^()（）,&/・、]+?)\s*[(\uff08]\s*"
    r"(?:(?:CV|VA)\s*(?:[:.：・]\s*|\s+)|CV(?=[A-Z]))"
    r"(?P<cv>[^()（）]+?)\s*[)\uff09]",
    re.IGNORECASE,
)
_CV_FORMAT_RE = re.compile(r"\s*[(\uff08]\s*CV\s*[:.：・]?\s*", re.IGNORECASE)
_FEAT_SPLIT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|starring|with)\s+", re.IGNORECASE)
_MULTI_NAME_SPLIT_RE = re.compile(r"\s*(?:,|&|/|・|、)\s*")

_MB_URL_RE = re.compile(
    r"musicbrainz\.org/(release-group|release|recording|artist|track)/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)
_MBID_UUID_RE = re.compile(
    r"^(?:(release-group|release|recording|artist|mbid)\s*[:=]\s*)?"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$",
    re.IGNORECASE,
)
_ISRC_QUERY_RE = re.compile(
    r"^(?:isrc\s*[:=]\s*)?([A-Z]{2}-?[A-Z0-9]{3}-?\d{2}-?\d{5})$",
    re.IGNORECASE,
)
_KV_SEGMENT_RE = re.compile(
    r"^(artist|title|album|albumartist|year|track|isrc|mbid|release|recording)\s*[:=]\s*(.+)$",
    re.IGNORECASE,
)

MusicBrainzSearchFn = Callable[[TrackMetadata, AudioFingerprint | None], list[dict[str, object]]]


def _escape_lucene(value: str) -> str:
    """Escape Lucene special characters in a free-text query string."""
    return _LUCENE_SPECIAL_RE.sub(r"\\\1", value)


def _normalize_compact(value: str) -> str:
    """Normalize a string to lowercase alphanumeric characters for punctuation-insensitive matching."""
    return _ALNUM_NORMALIZE_RE.sub(" ", value.lower()).strip()


def extract_artist_search_variants(artist: str) -> list[str]:
    """Extract candidate search names from composite anime/character credits like `EMILIA(CV:RIE TAKAHASHI)`.

    Returns a deduplicated list ordered from most specific to component voice actor (`CV`) and
    character names so MusicBrainz and LRCLIB lookups succeed even when credited solely to the voice actor.
    """
    cleaned = artist.strip()
    if not cleaned or cleaned == "Unknown Artist":
        return []

    variants: list[str] = [cleaned]
    seen: set[str] = {cleaned.lower()}

    def _add(candidate: str) -> None:
        norm = candidate.strip()
        if norm and len(norm) >= 2 and norm.lower() not in seen:
            seen.add(norm.lower())
            variants.append(norm)

    # Normalize spacing around `(CV: ...)` if present
    if _CV_FORMAT_RE.search(cleaned):
        formatted_cv = _CV_FORMAT_RE.sub(" (CV: ", cleaned).replace("）", ")").strip()
        _add(formatted_cv)

    cv_actors: list[str] = []
    characters: list[str] = []
    for match in _CV_SEGMENT_RE.finditer(cleaned):
        char_part = match.group("char").strip()
        cv_part = match.group("cv").strip()
        if cv_part:
            for sub_cv in _MULTI_NAME_SPLIT_RE.split(cv_part):
                if sub_cv.strip():
                    cv_actors.append(sub_cv.strip())
        if char_part:
            for sub_char in _MULTI_NAME_SPLIT_RE.split(char_part):
                if sub_char.strip():
                    characters.append(sub_char.strip())

    # Prioritize voice actors (CV) right after the full credit, followed by character names
    for actor in cv_actors:
        _add(actor)
    for char_name in characters:
        _add(char_name)

    for part in _FEAT_SPLIT_RE.split(cleaned):
        if part.strip() and part.strip() != cleaned:
            _add(part.strip())

    return variants


def _best_artist_similarity(source_artist: str, candidate_artist: str) -> float:
    """Compute artist similarity accounting for `Character (CV: Voice Actor)` credits and punctuation."""
    src_clean = source_artist.strip().lower()
    cand_clean = candidate_artist.strip().lower()
    if not src_clean or not cand_clean:
        return 0.0

    direct_ratio = SequenceMatcher(None, src_clean, cand_clean).ratio()
    compact_ratio = SequenceMatcher(
        None,
        _normalize_compact(src_clean),
        _normalize_compact(cand_clean),
    ).ratio()
    best = max(direct_ratio, compact_ratio)

    src_variants = extract_artist_search_variants(source_artist)
    cand_variants = extract_artist_search_variants(candidate_artist)
    if len(src_variants) > 1 or len(cand_variants) > 1:
        for s_var in src_variants:
            for c_var in cand_variants:
                var_ratio = SequenceMatcher(
                    None,
                    _normalize_compact(s_var),
                    _normalize_compact(c_var),
                ).ratio()
                # Slightly discount partial component matches (0.95) vs full credit matches (1.0)
                weight = 1.0 if (s_var == source_artist and c_var == candidate_artist) else 0.95
                best = max(best, var_ratio * weight)

    return min(best, 1.0)


def _best_album_similarity(source_album: str, candidate_album: str) -> float:
    """Compute album similarity, rewarding short titles contained in long promotional titles."""
    src_clean = source_album.strip().lower()
    cand_clean = candidate_album.strip().lower()
    if not src_clean or not cand_clean:
        return 0.0

    direct_ratio = SequenceMatcher(None, src_clean, cand_clean).ratio()
    shorter, longer = (
        (src_clean, cand_clean) if len(src_clean) <= len(cand_clean) else (cand_clean, src_clean)
    )
    if len(shorter) >= 3 and shorter in longer:
        return max(direct_ratio, 0.90)
    return direct_ratio


def compute_metadata_similarity(source: TrackMetadata, candidate: TrackMetadata) -> float:
    """Calculate a normalized similarity confidence score in [0.0, 1.0] between two metadata sets."""
    title_ratio = SequenceMatcher(
        None,
        source.title.strip().lower(),
        candidate.title.strip().lower(),
    ).ratio()
    artist_ratio = _best_artist_similarity(source.artist, candidate.artist)
    album_ratio = _best_album_similarity(source.album, candidate.album)

    weighted_score = (0.45 * title_ratio) + (0.35 * artist_ratio) + (0.20 * album_ratio)
    return round(min(max(weighted_score, 0.0), 1.0), 4)


def parse_filename_metadata(stem: str) -> TrackMetadata:
    """Parse optional `[disc-]track - Artist - Title` or `Artist - Title` from a filename stem."""
    cleaned = stem.strip()
    disc_number: int | None = None
    track_number: int | None = None
    remainder = cleaned

    track_match = _LEADING_TRACK_RE.match(cleaned)
    if track_match is not None:
        disc_number = int(track_match.group(1)) if track_match.group(1) else None
        track_number = int(track_match.group(2))
        remainder = track_match.group(3).strip()

    parts = _ARTIST_TITLE_SPLIT_RE.split(remainder, maxsplit=1)
    if len(parts) == 2:
        artist = parts[0].strip() or "Unknown Artist"
        title = parts[1].strip() or remainder
    else:
        artist = "Unknown Artist"
        title = remainder or cleaned

    return TrackMetadata(
        title=title,
        artist=artist,
        album="Unknown Album",
        track_number=track_number,
        disc_number=disc_number,
    )


def parse_manual_search_query(
    query: str,
    base_metadata: TrackMetadata | None = None,
) -> TrackMetadata:
    """Parse a user's manual search input into a `TrackMetadata` query descriptor.

    Supports:
    - MusicBrainz URLs (`musicbrainz.org/release/<uuid>`, `/recording/<uuid>`, etc.)
    - Raw or prefixed MBIDs (`<uuid>`, `release:<uuid>`, `recording:<uuid>`)
    - ISRC codes (`isrc:JPD731600777` or `JPD731600777`)
    - Key-value overrides (`artist: Rie Takahashi | title: Stay Alive`)
    - `Artist - Title | Album` or single artist override when `base_metadata.title` is known
    """
    base = base_metadata or TrackMetadata(
        title="Unknown Title",
        artist="Unknown Artist",
        album="Unknown Album",
    )
    cleaned = query.strip()
    if not cleaned:
        return base

    url_match = _MB_URL_RE.search(cleaned)
    if url_match is not None:
        entity_type = url_match.group(1).lower()
        mbid = url_match.group(2).lower()
        if entity_type == "release":
            return replace(base, release_mbid=mbid)
        if entity_type in {"recording", "track"}:
            return replace(base, recording_mbid=mbid)
        if entity_type == "release-group":
            return replace(base, release_mbid=f"release-group:{mbid}")
        if entity_type == "artist":
            return replace(base, recording_mbid=f"artist:{mbid}")

    uuid_match = _MBID_UUID_RE.match(cleaned)
    if uuid_match is not None:
        prefix = (uuid_match.group(1) or "").lower()
        mbid = uuid_match.group(2).lower()
        if prefix == "release":
            return replace(base, release_mbid=mbid)
        if prefix == "recording":
            return replace(base, recording_mbid=mbid)
        if prefix == "release-group":
            return replace(base, release_mbid=f"release-group:{mbid}")
        if prefix == "artist":
            return replace(base, recording_mbid=f"artist:{mbid}")
        return replace(base, release_mbid=mbid, recording_mbid=mbid)

    isrc_match = _ISRC_QUERY_RE.match(cleaned)
    if isrc_match is not None:
        normalized_isrc = isrc_match.group(1).replace("-", "").upper()
        return replace(base, isrc=normalized_isrc)

    segments = [seg.strip() for seg in re.split(r"[|;\n]+", cleaned) if seg.strip()]
    kv_updates: dict[str, object] = {}
    non_kv_segments: list[str] = []
    for seg in segments:
        kv_match = _KV_SEGMENT_RE.match(seg)
        if kv_match is not None:
            key = kv_match.group(1).lower()
            val = kv_match.group(2).strip()
            if key == "artist":
                kv_updates["artist"] = val
            elif key == "title":
                kv_updates["title"] = val
            elif key == "album":
                kv_updates["album"] = val
            elif key == "albumartist":
                kv_updates["album_artist"] = val
            elif key == "year":
                kv_updates["year"] = _parse_year(val) or base.year
            elif key == "track":
                kv_updates["track_number"] = _parse_int_prefix(val) or base.track_number
            elif key == "isrc":
                kv_updates["isrc"] = val.replace("-", "").upper()
            elif key in {"release", "mbid"}:
                kv_updates["release_mbid"] = val.lower()
            elif key == "recording":
                kv_updates["recording_mbid"] = val.lower()
        else:
            non_kv_segments.append(seg)

    if kv_updates and not non_kv_segments:
        return TrackMetadata(
            title=str(kv_updates.get("title", base.title)),
            artist=str(kv_updates.get("artist", base.artist)),
            album=str(kv_updates.get("album", base.album)),
            album_artist=(
                str(kv_updates["album_artist"])
                if "albumartist" in kv_updates or "album_artist" in kv_updates
                else base.album_artist
            ),
            year=int(str(kv_updates["year"])) if kv_updates.get("year") is not None else base.year,
            track_number=(
                int(str(kv_updates["track_number"]))
                if kv_updates.get("track_number") is not None
                else base.track_number
            ),
            disc_number=base.disc_number,
            genre=base.genre,
            duration_seconds=base.duration_seconds,
            recording_mbid=(
                str(kv_updates["recording_mbid"])
                if kv_updates.get("recording_mbid") is not None
                else base.recording_mbid
            ),
            release_mbid=(
                str(kv_updates["release_mbid"])
                if kv_updates.get("release_mbid") is not None
                else base.release_mbid
            ),
            isrc=str(kv_updates["isrc"]) if kv_updates.get("isrc") is not None else base.isrc,
        )

    album_override = str(kv_updates.get("album", "Unknown Album"))
    query_main = cleaned
    if "|" in cleaned:
        left_part, right_part = cleaned.split("|", maxsplit=1)
        query_main = left_part.strip()
        if "album" not in kv_updates:
            album_override = right_part.strip() or "Unknown Album"

    parsed = parse_filename_metadata(query_main)
    resolved_artist = parsed.artist
    resolved_title = parsed.title

    # If the user entered a single name without " - " (e.g. "Rie Takahashi") and the track already
    # has a known title, treat the single phrase as an artist override while keeping the known title.
    if (
        resolved_artist == "Unknown Artist"
        and base_metadata is not None
        and base_metadata.title not in {"", "Unknown Title"}
        and resolved_title.strip().lower() != base_metadata.title.strip().lower()
    ):
        resolved_artist = resolved_title.strip()
        resolved_title = base_metadata.title

    return TrackMetadata(
        title=str(kv_updates.get("title", resolved_title)),
        artist=str(kv_updates.get("artist", resolved_artist)),
        album=album_override,
        album_artist=base.album_artist,
        year=base.year,
        track_number=parsed.track_number or base.track_number,
        disc_number=parsed.disc_number or base.disc_number,
        genre=base.genre,
        duration_seconds=base.duration_seconds,
        recording_mbid=None,
        release_mbid=None,
        isrc=None,
    )


def _first_tag_value(tags: object, *keys: str) -> str | None:
    """Extract the first non-empty string value for any of `keys` from a Mutagen tag mapping."""
    getter = getattr(tags, "get", None)
    if not callable(getter):
        return None
    for key in keys:
        try:
            raw = getter(key)
        except Exception:
            continue
        if isinstance(raw, list) and raw:
            text = str(raw[0]).strip()
            if text:
                return text
        elif isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _parse_int_prefix(value: str | None) -> int | None:
    """Extract a leading integer from strings like `'01/12'` or `'1'`."""
    if not value:
        return None
    match = _LEADING_INT_RE.match(value)
    return int(match.group(1)) if match else None


def _parse_year(value: str | None) -> int | None:
    """Extract a 4-digit release year from strings like `'2016-08-24'` or `'2016'`."""
    if not value:
        return None
    match = _YEAR_RE.search(value)
    return int(match.group(1)) if match else None


def _read_mutagen_metadata_sync(audio_path: Path, fallback: TrackMetadata) -> TrackMetadata:
    """Inspect embedded audio tags via `mutagen` and merge with filename fallback values."""
    if not audio_path.is_file():
        return fallback

    try:
        audio = mutagen.File(audio_path, easy=True)
    except Exception as exc:
        logger.debug("Mutagen could not parse %s (%s); using filename fallback", audio_path.name, exc)
        return fallback

    if audio is None:
        return fallback

    tags = getattr(audio, "tags", None)
    info = getattr(audio, "info", None)
    length_raw = getattr(info, "length", None)
    duration_seconds = (
        int(round(float(length_raw)))
        if isinstance(length_raw, (int, float)) and length_raw > 0
        else fallback.duration_seconds
    )

    title = _first_tag_value(tags, "title") or fallback.title
    artist = _first_tag_value(tags, "artist") or fallback.artist
    album = _first_tag_value(tags, "album") or fallback.album
    album_artist = _first_tag_value(tags, "albumartist", "album artist", "performer") or fallback.album_artist
    year = _parse_year(_first_tag_value(tags, "date", "year", "originaldate")) or fallback.year
    track_number = _parse_int_prefix(_first_tag_value(tags, "tracknumber")) or fallback.track_number
    disc_number = _parse_int_prefix(_first_tag_value(tags, "discnumber")) or fallback.disc_number
    genre = _first_tag_value(tags, "genre") or fallback.genre
    recording_mbid = (
        _first_tag_value(tags, "musicbrainz_trackid", "musicbrainz_releasetrackid") or fallback.recording_mbid
    )
    release_mbid = _first_tag_value(tags, "musicbrainz_albumid") or fallback.release_mbid
    isrc_raw = _first_tag_value(tags, "isrc") or fallback.isrc
    isrc = isrc_raw.replace("-", "").strip().upper() if isrc_raw else None

    return TrackMetadata(
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        year=year,
        track_number=track_number,
        disc_number=disc_number,
        genre=genre,
        duration_seconds=duration_seconds,
        recording_mbid=recording_mbid,
        release_mbid=release_mbid,
        isrc=isrc,
    )


def _extract_track_and_disc_from_release(
    release: dict[str, object],
    default_track: int | None,
    default_disc: int | None,
) -> tuple[int, int, bool]:
    """Extract track and disc numbers from a MusicBrainz `medium-list` structure."""
    track_num = default_track or 1
    disc_num = default_disc or 1
    has_explicit_track = False
    medium_list = release.get("medium-list")
    if isinstance(medium_list, list) and medium_list:
        first_medium = medium_list[0]
        if isinstance(first_medium, dict):
            pos = first_medium.get("position")
            if pos is not None:
                disc_num = _parse_int_prefix(str(pos)) or disc_num
            track_list = first_medium.get("track-list")
            if isinstance(track_list, list) and track_list:
                first_track = track_list[0]
                if isinstance(first_track, dict):
                    num = first_track.get("number") or first_track.get("position")
                    if num is not None:
                        parsed_num = _parse_int_prefix(str(num))
                        if parsed_num is not None:
                            track_num = parsed_num
                            has_explicit_track = True
    return track_num, disc_num, has_explicit_track


def _flip_sort_name(sort_name: str) -> str:
    """Convert `'Last, First'` sort-names into `'First Last'` display order."""
    if "," in sort_name:
        parts = [p.strip() for p in sort_name.split(",", maxsplit=1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[1]} {parts[0]}"
    return sort_name.strip()


def _extract_english_artist_credit(artist_credit: object) -> str | None:
    """Reconstruct an English/Romaji artist credit string from MusicBrainz `artist-credit` aliases."""
    if not isinstance(artist_credit, list) or not artist_credit:
        return None

    pieces: list[str] = []
    found_english = False
    for idx, item in enumerate(artist_credit):
        if isinstance(item, str):
            pieces.append(item)
            continue
        if not isinstance(item, dict):
            continue

        artist_obj = item.get("artist")
        chosen_name: str | None = None
        if isinstance(artist_obj, dict):
            alias_list = artist_obj.get("alias-list")
            if isinstance(alias_list, list):
                for alias in alias_list:
                    if isinstance(alias, dict):
                        alias_text = str(alias.get("alias", "")).strip()
                        locale = str(alias.get("locale", "")).lower()
                        is_primary = str(alias.get("primary", "")).lower() == "primary"
                        if alias_text and locale.startswith("en") and is_primary:
                            chosen_name = alias_text
                            found_english = True
                            break
                if chosen_name is None:
                    for alias in alias_list:
                        if isinstance(alias, dict):
                            alias_text = str(alias.get("alias", "")).strip()
                            locale = str(alias.get("locale", "")).lower()
                            if alias_text and (locale.startswith("en") or alias_text.isascii()):
                                chosen_name = alias_text
                                found_english = True
                                break

            if chosen_name is None:
                sort_name = str(artist_obj.get("sort-name", "")).strip()
                if sort_name and sort_name.isascii():
                    chosen_name = _flip_sort_name(sort_name)
                    found_english = True

        if chosen_name is None:
            fallback_name = str(
                item.get("name") or (artist_obj.get("name") if isinstance(artist_obj, dict) else "") or ""
            ).strip()
            chosen_name = fallback_name

        pieces.append(chosen_name)
        join_phrase = item.get("joinphrase")
        if (
            isinstance(join_phrase, str)
            and join_phrase
            and (idx + 1 >= len(artist_credit) or not isinstance(artist_credit[idx + 1], str))
        ):
            pieces.append(join_phrase)

    if not found_english:
        return None

    combined = "".join(pieces).strip()
    if not combined:
        return None
    return _CV_FORMAT_RE.sub(" (CV: ", combined).replace("）", ")").strip()


class MusicBrainzMetadataService:
    """Reads local audio tags via `mutagen` and queries canonical MusicBrainz releases."""

    def __init__(
        self,
        app_name: str = "MusicIngestBot",
        app_version: str = "0.1.0",
        contact: str = "admin@example.com",
        *,
        enable_network: bool = False,
        search_fn: MusicBrainzSearchFn | None = None,
    ) -> None:
        self._app_name = app_name
        self._app_version = app_version
        self._contact = contact
        self._enable_network = enable_network
        self._search_fn = search_fn
        musicbrainzngs.set_useragent(self._app_name, self._app_version, self._contact)

    async def read_local_metadata(self, audio_path: Path) -> TrackMetadata:
        """Derive baseline metadata from embedded `mutagen` tags and filename stem."""
        fallback = parse_filename_metadata(audio_path.stem)
        return await asyncio.to_thread(_read_mutagen_metadata_sync, audio_path, fallback)

    async def find_candidates(
        self,
        local_metadata: TrackMetadata,
        fingerprint: AudioFingerprint | None = None,
    ) -> list[MetadataCandidate]:
        """Return candidate metadata matches ordered by descending confidence."""
        logger.debug(
            "Searching metadata candidates for artist=%s, title=%s (app=%s/%s)",
            local_metadata.artist,
            local_metadata.title,
            self._app_name,
            self._app_version,
        )
        if self._enable_network or self._search_fn is not None:
            mb_candidates = await asyncio.to_thread(
                self._search_musicbrainz_candidates_sync,
                local_metadata,
                fingerprint,
            )
            if mb_candidates:
                return mb_candidates

        return [self._build_fallback_candidate(local_metadata, fingerprint)]

    def _build_fallback_candidate(
        self,
        local_metadata: TrackMetadata,
        fingerprint: AudioFingerprint | None,
    ) -> MetadataCandidate:
        """Construct a baseline candidate directly from local tags when MusicBrainz returns no match."""
        baseline_confidence = 0.95 if local_metadata.artist != "Unknown Artist" else 0.50
        enriched = TrackMetadata(
            title=local_metadata.title,
            artist=local_metadata.artist,
            album=local_metadata.album,
            album_artist=local_metadata.album_artist,
            year=local_metadata.year,
            track_number=local_metadata.track_number or 1,
            disc_number=local_metadata.disc_number or 1,
            genre=local_metadata.genre,
            duration_seconds=(
                fingerprint.duration_seconds if fingerprint is not None else local_metadata.duration_seconds
            ),
            recording_mbid=(
                fingerprint.acoustid_mbid
                if fingerprint is not None and fingerprint.acoustid_mbid
                else local_metadata.recording_mbid
            ),
            release_mbid=local_metadata.release_mbid,
            isrc=local_metadata.isrc,
        )
        return MetadataCandidate(
            metadata=enriched,
            confidence=baseline_confidence,
        )

    def _lookup_recordings_from_release_mbid(
        self,
        release_mbid: str,
        local_metadata: TrackMetadata,
    ) -> list[dict[str, object]]:
        """Fetch a MusicBrainz release by UUID and convert its matching track(s) into recording payloads."""
        try:
            rel_resp = musicbrainzngs.get_release_by_id(
                release_mbid,
                includes=["artists", "recordings", "release-groups", "media"],
            )
        except Exception as exc:
            logger.debug("MusicBrainz get_release_by_id(%s) failed: %s", release_mbid, exc)
            return []

        rel_obj = rel_resp.get("release") if isinstance(rel_resp, dict) else None
        if not isinstance(rel_obj, dict):
            return []

        rel_title = str(rel_obj.get("title") or local_metadata.album).strip()
        rel_date = str(rel_obj.get("date") or "")
        rel_group = rel_obj.get("release-group")
        rel_disambig = str(rel_obj.get("disambiguation") or "")
        rel_artist_credit = rel_obj.get("artist-credit")
        rel_artist_phrase = str(rel_obj.get("artist-credit-phrase") or local_metadata.artist).strip()

        scored_tracks: list[tuple[float, dict[str, object]]] = []
        medium_list = rel_obj.get("medium-list")
        if isinstance(medium_list, list):
            for medium in medium_list:
                if not isinstance(medium, dict):
                    continue
                med_pos = str(medium.get("position") or "1")
                track_list = medium.get("track-list")
                if not isinstance(track_list, list):
                    continue
                for trk in track_list:
                    if not isinstance(trk, dict):
                        continue
                    rec_inner = trk.get("recording")
                    rec_dict = rec_inner if isinstance(rec_inner, dict) else {}
                    trk_title = str(trk.get("title") or rec_dict.get("title") or local_metadata.title).strip()
                    trk_num_str = str(trk.get("number") or trk.get("position") or "1")
                    trk_num = _parse_int_prefix(trk_num_str) or 1
                    trk_length = trk.get("length") or rec_dict.get("length")
                    rec_id = str(rec_dict.get("id") or trk.get("id") or "").strip()
                    trk_artist_credit = (
                        trk.get("artist-credit") or rec_dict.get("artist-credit") or rel_artist_credit
                    )
                    trk_artist_phrase = str(
                        trk.get("artist-credit-phrase")
                        or rec_dict.get("artist-credit-phrase")
                        or rel_artist_phrase
                    ).strip()

                    score = 0.5
                    if local_metadata.title not in {"", "Unknown Title"}:
                        score = SequenceMatcher(
                            None,
                            local_metadata.title.strip().lower(),
                            trk_title.lower(),
                        ).ratio()
                    if local_metadata.track_number and trk_num == local_metadata.track_number:
                        score += 0.25

                    synthetic_rec: dict[str, object] = {
                        "id": rec_id,
                        "title": trk_title,
                        "artist-credit": trk_artist_credit,
                        "artist-credit-phrase": trk_artist_phrase,
                        "length": trk_length,
                        "ext:score": "100",
                        "release-list": [
                            {
                                "id": release_mbid,
                                "title": rel_title,
                                "date": rel_date,
                                "disambiguation": rel_disambig,
                                "release-group": rel_group,
                                "medium-list": [
                                    {
                                        "position": med_pos,
                                        "track-list": [{"number": str(trk_num)}],
                                    }
                                ],
                            }
                        ],
                    }
                    scored_tracks.append((score, synthetic_rec))

        if not scored_tracks:
            return [
                {
                    "id": local_metadata.recording_mbid or "",
                    "title": local_metadata.title,
                    "artist-credit": rel_artist_credit,
                    "artist-credit-phrase": rel_artist_phrase,
                    "ext:score": "100",
                    "release-list": [rel_obj],
                }
            ]

        scored_tracks.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored_tracks[:3]]

    def _resolve_artist_mbids(self, artist_variants: list[str]) -> list[str]:
        """Resolve English/Romaji or CV artist names to MusicBrainz artist UUIDs (`arid`)."""
        resolved_ids: list[str] = []
        for variant in artist_variants[:3]:
            target_tokens = set(_normalize_compact(variant).split())
            if not target_tokens:
                continue
            try:
                resp = musicbrainzngs.search_artists(query=variant, limit=3)
            except Exception as exc:
                logger.debug("MusicBrainz search_artists(%s) failed: %s", variant, exc)
                continue

            artist_list = resp.get("artist-list") if isinstance(resp, dict) else None
            if not isinstance(artist_list, list):
                continue

            for art in artist_list:
                if not isinstance(art, dict):
                    continue
                art_id = str(art.get("id") or "").strip()
                if not art_id or art_id in resolved_ids:
                    continue

                candidate_names = [
                    str(art.get("name") or ""),
                    _flip_sort_name(str(art.get("sort-name") or "")),
                ]
                alias_list = art.get("alias-list")
                if isinstance(alias_list, list):
                    for alias in alias_list:
                        if isinstance(alias, dict):
                            candidate_names.append(str(alias.get("alias") or ""))
                            candidate_names.append(_flip_sort_name(str(alias.get("sort-name") or "")))

                for cand_name in candidate_names:
                    cand_tokens = set(_normalize_compact(cand_name).split())
                    if cand_tokens and cand_tokens == target_tokens:
                        resolved_ids.append(art_id)
                        break
        return resolved_ids

    def _query_recordings_from_musicbrainz(
        self,
        local_metadata: TrackMetadata,
        fingerprint: AudioFingerprint | None,
    ) -> list[dict[str, object]]:
        """Execute synchronous `musicbrainzngs` recording queries with progressive fallback."""
        if self._search_fn is not None:
            return self._search_fn(local_metadata, fingerprint)

        recordings: list[dict[str, object]] = []

        # 1. Direct Release MBID or Release-Group MBID lookup (e.g. from pasted MusicBrainz URL)
        rel_mbid = local_metadata.release_mbid
        if rel_mbid:
            if rel_mbid.startswith("release-group:"):
                rgid = rel_mbid.split(":", maxsplit=1)[1]
                title_clause = (
                    f' AND recording:"{_escape_lucene(local_metadata.title)}"'
                    if local_metadata.title not in {"", "Unknown Title"}
                    else ""
                )
                try:
                    rg_resp = musicbrainzngs.search_recordings(
                        query=f"rgid:{rgid}{title_clause}",
                        limit=5,
                    )
                    rg_list = rg_resp.get("recording-list") if isinstance(rg_resp, dict) else None
                    if isinstance(rg_list, list):
                        recordings.extend(r for r in rg_list if isinstance(r, dict))
                except Exception as exc:
                    logger.debug("MusicBrainz search_recordings(rgid=%s) failed: %s", rgid, exc)
            elif len(rel_mbid) >= 32:
                recordings.extend(self._lookup_recordings_from_release_mbid(rel_mbid, local_metadata))

        # 2. Direct Recording MBID or Artist MBID lookup
        mbid = (
            fingerprint.acoustid_mbid
            if fingerprint is not None and fingerprint.acoustid_mbid
            else local_metadata.recording_mbid
        )
        if not recordings and mbid:
            if mbid.startswith("artist:"):
                arid = mbid.split(":", maxsplit=1)[1]
                title_clause = (
                    f' AND recording:"{_escape_lucene(local_metadata.title)}"'
                    if local_metadata.title not in {"", "Unknown Title"}
                    else ""
                )
                try:
                    ar_resp = musicbrainzngs.search_recordings(
                        query=f"arid:{arid}{title_clause}",
                        limit=5,
                    )
                    ar_list = ar_resp.get("recording-list") if isinstance(ar_resp, dict) else None
                    if isinstance(ar_list, list):
                        recordings.extend(r for r in ar_list if isinstance(r, dict))
                except Exception as exc:
                    logger.debug("MusicBrainz search_recordings(arid=%s) failed: %s", arid, exc)
            elif len(mbid) >= 32:
                try:
                    rec_resp = musicbrainzngs.get_recording_by_id(
                        mbid,
                        includes=["artists", "releases", "release-groups", "media"],
                    )
                    rec_obj = rec_resp.get("recording") if isinstance(rec_resp, dict) else None
                    if isinstance(rec_obj, dict):
                        rec_obj["ext:score"] = "100"
                        recordings.append(rec_obj)
                except Exception as exc:
                    logger.debug("MusicBrainz get_recording_by_id(%s) failed: %s", mbid, exc)

        # 3. ISRC lookup (exact recording identifier when embedded in FLAC/MP3 tags or entered manually)
        if not recordings and local_metadata.isrc:
            try:
                isrc_resp = musicbrainzngs.search_recordings(
                    query=f"isrc:{_escape_lucene(local_metadata.isrc.strip())}",
                    limit=5,
                )
                isrc_list = isrc_resp.get("recording-list") if isinstance(isrc_resp, dict) else None
                if isinstance(isrc_list, list):
                    recordings.extend(r for r in isrc_list if isinstance(r, dict))
            except Exception as exc:
                logger.debug("MusicBrainz ISRC search (%s) failed: %s", local_metadata.isrc, exc)

        # 4. Title + Artist (including CV / voice actor decomposition and English alias -> arid lookup)
        title_clean = local_metadata.title.strip()
        if not recordings and title_clean and title_clean != "Unknown Title":
            artist_variants = extract_artist_search_variants(local_metadata.artist)
            if not artist_variants:
                query_kwargs: dict[str, object] = {"recording": title_clean, "limit": 5}
                if local_metadata.album != "Unknown Album":
                    query_kwargs["release"] = local_metadata.album.strip()
                try:
                    search_resp = musicbrainzngs.search_recordings(**query_kwargs)
                    rec_list = search_resp.get("recording-list") if isinstance(search_resp, dict) else None
                    if isinstance(rec_list, list):
                        recordings.extend(r for r in rec_list if isinstance(r, dict))
                except Exception as exc:
                    logger.warning("MusicBrainz search_recordings failed: %s", exc)
            else:
                for variant in artist_variants[:2]:
                    try:
                        search_resp = musicbrainzngs.search_recordings(
                            recording=title_clean,
                            artist=variant,
                            limit=5,
                        )
                        rec_list = (
                            search_resp.get("recording-list") if isinstance(search_resp, dict) else None
                        )
                        if isinstance(rec_list, list) and rec_list:
                            recordings.extend(r for r in rec_list if isinstance(r, dict))
                            break
                    except Exception as exc:
                        logger.warning("MusicBrainz search_recordings(%s) failed: %s", variant, exc)

                # If direct artist-credit search found nothing (e.g. Romaji CV vs Japanese artist script),
                # resolve the artist/CV alias to MusicBrainz artist IDs (`arid`) and query by `arid`.
                if not recordings:
                    arids = self._resolve_artist_mbids(artist_variants)
                    if arids:
                        arid_clause = " OR ".join(f"arid:{aid}" for aid in arids)
                        lucene_query = f'recording:"{_escape_lucene(title_clean)}" AND ({arid_clause})'
                        try:
                            arid_resp = musicbrainzngs.search_recordings(query=lucene_query, limit=5)
                            arid_list = (
                                arid_resp.get("recording-list") if isinstance(arid_resp, dict) else None
                            )
                            if isinstance(arid_list, list):
                                recordings.extend(r for r in arid_list if isinstance(r, dict))
                        except Exception as exc:
                            logger.warning("MusicBrainz arid search_recordings failed: %s", exc)

                if not recordings:
                    escaped_artist = _escape_lucene(artist_variants[0])
                    escaped_title = _escape_lucene(title_clean)
                    try:
                        broad_resp = musicbrainzngs.search_recordings(
                            query=f"{escaped_artist} {escaped_title}",
                            limit=5,
                        )
                        broad_list = (
                            broad_resp.get("recording-list") if isinstance(broad_resp, dict) else None
                        )
                        if isinstance(broad_list, list):
                            recordings.extend(r for r in broad_list if isinstance(r, dict))
                    except Exception as exc:
                        logger.warning("MusicBrainz broad search_recordings failed: %s", exc)

        return recordings

    def _search_musicbrainz_candidates_sync(
        self,
        local_metadata: TrackMetadata,
        fingerprint: AudioFingerprint | None,
    ) -> list[MetadataCandidate]:
        """Convert raw MusicBrainz recording/release payloads into ranked `MetadataCandidate` items."""
        recordings = self._query_recordings_from_musicbrainz(local_metadata, fingerprint)
        if not recordings:
            return []

        candidates: list[MetadataCandidate] = []
        seen_keys: set[tuple[str, str, str, int | None, str]] = set()

        for rec in recordings:
            rec_id = str(rec.get("id", "")).strip() or local_metadata.recording_mbid
            rec_title = str(rec.get("title") or local_metadata.title).strip()
            rec_artist = str(rec.get("artist-credit-phrase") or local_metadata.artist).strip()
            english_artist = _extract_english_artist_credit(rec.get("artist-credit"))

            length_raw = rec.get("length")
            duration_sec = (
                int(str(length_raw)) // 1000
                if length_raw is not None and str(length_raw).isdigit()
                else (
                    fingerprint.duration_seconds
                    if fingerprint is not None
                    else local_metadata.duration_seconds
                )
            )
            ext_score_raw = rec.get("ext:score")
            mb_score = (
                float(str(ext_score_raw)) / 100.0
                if ext_score_raw is not None and str(ext_score_raw).isdigit()
                else 0.85
            )
            rec_disambig = str(rec.get("disambiguation", "")).strip()

            release_list = rec.get("release-list")
            releases: list[dict[str, object]] = (
                [r for r in release_list if isinstance(r, dict)]
                if isinstance(release_list, list) and release_list
                else [{"title": local_metadata.album}]
            )

            # Score all releases in `release-list` before selecting top releases for this recording
            per_recording_candidates: list[MetadataCandidate] = []
            for rel in releases:
                rel_id = str(rel.get("id", "")).strip() or local_metadata.release_mbid
                rel_title = str(rel.get("title") or local_metadata.album).strip()
                rel_year = _parse_year(str(rel.get("date", ""))) or local_metadata.year
                track_num, disc_num, has_explicit_track = _extract_track_and_disc_from_release(
                    rel,
                    local_metadata.track_number,
                    local_metadata.disc_number,
                )
                rel_group = rel.get("release-group")
                rel_type = (
                    str(rel_group.get("primary-type", "Album"))
                    if isinstance(rel_group, dict) and rel_group.get("primary-type")
                    else "Album"
                )
                secondary_types = (
                    rel_group.get("secondary-type-list") if isinstance(rel_group, dict) else None
                )
                is_compilation = rel_type.lower() == "compilation" or (
                    isinstance(secondary_types, list)
                    and any(str(t).lower() == "compilation" for t in secondary_types)
                )
                disambig = str(rel.get("disambiguation") or rec_disambig).strip()

                # Release-level ranking adjustments (penalize compilations, reward year/track matches)
                release_bonus = 0.0
                if is_compilation and "best" not in local_metadata.album.lower():
                    release_bonus -= 0.08
                if local_metadata.year and rel_year:
                    if rel_year == local_metadata.year:
                        release_bonus += 0.03
                    elif abs(rel_year - local_metadata.year) > 1:
                        release_bonus -= 0.04
                if local_metadata.track_number and has_explicit_track:
                    if track_num == local_metadata.track_number:
                        release_bonus += 0.02
                    else:
                        release_bonus -= 0.04
                if (
                    local_metadata.release_mbid
                    and rel_id
                    and rel_id.lower() == local_metadata.release_mbid.lower()
                ):
                    release_bonus += 0.12

                artist_choices: list[str] = [rec_artist]
                if english_artist and english_artist.lower() != rec_artist.lower():
                    artist_choices = (
                        [english_artist, rec_artist]
                        if local_metadata.artist.isascii()
                        else [rec_artist, english_artist]
                    )

                # Calculate similarity against the best-matching artist script (Romaji or native)
                comparison_source = (
                    TrackMetadata(
                        title=local_metadata.title,
                        artist=local_metadata.artist,
                        album=rel_title,
                    )
                    if local_metadata.album in {"", "Unknown Album"}
                    else local_metadata
                )
                best_sim = max(
                    compute_metadata_similarity(
                        comparison_source,
                        TrackMetadata(title=rec_title, artist=art_opt, album=rel_title),
                    )
                    for art_opt in artist_choices
                )
                base_confidence = (0.65 * best_sim) + (0.35 * mb_score) + release_bonus

                for idx, art_name in enumerate(artist_choices):
                    # Slightly favor the user's preferred script (0.005 delta) while keeping both selectable
                    script_delta = -0.005 * idx
                    combined_confidence = round(
                        min(max(base_confidence + script_delta, 0.0), 1.0),
                        4,
                    )
                    cand_meta = TrackMetadata(
                        title=rec_title,
                        artist=art_name,
                        album=rel_title,
                        album_artist=art_name,
                        year=rel_year,
                        track_number=track_num,
                        disc_number=disc_num,
                        genre=local_metadata.genre,
                        duration_seconds=duration_sec,
                        recording_mbid=rec_id,
                        release_mbid=rel_id,
                        isrc=local_metadata.isrc,
                    )
                    per_recording_candidates.append(
                        MetadataCandidate(
                            metadata=cand_meta,
                            confidence=combined_confidence,
                            release_type=rel_type,
                            disambiguation=disambig,
                        )
                    )

            per_recording_candidates.sort(key=lambda item: item.confidence, reverse=True)
            for cand in per_recording_candidates[:4]:
                dedup_key = (
                    cand.metadata.artist.lower(),
                    cand.metadata.title.lower(),
                    cand.metadata.album.lower(),
                    cand.metadata.year,
                    cand.disambiguation.lower(),
                )
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                candidates.append(cand)

        candidates.sort(key=lambda item: item.confidence, reverse=True)
        return candidates[:5]
