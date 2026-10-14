"""Unit tests for Telegram Bot auto-lyrics research, dry-run review, and selective field changes."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    SendChatAction,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from src.bot.keyboards import (
    build_lyrics_input_keyboard,
    build_selective_changes_keyboard,
)
from src.bot.middlewares import RbacMiddleware
from src.bot.routers import build_root_router
from src.config.settings import AppSettings
from src.core.models import (
    JobState,
    LibraryTrackRecord,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
    UserRole,
)
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.queue import IngestionQueueWorker
from src.core.rbac import GovernanceService
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository
from tests.unit.test_pipeline import (
    StubExtractor,
    StubFingerprinter,
    StubMetadataProvider,
)


class StubLyricsProvider:
    """Stub lyrics client returning pre-configured lyrics data and tracking search metadata."""

    def __init__(self, lyrics: LyricsData | None = None) -> None:
        self.lyrics = lyrics
        self.calls: list[TrackMetadata] = []

    async def fetch_lyrics(self, metadata: TrackMetadata) -> LyricsData | None:
        self.calls.append(metadata)
        return self.lyrics


@pytest.mark.anyio
async def test_bot_lyrics_input_keyboard_contains_auto_search() -> None:
    """Verify build_lyrics_input_keyboard includes the '⚡ Auto-Search Lyrics' button."""
    kb = build_lyrics_input_keyboard("trk_123")
    assert kb.inline_keyboard
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    auto_btn = next((b for b in all_buttons if "Auto-Search Lyrics" in b.text), None)
    assert auto_btn is not None
    assert auto_btn.callback_data == "songs:research_lyrics:trk_123"


@pytest.mark.anyio
async def test_bot_selective_changes_keyboard_builder() -> None:
    """Verify build_selective_changes_keyboard renders toggle rows, counter button, discard, and back."""
    available = [("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("lyrics", "Lyrics")]
    enabled = {"title", "lyrics"}
    kb = build_selective_changes_keyboard(
        "trk_test_123456",
        enabled_fields=enabled,
        available_fields=available,
    )
    assert kb.inline_keyboard

    # Verify toggle rows
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    title_btn = next((b for b in all_buttons if "Title" in b.text), None)
    assert title_btn is not None
    assert title_btn.text.startswith("✅ [Apply]")

    artist_btn = next((b for b in all_buttons if "Artist" in b.text), None)
    assert artist_btn is not None
    assert artist_btn.text.startswith("⬜ [Keep]")

    # Verify action buttons
    apply_btn = next((b for b in all_buttons if "Apply Selected" in b.text), None)
    assert apply_btn is not None
    assert "(2/4)" in apply_btn.text
    assert apply_btn.callback_data == "songs:confirm_apply_selected:trk_test_123"

    discard_btn = next((b for b in all_buttons if "Discard Changes" in b.text), None)
    assert discard_btn is not None
    assert discard_btn.callback_data == "songs:discard_changes:trk_test_123"


@pytest.mark.anyio
async def test_bot_auto_lyrics_research_toggle_and_selective_apply_lifecycle(
    tmp_path: Path,
    sample_settings: AppSettings,
) -> None:
    """Verify full Telegram bot lifecycle for auto-lyrics lookup, toggling fields, and selective apply."""
    db_path = tmp_path / "bot_lyrics.db"
    db_manager = SqliteDatabaseManager(db_path)
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))

    organizer = LibraryOrganizer(library_root=tmp_path / "lib", enable_network=False)
    lyrics_provider = StubLyricsProvider(
        lyrics=LyricsData(
            synced_lyrics="[00:01.00] Auto fetched bot lyrics line 1\n[00:05.00] Line 2\n",
            plain_lyrics="Auto fetched bot lyrics line 1\nLine 2\n",
        )
    )

    pipeline = IngestionPipeline(
        extractor=StubExtractor(),
        fingerprinter=StubFingerprinter("FP_BOT_LYR"),
        metadata_provider=StubMetadataProvider([]),
        lyrics_provider=lyrics_provider,
        organizer=organizer,
        catalog_repository=repo,
    )
    worker = IngestionQueueWorker(pipeline=pipeline, catalog_repository=repo, governance_repository=repo)

    owner_account = await gov.ensure_user(1001, "Owner User", username="owner_u")
    owner_account = await gov.change_user_role(owner_account, 1001, UserRole.OWNER)

    # Create dummy audio file on disk and library record
    song_dir = tmp_path / "lib" / "Test Artist" / "Test Album"
    song_dir.mkdir(parents=True, exist_ok=True)
    audio_file = song_dir / "01. Test Song.mp3"
    audio_file.write_bytes(b"ID3" + b"\x00" * 50)
    original_lrc = song_dir / "01. Test Song.lrc"
    original_lrc.write_text("[00:01.00] Old original lyrics\n", encoding="utf-8")

    track_record = LibraryTrackRecord(
        id="trk_bot_lyr_1",
        job_id="job_init",
        uploaded_by=1001,
        storage_path=audio_file,
        fingerprint="FP_BOT_LYR",
        duration_seconds=120.0,
        artist="Test Artist",
        album="Test Album",
        title="Test Song",
        year=2022,
        track_number=1,
        has_synced_lyrics=True,
    )
    await repo.upsert_scanned_library_track(track_record)

    # Wire real aiogram Bot and Dispatcher with RbacMiddleware and Root Router
    bot = Bot(token=sample_settings.telegram_bot_token)
    rbac_mw = RbacMiddleware(governance_service=gov, oidc_enabled=False)
    dp = Dispatcher()
    dp.message.outer_middleware(rbac_mw)
    dp.callback_query.outer_middleware(rbac_mw)
    dp["pipeline"] = pipeline
    dp["settings"] = sample_settings
    dp["catalog_repository"] = repo
    dp["governance_repository"] = repo
    dp["governance_service"] = gov
    dp["queue_worker"] = worker
    dp.include_router(build_root_router())

    tg_user = User(id=1001, is_bot=False, first_name="Owner", username="owner_u")
    tg_chat = Chat(id=1001, type="private")

    dispatched_methods: list[TelegramMethod[object]] = []
    screen_state: dict[str, object] = {
        "message_id": 300,
        "text": "",
        "reply_markup": None,
    }
    next_update_id = 1

    async def _fake_make_request(
        _bot: Bot,
        method: TelegramMethod[object],
        timeout: int | None = None,
    ) -> object:
        _ = timeout
        dispatched_methods.append(method)
        if isinstance(method, SendMessage):
            msg_id = int(str(screen_state["message_id"])) + 1
            screen_state["message_id"] = msg_id
            screen_state["text"] = method.text
            screen_state["reply_markup"] = method.reply_markup
            return Message(
                message_id=msg_id,
                date=datetime.now(UTC),
                chat=tg_chat,
                from_user=tg_user,
                text=method.text,
                reply_markup=(
                    method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
                ),
            )
        if isinstance(method, EditMessageText):
            screen_state["text"] = method.text
            screen_state["reply_markup"] = method.reply_markup
            return Message(
                message_id=int(str(screen_state["message_id"])),
                date=datetime.now(UTC),
                chat=tg_chat,
                from_user=tg_user,
                text=method.text,
                reply_markup=method.reply_markup,
            )
        if isinstance(method, (AnswerCallbackQuery, SendChatAction, DeleteMessage)):
            return True
        return True

    bot.session.__dict__["make_request"] = _fake_make_request

    async def _send_user_text(text: str) -> str:
        nonlocal next_update_id
        msg_id = int(str(screen_state["message_id"])) + 1
        screen_state["message_id"] = msg_id
        user_msg = Message(
            message_id=msg_id,
            date=datetime.now(UTC),
            chat=tg_chat,
            from_user=tg_user,
            text=text,
        )
        upd = Update(update_id=next_update_id, message=user_msg)
        next_update_id += 1
        await dp.feed_update(bot, upd)
        return str(screen_state["text"])

    async def _click_button(label_substring: str) -> str:
        nonlocal next_update_id
        markup = screen_state["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup), "No active reply_markup"
        matched_callback: str | None = None
        for row in markup.inline_keyboard:
            for btn in row:
                if label_substring in btn.text and btn.callback_data:
                    matched_callback = btn.callback_data
                    break
            if matched_callback is not None:
                break
        all_labels = [b.text for r in markup.inline_keyboard for b in r]
        assert matched_callback is not None, f"Button {label_substring!r} not found in {all_labels}"

        before_count = len(dispatched_methods)
        active_msg = Message(
            message_id=int(str(screen_state["message_id"])),
            date=datetime.now(UTC),
            chat=tg_chat,
            from_user=tg_user,
            text=str(screen_state["text"]),
            reply_markup=markup,
        )
        cb_query = CallbackQuery(
            id=f"cb-{next_update_id}",
            from_user=tg_user,
            chat_instance="ci-lyrics",
            message=active_msg,
            data=matched_callback,
        )
        upd = Update(update_id=next_update_id, callback_query=cb_query)
        next_update_id += 1
        await dp.feed_update(bot, upd)

        recent = dispatched_methods[before_count:]
        assert any(isinstance(m, AnswerCallbackQuery) for m in recent)
        return str(screen_state["text"])

    try:
        # 1. Open `/songs` -> select song -> open Song Details
        await _send_user_text("/songs")
        text = await _click_button("Test Artist - Test Song")
        assert "Song Details &amp; Actions" in text

        # 2. Click 'Edit Lyrics' -> keyboard provides '⚡ Auto-Search Lyrics'
        text = await _click_button("Edit Lyrics")
        assert "Edit Lyrics" in text

        # 3. Click '⚡ Auto-Search Lyrics' -> triggers LRCLIB search and renders Dry-Run Preview Card
        text = await _click_button("Auto-Search Lyrics")
        assert "Dry-Run Preview" in text
        assert "Synced LRC" in text
        assert len(lyrics_provider.calls) >= 1

        # 4. Dry-run card contains 'Select Changes...', 'Accept All Changes', 'Discard Changes'
        markup = screen_state["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup)
        btn_texts = [btn.text for row in markup.inline_keyboard for btn in row]
        assert any("Accept All Changes" in b for b in btn_texts)
        assert any("Select Changes..." in b for b in btn_texts)
        assert any("Discard Changes" in b for b in btn_texts)

        # 5. Click 'Select Changes...' -> renders Selective Changes screen with toggle buttons
        text = await _click_button("Select Changes...")
        assert "Select Changes" in text
        assert "Choose which changes to apply" in text

        def _btn_texts() -> list[str]:
            m = screen_state["reply_markup"]
            if not isinstance(m, InlineKeyboardMarkup):
                return []
            return [b.text for r in m.inline_keyboard for b in r]

        # Toggle button initially shows '✅ [Apply] Lyrics'
        assert any("✅ [Apply] Lyrics" in t for t in _btn_texts())
        assert any("Apply Selected (1/1)" in t for t in _btn_texts())

        # 6. Click '✅ [Apply] Lyrics' -> toggles to '⬜ [Keep] Lyrics', counter becomes (0/1)
        await _click_button("Lyrics")
        assert any("⬜ [Keep] Lyrics" in t for t in _btn_texts())
        # When 0 fields enabled, Apply Selected button is hidden
        assert not any("Apply Selected" in t for t in _btn_texts())

        # 7. Click '⬜ [Keep] Lyrics' -> toggles back to '✅ [Apply] Lyrics', counter becomes (1/1)
        await _click_button("Lyrics")
        assert any("✅ [Apply] Lyrics" in t for t in _btn_texts())
        assert any("Apply Selected (1/1)" in t for t in _btn_texts())

        text = await _click_button("Apply Selected (1/1)")
        assert "Changes Applied Successfully" in text
        assert "Song Details &amp; Actions" in text
        assert "Lyrics: ✅ Synced" in text

        # Verify .lrc on disk was updated with the auto-searched lyrics
        updated_track = await repo.get_library_track("trk_bot_lyr_1")
        assert updated_track is not None
        assert updated_track.modification_count == 1
        lrc_content = updated_track.storage_path.with_suffix(".lrc").read_text(encoding="utf-8")
        assert "Auto fetched bot lyrics line 1" in lrc_content

        # 9. Test Discard Changes: start another lyrics auto-search, then click 'Discard Changes'
        await _click_button("Edit Lyrics")
        text = await _click_button("Auto-Search Lyrics")
        assert "Dry-Run Preview" in text

        text = await _click_button("Discard Changes")
        assert "Song Details &amp; Actions" in text
        # Modification count remains 1
        refreshed = await repo.get_library_track("trk_bot_lyr_1")
        assert refreshed is not None
        assert refreshed.modification_count == 1
    finally:
        await bot.session.close()


@pytest.mark.anyio
async def test_bot_queued_job_auto_lyrics_action(
    tmp_path: Path,
    sample_settings: AppSettings,
) -> None:
    """Verify queued job Manual Tag Editor has '⚡ Auto-Search Lyrics' and attaches fetched lyrics."""
    db_path = tmp_path / "bot_queue_lyr.db"
    db_manager = SqliteDatabaseManager(db_path)
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))

    organizer = LibraryOrganizer(library_root=tmp_path / "lib", enable_network=False)
    lyrics_provider = StubLyricsProvider(
        lyrics=LyricsData(
            synced_lyrics="[00:02.00] Queued job auto lyrics\n",
            plain_lyrics="Queued job auto lyrics\n",
        )
    )

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Job Song",
                artist="Job Artist",
                album="Job Album",
                year=2023,
                track_number=1,
            ),
            confidence=0.85,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=StubExtractor(),
        fingerprinter=StubFingerprinter("FP_Q_LYR"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=lyrics_provider,
        organizer=organizer,
        catalog_repository=repo,
    )
    worker = IngestionQueueWorker(pipeline=pipeline, catalog_repository=repo, governance_repository=repo)

    owner_account = await gov.ensure_user(1001, "Owner User", username="owner_u")
    owner_account = await gov.change_user_role(owner_account, 1001, UserRole.OWNER)

    # Enqueue a job awaiting confirmation
    src_file = tmp_path / "01. Job Song.flac"
    src_file.write_bytes(b"fLaC-queued-song")
    job, _ = await worker.enqueue_job(
        user=owner_account,
        source_path=src_file,
        original_filename="01. Job Song.flac",
        job_id="job-lyr-test-001",
    )
    job.state = JobState.AWAITING_CONFIRMATION
    job.candidates = candidates
    await repo.save_job(job)

    # Wire Bot and Dispatcher
    bot = Bot(token=sample_settings.telegram_bot_token)
    rbac_mw = RbacMiddleware(governance_service=gov, oidc_enabled=False)
    dp = Dispatcher()
    dp.message.outer_middleware(rbac_mw)
    dp.callback_query.outer_middleware(rbac_mw)
    dp["pipeline"] = pipeline
    dp["settings"] = sample_settings
    dp["catalog_repository"] = repo
    dp["governance_repository"] = repo
    dp["governance_service"] = gov
    dp["queue_worker"] = worker
    dp.include_router(build_root_router())

    tg_user = User(id=1001, is_bot=False, first_name="Owner", username="owner_u")
    tg_chat = Chat(id=1001, type="private")

    dispatched_methods: list[TelegramMethod[object]] = []
    screen_state: dict[str, object] = {
        "message_id": 400,
        "text": "",
        "reply_markup": None,
    }
    next_update_id = 1

    async def _fake_make_request(
        _bot: Bot,
        method: TelegramMethod[object],
        timeout: int | None = None,
    ) -> object:
        _ = timeout
        dispatched_methods.append(method)
        if isinstance(method, SendMessage):
            msg_id = int(str(screen_state["message_id"])) + 1
            screen_state["message_id"] = msg_id
            screen_state["text"] = method.text
            screen_state["reply_markup"] = method.reply_markup
            return Message(
                message_id=msg_id,
                date=datetime.now(UTC),
                chat=tg_chat,
                from_user=tg_user,
                text=method.text,
                reply_markup=(
                    method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
                ),
            )
        if isinstance(method, EditMessageText):
            screen_state["text"] = method.text
            screen_state["reply_markup"] = method.reply_markup
            return Message(
                message_id=int(str(screen_state["message_id"])),
                date=datetime.now(UTC),
                chat=tg_chat,
                from_user=tg_user,
                text=method.text,
                reply_markup=method.reply_markup,
            )
        if isinstance(method, (AnswerCallbackQuery, SendChatAction, DeleteMessage)):
            return True
        return True

    bot.session.__dict__["make_request"] = _fake_make_request

    async def _send_user_text(text: str) -> str:
        nonlocal next_update_id
        msg_id = int(str(screen_state["message_id"])) + 1
        screen_state["message_id"] = msg_id
        user_msg = Message(
            message_id=msg_id,
            date=datetime.now(UTC),
            chat=tg_chat,
            from_user=tg_user,
            text=text,
        )
        upd = Update(update_id=next_update_id, message=user_msg)
        next_update_id += 1
        await dp.feed_update(bot, upd)
        return str(screen_state["text"])

    async def _click_button(label_substring: str) -> str:
        nonlocal next_update_id
        markup = screen_state["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup), "No active reply_markup"
        matched_callback: str | None = None
        for row in markup.inline_keyboard:
            for btn in row:
                if label_substring in btn.text and btn.callback_data:
                    matched_callback = btn.callback_data
                    break
            if matched_callback is not None:
                break
        assert matched_callback is not None, f"Button {label_substring!r} not found"

        active_msg = Message(
            message_id=int(str(screen_state["message_id"])),
            date=datetime.now(UTC),
            chat=tg_chat,
            from_user=tg_user,
            text=str(screen_state["text"]),
            reply_markup=markup,
        )
        cb_query = CallbackQuery(
            id=f"cb-{next_update_id}",
            from_user=tg_user,
            chat_instance="ci-job-lyr",
            message=active_msg,
            data=matched_callback,
        )
        upd = Update(update_id=next_update_id, callback_query=cb_query)
        next_update_id += 1
        await dp.feed_update(bot, upd)
        return str(screen_state["text"])

    try:
        # Paste job ID into chat to open inspector
        await _send_user_text(job.job_id)
        # Open Manual Review
        await _click_button("Manual Review")
        # Open Manual Tag & Editor
        text = await _click_button("Manual Tag & Editor")
        assert "Queued Job Tag Editor" in text

        # Click 'Edit Lyrics' -> keyboard provides '⚡ Auto-Search Lyrics'
        text = await _click_button("Edit Lyrics")
        assert "Set Lyrics for Queued Job" in text

        # Click '⚡ Auto-Search Lyrics' -> fetches lyrics and returns to editor with lyrics attached!
        text = await _click_button("Auto-Search Lyrics")
        assert "Queued Job Tag Editor" in text
        assert "Synced LRC" in text
    finally:
        await bot.session.close()
