"""Inline keyboard builders for the `/songs` interactive library browser, editor, and rescan menu."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.keyboards.admin import MenuCallback, append_escape_row
from src.core.models import LibraryTrackRecord, UserAccount, UserPreferences


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


class SongsToggleFieldCallback(CallbackData, prefix="songstog"):
    """Callback schema for toggling a field during selective changes review."""

    action: str = "t"
    track_id: str
    field: str


class SettingsCallback(CallbackData, prefix="stg"):
    """Callback schema for user preferences settings (`stg:<action>:<value>`)."""

    action: str
    value: str


class SongsBulkReviewCallback(CallbackData, prefix="songsb"):
    """Callback schema for bulk review session pagination and resolution (`songsb:<action>:<index>`)."""

    action: str
    index: int = 0


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
            ),
            InlineKeyboardButton(
                text="✨ Bulk Re-Search",
                callback_data=SongsMenuCallback(action="bulk_menu").pack(),
            ),
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
    can_edit: bool = True,
    back_callback: str | None = None,
    web_app_url: str | None = None,
    is_followed: bool | None = None,
) -> InlineKeyboardMarkup:
    """Build the ordered action menu for a song in `/songs` (Retrieval rows first, Edit rows second)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    if web_app_url and web_app_url.strip():
        builder.row(
            InlineKeyboardButton(
                text="🚀 Open in Mini App",
                web_app=WebAppInfo(url=f"{web_app_url.strip().rstrip('/')}?startapp=track_{track_id}"),
            )
        )
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
    if can_edit:
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
    else:
        builder.row(
            InlineKeyboardButton(
                text=f"📋 Copy Track ID ({sid})",
                copy_text=CopyTextButton(text=track_id),
            ),
        )
    radar_label = (
        "📡 Artist on Radar (🔔 Following)"
        if is_followed is True
        else ("📡 Artist on Radar (🔕 Follow)" if is_followed is False else "📡 Artist on Radar")
    )
    builder.row(
        InlineKeyboardButton(
            text=radar_label,
            callback_data=SongsTrackCallback(action="radar_artist", track_id=sid).pack(),
        )
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
            text="🎵 Auto Re-Search Lyrics Only",
            callback_data=SongsTrackCallback(action="research_lyrics", track_id=sid).pack(),
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


def build_lyrics_input_keyboard(track_id: str) -> InlineKeyboardMarkup:
    """Build keyboard for active lyrics prompt offering auto-search and escape (Level 3)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⚡ Auto-Search Lyrics",
            callback_data=SongsTrackCallback(action="research_lyrics", track_id=sid).pack(),
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
    *,
    has_lyrics_change: bool = False,
    has_meta_change: bool = False,
) -> InlineKeyboardMarkup:
    """Build the Dry-Run Preview confirmation keyboard with Accept All, Select Some, and Discard (Level 3)."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Accept All Changes",
            callback_data=SongsTrackCallback(action="confirm_apply", track_id=sid).pack(),
        )
    )
    if has_lyrics_change and has_meta_change:
        builder.row(
            InlineKeyboardButton(
                text="🏷️ Metadata Only",
                callback_data=SongsTrackCallback(action="apply_meta_only", track_id=sid).pack(),
            ),
            InlineKeyboardButton(
                text="📝 Lyrics Only",
                callback_data=SongsTrackCallback(action="apply_lyrics_only", track_id=sid).pack(),
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text="🎛️ Select Changes...",
            callback_data=SongsTrackCallback(action="select_changes", track_id=sid).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Discard Changes",
            callback_data=SongsTrackCallback(action="discard_changes", track_id=sid).pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Edit Again",
            callback_data=SongsTrackCallback(action=retry_action, track_id=sid).pack(),
        ),
    )
    append_escape_row(
        builder,
        back_callback=SongsTrackCallback(action="view", track_id=sid).pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_selective_changes_keyboard(
    track_id: str,
    enabled_fields: set[str],
    available_fields: list[tuple[str, str]],
) -> InlineKeyboardMarkup:
    """Build interactive toggle keyboard for selective changes acceptance."""
    sid = _short_id(track_id)
    builder = InlineKeyboardBuilder()
    for field_key, display_label in available_fields:
        is_enabled = field_key in enabled_fields
        prefix = "✅ [Apply]" if is_enabled else "⬜ [Keep]"
        btn_text = f"{prefix} {display_label}"
        builder.row(
            InlineKeyboardButton(
                text=btn_text[:64],
                callback_data=SongsToggleFieldCallback(track_id=sid, field=field_key).pack(),
            )
        )
    enabled_count = len(enabled_fields)
    total_count = len(available_fields)
    if enabled_count > 0:
        builder.row(
            InlineKeyboardButton(
                text=f"✅ Apply Selected ({enabled_count}/{total_count})",
                callback_data=SongsTrackCallback(action="confirm_apply_selected", track_id=sid).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="❌ Discard Changes",
            callback_data=SongsTrackCallback(action="discard_changes", track_id=sid).pack(),
        ),
        InlineKeyboardButton(
            text="◀ Back to Review",
            callback_data=SongsTrackCallback(action="review_proposal", track_id=sid).pack(),
        ),
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


def build_bulk_research_menu_keyboard(
    *,
    has_filter: bool = False,
    query: str = "",
) -> InlineKeyboardMarkup:
    """Build the scope selection keyboard for planned bulk metadata & lyrics re-search (Level 2)."""
    builder = InlineKeyboardBuilder()
    if has_filter and query.strip():
        q_label = query.strip()[:16]
        builder.row(
            InlineKeyboardButton(
                text=f"🎯 Current Search Filter ('{q_label}...')",
                callback_data=SongsMenuCallback(action="bulk_plan_search").pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🔍 All Library Tracks",
            callback_data=SongsMenuCallback(action="bulk_plan_all").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📝 Missing Lyrics Only",
            callback_data=SongsMenuCallback(action="bulk_plan_missing_lyrics").pack(),
        ),
        InlineKeyboardButton(
            text="🏷️ Missing MBID Tags",
            callback_data=SongsMenuCallback(action="bulk_plan_missing_mbid").pack(),
        ),
    )
    append_escape_row(
        builder,
        back_callback=SongsMenuCallback(action="list").pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_bulk_research_plan_keyboard(filter_mode: str) -> InlineKeyboardMarkup:
    """Build the execution confirmation keyboard for a planned bulk research run."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🚀 Start Bulk Re-Search",
            callback_data=SongsMenuCallback(action=f"bulk_run_{filter_mode}").pack(),
        )
    )
    append_escape_row(
        builder,
        back_callback=SongsMenuCallback(action="bulk_menu").pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_user_settings_keyboard(
    prefs: UserPreferences,
    *,
    user: UserAccount | None = None,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the interactive single-screen keyboard for user preferences."""
    builder = InlineKeyboardBuilder()

    fmt = prefs.stream_format
    builder.row(
        InlineKeyboardButton(
            text=f"{'✅ ' if fmt == 'auto' else ''}Auto",
            callback_data=SettingsCallback(action="fmt", value="auto").pack(),
        ),
        InlineKeyboardButton(
            text=f"{'✅ ' if fmt == 'original' else ''}Direct (Raw)",
            callback_data=SettingsCallback(action="fmt", value="original").pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"{'✅ ' if fmt == 'opus' else ''}Opus",
            callback_data=SettingsCallback(action="fmt", value="opus").pack(),
        ),
        InlineKeyboardButton(
            text=f"{'✅ ' if fmt == 'mp3' else ''}MP3",
            callback_data=SettingsCallback(action="fmt", value="mp3").pack(),
        ),
    )

    if fmt in {"auto", "opus", "mp3"}:
        br = prefs.stream_bitrate
        builder.row(
            InlineKeyboardButton(
                text=f"{'✅ ' if br == 128 else ''}128 kbps",
                callback_data=SettingsCallback(action="br", value="128").pack(),
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if br == 192 else ''}192 kbps",
                callback_data=SettingsCallback(action="br", value="192").pack(),
            ),
            InlineKeyboardButton(
                text=f"{'✅ ' if br == 320 else ''}320 kbps",
                callback_data=SettingsCallback(action="br", value="320").pack(),
            ),
        )

    lyr_state = (
        "✅ [Enabled] Auto-Search Lyrics" if prefs.auto_lyrics_search else "⬜ [Disabled] Auto-Search Lyrics"
    )
    builder.row(
        InlineKeyboardButton(
            text=lyr_state,
            callback_data=SettingsCallback(action="lyrics", value="toggle").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🎛️ Platform Links Priority",
            callback_data=SettingsCallback(action="platforms", value="order").pack(),
        )
    )

    if user is not None and user.is_oidc_linked:
        builder.row(
            InlineKeyboardButton(
                text="❌ Disconnect Authentik SSO",
                callback_data=SettingsCallback(action="oidc", value="disconnect").pack(),
            )
        )

    effective_back = back_callback or MenuCallback(action="home").pack()
    append_escape_row(
        builder,
        back_callback=effective_back,
        include_home=effective_back != MenuCallback(action="home").pack(),
    )
    return builder.as_markup()


def build_bulk_review_overview_keyboard(
    pending_count: int,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the summary action keyboard for tracks requiring review after bulk research."""
    builder = InlineKeyboardBuilder()
    if pending_count > 0:
        builder.row(
            InlineKeyboardButton(
                text=f"🎛️ Review Tracks ({pending_count} pending)",
                callback_data=SongsBulkReviewCallback(action="view", index=0).pack(),
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="✅ Apply All Pending Changes",
                callback_data=SongsBulkReviewCallback(action="apply_all", index=0).pack(),
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="❌ Discard All Pending",
                callback_data=SongsBulkReviewCallback(action="discard_all", index=0).pack(),
            )
        )
    append_escape_row(
        builder,
        back_callback=back_callback or SongsMenuCallback(action="bulk_menu").pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_bulk_track_review_keyboard(
    current_index: int,
    total_count: int,
) -> InlineKeyboardMarkup:
    """Build the single-track decision keyboard during an interactive bulk review session."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Accept & Apply",
            callback_data=SongsBulkReviewCallback(action="apply_one", index=current_index).pack(),
        ),
        InlineKeyboardButton(
            text="⏭️ Skip Track",
            callback_data=SongsBulkReviewCallback(action="skip_one", index=current_index).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ Discard Changes",
            callback_data=SongsBulkReviewCallback(action="discard_one", index=current_index).pack(),
        )
    )

    prev_idx = max(0, current_index - 1)
    next_idx = min(total_count - 1, current_index + 1)
    builder.row(
        InlineKeyboardButton(
            text="◀ Prev" if current_index > 0 else "◀",
            callback_data=SongsBulkReviewCallback(action="view", index=prev_idx).pack(),
        ),
        InlineKeyboardButton(
            text=f"📄 {current_index + 1} / {total_count}",
            callback_data=SongsBulkReviewCallback(action="noop", index=current_index).pack(),
        ),
        InlineKeyboardButton(
            text="Next ▶" if current_index < total_count - 1 else "▶",
            callback_data=SongsBulkReviewCallback(action="view", index=next_idx).pack(),
        ),
    )

    append_escape_row(
        builder,
        back_callback=SongsBulkReviewCallback(action="list", index=0).pack(),
        include_home=True,
    )
    return builder.as_markup()


def build_cancel_platform_order_keyboard(back_callback: str = "menu:settings") -> InlineKeyboardMarkup:
    """Build cancel escape keyboard during platform priority numeric input."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="◀️ Cancel",
            callback_data=SettingsCallback(action="cancel", value="cancel").pack(),
        )
    )
    return builder.as_markup()
