"""Inline keyboard builders for the `/songs` interactive library browser, editor, and rescan menu."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.core.models import LibraryTrackRecord


def _short_id(track_id: str) -> str:
    """Return a 12-character track ID prefix so Telegram callback data stays well under 64 bytes."""
    return track_id.strip()[:12]


def build_songs_search_menu_keyboard(
    tracks: list[LibraryTrackRecord],
    *,
    can_rescan: bool = True,
) -> InlineKeyboardMarkup:
    """Build the `/songs` search results & library menu inline keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    for rec in tracks[:12]:
        sid = _short_id(rec.id)
        year_part = f" ({rec.year})" if rec.year else ""
        label = f"🎵 {rec.artist} - {rec.title} | {rec.album}{year_part}"
        if len(label) > 60:
            label = f"{label[:57]}..."
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"songs:view:{sid}",
                )
            ]
        )

    bottom_row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text="🔎 Search Library",
            callback_data="songs:search_prompt",
        ),
        InlineKeyboardButton(
            text="📤 Export All JSON",
            callback_data="songs:export_all",
        ),
    ]
    rows.append(bottom_row)

    if can_rescan:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Rescan Library Folder",
                    callback_data="songs:rescan_dryrun",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_song_action_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build the action menu for a selected song in `/songs`."""
    sid = _short_id(track_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Get Song File",
                    callback_data=f"songs:get_track:{sid}",
                ),
                InlineKeyboardButton(
                    text="💿 Get Full Album",
                    callback_data=f"songs:get_album:{sid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Edit Name / Metadata",
                    callback_data=f"songs:edit_meta:{sid}",
                ),
                InlineKeyboardButton(
                    text="🖼️ Edit Cover",
                    callback_data=f"songs:edit_cover:{sid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📝 Edit Lyrics",
                    callback_data=f"songs:edit_lyrics:{sid}",
                ),
                InlineKeyboardButton(
                    text="🔎 Auto Re-Search",
                    callback_data=f"songs:research:{sid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📤 Export Data",
                    callback_data=f"songs:export:{sid}",
                ),
                InlineKeyboardButton(
                    text="📥 Import Data",
                    callback_data=f"songs:import:{sid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back to Songs List",
                    callback_data="songs:list",
                )
            ],
        ]
    )


def build_metadata_field_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build the field picker keyboard for editing song title, artist, album, year, or track number."""
    sid = _short_id(track_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏷️ Edit Title",
                    callback_data=f"songs:field:{sid}:title",
                ),
                InlineKeyboardButton(
                    text="🎤 Edit Artist",
                    callback_data=f"songs:field:{sid}:artist",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💿 Edit Album",
                    callback_data=f"songs:field:{sid}:album",
                ),
                InlineKeyboardButton(
                    text="📅 Edit Year",
                    callback_data=f"songs:field:{sid}:year",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔢 Edit Track #",
                    callback_data=f"songs:field:{sid}:track",
                ),
                InlineKeyboardButton(
                    text="✏️ Edit Multiple Fields",
                    callback_data=f"songs:field:{sid}:multi",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back to Song",
                    callback_data=f"songs:view:{sid}",
                )
            ],
        ]
    )


def build_research_mode_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build the re-search mode picker keyboard (Auto vs Custom Query/URL/MBID)."""
    sid = _short_id(track_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡ Auto Re-Search (MB + Genius + Last.fm + LRCLIB)",
                    callback_data=f"songs:research_auto:{sid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⌨️ Custom Search Query / URL / MBID",
                    callback_data=f"songs:research_custom:{sid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back to Song",
                    callback_data=f"songs:view:{sid}",
                )
            ],
        ]
    )


def build_dry_run_confirmation_keyboard(
    track_id: str,
    retry_action: str = "edit_meta",
) -> InlineKeyboardMarkup:
    """Build the Dry-Run Preview confirmation keyboard (`Confirm & Apply` / `Edit` / `Cancel`)."""
    sid = _short_id(track_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Confirm & Apply Changes",
                    callback_data=f"songs:confirm_apply:{sid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Edit Again",
                    callback_data=f"songs:{retry_action}:{sid}",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data=f"songs:view:{sid}",
                ),
            ],
        ]
    )


def build_rescan_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Build the Dry-Run Preview confirmation keyboard for a library folder rescan."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Confirm & Sync Database",
                    callback_data="songs:rescan_apply",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="songs:list",
                )
            ],
        ]
    )


def build_input_cancel_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build a focused Cancel / Back keyboard for active cover, lyrics, or JSON import input prompts."""
    sid = _short_id(track_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Cancel & Back to Song",
                    callback_data=f"songs:view:{sid}",
                ),
                InlineKeyboardButton(
                    text="🎵 Songs List",
                    callback_data="songs:list",
                ),
            ]
        ]
    )
