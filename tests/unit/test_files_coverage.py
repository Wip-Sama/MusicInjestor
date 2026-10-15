"""Unit tests covering files_router helpers, formatting, and ingestion edge cases."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Audio, Document, Message, User

from src.bot.routers.files import (
    _derive_effective_filename,
    _edit_or_answer,
    _resolve_incoming_file_path,
    format_ingestion_summary,
    handle_incoming_media,
    render_job_stage_progress,
)
from src.core.models import (
    DuplicateCheckResult,
    IngestionJob,
    IngestionResult,
    JobState,
    LibraryTrackRecord,
    MetadataCandidate,
    TrackMetadata,
    UserAccount,
    UserRole,
)


def _make_msg(audio: Audio | None = None, doc: Document | None = None, user_id: int = 100) -> Message:
    msg = MagicMock(spec=Message)
    msg.audio = audio
    msg.document = doc
    msg.from_user = User(id=user_id, is_bot=False, first_name="User")
    msg.chat = MagicMock()
    msg.chat.id = user_id
    msg.message_id = 101
    msg.answer = AsyncMock(return_value=msg)
    msg.edit_text = AsyncMock()
    return msg


def test_render_job_stage_progress_states(tmp_path: Path) -> None:
    """Verify render_job_stage_progress across all JobState variants."""
    base_job = IngestionJob(
        job_id="j1",
        source_path=tmp_path / "test.mp3",
        original_filename="test.mp3",
        confidence_score=0.95,
        total_tracks=1,
    )

    # 1. PROCESSING
    base_job.state = JobState.PROCESSING
    assert "Starting" in render_job_stage_progress(base_job)

    # 2. ENRICHING_METADATA
    base_job.state = JobState.ENRICHING_METADATA
    assert "Querying MusicBrainz" in render_job_stage_progress(base_job)

    # 3. AWAITING_CONFIRMATION
    base_job.state = JobState.AWAITING_CONFIRMATION
    assert "Waiting for release confirmation" in render_job_stage_progress(base_job)

    # 4. FETCHING_LYRICS
    base_job.state = JobState.FETCHING_LYRICS
    assert "Fetching LRCLIB" in render_job_stage_progress(base_job)

    # 5. ORGANIZING
    base_job.state = JobState.ORGANIZING
    assert "Writing tags" in render_job_stage_progress(base_job)

    # 6. Fallback (COMPLETED)
    base_job.state = JobState.COMPLETED
    assert "100%" in render_job_stage_progress(base_job)


def test_format_ingestion_summary_branches(tmp_path: Path) -> None:
    """Verify format_ingestion_summary for duplicate and confirmation cards."""
    job = IngestionJob(
        job_id="j_sum",
        source_path=tmp_path / "song.flac",
        original_filename="song.flac",
        confidence_score=0.85,
    )

    # 1. Duplicate
    dup_res = IngestionResult(
        job=job,
        duplicate=DuplicateCheckResult(
            is_duplicate=True,
            existing_path=tmp_path / "existing.flac",
            match_reason="fingerprint",
        ),
    )
    dup_card = format_ingestion_summary(dup_res)
    assert "Duplicate Rejected" in dup_card

    # 2. Confirmation required
    cand = MetadataCandidate(
        metadata=TrackMetadata(
            title="Candidate Song",
            artist="Candidate Artist",
            album="Candidate Album",
            year=2024,
        ),
        confidence=0.85,
    )
    job.candidates = [cand]
    conf_res = IngestionResult(
        job=job,
        requires_user_confirmation=True,
    )
    conf_card = format_ingestion_summary(conf_res)
    assert "Confirmation Required" in conf_card


def test_derive_effective_filename_audio_attributes() -> None:
    """Verify _derive_effective_filename extraction from audio performer and title tags."""
    # 1. Has performer and title
    aud1 = MagicMock(spec=Audio)
    aud1.file_name = None
    aud1.performer = "The Beatles"
    aud1.title = "Hey Jude"
    msg1 = _make_msg(audio=aud1)
    assert _derive_effective_filename(msg1, "fid1") == "The Beatles - Hey Jude.flac"

    # 2. Has title only
    aud2 = MagicMock(spec=Audio)
    aud2.file_name = None
    aud2.performer = None
    aud2.title = "Imagine"
    msg2 = _make_msg(audio=aud2)
    assert _derive_effective_filename(msg2, "fid2") == "Imagine.flac"

    # 3. Audio without tags
    aud3 = MagicMock(spec=Audio)
    aud3.file_name = None
    aud3.performer = None
    aud3.title = None
    msg3 = _make_msg(audio=aud3)
    assert _derive_effective_filename(msg3, "fid3") == "fid3.flac"

    # 4. Document without file_name
    doc = MagicMock(spec=Document)
    doc.file_name = None
    msg4 = _make_msg(doc=doc)
    assert _derive_effective_filename(msg4, "doc1") == "doc1.bin"


@pytest.mark.anyio
async def test_resolve_incoming_file_path_edge_cases(tmp_path: Path) -> None:
    """Verify _resolve_incoming_file_path with big files, API errors, and stream downloads."""
    msg = _make_msg()
    scratch_dir = tmp_path / "scratch"

    # 1. Bot is None
    msg.bot = None
    p1, name1, err1 = await _resolve_incoming_file_path(msg, "fid", "f.mp3", scratch_dir, None)
    assert p1 is None
    assert "Bot instance not available" in (err1 or "")

    # 2. Telegram says file is too big
    bot = MagicMock()
    bot.get_file = AsyncMock(side_effect=RuntimeError("File is too big"))
    msg.bot = bot
    p2, name2, err2 = await _resolve_incoming_file_path(
        msg, "fid", "f.mp3", scratch_dir, None, file_size_bytes=50 * 1024 * 1024
    )
    assert p2 is None
    assert "exceeds Telegram Cloud Bot API 20 MB limit" in (err2 or "")

    # 3. Generic Telegram get_file error
    bot.get_file = AsyncMock(side_effect=RuntimeError("Network timeout"))
    p3, name3, err3 = await _resolve_incoming_file_path(msg, "fid", "f.mp3", scratch_dir, None)
    assert p3 is None
    assert "Network timeout" in (err3 or "")

    # 4. Empty file_path returned
    tg_file_empty = MagicMock()
    tg_file_empty.file_path = ""
    bot.get_file = AsyncMock(return_value=tg_file_empty)
    p4, name4, err4 = await _resolve_incoming_file_path(msg, "fid", "f.mp3", scratch_dir, None)
    assert p4 is None
    assert "empty file_path" in (err4 or "")


@pytest.mark.anyio
async def test_edit_or_answer_fallback() -> None:
    """Verify _edit_or_answer falls back to answer when edit fails."""
    msg = _make_msg()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock(side_effect=RuntimeError("Cannot edit"))
    await _edit_or_answer(msg, status_msg, "Updated text")
    assert msg.answer.called


@pytest.mark.anyio
async def test_handle_incoming_media_unsupported_and_failed(tmp_path: Path) -> None:
    """Verify handle_incoming_media handles files that fail to resolve."""
    doc_exe = MagicMock(spec=Document)
    doc_exe.file_id = "doc_exe"
    doc_exe.file_name = "setup.exe"
    doc_exe.file_size = 5000
    msg_exe = _make_msg(doc=doc_exe)

    user = UserAccount(telegram_id=100, display_name="User", role=UserRole.UPLOADER)
    worker = MagicMock()
    await handle_incoming_media(msg_exe, current_user=user, queue_worker=worker)
    assert "setup.exe" in msg_exe.answer.call_args[0][0]
    assert "Ingestion Queued" in msg_exe.answer.call_args[0][0]


@pytest.mark.anyio
async def test_handle_incoming_media_fallback_no_worker(tmp_path: Path) -> None:
    """Verify handle_incoming_media fallback path when queue_worker is None."""
    audio_file = tmp_path / "song.mp3"
    audio_file.write_text("dummy audio")

    aud = MagicMock(spec=Audio)
    aud.file_id = "aud_no_worker"
    aud.file_name = "song.mp3"
    aud.file_size = 1000

    msg = _make_msg(audio=aud)
    tg_file = MagicMock()
    tg_file.file_path = str(audio_file)
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=tg_file)
    msg.bot = bot

    pipeline = MagicMock()
    job = IngestionJob(
        job_id="j_fallback",
        source_path=audio_file,
        original_filename="song.mp3",
    )
    res = IngestionResult(job=job)
    pipeline.process_file = AsyncMock(return_value=res)

    await handle_incoming_media(
        msg,
        current_user=None,
        queue_worker=None,
        pipeline=pipeline,
    )
    assert msg.answer.called


@pytest.mark.anyio
async def test_handle_incoming_media_single_file_execution(tmp_path: Path) -> None:
    """Verify handle_incoming_media handles execution error and success with track caching."""
    audio_file = tmp_path / "song.mp3"
    audio_file.write_text("dummy audio")

    aud = MagicMock(spec=Audio)
    aud.file_id = "aud_err"
    aud.file_name = "song.mp3"
    aud.file_size = 1000

    msg = _make_msg(audio=aud)
    tg_file = MagicMock()
    tg_file.file_path = str(audio_file)
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=tg_file)
    msg.bot = bot

    user = UserAccount(telegram_id=100, display_name="User", role=UserRole.UPLOADER)
    worker = MagicMock()
    job = IngestionJob(
        job_id="j_err",
        source_path=audio_file,
        original_filename="song.mp3",
    )
    worker.enqueue_job = AsyncMock(return_value=(job, 1))
    worker.execute_job = AsyncMock(side_effect=RuntimeError("Transcoder crashed"))

    # Error path
    await handle_incoming_media(
        msg,
        current_user=user,
        queue_worker=worker,
    )
    assert msg.answer.called

    # Success path with telegram file id caching
    job_ok = IngestionJob(
        job_id="j_ok",
        source_path=audio_file,
        final_path=audio_file,
        original_filename="song.mp3",
        state=JobState.COMPLETED,
    )
    res_ok = IngestionResult(job=job_ok, tracks_ingested=1)
    worker.enqueue_job = AsyncMock(return_value=(job_ok, 1))
    worker.execute_job = AsyncMock(return_value=res_ok)

    gov_repo = MagicMock()
    track_rec = LibraryTrackRecord(
        id="trk_ok",
        job_id="j_ok",
        uploaded_by=100,
        storage_path=audio_file,
        fingerprint="fp",
        duration_seconds=120.0,
        title="Title",
        artist="Artist",
        album="Album",
    )
    gov_repo.list_library_tracks = AsyncMock(return_value=[track_rec])
    gov_repo.list_active_jobs = AsyncMock(return_value=[job_ok])
    gov_repo.cache_track_telegram_file = AsyncMock()

    await handle_incoming_media(
        msg,
        current_user=user,
        queue_worker=worker,
        governance_repository=gov_repo,
    )
    assert gov_repo.cache_track_telegram_file.called


@pytest.mark.anyio
async def test_handle_incoming_media_batch(tmp_path: Path) -> None:
    """Verify handle_incoming_media buffers and processes multi-item albums/batches."""
    file1 = tmp_path / "song1.mp3"
    file1.write_text("audio1")
    file2 = tmp_path / "song2.mp3"
    file2.write_text("audio2")

    aud1 = MagicMock(spec=Audio)
    aud1.file_id = "aud1"
    aud1.file_name = "song1.mp3"
    aud1.file_size = 1000

    aud2 = MagicMock(spec=Audio)
    aud2.file_id = "aud2"
    aud2.file_name = "song2.mp3"
    aud2.file_size = 2000

    msg1 = _make_msg(audio=aud1)
    msg1.media_group_id = "album_123"
    msg2 = _make_msg(audio=aud2)
    msg2.media_group_id = "album_123"

    tg1 = MagicMock()
    tg1.file_path = str(file1)
    tg2 = MagicMock()
    tg2.file_path = str(file2)

    bot = MagicMock()
    bot.get_file = AsyncMock(side_effect=[tg1, tg2])
    msg1.bot = bot
    msg2.bot = bot

    user = UserAccount(telegram_id=100, display_name="User", role=UserRole.UPLOADER)
    worker = MagicMock()
    job1 = IngestionJob(job_id="j1", source_path=file1, original_filename="song1.mp3")
    job2 = IngestionJob(job_id="j2", source_path=file2, original_filename="song2.mp3")
    worker.enqueue_job = AsyncMock(side_effect=[(job1, 1), (job2, 2)])
    worker.add_progress_callback = MagicMock()

    await handle_incoming_media(msg1, current_user=user, queue_worker=worker, batch_debounce_seconds=0)
    assert msg1.answer.called
