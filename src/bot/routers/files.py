"""Handlers for incoming audio tracks and archive documents via Local Telegram Bot API."""

import logging

from aiogram import F, Router
from aiogram.types import Message

from src.bot.keyboards.verification import build_verification_keyboard
from src.core.models import IngestionResult

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
async def handle_incoming_media(message: Message) -> None:
    """Acknowledge incoming audio/archive payloads before delegating to the core pipeline."""
    file_id = (
        message.audio.file_id
        if message.audio is not None
        else (message.document.file_id if message.document is not None else "unknown")
    )
    logger.info("Received media payload file_id=%s", file_id)
    await message.answer(f"Queued payload <code>{file_id}</code> for ingestion.")


__all__ = ["build_verification_keyboard", "files_router", "format_ingestion_summary"]
