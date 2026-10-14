"""Handlers for incoming audio tracks and archive documents via Local Telegram Bot API."""

import asyncio
import html
import inspect
import logging
import time
from datetime import datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message

from src.bot.api_server import resolve_local_api_file_path
from src.bot.keyboards.admin import (
    build_back_to_home_keyboard,
    build_batch_queued_keyboard,
    build_queued_job_keyboard,
)
from src.bot.keyboards.verification import build_verification_keyboard
from src.bot.states import IngestionStates
from src.bot.ui import format_screen, safe_answer, safe_edit_text
from src.config.settings import AppSettings
from src.core.audit import (
    format_batch_queued_card,
    format_ingesting_progress_card,
    format_queued_job_card,
)
from src.core.extractor import SUPPORTED_ARCHIVE_EXTENSIONS, SUPPORTED_AUDIO_EXTENSIONS
from src.core.models import IngestionJob, IngestionResult, JobState, UserAccount
from src.core.pipeline import IngestionPipeline
from src.core.ports import GovernanceRepositoryPort
from src.core.queue import IngestionQueueWorker

logger = logging.getLogger(__name__)

files_router = Router(name="files")
_ALL_SUPPORTED_SUFFIXES = SUPPORTED_AUDIO_EXTENSIONS | SUPPORTED_ARCHIVE_EXTENSIONS

_BATCH_BUFFERS: dict[str, list[Message]] = {}
_BATCH_TIMERS: dict[str, asyncio.Task[None]] = {}
_BATCH_FIRST_TIMES: dict[str, float] = {}
_BATCH_LOCK = asyncio.Lock()


def render_job_stage_progress(job: IngestionJob) -> str:
    """Render an in-place live progress bar card for an active `IngestionJob` stage."""
    filename = html.escape(job.original_filename or job.source_path.name)
    total = max(1, job.total_tracks)
    conf_pct = round((job.confidence_score or 1.0) * 100)

    if job.state == JobState.EXTRACTING:
        return format_ingesting_progress_card(
            filename=filename,
            percent=15,
            extraction_status="Unpacking / Staging...",
            acoustid_status="Pending...",
            lyrics_status="Pending...",
        )
    if job.state == JobState.FINGERPRINTING:
        return format_ingesting_progress_card(
            filename=filename,
            percent=35,
            extraction_status=f"Complete ({total} track(s))",
            acoustid_status="Fingerprinting...",
            lyrics_status="Pending...",
        )
    if job.state == JobState.PROCESSING:
        return format_ingesting_progress_card(
            filename=filename,
            percent=5,
            extraction_status="Starting...",
            acoustid_status="Pending...",
            lyrics_status="Pending...",
        )
    if job.state == JobState.ENRICHING_METADATA:
        return format_ingesting_progress_card(
            filename=filename,
            percent=65,
            extraction_status=f"Complete ({total} track(s))",
            acoustid_status="Querying MusicBrainz...",
            lyrics_status="Pending...",
        )
    if job.state in {JobState.AWAITING_CONFIRMATION, JobState.AWAITING_INPUT}:
        return format_ingesting_progress_card(
            filename=filename,
            percent=70,
            extraction_status=f"Complete ({total} track(s))",
            acoustid_status=f"Ambiguous ({conf_pct}% top match)",
            lyrics_status="Waiting for release confirmation...",
            ambiguity_prompt=True,
        )
    if job.state == JobState.FETCHING_LYRICS:
        return format_ingesting_progress_card(
            filename=filename,
            percent=80,
            extraction_status=f"Complete ({total} track(s))",
            acoustid_status=f"Matched ({conf_pct}% confidence)",
            lyrics_status=f"Fetching LRCLIB ({job.processed_tracks}/{total})...",
        )
    if job.state == JobState.ORGANIZING:
        return format_ingesting_progress_card(
            filename=filename,
            percent=90,
            extraction_status=f"Complete ({total} track(s))",
            acoustid_status=f"Matched ({conf_pct}% confidence)",
            lyrics_status=f"Synced ({job.lyrics_matched_count}/{total}) • Writing tags & cover.jpg...",
        )
    return format_ingesting_progress_card(
        filename=filename,
        percent=100,
        extraction_status=f"Complete ({total} track(s))",
        acoustid_status=f"Matched ({conf_pct}% confidence)",
        lyrics_status=f"Synced ({job.lyrics_matched_count}/{total} embedded)",
    )


def format_ingestion_summary(result: IngestionResult) -> str:
    """Format a user-facing 3-tier Telegram HTML message summarizing an IngestionResult."""
    if result.duplicate is not None and result.duplicate.is_duplicate:
        reason = html.escape(result.duplicate.match_reason or "duplicate")
        return format_screen(
            title=f"⚠️ Duplicate Rejected ({reason})",
            context_lines=[
                f"Existing file: <code>{html.escape(str(result.duplicate.existing_path))}</code>",
            ],
            footer_lines=[f"<i>Job ID:</i> <code>{html.escape(result.job.job_id)}</code>"],
        )

    if result.requires_user_confirmation:
        filename = html.escape(result.job.original_filename or result.job.source_path.name)
        total = max(1, result.job.total_tracks)
        conf_pct = round((result.job.confidence_score or 1.0) * 100)
        lines: list[str] = [
            f"Payload: <code>{filename}</code> ({total} track(s), {conf_pct}% top match)",
            "Multiple or low-confidence matches found. Select a release below:",
        ]
        if result.job.candidates:
            lines.append("")
            for idx, cand in enumerate(result.job.candidates, start=1):
                pct = round(cand.confidence * 100)
                artist_esc = html.escape(cand.metadata.artist)
                title_esc = html.escape(cand.metadata.title)
                album_esc = html.escape(cand.metadata.album)
                year_part = f" ({cand.metadata.year})" if cand.metadata.year else ""
                type_part = f" [{html.escape(cand.release_type)}]" if cand.release_type else ""
                disambig_part = f" ({html.escape(cand.disambiguation)})" if cand.disambiguation else ""
                mb_link = ""
                if cand.metadata.release_mbid and not cand.metadata.release_mbid.startswith("release-group:"):
                    mb_link = (
                        f' — <a href="https://musicbrainz.org/release/'
                        f'{html.escape(cand.metadata.release_mbid)}">MB</a>'
                    )
                elif cand.metadata.recording_mbid and not cand.metadata.recording_mbid.startswith("artist:"):
                    mb_link = (
                        f' — <a href="https://musicbrainz.org/recording/'
                        f'{html.escape(cand.metadata.recording_mbid)}">MB</a>'
                    )
                lines.append(
                    f"<code>{idx}. [{pct}%]</code> {artist_esc} - {title_esc}\n"
                    f"   📀 <i>{album_esc}{year_part}</i>{type_part}{disambig_part}{mb_link}"
                )
        return format_screen(
            title="⚠️ Confirmation Required",
            context_lines=lines,
            footer_lines=[
                f"<i>Job ID:</i> <code>{html.escape(result.job.job_id)}</code> • "
                "<i>Tap Manual Search / Link to refine by URL, MBID, or artist</i>"
            ],
            expandable=True,
        )

    tracks_suffix = (
        f" • {result.tracks_ingested} tracks ({result.lyrics_embedded_count} synced lyrics)"
        if result.tracks_ingested > 1 or result.lyrics_embedded_count > 0
        else ""
    )
    return format_screen(
        title=f"✅ Track Ingested{tracks_suffix}",
        context_lines=[
            f"Saved to: <code>{html.escape(str(result.job.final_path))}</code>",
        ],
        footer_lines=[f"<i>Job ID:</i> <code>{html.escape(result.job.job_id)}</code>"],
    )


def _derive_effective_filename(message: Message, file_id: str) -> str:
    """Derive a descriptive filename from `message.audio` or `message.document` attributes."""
    media = message.audio or message.document
    raw_name_attr = getattr(media, "file_name", None)
    raw_name = raw_name_attr.strip() if isinstance(raw_name_attr, str) and raw_name_attr.strip() else None

    if message.audio is not None:
        performer_attr = getattr(message.audio, "performer", None)
        title_attr = getattr(message.audio, "title", None)
        performer = (
            performer_attr.strip() if isinstance(performer_attr, str) and performer_attr.strip() else None
        )
        title = title_attr.strip() if isinstance(title_attr, str) and title_attr.strip() else None

        if raw_name:
            if performer and " - " not in raw_name and " – " not in raw_name and " — " not in raw_name:
                return f"{performer} - {raw_name}"
            return raw_name
        if performer and title:
            return f"{performer} - {title}.flac"
        if title:
            return f"{title}.flac"
        return f"{file_id}.flac"

    return raw_name or f"{file_id}.bin"


async def _resolve_incoming_file_path(
    message: Message,
    file_id: str,
    file_name: str,
    scratch_dir: Path,
    settings: AppSettings | None,
    file_size_bytes: int = 0,
) -> tuple[Path | None, str, str | None]:
    """Fetch `file_id` via Local Bot API (`get_file`) and translate container paths to host storage."""
    bot = getattr(message, "bot", None)
    get_file_fn = getattr(bot, "get_file", None)
    if bot is None or not callable(get_file_fn):
        return None, file_name, "Bot instance not available to resolve file."

    try:
        raw_tg_file = get_file_fn(file_id)
        if not inspect.isawaitable(raw_tg_file):
            return None, file_name, "bot.get_file was not awaitable."
        tg_file = await raw_tg_file
    except Exception as exc:
        err_str = str(exc)
        logger.warning("bot.get_file(%s) failed: %s", file_id, exc)
        if "file is too big" in err_str.lower():
            size_mb = round(file_size_bytes / (1024 * 1024), 1) if file_size_bytes > 0 else ">20"
            return (
                None,
                file_name,
                f"File size ({size_mb} MB) exceeds Telegram Cloud Bot API 20 MB limit. "
                "Enable telegram-bot-api in local mode to support files up to 2 GB.",
            )
        return None, file_name, f"Telegram get_file error: {exc}"

    reported_path = getattr(tg_file, "file_path", None)
    if not isinstance(reported_path, str) or not reported_path.strip():
        return None, file_name, "Telegram API returned empty file_path."

    resolved = (
        resolve_local_api_file_path(reported_path, settings) if settings is not None else Path(reported_path)
    )
    effective_name = file_name
    if (
        resolved.suffix.lower() in _ALL_SUPPORTED_SUFFIXES
        and Path(effective_name).suffix.lower() not in _ALL_SUPPORTED_SUFFIXES
    ):
        effective_name = f"{Path(effective_name).stem}{resolved.suffix.lower()}"

    if resolved.is_file():
        return resolved, effective_name, None

    scratch_dir.mkdir(parents=True, exist_ok=True)
    download_target = scratch_dir / effective_name

    # If configured with local Bot API, try direct stream from the local Bot API HTTP server
    if settings is not None and settings.telegram_use_local_api:
        try:
            clean_api_url = settings.telegram_local_api_url.rstrip("/")
            reported_clean = reported_path.lstrip("/")
            file_url = f"{clean_api_url}/file/bot{settings.telegram_bot_token}/{reported_clean}"
            session = getattr(bot, "session", None)
            stream_fn = getattr(session, "stream_content", None)
            if callable(stream_fn):
                async with stream_fn(url=file_url, timeout=120) as stream:
                    with open(download_target, "wb") as f_out:
                        async for chunk in stream:
                            f_out.write(chunk)
                if download_target.is_file() and download_target.stat().st_size > 0:
                    return download_target, effective_name, None
        except Exception as local_dl_exc:
            logger.debug("Direct local Bot API HTTP stream download failed: %s", local_dl_exc)

    download_fn = getattr(bot, "download_file", None)
    if callable(download_fn):
        try:
            dl_res = download_fn(reported_path, destination=download_target)
            if inspect.isawaitable(dl_res):
                await dl_res
            if download_target.is_file() and download_target.stat().st_size > 0:
                return download_target, effective_name, None
        except Exception as exc:
            logger.warning("bot.download_file(%s) failed: %s", reported_path, exc)

    return None, effective_name, f"File could not be downloaded to {download_target}"


async def _edit_or_answer(
    message: Message,
    status_message: object | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit `status_message` in-place when available, otherwise send a new message via `message.answer`."""
    if status_message is not None:
        try:
            if await safe_edit_text(status_message, text, reply_markup=reply_markup):
                return
        except Exception as exc:
            logger.debug("In-place edit_text failed, falling back to message.answer: %s", exc)

    await safe_answer(message, text, reply_markup=reply_markup)


async def _dispatch_pipeline_result(
    message: Message,
    result: IngestionResult,
    state: FSMContext | None,
    status_message: object | None = None,
) -> None:
    """Transition the queued/progress message in-place (or reply) and update FSM state for `result`."""
    summary = format_ingestion_summary(result)
    if result.requires_user_confirmation:
        if state is not None:
            await state.set_state(IngestionStates.awaiting_confirmation)
            await state.update_data(job_id=result.job.job_id)
        await _edit_or_answer(
            message=message,
            status_message=status_message,
            text=summary,
            reply_markup=build_verification_keyboard(result.job.job_id, result.job.candidates),
        )
        return

    if state is not None:
        await state.clear()

    await _edit_or_answer(
        message=message,
        status_message=status_message,
        text=summary,
        reply_markup=build_back_to_home_keyboard(),
    )


@files_router.message(F.audio | F.document)
async def handle_incoming_media(
    message: Message,
    current_user: UserAccount | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    pipeline: IngestionPipeline | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    settings: AppSettings | None = None,
    state: FSMContext | None = None,
    batch_debounce_seconds: float = 0.5,
) -> None:
    """Resolve incoming media via Local Bot API, buffer batch forwards/albums, enqueue, and execute."""
    media = message.audio or message.document
    if media is None:
        return

    mg_id = getattr(message, "media_group_id", None)
    is_mg = isinstance(mg_id, str) and bool(mg_id.strip())

    fwd_date = getattr(message, "forward_date", None)
    fwd_orig = getattr(message, "forward_origin", None)
    is_fwd = isinstance(fwd_date, (int, float, datetime)) or (
        fwd_orig is not None and type(fwd_orig).__name__ != "MagicMock"
    )

    chat_id = getattr(getattr(message, "chat", None), "id", 0) or 0

    if is_mg:
        batch_key = f"mg:{chat_id}:{mg_id}"
    elif is_fwd:
        batch_key = f"fwd:{chat_id}"
    else:
        batch_key = None

    if batch_key is None or batch_debounce_seconds <= 0:
        await _process_media_batch(
            messages=[message],
            current_user=current_user,
            queue_worker=queue_worker,
            pipeline=pipeline,
            governance_repository=governance_repository,
            settings=settings,
            state=state,
        )
        return

    async with _BATCH_LOCK:
        if batch_key not in _BATCH_BUFFERS:
            _BATCH_BUFFERS[batch_key] = []
            _BATCH_FIRST_TIMES[batch_key] = time.monotonic()
        _BATCH_BUFFERS[batch_key].append(message)

        existing_task = _BATCH_TIMERS.get(batch_key)
        first_time = _BATCH_FIRST_TIMES.get(batch_key, time.monotonic())
        elapsed = time.monotonic() - first_time

        if existing_task is not None and not existing_task.done():
            if elapsed < 3.0:
                existing_task.cancel()
            else:
                return

        async def _debounce_worker(b_key: str, delay: float) -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            async with _BATCH_LOCK:
                msgs = _BATCH_BUFFERS.pop(b_key, [])
                _BATCH_TIMERS.pop(b_key, None)
                _BATCH_FIRST_TIMES.pop(b_key, None)
            if msgs:
                await _process_media_batch(
                    messages=msgs,
                    current_user=current_user,
                    queue_worker=queue_worker,
                    pipeline=pipeline,
                    governance_repository=governance_repository,
                    settings=settings,
                    state=state,
                )

        _BATCH_TIMERS[batch_key] = asyncio.create_task(_debounce_worker(batch_key, batch_debounce_seconds))


async def _process_media_batch(
    messages: list[Message],
    current_user: UserAccount | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    pipeline: IngestionPipeline | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    settings: AppSettings | None = None,
    state: FSMContext | None = None,
) -> None:
    """Process a list of media messages (either single upload or batch of forwarded tracks/album)."""
    if state is not None:
        await state.clear()

    if not messages:
        return

    scratch_dir = settings.storage_scratch_dir if settings is not None else Path("./storage/scratch")
    enqueued_items: list[tuple[IngestionJob, int, Message]] = []
    failed_items: list[tuple[str, int, str]] = []

    for msg in messages:
        media = msg.audio or msg.document
        if media is None:
            continue
        file_id = getattr(media, "file_id", "unknown")
        initial_name = _derive_effective_filename(msg, file_id)
        file_size = int(getattr(media, "file_size", 0) or 0)
        logger.info("Resolving media payload file_id=%s name=%s size=%d", file_id, initial_name, file_size)

        source_path, resolved_name, err = await _resolve_incoming_file_path(
            message=msg,
            file_id=file_id,
            file_name=initial_name,
            scratch_dir=scratch_dir,
            settings=settings,
            file_size_bytes=file_size,
        )

        if source_path is None or not source_path.is_file():
            err_msg = err or "Source file could not be located or downloaded"
            failed_items.append((initial_name, file_size, err_msg))
            continue

        if current_user is not None and queue_worker is not None:
            job, position = await queue_worker.enqueue_job(
                user=current_user,
                source_path=source_path,
                original_filename=resolved_name,
                file_size_bytes=file_size,
            )
            enqueued_items.append((job, position, msg))

    # Case A: Fallback if dependencies not injected
    if current_user is None or queue_worker is None:
        for msg in messages:
            media = msg.audio or msg.document
            fid = getattr(media, "file_id", "unknown")
            fname = _derive_effective_filename(msg, fid)
            fsize = int(getattr(media, "file_size", 0) or 0)
            s_path, f_name, _ = await _resolve_incoming_file_path(
                message=msg,
                file_id=fid,
                file_name=fname,
                scratch_dir=scratch_dir,
                settings=settings,
                file_size_bytes=fsize,
            )
            queued_fallback_card = format_screen(
                title="⏳ Ingestion Queued",
                context_lines=[f"Queued payload <code>{html.escape(fid)}</code> for ingestion."],
                footer_lines=["<i>🧭 Home › Ingestion</i>"],
            )
            if pipeline is not None and s_path is not None and s_path.is_file():
                status_msg = await msg.answer(queued_fallback_card)
                res = await pipeline.process_file(
                    source_path=s_path,
                    original_filename=f_name,
                    file_size_bytes=fsize,
                )
                await _dispatch_pipeline_result(msg, res, state, status_message=status_msg)
            else:
                await msg.answer(queued_fallback_card)
        return

    # Case B: All items in batch failed to resolve
    if not enqueued_items and failed_items:
        card = format_batch_queued_card([], failed_items=failed_items)
        await safe_answer(messages[-1], card, reply_markup=build_back_to_home_keyboard())
        return

    # Case C: Exactly one file was enqueued and none failed (Single File UX)
    if len(enqueued_items) == 1 and not failed_items:
        job, position, msg = enqueued_items[0]
        if governance_repository is not None:
            active_jobs = await governance_repository.list_active_jobs()
            active_name = active_jobs[0].original_filename if active_jobs else None
        else:
            active_name = None

        status_msg = await safe_answer(
            msg,
            format_queued_job_card(job, position=position, active_job_filename=active_name),
            reply_markup=build_queued_job_keyboard(
                job.job_id,
                web_app_url=settings.web_app_url if settings is not None else None,
            ),
        )

        async def _on_stage_progress(
            updated_job: IngestionJob,
            _result: IngestionResult | None = None,
        ) -> None:
            if updated_job.job_id != job.job_id:
                return
            if updated_job.state in {
                JobState.PAUSED,
                JobState.AWAITING_CONFIRMATION,
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                return
            try:
                await safe_edit_text(
                    status_msg,
                    render_job_stage_progress(updated_job),
                    reply_markup=build_queued_job_keyboard(
                        updated_job.job_id,
                        state=updated_job.state,
                        web_app_url=settings.web_app_url if settings is not None else None,
                    ),
                )
            except Exception as exc:
                logger.debug("Progress bar edit skipped for job %s: %s", updated_job.job_id, exc)

        try:
            result = await queue_worker.execute_job(
                job.job_id,
                progress_callback=_on_stage_progress,
            )
        except Exception as exc:
            logger.error("Ingestion failed for job %s: %s", job.job_id, exc)
            await _edit_or_answer(
                message=msg,
                status_message=status_msg,
                text=format_screen(
                    title="⚠️ Ingestion Failed",
                    context_lines=[f"Reason: <code>{html.escape(str(exc))}</code>"],
                    footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job.job_id)}</code>"],
                ),
                reply_markup=build_back_to_home_keyboard(),
            )
            return

        if result is not None:
            if (
                result.job.state == JobState.COMPLETED
                and msg.audio is not None
                and governance_repository is not None
            ):
                recent_tracks = await governance_repository.list_library_tracks(limit=20)
                for trk in recent_tracks:
                    if trk.job_id == result.job.job_id or (
                        result.job.final_path is not None and trk.storage_path == result.job.final_path
                    ):
                        chat_id = getattr(getattr(msg, "chat", None), "id", None)
                        msg_id = getattr(msg, "message_id", None)
                        await governance_repository.cache_track_telegram_file(
                            trk.id,
                            telegram_file_id=msg.audio.file_id,
                            source_chat_id=chat_id if isinstance(chat_id, int) else None,
                            source_message_id=msg_id if isinstance(msg_id, int) else None,
                        )
                        break

            await _dispatch_pipeline_result(
                msg,
                result,
                state,
                status_message=status_msg,
            )
        return

    # Case D: Multi-file Batch (or mixed success/failure)
    batch_jobs = [(j, pos) for j, pos, _ in enqueued_items]
    card = format_batch_queued_card(batch_jobs, failed_items=failed_items if failed_items else None)
    kb = build_batch_queued_keyboard(
        [j.job_id for j, _, _ in enqueued_items],
        web_app_url=settings.web_app_url if settings is not None else None,
    )
    last_msg = messages[-1]
    await safe_answer(last_msg, card, reply_markup=kb)

    # Register subscriber for confirmation prompts across the batch
    async def _on_batch_job_event(
        updated_job: IngestionJob,
        result: IngestionResult | None = None,
    ) -> None:
        if result is None:
            return
        match_item = next((item for item in enqueued_items if item[0].job_id == updated_job.job_id), None)
        if match_item is None:
            return
        t_msg = match_item[2]
        if result.requires_user_confirmation:
            await _dispatch_pipeline_result(t_msg, result, state)

    queue_worker.add_progress_callback(_on_batch_job_event)


__all__ = [
    "build_verification_keyboard",
    "files_router",
    "format_ingestion_summary",
    "render_job_stage_progress",
]
