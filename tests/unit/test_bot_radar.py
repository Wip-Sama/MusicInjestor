"""Unit tests for the `/radar` Telegram bot router and keyboards."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, User

from src.bot.keyboards.radar import (
    RadarCallback,
    build_followed_artists_keyboard,
    build_preview_audio_keyboard,
    build_radar_home_keyboard,
    build_release_detail_keyboard,
)
from src.bot.routers.radar import (
    format_radar_home_card,
    format_release_detail_card,
    handle_cmd_radar,
    handle_radar_preview_audio,
    handle_radar_toggle_follow,
    handle_radar_view_release,
)
from src.config.settings import AppSettings
from src.core.models import DiscoveredRelease, FollowedArtist, UserAccount, UserRole
from src.core.ports import GovernanceRepositoryPort
from src.core.radar import ReleaseRadarService


def test_radar_keyboards_and_cards() -> None:
    """Test formatters and inline keyboard generation."""
    card = format_radar_home_card(
        total_releases=10, followed_count=2, monitored_count=5, page=1, total_pages=2
    )
    assert "Release Radar" in card
    assert "10" in card

    release = DiscoveredRelease(
        id="rel_1",
        artist_name="YOASOBI",
        title="Idol",
        release_type="single",
        release_date="2023-04-12",
        preview_url="https://preview.mp3",
    )
    detail_card = format_release_detail_card(release, is_followed=True)
    assert "YOASOBI" in detail_card
    assert "Following Artist" in detail_card

    home_kb = build_radar_home_keyboard([release], page=1, total_pages=1, followed_count=1)
    assert home_kb.inline_keyboard

    detail_kb = build_release_detail_keyboard(release, is_followed=False)
    assert detail_kb.inline_keyboard

    audio_kb = build_preview_audio_keyboard(release)
    assert audio_kb.inline_keyboard

    follows_kb = build_followed_artists_keyboard(
        [FollowedArtist(id=1, telegram_id=123, artist_name="YOASOBI")]
    )
    assert follows_kb.inline_keyboard


@pytest.mark.anyio
async def test_handle_cmd_radar() -> None:
    """Test /radar command execution."""
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=1001, is_bot=False, first_name="Test")
    msg.answer = AsyncMock()
    user = UserAccount(telegram_id=1001, display_name="Test User", role=UserRole.LISTENER)

    mock_repo = AsyncMock(spec=GovernanceRepositoryPort)
    mock_repo.list_discovered_releases.return_value = []
    mock_repo.list_user_followed_artists.return_value = []
    mock_repo.list_all_monitored_artists.return_value = []

    settings = AppSettings()

    await handle_cmd_radar(
        message=msg,
        current_user=user,
        governance_repository=mock_repo,
        settings=settings,
    )
    msg.answer.assert_awaited_once()


@pytest.mark.anyio
async def test_handle_radar_view_and_preview() -> None:
    """Test viewing a release and playing a preview."""
    query = MagicMock(spec=CallbackQuery)
    query.from_user = User(id=1001, is_bot=False, first_name="Test")
    query.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.answer_audio = AsyncMock()
    msg.edit_text = AsyncMock()
    query.message = msg
    user = UserAccount(telegram_id=1001, display_name="Test User", role=UserRole.LISTENER)

    release = DiscoveredRelease(
        id="deezer:10",
        artist_name="Ado",
        title="Show",
        release_type="single",
        release_date="2023-09-06",
        preview_url="https://preview.mp3",
    )

    mock_repo = AsyncMock(spec=GovernanceRepositoryPort)
    mock_repo.get_discovered_release.return_value = release
    mock_repo.is_artist_followed.return_value = False

    settings = AppSettings()
    mock_radar = AsyncMock(spec=ReleaseRadarService)
    mock_radar.get_preview_audio_bytes.return_value = b"MP3_BYTES_123"

    # View release
    await handle_radar_view_release(
        query=query,
        callback_data=RadarCallback(action="view", target_id="deezer:10"),
        current_user=user,
        governance_repository=mock_repo,
        settings=settings,
    )

    # Preview audio
    await handle_radar_preview_audio(
        query=query,
        callback_data=RadarCallback(action="preview", target_id="deezer:10"),
        governance_repository=mock_repo,
        radar_service=mock_radar,
        settings=settings,
    )
    mock_radar.get_preview_audio_bytes.assert_awaited_once_with("https://preview.mp3")
    query.message.answer_audio.assert_awaited_once()


@pytest.mark.anyio
async def test_handle_radar_toggle_follow() -> None:
    """Test following and unfollowing an artist via inline callback."""
    query = MagicMock(spec=CallbackQuery)
    query.from_user = User(id=1001, is_bot=False, first_name="Test")
    query.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    query.message = msg
    user = UserAccount(telegram_id=1001, display_name="Test User", role=UserRole.LISTENER)

    mock_repo = AsyncMock(spec=GovernanceRepositoryPort)
    mock_repo.get_discovered_release.return_value = None
    mock_repo.list_discovered_releases.return_value = []
    mock_radar = AsyncMock(spec=ReleaseRadarService)

    settings = AppSettings()

    # Follow
    await handle_radar_toggle_follow(
        query=query,
        callback_data=RadarCallback(action="follow", target_id="Ado"),
        current_user=user,
        governance_repository=mock_repo,
        radar_service=mock_radar,
        settings=settings,
    )
    mock_radar.follow_artist.assert_awaited_once_with(1001, "Ado")

    # Unfollow
    await handle_radar_toggle_follow(
        query=query,
        callback_data=RadarCallback(action="unfollow", target_id="Ado"),
        current_user=user,
        governance_repository=mock_repo,
        radar_service=mock_radar,
        settings=settings,
    )
    mock_radar.unfollow_artist.assert_awaited_once_with(1001, "Ado")
