"""Metadata inspection and MusicBrainz, Genius, and Last.fm confidence scoring service."""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path

import httpx
import musicbrainzngs
import mutagen

from src.core.models import AudioFingerprint, MetadataCandidate, TrackMetadata
from src.core.throttler import ApiThrottler, default_api_throttler

logger = logging.getLogger(__name__)

_LEADING_TRACK_RE = re.compile(r"^(?:(\d{1,2})-)?(\d{1,2})\s*(?:[-–—.]\s*|\s+)(.+)$")
_ARTIST_TITLE_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_LEADING_INT_RE = re.compile(r"^\s*(\d+)")
_LUCENE_SPECIAL_RE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
_ALNUM_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
_YEAR_SUFFIX_RE = re.compile(r"\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]")

_CV_SEGMENT_RE = re.compile(
    r"(?P<char>[^()（）,&/・、]+?)\s*[(\uff08]\s*"
    r"(?:(?:CV|VA)\s*(?:[:.：・]\s*|\s+)|CV(?=[A-Z]))"
    r"(?P<cv>[^()（）]+?)\s*[)\uff09]",
    re.IGNORECASE,
)
_CV_FORMAT_RE = re.compile(r"\s*[(\uff08]\s*(?:CV|VA)\s*[:.：・]?\s*", re.IGNORECASE)
_CHAR_PAREN_SINGER_RE = re.compile(
    r"^(?P<outer>[^()（）]+?)\s*[(\uff08]\s*(?P<inner>[^()（）]+?)\s*[)\uff09]$"
)
_BILINGUAL_SUFFIX_RE = re.compile(
    r"^(?P<native>.*[^\x00-\x7f].*?)\s*[(\uff08]\s*(?P<latin>[\x20-\x7e]+?)\s*[)\uff09]$"
)
_ANIME_OP_ED_RE = re.compile(
    r"^.+?\b(?:OP|ED|Opening|Ending)\s*\d*(?:\s*[-–—:]\s*|\s+)(?P<song>.+)$",
    re.IGNORECASE,
)
_COLLAB_WORD_SPLIT_RE = re.compile(
    r"\s+(?:con|feat\.?|ft\.?|featuring|starring|with|vs\.?|x)\s+",
    re.IGNORECASE,
)
_MULTI_NAME_SPLIT_RE = re.compile(r"\s*(?:,|&|/|;|・|、|\+)\s*")

_PAREN_DISAMBIG_KEYWORDS: frozenset[str] = frozenset(
    {
        "band",
        "group",
        "singer",
        "artist",
        "rapper",
        "dj",
        "producer",
        "duo",
        "trio",
        "choir",
        "orchestra",
        "official",
        "jp",
        "uk",
        "us",
        "usa",
        "kr",
        "cn",
        "it",
        "fr",
        "de",
        "inst",
        "instrumental",
        "off vocal",
        "live",
        "remix",
        "ver",
        "version",
        "tv size",
        "tv mix",
        "short ver",
        "movie ver",
        "acoustic",
        "remastered",
        "remaster",
        "romanized",
        "english translation",
    }
)

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
ReleaseLookupFn = Callable[[str], dict[str, object]]


def _escape_lucene(value: str) -> str:
    """Escape Lucene special characters in a free-text query string."""
    return _LUCENE_SPECIAL_RE.sub(r"\\\1", value)


def _normalize_compact(value: str) -> str:
    """Normalize a string to lowercase alphanumeric characters for punctuation-insensitive matching."""
    return _ALNUM_NORMALIZE_RE.sub(" ", value.lower()).strip()


def _normalize_token_sorted(value: str) -> str:
    """Normalize and alphabetically sort tokens so `Hayami Saori` matches `Saori Hayami`."""
    tokens = _normalize_compact(value).split()
    return " ".join(sorted(tokens))


def _normalize_ascii_casing(name: str) -> str:
    """Convert all-uppercase ASCII names like `RIE TAKAHASHI` into `Rie Takahashi`."""
    cleaned = name.strip()
    if cleaned.isascii() and cleaned.isupper() and (" " in cleaned or len(cleaned) > 4):
        return cleaned.title()
    return cleaned


def extract_bilingual_english(text: str) -> str:
    """Extract the English/Romaji portion from bilingual strings like `オレンジミント (Orange Mint)`."""
    cleaned = text.strip()
    if not cleaned or cleaned.isascii():
        return cleaned

    # Check balanced trailing `(...)` when the prefix contains non-ASCII characters
    open_idx = -1
    depth = 0
    for idx in range(len(cleaned) - 1, -1, -1):
        ch = cleaned[idx]
        if ch in {")", "）"}:
            depth += 1
        elif ch in {"(", "uff08", "（"}:
            depth -= 1
            if depth == 0:
                open_idx = idx
                break

    if open_idx > 0 and cleaned[-1] in {")", "）"}:
        prefix = cleaned[:open_idx].strip()
        inner = cleaned[open_idx + 1 : -1].strip()
        inner_norm = inner.lower().strip(".")
        if (
            prefix
            and not prefix.isascii()
            and inner
            and inner.isascii()
            and any(c.isalpha() for c in inner)
            and inner_norm not in _PAREN_DISAMBIG_KEYWORDS
            and not inner_norm.isdigit()
        ):
            return inner

    match = _BILINGUAL_SUFFIX_RE.match(cleaned)
    if match is not None:
        latin = match.group("latin").strip()
        if latin.lower().strip(".") not in _PAREN_DISAMBIG_KEYWORDS and not latin.isdigit():
            return latin
    return cleaned


def normalize_primary_artist(artist: str) -> str:
    """Extract the single primary real singer from composite, collaborative, or character/CV credits.

    Examples:
    - `Rkomi con Elodie` -> `Rkomi`
    - `Yotsugi Ononoki(Saori Hayami)` -> `Saori Hayami`
    - `Ononoki Yotsugi (CV: Hayami Saori)` -> `Hayami Saori`
    - `EMILIA(CV:RIE TAKAHASHI)` -> `Rie Takahashi`
    - `斧乃木余接(CV.早見沙織) (Yotsugi Ononoki (CV. Saori Hayami))` -> `Saori Hayami`
    """
    cleaned = artist.strip()
    if not cleaned or cleaned == "Unknown Artist":
        return "Unknown Artist"

    # 1. Unwrap bilingual outer parentheses if present (e.g. Genius Japanese + English artist strings)
    cleaned = extract_bilingual_english(cleaned)

    # 2. Extract explicit Voice Actor (`CV:` / `CV.` / `VA:`) credit if present
    cv_match = _CV_SEGMENT_RE.search(cleaned)
    if cv_match is not None:
        cv_part = cv_match.group("cv").strip()
        if cv_part:
            cleaned = cv_part
    else:
        # 3. Extract parenthetical singer when formatted as `Character(Singer)` without `CV:`
        paren_match = _CHAR_PAREN_SINGER_RE.match(cleaned)
        if paren_match is not None:
            outer = paren_match.group("outer").strip()
            inner = paren_match.group("inner").strip()
            inner_clean = _CV_FORMAT_RE.sub("", inner).strip()
            inner_norm = inner_clean.lower().strip(".")
            if (
                inner_clean
                and inner_norm not in _PAREN_DISAMBIG_KEYWORDS
                and not inner_norm.isdigit()
                and len(inner_clean) >= 2
            ):
                cleaned = inner_clean
            else:
                cleaned = outer

    # 4. Split on collaboration joiners (`con`, `feat.`, `&`, `,`, etc.) and keep the primary singer
    collab_parts = _COLLAB_WORD_SPLIT_RE.split(cleaned)
    primary = collab_parts[0].strip() if collab_parts else cleaned
    symbol_parts = _MULTI_NAME_SPLIT_RE.split(primary)
    if symbol_parts and symbol_parts[0].strip():
        primary = symbol_parts[0].strip()

    return _normalize_ascii_casing(primary) or "Unknown Artist"


def extract_title_search_variants(title: str) -> list[str]:
    """Return candidate song titles, stripping `<Show> OP/ED` prefixes and bilingual suffixes."""
    cleaned = title.strip()
    if not cleaned or cleaned == "Unknown Title":
        return []

    variants: list[str] = []
    seen: set[str] = set()

    def _add(val: str) -> None:
        norm = val.strip()
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            variants.append(norm)

    bilingual = extract_bilingual_english(cleaned)
    op_ed_match = _ANIME_OP_ED_RE.match(bilingual)
    if op_ed_match is not None:
        song_only = op_ed_match.group("song").strip()
        if song_only:
            _add(song_only)

    track_match = _LEADING_TRACK_RE.match(cleaned)
    if track_match is not None:
        stripped_track = track_match.group(3).strip()
        if " - " in stripped_track:
            parts = stripped_track.split(" - ", 1)
            _add(parts[1])
        _add(stripped_track)
    elif " - " in cleaned:
        parts = cleaned.split(" - ", 1)
        _add(parts[1])

    _add(bilingual)
    _add(cleaned)
    return variants


def extract_artist_search_variants(artist: str) -> list[str]:
    """Extract candidate search names from composite anime/character credits and collaborations.

    Returns a deduplicated list ordered from normalized primary singer and voice actor (`CV`)
    variants to full credit and character names so MusicBrainz, Genius, Last.fm, and LRCLIB lookups succeed.
    """
    cleaned = artist.strip()
    if not cleaned or cleaned == "Unknown Artist":
        return []

    variants: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        norm = candidate.strip()
        if norm and len(norm) >= 2 and norm.lower() not in seen:
            seen.add(norm.lower())
            variants.append(norm)

    primary = normalize_primary_artist(cleaned)
    if primary and primary != "Unknown Artist":
        _add(primary)
        # If the primary singer is a 2-word ASCII name (e.g. `Hayami Saori`), also add `Saori Hayami`
        tokens = primary.split()
        if len(tokens) == 2 and primary.isascii():
            _add(f"{tokens[1]} {tokens[0]}")

    _add(cleaned)

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
                    cv_actors.append(_normalize_ascii_casing(sub_cv.strip()))
        if char_part:
            for sub_char in _MULTI_NAME_SPLIT_RE.split(char_part):
                if sub_char.strip():
                    characters.append(sub_char.strip())

    paren_match = _CHAR_PAREN_SINGER_RE.match(cleaned)
    if paren_match is not None and not cv_actors:
        outer = paren_match.group("outer").strip()
        inner = paren_match.group("inner").strip()
        if inner and inner.lower().strip(".") not in _PAREN_DISAMBIG_KEYWORDS:
            cv_actors.append(_normalize_ascii_casing(inner))
            if outer:
                characters.append(outer)

    for actor in cv_actors:
        _add(actor)
        actor_tokens = actor.split()
        if len(actor_tokens) == 2 and actor.isascii():
            _add(f"{actor_tokens[1]} {actor_tokens[0]}")
    for char_name in characters:
        _add(char_name)
        char_tokens = char_name.split()
        if len(char_tokens) == 2 and char_name.isascii():
            _add(f"{char_tokens[1]} {char_tokens[0]}")

    for part in _COLLAB_WORD_SPLIT_RE.split(cleaned):
        if part.strip() and part.strip() != cleaned:
            _add(part.strip())

    return variants


def _best_artist_similarity(source_artist: str, candidate_artist: str) -> float:
    """Compute artist similarity accounting for primary singer extraction, CV credits, and name order."""
    src_clean = source_artist.strip().lower()
    cand_clean = candidate_artist.strip().lower()
    if not src_clean or not cand_clean:
        return 0.0

    src_primary = normalize_primary_artist(source_artist)
    cand_primary = normalize_primary_artist(candidate_artist)

    direct_ratio = SequenceMatcher(None, src_clean, cand_clean).ratio()
    primary_ratio = SequenceMatcher(
        None,
        _normalize_compact(src_primary),
        _normalize_compact(cand_primary),
    ).ratio()
    sorted_primary_ratio = SequenceMatcher(
        None,
        _normalize_token_sorted(src_primary),
        _normalize_token_sorted(cand_primary),
    ).ratio()
    best = max(direct_ratio, primary_ratio, sorted_primary_ratio)

    src_variants = extract_artist_search_variants(source_artist)
    cand_variants = extract_artist_search_variants(candidate_artist)
    if len(src_variants) > 1 or len(cand_variants) > 1:
        for s_var in src_variants:
            for c_var in cand_variants:
                var_ratio = max(
                    SequenceMatcher(
                        None,
                        _normalize_compact(s_var),
                        _normalize_compact(c_var),
                    ).ratio(),
                    SequenceMatcher(
                        None,
                        _normalize_token_sorted(s_var),
                        _normalize_token_sorted(c_var),
                    ).ratio(),
                )
                best = max(best, var_ratio)

    return min(best, 1.0)


def _best_title_similarity(source_title: str, candidate_title: str) -> float:
    """Compute title similarity accounting for `<Anime> OP/ED <Title>` and bilingual titles."""
    src_variants = extract_title_search_variants(source_title) or [source_title]
    cand_variants = extract_title_search_variants(candidate_title) or [candidate_title]

    best = 0.0
    for s_var in src_variants:
        for c_var in cand_variants:
            s_norm = _normalize_compact(s_var)
            c_norm = _normalize_compact(c_var)
            if not s_norm or not c_norm:
                continue
            ratio = SequenceMatcher(None, s_norm, c_norm).ratio()
            best = max(best, ratio)
    return min(best, 1.0)


def _best_album_similarity(source_album: str, candidate_album: str) -> float:
    """Compute album similarity, rewarding short titles contained in long promotional titles."""
    src_clean = extract_bilingual_english(source_album).strip().lower()
    cand_clean = extract_bilingual_english(candidate_album).strip().lower()
    if not src_clean or not cand_clean:
        return 0.0

    direct_ratio = SequenceMatcher(None, src_clean, cand_clean).ratio()
    shorter, longer = (
        (src_clean, cand_clean) if len(src_clean) <= len(cand_clean) else (cand_clean, src_clean)
    )
    if len(shorter) >= 3 and shorter in longer:
        return max(direct_ratio, 0.90)

    src_no_year = _YEAR_SUFFIX_RE.sub("", src_clean).strip()
    cand_no_year = _YEAR_SUFFIX_RE.sub("", cand_clean).strip()
    if src_no_year and cand_no_year:
        no_year_ratio = SequenceMatcher(None, src_no_year, cand_no_year).ratio()
        shorter_ny, longer_ny = (
            (src_no_year, cand_no_year)
            if len(src_no_year) <= len(cand_no_year)
            else (cand_no_year, src_no_year)
        )
        if len(shorter_ny) >= 3 and shorter_ny in longer_ny:
            return max(direct_ratio, no_year_ratio, 0.90)
        direct_ratio = max(direct_ratio, no_year_ratio)

    return direct_ratio


def compute_metadata_similarity(source: TrackMetadata, candidate: TrackMetadata) -> float:
    """Calculate a normalized similarity confidence score in [0.0, 1.0] between two metadata sets."""
    title_ratio = _best_title_similarity(source.title, candidate.title)
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
        raw_artist = parts[0].strip() or "Unknown Artist"
        raw_title = parts[1].strip() or remainder
    else:
        raw_artist = "Unknown Artist"
        raw_title = remainder or cleaned

    artist = normalize_primary_artist(raw_artist) if raw_artist != "Unknown Artist" else "Unknown Artist"
    title_variants = extract_title_search_variants(raw_title)
    title = title_variants[0] if title_variants else raw_title

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
                kv_updates["artist"] = normalize_primary_artist(val)
            elif key == "title":
                kv_updates["title"] = val
            elif key == "album":
                kv_updates["album"] = val
            elif key == "albumartist":
                kv_updates["album_artist"] = normalize_primary_artist(val)
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

    if (
        resolved_artist == "Unknown Artist"
        and base_metadata is not None
        and base_metadata.title not in {"", "Unknown Title"}
        and resolved_title.strip().lower() != base_metadata.title.strip().lower()
    ):
        resolved_artist = normalize_primary_artist(resolved_title.strip())
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
) -> tuple[int, int, bool, str | None]:
    """Extract `(track_num, disc_num, has_explicit_track, track_title)` from a `medium-list`."""
    track_num = default_track or 1
    disc_num = default_disc or 1
    has_explicit_track = False
    track_title: str | None = None
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
                    raw_trk_title = str(first_track.get("title") or "").strip()
                    if raw_trk_title:
                        track_title = raw_trk_title
    return track_num, disc_num, has_explicit_track, track_title


def _flip_sort_name(sort_name: str) -> str:
    """Convert `'Last, First'` sort-names into `'First Last'` display order."""
    if "," in sort_name:
        parts = [p.strip() for p in sort_name.split(",", maxsplit=1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[1]} {parts[0]}"
    return sort_name.strip()


def _is_character_credit_item(item: dict[str, object]) -> bool:
    """Return True when a MusicBrainz artist-credit dict represents a fictional `Character`."""
    artist_obj = item.get("artist")
    if not isinstance(artist_obj, dict):
        return False
    return str(artist_obj.get("type") or "").strip().lower() == "character"


def _select_primary_artist_item_from_credit(artist_credit: list[object]) -> dict[str, object] | None:
    """Select the single primary real singer dict from a MusicBrainz `artist-credit` list.

    - Discards `type == "Character"` entries when a real person/group artist is present.
    - When formatted as `[Character, "(" or "(CV:", RealSinger, ")"]`, selects `RealSinger`.
    - For standard multi-artist collaborations (`[Artist1, " con ", Artist2]`), selects `Artist1`.
    """
    indexed_dicts: list[tuple[int, dict[str, object]]] = [
        (idx, item)
        for idx, item in enumerate(artist_credit)
        if isinstance(item, dict) and (isinstance(item.get("artist"), dict) or item.get("name"))
    ]
    if not indexed_dicts:
        return None
    if len(indexed_dicts) == 1:
        return indexed_dicts[0][1]

    # 1. Check if MusicBrainz explicitly marks any artist as `type == "Character"`
    non_character = [item for _, item in indexed_dicts if not _is_character_credit_item(item)]
    if non_character and len(non_character) < len(indexed_dicts):
        return non_character[0]

    # 2. Check if the second artist is enclosed in `(` or `(CV:` parentheses (character + voice actor)
    first_idx, first_item = indexed_dicts[0]
    second_idx, second_item = indexed_dicts[1]
    first_join = str(first_item.get("joinphrase") or "")
    between_tokens = [str(tok) for tok in artist_credit[first_idx + 1 : second_idx] if isinstance(tok, str)]
    combined_sep = first_join + "".join(between_tokens)
    if "(" in combined_sep or "（" in combined_sep or "cv" in combined_sep.lower():
        return second_item

    # 3. Standard collaboration (e.g. `Rkomi con Elodie`): return the first (primary) artist
    return first_item


def _resolve_artist_dict_name(artist_item: dict[str, object], prefer_english: bool = True) -> str:
    """Resolve the display name for a single MusicBrainz artist-credit entry."""
    artist_obj = artist_item.get("artist")
    raw_name = str(
        artist_item.get("name") or (artist_obj.get("name") if isinstance(artist_obj, dict) else "") or ""
    ).strip()

    if not prefer_english:
        return normalize_primary_artist(raw_name)

    # Never overwrite an already-ASCII stage name (like `Rkomi` or `ClariS`) with a legal-name alias!
    if raw_name and raw_name.isascii():
        return normalize_primary_artist(raw_name)

    if isinstance(artist_obj, dict):
        alias_list = artist_obj.get("alias-list")
        if isinstance(alias_list, list):
            # Pass 1: Primary English artist-name alias (excluding Legal name)
            for alias in alias_list:
                if isinstance(alias, dict):
                    alias_type = str(alias.get("type") or "").lower()
                    if alias_type == "legal name":
                        continue
                    alias_text = str(alias.get("alias", "")).strip()
                    locale = str(alias.get("locale", "")).lower()
                    is_primary = str(alias.get("primary", "")).lower() == "primary"
                    if alias_text and locale.startswith("en") and is_primary:
                        return normalize_primary_artist(alias_text)

        sort_name = str(artist_obj.get("sort-name", "")).strip()
        if sort_name and sort_name.isascii() and "," in sort_name:
            return normalize_primary_artist(_flip_sort_name(sort_name))

        if isinstance(alias_list, list):
            # Pass 2: Any ASCII/English non-legal alias
            for alias in alias_list:
                if isinstance(alias, dict):
                    alias_type = str(alias.get("type") or "").lower()
                    if alias_type == "legal name":
                        continue
                    alias_text = str(alias.get("alias", "")).strip()
                    locale = str(alias.get("locale", "")).lower()
                    if alias_text and (locale.startswith("en") or alias_text.isascii()):
                        return normalize_primary_artist(alias_text)

        if sort_name and sort_name.isascii():
            return normalize_primary_artist(_flip_sort_name(sort_name))

    return normalize_primary_artist(raw_name)


def _extract_english_artist_credit(artist_credit: object) -> str | None:
    """Extract the canonical English/Romaji primary singer name from MusicBrainz `artist-credit`."""
    if not isinstance(artist_credit, list) or not artist_credit:
        return None

    primary_item = _select_primary_artist_item_from_credit(artist_credit)
    if primary_item is None:
        return None

    resolved = _resolve_artist_dict_name(primary_item, prefer_english=True)
    if not resolved or resolved == "Unknown Artist":
        return None
    return resolved


def _extract_primary_artist_from_credit(
    artist_credit: object,
    fallback_phrase: str,
    *,
    prefer_english: bool = True,
) -> str:
    """Extract the single primary singer from `artist_credit` or `fallback_phrase`."""
    if isinstance(artist_credit, list) and artist_credit:
        primary_item = _select_primary_artist_item_from_credit(artist_credit)
        if primary_item is not None:
            resolved = _resolve_artist_dict_name(primary_item, prefer_english=prefer_english)
            if resolved and resolved != "Unknown Artist":
                return resolved
    return normalize_primary_artist(fallback_phrase)


def _find_english_track_title_in_releases(
    rec: dict[str, object],
    releases: list[dict[str, object]],
) -> str | None:
    """Search recording aliases and release `medium-list` entries for an English/Romaji track title."""
    alias_list = rec.get("alias-list")
    if isinstance(alias_list, list):
        for alias in alias_list:
            if isinstance(alias, dict):
                alias_text = str(alias.get("alias") or "").strip()
                if alias_text and alias_text.isascii():
                    return alias_text

    for rel in releases:
        _, _, _, rel_trk_title = _extract_track_and_disc_from_release(rel, None, None)
        if rel_trk_title and rel_trk_title.isascii():
            return rel_trk_title
        bilingual_trk = extract_bilingual_english(rel_trk_title or "")
        if bilingual_trk and bilingual_trk.isascii():
            return bilingual_trk
    return None


class MusicBrainzMetadataService:
    """Reads local audio tags via `mutagen` and queries MusicBrainz, Genius, and Last.fm."""

    def __init__(
        self,
        app_name: str = "MusicIngestBot",
        app_version: str = "0.1.0",
        contact: str = "admin@example.com",
        *,
        genius_access_token: str = "",
        lastfm_api_key: str = "",
        enable_network: bool = False,
        search_fn: MusicBrainzSearchFn | None = None,
        release_lookup_fn: ReleaseLookupFn | None = None,
        http_transport: httpx.BaseTransport | None = None,
        throttler: ApiThrottler | None = None,
    ) -> None:
        self._app_name = app_name
        self._app_version = app_version
        self._contact = contact
        self._genius_access_token = genius_access_token.strip()
        self._lastfm_api_key = lastfm_api_key.strip()
        self._enable_network = enable_network
        self._search_fn = search_fn
        self._release_lookup_fn = release_lookup_fn
        self._http_transport = http_transport
        self._throttler = throttler or default_api_throttler
        musicbrainzngs.set_useragent(self._app_name, self._app_version, self._contact)

    async def read_local_metadata(self, audio_path: Path) -> TrackMetadata:
        """Derive baseline metadata from embedded `mutagen` tags and filename stem."""
        fallback = parse_filename_metadata(audio_path.stem)
        return await asyncio.to_thread(_read_mutagen_metadata_sync, audio_path, fallback)

    async def fetch_release_tracks(
        self,
        release_mbid: str,
        base_metadata: TrackMetadata | None = None,
        *,
        prefer_english: bool = True,
    ) -> list[TrackMetadata]:
        """Fetch the full ordered `medium-list` tracklist for a MusicBrainz release UUID."""
        clean_mbid = release_mbid.strip()
        if not clean_mbid or clean_mbid.startswith("release-group:"):
            return []
        if not (self._enable_network or self._release_lookup_fn is not None):
            return []
        if self._enable_network:
            await self._throttler.throttle("musicbrainz")
        return await asyncio.to_thread(
            self._fetch_release_tracks_sync,
            clean_mbid,
            base_metadata,
            prefer_english,
        )

    def _fetch_release_tracks_sync(
        self,
        release_mbid: str,
        base_metadata: TrackMetadata | None = None,
        prefer_english: bool = True,
    ) -> list[TrackMetadata]:
        """Synchronously query `get_release_by_id` and map its `medium-list` into `TrackMetadata` items."""
        try:
            if self._release_lookup_fn is not None:
                rel_resp = self._release_lookup_fn(release_mbid)
            else:
                rel_resp = musicbrainzngs.get_release_by_id(
                    release_mbid,
                    includes=["artists", "recordings", "release-groups", "media"],
                )
        except Exception as exc:
            logger.debug("MusicBrainz fetch_release_tracks(%s) failed: %s", release_mbid, exc)
            return []

        rel_obj = (
            rel_resp.get("release")
            if isinstance(rel_resp, dict) and isinstance(rel_resp.get("release"), dict)
            else (rel_resp if isinstance(rel_resp, dict) else None)
        )
        if not isinstance(rel_obj, dict):
            return []

        rel_title = extract_bilingual_english(
            str(
                rel_obj.get("title")
                or (base_metadata.album if base_metadata is not None else "Unknown Album")
            ).strip()
        )
        rel_year = _parse_year(str(rel_obj.get("date") or "")) or (
            base_metadata.year if base_metadata is not None else None
        )
        rel_artist_credit = rel_obj.get("artist-credit")
        rel_artist_phrase = str(
            rel_obj.get("artist-credit-phrase")
            or (base_metadata.artist if base_metadata is not None else "Unknown Artist")
        ).strip()
        effective_rel_artist = _extract_primary_artist_from_credit(
            rel_artist_credit,
            rel_artist_phrase,
            prefer_english=prefer_english,
        )
        album_artist = (
            normalize_primary_artist(base_metadata.effective_album_artist)
            if (base_metadata is not None and base_metadata.album_artist)
            else effective_rel_artist
        )

        tracks: list[TrackMetadata] = []
        medium_list = rel_obj.get("medium-list")
        if not isinstance(medium_list, list):
            return tracks

        for medium in medium_list:
            if not isinstance(medium, dict):
                continue
            disc_num = _parse_int_prefix(str(medium.get("position") or "1")) or 1
            track_list = medium.get("track-list")
            if not isinstance(track_list, list):
                continue
            for idx, trk in enumerate(track_list, start=1):
                if not isinstance(trk, dict):
                    continue
                rec_inner = trk.get("recording")
                rec_dict = rec_inner if isinstance(rec_inner, dict) else {}
                trk_title = extract_bilingual_english(
                    str(trk.get("title") or rec_dict.get("title") or "").strip()
                )
                if not trk_title:
                    continue
                trk_num = _parse_int_prefix(str(trk.get("position") or trk.get("number") or idx)) or idx
                trk_length = trk.get("length") or rec_dict.get("length")
                duration_sec = (
                    int(str(trk_length)) // 1000
                    if trk_length is not None and str(trk_length).isdigit()
                    else None
                )
                rec_id = str(rec_dict.get("id") or trk.get("id") or "").strip() or None
                explicit_trk_credit = trk.get("artist-credit") or rec_dict.get("artist-credit")
                explicit_trk_phrase = trk.get("artist-credit-phrase") or rec_dict.get("artist-credit-phrase")
                trk_artist_credit = (
                    explicit_trk_credit
                    if explicit_trk_credit is not None
                    else (None if explicit_trk_phrase else rel_artist_credit)
                )
                trk_artist_phrase = str(explicit_trk_phrase or rel_artist_phrase).strip()
                chosen_artist = _extract_primary_artist_from_credit(
                    trk_artist_credit,
                    trk_artist_phrase,
                    prefer_english=prefer_english,
                )

                tracks.append(
                    TrackMetadata(
                        title=trk_title,
                        artist=chosen_artist,
                        album=rel_title,
                        album_artist=album_artist,
                        year=rel_year,
                        track_number=trk_num,
                        disc_number=disc_num,
                        genre=base_metadata.genre if base_metadata is not None else None,
                        duration_seconds=duration_sec,
                        recording_mbid=rec_id,
                        release_mbid=release_mbid,
                    )
                )

        return tracks

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
        if self._enable_network or self._search_fn is not None or self._http_transport is not None:
            if self._enable_network:
                await self._throttler.throttle("musicbrainz")
                if self._genius_access_token:
                    await self._throttler.throttle("genius")
                if self._lastfm_api_key:
                    await self._throttler.throttle("lastfm")
            candidates = await asyncio.to_thread(
                self._search_all_providers_sync,
                local_metadata,
                fingerprint,
            )
            if candidates:
                return candidates

        return [self._build_fallback_candidate(local_metadata, fingerprint)]

    def _build_fallback_candidate(
        self,
        local_metadata: TrackMetadata,
        fingerprint: AudioFingerprint | None,
    ) -> MetadataCandidate:
        """Construct a baseline candidate directly from local tags when external providers return no match."""
        norm_artist = normalize_primary_artist(local_metadata.artist)
        title_variants = extract_title_search_variants(local_metadata.title)
        norm_title = title_variants[0] if title_variants else local_metadata.title
        norm_album = extract_bilingual_english(local_metadata.album)
        baseline_confidence = 0.95 if norm_artist != "Unknown Artist" else 0.50
        enriched = TrackMetadata(
            title=norm_title,
            artist=norm_artist,
            album=norm_album,
            album_artist=normalize_primary_artist(local_metadata.effective_album_artist),
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

    def _query_genius_enrichment_sync(
        self,
        local_metadata: TrackMetadata,
        extra_titles: list[str] | None = None,
    ) -> list[TrackMetadata]:
        """Query Genius API for English/Romaji song, artist, and album info."""
        if not (self._enable_network or self._http_transport is not None):
            return []

        title_variants = list(
            dict.fromkeys([*(extra_titles or []), *extract_title_search_variants(local_metadata.title)])
        )
        artist_variants = extract_artist_search_variants(local_metadata.artist)
        queries: list[str] = []
        for t_var in title_variants[:2]:
            if artist_variants:
                queries.append(f"{artist_variants[0]} {t_var}")
            queries.append(t_var)

        results: list[TrackMetadata] = []
        headers: dict[str, str] = {}
        if self._genius_access_token:
            headers["Authorization"] = f"Bearer {self._genius_access_token}"

        try:
            with httpx.Client(
                transport=self._http_transport,
                timeout=8.0,
                follow_redirects=True,
                headers=headers,
            ) as client:
                for q in dict.fromkeys(queries):
                    if not q.strip():
                        continue
                    search_url = (
                        "https://api.genius.com/search"
                        if self._genius_access_token
                        else "https://genius.com/api/search/song"
                    )
                    resp = client.get(search_url, params={"q": q})
                    if resp.status_code != 200:
                        continue
                    payload = resp.json()
                    if not isinstance(payload, dict):
                        continue
                    response_obj = payload.get("response")
                    if not isinstance(response_obj, dict):
                        continue

                    raw_hits: list[dict[str, object]] = []
                    if isinstance(response_obj.get("hits"), list):
                        raw_hits.extend(h for h in response_obj["hits"] if isinstance(h, dict))
                    elif isinstance(response_obj.get("sections"), list):
                        for sec in response_obj["sections"]:
                            if isinstance(sec, dict) and isinstance(sec.get("hits"), list):
                                raw_hits.extend(h for h in sec["hits"] if isinstance(h, dict))

                    for hit in raw_hits[:4]:
                        song_obj = hit.get("result")
                        if not isinstance(song_obj, dict):
                            continue
                        primary_artist_obj = song_obj.get("primary_artist")
                        raw_artist = str(
                            primary_artist_obj.get("name") if isinstance(primary_artist_obj, dict) else ""
                        ).strip()
                        raw_title = str(song_obj.get("title") or "").strip()
                        if not raw_title or not raw_artist:
                            continue
                        if raw_artist.lower().startswith("genius "):
                            continue

                        clean_title = extract_bilingual_english(raw_title)
                        clean_artist = normalize_primary_artist(raw_artist)
                        release_year: int | None = local_metadata.year
                        rel_date = song_obj.get("release_date_components")
                        if isinstance(rel_date, dict) and rel_date.get("year"):
                            release_year = _parse_int_prefix(str(rel_date["year"])) or release_year

                        album_name = local_metadata.album
                        song_id = song_obj.get("id")
                        if song_id is not None and not results:
                            song_url = (
                                f"https://api.genius.com/songs/{song_id}"
                                if self._genius_access_token
                                else f"https://genius.com/api/songs/{song_id}"
                            )
                            try:
                                song_resp = client.get(song_url)
                                if song_resp.status_code == 200:
                                    song_data = (song_resp.json().get("response") or {}).get("song") or {}
                                    if isinstance(song_data, dict):
                                        album_obj = song_data.get("album")
                                        if isinstance(album_obj, dict) and album_obj.get("name"):
                                            album_name = extract_bilingual_english(
                                                str(album_obj["name"]).strip()
                                            )
                                        rel_comp = song_data.get("release_date_components")
                                        if isinstance(rel_comp, dict) and rel_comp.get("year"):
                                            release_year = (
                                                _parse_int_prefix(str(rel_comp["year"])) or release_year
                                            )
                            except Exception:
                                pass

                        results.append(
                            TrackMetadata(
                                title=clean_title,
                                artist=clean_artist,
                                album=(
                                    album_name
                                    if album_name and album_name != "Unknown Album"
                                    else clean_title
                                ),
                                album_artist=clean_artist,
                                year=release_year,
                                track_number=local_metadata.track_number or 1,
                                disc_number=local_metadata.disc_number or 1,
                                genre=local_metadata.genre,
                                duration_seconds=local_metadata.duration_seconds,
                            )
                        )
                    if results:
                        break
        except Exception as exc:
            logger.debug("Genius metadata lookup failed for %s: %s", local_metadata.title, exc)

        return results

    def _query_lastfm_enrichment_sync(
        self,
        local_metadata: TrackMetadata,
    ) -> list[TrackMetadata]:
        """Query Last.fm API (`track.search` / `track.getInfo`) when `lastfm_api_key` is configured."""
        if not self._lastfm_api_key or not (self._enable_network or self._http_transport is not None):
            return []

        title_variants = extract_title_search_variants(local_metadata.title)
        clean_title = title_variants[0] if title_variants else local_metadata.title
        clean_artist = normalize_primary_artist(local_metadata.artist)
        if not clean_title or clean_title == "Unknown Title":
            return []

        results: list[TrackMetadata] = []
        try:
            with httpx.Client(
                transport=self._http_transport,
                timeout=8.0,
            ) as client:
                params: dict[str, str | int] = {
                    "method": "track.search",
                    "track": clean_title,
                    "api_key": self._lastfm_api_key,
                    "format": "json",
                    "limit": 3,
                }
                if clean_artist and clean_artist != "Unknown Artist":
                    params["artist"] = clean_artist
                resp = client.get("https://ws.audioscrobbler.com/2.0/", params=params)
                if resp.status_code != 200:
                    return []
                data = resp.json()
                matches = (
                    ((data.get("results") or {}).get("trackmatches") or {}).get("track")
                    if isinstance(data, dict)
                    else None
                )
                if not isinstance(matches, list):
                    return []

                for item in matches[:3]:
                    if not isinstance(item, dict):
                        continue
                    t_name = extract_bilingual_english(str(item.get("name") or "").strip())
                    a_name = normalize_primary_artist(str(item.get("artist") or "").strip())
                    if not t_name or not a_name:
                        continue
                    results.append(
                        TrackMetadata(
                            title=t_name,
                            artist=a_name,
                            album=(
                                local_metadata.album if local_metadata.album != "Unknown Album" else t_name
                            ),
                            album_artist=a_name,
                            year=local_metadata.year,
                            track_number=local_metadata.track_number or 1,
                            disc_number=local_metadata.disc_number or 1,
                            genre=local_metadata.genre,
                            duration_seconds=local_metadata.duration_seconds,
                        )
                    )
        except Exception as exc:
            logger.debug("Last.fm metadata lookup failed for %s: %s", local_metadata.title, exc)

        return results

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
                        score = _best_title_similarity(local_metadata.title, trk_title)
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
                                        "track-list": [{"number": str(trk_num), "title": trk_title}],
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
        for variant in artist_variants[:4]:
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
        if not self._enable_network:
            return []

        recordings: list[dict[str, object]] = []
        title_variants = extract_title_search_variants(local_metadata.title)
        primary_title = title_variants[0] if title_variants else local_metadata.title.strip()

        # 1. Direct Release MBID or Release-Group MBID lookup
        rel_mbid = local_metadata.release_mbid
        if rel_mbid:
            if rel_mbid.startswith("release-group:"):
                rgid = rel_mbid.split(":", maxsplit=1)[1]
                title_clause = (
                    f' AND recording:"{_escape_lucene(primary_title)}"'
                    if primary_title not in {"", "Unknown Title"}
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
                    f' AND recording:"{_escape_lucene(primary_title)}"'
                    if primary_title not in {"", "Unknown Title"}
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

        # 3. ISRC lookup
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

        # 4. Title + Artist (including primary singer, CV decomposition, and English alias -> arid lookup)
        if not recordings and primary_title and primary_title != "Unknown Title":
            artist_variants = extract_artist_search_variants(local_metadata.artist)
            if not artist_variants:
                query_kwargs: dict[str, object] = {"recording": primary_title, "limit": 5}
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
                for t_var in title_variants[:2]:
                    for variant in artist_variants[:3]:
                        try:
                            search_resp = musicbrainzngs.search_recordings(
                                recording=t_var,
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
                    if recordings:
                        break

                if not recordings:
                    arids = self._resolve_artist_mbids(artist_variants)
                    if arids:
                        arid_clause = " OR ".join(f"arid:{aid}" for aid in arids)
                        for t_var in title_variants[:2]:
                            lucene_query = f'recording:"{_escape_lucene(t_var)}" AND ({arid_clause})'
                            try:
                                arid_resp = musicbrainzngs.search_recordings(query=lucene_query, limit=5)
                                arid_list = (
                                    arid_resp.get("recording-list") if isinstance(arid_resp, dict) else None
                                )
                                if isinstance(arid_list, list) and arid_list:
                                    recordings.extend(r for r in arid_list if isinstance(r, dict))
                                    break
                            except Exception as exc:
                                logger.warning("MusicBrainz arid search_recordings failed: %s", exc)

                if not recordings:
                    escaped_artist = _escape_lucene(artist_variants[0])
                    escaped_title = _escape_lucene(primary_title)
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

    def _search_all_providers_sync(
        self,
        local_metadata: TrackMetadata,
        fingerprint: AudioFingerprint | None,
    ) -> list[MetadataCandidate]:
        """Query MusicBrainz, enrich non-ASCII or missing fields via Genius/Last.fm, and rank candidates."""
        mb_candidates = self._search_musicbrainz_candidates_sync(local_metadata, fingerprint)

        # Query Genius/Last.fm if MB returned no candidates, low confidence, or non-ASCII titles
        needs_enrichment = not mb_candidates or mb_candidates[0].confidence < 0.90
        if mb_candidates:
            top_meta = mb_candidates[0].metadata
            if not (top_meta.title.isascii() and top_meta.artist.isascii() and top_meta.album.isascii()):
                needs_enrichment = True

        extra_titles = [c.metadata.title for c in mb_candidates[:2]] if mb_candidates else []
        external_metas: list[TrackMetadata] = []
        if needs_enrichment:
            external_metas.extend(self._query_genius_enrichment_sync(local_metadata, extra_titles))
            external_metas.extend(self._query_lastfm_enrichment_sync(local_metadata))

        if mb_candidates and external_metas:
            best_ext = external_metas[0]
            enriched_mb: list[MetadataCandidate] = []
            for cand in mb_candidates:
                m = cand.metadata
                new_title = best_ext.title if not m.title.isascii() and best_ext.title.isascii() else m.title
                new_artist = (
                    best_ext.artist if not m.artist.isascii() and best_ext.artist.isascii() else m.artist
                )
                new_album = m.album
                if not m.album.isascii():
                    if m.album == m.title and new_title.isascii():
                        new_album = new_title
                    elif best_ext.album.isascii() and best_ext.album != "Unknown Album":
                        new_album = best_ext.album

                updated_meta = replace(
                    m,
                    title=new_title,
                    artist=new_artist,
                    album=new_album,
                    album_artist=new_artist,
                )
                sim = compute_metadata_similarity(
                    TrackMetadata(
                        title=local_metadata.title,
                        artist=local_metadata.artist,
                        album=(
                            new_album
                            if local_metadata.album in {"", "Unknown Album"}
                            else local_metadata.album
                        ),
                    ),
                    updated_meta,
                )
                new_conf = round(max(cand.confidence, min((0.65 * sim) + 0.35, 1.0)), 4)
                enriched_mb.append(
                    MetadataCandidate(
                        metadata=updated_meta,
                        confidence=new_conf,
                        release_type=cand.release_type,
                        disambiguation=cand.disambiguation,
                    )
                )
            enriched_mb.sort(key=lambda item: item.confidence, reverse=True)
            mb_candidates = enriched_mb

        if not mb_candidates and external_metas:
            for ext_meta in external_metas[:3]:
                sim = compute_metadata_similarity(
                    TrackMetadata(
                        title=local_metadata.title,
                        artist=local_metadata.artist,
                        album=(
                            ext_meta.album
                            if local_metadata.album in {"", "Unknown Album"}
                            else local_metadata.album
                        ),
                    ),
                    ext_meta,
                )
                conf = round(min(max((0.70 * sim) + 0.30, 0.50), 1.0), 4)
                mb_candidates.append(MetadataCandidate(metadata=ext_meta, confidence=conf))
            mb_candidates.sort(key=lambda item: item.confidence, reverse=True)

        deduped: list[MetadataCandidate] = []
        seen_final: set[tuple[str, str, str, int | None]] = set()
        for cand in mb_candidates:
            key = (
                _normalize_token_sorted(cand.metadata.artist),
                _normalize_compact(cand.metadata.title),
                _normalize_compact(cand.metadata.album),
                cand.metadata.year,
            )
            if key in seen_final:
                continue
            seen_final.add(key)
            deduped.append(cand)

        return deduped[:5]

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
        seen_keys: set[tuple[str, str, str, int | None]] = set()
        local_title_variants = extract_title_search_variants(local_metadata.title)
        local_ascii_title = next((t for t in local_title_variants if t.isascii()), None)

        for rec in recordings:
            rec_id = str(rec.get("id", "")).strip() or local_metadata.recording_mbid
            raw_rec_title = str(rec.get("title") or local_metadata.title).strip()
            rec_title = extract_bilingual_english(raw_rec_title)
            raw_rec_artist = str(rec.get("artist-credit-phrase") or local_metadata.artist).strip()

            primary_en_artist = _extract_english_artist_credit(rec.get("artist-credit"))
            primary_native_artist = _extract_primary_artist_from_credit(
                rec.get("artist-credit"),
                raw_rec_artist,
                prefer_english=False,
            )
            canonical_artist = primary_en_artist or normalize_primary_artist(raw_rec_artist)

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

            # Resolve English/Romaji track title across all releases in `release-list` if `rec_title` is CJK
            en_track_title = _find_english_track_title_in_releases(rec, releases)
            if not rec_title.isascii():
                if en_track_title:
                    rec_title = en_track_title
                elif local_ascii_title and local_ascii_title != "Unknown Title":
                    rec_title = local_ascii_title

            per_recording_candidates: list[MetadataCandidate] = []
            for rel in releases:
                rel_id = str(rel.get("id", "")).strip() or local_metadata.release_mbid
                raw_rel_title = str(rel.get("title") or local_metadata.album).strip()
                rel_title = extract_bilingual_english(raw_rel_title)
                if not rel_title.isascii() and raw_rel_title == raw_rec_title and rec_title.isascii():
                    rel_title = rec_title

                rel_year = _parse_year(str(rel.get("date", ""))) or local_metadata.year
                track_num, disc_num, has_explicit_track, rel_trk_title = _extract_track_and_disc_from_release(
                    rel,
                    local_metadata.track_number,
                    local_metadata.disc_number,
                )
                effective_title = (
                    extract_bilingual_english(rel_trk_title)
                    if rel_trk_title and rel_trk_title.isascii()
                    else rec_title
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
                if rel_title.isascii():
                    release_bonus += 0.01
                if (
                    local_metadata.release_mbid
                    and rel_id
                    and rel_id.lower() == local_metadata.release_mbid.lower()
                ):
                    release_bonus += 0.12

                artist_choices: list[str] = [canonical_artist]
                if (
                    primary_native_artist
                    and primary_native_artist != "Unknown Artist"
                    and primary_native_artist.lower() != canonical_artist.lower()
                ):
                    artist_choices.append(primary_native_artist)

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
                        TrackMetadata(title=effective_title, artist=art_opt, album=rel_title),
                    )
                    for art_opt in artist_choices
                )
                base_confidence = (0.65 * best_sim) + (0.35 * mb_score) + release_bonus

                for idx, art_name in enumerate(artist_choices):
                    script_delta = -0.02 * idx
                    combined_confidence = round(
                        min(max(base_confidence + script_delta, 0.0), 1.0),
                        4,
                    )
                    cand_meta = TrackMetadata(
                        title=effective_title,
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
                # Deduplicate identical `(artist, title, album, year)` editions so multiple pressings
                # of the same release do not create duplicate 100% candidates
                dedup_key = (
                    _normalize_compact(cand.metadata.artist),
                    _normalize_compact(cand.metadata.title),
                    _normalize_compact(cand.metadata.album),
                    cand.metadata.year,
                )
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                candidates.append(cand)

        candidates.sort(key=lambda item: item.confidence, reverse=True)
        return candidates[:5]
