"""Handlers for incoming audio tracks and archive documents via Local Telegram Bot API."""

import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.types import Message

from src.bot.keyboards.admin import build_queued_job_keyboard
from src.bot.keyboards.verification import build_verification_keyboard
from src.config.settings import AppSettings
from src.core.audit import format_queued_job_card
from src.core.models import IngestionResult, UserAccount
from src.core.ports import GovernanceRepositoryPort
from src.core.queue import IngestionQueueWorker

logger = logging.getLogger(__name__)

files_router = Router(name="files")


def format_ingestion_summary(result: IngestionResult) -> str:
    """Format a user-facing Telegram HTML message summarizing an IngestionResult."""
    if result.duplicate is not None and result.duplicate.is_duplicate:
        return (
            f"<b>Duplicate Rejected</b> ({result.duplicate.match_reason})\n"
            f"Existing file: <code>{result.duplicate.existing_path}</code>"
        )

    if result.requires_user_confirmation:
        return (
            f"<b>Confirmation Required</b> (Job <code>{result.job.job_id}</code>)\n"
            "Multiple or low-confidence matches found. Select a release below:"
        )

    return (
        f"<b>Track Ingested</b> (Job <code>{result.job.job_id}</code>)\n"
        f"Saved to: <code>{result.job.final_path}</code>"
    )


@files_router.message(F.audio | F.document)
async def handle_incoming_media(
    message: Message,
    current_user: UserAccount | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Enqueue incoming audio/archive payloads into the persistent SQLite ingestion queue."""
    media = message.audio or message.document
    file_id = media.file_id if media is not None else "unknown"
    file_name = getattr(media, "file_name", None) or (
        f"{file_id}.flac" if message.audio is not None else f"{file_id}.bin"
    )
    file_size = int(getattr(media, "file_size", 0) or 0)
    logger.info("Received media payload file_id=%s name=%s size=%d", file_id, file_name, file_size)

    if current_user is not None and queue_worker is not None:
        scratch_dir = settings.storage_scratch_dir if settings is not None else Path("./storage/scratch")
        staged_path = scratch_dir / file_name
        job, position = await queue_worker.enqueue_job(
            user=current_user,
            source_path=staged_path,
            original_filename=file_name,
        )
        job.file_size_bytes = file_size
        if governance_repository is not None:
            active_jobs = await governance_repository.list_active_jobs()
            active_name = active_jobs[0].original_filename if active_jobs else None
        else:
            active_name = None

        await message.answer(
            format_queued_job_card(job, position=position, active_job_filename=active_name),
            reply_markup=build_queued_job_keyboard(job.job_id),
        )
        return

    await message.answer(f"Queued payload <code>{file_id}</code> for ingestion.")


__all__ = ["build_verification_keyboard", "files_router", "format_ingestion_summary"]
