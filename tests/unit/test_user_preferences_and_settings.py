"""Unit tests for UserPreferences repository operations, bot /settings, and webapp settings."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, User

from src.bot.keyboards.songs import (
    SettingsCallback,
    SongsBulkReviewCallback,
    build_bulk_review_overview_keyboard,
    build_bulk_track_review_keyboard,
    build_user_settings_keyboard,
)
from src.bot.routers.commands import (
    _render_settings_card,
    handle_platform_order_text_input,
    handle_settings_callback,
    handle_settings_command,
)
from src.bot.routers.songs import (
    handle_bulk_review_callback,
    handle_songs_callback,
)
from src.core.models import (
    LibraryTrackRecord,
    UserAccount,
    UserPreferences,
    UserRole,
)
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository


def _make_msg(text: str = "", user_id: int = 100) -> Message:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = User(id=user_id, is_bot=False, first_name="Test")
    msg.chat = MagicMock()
    msg.chat.id = user_id
    msg.message_id = 42
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    return msg


def _make_cb(data: str = "", user_id: int = 100, message: Message | None = None) -> CallbackQuery:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = User(id=user_id, is_bot=False, first_name="Test")
    cb.answer = AsyncMock()
    cb.message = message or _make_msg("", user_id=user_id)
    return cb


@pytest.mark.anyio
async def test_user_preferences_sqlite_roundtrip(tmp_path: Path) -> None:
    """Verify default retrieval and upsert persistence of UserPreferences in SQLite."""
    db_path = tmp_path / "catalog.db"
    manager = SqliteDatabaseManager(db_path)
    await manager.initialize()
    repo = SqliteCatalogRepository(manager)

    # 1. Default preferences when none exist
    prefs = await repo.get_user_preferences(telegram_id=456)
    assert prefs.telegram_id == 456
    assert prefs.stream_format == "auto"
    assert prefs.stream_bitrate == 128
    assert prefs.auto_lyrics_search is True

    # 2. Insert new preferences
    prefs.stream_format = "opus"
    prefs.stream_bitrate = 128
    prefs.auto_lyrics_search = False
    await repo.update_user_preferences(prefs)

    reloaded = await repo.get_user_preferences(telegram_id=456)
    assert reloaded.telegram_id == 456
    assert reloaded.stream_format == "opus"
    assert reloaded.stream_bitrate == 128
    assert reloaded.auto_lyrics_search is False

    # 3. Update existing preferences
    reloaded.stream_format = "mp3"
    reloaded.stream_bitrate = 320
    reloaded.auto_lyrics_search = True
    await repo.update_user_preferences(reloaded)

    reloaded2 = await repo.get_user_preferences(telegram_id=456)
    assert reloaded2.stream_format == "mp3"
    assert reloaded2.stream_bitrate == 320
    assert reloaded2.auto_lyrics_search is True


def test_render_settings_card_and_keyboards() -> None:
    """Verify _render_settings_card formatting and keyboard builders."""
    prefs = UserPreferences(
        telegram_id=123,
        stream_format="opus",
        stream_bitrate=128,
        auto_lyrics_search=False,
    )
    card = _render_settings_card(prefs)
    assert "User Streaming &amp; Audio Preferences" in card
    assert "Opus" in card
    assert "128 kbps" in card
    assert "Disabled" in card

    kb = build_user_settings_keyboard(prefs, back_callback="menu:home")
    assert kb is not None
    assert len(kb.inline_keyboard) > 0

    overview_kb = build_bulk_review_overview_keyboard(pending_count=3)
    assert overview_kb is not None

    track_kb = build_bulk_track_review_keyboard(current_index=0, total_count=3)
    assert track_kb is not None


@pytest.mark.anyio
async def test_bot_settings_command_and_callbacks() -> None:
    """Verify handle_settings_command and handle_settings_callback handlers."""
    repo = AsyncMock()
    user = UserAccount(telegram_id=789, display_name="Auditor", role=UserRole.ADMIN)
    prefs = UserPreferences(
        telegram_id=789,
        stream_format="original",
        stream_bitrate=192,
        auto_lyrics_search=True,
    )
    repo.get_user_preferences.return_value = prefs

    # Command
    msg = _make_msg("/settings", user_id=789)
    await handle_settings_command(msg, current_user=user, governance_repository=repo, state=None)
    assert msg.answer.called or msg.edit_text.called

    # Callback: change format to mp3
    cb_msg = _make_msg(user_id=789)
    cb = _make_cb(user_id=789, message=cb_msg)
    cb_data = SettingsCallback(action="fmt", value="mp3")
    await handle_settings_callback(
        cb,
        cb_data,
        current_user=user,
        governance_repository=repo,
        state=None,
    )
    assert prefs.stream_format == "mp3"
    assert repo.update_user_preferences.called

    # Callback: change bitrate to 320
    cb_data_br = SettingsCallback(action="br", value="320")
    await handle_settings_callback(
        cb,
        cb_data_br,
        current_user=user,
        governance_repository=repo,
        state=None,
    )
    assert prefs.stream_bitrate == 320

    # Callback: toggle lyrics
    cb_data_lyrics = SettingsCallback(action="lyrics", value="toggle")
    await handle_settings_callback(
        cb,
        cb_data_lyrics,
        current_user=user,
        governance_repository=repo,
        state=None,
    )
    assert prefs.auto_lyrics_search is False

    # Callback: oidc disconnect
    linked_user = UserAccount(
        telegram_id=789, display_name="Auditor", role=UserRole.ADMIN, authentik_sub="sub_789"
    )
    unlinked_user = UserAccount(telegram_id=789, display_name="Auditor", role=UserRole.ADMIN)
    gov_service = AsyncMock()
    gov_service.unlink_user_oidc.return_value = unlinked_user
    cb_data_oidc = SettingsCallback(action="oidc", value="disconnect")
    await handle_settings_callback(
        cb,
        cb_data_oidc,
        current_user=linked_user,
        governance_repository=repo,
        governance_service=gov_service,
        state=None,
    )
    gov_service.unlink_user_oidc.assert_awaited_with(
        actor=linked_user, target_telegram_id=linked_user.telegram_id
    )

    # Callback: platforms prompt
    state_mock = AsyncMock()
    cb_data_plat = SettingsCallback(action="platforms", value="order")
    await handle_settings_callback(
        cb,
        cb_data_plat,
        current_user=user,
        governance_repository=repo,
        state=state_mock,
    )
    state_mock.set_state.assert_awaited()

    # Callback: cancel platform order
    cb_data_cancel = SettingsCallback(action="cancel", value="cancel")
    await handle_settings_callback(
        cb,
        cb_data_cancel,
        current_user=user,
        governance_repository=repo,
        state=state_mock,
    )

    # Message: platform order text input "2 3 1"
    order_msg = MagicMock(spec=Message)
    order_msg.text = "2 3 1"
    order_msg.answer = AsyncMock()
    await handle_platform_order_text_input(
        message=order_msg,
        current_user=user,
        governance_repository=repo,
        state=state_mock,
    )
    assert prefs.platform_priority_list[0] == "tidal"
    assert prefs.platform_priority_list[1] == "qobuz"
    assert prefs.platform_priority_list[2] == "deezer"
    order_msg.answer.assert_awaited()


@pytest.mark.anyio
async def test_bot_bulk_review_callbacks(tmp_path: Path) -> None:
    """Verify handle_bulk_review_callback handlers in songs router."""
    f1 = tmp_path / "song1.mp3"
    f1.write_bytes(b"DATA")
    track = LibraryTrackRecord(
        id="trk_review_1",
        job_id="job_1",
        uploaded_by=100,
        storage_path=f1,
        fingerprint="fp1",
        duration_seconds=180,
        artist="Original Artist",
        album="Original Album",
        title="Original Title",
        year=2020,
    )
    cat_repo = AsyncMock()
    cat_repo.get_library_track.return_value = track
    cat_repo.list_library_tracks.return_value = [track]

    organizer = MagicMock()
    organizer.write_metadata_to_file = AsyncMock()
    organizer.write_sidecar_lrc = AsyncMock()
    organizer.relocate_and_retag_track = AsyncMock(return_value=f1)
    organizer.read_track_lyrics = AsyncMock(return_value=None)

    user = UserAccount(telegram_id=100, display_name="Admin", role=UserRole.ADMIN)
    state = AsyncMock()
    state.get_data.return_value = {}

    # Bulk menu callback
    menu_cb = _make_cb("songs:bulk_menu", user_id=100)
    await handle_songs_callback(
        menu_cb,
        state=state,
        current_user=user,
        governance_repository=cat_repo,
        catalog_repository=cat_repo,
    )
    assert menu_cb.message.edit_text.called or menu_cb.answer.called

    # Review callback: view item 0
    state.get_data.return_value = {
        "bulk_pending_items": [
            {
                "track_id": "trk_review_1",
                "current_title": "Original Title",
                "current_artist": "Original Artist",
                "proposed_title": "Proposed Title",
                "proposed_artist": "Proposed Artist",
                "proposed_album": "Proposed Album",
                "proposed_meta": {
                    "title": "Proposed Title",
                    "artist": "Proposed Artist",
                    "album": "Proposed Album",
                    "year": 2020,
                },
                "confidence": 0.95,
            }
        ],
    }

    rev_cb = _make_cb("rev", user_id=100)
    rev_data = SongsBulkReviewCallback(action="view", index=0)
    await handle_bulk_review_callback(
        rev_cb,
        rev_data,
        current_user=user,
        governance_repository=cat_repo,
        organizer=organizer,
        state=state,
    )
    assert rev_cb.message.edit_text.called

    # Review callback: apply_one item 0
    rev_apply_data = SongsBulkReviewCallback(action="apply_one", index=0)
    await handle_bulk_review_callback(
        rev_cb,
        rev_apply_data,
        current_user=user,
        governance_repository=cat_repo,
        organizer=organizer,
        state=state,
    )
    assert organizer.relocate_and_retag_track.called

    # Review callback: skip_one item 0
    state.get_data.return_value = {
        "bulk_pending_items": [
            {
                "track_id": "trk_review_1",
                "current_title": "Original Title",
                "current_artist": "Original Artist",
                "proposed_title": "Proposed Title",
                "proposed_artist": "Proposed Artist",
                "proposed_album": "Proposed Album",
                "proposed_meta": {},
                "confidence": 0.95,
            }
        ],
    }
    rev_skip_data = SongsBulkReviewCallback(action="skip_one", index=0)
    await handle_bulk_review_callback(
        rev_cb,
        rev_skip_data,
        current_user=user,
        governance_repository=cat_repo,
        organizer=organizer,
        state=state,
    )

    # Review callback: discard_one item 0
    state.get_data.return_value = {
        "bulk_pending_items": [
            {
                "track_id": "trk_review_1",
                "current_title": "Original Title",
                "current_artist": "Original Artist",
                "proposed_title": "Proposed Title",
                "proposed_artist": "Proposed Artist",
                "proposed_album": "Proposed Album",
                "proposed_meta": {},
                "confidence": 0.95,
            }
        ],
    }
    rev_discard_data = SongsBulkReviewCallback(action="discard_one", index=0)
    await handle_bulk_review_callback(
        rev_cb,
        rev_discard_data,
        current_user=user,
        governance_repository=cat_repo,
        organizer=organizer,
        state=state,
    )

    # Review callback: apply_all
    state.get_data.return_value = {
        "bulk_pending_items": [
            {
                "track_id": "trk_review_1",
                "current_title": "Original Title",
                "current_artist": "Original Artist",
                "proposed_title": "Proposed Title",
                "proposed_artist": "Proposed Artist",
                "proposed_album": "Proposed Album",
                "proposed_meta": {
                    "title": "Proposed Title",
                    "artist": "Proposed Artist",
                    "album": "Proposed Album",
                    "year": 2020,
                },
                "confidence": 0.95,
            }
        ],
    }
    rev_all_data = SongsBulkReviewCallback(action="apply_all", index=0)
    await handle_bulk_review_callback(
        rev_cb,
        rev_all_data,
        current_user=user,
        governance_repository=cat_repo,
        organizer=organizer,
        state=state,
    )

    # Review callback: discard_all
    state.get_data.return_value = {
        "bulk_pending_items": [
            {
                "track_id": "trk_review_1",
                "current_title": "Original Title",
                "current_artist": "Original Artist",
                "proposed_title": "Proposed Title",
                "proposed_artist": "Proposed Artist",
                "proposed_album": "Proposed Album",
                "proposed_meta": {},
                "confidence": 0.95,
            }
        ],
    }
    rev_disc_all_data = SongsBulkReviewCallback(action="discard_all", index=0)
    await handle_bulk_review_callback(
        rev_cb,
        rev_disc_all_data,
        current_user=user,
        governance_repository=cat_repo,
        organizer=organizer,
        state=state,
    )
