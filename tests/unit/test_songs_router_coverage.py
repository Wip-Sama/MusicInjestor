"""Comprehensive unit tests covering songs router handlers, callbacks, FSM flows, and dry-run editing."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, User

from src.bot.keyboards.songs import SongsToggleFieldCallback, SongsTrackCallback
from src.bot.routers.songs import (
    _can_access_songs,
    _can_edit_songs,
    _parse_metadata_edit_input,
    handle_cover_edit_input,
    handle_custom_research_input,
    handle_dry_run_unexpected_text,
    handle_import_payload_input,
    handle_lyrics_edit_input,
    handle_metadata_edit_input,
    handle_song_field_toggle,
    handle_songs_callback,
    handle_songs_command,
    handle_songs_search_text,
)
from src.bot.states import SongManagementStates
from src.core.models import (
    DiscoveredRelease,
    IngestionJob,
    LibraryRescanReport,
    LibraryTrackRecord,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
    UserAccount,
    UserRole,
)
from src.core.organizer import LibraryOrganizer


def _make_context(bot: AsyncMock, chat_id: int = 12345, user_id: int = 12345) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=bot.id or 1, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _make_message(text: str = "/songs", user_id: int = 12345) -> Message:
    bot = AsyncMock()
    bot.id = 1
    chat = Chat(id=user_id, type="private")
    from_user = User(id=user_id, is_bot=False, first_name="TestUser")
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.message_id = 100
    msg.chat = chat
    msg.from_user = from_user
    msg.bot = bot
    msg.photo = None
    msg.document = None
    msg.answer = AsyncMock()
    msg.answer_audio = AsyncMock()
    msg.answer_document = AsyncMock()
    msg.answer_photo = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _make_callback(data: str, user_id: int = 12345) -> CallbackQuery:
    bot = AsyncMock()
    bot.id = 1
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.id = "cb_songs_1"
    cb.bot = bot
    cb.from_user = User(id=user_id, is_bot=False, first_name="TestUser")
    msg = _make_message(user_id=user_id)
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


def _make_track(track_id: str, tmp_path: Path) -> LibraryTrackRecord:
    audio_path = tmp_path / f"{track_id}.flac"
    audio_path.write_bytes(b"FLAC DATA")
    return LibraryTrackRecord(
        id=track_id,
        job_id="job_001",
        uploaded_by=12345,
        storage_path=audio_path,
        fingerprint="fp12345",
        duration_seconds=180.0,
        artist="Test Artist",
        album="Test Album",
        title="Test Title",
        year=2024,
        track_number=1,
    )


@pytest.mark.anyio
async def test_can_access_and_edit_permissions():
    guest = UserAccount(telegram_id=1, display_name="Guest", role=UserRole.GUEST)
    listener = UserAccount(telegram_id=2, display_name="Listener", role=UserRole.LISTENER)
    uploader = UserAccount(telegram_id=3, display_name="Uploader", role=UserRole.UPLOADER)
    admin = UserAccount(telegram_id=4, display_name="Admin", role=UserRole.ADMIN)

    assert _can_access_songs(None) is True
    assert _can_access_songs(guest) is False
    assert _can_access_songs(listener) is True
    assert _can_access_songs(uploader) is True

    assert _can_edit_songs(None) is False
    assert _can_edit_songs(listener) is False
    assert _can_edit_songs(uploader) is True
    assert _can_edit_songs(admin) is True


@pytest.mark.anyio
async def test_parse_metadata_edit_input_all_fields(tmp_path: Path):
    track = _make_track("trk1", tmp_path)

    p_artist = _parse_metadata_edit_input("New Artist", "artist", track)
    assert p_artist["new_artist"] == "New Artist"

    p_album = _parse_metadata_edit_input("New Album", "album", track)
    assert p_album["new_album"] == "New Album"

    p_year = _parse_metadata_edit_input("2025", "year", track)
    assert p_year["new_year"] == 2025

    p_track = _parse_metadata_edit_input("5", "track", track)
    assert p_track["new_track_number"] == 5

    p_multi_bar = _parse_metadata_edit_input(
        "artist: Best Artist | title: Hit Song | album: Great Record | year: 2023 | track: 2",
        "multi",
        track,
    )
    assert p_multi_bar["new_artist"] == "Best Artist"
    assert p_multi_bar["new_title"] == "Hit Song"
    assert p_multi_bar["new_album"] == "Great Record"
    assert p_multi_bar["new_year"] == 2023
    assert p_multi_bar["new_track_number"] == 2

    p_multi_dash = _parse_metadata_edit_input("Singer - Awesome Track", "multi", track)
    assert p_multi_dash["new_artist"] == "Singer"
    assert p_multi_dash["new_title"] == "Awesome Track"


@pytest.mark.anyio
async def test_handle_songs_command_and_search_text(tmp_path: Path):
    uploader = UserAccount(telegram_id=12345, display_name="Uploader", role=UserRole.UPLOADER)
    msg = _make_message("/songs")
    bot = msg.bot
    state = _make_context(bot)
    gov_repo = AsyncMock()
    track = _make_track("trk1", tmp_path)
    gov_repo.count_library_tracks.return_value = 1
    gov_repo.list_library_tracks.return_value = [track]

    # /songs command
    await handle_songs_command(msg, state, current_user=uploader, governance_repository=gov_repo)
    msg.answer.assert_awaited()
    assert "Music Library" in msg.answer.call_args[0][0]

    # Search text matches 0 but direct track exists
    msg.text = "trk1"
    msg.answer.reset_mock()
    gov_repo.count_library_tracks.return_value = 0
    gov_repo.get_library_track.return_value = track
    gov_repo.get_user.return_value = uploader
    await handle_songs_search_text(msg, state, current_user=uploader, governance_repository=gov_repo)
    assert "Test Title" in msg.answer.call_args[0][0]

    # Search text matches 0 and matches job
    msg.text = "job_001"
    msg.answer.reset_mock()
    gov_repo.get_library_track.return_value = None
    cat_repo = AsyncMock()
    job = IngestionJob(job_id="job_001", source_path=tmp_path / "song.mp3", user_id=12345)
    cat_repo.get_job.return_value = job
    await handle_songs_search_text(
        msg, state, current_user=uploader, governance_repository=gov_repo, catalog_repository=cat_repo
    )
    assert "job_001" in msg.answer.call_args[0][0]

    # Search text matches nothing
    msg.text = "nonexistent"
    msg.answer.reset_mock()
    cat_repo.get_job.return_value = None
    await handle_songs_search_text(
        msg, state, current_user=uploader, governance_repository=gov_repo, catalog_repository=cat_repo
    )
    assert "No tracks found" in msg.answer.call_args[0][0]


@pytest.mark.anyio
async def test_handle_songs_callback_navigation_and_export(tmp_path: Path):
    uploader = UserAccount(telegram_id=12345, display_name="Uploader", role=UserRole.UPLOADER)
    gov_repo = AsyncMock()
    pipeline = AsyncMock()
    track = _make_track("trk1", tmp_path)
    gov_repo.count_library_tracks.return_value = 1
    gov_repo.list_library_tracks.return_value = [track]
    gov_repo.get_library_track.return_value = track

    cb = _make_callback("songs:list")
    state = _make_context(cb.bot)

    # songs:list
    await handle_songs_callback(
        cb, state, current_user=uploader, governance_repository=gov_repo, pipeline=pipeline
    )
    cb.message.edit_text.assert_awaited()

    # songs:sort_menu
    cb = _make_callback("songs:sort_menu")
    await handle_songs_callback(
        cb, state, current_user=uploader, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Sort Music Library" in cb.message.edit_text.call_args[0][0]

    # songs:clear_search
    cb = _make_callback("songs:clear_search")
    await handle_songs_callback(
        cb, state, current_user=uploader, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Music Library" in cb.message.edit_text.call_args[0][0]

    # songs:search_prompt
    cb = _make_callback("songs:search_prompt")
    await handle_songs_callback(
        cb, state, current_user=uploader, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Search Music Library" in cb.message.edit_text.call_args[0][0]

    # songs:export_all
    cb = _make_callback("songs:export_all")
    organizer = LibraryOrganizer(tmp_path)
    pipeline._organizer = organizer
    organizer.export_song_bundle = AsyncMock(return_value={"id": "trk1", "title": "Test Title"})
    await handle_songs_callback(
        cb, state, current_user=uploader, governance_repository=gov_repo, pipeline=pipeline
    )
    cb.message.answer_document.assert_awaited()


@pytest.mark.anyio
async def test_handle_songs_callback_rescan_and_bulk(tmp_path: Path):
    admin = UserAccount(telegram_id=12345, display_name="Admin", role=UserRole.ADMIN)
    gov_repo = AsyncMock()
    pipeline = AsyncMock()
    track = _make_track("trk1", tmp_path)
    gov_repo.list_library_tracks.return_value = [track]
    gov_repo.count_library_tracks.return_value = 1

    organizer = LibraryOrganizer(tmp_path)
    pipeline._organizer = organizer
    pipeline._metadata = AsyncMock()
    pipeline._lyrics = AsyncMock()

    # rescan_dryrun without changes
    cb = _make_callback("songs:rescan_dryrun")
    state = _make_context(cb.bot)
    clean_report = LibraryRescanReport(
        added_tracks=(), updated_tracks=(), removed_tracks=(), unchanged_count=1
    )
    organizer.scan_library_filesystem = AsyncMock(return_value=clean_report)
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    cb.message.edit_text.assert_awaited()

    # rescan_dryrun with changes
    dirty_report = LibraryRescanReport(
        added_tracks=(track,), updated_tracks=(), removed_tracks=(), unchanged_count=0
    )
    organizer.scan_library_filesystem = AsyncMock(return_value=dirty_report)
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Rescan" in cb.message.edit_text.call_args[0][0]

    # rescan_apply
    cb = _make_callback("songs:rescan_apply")
    organizer.apply_rescan_report = AsyncMock()
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    organizer.apply_rescan_report.assert_awaited()

    # bulk_menu
    cb = _make_callback("songs:bulk_menu")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Bulk Metadata Re-Search" in cb.message.edit_text.call_args[0][0]

    # bulk_plan_all
    cb = _make_callback("songs:bulk_plan_all")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Bulk Re-Search Plan" in cb.message.edit_text.call_args[0][0]

    # bulk_run_all
    cb = _make_callback("songs:bulk_run_all")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Bulk Re-Search Completed" in cb.message.edit_text.call_args[0][0]


@pytest.mark.anyio
async def test_handle_songs_callback_song_actions(tmp_path: Path):
    admin = UserAccount(telegram_id=12345, display_name="Admin", role=UserRole.ADMIN)
    gov_repo = AsyncMock()
    pipeline = AsyncMock()
    track = _make_track("trk1", tmp_path)
    gov_repo.get_library_track.return_value = track
    gov_repo.get_user.return_value = admin
    gov_repo.list_entity_audit_logs.return_value = []
    gov_repo.list_album_tracks.return_value = [track]

    organizer = LibraryOrganizer(tmp_path)
    pipeline._organizer = organizer
    organizer.read_track_lyrics = AsyncMock(return_value=LyricsData(synced_lyrics="[00:01.00] Line"))
    organizer.export_song_bundle = AsyncMock(return_value={"id": "trk1"})

    cb = _make_callback("songs:view:trk1")
    state = _make_context(cb.bot)

    # view
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Test Title" in cb.message.edit_text.call_args[0][0]

    # history
    cb = _make_callback("songs:history:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "History" in cb.message.edit_text.call_args[0][0]

    # get_track
    cb = _make_callback("songs:get_track:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    cb.message.answer_audio.assert_awaited()

    # get_album
    cb = _make_callback("songs:get_album:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert cb.message.answer_audio.await_count >= 1

    # edit_meta
    cb = _make_callback("songs:edit_meta:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Edit Metadata" in cb.message.edit_text.call_args[0][0]

    # field
    cb = _make_callback("songs:field:trk1:title")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Enter New Title" in cb.message.edit_text.call_args[0][0]

    # edit_cover
    cb = _make_callback("songs:edit_cover:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Edit Cover Art" in cb.message.edit_text.call_args[0][0]

    # edit_lyrics
    cb = _make_callback("songs:edit_lyrics:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Edit Lyrics" in cb.message.edit_text.call_args[0][0]

    # research_lyrics
    cb = _make_callback("songs:research_lyrics:trk1")
    pipeline._lyrics = AsyncMock()
    pipeline._lyrics.fetch_lyrics = AsyncMock(return_value=LyricsData(synced_lyrics="[00:02.00] Auto lyrics"))
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Dry-Run Preview" in cb.message.edit_text.call_args[0][0]

    # research_lyrics not found
    cb = _make_callback("songs:research_lyrics:trk1")
    pipeline._lyrics.fetch_lyrics = AsyncMock(return_value=None)
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "No Lyrics Found" in cb.message.edit_text.call_args[0][0]

    # research
    cb = _make_callback("songs:research:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Re-Search Song Data" in cb.message.edit_text.call_args[0][0]

    # research_custom
    cb = _make_callback("songs:research_custom:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Custom Re-Search" in cb.message.edit_text.call_args[0][0]

    # research_auto
    cb = _make_callback("songs:research_auto:trk1")
    pipeline._metadata = AsyncMock()
    meta_cand = MetadataCandidate(
        metadata=TrackMetadata(title="Enriched Title", artist="Enriched Artist", album="Enriched Album"),
        confidence=0.99,
    )
    pipeline._metadata.find_candidates = AsyncMock(return_value=[meta_cand])
    pipeline._metadata.read_local_metadata = AsyncMock(return_value=meta_cand.metadata)
    pipeline._lyrics.fetch_lyrics = AsyncMock(return_value=LyricsData(synced_lyrics="[00:03.00] Enriched"))
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Dry-Run Preview" in cb.message.edit_text.call_args[0][0]

    # export
    cb = _make_callback("songs:export:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    cb.message.answer_document.assert_awaited()

    # import
    cb = _make_callback("songs:import:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert await state.get_state() == SongManagementStates.awaiting_import_payload

    # discard_changes
    cb = _make_callback("songs:discard_changes:trk1")
    await handle_songs_callback(
        cb, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Test Title" in cb.message.edit_text.call_args[0][0]


@pytest.mark.anyio
async def test_handle_metadata_input_and_dry_run_flow(tmp_path: Path):
    admin = UserAccount(telegram_id=12345, display_name="Admin", role=UserRole.ADMIN)
    gov_repo = AsyncMock()
    pipeline = AsyncMock()
    track = _make_track("trk1", tmp_path)
    gov_repo.get_library_track.return_value = track

    organizer = LibraryOrganizer(tmp_path)
    pipeline._organizer = organizer
    organizer.build_target_audio_path = MagicMock(return_value=tmp_path / "new_path.flac")
    organizer.relocate_and_retag_track = AsyncMock(return_value=track.storage_path)
    organizer.read_track_lyrics = AsyncMock(return_value=LyricsData(synced_lyrics="[00:01.00] Line"))
    gov_repo.update_library_track_full = AsyncMock(return_value=track)

    msg = _make_message("New Song Title")
    state = _make_context(msg.bot)
    await state.set_state(SongManagementStates.awaiting_metadata_input)
    await state.update_data(active_song_id="trk1", active_meta_field="title")

    # input metadata
    await handle_metadata_edit_input(msg, state, governance_repository=gov_repo, pipeline=pipeline)
    msg.answer.assert_awaited()
    assert "Dry-Run Preview" in msg.answer.call_args[0][0]

    # toggle field
    cb = _make_callback("songs:toggle_field:trk1:title")
    data = await state.get_data()
    proposal = data.get("pending_song_proposal")
    proposal["proposed_changes"] = {"title": "New Song Title"}
    await state.update_data(pending_song_proposal=proposal)
    cb_data = SongsToggleFieldCallback(track_id="trk1", field="title")
    await handle_song_field_toggle(cb, cb_data, state, governance_repository=gov_repo)
    assert cb.answer.await_count >= 1

    # select_changes & review_proposal
    cb_sel = _make_callback("songs:select_changes:trk1")
    await handle_songs_callback(
        cb_sel, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Select Changes" in cb_sel.message.edit_text.call_args[0][0]

    cb_rev = _make_callback("songs:review_proposal:trk1")
    await handle_songs_callback(
        cb_rev, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Dry-Run Preview" in cb_rev.message.edit_text.call_args[0][0]

    # confirm_apply
    cb_apply = _make_callback("songs:confirm_apply:trk1")
    await handle_songs_callback(
        cb_apply, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    organizer.relocate_and_retag_track.assert_awaited()

    # confirm_apply_selected, apply_meta_only, apply_lyrics_only
    for mode in ("confirm_apply_selected", "apply_meta_only", "apply_lyrics_only"):
        await state.update_data(pending_song_proposal=proposal)
        cb_mode = _make_callback(f"songs:{mode}:trk1")
        await handle_songs_callback(
            cb_mode, state, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
        )
        assert cb_mode.message.edit_text.await_count >= 1


@pytest.mark.anyio
async def test_handle_lyrics_and_cover_input(tmp_path: Path):
    gov_repo = AsyncMock()
    pipeline = AsyncMock()
    track = _make_track("trk1", tmp_path)
    gov_repo.get_library_track.return_value = track

    organizer = LibraryOrganizer(tmp_path)
    pipeline._organizer = organizer
    organizer.build_target_audio_path = MagicMock(return_value=tmp_path / "song.flac")

    # lyrics input text
    msg = _make_message("[00:05.00] New Synced Lyrics")
    state = _make_context(msg.bot)
    await state.set_state(SongManagementStates.awaiting_lyrics_input)
    await state.update_data(active_song_id="trk1")

    await handle_lyrics_edit_input(msg, state, governance_repository=gov_repo, pipeline=pipeline)
    msg.answer.assert_awaited()
    assert "Dry-Run Preview" in msg.answer.call_args[0][0]

    # lyrics input document
    lrc_file = tmp_path / "sample.lrc"
    lrc_file.write_text("[00:01.00] File lyrics", encoding="utf-8")
    msg_doc = _make_message()
    msg_doc.text = None
    msg_doc.document = MagicMock(spec=Document)
    msg_doc.document.file_id = "doc123"
    msg_doc.bot.get_file.return_value = MagicMock(file_path=str(lrc_file))
    await handle_lyrics_edit_input(msg_doc, state, governance_repository=gov_repo, pipeline=pipeline)
    assert "Dry-Run Preview" in msg_doc.answer.call_args[0][0]

    # cover input URL
    msg_cover = _make_message("https://example.com/cover.jpg")
    state_cover = _make_context(msg_cover.bot)
    await state_cover.set_state(SongManagementStates.awaiting_cover_input)
    await state_cover.update_data(active_song_id="trk1")

    # invalid cover
    await handle_cover_edit_input(msg_cover, state_cover, governance_repository=gov_repo, pipeline=pipeline)
    assert "Invalid Cover Image" in msg_cover.answer.call_args[0][0]

    # cover input photo
    img_file = tmp_path / "cover.jpg"
    img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)
    msg_photo = _make_message()
    msg_photo.text = None
    photo_mock = MagicMock(spec=PhotoSize)
    photo_mock.file_id = "photo123"
    msg_photo.photo = [photo_mock]
    msg_photo.bot.get_file.return_value = MagicMock(file_path=str(img_file))
    await handle_cover_edit_input(msg_photo, state_cover, governance_repository=gov_repo, pipeline=pipeline)
    assert "Dry-Run Preview" in msg_photo.answer.call_args[0][0]


@pytest.mark.anyio
async def test_handle_import_and_dry_run_unexpected(tmp_path: Path):
    gov_repo = AsyncMock()
    pipeline = AsyncMock()
    track = _make_track("trk1", tmp_path)
    gov_repo.get_library_track.return_value = track
    organizer = LibraryOrganizer(tmp_path)
    pipeline._organizer = organizer
    organizer.build_target_audio_path = MagicMock(return_value=tmp_path / "song.flac")

    msg = _make_message('{"title": "Imported Title", "artist": "Imported Artist", "album": "Imported Album"}')
    state = _make_context(msg.bot)
    await state.set_state(SongManagementStates.awaiting_import_payload)
    await state.update_data(active_song_id="trk1")

    await handle_import_payload_input(msg, state, governance_repository=gov_repo, pipeline=pipeline)
    msg.answer.assert_awaited()
    assert "Dry-Run Preview" in msg.answer.call_args[0][0]

    # invalid JSON
    msg_bad = _make_message("not json")
    await handle_import_payload_input(msg_bad, state, governance_repository=gov_repo, pipeline=pipeline)
    assert "Invalid JSON Payload" in msg_bad.answer.call_args[0][0]

    # non-dict JSON
    msg_array = _make_message("123")
    await handle_import_payload_input(msg_array, state, governance_repository=gov_repo, pipeline=pipeline)
    assert "Invalid JSON Structure" in msg_array.answer.call_args[0][0]

    # unexpected text when awaiting confirmation
    msg_unexp = _make_message("random text")
    await handle_dry_run_unexpected_text(msg_unexp, state, governance_repository=gov_repo)
    assert "Pending Dry-Run Preview" in msg_unexp.answer.call_args[0][0]


@pytest.mark.anyio
async def test_handle_custom_research_input(tmp_path: Path):
    gov_repo = AsyncMock()
    pipeline = AsyncMock()
    track = _make_track("trk1", tmp_path)
    gov_repo.get_library_track.return_value = track

    organizer = LibraryOrganizer(tmp_path)
    pipeline._organizer = organizer
    organizer.build_target_audio_path = MagicMock(return_value=tmp_path / "song.flac")

    pipeline._metadata = AsyncMock()
    pipeline._lyrics = AsyncMock()
    cand = MetadataCandidate(
        metadata=TrackMetadata(title="Query Title", artist="Query Artist", album="Query Album"),
        confidence=0.95,
    )
    pipeline._metadata.find_candidates = AsyncMock(return_value=[cand])
    pipeline._lyrics.fetch_lyrics = AsyncMock(return_value=LyricsData(plain_lyrics="Plain lyrics"))

    msg = _make_message("Artist - Title | Album")
    state = _make_context(msg.bot)
    await state.set_state(SongManagementStates.awaiting_research_query)
    await state.update_data(active_song_id="trk1")

    await handle_custom_research_input(msg, state, governance_repository=gov_repo, pipeline=pipeline)
    msg.answer.assert_awaited()
    assert "Dry-Run Preview" in msg.answer.call_args[0][0]


@pytest.mark.anyio
async def test_handle_song_action_radar_artist(tmp_path: Path):
    gov_repo = AsyncMock()
    track = replace(_make_track("trk_radar", tmp_path), artist="Saori Hayami")
    gov_repo.get_library_track.return_value = track
    gov_repo.is_artist_followed.return_value = False

    cb = _make_callback("songs:radar_artist:trk_radar")
    cb.data = SongsTrackCallback(action="radar_artist", track_id="trk_radar").pack()
    cb.message.edit_text = AsyncMock()
    state = _make_context(cb.bot)
    user = UserAccount(telegram_id=12345, display_name="Test", role=UserRole.LISTENER)

    # 1. When no releases exist on radar yet -> displays artist follow screen with Follow button
    gov_repo.list_discovered_releases.return_value = []
    await handle_songs_callback(
        cb,
        state,
        current_user=user,
        governance_repository=gov_repo,
    )
    cb.message.edit_text.assert_awaited()
    assert "Saori Hayami" in cb.message.edit_text.call_args[0][0]
    assert "Not Following" in cb.message.edit_text.call_args[0][0]

    # 2. When releases exist on radar -> displays release detail card
    release = DiscoveredRelease(
        id="deezer_99",
        artist_name="Saori Hayami",
        title="Orange Mint",
        release_type="single",
        release_date="2024-01-01",
    )
    gov_repo.list_discovered_releases.return_value = [release]
    await handle_songs_callback(
        cb,
        state,
        current_user=user,
        governance_repository=gov_repo,
    )
    assert "Orange Mint" in cb.message.edit_text.call_args[0][0]
