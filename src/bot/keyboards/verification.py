"""InlineKeyboardMarkup builder for selecting metadata candidates or cancelling a job."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.core.models import MetadataCandidate


def build_verification_keyboard(
    job_id: str,
    candidates: list[MetadataCandidate],
) -> InlineKeyboardMarkup:
    """Create inline buttons for each metadata candidate plus manual search and cancel actions."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, candidate in enumerate(candidates):
        button_label = f"{index + 1}. {candidate.display_label}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=button_label[:64],
                    callback_data=f"verify:select:{job_id}:{index}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="Manual Search / Link",
                callback_data=f"verify:manual:{job_id}",
            ),
            InlineKeyboardButton(
                text="Cancel",
                callback_data=f"verify:cancel:{job_id}",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
