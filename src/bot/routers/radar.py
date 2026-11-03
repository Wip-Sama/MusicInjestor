"""Interactive `/radar` router for release discovery, artist following, and audio previews."""

import html
import logging
from math import ceil

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.keyboards.radar import (
    RadarCallback,
    build_album_tracks_keyboard,
    build_artist_dossier_keyboard,
    build_followed_artists_keyboard,
    build_other_platforms_keyboard,
    build_preview_audio_keyboard,
    build_radar_home_keyboard,
    build_release_detail_keyboard,
    build_release_notification_keyboard,
)
from src.bot.states import RadarStates
from src.bot.ui import ack_callback, format_screen, safe_answer, safe_edit_text
from src.config.settings import AppSettings
from src.core.deezer import DeezerReleaseProvider
from src.core.models import DiscoveredRelease, LibraryTrackRecord, UserAccount
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


def format_release_notification_card(
    release: DiscoveredRelease,
    lib_track: LibraryTrackRecord | None = None,
) -> str:
    """Format the 3-tier HTML notification card for new music drops."""
    is_in_library = bool(lib_track or release.in_library)
    lib_badge = (
        "✅ <b>Available in Library (Full Track)</b>" if is_in_library else "🆕 <i>Not yet in Library</i>"
    )
    context = [
        f"🎤 <b>Artist:</b> <code>{html.escape(release.artist_name)}</code>",
        f"💿 <b>Release:</b> <b>{html.escape(release.title)}</b>",
        (
            f"🏷️ <b>Format:</b> <code>{html.escape(release.release_type.upper())}</code> "
            f"({release.track_count} tracks)"
        ),
        f"📅 <b>Date:</b> <code>{html.escape(release.release_date or 'Unknown')}</code>",
        f"📊 <b>Status:</b> {lib_badge}",
        "",
        (
            "<i>Tap below to stream the full track in bot or mini app, "
            "or listen to a 30-second audio preview!</i>"
            if is_in_library
            else "<i>Tap below to listen to a 30-second audio preview or view in /radar!</i>"
        ),
    ]
    return format_screen(
        title="🌟 New Music Drop Alert!",
        context_lines=context,
        footer_lines=[f"Provider: <code>{html.escape(release.provider)}</code> • <i>Radar Notification</i>"],
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


@radar_router.callback_query(RadarCallback.filter(F.action.in_({"page", "home"})))
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


@radar_router.callback_query(F.data.in_({"radar:page:1", "radar:home", "radar:list", "radar"}))
async def handle_radar_home_string_cb(
    query: CallbackQuery,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    settings: AppSettings,
) -> None:
    """Handle plain string navigation callbacks to the radar screen."""
    dummy_data = RadarCallback(action="page", page=1)
    await handle_radar_page(query, dummy_data, current_user, governance_repository, settings)


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
    lib_tracks = await governance_repository.find_library_tracks_for_release(
        release.artist_name, release.title
    )
    lib_track_id = lib_tracks[0].id if lib_tracks else release.library_track_id

    card = format_release_detail_card(release, is_followed)
    keyboard = build_release_detail_keyboard(
        release=release,
        is_followed=is_followed,
        web_app_url=settings.web_app_url,
        page=callback_data.page,
        platform_priority=prefs.platform_priority_list,
        lib_track_id=lib_track_id,
    )
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "play_full"))
async def handle_radar_play_full_track(
    query: CallbackQuery,
    callback_data: RadarCallback,
    governance_repository: GovernanceRepositoryPort,
    settings: AppSettings,
    current_user: UserAccount | None = None,
) -> None:
    """Stream/play complete track for releases in library rather than defaulting to preview snippet."""
    release = await governance_repository.get_discovered_release(callback_data.target_id)
    if not release:
        await ack_callback(query, "Release not found.", show_alert=True)
        return

    lib_tracks = await governance_repository.find_library_tracks_for_release(
        release.artist_name, release.title
    )
    if not lib_tracks:
        await ack_callback(query, "Full track not found in library.", show_alert=True)
        return

    track = lib_tracks[0]
    if not track.storage_path.is_file() and not track.telegram_file_id and track.source_message_id is None:
        await ack_callback(query, "Audio file is missing on disk. Run /rescan.", show_alert=True)
        return

    await ack_callback(query, f"▶️ Delivering complete track: {track.title}...")
    if query.message is not None:
        user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
        prefs = await governance_repository.get_user_preferences(user_id)
        caption = (
            f"🎵 <b>Complete Track (Library)</b>\n"
            f"🎤 <b>{html.escape(track.artist)}</b> — <b>{html.escape(track.title)}</b>\n"
            f"💿 Album: <code>{html.escape(track.album)}</code>"
        )
        markup = build_preview_audio_keyboard(
            release,
            settings.web_app_url,
            platform_priority=prefs.platform_priority_list,
            lib_track_id=track.id,
        )
        try:
            if track.telegram_file_id:
                await query.message.answer_audio(
                    track.telegram_file_id,
                    title=track.title,
                    performer=track.artist,
                    caption=caption,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            elif track.storage_path.is_file():
                await query.message.answer_audio(
                    FSInputFile(track.storage_path),
                    title=track.title,
                    performer=track.artist,
                    caption=caption,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            lrc_path = track.storage_path.with_suffix(".lrc")
            if lrc_path.is_file():
                await query.message.answer_document(
                    FSInputFile(lrc_path),
                    caption=(
                        "<b>📝 Synchronized Lyrics</b>\n"
                        "<blockquote>Sidecar <code>.lrc</code> file</blockquote>"
                    ),
                    parse_mode="HTML",
                )
        except Exception as exc:
            logger.warning("Could not deliver full track audio: %s", exc)
            await ack_callback(query, "Could not send audio file to Telegram.", show_alert=True)


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
        lib_tracks = await governance_repository.find_library_tracks_for_release(
            release.artist_name, release.title
        )
        lib_track_id = lib_tracks[0].id if lib_tracks else release.library_track_id
        markup = build_preview_audio_keyboard(
            release,
            settings.web_app_url,
            platform_priority=prefs.platform_priority_list,
            lib_track_id=lib_track_id,
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
        lib_tracks = await governance_repository.find_library_tracks_for_release(rel.artist_name, rel.title)
        lib_track_id = lib_tracks[0].id if lib_tracks else rel.library_track_id
        keyboard = build_release_detail_keyboard(
            rel,
            is_followed,
            settings.web_app_url,
            page=callback_data.page,
            platform_priority=prefs.platform_priority_list,
            lib_track_id=lib_track_id,
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
                text="➕ Follow New Artist",
                callback_data=RadarCallback(action="add_follow").pack(),
            ),
            InlineKeyboardButton(
                text="🧪 Test Alert",
                callback_data=RadarCallback(action="test_alert").pack(),
            ),
        )
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
                text="➕ Follow New Artist",
                callback_data=RadarCallback(action="add_follow").pack(),
            ),
            InlineKeyboardButton(
                text="🧪 Test Alert",
                callback_data=RadarCallback(action="test_alert").pack(),
            ),
        )
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


def format_artist_dossier_card(
    artist_name: str,
    is_followed: bool,
    library_tracks_count: int,
    releases_count: int,
    library_tracks: list[LibraryTrackRecord],
    releases: list[DiscoveredRelease],
) -> str:
    """Format full intelligence dossier card for a specific artist."""
    follow_status = "🔔 Following (Alerts Active)" if is_followed else "🔕 Not Followed"
    context: list[str] = [
        f"• <b>Artist Name:</b> <code>{html.escape(artist_name)}</code>",
        f"• <b>Follow Status:</b> <code>{follow_status}</code>",
        f"• <b>Songs in Library:</b> <code>{library_tracks_count}</code> tracks",
        f"• <b>Radar Drops Discovered:</b> <code>{releases_count}</code> releases",
        "",
    ]
    if library_tracks:
        context.append("<b>🎵 Top Library Tracks:</b>")
        for trk in library_tracks[:4]:
            yr = f" ({trk.year})" if trk.year else ""
            context.append(f"• <b>{html.escape(trk.title)}</b> — <i>{html.escape(trk.album)}</i>{yr}")
        context.append("")
    else:
        context.append("<i>No songs currently stored in library for this artist.</i>")
        context.append("")

    if releases:
        context.append("<b>📡 Recent &amp; Upcoming Radar Drops:</b>")
        for rel in releases[:4]:
            t_count = f"{rel.track_count} tracks" if rel.track_count > 1 else "Single"
            context.append(
                f"• <b>{html.escape(rel.title)}</b> [{rel.release_date or 'Recent'}] — <i>{t_count}</i>"
            )
    else:
        context.append("<i>No external releases currently cached for this artist.</i>")

    return format_screen(
        title=f"🎤 Artist Dossier — {html.escape(artist_name)}",
        context_lines=context,
        footer_lines=[f"<i>🧭 Home › Radar › {html.escape(artist_name)}</i>"],
    )


@radar_router.callback_query(RadarCallback.filter(F.action.in_({"artist_releases", "artist_dossier"})))
async def handle_radar_artist_dossier(
    query: CallbackQuery,
    callback_data: RadarCallback,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    settings: AppSettings,
) -> None:
    """Display comprehensive dossier for an artist (library tracks, radar drops, follow state)."""
    await ack_callback(query)
    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    artist_name = callback_data.target_id.strip()
    if not artist_name:
        await ack_callback(query, "Artist name missing.", show_alert=True)
        return

    is_followed = await governance_repository.is_artist_followed(user_id, artist_name)
    lib_tracks = await governance_repository.list_library_tracks_by_artist(artist_name, limit=20)
    releases = await governance_repository.list_discovered_releases(artist_name=artist_name, limit=20)
    prefs = await governance_repository.get_user_preferences(user_id)

    card = format_artist_dossier_card(
        artist_name=artist_name,
        is_followed=is_followed,
        library_tracks_count=len(lib_tracks),
        releases_count=len(releases),
        library_tracks=lib_tracks,
        releases=releases,
    )
    keyboard = build_artist_dossier_keyboard(
        artist_name=artist_name,
        is_followed=is_followed,
        library_tracks=lib_tracks,
        releases=releases,
        web_app_url=settings.web_app_url,
        platform_priority=prefs.platform_priority_list,
        back_callback="radar~follows_list~~1",
    )
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action.in_({"follow_artist", "unfollow_artist"})))
async def handle_radar_toggle_follow_artist(
    query: CallbackQuery,
    callback_data: RadarCallback,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
    settings: AppSettings,
) -> None:
    """Toggle follow status from the artist dossier and re-render."""
    user_id = current_user.telegram_id if current_user else (query.from_user.id if query.from_user else 0)
    artist_name = callback_data.target_id.strip()
    if not artist_name:
        return

    is_following = callback_data.action == "follow_artist"
    if is_following:
        await radar_service.follow_artist(user_id, artist_name)
        await ack_callback(query, f"🔔 Now following {artist_name}!")
    else:
        await radar_service.unfollow_artist(user_id, artist_name)
        await ack_callback(query, f"🔕 Unfollowed {artist_name}.")

    is_followed = is_following
    lib_tracks = await governance_repository.list_library_tracks_by_artist(artist_name, limit=20)
    releases = await governance_repository.list_discovered_releases(artist_name=artist_name, limit=20)
    prefs = await governance_repository.get_user_preferences(user_id)

    card = format_artist_dossier_card(
        artist_name=artist_name,
        is_followed=is_followed,
        library_tracks_count=len(lib_tracks),
        releases_count=len(releases),
        library_tracks=lib_tracks,
        releases=releases,
    )
    keyboard = build_artist_dossier_keyboard(
        artist_name=artist_name,
        is_followed=is_followed,
        library_tracks=lib_tracks,
        releases=releases,
        web_app_url=settings.web_app_url,
        platform_priority=prefs.platform_priority_list,
        back_callback="radar~follows_list~~1",
    )
    await safe_edit_text(query.message, card, reply_markup=keyboard)


@radar_router.callback_query(RadarCallback.filter(F.action == "add_follow"))
async def handle_radar_add_follow_prompt(
    query: CallbackQuery,
    state: FSMContext,
) -> None:
    """Prompt user to type the name of an artist to monitor for new drops."""
    await ack_callback(query)
    await state.set_state(RadarStates.awaiting_artist_to_follow)
    card = format_screen(
        title="➕ Follow Artist for Release Alerts",
        context_lines=[
            "Please send the <b>Artist Name</b> you would like to follow.",
            "",
            "<i>Examples:</i> <code>YOASOBI</code>, <code>Taylor Swift</code>, <code>Fujii Kaze</code>",
            "",
            "💡 <i>You will receive automatic notification alerts whenever they release new music!</i>",
        ],
        footer_lines=["<i>Type an artist name below, or /cancel to abort.</i>"],
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="◀️ Cancel",
            callback_data=RadarCallback(action="follows_list", page=1).pack(),
        )
    )
    await safe_edit_text(query.message, card, reply_markup=builder.as_markup())


@radar_router.message(RadarStates.awaiting_artist_to_follow, F.text)
async def handle_radar_artist_name_input(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
    settings: AppSettings,
) -> None:
    """Process user text input for following a new artist."""
    raw_name = (message.text or "").strip()
    await state.clear()

    if not raw_name or raw_name.startswith("/"):
        return

    user_id = current_user.telegram_id if current_user else (message.from_user.id if message.from_user else 0)
    followed = await radar_service.follow_artist(user_id, raw_name)
    artist_name = followed.artist_name

    lib_tracks = await governance_repository.list_library_tracks_by_artist(artist_name, limit=20)
    releases = await governance_repository.list_discovered_releases(artist_name=artist_name, limit=20)
    prefs = await governance_repository.get_user_preferences(user_id)

    card = format_artist_dossier_card(
        artist_name=artist_name,
        is_followed=True,
        library_tracks_count=len(lib_tracks),
        releases_count=len(releases),
        library_tracks=lib_tracks,
        releases=releases,
    )
    keyboard = build_artist_dossier_keyboard(
        artist_name=artist_name,
        is_followed=True,
        library_tracks=lib_tracks,
        releases=releases,
        web_app_url=settings.web_app_url,
        platform_priority=prefs.platform_priority_list,
        back_callback="radar~follows_list~~1",
    )
    await safe_answer(message, card, reply_markup=keyboard)


@radar_router.message(Command("follow"))
async def handle_cmd_follow(
    message: Message,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
    settings: AppSettings,
) -> None:
    """Follow an artist via `/follow <artist_name>`."""
    raw = (message.text or "").strip()
    artist_name = raw.removeprefix("/follow").strip()
    if not artist_name:
        await safe_answer(
            message,
            format_screen(
                title="🔔 Follow Artist",
                context_lines=[
                    "Please specify the artist name to follow.",
                    "<b>Usage:</b> <code>/follow &lt;Artist Name&gt;</code>",
                ],
                footer_lines=["<i>🧭 Home › Radar › Follow</i>"],
            ),
        )
        return

    user_id = current_user.telegram_id if current_user else (message.from_user.id if message.from_user else 0)
    await radar_service.follow_artist(user_id, artist_name)
    lib_tracks = await governance_repository.list_library_tracks_by_artist(artist_name, limit=20)
    releases = await governance_repository.list_discovered_releases(artist_name=artist_name, limit=20)
    prefs = await governance_repository.get_user_preferences(user_id)

    card = format_artist_dossier_card(
        artist_name=artist_name,
        is_followed=True,
        library_tracks_count=len(lib_tracks),
        releases_count=len(releases),
        library_tracks=lib_tracks,
        releases=releases,
    )
    keyboard = build_artist_dossier_keyboard(
        artist_name=artist_name,
        is_followed=True,
        library_tracks=lib_tracks,
        releases=releases,
        web_app_url=settings.web_app_url,
        platform_priority=prefs.platform_priority_list,
    )
    await safe_answer(message, card, reply_markup=keyboard)


@radar_router.message(Command("unfollow"))
async def handle_cmd_unfollow(
    message: Message,
    current_user: UserAccount | None,
    radar_service: ReleaseRadarService,
) -> None:
    """Unfollow an artist via `/unfollow <artist_name>`."""
    raw = (message.text or "").strip()
    artist_name = raw.removeprefix("/unfollow").strip()
    if not artist_name:
        await safe_answer(
            message,
            format_screen(
                title="🔕 Unfollow Artist",
                context_lines=[
                    "Please specify the artist name to unfollow.",
                    "<b>Usage:</b> <code>/unfollow &lt;Artist Name&gt;</code>",
                ],
                footer_lines=["<i>🧭 Home › Radar › Unfollow</i>"],
            ),
        )
        return

    user_id = current_user.telegram_id if current_user else (message.from_user.id if message.from_user else 0)
    await radar_service.unfollow_artist(user_id, artist_name)
    await safe_answer(
        message,
        format_screen(
            title="🔕 Artist Unfollowed",
            context_lines=[
                f"You have stopped monitoring <code>{html.escape(artist_name)}</code> for new music drops."
            ],
            footer_lines=["<i>🧭 Home › Radar</i>"],
        ),
    )


@radar_router.callback_query(RadarCallback.filter(F.action == "test_alert"))
@radar_router.message(Command("radar_test"))
async def handle_radar_test_alert(
    event: CallbackQuery | Message,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    settings: AppSettings,
) -> None:
    """Deliver a test release notification card into the chat to verify alert delivery."""
    user_id = current_user.telegram_id if current_user else (event.from_user.id if event.from_user else 0)
    prefs = await governance_repository.get_user_preferences(user_id)

    if isinstance(event, CallbackQuery):
        await ack_callback(event, "🧪 Dispatching test release drop notification...")

    # Find an actual release or generate a representative sample
    sample_release = None
    all_releases = await governance_repository.list_discovered_releases(limit=5)
    if all_releases:
        sample_release = all_releases[0]
    else:
        sample_release = DiscoveredRelease(
            id="test:yoasobi-idol",
            artist_name="YOASOBI",
            title="Idol (Sample Drop)",
            release_type="single",
            release_date="2026-10-28",
            cover_url=None,
            preview_url=None,
            external_url="https://www.deezer.com",
            provider="deezer",
            track_count=1,
            in_library=False,
        )

    matching_tracks = await governance_repository.find_library_tracks_for_release(
        sample_release.artist_name, sample_release.title
    )
    lib_track = matching_tracks[0] if matching_tracks else None

    notif_card = format_release_notification_card(sample_release, lib_track)
    notif_card = f"🧪 <b>[TEST NOTIFICATION]</b>\n{notif_card}"
    if not prefs.notifications_enabled:
        notif_card += (
            "\n\n⚠️ <i>Note: Release Notifications are currently <b>OFF</b> in your /settings. "
            "Turn them ON to receive real-time alerts when drops happen!</i>"
        )

    keyboard = build_release_notification_keyboard(
        release=sample_release,
        lib_track=lib_track,
        web_app_url=settings.web_app_url,
        platform_priority=prefs.platform_priority_list,
    )

    bot = event.bot
    if bot is not None:
        await bot.send_message(
            chat_id=user_id,
            text=notif_card,
            reply_markup=keyboard,
            parse_mode="HTML",
        )


@radar_router.callback_query(RadarCallback.filter(F.action == "refresh"))
async def handle_radar_refresh(
    query: CallbackQuery,
    current_user: UserAccount | None,
    governance_repository: GovernanceRepositoryPort,
    radar_service: ReleaseRadarService,
    settings: AppSettings,
) -> None:
    """Run an on-demand scan across all monitored artists and refresh the radar screen."""
    if radar_service.is_scanning:
        prog = radar_service.progress
        msg = (
            f"⏳ Radar scan already running ({prog.processed_artists}/{prog.total_artists} artists). "
            "Please wait!"
        )
        await ack_callback(
            query,
            msg,
            show_alert=True,
        )
        return

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
    lib_tracks = await governance_repository.find_library_tracks_for_release(
        release.artist_name, release.title
    )
    lib_track_id = lib_tracks[0].id if lib_tracks else release.library_track_id

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
            lib_track_id=lib_track_id,
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
