"""Callback query handlers for interactive metadata confirmation and cancellation."""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)

verification_router = Router(name="verification")


def parse_verification_callback(data: str) -> tuple[str, str, int | None]:
    """Parse callback strings of the form `verify:<action>:<job_id>[:<candidate_index>]`."""
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "verify":
        msg = f"Malformed verification callback data: {data}"
        raise ValueError(msg)

    action = parts[1]
    job_id = parts[2]
    if action == "select":
        if len(parts) != 4:
            msg = f"Missing candidate index in callback data: {data}"
            raise ValueError(msg)
        return action, job_id, int(parts[3])
    return action, job_id, None


@verification_router.callback_query(F.data.startswith("verify:"))
async def handle_verification_callback(callback: CallbackQuery) -> None:
    """Process user button selections for ambiguous metadata candidates."""
    if not callback.data:
        await callback.answer("Invalid selection.", show_alert=True)
        return

    action, job_id, candidate_index = parse_verification_callback(callback.data)
    logger.info(
        "Received verification callback: action=%s, job_id=%s, candidate_index=%s",
        action,
        job_id,
        candidate_index,
    )
    await callback.answer(f"Action '{action}' registered for job {job_id}.")
