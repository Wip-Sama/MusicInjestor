"""Unit tests for the `/radar` Telegram bot router and keyboards."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, User

from src.bot.keyboards.radar import (
    RadarCallback,
    build_album_tracks_keyboard,
    build_artist_dossier_keyboard,
    build_followed_artists_keyboard,
    build_other_platforms_keyboard,
    build_preview_audio_keyboard,
    build_radar_home_keyboard,
    build_release_detail_keyboard,
    build_release_notification_keyboard,
)
from src.bot.routers.radar import (
    format_artist_dossier_card,
    format_radar_home_card,
    format_release_detail_card,
    format_release_notification_card,
    handle_cmd_follow,
    handle_cmd_radar,
    handle_cmd_unfollow,
    handle_radar_album_tracks,
    handle_radar_other_links,
    handle_radar_play_full_track,
    handle_radar_preview_audio,
    handle_radar_preview_individual_track,
    handle_radar_test_alert,
    handle_radar_toggle_follow,
    handle_radar_view_release,
)
from src.config.settings import AppSettings
from src.core.models import (
    DiscoveredRelease,
    FollowedArtist,
    LibraryTrackRecord,
    ReleaseTrackPreview,
    UserAccount,
    UserPreferences,
    UserRole,
)
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
    mock_repo.get_user_preferences.return_value = UserPreferences(telegram_id=1001)
    mock_repo.find_library_tracks_for_release.return_value = []

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


@pytest.mark.anyio
async def test_handle_radar_album_tracks_and_other_platforms() -> None:
    """Test viewing album tracks, listening to a track preview, and viewing other platform links."""
    query = MagicMock(spec=CallbackQuery)
    query.from_user = User(id=1001, is_bot=False, first_name="Test")
    query.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.answer_audio = AsyncMock()
    msg.edit_text = AsyncMock()
    query.message = msg
    user = UserAccount(telegram_id=1001, display_name="Test User", role=UserRole.LISTENER)

    album_release = DiscoveredRelease(
        id="deezer:album_99",
        artist_name="Ado",
        title="Kyogen",
        release_type="album",
        release_date="2022-01-26",
        track_count=14,
        provider_release_id="album_99",
        external_url="https://deezer.com/album/99",
    )
    track_previews = [
        ReleaseTrackPreview(
            track_id="tr_1",
            title="Readymade",
            preview_url="https://cdn.mp3/1",
            duration_seconds=30,
            track_position=1,
        ),
        ReleaseTrackPreview(
            track_id="tr_2",
            title="Odo",
            preview_url="https://cdn.mp3/2",
            duration_seconds=30,
            track_position=2,
        ),
    ]

    mock_repo = AsyncMock(spec=GovernanceRepositoryPort)
    mock_repo.get_discovered_release.return_value = album_release
    mock_repo.get_user_preferences.return_value = UserPreferences(telegram_id=1001)
    mock_repo.find_library_tracks_for_release.return_value = []
    mock_radar = AsyncMock(spec=ReleaseRadarService)
    mock_radar.get_release_track_previews.return_value = track_previews
    mock_radar.get_preview_audio_bytes.return_value = b"TRACK_AUDIO_BYTES"

    settings = AppSettings()

    # 1. Test album_tracks keyboard & handler
    album_kb = build_album_tracks_keyboard(album_release, track_previews)
    assert len(album_kb.inline_keyboard) >= 3

    await handle_radar_album_tracks(
        query=query,
        callback_data=RadarCallback(action="album_tracks", target_id="deezer:album_99"),
        governance_repository=mock_repo,
        radar_service=mock_radar,
    )
    query.message.edit_text.assert_awaited()

    # 2. Test preview_track handler
    await handle_radar_preview_individual_track(
        query=query,
        callback_data=RadarCallback(action="preview_track", target_id="deezer:album_99:tr_2"),
        current_user=user,
        governance_repository=mock_repo,
        radar_service=mock_radar,
        settings=settings,
    )
    mock_radar.get_preview_audio_bytes.assert_awaited_with("https://cdn.mp3/2")
    query.message.answer_audio.assert_awaited()

    # 3. Test other_platforms keyboard & handler
    other_kb = build_other_platforms_keyboard(
        album_release, {"tidal": "https://tidal.com", "qobuz": "https://qobuz.com"}
    )
    assert len(other_kb.inline_keyboard) == 3

    await handle_radar_other_links(
        query=query,
        callback_data=RadarCallback(action="other_links", target_id="deezer:album_99"),
        governance_repository=mock_repo,
    )
    query.message.edit_text.assert_awaited()


@pytest.mark.anyio
async def test_handle_radar_play_full_track(tmp_path: Path) -> None:
    """Test playing a complete track when in library vs fallback error."""
    query = MagicMock(spec=CallbackQuery)
    query.from_user = User(id=1001, is_bot=False, first_name="Test")
    query.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.answer_audio = AsyncMock()
    query.message = msg
    user = UserAccount(telegram_id=1001, display_name="Test User", role=UserRole.LISTENER)

    release = DiscoveredRelease(
        id="deezer:10",
        artist_name="Ado",
        title="Show",
        release_type="single",
        release_date="2023-09-06",
        in_library=True,
    )
    fake_audio = tmp_path / "Ado - Show.flac"
    fake_audio.write_bytes(b"FLAC_DATA")
    fake_lrc = tmp_path / "Ado - Show.lrc"
    fake_lrc.write_text("[00:01.00] Show lyrics", encoding="utf-8")

    lib_track = LibraryTrackRecord(
        id="tr_full_123",
        job_id="job_1",
        uploaded_by=1001,
        storage_path=fake_audio,
        fingerprint="fp_show",
        duration_seconds=190.0,
        artist="Ado",
        album="Show - Single",
        title="Show",
        telegram_file_id="tg_file_ado_show",
    )

    mock_repo = AsyncMock(spec=GovernanceRepositoryPort)
    mock_repo.get_discovered_release.return_value = release
    mock_repo.find_library_tracks_for_release.return_value = [lib_track]
    mock_repo.get_user_preferences.return_value = UserPreferences(telegram_id=1001)

    settings = AppSettings(web_app_url="https://app.example.com")

    # 1. Successful playback using telegram_file_id
    await handle_radar_play_full_track(
        query=query,
        callback_data=RadarCallback(action="play_full", target_id="deezer:10"),
        governance_repository=mock_repo,
        settings=settings,
        current_user=user,
    )
    query.message.answer_audio.assert_awaited_once()
    assert query.message.answer_audio.call_args[0][0] == "tg_file_ado_show"

    # 2. Track not in library
    mock_repo.find_library_tracks_for_release.return_value = []
    await handle_radar_play_full_track(
        query=query,
        callback_data=RadarCallback(action="play_full", target_id="deezer:10"),
        governance_repository=mock_repo,
        settings=settings,
        current_user=user,
    )
    query.answer.assert_awaited_with("Full track not found in library.", show_alert=True)


def test_radar_notification_formatting_and_keyboard(tmp_path: Path) -> None:
    """Test release notification card and keyboard with both full track and preview links."""
    release = DiscoveredRelease(
        id="deezer:55",
        artist_name="YOASOBI",
        title="Biri-Biri",
        release_type="single",
        release_date="2023-11-18",
        in_library=True,
        library_track_id="tr_biri_1",
    )
    lib_track = LibraryTrackRecord(
        id="tr_biri_1",
        job_id="job_2",
        uploaded_by=1001,
        storage_path=tmp_path / "biri.mp3",
        fingerprint="fp_biri",
        duration_seconds=200.0,
        artist="YOASOBI",
        album="Biri-Biri - Single",
        title="Biri-Biri",
    )

    # Card formatting
    card = format_release_notification_card(release, lib_track=lib_track)
    assert "Available in Library (Full Track)" in card
    assert "YOASOBI" in card
    assert "Biri-Biri" in card

    # Keyboard building with Mini App deep-link
    kb = build_release_notification_keyboard(
        release,
        lib_track=lib_track,
        web_app_url="https://app.example.com",
    )
    assert kb.inline_keyboard
    # Verify Play Full Track and Song in Bot buttons are present
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("Play Full Track" in t for t in button_texts)
    assert any("Song in Bot" in t for t in button_texts)
    assert any("Play in Mini App" in t for t in button_texts)


def test_artist_dossier_card_and_keyboard(tmp_path: Path) -> None:
    """Test format_artist_dossier_card and build_artist_dossier_keyboard."""
    trk = LibraryTrackRecord(
        id="tr_1",
        job_id="job_1",
        uploaded_by=1001,
        storage_path=tmp_path / "song.flac",
        fingerprint="fp1",
        duration_seconds=180.0,
        artist="YOASOBI",
        album="THE BOOK",
        title="Into The Night",
        year=2021,
    )
    rel = DiscoveredRelease(
        id="rel_1",
        artist_name="YOASOBI",
        title="Idol",
        release_type="single",
        release_date="2023-04-12",
    )
    card = format_artist_dossier_card(
        artist_name="YOASOBI",
        is_followed=True,
        library_tracks_count=1,
        releases_count=1,
        library_tracks=[trk],
        releases=[rel],
    )
    assert "Artist Dossier — YOASOBI" in card
    assert "Following (Alerts Active)" in card
    assert "Into The Night" in card
    assert "Idol" in card

    kb = build_artist_dossier_keyboard(
        artist_name="YOASOBI",
        is_followed=True,
        library_tracks=[trk],
        releases=[rel],
        web_app_url="https://app.example.com",
    )
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("Unfollow YOASOBI" in t for t in button_texts)
    assert any("Into The Night" in t for t in button_texts)
    assert any("Idol" in t for t in button_texts)


@pytest.mark.anyio
async def test_cmd_follow_and_unfollow() -> None:
    """Test /follow and /unfollow command handlers."""
    radar_service = AsyncMock(spec=ReleaseRadarService)
    radar_service.follow_artist.return_value = FollowedArtist(id=1, telegram_id=123, artist_name="YOASOBI")
    radar_service.unfollow_artist.return_value = True

    repo = AsyncMock(spec=GovernanceRepositoryPort)
    repo.list_library_tracks_by_artist.return_value = []
    repo.list_discovered_releases.return_value = []
    repo.get_user_preferences.return_value = UserPreferences(telegram_id=123)

    settings = AppSettings()
    user = UserAccount(telegram_id=123, display_name="User", role=UserRole.UPLOADER)

    # 1. /follow without argument -> usage prompt
    msg_empty = AsyncMock(spec=Message)
    msg_empty.text = "/follow"
    await handle_cmd_follow(
        msg_empty,
        current_user=user,
        governance_repository=repo,
        radar_service=radar_service,
        settings=settings,
    )
    assert msg_empty.answer.called

    # 2. /follow with artist name -> follows and shows dossier
    msg_valid = AsyncMock(spec=Message)
    msg_valid.text = "/follow YOASOBI"
    await handle_cmd_follow(
        msg_valid,
        current_user=user,
        governance_repository=repo,
        radar_service=radar_service,
        settings=settings,
    )
    radar_service.follow_artist.assert_awaited_with(123, "YOASOBI")
    assert msg_valid.answer.called

    # 3. /unfollow without argument -> usage prompt
    msg_un_empty = AsyncMock(spec=Message)
    msg_un_empty.text = "/unfollow"
    await handle_cmd_unfollow(
        msg_un_empty,
        current_user=user,
        radar_service=radar_service,
    )
    assert msg_un_empty.answer.called

    # 4. /unfollow with artist name
    msg_un_valid = AsyncMock(spec=Message)
    msg_un_valid.text = "/unfollow YOASOBI"
    await handle_cmd_unfollow(
        msg_un_valid,
        current_user=user,
        radar_service=radar_service,
    )
    radar_service.unfollow_artist.assert_awaited_with(123, "YOASOBI")
    assert msg_un_valid.answer.called


@pytest.mark.anyio
async def test_handle_radar_test_alert() -> None:
    """Test delivering a test release drop notification card."""
    repo = AsyncMock(spec=GovernanceRepositoryPort)
    repo.get_user_preferences.return_value = UserPreferences(telegram_id=123, notifications_enabled=True)
    repo.list_discovered_releases.return_value = [
        DiscoveredRelease(
            id="rel_1",
            artist_name="YOASOBI",
            title="Idol",
            release_type="single",
            release_date="2023-04-12",
        )
    ]
    repo.find_library_tracks_for_release.return_value = []

    settings = AppSettings()
    user = UserAccount(telegram_id=123, display_name="User", role=UserRole.UPLOADER)

    mock_bot = AsyncMock()
    msg = AsyncMock(spec=Message)
    msg.from_user = User(id=123, is_bot=False, first_name="User")
    msg.bot = mock_bot

    await handle_radar_test_alert(
        event=msg,
        current_user=user,
        governance_repository=repo,
        settings=settings,
    )
    mock_bot.send_message.assert_awaited_once()
    call_args = mock_bot.send_message.call_args[1]
    assert call_args["chat_id"] == 123
    assert "[TEST NOTIFICATION]" in call_args["text"]
    assert "YOASOBI" in call_args["text"]
