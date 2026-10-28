"""Unified search and album gap analysis router for Telegram chat interactions."""

import html
import logging
import re

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from src.bot.keyboards.admin import (
    RESERVED_MENU_BUTTONS,
    build_guest_onboarding_keyboard,
)
from src.bot.keyboards.search import (
    AlbumGapCallback,
    SearchActionCallback,
    build_album_gap_keyboard,
    build_search_results_keyboard,
)
from src.bot.ui import ack_callback, format_screen, resolve_oidc_url, safe_answer, safe_edit_text
from src.config.settings import AppSettings
from src.core.audit import format_guest_access_denied_card
from src.core.models import AlbumGapAnalysis, UnifiedSearchResult, UserAccount, UserRole
from src.core.oidc import AuthentikOidcService
from src.core.ports import CatalogRepositoryPort, GovernanceRepositoryPort
from src.core.radar import ReleaseRadarService
from src.core.search import AlbumSearchService

logger = logging.getLogger(__name__)

search_router = Router(name="search")

_URL_PATTERN = re.compile(
    r"(?:https?:\/\/|www\.)[^\s/$.?#].[^\s]*",
    re.IGNORECASE,
)


def format_search_results_card(result: UnifiedSearchResult) -> str:
    """Format unified search results screen presenting albums and tracks."""
    scope_desc = "🌐 Global (Deezer + Library)" if result.scope == "global" else "📚 Library Only"
    context: list[str] = [
        f"• <b>Search Scope:</b> <code>{scope_desc}</code>",
        "",
    ]
    if result.albums:
        context.append("<b>💿 Matched Albums:</b>")
        for alb in result.albums[:5]:
            coverage_tag = f"[{alb.owned_tracks_count}/{alb.total_tracks} owned]"
            context.append(
                f"• <b>{html.escape(alb.title)}</b> — {html.escape(alb.artist)} <code>{coverage_tag}</code>"
            )
        context.append("")

    if result.tracks:
        context.append("<b>🎵 Matched Tracks:</b>")
        for trk in result.tracks[:6]:
            if trk.in_library:
                status_tag = "✅ In Library"
            elif trk.preview_url:
                status_tag = "🎧 30s Preview"
            else:
                status_tag = "Missing"
            artist_prefix = f"{html.escape(trk.artist)} — " if trk.artist else ""
            context.append(f"• {artist_prefix}<b>{html.escape(trk.title)}</b> [<code>{status_tag}</code>]")

    if not result.albums and not result.tracks:
        context.append("<i>No matching albums or tracks were found.</i>")
        if result.scope == "library":
            context.append("💡 <i>Switch to Global Search below to query external music catalogs!</i>")

    return format_screen(
        title=f"🔍 Search: {html.escape(result.query)}",
        context_lines=context,
        footer_lines=["<i>🧭 Home › Search</i>"],
    )


def format_album_gap_card(gap: AlbumGapAnalysis) -> str:
    """Format detailed album overview with track-by-track library possession analysis."""
    cov_pct = gap.coverage_percent
    cov_str = f"{gap.owned_tracks_count}/{gap.total_tracks} tracks ({cov_pct}%)"
    context: list[str] = [
        f"• <b>Artist:</b> <code>{html.escape(gap.artist)}</code>",
        f"• <b>Album:</b> <code>{html.escape(gap.title)}</code>",
        f"• <b>Library Coverage:</b> <code>{cov_str}</code>",
        f"• <b>Missing Tracks:</b> <code>{gap.missing_tracks_count}</code>",
        "",
        "<b>Tracklist Overview:</b>",
    ]
    for trk in gap.tracks[:15]:
        status_icon = "✅" if trk.is_owned else "❌"
        status_note = (
            "In Library" if trk.is_owned else "Missing (Preview Available)" if trk.preview_url else "Missing"
        )
        context.append(
            f"{status_icon} <b>#{trk.position}</b> {html.escape(trk.title)} — <i>{status_note}</i>"
        )
    if len(gap.tracks) > 15:
        context.append(f"<i>... and {len(gap.tracks) - 15} more tracks</i>")

    return format_screen(
        title="💿 Album Overview &amp; Gap Analysis",
        context_lines=context,
        footer_lines=["<i>🧭 Home › Search › Album Gap Analysis</i>"],
    )


@search_router.message(
    StateFilter(None),
    F.text,
)
async def handle_chat_text_message(
    message: Message,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    album_search_service: AlbumSearchService | None = None,
    oidc_service: AuthentikOidcService | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Process incoming chat text: recognize external URLs or execute unified album/track search."""
    text = (message.text or "").strip()
    if not text or text.startswith("/") or text in RESERVED_MENU_BUTTONS:
        return

    if current_user is not None and current_user.role == UserRole.GUEST:
        oidc_url = resolve_oidc_url(current_user, oidc_service, settings)
        await safe_answer(
            message,
            format_guest_access_denied_card(current_user.telegram_id),
            reply_markup=build_guest_onboarding_keyboard(
                oidc_url,
                telegram_id=current_user.telegram_id,
            ),
        )
        return

    # 1. URL / Link recognition
    url_match = _URL_PATTERN.search(text)
    if url_match:
        detected_url = url_match.group(0)
        card = format_screen(
            title="🔗 External Link Recognized",
            context_lines=[
                f"• <b>Detected Link:</b> <code>{html.escape(detected_url)}</code>",
                "",
                "<i>Direct streaming or downloading from external web links is not yet supported.</i>",
                (
                    "💡 <i>To ingest music into the library, please upload audio files (.mp3, .flac) "
                    "or archives (.zip, .7z) directly to the chat!</i>"
                ),
            ],
            footer_lines=["<i>🧭 Home › Link Ingestion Notice</i>"],
        )
        await safe_answer(message, card)
        return

    # 2. Treat plain text as search query
    if governance_repository is None or current_user is None:
        return

    if album_search_service is None:
        album_search_service = AlbumSearchService(
            catalog_repository=governance_repository, radar_provider=None
        )

    prefs = await governance_repository.get_user_preferences(current_user.telegram_id)
    scope = prefs.search_scope or "global"
    result = await album_search_service.search(query=text, scope=scope)

    web_url = settings.web_app_url.strip() if settings and settings.web_app_url else None
    card = format_search_results_card(result)
    markup = build_search_results_keyboard(result, web_app_url=web_url)
    await safe_answer(message, card, reply_markup=markup)


@search_router.callback_query(AlbumGapCallback.filter(F.action == "view"))
async def handle_album_gap_view(
    query: CallbackQuery,
    callback_data: AlbumGapCallback,
    album_search_service: AlbumSearchService | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Render album gap analysis overview displaying owned vs missing track coverage."""
    if album_search_service is None or query.message is None:
        await ack_callback(query)
        return

    alb_id = callback_data.album_id
    provider_id: str | None = None
    art_name = ""
    alb_name = ""
    if alb_id.startswith("deezer:"):
        provider_id = alb_id.replace("deezer:", "", 1)
    elif alb_id.startswith("local:"):
        parts = alb_id.split(":", 2)
        if len(parts) >= 3:
            art_name, alb_name = parts[1], parts[2]

    await ack_callback(query, "Analyzing album coverage...")
    gap = await album_search_service.analyze_album_gap(
        artist=art_name,
        album=alb_name,
        provider_release_id=provider_id,
    )

    web_url = settings.web_app_url.strip() if settings and settings.web_app_url else None
    card = format_album_gap_card(gap)
    markup = build_album_gap_keyboard(gap, web_app_url=web_url)
    await safe_edit_text(query.message, card, reply_markup=markup)


@search_router.callback_query(AlbumGapCallback.filter(F.action == "preview"))
async def handle_album_gap_preview(
    query: CallbackQuery,
    callback_data: AlbumGapCallback,
    album_search_service: AlbumSearchService | None = None,
    radar_service: ReleaseRadarService | None = None,
) -> None:
    """Download and deliver 30-second audio preview for a missing track in an album gap analysis."""
    if album_search_service is None or radar_service is None or query.message is None:
        await ack_callback(query)
        return

    alb_id = callback_data.album_id
    target_pos = callback_data.track_pos
    provider_id = alb_id.replace("deezer:", "", 1) if alb_id.startswith("deezer:") else None

    gap = await album_search_service.analyze_album_gap(
        artist="",
        album="",
        provider_release_id=provider_id,
    )
    target_track = next((t for t in gap.tracks if t.position == target_pos), None)
    if not target_track or not target_track.preview_url:
        await ack_callback(query, "No audio preview snippet available for this track.", show_alert=True)
        return

    await ack_callback(query, f"🎧 Loading preview #{target_pos}: {target_track.title[:20]}...")
    audio_bytes = await radar_service.get_preview_audio_bytes(target_track.preview_url)
    if not audio_bytes:
        await ack_callback(query, "Failed to download preview audio snippet.", show_alert=True)
        return

    filename = f"preview_{gap.artist[:10]}_{target_track.title[:15]}.mp3"
    try:
        await query.message.answer_audio(
            BufferedInputFile(audio_bytes, filename=filename),
            caption=(
                f"🎧 <b>30-Second Preview:</b> {html.escape(target_track.title)}\n"
                f"👤 <b>Artist:</b> {html.escape(gap.artist)}\n"
                f"💿 <b>Album:</b> {html.escape(gap.title)}"
            ),
            performer=gap.artist,
            title=f"{target_track.title} (Preview)",
            duration=30,
        )
    except Exception as exc:
        logger.warning("Could not deliver album gap audio preview: %s", exc)


@search_router.callback_query(SearchActionCallback.filter(F.action == "scope"))
async def handle_search_scope_toggle(
    query: CallbackQuery,
    callback_data: SearchActionCallback,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    album_search_service: AlbumSearchService | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Toggle search scope between library and global and re-render results in-place."""
    if (
        query.message is None
        or current_user is None
        or governance_repository is None
        or album_search_service is None
    ):
        await ack_callback(query)
        return

    new_scope = callback_data.value or "global"
    prefs = await governance_repository.get_user_preferences(current_user.telegram_id)
    prefs.search_scope = new_scope
    await governance_repository.update_user_preferences(prefs)

    await ack_callback(query, f"Switched scope to {new_scope.capitalize()}!")
    result = await album_search_service.search(query=callback_data.query, scope=new_scope)

    web_url = settings.web_app_url.strip() if settings and settings.web_app_url else None
    card = format_search_results_card(result)
    markup = build_search_results_keyboard(result, web_app_url=web_url)
    await safe_edit_text(query.message, card, reply_markup=markup)


@search_router.callback_query(SearchActionCallback.filter(F.action == "preview"))
async def handle_search_track_preview(
    query: CallbackQuery,
    callback_data: SearchActionCallback,
    radar_service: ReleaseRadarService | None = None,
    catalog_repository: CatalogRepositoryPort | None = None,
) -> None:
    """Download and deliver 30-second audio preview from search results."""
    if radar_service is None or query.message is None:
        await ack_callback(query)
        return

    track_id = callback_data.value
    # If it is a Deezer preview ID or direct preview
    target_id = track_id.replace("preview:", "", 1)
    # Fetch preview url
    preview_url = None
    if hasattr(radar_service, "_provider"):
        try:
            tracks = await radar_service._provider.search_tracks(callback_data.query, limit=10)
            matched = next((t for t in tracks if t.track_id == target_id), None)
            if matched:
                preview_url = matched.preview_url
        except Exception as exc:
            logger.warning("Failed to lookup preview URL for %s: %s", target_id, exc)

    if not preview_url:
        await ack_callback(query, "Preview URL not found.", show_alert=True)
        return

    await ack_callback(query, "🎧 Loading 30s preview...")
    audio_bytes = await radar_service.get_preview_audio_bytes(preview_url)
    if not audio_bytes:
        await ack_callback(query, "Failed to download preview audio stream.", show_alert=True)
        return

    filename = f"search_preview_{target_id}.mp3"
    try:
        caption_query = html.escape(callback_data.query)
        await query.message.answer_audio(
            BufferedInputFile(audio_bytes, filename=filename),
            caption=f"🎧 <b>30-Second Audio Preview</b>\n<i>From search: {caption_query}</i>",
            title="Audio Preview (30s)",
            duration=30,
        )
    except Exception as exc:
        logger.warning("Failed to deliver search audio preview: %s", exc)
