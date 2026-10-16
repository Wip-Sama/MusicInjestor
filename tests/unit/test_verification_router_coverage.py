"""Comprehensive unit tests covering verification router actions, inputs, and dry-run editor."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Document, Message, PhotoSize, User

from src.bot.routers.verification import (
    _can_resolve_job,
    _parse_job_metadata_input,
    format_job_edit_dry_run_card,
    handle_job_cover_input,
    handle_job_lyrics_input,
    handle_job_metadata_input,
    handle_manual_search_query,
    handle_verification_callback,
    parse_verification_callback,
)
from src.bot.states import IngestionStates
from src.core.models import (
    IngestionJob,
    IngestionResult,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
    UserAccount,
    UserRole,
)


def test_parse_verification_callback_validation() -> None:
    """Verify callback parsing edge cases and errors."""
    with pytest.raises(ValueError, match="Malformed verification callback"):
        parse_verification_callback("invalid:data")

    with pytest.raises(ValueError, match="Missing candidate index"):
        parse_verification_callback("verify:select:job123")

    action, job_id, idx = parse_verification_callback("verify:select:job123:2")
    assert action == "select"
    assert job_id == "job123"
    assert idx == 2

    action, job_id, idx = parse_verification_callback("verify:cancel:job123")
    assert action == "cancel"
    assert idx is None


def test_can_resolve_job_permissions() -> None:
    """Verify _can_resolve_job logic for own vs other user's jobs."""
    assert _can_resolve_job(None, None) is True

    owner = UserAccount(telegram_id=100, display_name="Owner", role=UserRole.UPLOADER)
    other = UserAccount(telegram_id=200, display_name="Other", role=UserRole.UPLOADER)
    mod = UserAccount(telegram_id=300, display_name="Mod", role=UserRole.MODERATOR)

    job_of_100 = IngestionJob(job_id="j1", source_path=Path("song.mp3"), user_id=100)

    assert _can_resolve_job(owner, job_of_100) is True
    assert _can_resolve_job(other, job_of_100) is False
    assert _can_resolve_job(mod, job_of_100) is True


def test_update_proposal_from_input_all_fields() -> None:
    """Verify _update_proposal_from_input parses single fields and complex multi-field inputs."""
    prop: dict[str, object] = {}

    # 1. title
    p1 = _parse_job_metadata_input("New Title", "title", prop)
    assert p1["new_title"] == "New Title"

    # 2. artist (normalizes CV / character tags)
    p2 = _parse_job_metadata_input("Character (CV: Real Artist)", "artist", prop)
    assert "Real Artist" in str(p2["new_artist"])

    # 3. album
    p3 = _parse_job_metadata_input("New Album", "album", prop)
    assert p3["new_album"] == "New Album"

    # 4. year (4 digits vs non-4 digits)
    p4 = _parse_job_metadata_input("Released in 2024", "year", prop)
    assert p4["new_year"] == 2024

    p4_bad = _parse_job_metadata_input("invalid year", "year", prop)
    assert "new_year" not in p4_bad

    # 5. track number
    p5 = _parse_job_metadata_input("Track #07", "track", prop)
    assert p5["new_track_number"] == 7

    # 6. multi field key-values
    p6 = _parse_job_metadata_input(
        "artist: Best Singer | title: Hit Song | album: Great Record | year: 2025 | track: 3",
        "multi",
        prop,
    )
    assert p6["new_artist"] == "Best Singer"
    assert p6["new_title"] == "Hit Song"
    assert p6["new_album"] == "Great Record"
    assert p6["new_year"] == 2025
    assert p6["new_track_number"] == 3

    # 7. multi field dash separated
    p7 = _parse_job_metadata_input("Artist Name - Track Title", "multi", prop)
    assert p7["new_artist"] == "Artist Name"
    assert p7["new_title"] == "Track Title"

    # 8. multi field single string fallback
    p8 = _parse_job_metadata_input("Just Song Name", "multi", prop)
    assert p8["new_title"] == "Just Song Name"


def test_format_job_edit_dry_run_card(tmp_path: Path) -> None:
    """Verify format_job_edit_dry_run_card with custom lyrics and cover art."""
    dummy_audio = tmp_path / "song.flac"
    dummy_audio.write_bytes(b"FLAC")
    job = IngestionJob(
        job_id="job_dry",
        source_path=dummy_audio,
        original_filename="song.flac",
    )
    proposal = {
        "new_title": "Custom Title",
        "new_artist": "Custom Artist",
        "new_album": "Custom Album",
        "new_year": 2024,
        "new_track_number": 2,
        "new_lyrics": "[00:10.00] Line one\n[00:15.00] Line two",
        "new_cover_size": 20480,
    }
    organizer = MagicMock()
    organizer.build_target_audio_path.return_value = tmp_path / "target.flac"

    card = format_job_edit_dry_run_card(job, proposal, organizer)
    assert "Queued Job Tag Editor" in card
    assert "Custom Title" in card
    assert "Synced LRC" in card
    assert "Custom image (20 KB)" in card


@pytest.mark.anyio
async def test_verification_callback_select_and_cancel(tmp_path: Path) -> None:
    """Verify candidate selection, completion, and cancellation branches."""
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=100, user_id=100)
    state = FSMContext(storage=storage, key=key)

    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"FLAC")
    cand = MetadataCandidate(
        metadata=TrackMetadata(title="Song", artist="Artist", album="Album"),
        confidence=0.85,
    )
    job = IngestionJob(
        job_id="job_sel",
        source_path=dummy_audio,
        user_id=100,
        candidates=[cand],
    )

    pipeline = MagicMock()
    pipeline.get_job = AsyncMock(return_value=job)
    worker = MagicMock()
    worker.record_confirmed_completion = AsyncMock()
    worker.cancel_job = AsyncMock()

    res = IngestionResult(job=job, tracks_ingested=1)
    pipeline.confirm_candidate = AsyncMock(return_value=res)

    user = UserAccount(telegram_id=100, display_name="Test", role=UserRole.UPLOADER)

    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.from_user = User(id=100, is_bot=False, first_name="Test")
    cb.answer = AsyncMock()

    # 1. Select candidate out of range
    cb.data = "verify:select:job_sel:5"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    assert "out of range" in cb.answer.call_args[0][0]

    # 2. Select valid candidate
    cb.data = "verify:select:job_sel:0"
    await handle_verification_callback(
        cb, state=state, current_user=user, pipeline=pipeline, queue_worker=worker
    )
    assert worker.record_confirmed_completion.called

    # 3. Cancel with worker
    cb.data = "verify:cancel:job_sel"
    await handle_verification_callback(
        cb, state=state, current_user=user, pipeline=pipeline, queue_worker=worker
    )
    assert worker.cancel_job.called


@pytest.mark.anyio
async def test_verification_callback_auto_lyrics_and_enrich(tmp_path: Path) -> None:
    """Verify auto_lyrics and auto_enrich actions."""
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=100, user_id=100)
    state = FSMContext(storage=storage, key=key)

    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"FLAC")
    cand = MetadataCandidate(
        metadata=TrackMetadata(title="Song", artist="Artist", album="Album"),
        confidence=0.85,
    )
    job = IngestionJob(
        job_id="job_auto",
        source_path=dummy_audio,
        user_id=100,
        candidates=[cand],
    )

    pipeline = MagicMock()
    pipeline.get_job = AsyncMock(return_value=job)

    lyrics_client = MagicMock()
    lyrics_client.fetch_lyrics = AsyncMock(return_value=LyricsData(synced_lyrics="[00:01.00] Auto lyrics"))
    pipeline._lyrics = lyrics_client

    meta_svc = MagicMock()
    cand_enriched = MetadataCandidate(
        metadata=TrackMetadata(
            title="Enriched Song",
            artist="Enriched Artist",
            album="Enriched Album",
            year=2024,
            recording_mbid="rec123",
            release_mbid="rel123",
        ),
        confidence=0.98,
    )
    meta_svc.find_candidates = AsyncMock(return_value=[cand_enriched])
    pipeline._metadata = meta_svc

    user = UserAccount(telegram_id=100, display_name="Test", role=UserRole.UPLOADER)
    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.from_user = User(id=100, is_bot=False, first_name="Test")
    cb.answer = AsyncMock()

    # 1. Auto lyrics
    cb.data = "verify:auto_lyrics:job_auto"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    data = await state.get_data()
    assert data["job_edit_proposal"]["new_lyrics"] == "[00:01.00] Auto lyrics"

    # 2. Auto enrich
    cb.data = "verify:auto_enrich:job_auto"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    data = await state.get_data()
    assert data["job_edit_proposal"]["new_title"] == "Enriched Song"


@pytest.mark.anyio
async def test_verification_callback_apply_edit(tmp_path: Path) -> None:
    """Verify apply_edit completes custom ingestion with proposed metadata."""
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=100, user_id=100)
    state = FSMContext(storage=storage, key=key)

    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"FLAC")
    job = IngestionJob(
        job_id="job_apply",
        source_path=dummy_audio,
        user_id=100,
    )

    pipeline = MagicMock()
    pipeline.get_job = AsyncMock(return_value=job)
    organizer = MagicMock()
    organizer.is_reconciling = False
    pipeline.organizer = organizer

    res = IngestionResult(job=job, tracks_ingested=1)
    pipeline.complete_custom_ingestion = AsyncMock(return_value=res)

    await state.set_state(IngestionStates.awaiting_job_metadata_input)
    await state.update_data(
        job_id="job_apply",
        job_edit_proposal={
            "new_title": "Applied Title",
            "new_artist": "Applied Artist",
            "new_album": "Applied Album",
            "new_year": 2025,
            "new_track_number": 1,
            "new_lyrics": "Sample plain lyrics",
        },
    )

    user = UserAccount(telegram_id=100, display_name="Test", role=UserRole.UPLOADER)
    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.from_user = User(id=100, is_bot=False, first_name="Test")
    cb.answer = AsyncMock()

    cb.data = "verify:apply_edit:job_apply"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    assert pipeline.complete_custom_ingestion.called


@pytest.mark.anyio
async def test_handle_job_cover_and_lyrics_input(tmp_path: Path) -> None:
    """Verify handling photo/document cover art and lyrics document inputs."""
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=100, user_id=100)
    state = FSMContext(storage=storage, key=key)

    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"FLAC")
    job = IngestionJob(job_id="job_media_in", source_path=dummy_audio, user_id=100)

    pipeline = MagicMock()
    pipeline.get_job = AsyncMock(return_value=job)
    organizer = MagicMock()
    pipeline.organizer = organizer

    user = UserAccount(telegram_id=100, display_name="User", role=UserRole.UPLOADER)

    # 1. Cover with Photo input
    await state.set_state(IngestionStates.awaiting_job_cover_input)
    await state.update_data(job_id="job_media_in", job_edit_proposal={})

    cover_file = tmp_path / "cover.jpg"
    cover_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    tg_file = MagicMock()
    tg_file.file_path = str(cover_file)
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=tg_file)

    msg_photo = MagicMock(spec=Message)
    msg_photo.photo = [PhotoSize(file_id="p1", file_unique_id="pu1", width=100, height=100)]
    msg_photo.document = None
    msg_photo.text = None
    msg_photo.bot = bot
    msg_photo.from_user = User(id=100, is_bot=False, first_name="User")
    msg_photo.answer = AsyncMock()

    await handle_job_cover_input(msg_photo, state=state, current_user=user, pipeline=pipeline)
    data = await state.get_data()
    assert "new_cover_hex" in data["job_edit_proposal"]

    # 2. Lyrics with text
    await state.set_state(IngestionStates.awaiting_job_lyrics_input)
    msg_lyr = MagicMock(spec=Message)
    msg_lyr.text = "[00:05.00] New lyrics entered directly"
    msg_lyr.document = None
    msg_lyr.from_user = User(id=100, is_bot=False, first_name="User")
    msg_lyr.answer = AsyncMock()

    await handle_job_lyrics_input(msg_lyr, state=state, current_user=user, pipeline=pipeline)
    data2 = await state.get_data()
    assert "[00:05.00]" in data2["job_edit_proposal"]["new_lyrics"]


@pytest.mark.anyio
async def test_verification_callback_field_manual_prompts_and_manual_search(tmp_path: Path) -> None:
    """Verify manual search, field prompts, and manual query search execution."""
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=100, user_id=100)
    state = FSMContext(storage=storage, key=key)

    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"FLAC")
    cand = MetadataCandidate(
        metadata=TrackMetadata(title="Song", artist="Artist", album="Album"),
        confidence=0.85,
    )
    job = IngestionJob(
        job_id="job_fld",
        source_path=dummy_audio,
        user_id=100,
        candidates=[cand],
    )

    pipeline = MagicMock()
    pipeline.get_job = AsyncMock(return_value=job)
    user = UserAccount(telegram_id=100, display_name="Test", role=UserRole.UPLOADER)

    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    msg.answer = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.from_user = User(id=100, is_bot=False, first_name="Test")
    cb.answer = AsyncMock()

    # 1. verify:manual:job_fld
    cb.data = "verify:manual:job_fld"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    assert await state.get_state() == IngestionStates.awaiting_manual_query

    # 2. verify:field:job_fld:artist
    cb.data = "verify:field:job_fld:artist"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    assert await state.get_state() == IngestionStates.awaiting_job_metadata_input

    # 3. verify:cover:job_fld
    cb.data = "verify:cover:job_fld"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    assert await state.get_state() == IngestionStates.awaiting_job_cover_input

    # 4. verify:lyrics:job_fld
    cb.data = "verify:lyrics:job_fld"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    assert await state.get_state() == IngestionStates.awaiting_job_lyrics_input

    # 5. handle_manual_search_query
    await state.set_state(IngestionStates.awaiting_manual_query)
    await state.update_data(job_id="job_fld")
    msg_q = MagicMock(spec=Message)
    msg_q.text = "https://musicbrainz.org/release/12345678-1234-1234-1234-123456789abc"
    msg_q.from_user = User(id=100, is_bot=False, first_name="Test")
    msg_q.answer = AsyncMock()

    res = IngestionResult(job=job, requires_user_confirmation=True)
    pipeline.search_manual_candidates = AsyncMock(return_value=res)

    await handle_manual_search_query(msg_q, state=state, current_user=user, pipeline=pipeline)
    assert pipeline.search_manual_candidates.called

    # 6. verify:back:job_fld
    cb.data = "verify:back:job_fld"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    assert await state.get_state() == IngestionStates.awaiting_confirmation

    # 7. Document lyrics input (.lrc)
    await state.set_state(IngestionStates.awaiting_job_lyrics_input)
    lrc_file = tmp_path / "song.lrc"
    lrc_file.write_text("[00:01.00] Synced line", encoding="utf-8")
    tg_lrc = MagicMock()
    tg_lrc.file_path = str(lrc_file)
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=tg_lrc)

    doc_lrc = MagicMock(spec=Document)
    doc_lrc.file_id = "doc_lrc_1"
    doc_lrc.file_name = "song.lrc"
    msg_doc_lyr = MagicMock(spec=Message)
    msg_doc_lyr.document = doc_lrc
    msg_doc_lyr.photo = None
    msg_doc_lyr.text = None
    msg_doc_lyr.bot = bot
    msg_doc_lyr.from_user = User(id=100, is_bot=False, first_name="Test")
    msg_doc_lyr.answer = AsyncMock()

    await handle_job_lyrics_input(msg_doc_lyr, state=state, current_user=user, pipeline=pipeline)
    data = await state.get_data()
    assert "[00:01.00]" in data["job_edit_proposal"]["new_lyrics"]

    # 8. Document cover input (.png)
    await state.set_state(IngestionStates.awaiting_job_cover_input)
    png_file = tmp_path / "cover.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    tg_png = MagicMock()
    tg_png.file_path = str(png_file)
    bot.get_file = AsyncMock(return_value=tg_png)

    doc_png = MagicMock(spec=Document)
    doc_png.file_id = "doc_png_1"
    doc_png.file_name = "cover.png"
    doc_png.mime_type = "image/png"
    msg_doc_cov = MagicMock(spec=Message)
    msg_doc_cov.document = doc_png
    msg_doc_cov.photo = None
    msg_doc_cov.text = None
    msg_doc_cov.bot = bot
    msg_doc_cov.from_user = User(id=100, is_bot=False, first_name="Test")
    msg_doc_cov.answer = AsyncMock()

    await handle_job_cover_input(msg_doc_cov, state=state, current_user=user, pipeline=pipeline)
    data2 = await state.get_data()
    assert data2["job_edit_proposal"]["new_cover_mime"] == "image/png"


@pytest.mark.anyio
async def test_verification_callback_link_search_and_input(tmp_path: Path) -> None:
    """Verify link search action prompt and metadata resolution from URL input."""
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=100, user_id=100)
    state = FSMContext(storage=storage, key=key)

    dummy_audio = tmp_path / "track.flac"
    dummy_audio.write_bytes(b"FLAC")
    cand = MetadataCandidate(
        metadata=TrackMetadata(
            title="Link Song",
            artist="Link Artist",
            album="Link Album",
            year=2024,
            track_number=3,
            recording_mbid="rec-1234",
            release_mbid="rel-5678",
        ),
        confidence=0.98,
    )
    job = IngestionJob(
        job_id="job_link",
        source_path=dummy_audio,
        user_id=100,
        candidates=[cand],
    )

    pipeline = MagicMock()
    pipeline.get_job = AsyncMock(return_value=job)
    organizer = MagicMock()
    organizer.build_target_audio_path = MagicMock(return_value=tmp_path / "out.flac")
    pipeline.organizer = organizer

    lyrics_client = MagicMock()
    lyrics_client.fetch_lyrics = AsyncMock(return_value=LyricsData(synced_lyrics="[00:02.00] Link Synced"))
    pipeline._lyrics = lyrics_client

    user = UserAccount(telegram_id=100, display_name="Test", role=UserRole.UPLOADER)
    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    msg.answer = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.from_user = User(id=100, is_bot=False, first_name="Test")
    cb.answer = AsyncMock()

    # 1. Trigger verify:link_search:job_link
    cb.data = "verify:link_search:job_link"
    await handle_verification_callback(cb, state=state, current_user=user, pipeline=pipeline)
    assert await state.get_state() == IngestionStates.awaiting_job_metadata_input
    data = await state.get_data()
    assert data.get("job_edit_field") == "link"

    # 2. Provide a valid MusicBrainz URL
    res = IngestionResult(job=job, requires_user_confirmation=True)
    pipeline.search_manual_candidates = AsyncMock(return_value=res)

    msg_link = MagicMock(spec=Message)
    msg_link.text = "https://musicbrainz.org/release/12345678-1234-1234-1234-123456789abc"
    msg_link.from_user = User(id=100, is_bot=False, first_name="Test")
    msg_link.answer = AsyncMock()

    await handle_job_metadata_input(msg_link, state=state, current_user=user, pipeline=pipeline)
    assert pipeline.search_manual_candidates.called
    updated_data = await state.get_data()
    proposal = updated_data["job_edit_proposal"]
    assert proposal["new_title"] == "Link Song"
    assert proposal["new_artist"] == "Link Artist"
    assert proposal["new_album"] == "Link Album"
    assert proposal["new_year"] == 2024
    assert proposal["new_track_number"] == 3
    assert proposal["new_lyrics"] == "[00:02.00] Link Synced"
    assert msg_link.answer.called

    # 3. No matches found for link input
    empty_job = IngestionJob(job_id="job_link", source_path=dummy_audio, user_id=100, candidates=[])
    res_empty = IngestionResult(job=empty_job, requires_user_confirmation=True)
    pipeline.search_manual_candidates = AsyncMock(return_value=res_empty)
    await state.update_data(job_edit_field="link")

    msg_none = MagicMock(spec=Message)
    msg_none.text = "https://musicbrainz.org/release/00000000-0000-0000-0000-000000000000"
    msg_none.from_user = User(id=100, is_bot=False, first_name="Test")
    msg_none.answer = AsyncMock()

    await handle_job_metadata_input(msg_none, state=state, current_user=user, pipeline=pipeline)
    call_args = msg_none.answer.call_args[0][0]
    assert "No Matches Found" in call_args
