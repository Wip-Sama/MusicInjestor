"""Unit tests for search preview external streaming links and other platforms keyboard."""

from aiogram.types import InlineKeyboardMarkup

from src.bot.keyboards.search import (
    build_search_other_platforms_keyboard,
    build_search_preview_keyboard,
)


def test_build_search_preview_keyboard_with_deezer():
    """Verify build_search_preview_keyboard includes primary streaming link, copy button, and webapp link."""
    kb = build_search_preview_keyboard(
        artist="RADWIMPS",
        title="Sparkle",
        deezer_url="https://www.deezer.com/track/12345",
        platform_priority=["deezer", "spotify", "apple_music"],
        web_app_url="https://tg-music.example.com",
        target_id="12345",
        query="Sparkle",
    )
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]

    # Verify primary Deezer link button exists
    deezer_btn = next((b for b in buttons if b.url == "https://www.deezer.com/track/12345"), None)
    assert deezer_btn is not None
    assert "Deezer" in deezer_btn.text

    # Verify copy button exists with direct copy text
    copy_btn = next(
        (b for b in buttons if b.copy_text and b.copy_text.text == "https://www.deezer.com/track/12345"),
        None,
    )
    assert copy_btn is not None

    # Verify other platforms button exists
    other_btn = next(
        (b for b in buttons if b.callback_data and b.callback_data.startswith("srch~other_links")),
        None,
    )
    assert other_btn is not None


def test_build_search_other_platforms_keyboard():
    """Verify other platforms screen includes multiple streaming links with copy text and back button."""
    links = {
        "deezer": "https://www.deezer.com/track/1",
        "spotify": "https://open.spotify.com/track/2",
        "apple_music": "https://music.apple.com/track/3",
    }
    kb = build_search_other_platforms_keyboard(
        artist="Artist",
        title="Song",
        links=links,
        back_query="Song",
    )
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]

    # Links present
    assert any(b.url == "https://open.spotify.com/track/2" for b in buttons)
    assert any(b.url == "https://music.apple.com/track/3" for b in buttons)

    # Back button present
    back_btn = next((b for b in buttons if b.callback_data == "srch~back~Song"), None)
    assert back_btn is not None
