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
    parse_access_request_callback,
    parse_admin_callback,
)
from src.bot.routers.commands import (
    handle_queue_command,
    handle_sso_command,
    handle_start,
    handle_status,
)
from src.bot.routers.files import format_ingestion_summary, handle_incoming_media
from src.bot.routers.verification import (
    handle_manual_search_query,
    handle_verification_callback,
    parse_verification_callback,
)
from src.config.settings import AppSettings
from src.core.extractor import SevenZipArchiveExtractor
from src.core.models import (
    DuplicateCheckResult,
    IngestionJob,
    IngestionResult,
    JobState,
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
    assert owner_menu.keyboard[0][0].text == BTN_MAIN_QUEUE
    assert owner_menu.keyboard[0][1].text == BTN_MAIN_SSO
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
    assert len(root.sub_routers) == 4

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

        # 1. Forwarded 32.2 MB track triggers get_file, resolves WSL \uf03a path, and pauses for confirmation
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
        assert msg.answer.await_count == 2
        second_reply_text = msg.answer.await_args_list[1].args[0]
        assert "Confirmation Required" in second_reply_text
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

        # 4. Forwarding the same track again immediately replies with Duplicate Rejected
        msg.answer.reset_mock()
        await handle_incoming_media(
            msg,
            current_user=uploader,
            queue_worker=worker,
            pipeline=pipeline,
            governance_repository=repo,
            settings=settings,
            state=fsm,
        )
        dup_reply = msg.answer.await_args_list[1].args[0]
        assert "Duplicate Rejected" in dup_reply

    asyncio.run(_scenario())
