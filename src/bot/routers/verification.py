"""Callback query handlers for interactive metadata confirmation and cancellation."""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.keyboards.admin import RESERVED_MENU_BUTTONS
from src.bot.keyboards.verification import build_verification_keyboard
from src.bot.routers.files import format_ingestion_summary
from src.bot.states import IngestionStates
from src.core.exceptions import PermissionDeniedError
from src.core.models import IngestionJob, Permission, UserAccount
from src.core.pipeline import IngestionPipeline
from src.core.queue import IngestionQueueWorker
from src.core.rbac import has_permission

logger = logging.getLogger(__name__)

verification_router = Router(name="verification")


def _can_resolve_job(user: UserAccount | None, job: IngestionJob | None) -> bool:
    """Return True if `user` is permitted to disambiguate `job` (own vs others' jobs)."""
    if user is None:
        return True
    if job is not None and job.user_id is not None and job.user_id != user.telegram_id:
        return has_permission(user, Permission.RESOLVE_OTHERS_JOBS)
    return has_permission(user, Permission.RESOLVE_OWN_DISAMBIGUATION)


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
async def handle_verification_callback(
    callback: CallbackQuery,
    state: FSMContext | None = None,
    current_user: UserAccount | None = None,
    pipeline: IngestionPipeline | None = None,
    queue_worker: IngestionQueueWorker | None = None,
) -> None:
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

    if pipeline is None:
        await callback.answer(f"Action '{action}' registered for job {job_id}.")
        return

    target_job = await pipeline.get_job(job_id)
    if not _can_resolve_job(current_user, target_job):
        await callback.answer(
            "🔒 You do not have permission to resolve another user's job.",
            show_alert=True,
        )
        return

    msg = callback.message if isinstance(callback.message, Message) else None

    if action == "select" and candidate_index is not None:
        try:
            result = await pipeline.confirm_candidate(
                job_id=job_id,
                candidate_index=candidate_index,
            )
        except (KeyError, IndexError) as exc:
            await callback.answer(f"Could not confirm release: {exc}", show_alert=True)
            return

        if queue_worker is not None:
            await queue_worker.record_confirmed_completion(result)
        if state is not None:
            await state.clear()

        summary = format_ingestion_summary(result)
        if msg is not None:
            await msg.edit_text(summary)
        await callback.answer("Release confirmed and ingested!")
        return

    if action == "cancel":
        try:
            if queue_worker is not None and current_user is not None:
                await queue_worker.cancel_job(job_id, current_user)
            else:
                await pipeline.cancel_job(job_id)
        except (KeyError, PermissionDeniedError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        if state is not None:
            await state.clear()
        if msg is not None:
            await msg.edit_text(f"🛑 <b>Job Cancelled</b> (Job <code>{job_id}</code>)")
        await callback.answer("Job cancelled.")
        return

    if action == "manual":
        if state is not None:
            await state.set_state(IngestionStates.awaiting_manual_query)
            await state.update_data(job_id=job_id)
        if msg is not None:
            await msg.answer(
                f"🔎 <b>Manual Metadata Search</b> (Job <code>{job_id}</code>)\n"
                "Reply with any of the following:\n"
                "• A <b>MusicBrainz URL or MBID</b> (<code>https://musicbrainz.org/release/...</code>)\n"
                "• <code>Artist - Title</code> or <code>Artist - Title | Album</code>\n"
                "• Just the artist / voice actor name (e.g. <code>Rie Takahashi</code>)\n"
                "• <code>isrc:JPD731600777</code> or <code>artist: Name | album: Title</code>"
            )
        await callback.answer("Enter manual search query or link.")
        return

    await callback.answer(f"Action '{action}' registered for job {job_id}.")


@verification_router.message(
    IngestionStates.awaiting_manual_query,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(RESERVED_MENU_BUTTONS),
)
async def handle_manual_search_query(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    pipeline: IngestionPipeline | None = None,
) -> None:
    """Re-query MusicBrainz candidates using the user's manual search query or link."""
    data = await state.get_data()
    job_id = str(data.get("job_id", "")).strip()
    if not job_id or pipeline is None:
        await state.clear()
        await message.answer("⚠️ No active verification job found for manual search.")
        return

    target_job = await pipeline.get_job(job_id)
    if not _can_resolve_job(current_user, target_job):
        await state.clear()
        await message.answer("🔒 You do not have permission to resolve another user's job.")
        return

    query_text = (message.text or "").strip()
    if not query_text:
        await message.answer(
            "Please reply with a MusicBrainz URL/MBID, <code>Artist - Title</code>, "
            "or <code>Artist - Title | Album</code>."
        )
        return

    try:
        result = await pipeline.search_manual_candidates(job_id, query_text)
    except KeyError:
        await state.clear()
        await message.answer(f"⚠️ Job <code>{job_id}</code> is no longer available.")
        return

    await state.set_state(IngestionStates.awaiting_confirmation)
    await message.answer(
        format_ingestion_summary(result),
        reply_markup=build_verification_keyboard(job_id, result.job.candidates),
    )
