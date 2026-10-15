"""Unit tests covering commands_router handlers, helpers, and edge cases."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, User

from src.bot.routers.commands import (
    _inspect_id_query,
    _parse_tag_correction_spec,
    _render_help_topic_card,
    _render_status_card,
    handle_cmd_app,
    handle_fixtags_command,
    handle_help,
    handle_help_topic_callback,
    handle_idle_id_lookup_text,
    handle_job_command,
    handle_menu_navigation_callback,
    handle_queue_command,
    handle_start,
)
from src.config.settings import AppSettings
from src.core.models import (
    IngestionJob,
    JobState,
    LibraryTrackRecord,
    TagCorrectionRequest,
    TagCorrectionStatus,
    UserAccount,
    UserRole,
)


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


def test_render_help_topic_card_topics() -> None:
    """Verify topic rendering for sso, status, admin, and fallback."""
    assert "Authentik SSO" in _render_help_topic_card("sso")
    assert "System &amp; Session Status" in _render_help_topic_card("status")
    assert "Admin Control Panel" in _render_help_topic_card("admin")
    assert "Music Ingestion Gateway Guide" in _render_help_topic_card("unknown_topic")


@pytest.mark.anyio
async def test_render_status_card_scenarios(tmp_path: Path) -> None:
    """Verify _render_status_card with None user, with organizer stats, and with active queue."""
    # None user
    card1 = await _render_status_card(None, None, None)
    assert "Online" in card1

    gov_repo = MagicMock()
    gov_repo.list_queued_jobs = AsyncMock(return_value=[])
    gov_repo.list_active_jobs = AsyncMock(return_value=[])

    user = UserAccount(telegram_id=1, display_name="Admin", role=UserRole.ADMIN)
    organizer = MagicMock()
    organizer.library_dir = tmp_path

    card2 = await _render_status_card(user, gov_repo, None, organizer=organizer)
    assert "System &amp; Session Status" in card2


def test_parse_fixtags_args() -> None:
    """Verify _parse_tag_correction_spec parses key-values and dash-separated artist/title."""
    # Key-values
    t, a, al, y, r = _parse_tag_correction_spec("t=Song | a=Artist | b=Album | y=2024 | r=Fix typo")
    assert t == "Song"
    assert a == "Artist"
    assert al == "Album"
    assert y == 2024
    assert r == "Fix typo"

    # Dash separated
    t2, a2, al2, y2, r2 = _parse_tag_correction_spec("Artist Name - Track Title")
    assert a2 == "Artist Name"
    assert t2 == "Track Title"

    # Empty value handling
    t3, a3, _, _, _ = _parse_tag_correction_spec("t= | Only Title")
    assert t3 == "Only Title"


@pytest.mark.anyio
async def test_handle_start_with_webapp() -> None:
    """Verify handle_start displays web app card when configured."""
    msg = _make_msg("/start")
    user = UserAccount(telegram_id=100, display_name="Uploader", role=UserRole.UPLOADER)
    settings = AppSettings(
        bot_token="fake:token",
        telegram_api_id=1,
        telegram_api_hash="0123456789abcdef",
        web_app_url="https://app.example.com",
    )
    await handle_start(msg, current_user=user, settings=settings)
    assert msg.answer.call_count == 2


@pytest.mark.anyio
async def test_handle_help_command_and_topic_callback() -> None:
    """Verify handle_help and handle_help_topic_callback."""
    msg = _make_msg("/help")
    await handle_help(msg)
    assert msg.answer.called

    cb = MagicMock(spec=CallbackQuery)
    cb.data = "help:status"
    cb.message = _make_msg()
    cb.answer = AsyncMock()
    await handle_help_topic_callback(cb)
    assert cb.message.edit_text.called or cb.message.answer.called


@pytest.mark.anyio
async def test_handle_menu_callback_actions() -> None:
    """Verify handle_menu_navigation_callback for help topic, sso, status, queue, and home."""
    cb = MagicMock(spec=CallbackQuery)
    cb.message = _make_msg()
    cb.answer = AsyncMock()

    user = UserAccount(telegram_id=100, display_name="Uploader", role=UserRole.UPLOADER)
    gov_repo = MagicMock()
    gov_repo.list_queued_jobs = AsyncMock(return_value=[])
    gov_repo.list_active_jobs = AsyncMock(return_value=[])

    # SSO
    cb.data = "menu:sso"
    await handle_menu_navigation_callback(cb, current_user=user, governance_repository=gov_repo)

    # Status
    cb.data = "menu:status"
    await handle_menu_navigation_callback(cb, current_user=user, governance_repository=gov_repo)

    # Queue
    cb.data = "menu:queue"
    await handle_menu_navigation_callback(cb, current_user=user, governance_repository=gov_repo)

    # Home
    cb.data = "menu:home"
    await handle_menu_navigation_callback(cb, current_user=user, governance_repository=gov_repo)

    # Guest user
    guest = UserAccount(telegram_id=5, display_name="Guest 5", role=UserRole.GUEST)
    cb.data = "menu:home"
    await handle_menu_navigation_callback(cb, current_user=guest)


@pytest.mark.anyio
async def test_handle_queue_command_permissions() -> None:
    """Verify handle_queue_command permissions and repository errors."""
    msg = _make_msg("/queue")

    # Guest user forbidden
    guest = UserAccount(telegram_id=10, display_name="Guest 10", role=UserRole.GUEST)
    await handle_queue_command(msg, current_user=guest)
    assert "Permission Required" in msg.answer.call_args[0][0]

    # No repository
    uploader = UserAccount(telegram_id=20, display_name="Uploader 20", role=UserRole.UPLOADER)
    await handle_queue_command(msg, current_user=uploader, governance_repository=None)
    assert "Queue Unavailable" in msg.answer.call_args[0][0]

    # Success
    gov_repo = MagicMock()
    gov_repo.list_queued_jobs = AsyncMock(return_value=[])
    gov_repo.list_active_jobs = AsyncMock(return_value=[])
    await handle_queue_command(msg, current_user=uploader, governance_repository=gov_repo)
    assert msg.answer.called


@pytest.mark.anyio
async def test_inspect_id_query_and_job_command(tmp_path: Path) -> None:
    """Verify _inspect_id_query and handle_job_command with jobs and tracks."""
    msg = _make_msg("/job")

    # 1. Empty query
    assert await _inspect_id_query(msg, "", None, None, None, None, None) is False

    # 2. Guest user calling /job
    guest = UserAccount(telegram_id=1, display_name="Guest 1", role=UserRole.GUEST)
    await handle_job_command(msg, current_user=guest)
    assert "Permission Required" in msg.answer.call_args[0][0]

    # 3. No args to /job
    uploader = UserAccount(telegram_id=100, display_name="Uploader 100", role=UserRole.UPLOADER)
    await handle_job_command(msg, current_user=uploader)
    assert "Inspect Job by ID" in msg.answer.call_args[0][0]

    # 4. Job belonging to another user, uploader forbidden
    job_other = IngestionJob(
        job_id="job123",
        source_path=tmp_path / "song.mp3",
        user_id=999,
        original_filename="song.mp3",
        file_size_bytes=1000,
        state=JobState.AWAITING_CONFIRMATION,
    )
    cat_repo = MagicMock()
    cat_repo.get_job = AsyncMock(return_value=job_other)
    msg_other = _make_msg("/job job123", user_id=100)
    await handle_job_command(
        msg_other,
        current_user=uploader,
        catalog_repository=cat_repo,
    )
    assert "Permission Denied" in msg_other.answer.call_args[0][0]

    # 5. Job belonging to current user
    job_own = IngestionJob(
        job_id="job_own",
        source_path=tmp_path / "song.mp3",
        user_id=100,
        original_filename="song.mp3",
        file_size_bytes=1000,
        state=JobState.QUEUED,
    )
    cat_repo.get_job = AsyncMock(return_value=job_own)
    gov_repo = MagicMock()
    gov_repo.get_user = AsyncMock(return_value=uploader)
    msg_own = _make_msg("/job job_own", user_id=100)
    await handle_job_command(
        msg_own,
        current_user=uploader,
        catalog_repository=cat_repo,
        governance_repository=gov_repo,
    )
    assert msg_own.answer.called

    # 6. Track lookup by ID
    cat_repo.get_job = AsyncMock(return_value=None)
    track_file = tmp_path / "song.mp3"
    track_file.write_text("audio")
    track = LibraryTrackRecord(
        id="trk-123456789012",
        job_id="job1",
        uploaded_by=100,
        storage_path=track_file,
        fingerprint="fp123",
        duration_seconds=180.0,
        title="Test Song",
        artist="Test Artist",
        album="Test Album",
        modified_by=100,
    )
    gov_repo.get_library_track = AsyncMock(return_value=track)
    msg_trk = _make_msg("/job trk-123456789012", user_id=100)
    await handle_job_command(
        msg_trk,
        current_user=uploader,
        catalog_repository=cat_repo,
        governance_repository=gov_repo,
    )
    assert msg_trk.answer.called

    # 7. Neither job nor track found
    gov_repo.get_library_track = AsyncMock(return_value=None)
    msg_none = _make_msg("/job missing_id", user_id=100)
    await handle_job_command(
        msg_none,
        current_user=uploader,
        catalog_repository=cat_repo,
        governance_repository=gov_repo,
    )
    assert "Job Not Found" in msg_none.answer.call_args[0][0]


@pytest.mark.anyio
async def test_handle_idle_id_lookup_text() -> None:
    """Verify handle_idle_id_lookup_text ignores short or spaced text and passes valid IDs."""
    guest = UserAccount(telegram_id=1, display_name="Guest 1", role=UserRole.GUEST)
    msg = _make_msg("abc")
    await handle_idle_id_lookup_text(msg, current_user=guest)
    assert not msg.answer.called

    uploader = UserAccount(telegram_id=100, display_name="Uploader 100", role=UserRole.UPLOADER)
    msg_short = _make_msg("ab", user_id=100)
    await handle_idle_id_lookup_text(msg_short, current_user=uploader)
    assert not msg_short.answer.called

    msg_spaced = _make_msg("hello world", user_id=100)
    await handle_idle_id_lookup_text(msg_spaced, current_user=uploader)
    assert not msg_spaced.answer.called


@pytest.mark.anyio
async def test_handle_fixtags_command_branches(tmp_path: Path) -> None:
    """Verify handle_fixtags_command validation, errors, and submission."""
    msg = _make_msg("/fixtags")

    # 1. Forbidden for Guest
    guest = UserAccount(telegram_id=1, display_name="Guest 1", role=UserRole.GUEST)
    await handle_fixtags_command(msg, current_user=guest)
    assert "Permission Required" in msg.answer.call_args[0][0]

    # 2. Missing service
    uploader = UserAccount(telegram_id=100, display_name="Uploader 100", role=UserRole.UPLOADER)
    await handle_fixtags_command(msg, current_user=uploader, governance_service=None)
    assert "Service Unavailable" in msg.answer.call_args[0][0]

    # 3. Too few arguments (shows help and recent tracks)
    gov_repo = MagicMock()
    track_file = tmp_path / "song.mp3"
    track_file.write_text("audio")
    sample_trk = LibraryTrackRecord(
        id="trk1",
        job_id="job1",
        uploaded_by=100,
        storage_path=track_file,
        fingerprint="fp123",
        duration_seconds=180.0,
        title="Title",
        artist="Artist",
        album="Album",
    )
    gov_repo.list_library_tracks = AsyncMock(return_value=[sample_trk])
    gov_svc = MagicMock()
    gov_svc.request_tag_correction = AsyncMock()
    await handle_fixtags_command(
        msg,
        current_user=uploader,
        governance_service=gov_svc,
        governance_repository=gov_repo,
    )
    assert "Post-Ingestion Tag Correction" in msg.answer.call_args[0][0]

    # 4. Missing tag fields (only reason given)
    msg_empty_fields = _make_msg("/fixtags trk1 reason=just_a_note")
    await handle_fixtags_command(
        msg_empty_fields,
        current_user=uploader,
        governance_service=gov_svc,
        governance_repository=gov_repo,
    )
    assert "Missing Tag Fields" in msg_empty_fields.answer.call_args[0][0]

    # 5. Service raises KeyError
    gov_svc.request_tag_correction = AsyncMock(side_effect=KeyError("Track not found"))
    msg_err = _make_msg("/fixtags trk1 title=New")
    await handle_fixtags_command(
        msg_err,
        current_user=uploader,
        governance_service=gov_svc,
        governance_repository=gov_repo,
    )
    assert "Tag Correction Rejected" in msg_err.answer.call_args[0][0]

    # 6. Successful submission
    req = TagCorrectionRequest(
        id=42,
        track_id="trk1",
        requested_by=100,
        proposed_title="New Title",
        proposed_artist="New Artist",
        proposed_album="New Album",
        proposed_year=2025,
        status=TagCorrectionStatus.PENDING,
    )
    gov_svc.request_tag_correction = AsyncMock(return_value=(req, sample_trk))
    msg_ok = _make_msg("/fixtags trk1 title=New Title | artist=New Artist | album=New Album | year=2025")
    await handle_fixtags_command(
        msg_ok,
        current_user=uploader,
        governance_service=gov_svc,
        governance_repository=gov_repo,
    )
    assert "Tag Correction Request #42 Submitted" in msg_ok.answer.call_args[0][0]


@pytest.mark.anyio
async def test_handle_cmd_app() -> None:
    """Verify handle_cmd_app with and without URL configured."""
    msg = _make_msg("/app")
    await handle_cmd_app(msg, settings=None)
    assert "Mini App URL is not configured" in msg.answer.call_args[0][0]

    settings = AppSettings(
        bot_token="fake:token",
        telegram_api_id=1,
        telegram_api_hash="0123456789abcdef",
        web_app_url="https://webapp.test",
    )
    msg_ok = _make_msg("/app")
    await handle_cmd_app(msg_ok, settings=settings)
    assert msg_ok.answer.called
