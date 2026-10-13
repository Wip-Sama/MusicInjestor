"""Inline keyboard builders for the `/songs` interactive library browser, editor, and rescan menu."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.keyboards.admin import MenuCallback, append_escape_row
from src.core.models import LibraryTrackRecord


class SongsMenuCallback(CallbackData, prefix="songs"):
    """Callback schema for top-level `/songs` menu actions (`songs:<action>`)."""

    action: str


class SongsTrackCallback(CallbackData, prefix="songs"):
    """Callback schema for song-specific actions (`songs:<action>:<track_id>`)."""

    action: str
    track_id: str


class SongsFieldCallback(CallbackData, prefix="songs"):
    """Callback schema for selecting a metadata field to edit (`songs:field:<track_id>:<field>`)."""

    action: str = "field"
    track_id: str
    field: str


class SongsPageCallback(CallbackData, prefix="songsp"):
    """Callback schema for library browser pagination (`songsp:<action>:<page>:<sort>`)."""

    action: str
    page: int
    sort: str = "recent"


class SongsSortCallback(CallbackData, prefix="songss"):
    """Callback schema for library browser sorting selection (`songss:<action>:<sort>`)."""

    action: str = "s"
    sort: str


SORT_LABELS: dict[str, str] = {
    "recent": "🕒 Recent",
    "artist": "🎤 Artist",
    "title": "🔤 Title",
    "album": "💿 Album",
    "year": "📅 Year",
}

SORT_DESCRIPTIONS: dict[str, str] = {
    "recent": "🕒 Recently Added (Newest First)",
    "artist": "🎤 Artist (A–Z)",
    "title": "🔤 Title (A–Z)",
    "album": "💿 Album (A–Z)",
    "year": "📅 Release Year (Newest First)",
}


def _short_id(track_id: str) -> str:
    """Return a 12-character track ID prefix so Telegram callback data stays well under 64 bytes."""
    return track_id.strip()[:12]


def build_songs_search_menu_keyboard(
    tracks: list[LibraryTrackRecord],
    *,
    page: int = 1,
    total_pages: int = 1,
    total_count: int = 0,
    sort_by: str = "recent",
    query: str = "",
    can_rescan: bool = True,
    is_filtered: bool = False,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the `/songs` library browser (Level 1) or filtered search results keyboard (Level 2)."""
    builder = InlineKeyboardBuilder()
    for rec in tracks:
        sid = _short_id(rec.id)
        year_part = f" ({rec.year})" if rec.year else ""
        label = f"🎵 {rec.artist} - {rec.title} | {rec.album}{year_part}"
        if len(label) > 60:
            label = f"{label[:57]}..."
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=SongsTrackCallback(action="view", track_id=sid).pack(),
            )
        )

    # Pagination navigation controls
    if total_pages > 1:
        prev_data = (
            SongsPageCallback(action="p", page=max(1, page - 1), sort=sort_by).pack()
            if page > 1
            else SongsPageCallback(action="noop_first", page=page, sort=sort_by).pack()
        )
        next_data = (
            SongsPageCallback(action="p", page=min(total_pages, page + 1), sort=sort_by).pack()
            if page < total_pages
            else SongsPageCallback(action="noop_last", page=page, sort=sort_by).pack()
        )
        info_data = SongsPageCallback(action="noop_info", page=page, sort=sort_by).pack()

        builder.row(
            InlineKeyboardButton(text="◀ Prev" if page > 1 else "◀", callback_data=prev_data),
            InlineKeyboardButton(text=f"📄 {page} / {total_pages}", callback_data=info_data),
            InlineKeyboardButton(text="Next ▶" if page < total_pages else "▶", callback_data=next_data),
        )

    # Sort selector button
    current_sort_label = SORT_LABELS.get(sort_by, "Recent")
    builder.row(
        InlineKeyboardButton(
            text=f"🔀 Sort: {current_sort_label} ▾",
            callback_data=SongsMenuCallback(action="sort_menu").pack(),
        )
    )

    # Action row: Search & Export
    builder.row(
        InlineKeyboardButton(
            text="🔎 Search Library",
            callback_data=SongsMenuCallback(action="search_prompt").pack(),
        ),
        InlineKeyboardButton(
            text="📤 Export All JSON",
            callback_data=SongsMenuCallback(action="export_all").pack(),
        ),
    )

    # If filtered, offer a clear search button
    if is_filtered:
        truncated_q = f"'{query[:18]}...'" if len(query) > 18 else f"'{query}'"
        filter_label = f"❌ Clear Search ({truncated_q})" if query else "❌ Clear Search Filter"
        builder.row(
            InlineKeyboardButton(
                text=filter_label,
                callback_data=SongsMenuCallback(action="clear_search").pack(),
            )
        )

    if can_rescan:
        builder.row(
            InlineKeyboardButton(
                text="🔄 Rescan Library Folder",
                callback_data=SongsMenuCallback(action="rescan_dryrun").pack(),
            )
        )

    if is_filtered:
        append_escape_row(
            builder,
            back_callback=back_callback or SongsMenuCallback(action="list").pack(),
            include_home=True,
        )
    else:
        effective_back = back_callback or MenuCallback(action="home").pack()
        append_escape_row(
            builder,
            back_callback=effective_back,
            include_home=effective_back != MenuCallback(action="home").pack(),
        )
    return builder.as_markup()


def build_songs_sort_keyboard(current_sort: str = "recent") -> InlineKeyboardMarkup:
    """Build the sort order selection keyboard for the library browser."""
    builder = InlineKeyboardBuilder()
    for sort_key, desc in SORT_DESCRIPTIONS.items():
        prefix = "✅ " if sort_key == current_sort else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{prefix}{desc}",
                callback_data=SongsSortCallback(action="s", sort=sort_key).pack(),
            )
        )
    append_escape_row(
        builder,
        back_callback=SongsMenuCallback(action="list").pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_search_prompt_keyboard() -> InlineKeyboardMarkup:
    """Build the 2-tier escape keyboard for the `/songs` search input prompt (Level 2)."""
    builder = InlineKeyboardBuilder()
    append_escape_row(
        builder,
        back_callback=SongsMenuCallback(action="list").pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_song_action_keyboard(
    track_id: str,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the ordered action menu for a song in `/songs` (Retrieval rows first, Edit rows second)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    # --- Section 1: Retrieval, Export & Provenance (Read-Only) ---
    builder.row(
        InlineKeyboardButton(
            text="📥 Get Song File",
            callback_data=SongsTrackCallback(action="get_track", track_id=sid).pack(),
        ),
        InlineKeyboardButton(
            text="💿 Get Full Album",
            callback_data=SongsTrackCallback(action="get_album", track_id=sid).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="📤 Export Data",
            callback_data=SongsTrackCallback(action="export", track_id=sid).pack(),
        ),
        InlineKeyboardButton(
            text="📜 Song History",
            callback_data=SongsTrackCallback(action="history", track_id=sid).pack(),
        ),
    )
    # --- Section 2: Editing, Enrichment & Import (Mutating with Dry-Run) ---
    builder.row(
        InlineKeyboardButton(
            text="✏️ Edit Name / Metadata",
            callback_data=SongsTrackCallback(action="edit_meta", track_id=sid).pack(),
        ),
        InlineKeyboardButton(
            text="🖼️ Edit Cover",
            callback_data=SongsTrackCallback(action="edit_cover", track_id=sid).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="📝 Edit Lyrics",
            callback_data=SongsTrackCallback(action="edit_lyrics", track_id=sid).pack(),
        ),
        InlineKeyboardButton(
            text="🔎 Auto Re-Search",
            callback_data=SongsTrackCallback(action="research", track_id=sid).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="📥 Import Data",
            callback_data=SongsTrackCallback(action="import", track_id=sid).pack(),
        ),
        InlineKeyboardButton(
            text=f"📋 Copy Track ID ({sid})",
            copy_text=CopyTextButton(text=track_id),
        ),
    )
    effective_back = back_callback or SongsMenuCallback(action="list").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_song_history_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build the 2-tier escape keyboard for the chronological Song History screen (Level 3)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    append_escape_row(
        builder,
        back_callback=SongsTrackCallback(action="view", track_id=sid).pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_metadata_field_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build the field picker keyboard for editing song metadata (Level 3)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🏷️ Edit Title",
        callback_data=SongsFieldCallback(track_id=sid, field="title").pack(),
    )
    builder.button(
        text="🎤 Edit Artist",
        callback_data=SongsFieldCallback(track_id=sid, field="artist").pack(),
    )
    builder.button(
        text="💿 Edit Album",
        callback_data=SongsFieldCallback(track_id=sid, field="album").pack(),
    )
    builder.button(
        text="📅 Edit Year",
        callback_data=SongsFieldCallback(track_id=sid, field="year").pack(),
    )
    builder.button(
        text="🔢 Edit Track #",
        callback_data=SongsFieldCallback(track_id=sid, field="track").pack(),
    )
    builder.button(
        text="✏️ Edit Multiple Fields",
        callback_data=SongsFieldCallback(track_id=sid, field="multi").pack(),
    )
    builder.adjust(2)
    append_escape_row(
        builder,
        back_callback=SongsTrackCallback(action="view", track_id=sid).pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_research_mode_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build the re-search mode picker keyboard (Level 3)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⚡ Auto Re-Search (MB + Genius + Last.fm + LRCLIB)",
            callback_data=SongsTrackCallback(action="research_auto", track_id=sid).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⌨️ Custom Search Query / URL / MBID",
            callback_data=SongsTrackCallback(action="research_custom", track_id=sid).pack(),
        )
    )
    append_escape_row(
        builder,
        back_callback=SongsTrackCallback(action="view", track_id=sid).pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_dry_run_confirmation_keyboard(
    track_id: str,
    retry_action: str = "edit_meta",
) -> InlineKeyboardMarkup:
    """Build the Dry-Run Preview confirmation keyboard (Level 3)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Confirm & Apply Changes",
            callback_data=SongsTrackCallback(action="confirm_apply", track_id=sid).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✏️ Edit Again",
            callback_data=SongsTrackCallback(action=retry_action, track_id=sid).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=SongsTrackCallback(action="view", track_id=sid).pack(),
        ),
    )
    append_escape_row(
        builder,
        back_callback=SongsTrackCallback(action="view", track_id=sid).pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_rescan_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Build the Dry-Run Preview confirmation keyboard for a library folder rescan (Level 2)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Confirm & Sync Database",
            callback_data=SongsMenuCallback(action="rescan_apply").pack(),
        )
    )
    append_escape_row(
        builder,
        back_callback=SongsMenuCallback(action="list").pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_input_cancel_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build a 2-tier escape keyboard for active cover, lyrics, or JSON import prompts (Level 3)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    append_escape_row(
        builder,
        back_callback=SongsTrackCallback(action="view", track_id=sid).pack(),
        include_home=True,
    )
    return builder.as_markup()
