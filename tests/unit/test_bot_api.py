"""Unit tests for the Telegram interface layer (`src.bot`)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.client.telegram import PRODUCTION
from aiogram.types import CallbackQuery, Message, User

from src.bot.api_server import build_telegram_api_server, resolve_local_api_file_path
from src.bot.keyboards import (
    BTN_MAIN_ADMIN,
    BTN_MAIN_INFO,
    BTN_MAIN_QUEUE,
    BTN_MAIN_SONGS,
    BTN_MAIN_SSO,
    build_access_request_approval_keyboard,
    build_admin_dashboard_keyboard,
    build_guest_onboarding_keyboard,
    build_main_menu_keyboard,
    build_oidc_link_keyboard,
    build_queued_job_keyboard,
    build_user_detail_keyboard,
    build_user_list_keyboard,
    build_verification_keyboard,
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
    assert len(markup.inline_keyboard) == 2
    first_btn = markup.inline_keyboard[0][0]
    assert first_btn.callback_data == "verify:select:job99:0"
    assert first_btn.text.startswith("1. [82%] Artist - Track A")

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
    assert len(approval_kb.inline_keyboard[0]) == 3
    assert approval_kb.inline_keyboard[0][0].callback_data == "access_req:approve:7:UPLOADER"

    queue_kb = build_queued_job_keyboard("job-uuid-1")
    assert queue_kb.inline_keyboard[0][0].callback_data == "queue:cancel:job-uuid-1"

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
    assert detail_kb.inline_keyboard[0][1].callback_data == "admin:demote:987654321:GUEST"

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
    assert "<b>1. [98%]</b>" in confirm_html
    assert "https://musicbrainz.org/release/ca344c5b-9c69-4283-8383-e7c4c026d571" in confirm_html

    job.final_path = Path("/music/Artist/Album/01 - a.flac")
    job.state = JobState.COMPLETED
    done_result = IngestionResult(job=job)
    assert "Track Ingested" in format_ingestion_summary(done_result)


def test_build_root_router_and_rbac_middleware_and_admin_handlers(tmp_path: Path) -> None:
    """Verify root router, `RbacMiddleware` guest/blocked gating, `/start`, `/adduser`, and `/admin` flows."""
    root = build_root_router()
    assert len(root.sub_routers) == 5

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
        assert "Added on Disk:</b> 1" in rescan_preview

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
