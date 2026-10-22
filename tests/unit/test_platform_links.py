"""Unit tests for platform_links domain logic and priority resolution."""

from src.core.models import UserPreferences
from src.core.platform_links import (
    SUPPORTED_PLATFORMS,
    build_streaming_platform_links,
    parse_platform_order_input,
    resolve_primary_platform_link,
)


def test_build_streaming_platform_links() -> None:
    """Test generating streaming platform links with custom Deezer URL and query fallbacks."""
    links = build_streaming_platform_links(
        artist="Ado",
        title="Kura Kura",
        deezer_url="https://www.deezer.com/album/99999",
    )
    assert links["deezer"] == "https://www.deezer.com/album/99999"
    assert "Ado+Kura+Kura" in links["tidal"]
    assert "listen.tidal.com" in links["tidal"]
    assert "open.qobuz.com" in links["qobuz"]
    assert "open.spotify.com" in links["spotify"]
    assert "music.apple.com" in links["apple_music"]

    # Without custom deezer URL
    fallback_links = build_streaming_platform_links(artist="Yoasobi", title="Idol")
    assert "api.deezer.com" not in fallback_links["deezer"]
    assert "deezer.com/search" in fallback_links["deezer"]


def test_resolve_primary_platform_link() -> None:
    """Test resolving top available platform link based on user priority."""
    links = {
        "deezer": "https://deezer.com/1",
        "tidal": "https://tidal.com/1",
        "qobuz": "https://qobuz.com/1",
    }

    # Tidal first
    plat, url = resolve_primary_platform_link(links, ["tidal", "deezer", "qobuz"])
    assert plat == "tidal"
    assert url == "https://tidal.com/1"

    # Qobuz first
    plat, url = resolve_primary_platform_link(links, ["qobuz", "tidal"])
    assert plat == "qobuz"
    assert url == "https://qobuz.com/1"

    # Priority platform missing from links -> fall back to next available
    plat, url = resolve_primary_platform_link(links, ["spotify", "apple_music", "deezer"])
    assert plat == "deezer"
    assert url == "https://deezer.com/1"

    # Empty priority
    plat, url = resolve_primary_platform_link(links, [])
    assert plat == "deezer"

    # Empty links
    plat, url = resolve_primary_platform_link({}, ["tidal"])
    assert plat == "deezer"
    assert url == ""


def test_parse_platform_order_input() -> None:
    """Test numerical and text input parsing for user platform preference."""
    # Numeric sequence 2 3 1
    order1 = parse_platform_order_input("2 3 1")
    assert order1[0] == "tidal"
    assert order1[1] == "qobuz"
    assert order1[2] == "deezer"
    assert "spotify" in order1
    assert "apple_music" in order1
    assert len(order1) == len(SUPPORTED_PLATFORMS)

    # Full 5 numbers in reverse
    order2 = parse_platform_order_input("5 4 3 2 1")
    assert order2 == ["apple_music", "spotify", "qobuz", "tidal", "deezer"]

    # Named text input with extra spaces and duplicate
    order3 = parse_platform_order_input("  QOBUZ   tidal  qobuz  ")
    assert order3[0] == "qobuz"
    assert order3[1] == "tidal"

    # Model property test
    prefs = UserPreferences(
        telegram_id=123,
        streaming_platform_order="tidal,qobuz,deezer",
    )
    p_list = prefs.platform_priority_list
    assert p_list[0] == "tidal"
    assert p_list[1] == "qobuz"
    assert p_list[2] == "deezer"
    assert "spotify" in p_list
    assert "apple_music" in p_list
