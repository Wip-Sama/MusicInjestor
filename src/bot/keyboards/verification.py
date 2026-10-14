"""InlineKeyboardMarkup builders for selecting metadata candidates, manual tagging, or cancelling a job."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.keyboards.admin import MenuCallback, QueueJobCallback, append_escape_row
from src.core.models import MetadataCandidate


class VerifySelectCallback(CallbackData, prefix="verify"):
    """Callback schema for selecting a specific metadata candidate (`verify:select:<job_id>:<index>`)."""

    action: str = "select"
    job_id: str
    candidate_index: int


class VerifyActionCallback(CallbackData, prefix="verify"):
    """Callback schema for manual search, full editor, back, or cancel (`verify:<action>:<job_id>`)."""

    action: str
    job_id: str


class VerifyFieldCallback(CallbackData, prefix="verify"):
    """Callback schema for editing a metadata field on a queued job (`verify:field:<job_id>:<field>`)."""

    action: str = "field"
    job_id: str
    field: str


def build_verification_keyboard(
    job_id: str,
    candidates: list[MetadataCandidate],
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Create inline buttons for candidates plus file retrieval, editor, manual search, hold, and cancel."""
    builder = InlineKeyboardBuilder()
    for index, candidate in enumerate(candidates):
        button_label = f"{index + 1}. {candidate.display_label}"
        builder.row(
            InlineKeyboardButton(
                text=button_label[:64],
                callback_data=VerifySelectCallback(
                    job_id=job_id,
                    candidate_index=index,
                ).pack(),
            )
        )

    # Row: Retrieve Queued File & Open Full-Pipeline Manual Tag Editor
    builder.row(
        InlineKeyboardButton(
            text="📥 Get Queued File",
            callback_data=QueueJobCallback(action="get_file", job_id=job_id).pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Manual Tag & Editor",
            callback_data=VerifyActionCallback(action="edit", job_id=job_id).pack(),
        ),
    )
    # Row: Manual Search / Link & Auto Re-Search
    builder.row(
        InlineKeyboardButton(
            text="🔎 Manual Search / Link",
            callback_data=VerifyActionCallback(action="manual", job_id=job_id).pack(),
        ),
        InlineKeyboardButton(
            text="⚡ Auto Re-Search",
            callback_data=VerifyActionCallback(action="auto_enrich", job_id=job_id).pack(),
        ),
    )
    # Row: Hold in Queue & Cancel
    builder.row(
        InlineKeyboardButton(
            text="⏸️ Hold in Queue",
            callback_data=QueueJobCallback(action="pause", job_id=job_id).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=VerifyActionCallback(action="cancel", job_id=job_id).pack(),
        ),
    )
    effective_back = back_callback or MenuCallback(action="home").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_manual_search_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Create a Level-2 escape keyboard (`◀ Back` to candidates and `🏠 Menu`) for manual search prompts."""
    builder = InlineKeyboardBuilder()
    append_escape_row(
        builder,
        back_callback=VerifyActionCallback(action="back", job_id=job_id).pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_job_edit_keyboard(
    job_id: str,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the full-pipeline manual tag & dry-run editor keyboard for a queued job."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Confirm & Ingest Track",
            callback_data=VerifyActionCallback(action="apply_edit", job_id=job_id).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📥 Get Queued File",
            callback_data=QueueJobCallback(action="get_file", job_id=job_id).pack(),
        ),
        InlineKeyboardButton(
            text="⚡ Auto Re-Search",
            callback_data=VerifyActionCallback(action="auto_enrich", job_id=job_id).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🏷️ Edit Title",
            callback_data=VerifyFieldCallback(job_id=job_id, field="title").pack(),
        ),
        InlineKeyboardButton(
            text="🎤 Edit Artist",
            callback_data=VerifyFieldCallback(job_id=job_id, field="artist").pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="💿 Edit Album",
            callback_data=VerifyFieldCallback(job_id=job_id, field="album").pack(),
        ),
        InlineKeyboardButton(
            text="📅 Edit Year",
            callback_data=VerifyFieldCallback(job_id=job_id, field="year").pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🔢 Edit Track #",
            callback_data=VerifyFieldCallback(job_id=job_id, field="track").pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Edit Multiple Fields",
            callback_data=VerifyFieldCallback(job_id=job_id, field="multi").pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🖼️ Edit Cover",
            callback_data=VerifyActionCallback(action="cover", job_id=job_id).pack(),
        ),
        InlineKeyboardButton(
            text="📝 Edit Lyrics",
            callback_data=VerifyActionCallback(action="lyrics", job_id=job_id).pack(),
        ),
    )
    effective_back = back_callback or VerifyActionCallback(action="back", job_id=job_id).pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=True,
    )
    return builder.as_markup()


def build_job_input_cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Build a 2-tier escape keyboard for active job metadata/cover/lyrics prompts."""
    builder = InlineKeyboardBuilder()
    append_escape_row(
        builder,
        back_callback=VerifyActionCallback(action="edit", job_id=job_id).pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_job_lyrics_prompt_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Build keyboard for queued job lyrics prompt with auto-search button."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⚡ Auto-Search Lyrics",
            callback_data=VerifyActionCallback(action="auto_lyrics", job_id=job_id).pack(),
        )
    )
    append_escape_row(
        builder,
        back_callback=VerifyActionCallback(action="edit", job_id=job_id).pack(),
        include_home=True,
    )
    return builder.as_markup()
