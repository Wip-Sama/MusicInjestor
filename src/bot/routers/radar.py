"""Interactive `/radar` router for release discovery, artist following, and audio previews."""

import html
import logging
from math import ceil

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.keyboards.radar import (
    RadarCallback,
    build_album_tracks_keyboard,
    build_followed_artists_keyboard,
    build_other_platforms_keyboard,
    build_preview_audio_keyboard,
    build_radar_home_keyboard,
    build_release_detail_keyboard,
)
from src.bot.ui import ack_callback, format_screen, safe_edit_text
from src.config.settings import AppSettings
from src.core.deezer import DeezerReleaseProvider
from src.core.models import DiscoveredRelease, UserAccount
from src.core.platform_links import build_streaming_platform_links
from src.core.ports import GovernanceRepositoryPort
from src.core.radar import ReleaseRadarService

logger = logging.getLogger(__name__)

radar_router = Router(name="radar")
RELEASES_PAGE_SIZE: int = 6


def format_radar_home_card(
    total_releases: int,
    followed_count: int,
    monitored_count: int,
    page: int,
    total_pages: int,
) -> str:
    """Format the 3-tier HTML Release Radar overview screen."""
    context = [
        f"• 📡 <b>Catalog Radar:</b> Tracking <b>{monitored_count}</b> artists in system.",
        f"• 🔔 <b>Followed Artists:</b> You are following <b>{followed_count}</b> artists.",
        f"• 🌟 <b>Discovered Releases:</b> <b>{total_releases}</b> recent drops found.",
        "",
        (
            "<i>Tap any release below to listen to a 30-second audio preview, "
            "toggle notifications, or view on Deezer.</i>"
        ),
    ]
    footers = [f"Page {page}/{max(1, total_pages)} • <i>📡 Release Radar</i>"]
    return format_screen(
        title="📡 Release Radar & New Drops",
        context_lines=context,
        footer_lines=footers,
    )


def format_release_detail_card(
    release: DiscoveredRelease,
    is_followed: bool,
) -> str:
    """Format the 3-tier HTML release card for inspection and listening."""
    lib_badge = "✅ <b>Already In Library</b>" if release.in_library else "🆕 <b>Not In Library</b>"
    follow_badge = "🔔 <b>Following Artist</b>" if is_followed else "🔕 <b>Not Following</b>"

    context = [
        f"🎤 <b>Artist:</b> <code>{html.escape(release.artist_name)}</code>",
        f"💿 <b>Release:</b> <b>{html.escape(release.title)}</b>",
        (
            f"🏷️ <b>Format:</b> <code>{html.escape(release.release_type.upper())}</code> "
            f"({release.track_count} tracks)"
        ),
        f"📅 <b>Date:</b> <code>{html.escape(release.release_date or 'Unknown')}</code>",
        f"📊 <b>Status:</b> {lib_badge}",
        f"👤 <b>Subscription:</b> {follow_badge}",
    ]
    if release.preview_url:
        context.append("\n🎧 <i>30-second audio preview is available for immediate listening!</i>")

    footers = [f"Provider: <code>{html.escape(release.provider)}</code> • <i>Release Detail</i>"]
    return format_screen(
        title=f"💿 {release.title}",
        context_lines=context,
        footer_lines=footers,
    )


@radar_router.message(Command("radar"))
async def handle_cmd_radar(
    message: Message,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    settings: AppSettings,
) -> None:
    """Handle `/radar` command by presenting the top recent releases and followed artists."""
    user_id = current_user.telegram_id if current_user else (message.from_user.id if message.from_user else 0)
    all_releases = await governance_repository.list_discovered_releases(limit=60)
    user_follows = await governance_repository.list_user_followed_artists(user_id)
    monitored = await governance_repository.list_all_monitored_artists()

    total_pages = max(1, ceil(len(all_releases) / RELEASES_PAGE_SIZE))
    slice_releases = all_releases[:RELEASES_PAGE_SIZE]

    card = format_radar_home_card(
        total_releases=len(all_releases),
        followed_count=len(user_follows),
        monitored_count=len(monitored),
        page=1,
        total_pages=total_pages,
    )
    keyboard = build_radar_home_keyboard(
        releases=slice_releases,
        page=1,
        total_pages=total_pages,
        followed_count=len(user_follows),
        web_app_url=settings.web_app_url,
    )
    await message.answer(card, reply_markup=keyboard, parse_mode="HTML")


@radar_router.callback_query(RadarCallback.filter(F.action == "page"))
async def handle_radar_page(
    query: CallbackQuery,
    callback_data: RadarCallback,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    settings: AppSettings,
) -> None:
    """Handle pagination across discovered releases."""
    await ack_callback(query)
    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    all_releases = await governance_repository.list_discovered_releases(limit=60)
    user_follows = await governance_repository.list_user_followed_artists(user_id)
    monitored = await governance_repository.list_all_monitored_artists()

    total_pages = max(1, ceil(len(all_releases) / RELEASES_PAGE_SIZE))
    page = max(1, min(total_pages, callback_data.page))
    start_idx = (page - 1) * RELEASES_PAGE_SIZE
    slice_releases = all_releases[start_idx : start_idx + RELEASES_PAGE_SIZE]

    card = format_radar_home_card(
        total_releases=len(all_releases),
        followed_count=len(user_follows),
        monitored_count=len(monitored),
        page=page,
        total_pages=total_pages,
    )
    keyboard = build_radar_home_keyboard(
        releases=slice_releases,
        page=page,
        total_pages=total_pages,
        followed_count=len(user_follows),
        web_app_url=settings.web_app_url,
    )
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "view"))
async def handle_radar_view_release(
    query: CallbackQuery,
    callback_data: RadarCallback,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    settings: AppSettings,
) -> None:
    """Inspect a single release with listening, following, and provider options."""
    await ack_callback(query)
    release = await governance_repository.get_discovered_release(callback_data.target_id)
    if not release:
        await ack_callback(query, "Release not found.", show_alert=True)
        return

    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    is_followed = await governance_repository.is_artist_followed(user_id, release.artist_name)
    prefs = await governance_repository.get_user_preferences(user_id)

    card = format_release_detail_card(release, is_followed)
    keyboard = build_release_detail_keyboard(
        release=release,
        is_followed=is_followed,
        web_app_url=settings.web_app_url,
        page=callback_data.page,
        platform_priority=prefs.platform_priority_list,
    )
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "preview"))
async def handle_radar_preview_audio(
    query: CallbackQuery,
    callback_data: RadarCallback,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
    settings: AppSettings,
    current_user: UserAccount | None = None,
) -> None:
    """Download and deliver the 30-second audio snippet directly into the chat."""
    release = await governance_repository.get_discovered_release(callback_data.target_id)
    if not release:
        await ack_callback(query, "Release not found.", show_alert=True)
        return

    preview_url = release.preview_url
    # If preview_url is missing, attempt to fetch it dynamically from provider
    if not preview_url:
        deezer_provider = getattr(radar_service, "_provider", None)
        if isinstance(deezer_provider, DeezerReleaseProvider):
            res = await deezer_provider.get_release_preview_track(release.provider_release_id)
            if res and res[1]:
                preview_url = res[1]

    if not preview_url:
        await ack_callback(query, "No audio preview available for this release.", show_alert=True)
        return

    await ack_callback(query, "🎧 Fetching 30s audio preview...")
    audio_bytes = await radar_service.get_preview_audio_bytes(preview_url)
    if not audio_bytes:
        await ack_callback(query, "Failed to load audio stream from provider.", show_alert=True)
        return

    if query.message is not None:
        filename = f"{release.artist_name[:20]}_{release.title[:20]}_preview.mp3"
        input_file = BufferedInputFile(audio_bytes, filename=filename)
        caption = (
            f"🎧 <b>30-Second Audio Preview</b>\n"
            f"🎤 <b>{html.escape(release.artist_name)}</b> — <b>{html.escape(release.title)}</b>\n"
            f"📅 Release Date: <code>{html.escape(release.release_date)}</code>"
        )
        user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
        prefs = await governance_repository.get_user_preferences(user_id)
        markup = build_preview_audio_keyboard(
            release,
            settings.web_app_url,
            platform_priority=prefs.platform_priority_list,
        )
        try:
            await query.message.answer_audio(
                audio=input_file,
                title=f"{release.title} (Preview)",
                performer=release.artist_name,
                caption=caption,
                reply_markup=markup,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Could not send audio preview message: %s", exc)
            await ack_callback(query, "Could not send audio file to Telegram.", show_alert=True)


@radar_router.callback_query(RadarCallback.filter(F.action.in_({"follow", "unfollow"})))
async def handle_radar_toggle_follow(
    query: CallbackQuery,
    callback_data: RadarCallback,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
    settings: AppSettings,
) -> None:
    """Follow or unfollow an artist from the release detail card."""
    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    target = callback_data.target_id.strip()
    rel = await governance_repository.get_discovered_release(target)
    artist_name = rel.artist_name if rel else target

    if callback_data.action == "follow":
        await radar_service.follow_artist(user_id, artist_name)
        await ack_callback(query, f"🔔 Now following {artist_name}!")
    else:
        await radar_service.unfollow_artist(user_id, artist_name)
        await ack_callback(query, f"🔕 Unfollowed {artist_name}.")

    # Refresh release card view
    if not rel:
        all_releases = await governance_repository.list_discovered_releases(artist_name=artist_name, limit=1)
        if all_releases:
            rel = all_releases[0]

    if rel:
        is_followed = callback_data.action == "follow"
        card = format_release_detail_card(rel, is_followed)
        prefs = await governance_repository.get_user_preferences(user_id)
        keyboard = build_release_detail_keyboard(
            rel,
            is_followed,
            settings.web_app_url,
            page=callback_data.page,
            platform_priority=prefs.platform_priority_list,
        )
        await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "follows_list"))
async def handle_radar_follows_list(
    query: CallbackQuery,
    callback_data: RadarCallback,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
) -> None:
    """Display paginated list of followed artists."""
    await ack_callback(query)
    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    user_follows = await governance_repository.list_user_followed_artists(user_id)

    if not user_follows:
        card = format_screen(
            title="🔔 Followed Artists",
            context_lines=[
                "You are not currently following any artists.",
                "",
                (
                    "<i>Browse releases in /radar or songs in /songs and tap 'Follow Artist' "
                    "to receive alerts!</i>"
                ),
            ],
            footer_lines=["<i>0 Follows</i> • <i>Radar Settings</i>"],
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="◀️ Back to Releases",
                callback_data=RadarCallback(action="page", page=1).pack(),
            )
        )
        await safe_edit_text(query.message, card, reply_markup=builder.as_markup())
        return

    card = format_screen(
        title=f"🔔 Followed Artists ({len(user_follows)})",
        context_lines=[
            "Below are all artists you are actively monitoring for new music drops.",
            "Tap <b>🔕 Unfollow</b> to stop receiving alerts for any artist.",
        ],
        footer_lines=[f"Total: {len(user_follows)} artists • <i>Radar Follows</i>"],
    )
    keyboard = build_followed_artists_keyboard(user_follows, page=callback_data.page)
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "unfollow_list"))
async def handle_radar_unfollow_from_list(
    query: CallbackQuery,
    callback_data: RadarCallback,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
) -> None:
    """Unfollow an artist from the followed list and refresh the screen."""
    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    artist_name = callback_data.target_id.strip()
    await radar_service.unfollow_artist(user_id, artist_name)
    await ack_callback(query, f"🔕 Unfollowed {artist_name}.")

    user_follows = await governance_repository.list_user_followed_artists(user_id)
    card = format_screen(
        title=f"🔔 Followed Artists ({len(user_follows)})",
        context_lines=[
            "Below are all artists you are actively monitoring for new music drops.",
            "Tap <b>🔕 Unfollow</b> to stop receiving alerts for any artist.",
        ],
        footer_lines=[f"Total: {len(user_follows)} artists • <i>Radar Follows</i>"],
    )
    keyboard = build_followed_artists_keyboard(user_follows, page=callback_data.page)
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "refresh"))
async def handle_radar_refresh(
    query: CallbackQuery,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
    settings: AppSettings,
) -> None:
    """Run an on-demand scan across all monitored artists and refresh the radar screen."""
    await ack_callback(query, "🔄 Scanning artists for new drops...")
    discovered, _ = await radar_service.scan_monitored_artists()
    await ack_callback(query, f"✅ Scan complete! Found {len(discovered)} recent releases.")

    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    all_releases = await governance_repository.list_discovered_releases(limit=60)
    user_follows = await governance_repository.list_user_followed_artists(user_id)
    monitored = await governance_repository.list_all_monitored_artists()

    total_pages = max(1, ceil(len(all_releases) / RELEASES_PAGE_SIZE))
    slice_releases = all_releases[:RELEASES_PAGE_SIZE]

    card = format_radar_home_card(
        total_releases=len(all_releases),
        followed_count=len(user_follows),
        monitored_count=len(monitored),
        page=1,
        total_pages=total_pages,
    )
    keyboard = build_radar_home_keyboard(
        releases=slice_releases,
        page=1,
        total_pages=total_pages,
        followed_count=len(user_follows),
        web_app_url=settings.web_app_url,
    )
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "album_tracks"))
async def handle_radar_album_tracks(
    query: CallbackQuery,
    callback_data: RadarCallback,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
) -> None:
    """Display paginated tracklist allowing user to preview individual tracks from an album."""
    await ack_callback(query, "🎵 Loading album tracks...")
    release = await governance_repository.get_discovered_release(callback_data.target_id)
    if not release:
        await ack_callback(query, "Release not found.", show_alert=True)
        return

    tracks = await radar_service.get_release_track_previews(release.provider_release_id)
    if not tracks:
        await ack_callback(query, "No preview tracks available for this album.", show_alert=True)
        return

    card = format_screen(
        title=f"💿 {release.title} — Track Previews",
        context_lines=[
            f"🎤 <b>Artist:</b> <code>{html.escape(release.artist_name)}</code>",
            f"💿 <b>Album:</b> <b>{html.escape(release.title)}</b>",
            f"🎵 <b>Available Previews:</b> {len(tracks)} tracks",
            "",
            "<i>Select any track below to listen to its 30-second audio preview directly in chat!</i>",
        ],
        footer_lines=[f"Album Previews ({len(tracks)} tracks) • <i>📡 Release Radar</i>"],
    )
    keyboard = build_album_tracks_keyboard(release, tracks, page=callback_data.page)
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "preview_track"))
async def handle_radar_preview_individual_track(
    query: CallbackQuery,
    callback_data: RadarCallback,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
    settings: AppSettings,
) -> None:
    """Download and deliver a specific album track 30-second preview snippet."""
    raw_target = callback_data.target_id
    if ":" in raw_target:
        parts = raw_target.rsplit(":", 1)
        rel_id = parts[0]
        track_id = parts[1]
    else:
        rel_id = raw_target
        track_id = ""

    release = await governance_repository.get_discovered_release(rel_id)
    if not release:
        await ack_callback(query, "Release not found.", show_alert=True)
        return

    tracks = await radar_service.get_release_track_previews(release.provider_release_id)
    target_track = next((t for t in tracks if str(t.track_id) == str(track_id)), None)
    preview_url = target_track.preview_url if target_track else release.preview_url
    track_title = target_track.title if target_track else release.title

    if not preview_url:
        await ack_callback(query, "Preview not available for this track.", show_alert=True)
        return

    await ack_callback(query, f"🎧 Loading preview: {track_title}...")
    audio_bytes = await radar_service.get_preview_audio_bytes(preview_url)
    if not audio_bytes:
        await ack_callback(query, "Failed to download preview audio stream.", show_alert=True)
        return

    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    prefs = await governance_repository.get_user_preferences(user_id)

    if query.message is not None:
        clean_track = "".join(c for c in track_title if c.isalnum() or c in " _-")[:20]
        filename = f"{release.artist_name[:15]}_{clean_track}_preview.mp3"
        input_file = BufferedInputFile(audio_bytes, filename=filename)
        caption = (
            f"🎧 <b>30-Second Audio Preview</b>\n"
            f"🎵 <b>{html.escape(track_title)}</b>\n"
            f"🎤 <b>{html.escape(release.artist_name)}</b> — 💿 <i>{html.escape(release.title)}</i>\n"
            f"📅 Release Date: <code>{html.escape(release.release_date)}</code>"
        )
        markup = build_preview_audio_keyboard(
            release,
            settings.web_app_url,
            platform_priority=prefs.platform_priority_list,
        )
        try:
            await query.message.answer_audio(
                audio=input_file,
                title=f"{track_title} (Preview)",
                performer=release.artist_name,
                caption=caption,
                reply_markup=markup,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Could not send individual audio preview: %s", exc)
            await ack_callback(query, "Could not send audio file to Telegram.", show_alert=True)


@radar_router.callback_query(RadarCallback.filter(F.action == "other_links"))
async def handle_radar_other_links(
    query: CallbackQuery,
    callback_data: RadarCallback,
    governance_repository: GovernanceRepositoryPort,
) -> None:
    """Show external links and copy buttons across all streaming platforms."""
    await ack_callback(query)
    release = await governance_repository.get_discovered_release(callback_data.target_id)
    if not release:
        await ack_callback(query, "Release not found.", show_alert=True)
        return

    links = build_streaming_platform_links(
        release.artist_name, release.title, deezer_url=release.external_url
    )
    card = format_screen(
        title=f"🌐 Streaming Links — {release.title}",
        context_lines=[
            f"🎤 <b>Artist:</b> <code>{html.escape(release.artist_name)}</code>",
            f"💿 <b>Release:</b> <b>{html.escape(release.title)}</b>",
            "",
            (
                "Tap any platform to open in web/app, or tap <b>📋 Copy</b> "
                "to copy its direct URL to your clipboard:"
            ),
        ],
        footer_lines=["External Streaming Links • <i>📡 Release Radar</i>"],
    )
    keyboard = build_other_platforms_keyboard(release, links, page=callback_data.page)
    await safe_edit_text(query.message, card, reply_markup=keyboard)
