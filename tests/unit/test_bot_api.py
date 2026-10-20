"""Unit tests for the Telegram interface layer (`src.bot`)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.telegram import PRODUCTION
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    ForwardMessage,
    SendAudio,
    SendChatAction,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Audio, CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from src.bot.api_server import build_telegram_api_server, resolve_local_api_file_path
from src.bot.keyboards import (
    BTN_BACK_LABEL,
    BTN_HOME_LABEL,
    BTN_MAIN_ADMIN,
    BTN_MAIN_INFO,
    BTN_MAIN_QUEUE,
    BTN_MAIN_SONGS,
    BTN_MAIN_SSO,
    build_access_request_approval_keyboard,
    build_admin_dashboard_keyboard,
    build_guest_onboarding_keyboard,
    build_main_menu_keyboard,
    build_manual_search_keyboard,
    build_oidc_link_keyboard,
    build_queued_job_keyboard,
    build_song_action_keyboard,
    build_user_detail_keyboard,
    build_user_list_keyboard,
    build_verification_keyboard,
    format_toggle_label,
)
from src.bot.middlewares import RbacMiddleware
from src.bot.routers import build_root_router
from src.bot.routers.admin import (
    handle_access_request_callback,
    handle_adduser_command,
    handle_admin_command,
    handle_admin_dashboard_callback,
    handle_queue_cancel_callback,
    handle_queue_view_callback,
    handle_wipe_command,
    parse_access_request_callback,
    parse_admin_callback,
)
from src.bot.routers.commands import (
    handle_cancel_command,
    handle_fixtags_command,
    handle_help,
    handle_menu_navigation_callback,
    handle_queue_command,
    handle_sso_command,
    handle_start,
    handle_status,
)
from src.bot.routers.files import format_ingestion_summary, handle_incoming_media
from src.bot.routers.songs import (
    _is_ingestion_media_message,
    _is_non_command_text,
    _is_non_media_input_message,
    handle_cover_edit_input,
    handle_custom_research_input,
    handle_dry_run_unexpected_text,
    handle_import_payload_input,
    handle_lyrics_edit_input,
    handle_metadata_edit_input,
    handle_rescan_command,
    handle_songs_callback,
    handle_songs_command,
    handle_songs_search_text,
)
from src.bot.routers.verification import (
    handle_manual_search_query,
    handle_verification_callback,
    parse_verification_callback,
)
from src.bot.ui import (
    ack_callback,
    format_loading_card,
    format_screen,
    safe_answer,
    safe_edit_text,
    send_chat_action_safe,
    strip_copy_text_buttons,
)
from src.config.settings import AppSettings
from src.core.extractor import SevenZipArchiveExtractor
from src.core.models import (
    AudioFingerprint,
    DuplicateCheckResult,
    IngestionJob,
    IngestionResult,
    JobState,
    LyricsData,
    MetadataCandidate,
    TrackMetadata,
    UserAccount,
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
    StubLyricsProvider,
    StubMetadataProvider,
)


def test_build_telegram_api_server_local_vs_production(sample_settings: AppSettings) -> None:
    """Verify `build_telegram_api_server` switches between `--local` server and PRODUCTION."""
    local_server = build_telegram_api_server(sample_settings)
    assert local_server.is_local is True
    assert "localhost:8081" in local_server.base

    prod_settings = sample_settings.model_copy(update={"telegram_use_local_api": False})
    prod_server = build_telegram_api_server(prod_settings)
    assert prod_server == PRODUCTION


def test_resolve_local_api_file_path_hybrid_and_fallback(sample_settings: AppSettings) -> None:
    """Verify container `/var/lib/telegram-bot-api/...` paths map to host bind-mount directory."""
    container_reported = "/var/lib/telegram-bot-api/12345:TOKEN/music/file_42.flac"
    resolved = resolve_local_api_file_path(container_reported, sample_settings)
    expected = sample_settings.telegram_local_api_host_dir / "12345:TOKEN" / "music" / "file_42.flac"
    assert resolved == expected

    unrelated_path = "/tmp/other_folder/song.flac"
    assert resolve_local_api_file_path(unrelated_path, sample_settings) == Path(unrelated_path)


def test_verification_keyboard_and_callback_parsing() -> None:
    """Verify inline keyboard construction with leading confidence and callback data parsing."""
    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(title="Track A", artist="Artist", album="Album A", year=2001),
            confidence=0.82,
        )
    ]
    markup = build_verification_keyboard("job99", candidates)
    assert len(markup.inline_keyboard) == 5
    first_btn = markup.inline_keyboard[0][0]
    assert first_btn.callback_data == "verify:select:job99:0"
    assert first_btn.text.startswith("1. [82%] Artist - Track A")
    assert markup.inline_keyboard[1][0].callback_data == "queue:get_file:job99"
    assert markup.inline_keyboard[1][1].callback_data == "verify:edit:job99"
    assert markup.inline_keyboard[2][0].callback_data == "verify:manual:job99"
    assert markup.inline_keyboard[2][1].callback_data == "verify:auto_enrich:job99"
    assert markup.inline_keyboard[3][0].callback_data == "queue:pause:job99"
    assert markup.inline_keyboard[3][1].callback_data == "verify:cancel:job99"

    action, job_id, idx = parse_verification_callback("verify:select:job99:0")
    assert action == "select"
    assert job_id == "job99"
    assert idx == 0

    cancel_action, cancel_job, cancel_idx = parse_verification_callback("verify:cancel:job99")
    assert cancel_action == "cancel"
    assert cancel_job == "job99"
    assert cancel_idx is None

    with pytest.raises(ValueError, match="Malformed"):
        parse_verification_callback("invalid:data")


def test_admin_and_onboarding_keyboards_and_parsers() -> None:
    """Verify persistent bottom ReplyKeyboardMarkup, guest onboarding, queue, and admin keyboards."""
    guest_kb_url = build_guest_onboarding_keyboard("https://auth.example.com/authorize?state=1")
    assert guest_kb_url.inline_keyboard[0][0].url == "https://auth.example.com/authorize?state=1"
    assert guest_kb_url.inline_keyboard[0][1].callback_data == "access_req:request"

    guest_kb_no_oidc = build_guest_onboarding_keyboard(None)
    assert guest_kb_no_oidc.inline_keyboard[0][0].callback_data == "access_req:oidc_disabled"

    oidc_kb_url = build_oidc_link_keyboard("https://auth.example.com/authorize")
    assert oidc_kb_url.inline_keyboard[0][0].url == "https://auth.example.com/authorize"
    oidc_kb_disabled = build_oidc_link_keyboard(None)
    assert oidc_kb_disabled.inline_keyboard[0][0].callback_data == "access_req:oidc_disabled"

    owner_user = UserAccount(telegram_id=1001, display_name="Owner", role=UserRole.OWNER)
    owner_menu = build_main_menu_keyboard(owner_user)
    assert owner_menu.resize_keyboard is True
    assert owner_menu.is_persistent is True
    assert owner_menu.keyboard[0][0].text == BTN_MAIN_SONGS
    assert owner_menu.keyboard[0][1].text == BTN_MAIN_QUEUE
    assert owner_menu.keyboard[0][2].text == BTN_MAIN_SSO
    assert owner_menu.keyboard[1][0].text == BTN_MAIN_ADMIN
    assert owner_menu.keyboard[1][1].text == BTN_MAIN_INFO

    mod_user = UserAccount(telegram_id=1002, display_name="Mod", role=UserRole.MODERATOR)
    mod_menu = build_main_menu_keyboard(mod_user)
    assert len(mod_menu.keyboard) == 2
    assert mod_menu.keyboard[1][0].text == BTN_MAIN_INFO

    approval_kb = build_access_request_approval_keyboard(7)
    assert len(approval_kb.inline_keyboard[0]) == 2
    assert approval_kb.inline_keyboard[0][0].callback_data == "access_req:approve:7:LISTENER"
    assert approval_kb.inline_keyboard[0][1].callback_data == "access_req:approve:7:UPLOADER"
    assert approval_kb.inline_keyboard[1][0].callback_data == "access_req:approve:7:MODERATOR"
    assert approval_kb.inline_keyboard[1][1].callback_data == "access_req:deny:7"

    queue_kb = build_queued_job_keyboard("job-uuid-1")
    assert queue_kb.inline_keyboard[0][0].callback_data == "queue:get_file:job-uuid-1"
    assert queue_kb.inline_keyboard[0][1].callback_data == "queue:review:job-uuid-1"
    assert queue_kb.inline_keyboard[1][0].callback_data == "queue:pause:job-uuid-1"
    assert queue_kb.inline_keyboard[1][1].callback_data == "queue:cancel:job-uuid-1"
    paused_kb = build_queued_job_keyboard("job-uuid-1", state=JobState.PAUSED)
    assert paused_kb.inline_keyboard[0][0].callback_data == "queue:get_file:job-uuid-1"
    assert paused_kb.inline_keyboard[0][1].callback_data == "queue:review:job-uuid-1"
    assert paused_kb.inline_keyboard[1][0].callback_data == "queue:unlock:job-uuid-1"
    assert paused_kb.inline_keyboard[1][1].callback_data == "queue:cancel:job-uuid-1"

    dash_kb = build_admin_dashboard_keyboard(queued_count=3, concurrency_limit=2)
    assert len(dash_kb.inline_keyboard) == 3
    assert "View Queue (3)" in dash_kb.inline_keyboard[0][1].text
    assert "Concurrency: [2]" in dash_kb.inline_keyboard[1][1].text

    sample_user = UserAccount(
        telegram_id=987654321,
        display_name="John Doe",
        username="john_doe",
        role=UserRole.UPLOADER,
    )
    user_list_kb = build_user_list_keyboard([sample_user], pending_user_ids={987654321})
    assert user_list_kb.inline_keyboard[0][0].callback_data == "admin:user:987654321"
    assert "(Pending)" in user_list_kb.inline_keyboard[0][0].text
    assert user_list_kb.inline_keyboard[1][0].callback_data == "admin:add_user"

    detail_kb = build_user_detail_keyboard(sample_user)
    assert detail_kb.inline_keyboard[0][0].callback_data == "admin:promote:987654321:MODERATOR"
    assert detail_kb.inline_keyboard[0][1].callback_data == "admin:demote:987654321:LISTENER"
    assert detail_kb.inline_keyboard[2][0].callback_data == "admin:user_uploads:987654321"
    assert detail_kb.inline_keyboard[2][1].callback_data == "admin:user_changes:987654321"

    assert parse_access_request_callback("access_req:request") == ("request", None, None)
    assert parse_access_request_callback("access_req:approve:12:MODERATOR") == (
        "approve",
        12,
        UserRole.MODERATOR,
    )
    assert parse_access_request_callback("access_req:deny:12") == ("deny", 12, None)

    assert parse_admin_callback("admin:panel") == ("panel", None, None)
    assert parse_admin_callback("admin:add_user") == ("add_user", None, None)
    assert parse_admin_callback("admin:user:987654321") == ("user", 987654321, None)
    assert parse_admin_callback("admin:user_uploads:987654321") == ("user_uploads", 987654321, None)
    assert parse_admin_callback("admin:user_changes:987654321") == ("user_changes", 987654321, None)
    assert parse_admin_callback("admin:promote:987654321:ADMIN") == (
        "promote",
        987654321,
        UserRole.ADMIN,
    )

    with pytest.raises(ValueError, match="Malformed"):
        parse_access_request_callback("wrong:prefix")
    with pytest.raises(ValueError, match="Malformed"):
        parse_admin_callback("wrong:prefix")


def test_format_ingestion_summary_variants() -> None:
    """Verify Telegram HTML summary formatting for duplicate, confirmation, and completed states."""
    job = IngestionJob(
        job_id="j1",
        source_path=Path("a.flac"),
        candidates=[
            MetadataCandidate(
                metadata=TrackMetadata(
                    title="Stay Alive",
                    artist="Emilia (CV: Rie Takahashi)",
                    album="Stay Alive",
                    year=2016,
                    release_mbid="ca344c5b-9c69-4283-8383-e7c4c026d571",
                ),
                confidence=0.98,
                release_type="Single",
            )
        ],
    )

    dup_result = IngestionResult(
        job=job,
        duplicate=DuplicateCheckResult(
            is_duplicate=True,
            existing_path=Path("/music/a.flac"),
            match_reason="fingerprint",
        ),
    )
    assert "Duplicate Rejected" in format_ingestion_summary(dup_result)

    confirm_result = IngestionResult(job=job, requires_user_confirmation=True)
    confirm_html = format_ingestion_summary(confirm_result)
    assert "Confirmation Required" in confirm_html
    assert "<code>1. [98%]</code>" in confirm_html
    assert "<blockquote expandable>" in confirm_html
    assert "https://musicbrainz.org/release/ca344c5b-9c69-4283-8383-e7c4c026d571" in confirm_html

    job.final_path = Path("/music/Artist/Album/01 - a.flac")
    job.state = JobState.COMPLETED
    done_result = IngestionResult(job=job)
    assert "Track Ingested" in format_ingestion_summary(done_result)


def test_build_root_router_and_rbac_middleware_and_admin_handlers(tmp_path: Path) -> None:
    """Verify root router, `RbacMiddleware` guest/blocked gating, `/start`, `/adduser`, and `/admin` flows."""
    root = build_root_router()
    assert len(root.sub_routers) == 6

    db = SqliteDatabaseManager(tmp_path / "mw.db")
    repo = SqliteCatalogRepository(db)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))
    mw = RbacMiddleware(governance_service=gov, oidc_enabled=False)
    assert mw.build_oidc_url_for_user(12345) is None

    pipeline = IngestionPipeline(
        extractor=StubExtractor(),
        fingerprinter=StubFingerprinter(),
        metadata_provider=StubMetadataProvider([]),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "lib"),
        catalog_repository=repo,
    )
    worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repo,
        governance_repository=repo,
    )

    async def _scenario() -> None:
        await db.initialize()
        handler = AsyncMock(return_value="ok")

        # 1. Guest upload is intercepted and discarded by RbacMiddleware
        guest_msg = MagicMock(spec=Message)
        guest_msg.from_user = User(id=999, is_bot=False, first_name="Guest", username="guest_u")
        guest_msg.audio = MagicMock()
        guest_msg.document = None
        guest_msg.answer = AsyncMock()

        res = await mw(handler, guest_msg, {})
        assert res is None
        handler.assert_not_called()
        guest_msg.answer.assert_awaited_once()

        # 2. Owner upload is permitted by RbacMiddleware
        owner_msg = MagicMock(spec=Message)
        owner_msg.from_user = User(id=1001, is_bot=False, first_name="Owner", username="owner_u")
        owner_msg.audio = MagicMock(file_id="fid_1", file_name="track.flac", file_size=2048)
        owner_msg.document = None
        owner_msg.answer = AsyncMock()
        data_dict: dict[str, object] = {}

        res_owner = await mw(handler, owner_msg, data_dict)
        assert res_owner == "ok"
        owner_user = await repo.get_user(1001)
        assert owner_user is not None
        assert owner_user.role == UserRole.OWNER

        # 3. /start, /status, /sso, /queue, /adduser, and media upload
        guest_user = await repo.get_user(999)
        assert guest_user is not None
        await handle_start(guest_msg, current_user=guest_user)
        await handle_start(owner_msg, current_user=owner_user)
        start_text = owner_msg.answer.call_args[0][0]
        assert "Automated Music Ingestion Bot" in start_text
        assert "Use the buttons below" not in start_text

        await handle_status(
            owner_msg,
            current_user=owner_user,
            governance_repository=repo,
            queue_worker=worker,
        )
        await handle_sso_command(owner_msg, current_user=owner_user)
        await handle_incoming_media(
            owner_msg,
            current_user=owner_user,
            queue_worker=worker,
            governance_repository=repo,
        )
        await handle_queue_command(
            owner_msg,
            current_user=owner_user,
            governance_repository=repo,
        )

        owner_msg.text = "/adduser 777888 UPLOADER"
        await handle_adduser_command(
            owner_msg,
            current_user=owner_user,
            governance_service=gov,
            governance_repository=repo,
        )
        added_user = await repo.get_user(777888)
        assert added_user is not None
        assert added_user.role == UserRole.UPLOADER

        # 4. /admin command for guest (denied) vs owner (rendered)
        await handle_admin_command(guest_msg, current_user=guest_user, governance_repository=repo)
        await handle_admin_command(
            owner_msg,
            current_user=owner_user,
            governance_repository=repo,
            queue_worker=worker,
        )

        # 5. Guest access request callback & Owner approval callback
        cb = MagicMock(spec=CallbackQuery)
        cb.data = "access_req:request"
        cb.answer = AsyncMock()
        cb.message = owner_msg
        cb.bot = AsyncMock()
        owner_msg.edit_text = AsyncMock()
        owner_msg.delete = AsyncMock()

        await handle_access_request_callback(cb, current_user=guest_user, governance_service=gov)
        pending = await repo.get_pending_access_request(999)
        assert pending is not None

        # Verify opening User Management (`admin:users`) edits in place without spamming msg.answer
        owner_msg.answer.reset_mock()
        cb.data = "admin:users"
        await handle_admin_dashboard_callback(
            cb,
            current_user=owner_user,
            governance_service=gov,
            governance_repository=repo,
            queue_worker=worker,
        )
        owner_msg.answer.assert_not_awaited()
        owner_msg.edit_text.assert_awaited()

        # Promoting a pending user directly from User Management auto-resolves their pending request
        cb.data = "admin:promote:999:UPLOADER"
        await handle_admin_dashboard_callback(
            cb,
            current_user=owner_user,
            governance_service=gov,
            governance_repository=repo,
            queue_worker=worker,
        )
        assert await repo.get_pending_access_request(999) is None

        # Also test explicit access_req:approve callback on another guest user
        second_guest = await gov.ensure_user(telegram_id=888, display_name="Guest Two")
        req2, _ = await gov.submit_access_request(second_guest)
        cb.data = f"access_req:approve:{req2.id}:UPLOADER"
        await handle_access_request_callback(cb, current_user=owner_user, governance_service=gov)
        upgraded = await repo.get_user(888)
        assert upgraded is not None
        assert upgraded.role == UserRole.UPLOADER

        # 6. Admin dashboard callbacks
        for cb_data in (
            "admin:panel",
            "admin:users",
            "admin:add_user",
            "admin:user:999",
            "admin:promote:999:MODERATOR",
            "admin:block:999",
            "admin:concurrency",
            "admin:queue",
            "admin:audit",
            "admin:close",
        ):
            cb.data = cb_data
            await handle_admin_dashboard_callback(
                cb,
                current_user=owner_user,
                governance_service=gov,
                governance_repository=repo,
                queue_worker=worker,
            )

        cb.data = "queue:mine"
        await handle_queue_view_callback(
            cb,
            current_user=owner_user,
            governance_repository=repo,
        )

        # 7. Blocked user rejected by RbacMiddleware
        blocked_res = await mw(handler, guest_msg, {})
        assert blocked_res is None

        # 8. Queue cancel callback
        audio_file = tmp_path / "song.flac"
        audio_file.write_bytes(b"fLaC")
        job, _ = await worker.enqueue_job(owner_user, audio_file)
        cb.data = f"queue:cancel:{job.job_id}"
        await handle_queue_cancel_callback(cb, current_user=owner_user, queue_worker=worker)

    asyncio.run(_scenario())


def test_end_to_end_local_bot_api_ingestion_and_interactive_verification_flow(
    tmp_path: Path,
    sample_settings: AppSettings,
) -> None:
    """Verify full >20MB Local Bot API download mapping, confirmation keyboard, manual search, and filing."""
    host_api_dir = tmp_path / "telegram-bot-api"
    wsl_token_dir = host_api_dir / "8931169297\uf03aTOKEN" / "music"
    wsl_token_dir.mkdir(parents=True)
    local_downloaded_flac = wsl_token_dir / "file_0.flac"
    local_downloaded_flac.write_bytes(b"fLaC-stay-alive-32mb")

    settings = sample_settings.model_copy(
        update={
            "telegram_local_api_host_dir": host_api_dir,
            "storage_scratch_dir": tmp_path / "scratch",
            "storage_library_dir": tmp_path / "library",
            "database_path": tmp_path / "library.db",
        }
    )

    db = SqliteDatabaseManager(settings.database_path)
    repo = SqliteCatalogRepository(db)

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Stay Alive",
                artist="Emilia (CV: Rie Takahashi)",
                album="Stay Alive",
                album_artist="Emilia (CV: Rie Takahashi)",
                year=2016,
                track_number=1,
            ),
            confidence=0.92,
        ),
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Stay Alive",
                artist="Emilia (CV: Rie Takahashi)",
                album="Re:Zero Ending Single",
                album_artist="Emilia (CV: Rie Takahashi)",
                year=2016,
                track_number=1,
            ),
            confidence=0.90,
            disambiguation="TV Size",
        ),
    ]
    pipeline = IngestionPipeline(
        extractor=SevenZipArchiveExtractor(scratch_root=settings.storage_scratch_dir),
        fingerprinter=StubFingerprinter("FP_STAY_ALIVE"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(settings.storage_library_dir),
        catalog_repository=repo,
        auto_approve_confidence=0.90,
    )
    worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repo,
        governance_repository=repo,
    )

    async def _scenario() -> None:
        await db.initialize()
        uploader = UserAccount(
            telegram_id=5295075799,
            display_name="WindsOf",
            username="WindsOf",
            role=UserRole.UPLOADER,
        )
        await repo.upsert_user(uploader)

        bot_mock = MagicMock()
        tg_file_mock = MagicMock()
        tg_file_mock.file_path = "/var/lib/telegram-bot-api/8931169297:TOKEN/music/file_0.flac"
        bot_mock.get_file = AsyncMock(return_value=tg_file_mock)

        msg = MagicMock(spec=Message)
        msg.bot = bot_mock
        msg.audio = MagicMock(
            file_id="big_32mb_fid",
            file_name="Stay Alive.flac",
            performer="EMILIA(CV:RIE TAKAHASHI)",
            title="Stay Alive",
            file_size=33_764_147,
        )
        msg.document = None
        msg.answer = AsyncMock()
        msg.edit_text = AsyncMock()

        fsm_data: dict[str, object] = {}
        fsm = MagicMock()
        fsm.set_state = AsyncMock()
        fsm.clear = AsyncMock()

        async def _update_data(**kwargs: object) -> None:
            fsm_data.update(kwargs)

        async def _get_data() -> dict[str, object]:
            return dict(fsm_data)

        fsm.update_data = AsyncMock(side_effect=_update_data)
        fsm.get_data = AsyncMock(side_effect=_get_data)

        # 1. Forwarded 32.2 MB track triggers get_file, resolves WSL \uf03a path,
        # edits progress bar in-place, and transitions the same message into confirmation card
        await handle_incoming_media(
            msg,
            current_user=uploader,
            queue_worker=worker,
            pipeline=pipeline,
            governance_repository=repo,
            settings=settings,
            state=fsm,
        )
        bot_mock.get_file.assert_awaited_once_with("big_32mb_fid")
        msg.answer.assert_awaited_once()
        status_msg = msg.answer.return_value
        assert status_msg.edit_text.await_count >= 3
        progress_edits = [str(call.args[0]) for call in status_msg.edit_text.await_args_list]
        assert any("Ingesting:" in text and "Stay Alive.flac" in text for text in progress_edits)
        final_confirm_text = progress_edits[-1]
        assert "Confirmation Required" in final_confirm_text
        active_job_id = str(fsm_data["job_id"])

        # 2. User clicks [Manual Search] and submits a refined query
        cb = MagicMock(spec=CallbackQuery)
        cb.message = msg
        cb.answer = AsyncMock()
        cb.data = f"verify:manual:{active_job_id}"
        await handle_verification_callback(
            cb,
            state=fsm,
            current_user=uploader,
            pipeline=pipeline,
            queue_worker=worker,
        )

        msg.text = "Emilia (CV: Rie Takahashi) - Stay Alive | Stay Alive"
        await handle_manual_search_query(msg, state=fsm, pipeline=pipeline)

        # 3. User taps candidate #0: [Emilia (CV: Rie Takahashi) - Stay Alive | Stay Alive (2016) (92%)]
        cb.data = f"verify:select:{active_job_id}:0"
        await handle_verification_callback(
            cb,
            state=fsm,
            current_user=uploader,
            pipeline=pipeline,
            queue_worker=worker,
        )
        msg.edit_text.assert_awaited()
        assert msg.edit_text.await_args is not None
        final_summary = str(msg.edit_text.await_args.args[0])
        assert "Track Ingested" in final_summary
        assert "01 - Stay Alive.flac" in final_summary

        expected_flac = (
            settings.storage_library_dir
            / "Emilia (CV_ Rie Takahashi)"
            / "Stay Alive (2016)"
            / "01 - Stay Alive.flac"
        )
        assert expected_flac.is_file()
        assert expected_flac.with_suffix(".lrc").is_file()

        # 4. Forwarding the same track again edits the queued message in-place with Duplicate Rejected
        msg.answer.reset_mock()
        status_msg.edit_text.reset_mock()
        await handle_incoming_media(
            msg,
            current_user=uploader,
            queue_worker=worker,
            pipeline=pipeline,
            governance_repository=repo,
            settings=settings,
            state=fsm,
        )
        msg.answer.assert_awaited_once()
        dup_reply = str(status_msg.edit_text.await_args_list[-1].args[0])
        assert "Duplicate Rejected" in dup_reply

    asyncio.run(_scenario())


def _last_arg(mock_fn: AsyncMock) -> str:
    """Return the first positional argument from the latest await call on `mock_fn`."""
    assert mock_fn.await_args is not None
    return str(mock_fn.await_args.args[0])


def test_fixtags_moderator_disambiguation_and_wipe_commands_and_callbacks(tmp_path: Path) -> None:
    """Verify `/fixtags`, `admin:tags` / `admin:tag_approve`, `admin:resolve_job`, and `/wipe CONFIRM`."""
    db = SqliteDatabaseManager(tmp_path / "mod_tags.db")
    repo = SqliteCatalogRepository(db)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Stay Alive",
                artist="Emilia (CV: Rie Takahashi)",
                album="Stay Alive",
                year=2016,
                track_number=1,
            ),
            confidence=0.75,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=StubExtractor(),
        fingerprinter=StubFingerprinter("FP_PAUSED"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "lib"),
        catalog_repository=repo,
        auto_approve_confidence=0.90,
    )
    worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repo,
        governance_repository=repo,
    )

    async def _scenario() -> None:
        await db.initialize()
        owner = await gov.ensure_user(1001, "Owner")
        uploader_a = await gov.ensure_user(2001, "Uploader A")
        uploader_a = await gov.change_user_role(owner, 2001, UserRole.UPLOADER)
        uploader_b = await gov.ensure_user(2002, "Uploader B")
        uploader_b = await gov.change_user_role(owner, 2002, UserRole.UPLOADER)
        moderator = await gov.ensure_user(3001, "Moderator")
        moderator = await gov.change_user_role(owner, 3001, UserRole.MODERATOR)

        # 1. Uploader A pauses a job at AWAITING_CONFIRMATION
        audio_path = tmp_path / "paused.flac"
        audio_path.write_bytes(b"fLaC-paused")
        job, _ = await worker.enqueue_job(uploader_a, audio_path, "paused.flac")
        res = await worker.execute_job(job.job_id)
        assert res is not None
        assert res.requires_user_confirmation is True

        # 2. Uploader B (another UPLOADER) is blocked from resolving Uploader A's job
        cb = MagicMock(spec=CallbackQuery)
        cb.data = f"verify:select:{job.job_id}:0"
        cb.answer = AsyncMock()
        msg = MagicMock(spec=Message)
        msg.answer = AsyncMock()
        msg.edit_text = AsyncMock()
        cb.message = msg

        await handle_verification_callback(
            cb,
            current_user=uploader_b,
            pipeline=pipeline,
            queue_worker=worker,
        )
        assert "permission" in _last_arg(cb.answer).lower()

        # 3. Moderator views `/queue`, sees paused job button, opens `admin:resolve_job`, and confirms it
        await handle_queue_command(msg, current_user=moderator, governance_repository=repo)
        assert "Awaiting Disambiguation" in _last_arg(msg.answer)

        fsm = MagicMock()
        fsm.set_state = AsyncMock()
        fsm.update_data = AsyncMock()
        fsm.clear = AsyncMock()

        cb.data = f"admin:resolve_job:{job.job_id}"
        await handle_admin_dashboard_callback(
            cb,
            current_user=moderator,
            governance_service=gov,
            governance_repository=repo,
            queue_worker=worker,
            pipeline=pipeline,
            state=fsm,
        )
        msg.edit_text.assert_awaited()

        cb.data = f"verify:select:{job.job_id}:0"
        await handle_verification_callback(
            cb,
            state=fsm,
            current_user=moderator,
            pipeline=pipeline,
            queue_worker=worker,
        )
        assert "Track Ingested" in _last_arg(msg.edit_text)

        # 4. Uploader A runs `/fixtags` (first without args to list tracks, then with tag edits)
        msg.text = "/fixtags"
        await handle_fixtags_command(
            msg,
            current_user=uploader_a,
            governance_service=gov,
            governance_repository=repo,
        )
        assert "Recent Library Tracks" in _last_arg(msg.answer)

        tracks = await repo.list_library_tracks(limit=5)
        assert len(tracks) == 1
        track_id = tracks[0].id

        msg.text = f"/fixtags {track_id} title=Stay Alive (Edited) | artist=Emilia | year=2017 | reason=Fix"
        await handle_fixtags_command(
            msg,
            current_user=uploader_a,
            governance_service=gov,
            governance_repository=repo,
        )
        assert "Tag Correction Request #1 Submitted" in _last_arg(msg.answer)

        # 5. Moderator reviews `admin:tags` and approves `admin:tag_approve:1`
        cb.data = "admin:tags"
        await handle_admin_dashboard_callback(
            cb,
            current_user=moderator,
            governance_service=gov,
            governance_repository=repo,
        )
        assert "Pending Tag Correction #1" in _last_arg(msg.edit_text)

        cb.data = "admin:tag_approve:1"
        await handle_admin_dashboard_callback(
            cb,
            current_user=moderator,
            governance_service=gov,
            governance_repository=repo,
        )
        assert "Approved" in _last_arg(msg.edit_text)

        updated_track = await repo.get_library_track(track_id)
        assert updated_track is not None
        assert updated_track.title == "Stay Alive (Edited)"

        # 6. Owner executes `/wipe CONFIRM`
        msg.text = "/wipe CONFIRM"
        await handle_wipe_command(msg, current_user=owner, governance_service=gov)
        assert "System Catalog Wipe Executed" in _last_arg(msg.answer)

    asyncio.run(_scenario())


def test_songs_interactive_menu_dry_run_editing_export_import_and_rescan(tmp_path: Path) -> None:
    """Verify `/songs` menu, dry-run previews, metadata/lyrics/cover edits, export/import, and `/rescan`."""
    db = SqliteDatabaseManager(tmp_path / "songs.db")
    repo = SqliteCatalogRepository(db)
    lib_dir = tmp_path / "library"
    scratch_dir = tmp_path / "scratch"
    organizer = LibraryOrganizer(lib_dir)

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Orange Mint",
                artist="Saori Hayami",
                album="Orange Mint",
                year=2015,
                track_number=1,
                duration_seconds=274,
                recording_mbid="rec-orange",
                release_mbid="rel-orange",
            ),
            confidence=1.0,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=SevenZipArchiveExtractor(scratch_root=scratch_dir),
        fingerprinter=StubFingerprinter(),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=organizer,
        catalog_repository=repo,
    )
    settings = AppSettings(
        telegram_api_id=12345,
        telegram_api_hash="0123456789abcdef",
        telegram_bot_token="123456:ABCDEF",
        storage_scratch_dir=scratch_dir,
        storage_library_dir=lib_dir,
        database_path=tmp_path / "songs.db",
    )

    async def _scenario() -> None:
        await db.initialize()
        uploader = UserAccount(telegram_id=3001, display_name="Uploader", role=UserRole.UPLOADER)
        await repo.upsert_user(uploader)

        # Create a real track file in the library and register it
        src_audio = tmp_path / "01 - Orange Mint.flac"
        src_audio.write_bytes(b"fake-flac-bytes-orange-mint")
        final_path = await organizer.finalize_track(
            src_audio,
            candidates[0].metadata,
            LyricsData(synced_lyrics="[00:08.53] Sayonara"),
        )
        await repo.register_ingested_track(
            job_id="job-orange-1",
            metadata=candidates[0].metadata,
            fingerprint=AudioFingerprint(duration_seconds=274, fingerprint="fp-orange-1"),
            library_path=final_path,
        )

        # Stateful FSM mock storing `state_data` dictionary
        state_data: dict[str, object] = {}
        fsm = MagicMock()
        fsm.set_state = AsyncMock()

        async def _update_data(**kwargs: object) -> dict[str, object]:
            state_data.update(kwargs)
            return dict(state_data)

        async def _get_data() -> dict[str, object]:
            return dict(state_data)

        fsm.update_data = AsyncMock(side_effect=_update_data)
        fsm.get_data = AsyncMock(side_effect=_get_data)
        fsm.clear = AsyncMock(side_effect=state_data.clear)

        msg = MagicMock(spec=Message)
        msg.answer = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.answer_audio = AsyncMock()
        msg.answer_document = AsyncMock()
        msg.answer_photo = AsyncMock()
        msg.photo = None
        msg.document = None
        msg.bot = MagicMock()

        # 1. `/songs` opens browser menu
        msg.text = "/songs"
        await handle_songs_command(
            msg,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
        )
        assert "Music Library Browser" in _last_arg(msg.answer)

        # 2. Type search query in `awaiting_search_query`
        msg.text = "Orange"
        await handle_songs_search_text(
            msg,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
        )
        assert "Search Results for" in _last_arg(msg.answer)

        tracks = await repo.list_library_tracks(limit=5)
        assert len(tracks) == 1
        track = tracks[0]
        sid = track.id[:12]

        cb = MagicMock(spec=CallbackQuery)
        cb.message = msg
        cb.answer = AsyncMock()

        # 3. View song card (`songs:view:<sid>`) & retrieve song/album (`songs:get_track`, `songs:get_album`)
        cb.data = f"songs:view:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        assert "Song Details" in _last_arg(msg.edit_text)

        cb.data = f"songs:get_track:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        msg.answer_audio.assert_awaited()
        msg.answer_document.assert_awaited()

        cb.data = f"songs:get_album:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )

        # 4. Edit Metadata -> Dry-Run Preview -> Confirm & Apply (relocates file on disk!)
        cb.data = f"songs:field:{sid}:multi"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        msg.text = "artist: Yotsugi Ononoki(Saori Hayami) | title: Orange Mint (TV Size) | year: 2016"
        await handle_metadata_edit_input(
            msg,
            state=fsm,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        dry_run_html = _last_arg(msg.answer)
        assert "Dry-Run Preview (METADATA)" in dry_run_html
        assert "Orange Mint (TV Size)" in dry_run_html
        assert "Saori Hayami" in dry_run_html

        cb.data = f"songs:confirm_apply:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        assert "Changes Applied Successfully" in _last_arg(msg.edit_text)
        updated_rec = await repo.get_library_track(sid)
        assert updated_rec is not None
        assert updated_rec.title == "Orange Mint (TV Size)"
        assert updated_rec.year == 2016
        assert updated_rec.storage_path.is_file()

        # 5. Edit Lyrics -> Dry-Run Preview -> Confirm & Apply
        cb.data = f"songs:edit_lyrics:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        msg.text = "[00:01.00] New synced line 1\n[00:05.00] New synced line 2"
        await handle_lyrics_edit_input(
            msg,
            state=fsm,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        assert "Dry-Run Preview (LYRICS)" in _last_arg(msg.answer)

        cb.data = f"songs:confirm_apply:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        lrc_file = updated_rec.storage_path.with_suffix(".lrc")
        assert lrc_file.is_file()
        assert "New synced line 1" in lrc_file.read_text(encoding="utf-8")

        # 6. Edit Cover Art via uploaded document -> Dry-Run Preview -> Confirm & Apply
        cb.data = f"songs:edit_cover:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        local_api_dir = tmp_path / "telegram-bot-api"
        local_api_dir.mkdir(parents=True, exist_ok=True)
        cover_upload = local_api_dir / "new_cover.jpg"
        cover_upload.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF-new-cover")
        doc_mock = MagicMock()
        doc_mock.file_id = "cover-file-id"
        doc_mock.mime_type = "image/jpeg"
        msg.document = doc_mock
        msg.text = None
        tg_file_mock = MagicMock()
        tg_file_mock.file_path = str(cover_upload)
        msg.bot.get_file = AsyncMock(return_value=tg_file_mock)

        await handle_cover_edit_input(
            msg,
            state=fsm,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        assert "Dry-Run Preview (COVER)" in _last_arg(msg.answer)
        msg.document = None

        cb.data = f"songs:confirm_apply:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        assert (
            updated_rec.storage_path.parent / "cover.jpg"
        ).read_bytes() == b"\xff\xd8\xff\xe0\x00\x10JFIF-new-cover"

        # 7. Export & Import JSON -> Dry-Run Preview -> Confirm & Apply
        cb.data = f"songs:export:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        cb.data = "songs:export_all"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )

        cb.data = f"songs:import:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        msg.text = (
            '{"title": "Orange Mint", "artist": "Saori Hayami", '
            '"album": "Orange Mint", "year": 2015, "lyrics": "[00:02.00] Imported LRC"}'
        )
        await handle_import_payload_input(
            msg,
            state=fsm,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        assert "Dry-Run Preview (IMPORT)" in _last_arg(msg.answer)

        cb.data = f"songs:confirm_apply:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        restored_rec = await repo.get_library_track(sid)
        assert restored_rec is not None
        assert restored_rec.title == "Orange Mint"
        assert restored_rec.year == 2015

        # 8. Custom Re-Search -> Dry-Run Preview
        cb.data = f"songs:research_custom:{sid}"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        msg.text = "Saori Hayami - Orange Mint"
        await handle_custom_research_input(
            msg,
            state=fsm,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        assert "Dry-Run Preview (RESEARCH)" in _last_arg(msg.answer)

        # 9. Library Folder Rescan (`/rescan` and `songs:rescan_apply`) when a new file is dropped on disk
        extra_dir = lib_dir / "ClariS" / "border (2015)"
        extra_dir.mkdir(parents=True, exist_ok=True)
        extra_audio = extra_dir / "01 - ClariS - border.flac"
        extra_audio.write_bytes(b"fake-flac-bytes-claris-border")
        extra_audio.with_suffix(".lrc").write_text("[00:01.00] border lrc", encoding="utf-8")

        msg.text = "/rescan"
        await handle_rescan_command(
            msg,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        rescan_preview = _last_arg(msg.answer)
        assert "Library Rescan Dry-Run Preview" in rescan_preview
        assert "Added on Disk: <code>1</code>" in rescan_preview

        cb.data = "songs:rescan_apply"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=uploader,
            governance_repository=repo,
            pipeline=pipeline,
            settings=settings,
        )
        assert "Library Rescan &amp; Sync Complete" in _last_arg(msg.edit_text)
        all_after_rescan = await repo.list_library_tracks(limit=10)
        assert len(all_after_rescan) == 2

    asyncio.run(_scenario())


def test_fsm_state_reset_and_audio_upload_isolation_prevent_stuck_states() -> None:
    """Verify audio uploads, commands, and menu buttons never get stuck in `/songs` FSM states."""
    # 1. Verify filter helpers never match audio tracks, archives, slash commands, or menu buttons
    audio_msg = MagicMock(spec=Message)
    audio_msg.audio = MagicMock(file_id="aud-1", file_name="etoile et toi.flac")
    audio_msg.document = None
    audio_msg.photo = None
    audio_msg.text = None
    assert _is_ingestion_media_message(audio_msg) is True
    assert _is_non_command_text(audio_msg) is False
    assert _is_non_media_input_message(audio_msg) is False

    archive_msg = MagicMock(spec=Message)
    archive_msg.audio = None
    archive_msg.document = MagicMock(file_name="album.zip", mime_type="application/zip")
    archive_msg.photo = None
    archive_msg.text = None
    assert _is_ingestion_media_message(archive_msg) is True
    assert _is_non_media_input_message(archive_msg) is False

    cmd_msg = MagicMock(spec=Message)
    cmd_msg.audio = None
    cmd_msg.document = None
    cmd_msg.photo = None
    cmd_msg.text = "/start"
    assert _is_non_command_text(cmd_msg) is False
    assert _is_non_media_input_message(cmd_msg) is False

    menu_msg = MagicMock(spec=Message)
    menu_msg.audio = None
    menu_msg.document = None
    menu_msg.photo = None
    menu_msg.text = BTN_MAIN_INFO
    assert _is_non_command_text(menu_msg) is False
    assert _is_non_media_input_message(menu_msg) is False

    json_doc_msg = MagicMock(spec=Message)
    json_doc_msg.audio = None
    json_doc_msg.document = MagicMock(file_name="song.json", mime_type="application/json")
    json_doc_msg.photo = None
    json_doc_msg.text = None
    assert _is_ingestion_media_message(json_doc_msg) is False
    assert _is_non_media_input_message(json_doc_msg) is True

    # 2. Verify `/start`, `/cancel`, and `handle_dry_run_unexpected_text` reset or guard FSM state
    async def _scenario() -> None:
        state_data: dict[str, object] = {
            "active_song_id": "trk-123456789012",
            "pending_song_proposal": {"new_title": "Test"},
        }
        fsm = MagicMock()
        fsm.set_state = AsyncMock()
        fsm.get_data = AsyncMock(side_effect=lambda: dict(state_data))
        fsm.clear = AsyncMock(side_effect=state_data.clear)

        msg = MagicMock(spec=Message)
        msg.answer = AsyncMock()
        msg.text = "unexpected text"

        await handle_dry_run_unexpected_text(msg, state=fsm)
        assert "Pending Dry-Run Preview" in _last_arg(msg.answer)
        assert "/cancel" not in _last_arg(msg.answer)

        await handle_cancel_command(msg, state=fsm)
        fsm.clear.assert_awaited()
        assert state_data == {}
        assert "Active prompt cancelled" in _last_arg(msg.answer)

        state_data["active_song_id"] = "stuck-id"
        owner = UserAccount(telegram_id=1, display_name="Owner", role=UserRole.OWNER)
        await handle_start(msg, current_user=owner, state=fsm)
        assert state_data == {}
        assert fsm.clear.await_count >= 2

        state_data["active_song_id"] = "stuck-id-2"
        await handle_songs_callback(
            MagicMock(
                spec=CallbackQuery,
                data="songs:view:trk-12345678",
                message=msg,
                answer=AsyncMock(),
            ),
            state=fsm,
            current_user=owner,
            governance_repository=MagicMock(
                get_library_track=AsyncMock(return_value=None),
            ),
        )
        assert fsm.set_state.await_count == 0 or state_data.get("active_song_id") == "stuck-id-2"

    asyncio.run(_scenario())


def test_ux_audit_rules_in_place_mutation_typography_and_ergonomics(tmp_path: Path) -> None:
    """Verify the 5 UX Audit Rules: in-place mutation, callbacks, 3-tier HTML, keyboards, and latency."""
    # Rule 3: 3-tier HTML hierarchy (`<b>Header</b>`, `<blockquote>`, `<code>`/`<i>` footer)
    screen = format_screen(
        title="🎛️ Dashboard",
        context_lines=["Line 1", "Line 2"],
        footer_lines=["<i>Breadcrumb</i> • <code>ID-1</code>"],
        expandable=True,
    )
    assert screen.startswith("<b>🎛️ Dashboard</b>\n<blockquote expandable>")
    assert "</blockquote>\n<i>Breadcrumb</i> • <code>ID-1</code>" in screen
    assert screen.count("<b>") == 1

    loading = format_loading_card("Scanning", "Please wait...", "🧭 Home › Rescan")
    assert "<b>⏳ Scanning</b>" in loading
    assert "<blockquote>Please wait...</blockquote>" in loading

    # Rule 4: Toggle states, CopyTextButton, and 2-tier escape navigation (`◀ Back` and `🏠 Menu`)
    assert format_toggle_label("Parallel Queue", True) == "✅ [Active] Parallel Queue"
    assert format_toggle_label("Parallel Queue", False) == "⬜ [Disabled] Parallel Queue"

    guest_kb = build_guest_onboarding_keyboard("https://auth.example.com", telegram_id=555666)
    copy_payloads = [
        btn.copy_text.text for row in guest_kb.inline_keyboard for btn in row if btn.copy_text is not None
    ]
    assert copy_payloads == ["555666"]

    oidc_kb = build_oidc_link_keyboard("https://auth.example.com", subject="sub-uuid-999")
    assert any(
        btn.copy_text is not None and btn.copy_text.text == "sub-uuid-999"
        for row in oidc_kb.inline_keyboard
        for btn in row
    )

    song_kb = build_song_action_keyboard("track-uuid-123456")
    song_buttons = [btn for row in song_kb.inline_keyboard for btn in row]
    assert any(
        btn.copy_text is not None and btn.copy_text.text == "track-uuid-123456" for btn in song_buttons
    )
    assert any(btn.text == BTN_BACK_LABEL for btn in song_buttons)
    assert any(btn.text == BTN_HOME_LABEL for btn in song_buttons)

    manual_kb = build_manual_search_keyboard("job-42")
    manual_buttons = [btn for row in manual_kb.inline_keyboard for btn in row]
    assert any(
        btn.text == BTN_BACK_LABEL and btn.callback_data == "verify:back:job-42" for btn in manual_buttons
    )
    assert any(btn.text == BTN_HOME_LABEL and btn.callback_data == "menu:home" for btn in manual_buttons)

    # Rules 1, 2, and 5: In-place mutation (`safe_edit_text`), unmodified suppression, caption fallback,
    # and zero `msg.answer` inside callback handlers (`songs:search_prompt`, `menu:*`, `verify:back`)
    async def _scenario() -> None:
        msg = MagicMock(spec=Message)
        msg.text = "existing text"
        msg.caption = None
        msg.answer = AsyncMock()
        msg.edit_text = AsyncMock(
            side_effect=TelegramBadRequest(
                method=EditMessageText(chat_id=1, message_id=1, text="same"),
                message="Bad Request: message is not modified",
            )
        )
        # Should suppress "message is not modified" without raising
        assert await safe_edit_text(msg, "same") is False

        # Caption-only message should mutate via `edit_caption`
        caption_msg = MagicMock(spec=Message)
        caption_msg.text = None
        caption_msg.caption = "old caption"
        caption_msg.edit_caption = AsyncMock()
        assert await safe_edit_text(caption_msg, "new caption") is True
        caption_msg.edit_caption.assert_awaited_once_with(caption="new caption")

        # Verify `handle_help` contains zero legacy CLI command dumps
        msg.edit_text = AsyncMock()
        await handle_help(msg)
        help_html = _last_arg(msg.answer)
        assert "/fixtags" not in help_html
        assert "/wipe" not in help_html
        assert "<blockquote expandable>" in help_html

        # Verify `songs:search_prompt` and `menu:*` callbacks edit in-place and never call `msg.answer`
        db = SqliteDatabaseManager(tmp_path / "ux.db")
        repo = SqliteCatalogRepository(db)
        await db.initialize()
        owner = UserAccount(telegram_id=1001, display_name="Owner", role=UserRole.OWNER)
        await repo.upsert_user(owner)

        fsm = MagicMock()
        fsm.set_state = AsyncMock()
        fsm.update_data = AsyncMock()
        fsm.clear = AsyncMock()

        msg.answer.reset_mock()
        msg.edit_text.reset_mock()
        cb = MagicMock(spec=CallbackQuery)
        cb.message = msg
        cb.answer = AsyncMock()
        cb.bot = MagicMock(send_chat_action=AsyncMock())
        msg.chat = MagicMock(id=1001)

        cb.data = "songs:search_prompt"
        await handle_songs_callback(
            cb,
            state=fsm,
            current_user=owner,
            governance_repository=repo,
        )
        msg.answer.assert_not_awaited()
        msg.edit_text.assert_awaited_once()
        assert "Search Music Library" in _last_arg(msg.edit_text)

        for nav_action in ("menu:home", "menu:status", "menu:sso", "menu:help"):
            cb.data = nav_action
            await handle_menu_navigation_callback(
                cb,
                state=fsm,
                current_user=owner,
                governance_repository=repo,
            )
        msg.answer.assert_not_awaited()

        await send_chat_action_safe(cb)
        cb.bot.send_chat_action.assert_awaited()
        await ack_callback(cb, "Toast")

    asyncio.run(_scenario())


def test_real_aiogram_dispatcher_living_screen_navigation_and_fallbacks(tmp_path: Path) -> None:
    """Simulate real `aiogram.Dispatcher`, `Bot`, `Message`, and `CallbackQuery` button navigation."""
    db = SqliteDatabaseManager(tmp_path / "real_sim.db")
    repo = SqliteCatalogRepository(db)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))
    lib_dir = tmp_path / "library"
    scratch_dir = tmp_path / "scratch"
    organizer = LibraryOrganizer(lib_dir)

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Stay Alive",
                artist="Rie Takahashi",
                album="Stay Alive",
                year=2016,
                track_number=1,
                duration_seconds=266,
                recording_mbid="rec-stay-alive",
                release_mbid="rel-stay-alive",
            ),
            confidence=1.0,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=SevenZipArchiveExtractor(scratch_root=scratch_dir),
        fingerprinter=StubFingerprinter("fp-stay-alive"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=organizer,
        catalog_repository=repo,
    )
    worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repo,
        governance_repository=repo,
    )
    settings = AppSettings(
        telegram_api_id=12345,
        telegram_api_hash="0123456789abcdef",
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        storage_scratch_dir=scratch_dir,
        storage_library_dir=lib_dir,
        database_path=tmp_path / "real_sim.db",
    )

    async def _scenario() -> None:
        await db.initialize()

        # Seed a real song in the library ("Rie Takahashi - Stay Alive")
        src_audio = tmp_path / "01 - Stay Alive.flac"
        src_audio.write_bytes(b"fLaC-stay-alive")
        final_path = await organizer.finalize_track(
            src_audio,
            candidates[0].metadata,
            LyricsData(synced_lyrics="[00:01.00] Stay Alive"),
        )
        await repo.register_ingested_track(
            job_id="job-rie-1",
            metadata=candidates[0].metadata,
            fingerprint=AudioFingerprint(duration_seconds=266, fingerprint="fp-stay-alive"),
            library_path=final_path,
        )

        # Wire real aiogram Bot and Dispatcher with RbacMiddleware and build_root_router()
        bot = Bot(token=settings.telegram_bot_token)
        rbac_mw = RbacMiddleware(governance_service=gov, oidc_enabled=False)
        dp = Dispatcher()
        dp.message.outer_middleware(rbac_mw)
        dp.callback_query.outer_middleware(rbac_mw)
        dp["pipeline"] = pipeline
        dp["settings"] = settings
        dp["governance_repository"] = repo
        dp["governance_service"] = gov
        dp["queue_worker"] = worker
        dp.include_router(build_root_router())

        tg_user = User(id=1001, is_bot=False, first_name="Owner", username="owner_u")
        tg_chat = Chat(id=1001, type="private")

        dispatched_methods: list[TelegramMethod[object]] = []
        screen_state: dict[str, object] = {
            "message_id": 100,
            "text": "",
            "reply_markup": None,
            "reject_copy_text": False,
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
                markup = method.reply_markup
                if (
                    screen_state["reject_copy_text"]
                    and isinstance(markup, InlineKeyboardMarkup)
                    and any(
                        getattr(btn, "copy_text", None) is not None
                        for row in markup.inline_keyboard
                        for btn in row
                    )
                ):
                    raise TelegramBadRequest(
                        method=method,
                        message="Bad Request: BUTTON_TYPE_INVALID",
                    )
                msg_id = int(str(screen_state["message_id"])) + 1
                screen_state["message_id"] = msg_id
                screen_state["text"] = method.text
                screen_state["reply_markup"] = markup
                return Message(
                    message_id=msg_id,
                    date=datetime.now(UTC),
                    chat=tg_chat,
                    from_user=tg_user,
                    text=method.text,
                    reply_markup=markup if isinstance(markup, InlineKeyboardMarkup) else None,
                )
            if isinstance(method, EditMessageText):
                markup = method.reply_markup
                if (
                    screen_state["reject_copy_text"]
                    and isinstance(markup, InlineKeyboardMarkup)
                    and any(
                        getattr(btn, "copy_text", None) is not None
                        for row in markup.inline_keyboard
                        for btn in row
                    )
                ):
                    raise TelegramBadRequest(
                        method=method,
                        message="Bad Request: BUTTON_TYPE_INVALID",
                    )
                screen_state["text"] = method.text
                screen_state["reply_markup"] = markup
                return Message(
                    message_id=int(str(screen_state["message_id"])),
                    date=datetime.now(UTC),
                    chat=tg_chat,
                    from_user=tg_user,
                    text=method.text,
                    reply_markup=markup,
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
            assert isinstance(markup, InlineKeyboardMarkup), (
                f"Expected InlineKeyboardMarkup on screen, got {markup!r} for text={screen_state['text']!r}"
            )
            matched_callback: str | None = None
            available: list[str] = []
            for row in markup.inline_keyboard:
                for btn in row:
                    available.append(btn.text)
                    if label_substring in btn.text and btn.callback_data:
                        matched_callback = btn.callback_data
                        break
                if matched_callback is not None:
                    break
            assert matched_callback is not None, (
                f"Button matching {label_substring!r} not found in {available}"
            )

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
                chat_instance="ci-1",
                message=active_msg,
                data=matched_callback,
            )
            upd = Update(update_id=next_update_id, callback_query=cb_query)
            next_update_id += 1
            await dp.feed_update(bot, upd)

            recent = dispatched_methods[before_count:]
            assert any(isinstance(m, AnswerCallbackQuery) for m in recent), (
                f"Callback {matched_callback!r} did not dispatch AnswerCallbackQuery!"
            )
            assert any(isinstance(m, EditMessageText) for m in recent), (
                f"Callback {matched_callback!r} did not dispatch EditMessageText!"
            )
            assert not any(isinstance(m, SendMessage) for m in recent), (
                f"Callback {matched_callback!r} sent a new message instead of editing in-place!"
            )
            return str(screen_state["text"])

        try:
            # 1. User opens `/songs` -> searches `"rie"` -> clicks song -> navigates submenus & `◀ Back`
            text = await _send_user_text("/songs")
            assert "Music Library Browser (1 track(s))" in text

            text = await _send_user_text("rie")
            assert "Search Results for rie (1)" in text

            # Clicking `◀ Back` from filtered search results returns to full Music Library Browser!
            text = await _click_button(BTN_BACK_LABEL)
            assert "Music Library Browser (1 track(s))" in text

            # Search `"rie"` again and click the song button (`Rie Takahashi - Stay Alive`)
            text = await _send_user_text("rie")
            assert "Search Results for rie (1)" in text
            text = await _click_button("Rie Takahashi - Stay Alive")
            assert "Song Details &amp; Actions" in text

            # Navigate into Edit Metadata -> Edit Title -> Back to Song Details
            text = await _click_button("Edit Name / Metadata")
            assert "Edit Metadata" in text
            text = await _click_button("Edit Title")
            assert "Enter New Title" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Song Details &amp; Actions" in text

            # Navigate into Edit Cover -> Back to Song Details
            text = await _click_button("Edit Cover")
            assert "Edit Cover Art" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Song Details &amp; Actions" in text

            # Navigate into Edit Lyrics -> Back to Song Details
            text = await _click_button("Edit Lyrics")
            assert "Edit Lyrics" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Song Details &amp; Actions" in text

            # Navigate into Auto Re-Search -> Custom Search -> Back to Song Details
            text = await _click_button("Auto Re-Search")
            assert "Re-Search Song Data" in text
            text = await _click_button("Custom Search")
            assert "Custom Re-Search" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Song Details &amp; Actions" in text

            # Navigate into Import Data -> Back to Song Details
            text = await _click_button("Import Data")
            assert "Import Song Data" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Song Details &amp; Actions" in text

            # Click `◀ Back` from Song Details -> returns to Music Library Browser
            text = await _click_button(BTN_BACK_LABEL)
            assert "Music Library Browser (1 track(s))" in text

            # Click `🔎 Search Library` -> `◀ Back` -> `🔄 Rescan Library Folder` -> `◀ Back` to Home
            text = await _click_button("Search Library")
            assert "Search Music Library" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Music Library Browser (1 track(s))" in text

            text = await _click_button("Rescan Library Folder")
            assert "Library Rescan Dry-Run Preview" in text
            text = await _click_button("Confirm & Sync Database")
            assert "Library Rescan &amp; Sync Complete" in text

            text = await _click_button(BTN_BACK_LABEL)
            assert "Automated Music Ingestion Bot" in text

            # 2. From Home screen, navigate Status, SSO, Help, and Admin Panel via inline buttons & `◀ Back`
            text = await _click_button("Status")
            assert "System &amp; Session Status" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Automated Music Ingestion Bot" in text

            text = await _click_button("Authentik SSO")
            assert "Authentik OIDC Single Sign-On" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Automated Music Ingestion Bot" in text

            text = await _click_button("Help")
            assert "Music Ingestion Gateway Guide" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Automated Music Ingestion Bot" in text

            # Navigate Admin Panel -> User Management -> Add User -> Back -> User Profile -> Back -> Home
            text = await _click_button("Admin Panel")
            assert "Administrator Control Panel" in text
            text = await _click_button("User Management")
            assert "User Management" in text
            text = await _click_button("Add / Authorize User")
            assert "Authorize or Promote User by Telegram ID" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "User Management" in text
            text = await _click_button("@owner_u")
            assert "Managing User: @owner_u" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "User Management" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Administrator Control Panel" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Automated Music Ingestion Bot" in text

            # 3. Simulate older local `telegram-bot-api` server rejecting `CopyTextButton` (`copy_text`)
            screen_state["reject_copy_text"] = True
            text = await _click_button("Songs")
            assert "Music Library Browser" in text
            text = await _click_button("Rie Takahashi - Stay Alive")
            assert "Song Details &amp; Actions" in text
            fallback_markup = screen_state["reply_markup"]
            assert isinstance(fallback_markup, InlineKeyboardMarkup)
            assert all(
                getattr(btn, "copy_text", None) is None
                for row in fallback_markup.inline_keyboard
                for btn in row
            )
            assert strip_copy_text_buttons(fallback_markup) is None

            # Verify `safe_answer` also strips `CopyTextButton` on `SendMessage` when rejected
            active_msg = Message(
                message_id=int(str(screen_state["message_id"])),
                date=datetime.now(UTC),
                chat=tg_chat,
                from_user=tg_user,
                text="test",
            ).as_(bot)
            await safe_answer(
                active_msg,
                "Guest card",
                reply_markup=build_guest_onboarding_keyboard(None, telegram_id=999),
            )
            assert "Guest card" in str(screen_state["text"])
        finally:
            await bot.session.close()

    asyncio.run(_scenario())


def test_song_provenance_history_user_activity_and_job_pause_review_controls(tmp_path: Path) -> None:
    """Verify ordered song menu, provenance/history, audio forwarding, user activity, and job controls."""
    db = SqliteDatabaseManager(tmp_path / "provenance_sim.db")
    repo = SqliteCatalogRepository(db)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))
    lib_dir = tmp_path / "library"
    scratch_dir = tmp_path / "scratch"
    organizer = LibraryOrganizer(lib_dir)

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Stay Alive",
                artist="Rie Takahashi",
                album="Stay Alive",
                year=2016,
                track_number=1,
                duration_seconds=266,
                recording_mbid="rec-stay-alive",
                release_mbid="rel-stay-alive",
            ),
            confidence=1.0,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=SevenZipArchiveExtractor(scratch_root=scratch_dir),
        fingerprinter=StubFingerprinter("fp-stay-alive"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=organizer,
        catalog_repository=repo,
    )
    worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repo,
        governance_repository=repo,
    )
    settings = AppSettings(
        telegram_api_id=12345,
        telegram_api_hash="0123456789abcdef",
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        storage_scratch_dir=scratch_dir,
        storage_library_dir=lib_dir,
        database_path=tmp_path / "provenance_sim.db",
    )

    # 1. Verify Song Action Keyboard ordering (Retrieval & History in rows 0-1, Editing in rows 2-4)
    song_kb = build_song_action_keyboard("trk-1234567890")
    assert [btn.text for btn in song_kb.inline_keyboard[0]] == [
        "📥 Get Song File",
        "💿 Get Full Album",
    ]
    assert [btn.text for btn in song_kb.inline_keyboard[1]] == [
        "📤 Export Data",
        "📜 Song History",
    ]
    assert [btn.text for btn in song_kb.inline_keyboard[2]] == [
        "✏️ Edit Name / Metadata",
        "🖼️ Edit Cover",
    ]
    assert [btn.text for btn in song_kb.inline_keyboard[3]] == [
        "📝 Edit Lyrics",
        "🔎 Auto Re-Search",
    ]
    assert song_kb.inline_keyboard[4][0].text == "📥 Import Data"
    assert "Copy Track ID" in song_kb.inline_keyboard[4][1].text

    async def _scenario() -> None:
        await db.initialize()
        owner_account = await gov.ensure_user(
            telegram_id=1001,
            username="owner_u",
            display_name="Owner User",
        )

        # Enqueue and ingest a track via worker so job + track provenance are linked
        src_audio = tmp_path / "01 - Stay Alive.flac"
        src_audio.write_bytes(b"fLaC-stay-alive")
        job, _ = await worker.enqueue_job(
            user=owner_account,
            source_path=src_audio,
            original_filename="01 - Stay Alive.flac",
        )
        res = await worker.execute_job(job.job_id)
        assert res is not None and res.job.state == JobState.COMPLETED

        tracks = await repo.list_library_tracks()
        assert len(tracks) == 1
        seeded_track = tracks[0]
        assert seeded_track.uploaded_by == 1001
        assert seeded_track.modification_count == 0

        # Wire real aiogram Bot and Dispatcher
        bot = Bot(token=settings.telegram_bot_token)
        rbac_mw = RbacMiddleware(governance_service=gov, oidc_enabled=False)
        dp = Dispatcher()
        dp.message.outer_middleware(rbac_mw)
        dp.callback_query.outer_middleware(rbac_mw)
        dp["pipeline"] = pipeline
        dp["settings"] = settings
        dp["catalog_repository"] = repo
        dp["governance_repository"] = repo
        dp["governance_service"] = gov
        dp["queue_worker"] = worker
        dp.include_router(build_root_router())

        tg_user = User(id=1001, is_bot=False, first_name="Owner", username="owner_u")
        tg_chat = Chat(id=1001, type="private")

        dispatched_methods: list[TelegramMethod[object]] = []
        screen_state: dict[str, object] = {
            "message_id": 200,
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
            if isinstance(method, SendAudio):
                msg_id = int(str(screen_state["message_id"])) + 1
                screen_state["message_id"] = msg_id
                return Message(
                    message_id=msg_id,
                    date=datetime.now(UTC),
                    chat=tg_chat,
                    from_user=tg_user,
                    audio=Audio(
                        file_id="cached-tg-file-id-999",
                        file_unique_id="uniq-999",
                        duration=266,
                    ),
                )
            if isinstance(method, ForwardMessage):
                msg_id = int(str(screen_state["message_id"])) + 1
                screen_state["message_id"] = msg_id
                return Message(
                    message_id=msg_id,
                    date=datetime.now(UTC),
                    chat=tg_chat,
                    from_user=tg_user,
                    text="forwarded",
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

        async def _click_button(label_substring: str, *, expect_edit: bool = True) -> str:
            nonlocal next_update_id
            markup = screen_state["reply_markup"]
            assert isinstance(markup, InlineKeyboardMarkup)
            matched_callback: str | None = None
            for row in markup.inline_keyboard:
                for btn in row:
                    if label_substring in btn.text and btn.callback_data:
                        matched_callback = btn.callback_data
                        break
                if matched_callback is not None:
                    break
            assert matched_callback is not None, f"Button {label_substring!r} not found"

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
                chat_instance="ci-2",
                message=active_msg,
                data=matched_callback,
            )
            upd = Update(update_id=next_update_id, callback_query=cb_query)
            next_update_id += 1
            await dp.feed_update(bot, upd)

            recent = dispatched_methods[before_count:]
            assert any(isinstance(m, AnswerCallbackQuery) for m in recent)
            if expect_edit:
                assert any(isinstance(m, EditMessageText) for m in recent)
            return str(screen_state["text"])

        try:
            # 2. Open `/songs` -> view song -> verify provenance & Never (0 edits)
            await _send_user_text("/songs")
            text = await _click_button("Rie Takahashi - Stay Alive")
            assert "Added By: <code>@owner_u (1001)</code>" in text
            assert "Modified: <code>Never (0 edits)</code>" in text

            # First `📥 Get Song File` uploads via `SendAudio` and caches file/message IDs
            before_dl = len(dispatched_methods)
            await _click_button("Get Song File", expect_edit=False)
            assert any(isinstance(m, SendAudio) for m in dispatched_methods[before_dl:])

            # Second `📥 Get Song File` forwards the cached message via `ForwardMessage`!
            before_fwd = len(dispatched_methods)
            await _click_button("Get Song File", expect_edit=False)
            assert any(isinstance(m, ForwardMessage) for m in dispatched_methods[before_fwd:])

            # Edit title -> dry-run preview -> Confirm & Apply -> modification_count becomes 1
            await _click_button("Edit Name / Metadata")
            await _click_button("Edit Title")
            text = await _send_user_text("Stay Alive (TV Size)")
            assert "Dry-Run Preview" in text
            text = await _click_button("Accept All Changes")
            assert "Modified: <code>1 time(s)</code>" in text
            assert "Last by <code>@owner_u (1001)</code>" in text

            # Open `📜 Song History` -> shows chronological edit history -> `◀ Back` returns to Song Details
            text = await _click_button("Song History")
            assert "Song Provenance &amp; Edit History" in text
            assert "Total Modifications: <code>1</code>" in text
            assert "SONG_UPDATED_INTERACTIVE" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Song Details &amp; Actions" in text

            # 3. Open `/admin` -> User Management -> `@owner_u` -> verify Uploads & Changes
            await _send_user_text("/admin")
            await _click_button("User Management")
            text = await _click_button("@owner_u")
            assert "Total Uploads: 1 albums (1 tracks)" in text
            assert "Recorded Actions / Edits:" in text

            text = await _click_button("User Uploads")
            assert "Uploaded Songs — @owner_u (1)" in text
            assert "Rie Takahashi — Stay Alive (TV Size)" in text
            assert "1 edit(s)" in text
            # Tapping a song inside User Uploads opens Song Details, and `◀ Back` returns to User Uploads!
            text = await _click_button("Rie Takahashi - Stay Alive (TV Size)")
            assert "Song Details &amp; Actions" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Uploaded Songs — @owner_u (1)" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Managing User: @owner_u" in text

            text = await _click_button("User Changes & History")
            assert "Chronological Activity &amp; Changes — @owner_u" in text
            assert "SONG_UPDATED_INTERACTIVE" in text
            assert "INGEST_SUCCESS" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Managing User: @owner_u" in text

            # 3b. Interactive Help topics & context-aware `◀ Back` when launching a module from Help
            text = await _send_user_text("/help")
            assert "Music Ingestion Gateway Guide" in text
            text = await _click_button("Ingestion & Pipeline")
            assert "Help Guide — Audio &amp; Archive Ingestion" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Music Ingestion Gateway Guide" in text

            text = await _click_button("Queue & Review")
            assert "Help Guide — Queue &amp; Manual Review" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Music Ingestion Gateway Guide" in text

            text = await _click_button("Songs & Editor")
            assert "Help Guide — Songs Library &amp; Editor" in text
            # Launch Songs Browser from inside `help:songs`, then press `◀ Back` -> returns to `help:songs`!
            text = await _click_button("Open Songs Browser")
            assert "Music Library Browser" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Help Guide — Songs Library &amp; Editor" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Music Ingestion Gateway Guide" in text

            # 4. Enqueue a pending job -> test Queue Overview, `📥 Get Queued File`, Pause/Unlock,
            # context-aware `◀ Back` (`admin:queue` vs `queue:mine`), and `✏️ Manual Tag & Editor`
            pipeline._fingerprinter = StubFingerprinter("fp-wishing")
            pipeline._metadata = StubMetadataProvider(
                [
                    MetadataCandidate(
                        metadata=TrackMetadata(
                            title="Wishing",
                            artist="Inori Minase",
                            album="Stay Alive",
                            year=2016,
                            track_number=2,
                            duration_seconds=360,
                            recording_mbid="rec-wishing",
                            release_mbid="rel-stay-alive",
                        ),
                        confidence=1.0,
                    )
                ]
            )
            pending_audio = tmp_path / "02 - Wishing.flac"
            pending_audio.write_bytes(b"fLaC-wishing")
            pending_job, _ = await worker.enqueue_job(
                user=owner_account,
                source_path=pending_audio,
                original_filename="02 - Wishing.flac",
                job_id="pending-job-wishing-001",
            )

            # Open Queue from `/admin` -> `admin:queue` -> click item -> `◀ Back` returns to `admin:queue`
            await _send_user_text("/admin")
            text = await _click_button("View Queue")
            assert "Ingestion Queue" in text
            text = await _click_button("02 - Wishing.flac")
            assert "Ingestion Job Inspector" in text
            text = await _click_button(BTN_BACK_LABEL)
            assert "Ingestion Queue" in text

            # Paste the Job ID directly in idle chat -> opens interactive `Ingestion Job Inspector`!
            await _send_user_text("/cancel")
            text = await _send_user_text(pending_job.job_id)
            assert "Ingestion Job Inspector" in text
            assert pending_job.job_id in text

            # Click `📥 Get Queued File` -> delivers the queued audio file via `SendAudio`
            before_q_dl = len(dispatched_methods)
            await _click_button("Get Queued File", expect_edit=False)
            assert any(isinstance(m, SendAudio) for m in dispatched_methods[before_q_dl:])

            # Click `⏸️ Pause / Hold` -> job state becomes `PAUSED` and worker skips auto-execution
            text = await _click_button("Pause / Hold")
            assert "Ingestion Paused (Held for Review)" in text
            skipped_res = await worker.execute_job(pending_job.job_id)
            assert skipped_res is None

            # Click `▶️ Unlock & Resume` -> job returns to `QUEUED`
            text = await _click_button("Unlock & Resume")
            assert "Ingestion Queued" in text

            # Click `🔎 Manual Review & Tag` -> forces pipeline to stop at `AWAITING_CONFIRMATION`
            text = await _click_button("Manual Review")
            assert "Confirmation Required" in text

            # Click `✏️ Manual Tag & Editor` -> opens full-pipeline Dry-Run & Tag Editor for the queued job
            text = await _click_button("Manual Tag & Editor")
            assert "Queued Job Tag Editor &amp; Dry-Run Preview" in text

            # Edit Title via `🏷️ Edit Title`
            text = await _click_button("Edit Title")
            assert "Enter New Title" in text
            text = await _send_user_text("Wishing (Remastered)")
            assert "Queued Job Tag Editor &amp; Dry-Run Preview" in text
            assert "Wishing (Remastered)" in text

            # Edit Lyrics via `📝 Edit Lyrics`
            text = await _click_button("Edit Lyrics")
            assert "Set Lyrics for Queued Job" in text
            text = await _send_user_text("[00:02.00] Wishing synced line")
            assert "Queued Job Tag Editor &amp; Dry-Run Preview" in text
            assert "Synced LRC" in text

            # Confirm & Ingest Track via `✅ Confirm & Ingest Track`
            text = await _click_button("Confirm & Ingest Track")
            assert "Track Ingested" in text
            all_tracks = await repo.list_library_tracks()
            assert any(t.title == "Wishing (Remastered)" for t in all_tracks)
        finally:
            await bot.session.close()

    asyncio.run(_scenario())


def test_multi_file_forwarding_and_media_group_batching(
    tmp_path: Path,
    sample_settings: AppSettings,
) -> None:
    """Verify multiple forwarded files or media group albums are batched into a single queue message."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True)
    db = SqliteDatabaseManager(tmp_path / "library.db")
    repo = SqliteCatalogRepository(db)

    f1 = scratch / "Track_01.flac"
    f1.write_bytes(b"fLaC-track-01")
    f2 = scratch / "Track_02.flac"
    f2.write_bytes(b"fLaC-track-02")
    f3 = scratch / "Track_03.flac"
    f3.write_bytes(b"fLaC-track-03")

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(title="Song", artist="Artist", album="Album", year=2024, track_number=1),
            confidence=0.99,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=SevenZipArchiveExtractor(scratch_root=scratch),
        fingerprinter=StubFingerprinter("FP_BATCH"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=repo,
        auto_approve_confidence=0.90,
    )
    worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repo,
        governance_repository=repo,
        concurrency_limit=2,
    )

    settings = sample_settings.model_copy(
        update={
            "storage_scratch_dir": scratch,
            "database_path": tmp_path / "library.db",
        }
    )

    async def _scenario() -> None:
        await db.initialize()
        uploader = UserAccount(
            telegram_id=987654321,
            display_name="MultiForwarder",
            username="forwarder",
            role=UserRole.UPLOADER,
        )
        await repo.upsert_user(uploader)

        bot_mock = MagicMock()

        # Mock get_file returning files that exist on disk
        async def _mock_get_file(fid: str) -> MagicMock:
            tg_f = MagicMock()
            if fid == "f1":
                tg_f.file_path = str(f1)
            elif fid == "f2":
                tg_f.file_path = str(f2)
            elif fid == "f3":
                tg_f.file_path = str(f3)
            elif fid == "f_toobig":
                raise TelegramBadRequest(method=MagicMock(), message="Bad Request: file is too big")
            else:
                tg_f.file_path = str(f1)
            return tg_f

        bot_mock.get_file = AsyncMock(side_effect=_mock_get_file)

        # 1. Simulate 3 forwarded messages arriving together with forward_date
        msg1 = MagicMock(spec=Message)
        msg1.bot = bot_mock
        msg1.chat = MagicMock(id=111222)
        msg1.audio = MagicMock(file_id="f1", file_name="01. Song One.flac", file_size=10_000_000)
        msg1.document = None
        msg1.forward_date = 1700000000
        msg1.media_group_id = None
        msg1.answer = AsyncMock()

        msg2 = MagicMock(spec=Message)
        msg2.bot = bot_mock
        msg2.chat = MagicMock(id=111222)
        msg2.audio = MagicMock(file_id="f2", file_name="02. Song Two.flac", file_size=12_000_000)
        msg2.document = None
        msg2.forward_date = 1700000001
        msg2.media_group_id = None
        msg2.answer = AsyncMock()

        msg3 = MagicMock(spec=Message)
        msg3.bot = bot_mock
        msg3.chat = MagicMock(id=111222)
        msg3.audio = MagicMock(file_id="f3", file_name="03. Song Three.flac", file_size=15_000_000)
        msg3.document = None
        msg3.forward_date = 1700000002
        msg3.media_group_id = None
        msg3.answer = AsyncMock()

        # Call with small debounce (0.1s)
        await handle_incoming_media(
            msg1,
            current_user=uploader,
            queue_worker=worker,
            pipeline=pipeline,
            governance_repository=repo,
            settings=settings,
            batch_debounce_seconds=0.1,
        )
        await handle_incoming_media(
            msg2,
            current_user=uploader,
            queue_worker=worker,
            pipeline=pipeline,
            governance_repository=repo,
            settings=settings,
            batch_debounce_seconds=0.1,
        )
        await handle_incoming_media(
            msg3,
            current_user=uploader,
            queue_worker=worker,
            pipeline=pipeline,
            governance_repository=repo,
            settings=settings,
            batch_debounce_seconds=0.1,
        )

        # Wait for debounce to complete
        await asyncio.sleep(0.3)

        # Only ONE single message was sent for the batch (to the last message in the batch)
        assert msg1.answer.call_count == 0
        assert msg2.answer.call_count == 0
        assert msg3.answer.call_count == 1

        batch_reply = str(msg3.answer.call_args[0][0])
        assert "Ingestion Queued (3/3 files)" in batch_reply
        assert "01. Song One.flac" in batch_reply
        assert "02. Song Two.flac" in batch_reply
        assert "03. Song Three.flac" in batch_reply

        # All 3 were enqueued in repository
        queued = await repo.list_queued_jobs()
        assert len(queued) == 3
        filenames = {j.original_filename for j in queued}
        assert filenames == {"01. Song One.flac", "02. Song Two.flac", "03. Song Three.flac"}

        # 2. Test Media Group album containing an oversized file (>20MB)
        msg_ok = MagicMock(spec=Message)
        msg_ok.bot = bot_mock
        msg_ok.chat = MagicMock(id=333444)
        msg_ok.audio = MagicMock(file_id="f1", file_name="Album_01.flac", file_size=18_000_000)
        msg_ok.document = None
        msg_ok.media_group_id = "album_999"
        msg_ok.answer = AsyncMock()

        msg_big = MagicMock(spec=Message)
        msg_big.bot = bot_mock
        msg_big.chat = MagicMock(id=333444)
        msg_big.audio = MagicMock(file_id="f_toobig", file_name="Album_02_Big.flac", file_size=25_000_000)
        msg_big.document = None
        msg_big.media_group_id = "album_999"
        msg_big.answer = AsyncMock()

        await handle_incoming_media(
            msg_ok,
            current_user=uploader,
            queue_worker=worker,
            pipeline=pipeline,
            governance_repository=repo,
            settings=settings,
            batch_debounce_seconds=0.1,
        )
        await handle_incoming_media(
            msg_big,
            current_user=uploader,
            queue_worker=worker,
            pipeline=pipeline,
            governance_repository=repo,
            settings=settings,
            batch_debounce_seconds=0.1,
        )

        await asyncio.sleep(0.3)

        assert msg_ok.answer.call_count == 0
        assert msg_big.answer.call_count == 1
        mixed_reply = str(msg_big.answer.call_args[0][0])
        assert "Ingestion Queued (1/2 files)" in mixed_reply
        assert "Album_01.flac" in mixed_reply
        assert "Album_02_Big.flac" in mixed_reply
        assert "exceeds Telegram Cloud Bot API 20 MB limit" in mixed_reply

    asyncio.run(_scenario())


def test_queue_worker_background_loop_and_missing_file_recovery(
    tmp_path: Path,
    sample_settings: AppSettings,
) -> None:
    """Verify background queue loop executes jobs and handles missing files without hanging."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True)
    db = SqliteDatabaseManager(tmp_path / "library.db")
    repo = SqliteCatalogRepository(db)

    valid_audio = scratch / "Valid_Track.flac"
    valid_audio.write_bytes(b"fLaC-valid-track")
    ghost_audio = scratch / "NonExistent_Ghost.flac"

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(title="Valid", artist="Singer", album="Album", year=2024, track_number=1),
            confidence=0.99,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=SevenZipArchiveExtractor(scratch_root=scratch),
        fingerprinter=StubFingerprinter("FP_VALID"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=repo,
        auto_approve_confidence=0.90,
    )
    worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repo,
        governance_repository=repo,
        concurrency_limit=1,
    )

    async def _scenario() -> None:
        await db.initialize()
        uploader = UserAccount(
            telegram_id=123123123,
            display_name="QueueTester",
            username="queuetester",
            role=UserRole.UPLOADER,
        )
        await repo.upsert_user(uploader)

        # Enqueue ghost job first (missing file on disk)
        ghost_job, _ = await worker.enqueue_job(
            user=uploader,
            source_path=ghost_audio,
            original_filename="NonExistent_Ghost.flac",
            file_size_bytes=20_000_000,
        )
        assert ghost_job.state == JobState.QUEUED

        # Enqueue valid job second
        valid_job, _ = await worker.enqueue_job(
            user=uploader,
            source_path=valid_audio,
            original_filename="Valid_Track.flac",
            file_size_bytes=valid_audio.stat().st_size,
        )
        assert valid_job.state == JobState.QUEUED

        # Start autonomous queue loop
        stop_event = asyncio.Event()
        loop_task = asyncio.create_task(worker.run_worker_loop(stop_event=stop_event))

        # Give worker loop time to process both jobs
        for _ in range(30):
            await asyncio.sleep(0.1)
            g_check = await repo.get_job(ghost_job.job_id)
            v_check = await repo.get_job(valid_job.job_id)
            ghost_done = g_check is not None and g_check.state == JobState.FAILED
            valid_done = v_check is not None and v_check.state == JobState.COMPLETED
            if ghost_done and valid_done:
                break

        stop_event.set()
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task

        # Verify ghost job was marked FAILED with clean error message
        finished_ghost = await repo.get_job(ghost_job.job_id)
        assert finished_ghost is not None
        assert finished_ghost.state == JobState.FAILED
        assert "Source audio file not found on disk" in (finished_ghost.error_message or "")

        # Verify valid job was successfully processed to COMPLETED without being blocked!
        finished_valid = await repo.get_job(valid_job.job_id)
        assert finished_valid is not None
        assert finished_valid.state == JobState.COMPLETED

    asyncio.run(_scenario())
