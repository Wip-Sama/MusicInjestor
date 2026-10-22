"""Domain utilities for generating external streaming platform links and resolving user priority."""

from urllib.parse import quote_plus

SUPPORTED_PLATFORMS: tuple[str, ...] = ("deezer", "tidal", "qobuz", "spotify", "apple_music")

PLATFORM_LABELS: dict[str, str] = {
    "deezer": "Deezer",
    "tidal": "Tidal",
    "qobuz": "Qobuz",
    "spotify": "Spotify",
    "apple_music": "Apple Music",
}

PLATFORM_ICONS: dict[str, str] = {
    "deezer": "🌐",
    "tidal": "🌊",
    "qobuz": "🎼",
    "spotify": "🟢",
    "apple_music": "🍎",
}

PLATFORM_NUMBER_MAP: dict[str, str] = {
    "1": "deezer",
    "2": "tidal",
    "3": "qobuz",
    "4": "spotify",
    "5": "apple_music",
}


def build_streaming_platform_links(
    artist: str,
    title: str,
    deezer_url: str | None = None,
) -> dict[str, str]:
    """Generate canonical links for all supported streaming services."""
    clean_artist = artist.strip()
    clean_title = title.strip()
    query = f"{clean_artist} {clean_title}".strip()
    encoded = quote_plus(query)

    links: dict[str, str] = {}

    if deezer_url and deezer_url.strip().startswith("http"):
        links["deezer"] = deezer_url.strip()
    else:
        links["deezer"] = f"https://www.deezer.com/search/{encoded}"

    links["tidal"] = f"https://listen.tidal.com/search?q={encoded}"
    links["qobuz"] = f"https://open.qobuz.com/search?q={encoded}"
    links["spotify"] = f"https://open.spotify.com/search/{encoded}"
    links["apple_music"] = f"https://music.apple.com/search?term={encoded}"

    return links


def resolve_primary_platform_link(
    links: dict[str, str],
    priority_order: list[str],
) -> tuple[str, str]:
    """Return `(platform_key, url)` for the highest-priority platform available."""
    for plat in priority_order:
        clean = plat.strip().lower()
        if clean in links and bool(links[clean]):
            return clean, links[clean]

    if "deezer" in links and links["deezer"]:
        return "deezer", links["deezer"]

    if links:
        first_key = next(iter(links))
        return first_key, links[first_key]

    return "deezer", ""


def parse_platform_order_input(text: str) -> list[str]:
    """Parse space-separated numeric (1-5) or named platform string into validated priority order."""
    tokens = text.strip().split()
    chosen: list[str] = []
    seen: set[str] = set()

    for token in tokens:
        clean = token.strip().lower()
        plat = PLATFORM_NUMBER_MAP.get(clean, clean)
        if plat in SUPPORTED_PLATFORMS and plat not in seen:
            seen.add(plat)
            chosen.append(plat)

    # Append any omitted platforms in default relative order
    for fallback in SUPPORTED_PLATFORMS:
        if fallback not in seen:
            seen.add(fallback)
            chosen.append(fallback)

    return chosen
