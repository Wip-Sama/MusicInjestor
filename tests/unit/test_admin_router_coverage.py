"""Comprehensive unit tests covering admin router branches, edge cases, and error handling."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from src.bot.routers.admin import (
    _authorize_user_by_spec,
    _deliver_queued_job_file,
    handle_access_request_callback,
    handle_adduser_command,
    handle_admin_command,
    handle_admin_dashboard_callback,
    handle_new_user_id_input,
    handle_queue_cancel_callback,
    handle_queue_job_control_callback,
    handle_queue_view_callback,
    handle_wipe_command,
    parse_access_request_callback,
    parse_admin_callback,
)
from src.bot.states import AdminStates
from src.config.settings import AppSettings
from src.core.exceptions import PermissionDeniedError
from src.core.models import (
    IngestionJob,
    IngestionResult,
    LibraryTrackRecord,
    TagCorrectionRequest,
    TrackMetadata,
    UserAccount,
    UserRole,
)


def _make_context(bot: AsyncMock, chat_id: int = 12345, user_id: int = 12345) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=bot.id or 1, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _make_message(text: str = "/admin", user_id: int = 12345) -> Message:
    bot = AsyncMock()
    bot.id = 1
    chat = Chat(id=user_id, type="private")
    from_user = User(id=user_id, is_bot=False, first_name="TestAdmin")
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.message_id = 999
    msg.chat = chat
    msg.from_user = from_user
    msg.bot = bot
    msg.answer = AsyncMock()
    msg.answer_audio = AsyncMock()
    msg.answer_document = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _make_callback(data: str, user_id: int = 12345) -> CallbackQuery:
    bot = AsyncMock()
    bot.id = 1
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.id = "cb_123"
    cb.bot = bot
    cb.from_user = User(id=user_id, is_bot=False, first_name="TestUser")
    msg = _make_message(user_id=user_id)
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


def test_parse_access_request_and_admin_callback_errors():
    with pytest.raises(ValueError, match="Malformed access_req"):
        parse_access_request_callback("invalid")

    with pytest.raises(ValueError, match="Unsupported access_req"):
        parse_access_request_callback("access_req:deny")

    assert parse_access_request_callback("access_req:request") == ("request", None, None)
    assert parse_access_request_callback("access_req:oidc_disabled") == ("oidc_disabled", None, None)
    assert parse_access_request_callback("access_req:deny:42") == ("deny", 42, None)
    assert parse_access_request_callback("access_req:approve:42:UPLOADER") == (
        "approve",
        42,
        UserRole.UPLOADER,
    )

    with pytest.raises(ValueError, match="Malformed admin"):
        parse_admin_callback("invalid")

    with pytest.raises(ValueError, match="Unsupported admin"):
        parse_admin_callback("admin:user")

    assert parse_admin_callback("admin:panel") == ("panel", None, None)
    assert parse_admin_callback("admin:resolve_job:job123") == ("resolve_job", None, None)
    assert parse_admin_callback("admin:user:123") == ("user", 123, None)
    assert parse_admin_callback("admin:promote:123:MODERATOR") == ("promote", 123, UserRole.MODERATOR)


@pytest.mark.anyio
async def test_authorize_user_by_spec_branches():
    actor = UserAccount(telegram_id=1, display_name="Owner", role=UserRole.OWNER)
    gov_service = AsyncMock()
    gov_repo = AsyncMock()

    with pytest.raises(ValueError, match="Please provide a numeric Telegram user ID"):
        await _authorize_user_by_spec("", actor, gov_service, gov_repo)

    gov_repo.get_user.return_value = None
    gov_service.change_user_role.return_value = UserAccount(
        telegram_id=99, display_name="User 99", role=UserRole.MODERATOR
    )

    user = await _authorize_user_by_spec("99 MODERATOR", actor, gov_service, gov_repo)
    assert user.role == UserRole.MODERATOR
    gov_service.ensure_user.assert_awaited_once_with(telegram_id=99, display_name="User 99")


@pytest.mark.anyio
async def test_handle_admin_command_permissions_and_render():
    msg = _make_message("/admin", user_id=10)
    bot = msg.bot
    state = _make_context(bot, 10, 10)

    guest = UserAccount(telegram_id=10, display_name="Guest", role=UserRole.GUEST)
    await handle_admin_command(msg, current_user=guest, state=state)
    msg.answer.assert_awaited()
    assert "Administrator Privileges Required" in msg.answer.call_args[0][0]

    admin = UserAccount(telegram_id=10, display_name="Admin", role=UserRole.ADMIN)
    msg.answer.reset_mock()
    await handle_admin_command(msg, current_user=admin, governance_repository=None, state=state)
    assert "Admin Panel Unavailable" in msg.answer.call_args[0][0]

    gov_repo = AsyncMock()
    gov_repo.list_users.return_value = [admin]
    gov_repo.list_pending_access_requests.return_value = []
    gov_repo.list_active_jobs.return_value = []
    gov_repo.list_queued_jobs.return_value = []
    queue_worker = AsyncMock()
    queue_worker.concurrency_limit = 2
    settings = MagicMock(spec=AppSettings)
    settings.node_name = "TestNode"

    msg.answer.reset_mock()
    await handle_admin_command(
        msg,
        current_user=admin,
        governance_repository=gov_repo,
        queue_worker=queue_worker,
        settings=settings,
        state=state,
    )
    assert msg.answer.await_count == 1


@pytest.mark.anyio
async def test_handle_adduser_command_all_paths():
    msg = _make_message("/adduser", user_id=10)
    bot = msg.bot
    state = _make_context(bot, 10, 10)
    guest = UserAccount(telegram_id=10, display_name="Guest", role=UserRole.GUEST)
    admin = UserAccount(telegram_id=10, display_name="Admin", role=UserRole.ADMIN)

    await handle_adduser_command(msg, current_user=guest, state=state)
    assert "Administrator Privileges Required" in msg.answer.call_args[0][0]

    msg.answer.reset_mock()
    await handle_adduser_command(msg, current_user=admin, governance_service=None, state=state)
    assert "Governance Unavailable" in msg.answer.call_args[0][0]

    gov_service = AsyncMock()
    gov_repo = AsyncMock()
    msg.answer.reset_mock()
    await handle_adduser_command(
        msg, current_user=admin, governance_service=gov_service, governance_repository=gov_repo, state=state
    )
    assert "Authorize User by Telegram ID" in msg.answer.call_args[0][0]

    msg.text = "/adduser invalid_id"
    msg.answer.reset_mock()
    await handle_adduser_command(
        msg, current_user=admin, governance_service=gov_service, governance_repository=gov_repo, state=state
    )
    assert "Authorization Failed" in msg.answer.call_args[0][0]

    msg.text = "/adduser 999 UPLOADER"
    gov_repo.get_user.return_value = None
    target_user = UserAccount(telegram_id=999, display_name="User 999", role=UserRole.UPLOADER)
    gov_service.change_user_role.return_value = target_user
    gov_repo.count_user_uploads.return_value = (1, 2)
    msg.answer.reset_mock()
    await handle_adduser_command(
        msg, current_user=admin, governance_service=gov_service, governance_repository=gov_repo, state=state
    )
    assert msg.answer.await_count == 1


@pytest.mark.anyio
async def test_handle_new_user_id_input_fsm():
    msg = _make_message("999 UPLOADER", user_id=10)
    bot = msg.bot
    state = _make_context(bot, 10, 10)
    await state.set_state(AdminStates.awaiting_new_user_id)

    guest = UserAccount(telegram_id=10, display_name="Guest", role=UserRole.GUEST)
    await handle_new_user_id_input(msg, state, current_user=guest)
    assert "Session Expired" in msg.answer.call_args[0][0]

    admin = UserAccount(telegram_id=10, display_name="Admin", role=UserRole.ADMIN)
    gov_service = AsyncMock()
    gov_repo = AsyncMock()

    msg.text = "invalid"
    msg.answer.reset_mock()
    await handle_new_user_id_input(
        msg, state, current_user=admin, governance_service=gov_service, governance_repository=gov_repo
    )
    assert "Invalid User Specification" in msg.answer.call_args[0][0]

    msg.text = "555 MODERATOR"
    target = UserAccount(telegram_id=555, display_name="User 555", role=UserRole.MODERATOR)
    gov_repo.get_user.return_value = target
    gov_service.change_user_role.return_value = target
    gov_repo.count_user_uploads.return_value = (0, 0)
    msg.answer.reset_mock()
    await handle_new_user_id_input(
        msg, state, current_user=admin, governance_service=gov_service, governance_repository=gov_repo
    )
    assert msg.answer.await_count == 1


@pytest.mark.anyio
async def test_handle_access_request_callback_branches():
    cb = _make_callback("access_req:oidc_disabled")
    user = UserAccount(telegram_id=10, display_name="User", role=UserRole.GUEST)
    gov_service = AsyncMock()

    await handle_access_request_callback(cb, current_user=user, governance_service=gov_service)
    cb.answer.assert_awaited()

    cb = _make_callback("access_req:request")
    uploader = UserAccount(telegram_id=10, display_name="Uploader", role=UserRole.UPLOADER)
    await handle_access_request_callback(cb, current_user=uploader, governance_service=gov_service)
    assert "You already hold" in cb.answer.call_args[0][0]

    gov_service.submit_access_request.return_value = (MagicMock(), False)
    await handle_access_request_callback(cb, current_user=user, governance_service=gov_service)
    assert "already pending" in cb.answer.call_args[0][0]

    admin = UserAccount(telegram_id=1, display_name="Admin", role=UserRole.ADMIN)
    cb = _make_callback("access_req:approve:42:UPLOADER")
    gov_service.approve_access_request.side_effect = PermissionDeniedError("Denied")
    await handle_access_request_callback(cb, current_user=admin, governance_service=gov_service)
    assert "Admin privileges required" in cb.answer.call_args[0][0]

    gov_service.approve_access_request.side_effect = None
    target_user = UserAccount(telegram_id=42, display_name="Target", role=UserRole.UPLOADER)
    gov_service.approve_access_request.return_value = target_user
    cb.bot.send_message.side_effect = Exception("DM blocked")
    await handle_access_request_callback(cb, current_user=admin, governance_service=gov_service)
    assert cb.message.edit_text.await_count >= 1

    cb = _make_callback("access_req:deny:42")
    gov_service.deny_access_request.side_effect = PermissionDeniedError("Denied")
    await handle_access_request_callback(cb, current_user=admin, governance_service=gov_service)
    assert "Admin privileges required" in cb.answer.call_args[0][0]

    req_mock = MagicMock()
    req_mock.telegram_id = 42
    gov_service.deny_access_request.side_effect = None
    gov_service.deny_access_request.return_value = req_mock
    await handle_access_request_callback(cb, current_user=admin, governance_service=gov_service)
    assert "Denied" in cb.message.edit_text.call_args[0][0]


@pytest.mark.anyio
async def test_deliver_queued_job_file_branches(tmp_path: Path):
    assert await _deliver_queued_job_file(None, MagicMock()) is False

    job = IngestionJob(job_id="j1", source_path=tmp_path / "nonexistent.flac", original_filename="song.flac")
    msg = _make_message()
    assert await _deliver_queued_job_file(msg, job) is False

    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"dummy audio")
    job.source_path = audio_file
    meta = TrackMetadata(artist="Artist", title="Title", album="Album")
    job.selected_metadata = meta

    assert await _deliver_queued_job_file(msg, job) is True
    msg.answer_audio.assert_awaited()

    doc_file = tmp_path / "archive.zip"
    doc_file.write_bytes(b"dummy archive")
    job.source_path = doc_file
    assert await _deliver_queued_job_file(msg, job) is True
    msg.answer_document.assert_awaited()


@pytest.mark.anyio
async def test_handle_admin_dashboard_callback_actions(tmp_path: Path):
    admin = UserAccount(telegram_id=1, display_name="Admin", role=UserRole.ADMIN)
    gov_service = AsyncMock()
    gov_repo = AsyncMock()
    queue_worker = AsyncMock()
    queue_worker.concurrency_limit = 2
    pipeline = AsyncMock()
    settings = MagicMock(spec=AppSettings)
    settings.node_name = "Node"
    settings.queue_concurrency_limit = 2

    cb = _make_callback("admin:resolve_job:j1")
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_repository=gov_repo, pipeline=None
    )
    assert "Pipeline unavailable" in cb.answer.call_args[0][0]

    pipeline.get_job.return_value = None
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Job not found" in cb.answer.call_args[0][0]

    target_job = IngestionJob(job_id="j1", source_path=tmp_path / "t.mp3", user_id=999)
    pipeline.get_job.return_value = target_job
    uploader = UserAccount(telegram_id=1, display_name="Uploader", role=UserRole.UPLOADER)
    await handle_admin_dashboard_callback(
        cb, current_user=uploader, governance_repository=gov_repo, pipeline=pipeline
    )
    assert "Moderator privileges required" in cb.answer.call_args[0][0]

    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_repository=gov_repo, pipeline=pipeline
    )
    assert cb.message.edit_text.await_count >= 1

    cb = _make_callback("admin:tags")
    gov_repo.list_pending_tag_correction_requests.return_value = []
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo)
    assert "No pending tag correction" in cb.answer.call_args[0][0]

    tag_req = TagCorrectionRequest(
        id=1,
        track_id="trk1",
        requested_by=10,
        proposed_title="New Title",
        proposed_artist="New Artist",
        proposed_album="New Album",
        proposed_year=2024,
        reason="Typo",
    )
    gov_repo.list_pending_tag_correction_requests.return_value = [tag_req]
    dummy_track = LibraryTrackRecord(
        id="trk1",
        job_id="j1",
        uploaded_by=10,
        storage_path=tmp_path / "song.mp3",
        fingerprint="fp",
        duration_seconds=120.0,
        artist="Old Artist",
        album="Old Album",
        title="Old Title",
    )
    gov_repo.get_library_track.return_value = dummy_track
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo)
    assert "Pending Tag Correction #1" in cb.message.edit_text.call_args[0][0]

    cb = _make_callback("admin:tag_approve:1")
    gov_service.approve_tag_correction.return_value = (tag_req, dummy_track)
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_service=gov_service, governance_repository=gov_repo
    )
    assert "Tag Correction #1 Approved" in cb.message.edit_text.call_args[0][0]

    cb = _make_callback("admin:tag_reject:1")
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_service=gov_service, governance_repository=gov_repo
    )
    assert "Rejected" in cb.message.edit_text.call_args[0][0]

    cb = _make_callback("admin:wipe")
    owner = UserAccount(telegram_id=1, display_name="Owner", role=UserRole.OWNER)
    gov_service.wipe_library_catalog.return_value = 5
    await handle_admin_dashboard_callback(
        cb, current_user=owner, governance_service=gov_service, governance_repository=gov_repo
    )
    assert "System Catalog Wipe Executed" in cb.message.edit_text.call_args[0][0]

    cb = _make_callback("admin:close")
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo)
    cb.message.delete.assert_awaited()

    cb = _make_callback("admin:panel")
    gov_repo.list_users.return_value = [admin]
    gov_repo.list_pending_access_requests.return_value = []
    gov_repo.list_active_jobs.return_value = []
    gov_repo.list_queued_jobs.return_value = []
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_repository=gov_repo, queue_worker=queue_worker, settings=settings
    )
    assert cb.message.edit_text.await_count >= 1

    cb = _make_callback("admin:users")
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo)
    assert "User Management" in cb.message.edit_text.call_args[0][0]

    state = _make_context(cb.bot, 1, 1)
    cb = _make_callback("admin:add_user")
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo, state=state)
    assert await state.get_state() == AdminStates.awaiting_new_user_id

    cb = _make_callback("admin:user:10")
    gov_repo.get_user.return_value = UserAccount(
        telegram_id=10, display_name="User 10", role=UserRole.UPLOADER
    )
    gov_repo.count_user_uploads.return_value = (0, 0)
    gov_repo.list_user_audit_logs.return_value = []
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo)
    assert "User 10" in cb.message.edit_text.call_args[0][0]

    cb = _make_callback("admin:user_uploads:10")
    gov_repo.list_user_uploaded_tracks.return_value = []
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo)
    assert "Uploaded Songs" in cb.message.edit_text.call_args[0][0]

    cb = _make_callback("admin:user_changes:10")
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo)
    assert "Chronological Activity" in cb.message.edit_text.call_args[0][0]

    cb = _make_callback("admin:promote:10:MODERATOR")
    gov_service.change_user_role.return_value = UserAccount(
        telegram_id=10, display_name="User 10", role=UserRole.MODERATOR
    )
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_service=gov_service, governance_repository=gov_repo
    )
    assert "Role updated to" in cb.answer.call_args[0][0]

    cb = _make_callback("admin:block:10")
    gov_service.set_user_blocked.return_value = UserAccount(
        telegram_id=10, display_name="User 10", role=UserRole.UPLOADER, is_blocked=True
    )
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_service=gov_service, governance_repository=gov_repo
    )
    assert "blocked" in cb.answer.call_args[0][0]

    cb = _make_callback("admin:resync:10")
    oidc_service = AsyncMock()
    outcome = MagicMock()
    outcome.new_role = UserRole.UPLOADER
    outcome.user = UserAccount(
        telegram_id=10, display_name="User 10", role=UserRole.UPLOADER, authentik_sub="sub_123"
    )
    oidc_service.refresh_user_from_idp.return_value = outcome
    gov_repo.get_user.return_value = outcome.user
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_repository=gov_repo, oidc_service=oidc_service
    )
    assert "OIDC synchronized" in cb.answer.call_args[0][0]

    cb = _make_callback("admin:sync_all")
    oidc_service.sync_all_linked_users.return_value = [outcome]
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_repository=gov_repo, oidc_service=oidc_service
    )
    assert "Synchronized" in cb.answer.call_args[0][0]

    cb = _make_callback("admin:concurrency")
    queue_worker.concurrency_limit = 2
    await handle_admin_dashboard_callback(
        cb, current_user=admin, governance_repository=gov_repo, queue_worker=queue_worker
    )
    queue_worker.set_concurrency_limit.assert_awaited()

    cb = _make_callback("admin:audit")
    gov_repo.list_audit_logs.return_value = []
    await handle_admin_dashboard_callback(cb, current_user=admin, governance_repository=gov_repo)
    assert "Audit Logs" in cb.message.edit_text.call_args[0][0]


@pytest.mark.anyio
async def test_handle_wipe_command_and_queue_controls(tmp_path: Path):
    msg = _make_message("/wipe", user_id=1)
    admin = UserAccount(telegram_id=1, display_name="Admin", role=UserRole.ADMIN)
    owner = UserAccount(telegram_id=1, display_name="Owner", role=UserRole.OWNER)
    gov_service = AsyncMock()

    await handle_wipe_command(msg, current_user=admin)
    assert "Owner Privileges Required" in msg.answer.call_args[0][0]

    msg.answer.reset_mock()
    await handle_wipe_command(msg, current_user=owner, governance_service=None)
    assert "Governance Unavailable" in msg.answer.call_args[0][0]

    msg.answer.reset_mock()
    await handle_wipe_command(msg, current_user=owner, governance_service=gov_service)
    assert "Destructive Action Warning" in msg.answer.call_args[0][0]

    msg.text = "/wipe CONFIRM"
    msg.answer.reset_mock()
    gov_service.wipe_library_catalog.return_value = 10
    await handle_wipe_command(msg, current_user=owner, governance_service=gov_service)
    assert "System Catalog Wipe Executed" in msg.answer.call_args[0][0]

    cb = _make_callback("queue:mine", user_id=1)
    gov_repo = AsyncMock()
    gov_repo.list_active_jobs.return_value = []
    gov_repo.list_queued_jobs.return_value = []
    await handle_queue_view_callback(cb, current_user=admin, governance_repository=gov_repo)
    assert cb.message.edit_text.await_count >= 1

    cb = _make_callback("queue:cancel:j1", user_id=1)
    queue_worker = AsyncMock()
    cancelled_job = IngestionJob(job_id="j1", source_path=tmp_path / "t.mp3", user_id=1)
    queue_worker.cancel_job.return_value = cancelled_job
    await handle_queue_cancel_callback(cb, current_user=admin, queue_worker=queue_worker)
    assert "Job Cancelled" in cb.message.edit_text.call_args[0][0]

    cb = _make_callback("queue:pause:j1", user_id=1)
    queue_worker.pause_job.return_value = cancelled_job
    await handle_queue_job_control_callback(cb, current_user=admin, queue_worker=queue_worker)
    assert "paused" in cb.answer.call_args[0][0]

    cb = _make_callback("queue:unlock:j1", user_id=1)
    queue_worker.unlock_job.return_value = cancelled_job
    queue_worker.get_queue_position.return_value = 1
    await handle_queue_job_control_callback(cb, current_user=admin, queue_worker=queue_worker)
    assert "unlocked" in cb.answer.call_args[0][0]

    # queue:view:j1
    cb = _make_callback("queue:view:j1", user_id=1)
    pipeline = AsyncMock()
    pipeline.get_job.return_value = cancelled_job
    gov_repo.get_user.return_value = admin
    await handle_queue_job_control_callback(
        cb, current_user=admin, queue_worker=queue_worker, pipeline=pipeline, governance_repository=gov_repo
    )
    assert cb.message.edit_text.await_count >= 1

    # queue:get_file:j1
    audio_file = tmp_path / "t.mp3"
    audio_file.write_bytes(b"audio content")
    cancelled_job.source_path = audio_file
    cb = _make_callback("queue:get_file:j1", user_id=1)
    await handle_queue_job_control_callback(
        cb, current_user=admin, queue_worker=queue_worker, pipeline=pipeline
    )
    assert "Sent queued file" in cb.answer.call_args[0][0]

    # queue:review:j1
    cb = _make_callback("queue:review:j1", user_id=1)
    state = _make_context(cb.bot, 1, 1)
    queue_worker.review_job_manually.return_value = IngestionResult(
        job=cancelled_job, requires_user_confirmation=True
    )
    await handle_queue_job_control_callback(
        cb, current_user=admin, queue_worker=queue_worker, pipeline=pipeline, state=state
    )
    assert cb.message.edit_text.await_count >= 1

    # queue controls error and permissions branches
    guest = UserAccount(telegram_id=99, display_name="Guest", role=UserRole.GUEST)
    other_job = IngestionJob(job_id="j2", source_path=tmp_path / "other.mp3", user_id=555)

    cb_err = _make_callback("queue:pause:j2", user_id=99)
    queue_worker.pause_job.side_effect = PermissionDeniedError("Denied")
    await handle_queue_job_control_callback(cb_err, current_user=guest, queue_worker=queue_worker)
    assert "permission to pause" in cb_err.answer.call_args[0][0]

    cb_err = _make_callback("queue:unlock:j2", user_id=99)
    queue_worker.unlock_job.side_effect = KeyError("Not found")
    await handle_queue_job_control_callback(cb_err, current_user=guest, queue_worker=queue_worker)
    assert "Job not found" in cb_err.answer.call_args[0][0]

    cb_err = _make_callback("queue:cancel:j2", user_id=99)
    queue_worker.cancel_job.side_effect = PermissionDeniedError("Denied")
    await handle_queue_cancel_callback(cb_err, current_user=guest, queue_worker=queue_worker)
    assert "permission to cancel" in cb_err.answer.call_args[0][0]

    cb_err = _make_callback("queue:get_file:j2", user_id=99)
    pipeline.get_job.return_value = other_job
    await handle_queue_job_control_callback(cb_err, current_user=guest, pipeline=pipeline)
    assert "download another user's" in cb_err.answer.call_args[0][0]
    assert cb.message.edit_text.await_count >= 1
