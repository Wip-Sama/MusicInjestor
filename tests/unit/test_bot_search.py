"""Unit tests for Telegram bot search router, URL recognition, and album gap keyboards."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.keyboards.admin import BTN_MAIN_SONGS
from src.bot.keyboards.search import (
    AlbumGapCallback,
    SearchActionCallback,
    build_album_gap_keyboard,
    build_search_results_keyboard,
)
from src.bot.routers.search import (
    handle_album_gap_preview,
    handle_album_gap_view,
    handle_chat_text_message,
    handle_search_scope_toggle,
)
from src.config.settings import AppSettings
from src.core.models import (
    AlbumGapAnalysis,
    AlbumTrackGap,
    SearchResultItem,
    UnifiedSearchResult,
    UserAccount,
    UserPreferences,
    UserRole,
)


def test_build_search_results_keyboard() -> None:
    result = UnifiedSearchResult(
        query="Discovery",
        scope="global",
        albums=[
            SearchResultItem(
                item_type="album",
                id="deezer:302127",
                title="Discovery",
                artist="Daft Punk",
                total_tracks=14,
                owned_tracks_count=10,
                missing_tracks_count=4,
            )
        ],
        tracks=[
            SearchResultItem(
                item_type="track",
                id="trk_1",
                title="One More Time",
                artist="Daft Punk",
                in_library=True,
                library_track_id="trk_1",
            ),
            SearchResultItem(
                item_type="track",
                id="preview:102",
                title="Aerodynamic",
                artist="Daft Punk",
                in_library=False,
                preview_url="https://preview.mp3",
            ),
        ],
    )
    markup = build_search_results_keyboard(result, web_app_url="https://music.example.com")
    all_buttons = [btn for row in markup.inline_keyboard for btn in row]
    btn_texts = [btn.text for btn in all_buttons]

    assert any("Discovery (10/14)" in t for t in btn_texts)
    assert any("One More Time (Owned)" in t for t in btn_texts)
    assert any("Preview: Aerodynamic" in t for t in btn_texts)
    assert any("Switch to Library Only" in t for t in btn_texts)
    assert any("Open Search in Web App" in t for t in btn_texts)


def test_build_album_gap_keyboard() -> None:
    gap = AlbumGapAnalysis(
        album_id="deezer:302127",
        title="Discovery",
        artist="Daft Punk",
        total_tracks=2,
        owned_tracks_count=1,
        missing_tracks_count=1,
        tracks=[
            AlbumTrackGap(
                position=1,
                title="One More Time",
                is_owned=True,
                library_track_id="trk_1",
            ),
            AlbumTrackGap(
                position=2,
                title="Aerodynamic",
                is_owned=False,
                preview_url="https://preview.mp3",
            ),
        ],
        external_url="https://www.deezer.com/album/302127",
    )
    markup = build_album_gap_keyboard(gap, web_app_url="https://music.example.com")
    all_buttons = [btn for row in markup.inline_keyboard for btn in row]
    btn_texts = [btn.text for btn in all_buttons]

    assert any("#1 One More Time" in t for t in btn_texts)
    assert any("Preview #2 Aerodynamic" in t for t in btn_texts)
    assert any("Open on Deezer" in t for t in btn_texts)
    assert any("View Album in Web App" in t for t in btn_texts)


@pytest.mark.anyio
async def test_handle_chat_text_message_url_recognition() -> None:
    message = AsyncMock()
    message.text = "Check this out https://open.spotify.com/album/12345"

    await handle_chat_text_message(
        message=message,
        current_user=MagicMock(),
        governance_repository=AsyncMock(),
        album_search_service=AsyncMock(),
    )

    message.answer.assert_called_once()
    called_card = message.answer.call_args[0][0]
    assert "External Link Recognized" in called_card
    assert "https://open.spotify.com/album/12345" in called_card


@pytest.mark.anyio
async def test_handle_chat_text_message_ignores_commands_and_buttons() -> None:
    message = AsyncMock()
    message.text = "/songs"
    mock_service = AsyncMock()

    await handle_chat_text_message(
        message=message,
        current_user=MagicMock(),
        governance_repository=AsyncMock(),
        album_search_service=mock_service,
    )
    mock_service.search.assert_not_called()
    message.answer.assert_not_called()

    # Reserved button
    message.text = BTN_MAIN_SONGS
    await handle_chat_text_message(
        message=message,
        current_user=MagicMock(),
        governance_repository=AsyncMock(),
        album_search_service=mock_service,
    )
    mock_service.search.assert_not_called()
    message.answer.assert_not_called()


@pytest.mark.anyio
async def test_handle_chat_text_message_triggers_unified_search() -> None:
    message = AsyncMock()
    message.text = "Daft Punk Discovery"

    user = UserAccount(telegram_id=123, display_name="Test User", role=UserRole.LISTENER)
    mock_repo = AsyncMock()
    mock_repo.get_user_preferences.return_value = UserPreferences(telegram_id=123, search_scope="global")

    mock_service = AsyncMock()
    mock_service.search.return_value = UnifiedSearchResult(
        query="Daft Punk Discovery",
        scope="global",
        albums=[
            SearchResultItem(
                item_type="album",
                id="deezer:302127",
                title="Discovery",
                artist="Daft Punk",
                total_tracks=14,
                owned_tracks_count=10,
                missing_tracks_count=4,
            )
        ],
        tracks=[],
    )

    settings = AppSettings(web_app_url="https://music.example.com")

    await handle_chat_text_message(
        message=message,
        current_user=user,
        governance_repository=mock_repo,
        album_search_service=mock_service,
        settings=settings,
    )

    mock_service.search.assert_called_once_with(query="Daft Punk Discovery", scope="global")
    message.answer.assert_called_once()
    text = message.answer.call_args[0][0]
    assert "Daft Punk Discovery" in text
    assert "Discovery" in text


@pytest.mark.anyio
async def test_handle_album_gap_view_callback() -> None:
    query = AsyncMock()
    query.message = AsyncMock()
    cb = AlbumGapCallback(action="view", album_id="deezer:302127")

    mock_service = AsyncMock()
    mock_service.analyze_album_gap.return_value = AlbumGapAnalysis(
        album_id="deezer:302127",
        title="Discovery",
        artist="Daft Punk",
        total_tracks=14,
        owned_tracks_count=14,
        missing_tracks_count=0,
        tracks=[],
    )

    await handle_album_gap_view(
        query=query,
        callback_data=cb,
        album_search_service=mock_service,
        settings=AppSettings(),
    )

    mock_service.analyze_album_gap.assert_called_once()
    query.message.edit_text.assert_called_once()
    text = query.message.edit_text.call_args[0][0]
    assert "Album Overview" in text
    assert "Discovery" in text


@pytest.mark.anyio
async def test_handle_album_gap_preview_callback() -> None:
    query = AsyncMock()
    query.message = AsyncMock()
    cb = AlbumGapCallback(action="preview", album_id="deezer:302127", track_pos=2)

    mock_service = AsyncMock()
    mock_service.analyze_album_gap.return_value = AlbumGapAnalysis(
        album_id="deezer:302127",
        title="Discovery",
        artist="Daft Punk",
        total_tracks=2,
        owned_tracks_count=1,
        missing_tracks_count=1,
        tracks=[
            AlbumTrackGap(position=1, title="One More Time", is_owned=True),
            AlbumTrackGap(position=2, title="Aerodynamic", is_owned=False, preview_url="https://preview.mp3"),
        ],
    )

    mock_radar = AsyncMock()
    mock_radar.get_preview_audio_bytes.return_value = b"ID3" + b"\x00" * 2000

    await handle_album_gap_preview(
        query=query,
        callback_data=cb,
        album_search_service=mock_service,
        radar_service=mock_radar,
    )

    mock_radar.get_preview_audio_bytes.assert_called_once_with("https://preview.mp3")
    query.message.answer_audio.assert_called_once()
    call_kwargs = query.message.answer_audio.call_args[1]
    assert call_kwargs["title"] == "Aerodynamic (Preview)"
    assert call_kwargs["performer"] == "Daft Punk"


@pytest.mark.anyio
async def test_handle_search_scope_toggle() -> None:
    query = AsyncMock()
    query.message = AsyncMock()
    cb = SearchActionCallback(action="scope", value="library", query="Discovery")

    user = UserAccount(telegram_id=123, display_name="Test User", role=UserRole.LISTENER)
    mock_repo = AsyncMock()
    mock_repo.get_user_preferences.return_value = UserPreferences(telegram_id=123, search_scope="global")

    mock_service = AsyncMock()
    mock_service.search.return_value = UnifiedSearchResult(
        query="Discovery",
        scope="library",
        albums=[],
        tracks=[],
    )

    await handle_search_scope_toggle(
        query=query,
        callback_data=cb,
        current_user=user,
        governance_repository=mock_repo,
        album_search_service=mock_service,
        settings=AppSettings(),
    )

    mock_repo.update_user_preferences.assert_called_once()
    mock_service.search.assert_called_once_with(query="Discovery", scope="library")
    query.message.edit_text.assert_called_once()
