"""Inline keyboards and CallbackData schemas for unified search and album gap analysis."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.keyboards.admin import append_escape_row
from src.core.models import AlbumGapAnalysis, UnifiedSearchResult


class SearchActionCallback(CallbackData, prefix="srch", sep="~"):
    """Callback schema for search scope toggling and navigation (`srch~<action>~<value>~<query>`)."""

    action: str  # "scope", "preview", "close"
    value: str = ""
    query: str = ""


class AlbumGapCallback(CallbackData, prefix="agap", sep="~"):
    """Callback schema for album gap actions (`agap~<action>~<album_id>~<track_pos>`)."""

    action: str  # "view", "preview", "play"
    album_id: str
    track_pos: int = 0


def build_search_results_keyboard(
    result: UnifiedSearchResult,
    *,
    web_app_url: str | None = None,
    back_callback: str = "menu:home",
) -> InlineKeyboardMarkup:
    """Build keyboard presenting matched albums, tracks, and scope toggles."""
    builder = InlineKeyboardBuilder()

    # 1. Matched albums (up to 5)
    for alb in result.albums[:5]:
        cov_text = f"{alb.owned_tracks_count}/{alb.total_tracks}"
        btn_label = f"💿 {alb.title[:22]} ({cov_text})"
        builder.row(
            InlineKeyboardButton(
                text=btn_label,
                callback_data=AlbumGapCallback(action="view", album_id=alb.id).pack(),
            )
        )

    # 2. Matched tracks (up to 5)
    for trk in result.tracks[:5]:
        if trk.in_library and trk.library_track_id:
            btn_label = f"▶️ {trk.title[:24]} (Owned)"
            builder.row(
                InlineKeyboardButton(
                    text=btn_label,
                    callback_data=f"songs:view:{trk.library_track_id[:12]}",
                )
            )
        elif trk.preview_url:
            btn_label = f"🎧 Preview: {trk.title[:24]}"
            builder.row(
                InlineKeyboardButton(
                    text=btn_label,
                    callback_data=SearchActionCallback(
                        action="preview",
                        value=trk.id,
                        query=result.query[:20],
                    ).pack(),
                )
            )

    # 3. Scope toggle button
    next_scope = "library" if result.scope == "global" else "global"
    scope_btn_label = (
        "📚 Switch to Library Only" if result.scope == "global" else "🌐 Switch to Global Search"
    )
    builder.row(
        InlineKeyboardButton(
            text=scope_btn_label,
            callback_data=SearchActionCallback(
                action="scope",
                value=next_scope,
                query=result.query[:30],
            ).pack(),
        )
    )

    # 4. Optional Mini App deep link
    if web_app_url and web_app_url.strip():
        builder.row(
            InlineKeyboardButton(
                text="🚀 Open Search in Web App",
                web_app=WebAppInfo(url=f"{web_app_url.strip().rstrip('/')}?startapp=search"),
            )
        )

    append_escape_row(builder, back_callback=back_callback, include_home=True)
    return builder.as_markup()


def build_album_gap_keyboard(
    gap: AlbumGapAnalysis,
    *,
    web_app_url: str | None = None,
    back_callback: str = "menu:home",
) -> InlineKeyboardMarkup:
    """Build keyboard for album gap analysis, offering track previews and library play buttons."""
    builder = InlineKeyboardBuilder()

    # Track actions (preview missing tracks or play library tracks)
    # Up to 8 tracks displayed as direct buttons to avoid huge keyboards
    for trk in gap.tracks[:8]:
        if trk.is_owned and trk.library_track_id:
            builder.row(
                InlineKeyboardButton(
                    text=f"▶️ #{trk.position} {trk.title[:22]} (Library)",
                    callback_data=f"songs:view:{trk.library_track_id[:12]}",
                )
            )
        elif trk.preview_url:
            builder.row(
                InlineKeyboardButton(
                    text=f"🎧 Preview #{trk.position} {trk.title[:22]} (30s)",
                    callback_data=AlbumGapCallback(
                        action="preview",
                        album_id=gap.album_id,
                        track_pos=trk.position,
                    ).pack(),
                )
            )

    # External streaming link if present
    if gap.external_url and gap.external_url.startswith("http"):
        builder.row(
            InlineKeyboardButton(
                text="🌐 Open on Deezer",
                url=gap.external_url,
            )
        )

    # Optional Mini App deep link
    if web_app_url and web_app_url.strip():
        builder.row(
            InlineKeyboardButton(
                text="🚀 View Album in Web App",
                web_app=WebAppInfo(url=f"{web_app_url.strip().rstrip('/')}?startapp=radar"),
            )
        )

    append_escape_row(builder, back_callback=back_callback, include_home=True)
    return builder.as_markup()
