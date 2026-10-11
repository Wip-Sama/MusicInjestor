"""Handlers for incoming audio tracks and archive documents via Local Telegram Bot API."""

import html
import inspect
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from src.bot.api_server import resolve_local_api_file_path
from src.bot.keyboards.admin import build_queued_job_keyboard
from src.bot.keyboards.verification import build_verification_keyboard
from src.bot.states import IngestionStates
from src.config.settings import AppSettings
from src.core.audit import format_queued_job_card
from src.core.extractor import SUPPORTED_ARCHIVE_EXTENSIONS, SUPPORTED_AUDIO_EXTENSIONS
from src.core.models import IngestionResult, UserAccount
from src.core.pipeline import IngestionPipeline
from src.core.ports import GovernanceRepositoryPort
from src.core.queue import IngestionQueueWorker

logger = logging.getLogger(__name__)

files_router = Router(name="files")
_ALL_SUPPORTED_SUFFIXES = SUPPORTED_AUDIO_EXTENSIONS | SUPPORTED_ARCHIVE_EXTENSIONS


def format_ingestion_summary(result: IngestionResult) -> str:
    """Format a user-facing Telegram HTML message summarizing an IngestionResult."""
    if result.duplicate is not None and result.duplicate.is_duplicate:
        return (
            f"<b>Duplicate Rejected</b> ({result.duplicate.match_reason})\n"
            f"Existing file: <code>{html.escape(str(result.duplicate.existing_path))}</code>"
        )

    if result.requires_user_confirmation:
        lines = [
            f"<b>Confirmation Required</b> (Job <code>{result.job.job_id}</code>)",
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
                    f"<b>{idx}. [{pct}%]</b> {artist_esc} - {title_esc}\n"
                    f"   📀 <i>{album_esc}{year_part}</i>{type_part}{disambig_part}{mb_link}"
                )
            lines.append("")
            lines.append(
                "💡 <i>Tap <b>Manual Search / Link</b> to send a MusicBrainz URL/MBID, "
                "<code>Artist - Title | Album</code>, or just the voice actor/artist name.</i>"
            )
        return "\n".join(lines)

    return (
        f"<b>Track Ingested</b> (Job <code>{result.job.job_id}</code>)\n"
        f"Saved to: <code>{html.escape(str(result.job.final_path))}</code>"
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
) -> tuple[Path, str]:
    """Fetch `file_id` via Local Bot API (`get_file`) and translate container paths to host storage."""
    fallback_path = scratch_dir / file_name
    bot = getattr(message, "bot", None)
    get_file_fn = getattr(bot, "get_file", None)
    if bot is None or not inspect.iscoroutinefunction(get_file_fn):
        return fallback_path, file_name

    try:
        tg_file = await get_file_fn(file_id)
    except Exception as exc:
        logger.warning("bot.get_file(%s) failed: %s", file_id, exc)
        return fallback_path, file_name

    reported_path = getattr(tg_file, "file_path", None)
    if not isinstance(reported_path, str) or not reported_path.strip():
        return fallback_path, file_name

    resolved = (
        resolve_local_api_file_path(reported_path, settings) if settings is not None else Path(reported_path)
    )
    effective_name = file_name
    if (
        resolved.suffix.lower() in _ALL_SUPPORTED_SUFFIXES
        and Path(effective_name).suffix.lower() not in _ALL_SUPPORTED_SUFFIXES
    ):
        effective_name = f"{Path(effective_name).stem}{resolved.suffix.lower()}"

    if not resolved.is_file():
        download_fn = getattr(bot, "download_file", None)
        if inspect.iscoroutinefunction(download_fn):
            scratch_dir.mkdir(parents=True, exist_ok=True)
            download_target = scratch_dir / effective_name
            try:
                await download_fn(reported_path, destination=download_target)
                return download_target, effective_name
            except Exception as exc:
                logger.warning("bot.download_file(%s) failed: %s", reported_path, exc)

    return resolved, effective_name


async def _dispatch_pipeline_result(
    message: Message,
    result: IngestionResult,
    state: FSMContext | None,
) -> None:
    """Send the appropriate Telegram response and update FSM state for `result`."""
    summary = format_ingestion_summary(result)
    if result.requires_user_confirmation:
        if state is not None:
            await state.set_state(IngestionStates.awaiting_confirmation)
            await state.update_data(job_id=result.job.job_id)
        await message.answer(
            summary,
            reply_markup=build_verification_keyboard(result.job.job_id, result.job.candidates),
        )
        return

    await message.answer(summary)


@files_router.message(F.audio | F.document)
async def handle_incoming_media(
    message: Message,
    current_user: UserAccount | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    pipeline: IngestionPipeline | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    settings: AppSettings | None = None,
    state: FSMContext | None = None,
) -> None:
    """Resolve incoming audio/archive via Local Bot API, enqueue, and execute ingestion."""
    media = message.audio or message.document
    file_id = media.file_id if media is not None else "unknown"
    initial_name = _derive_effective_filename(message, file_id)
    file_size = int(getattr(media, "file_size", 0) or 0)
    logger.info("Received media payload file_id=%s name=%s size=%d", file_id, initial_name, file_size)

    scratch_dir = settings.storage_scratch_dir if settings is not None else Path("./storage/scratch")
    source_path, file_name = await _resolve_incoming_file_path(
        message=message,
        file_id=file_id,
        file_name=initial_name,
        scratch_dir=scratch_dir,
        settings=settings,
    )

    if current_user is not None and queue_worker is not None:
        job, position = await queue_worker.enqueue_job(
            user=current_user,
            source_path=source_path,
            original_filename=file_name,
            file_size_bytes=file_size,
        )
        if governance_repository is not None:
            active_jobs = await governance_repository.list_active_jobs()
            active_name = active_jobs[0].original_filename if active_jobs else None
        else:
            active_name = None

        await message.answer(
            format_queued_job_card(job, position=position, active_job_filename=active_name),
            reply_markup=build_queued_job_keyboard(job.job_id),
        )

        if source_path.is_file():
            try:
                result = await queue_worker.execute_job(job.job_id)
            except Exception as exc:
                logger.error("Ingestion failed for job %s: %s", job.job_id, exc)
                await message.answer(
                    f"⚠️ <b>Ingestion Failed</b> (Job <code>{job.job_id}</code>)\nReason: <code>{exc}</code>"
                )
                return

            if result is not None:
                await _dispatch_pipeline_result(message, result, state)
        return

    if pipeline is not None and source_path.is_file():
        await message.answer(f"Queued payload <code>{file_id}</code> for ingestion.")
        result = await pipeline.process_file(
            source_path=source_path,
            original_filename=file_name,
            file_size_bytes=file_size,
        )
        await _dispatch_pipeline_result(message, result, state)
        return

    await message.answer(f"Queued payload <code>{file_id}</code> for ingestion.")


__all__ = ["build_verification_keyboard", "files_router", "format_ingestion_summary"]
