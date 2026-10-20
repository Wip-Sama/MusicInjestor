"""Inline keyboard builders and callback models for the `/radar` Release Radar menu."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.keyboards.admin import append_escape_row
from src.core.models import DiscoveredRelease, FollowedArtist


class RadarCallback(CallbackData, prefix="radar", sep="~"):
    """Callback schema for Release Radar navigation (`radar~<action>~<target_id>~<page>`)."""

    action: str
    target_id: str = ""
    page: int = 1


def build_radar_home_keyboard(
    releases: list[DiscoveredRelease],
    page: int,
    total_pages: int,
    followed_count: int,
    web_app_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the paginated Release Radar home screen keyboard."""
    builder = InlineKeyboardBuilder()

    if web_app_url and web_app_url.strip():
        builder.row(
            InlineKeyboardButton(
                text="📡 Open Radar in Mini App",
                web_app=WebAppInfo(url=f"{web_app_url.strip().rstrip('/')}?startapp=radar"),
            )
        )

    # Release items on this page (each gets a clickable button)
    for rel in releases:
        type_badge = "💿" if rel.release_type == "album" else "🎵"
        label = f"{type_badge} {rel.artist_name} - {rel.title} ({rel.release_date[:4]})"
        if len(label) > 45:
            label = f"{label[:42]}..."
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=RadarCallback(action="view", target_id=rel.id, page=page).pack(),
            )
        )

    # Pagination row
    if total_pages > 1:
        prev_page = max(1, page - 1)
        next_page = min(total_pages, page + 1)
        builder.row(
            InlineKeyboardButton(
                text="◀️ Prev" if page > 1 else "⏮️",
                callback_data=RadarCallback(action="page", page=prev_page).pack(),
            ),
            InlineKeyboardButton(
                text=f"{page}/{total_pages}",
                callback_data=RadarCallback(action="noop").pack(),
            ),
            InlineKeyboardButton(
                text="Next ▶️" if page < total_pages else "⏭️",
                callback_data=RadarCallback(action="page", page=next_page).pack(),
            ),
        )

    # Management controls
    builder.row(
        InlineKeyboardButton(
            text=f"🔔 Followed Artists ({followed_count})",
            callback_data=RadarCallback(action="follows_list", page=1).pack(),
        ),
        InlineKeyboardButton(
            text="🔄 Scan Releases Now",
            callback_data=RadarCallback(action="refresh").pack(),
        ),
    )

    append_escape_row(builder, back_callback="menu:home", include_home=True)
    return builder.as_markup()


def build_release_detail_keyboard(
    release: DiscoveredRelease,
    is_followed: bool,
    web_app_url: str | None = None,
    page: int = 1,
) -> InlineKeyboardMarkup:
    """Build action buttons for inspecting a single discovered release."""
    builder = InlineKeyboardBuilder()

    # Audio preview button if preview_url is present or can be fetched
    builder.row(
        InlineKeyboardButton(
            text="🎧 Listen 30s Audio Preview",
            callback_data=RadarCallback(action="preview", target_id=release.id, page=page).pack(),
        )
    )

    # Follow/Unfollow toggle for this release's artist
    follow_text = f"🔕 Unfollow {release.artist_name}" if is_followed else f"🔔 Follow {release.artist_name}"
    follow_action = "unfollow" if is_followed else "follow"
    builder.row(
        InlineKeyboardButton(
            text=follow_text,
            callback_data=RadarCallback(action=follow_action, target_id=release.id, page=page).pack(),
        )
    )

    # Provider and Web App links
    row_links: list[InlineKeyboardButton] = []
    if release.external_url and release.external_url.startswith("http"):
        row_links.append(
            InlineKeyboardButton(
                text="🌐 Open Deezer Link",
                url=release.external_url,
            )
        )
    if web_app_url and web_app_url.strip():
        row_links.append(
            InlineKeyboardButton(
                text="🎵 Mini App",
                web_app=WebAppInfo(url=f"{web_app_url.strip().rstrip('/')}?startapp=release_{release.id}"),
            )
        )
    if row_links:
        builder.row(*row_links)

    builder.row(
        InlineKeyboardButton(
            text="◀️ Back to Releases",
            callback_data=RadarCallback(action="page", page=page).pack(),
        )
    )
    return builder.as_markup()


def build_preview_audio_keyboard(
    release: DiscoveredRelease,
    web_app_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Build inline buttons attached to the 30-second audio preview file sent in chat."""
    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    if release.external_url and release.external_url.startswith("http"):
        row.append(
            InlineKeyboardButton(
                text="🌐 Open Provider",
                url=release.external_url,
            )
        )
    if web_app_url and web_app_url.strip():
        row.append(
            InlineKeyboardButton(
                text="🎵 Open in Mini App",
                web_app=WebAppInfo(url=f"{web_app_url.strip().rstrip('/')}?startapp=release_{release.id}"),
            )
        )
    if not row:
        row.append(
            InlineKeyboardButton(
                text="📡 Release Radar",
                callback_data=RadarCallback(action="page", page=1).pack(),
            )
        )
    builder.row(*row)
    return builder.as_markup()


def build_followed_artists_keyboard(
    follows: list[FollowedArtist],
    page: int = 1,
    per_page: int = 6,
) -> InlineKeyboardMarkup:
    """Build the paginated list of followed artists with one-tap unfollow buttons."""
    builder = InlineKeyboardBuilder()
    total_pages = max(1, (len(follows) + per_page - 1) // per_page)
    start_idx = (page - 1) * per_page
    slice_follows = follows[start_idx : start_idx + per_page]

    for item in slice_follows:
        builder.row(
            InlineKeyboardButton(
                text=f"🎤 {item.artist_name}",
                callback_data=RadarCallback(action="artist_releases", target_id=item.artist_name).pack(),
            ),
            InlineKeyboardButton(
                text="🔕 Unfollow",
                callback_data=RadarCallback(
                    action="unfollow_list", target_id=item.artist_name, page=page
                ).pack(),
            ),
        )

    if total_pages > 1:
        prev_p = max(1, page - 1)
        next_p = min(total_pages, page + 1)
        builder.row(
            InlineKeyboardButton(
                text="◀️ Prev" if page > 1 else "⏮️",
                callback_data=RadarCallback(action="follows_list", page=prev_p).pack(),
            ),
            InlineKeyboardButton(
                text=f"{page}/{total_pages}",
                callback_data=RadarCallback(action="noop").pack(),
            ),
            InlineKeyboardButton(
                text="Next ▶️" if page < total_pages else "⏭️",
                callback_data=RadarCallback(action="follows_list", page=next_p).pack(),
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text="◀️ Back to Releases",
            callback_data=RadarCallback(action="page", page=1).pack(),
        )
    )
    return builder.as_markup()
