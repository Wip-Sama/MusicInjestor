"""Interactive `/songs` & `/rescan` router for searching, editing, exporting, and reconciling tracks."""

import html
import inspect
import json
import logging
from pathlib import Path

import httpx
from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message

from src.bot.api_server import resolve_local_api_file_path
from src.bot.keyboards.admin import (
    BTN_MAIN_SONGS,
    RESERVED_MENU_BUTTONS,
    build_queued_job_keyboard,
)
from src.bot.keyboards.songs import (
    build_dry_run_confirmation_keyboard,
    build_input_cancel_keyboard,
    build_metadata_field_keyboard,
    build_rescan_confirmation_keyboard,
    build_research_mode_keyboard,
    build_search_prompt_keyboard,
    build_song_action_keyboard,
    build_song_history_keyboard,
    build_songs_search_menu_keyboard,
)
from src.bot.states import SongManagementStates
from src.bot.ui import (
    ack_callback,
    format_loading_card,
    format_screen,
    resolve_back_callback,
    safe_edit_text,
    send_chat_action_safe,
)
from src.config.settings import AppSettings
from src.core.audit import format_job_inspector_card
from src.core.extractor import SUPPORTED_ARCHIVE_EXTENSIONS, SUPPORTED_AUDIO_EXTENSIONS
from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import (
    MusicBrainzMetadataService,
    normalize_primary_artist,
    parse_manual_search_query,
)
from src.core.models import (
    AuditLogEntry,
    LibraryRescanReport,
    LibraryTrackRecord,
    LyricsData,
    Permission,
    TrackMetadata,
    UserAccount,
    UserRole,
)
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.ports import CatalogRepositoryPort, GovernanceRepositoryPort
from src.core.rbac import has_permission

logger = logging.getLogger(__name__)

songs_router = Router(name="songs")

_INGESTION_MEDIA_EXTENSIONS: frozenset[str] = SUPPORTED_AUDIO_EXTENSIONS | SUPPORTED_ARCHIVE_EXTENSIONS


def _is_ingestion_media_message(message: Message) -> bool:
    """Return True if `message` carries an audio track or archive intended for the ingestion pipeline."""
    if message.audio is not None:
        return True
    if message.document is not None:
        file_name = (getattr(message.document, "file_name", None) or "").lower()
        if file_name and Path(file_name).suffix.lower() in _INGESTION_MEDIA_EXTENSIONS:
            return True
        mime = (getattr(message.document, "mime_type", None) or "").lower()
        if mime.startswith("audio/") or mime in {
            "application/zip",
            "application/x-7z-compressed",
            "application/x-zip-compressed",
        }:
            return True
    return False


def _is_non_command_text(message: Message) -> bool:
    """Return True if `message` is plain user text (not a slash command, menu button, or media attachment)."""
    if _is_ingestion_media_message(message) or message.document is not None or message.photo is not None:
        return False
    text = (message.text or "").strip()
    return bool(text) and not text.startswith("/") and text not in RESERVED_MENU_BUTTONS


def _is_non_media_input_message(message: Message) -> bool:
    """Return True if `message` is a valid song-editor input (text, photo, or non-audio/archive document)."""
    if _is_ingestion_media_message(message):
        return False
    text = (message.text or "").strip()
    if text.startswith("/") or text in RESERVED_MENU_BUTTONS:
        return False
    return bool(text) or message.document is not None or bool(message.photo)


async def _reset_to_search_state(
    state: FSMContext,
    *,
    active_song_id: str | None = None,
) -> None:
    """Reset any pending edit/import/cover/lyrics FSM prompt back to `awaiting_search_query`."""
    await state.update_data(
        active_song_id=active_song_id,
        active_meta_field=None,
        pending_song_proposal=None,
    )
    await state.set_state(SongManagementStates.awaiting_search_query)


def _resolve_organizer(
    pipeline: IngestionPipeline | None,
    settings: AppSettings | None,
) -> LibraryOrganizer:
    """Obtain the active `LibraryOrganizer` from `pipeline` or construct a default instance."""
    organizer_attr = getattr(pipeline, "_organizer", None)
    if isinstance(organizer_attr, LibraryOrganizer):
        return organizer_attr
    lib_dir = settings.storage_library_dir if settings is not None else Path("./storage/library")
    return LibraryOrganizer(library_root=lib_dir, enable_network=True)


def _resolve_metadata_service(
    pipeline: IngestionPipeline | None,
    settings: AppSettings | None,
) -> MusicBrainzMetadataService:
    """Obtain the active `MusicBrainzMetadataService` from `pipeline` or construct one."""
    meta_attr = getattr(pipeline, "_metadata_provider", None)
    if isinstance(meta_attr, MusicBrainzMetadataService):
        return meta_attr
    if settings is not None:
        return MusicBrainzMetadataService(
            app_name=settings.musicbrainz_app_name,
            app_version=settings.musicbrainz_app_version,
            contact=settings.musicbrainz_contact,
            genius_access_token=settings.genius_access_token,
            lastfm_api_key=settings.lastfm_api_key,
            enable_network=True,
        )
    return MusicBrainzMetadataService(enable_network=False)


def _resolve_lyrics_client(
    pipeline: IngestionPipeline | None,
    settings: AppSettings | None,
) -> LrclibLyricsClient:
    """Obtain the active `LrclibLyricsClient` from `pipeline` or construct one."""
    lyrics_attr = getattr(pipeline, "_lyrics_provider", None)
    if isinstance(lyrics_attr, LrclibLyricsClient):
        return lyrics_attr
    base_url = settings.lrclib_base_url if settings is not None else "https://lrclib.net/api"
    return LrclibLyricsClient(base_url=base_url)


def _resolve_uploaded_file_path(file_path: str | Path, settings: AppSettings | None) -> Path:
    """Resolve a Telegram Bot API file path using `settings` when available."""
    if settings is not None:
        return resolve_local_api_file_path(file_path, settings)
    return Path(file_path)


def _can_access_songs(user: UserAccount | None) -> bool:
    """Return True if `user` is permitted to browse and manage library songs."""
    if user is None:
        return True
    if user.is_blocked or user.role == UserRole.GUEST:
        return False
    return has_permission(user, Permission.SUBMIT_INGESTION)


async def _resolve_song_provenance_labels(
    record: LibraryTrackRecord,
    governance_repository: GovernanceRepositoryPort,
    catalog_repository: CatalogRepositoryPort | None = None,
) -> tuple[str | None, str | None]:
    """Resolve human-readable `@username (id)` labels for `uploaded_by` and `modified_by`."""
    uploader_id = record.uploaded_by
    if uploader_id <= 0 and record.job_id and catalog_repository is not None:
        job = await catalog_repository.get_job(record.job_id)
        if job is not None and job.user_id:
            uploader_id = job.user_id

    uploader_label: str | None = None
    if uploader_id > 0:
        user_obj = await governance_repository.get_user(uploader_id)
        if user_obj is not None:
            uploader_label = f"{user_obj.mention_handle} ({user_obj.telegram_id})"
        else:
            uploader_label = f"User {uploader_id}"

    modifier_label: str | None = None
    if record.modified_by and record.modified_by > 0:
        mod_obj = await governance_repository.get_user(record.modified_by)
        if mod_obj is not None:
            modifier_label = f"{mod_obj.mention_handle} ({mod_obj.telegram_id})"
        else:
            modifier_label = f"User {record.modified_by}"

    return uploader_label, modifier_label


def format_song_detail_card(
    record: LibraryTrackRecord,
    *,
    has_lrc_file: bool,
    has_cover_file: bool,
    changes_applied: bool = False,
    uploader_label: str | None = None,
    modifier_label: str | None = None,
) -> str:
    """Render a 3-tier HTML summary card for a library song with provenance and edit history."""
    year_str = str(record.year) if record.year else "Unknown"
    trk_str = f"{record.track_number:02d}" if record.track_number else "01"
    mins, secs = divmod(int(round(record.duration_seconds)), 60)
    dur_str = f"{mins}:{secs:02d}"
    if record.has_synced_lyrics and has_lrc_file:
        lyrics_badge = "✅ Synced (.lrc + embedded)"
    elif has_lrc_file or record.has_synced_lyrics:
        lyrics_badge = "📄 Present (.lrc / embedded)"
    else:
        lyrics_badge = "❌ Missing"
    cover_badge = "✅ cover.jpg present" if has_cover_file else "❌ Missing"

    resolved_uploader = (
        uploader_label
        if uploader_label
        else (f"User {record.uploaded_by}" if record.uploaded_by > 0 else "Library Rescan / System")
    )
    ingested_ts = record.ingested_at.strftime("%Y-%m-%d %H:%M UTC")
    if record.modification_count > 0 or record.updated_at is not None:
        mod_count = max(1, record.modification_count)
        resolved_modifier = (
            modifier_label
            if modifier_label
            else (f"User {record.modified_by}" if record.modified_by else "Editor")
        )
        updated_ts = (
            record.updated_at.strftime("%Y-%m-%d %H:%M UTC") if record.updated_at is not None else "Recently"
        )
        mod_line = (
            f"• Modified: <code>{mod_count} time(s)</code> • "
            f"Last by <code>{html.escape(resolved_modifier)}</code> on <code>{updated_ts}</code>"
        )
    else:
        mod_line = "• Modified: <code>Never (0 edits)</code>"

    title = (
        "✅ Changes Applied Successfully — Song Details &amp; Actions"
        if changes_applied
        else "🎵 Song Details &amp; Actions"
    )
    return format_screen(
        title=title,
        context_lines=[
            f"• Title: <code>{html.escape(record.title)}</code>",
            f"• Artist: <code>{html.escape(record.artist)}</code>",
            f"• Album: <code>{html.escape(record.album)}</code> (<code>{year_str}</code>)",
            f"• Track #: <code>{trk_str}</code> • Duration: <code>{dur_str}</code>",
            f"• Lyrics: {lyrics_badge}",
            f"• Cover Art: {cover_badge}",
            f"• Added By: <code>{html.escape(resolved_uploader)}</code> on <code>{ingested_ts}</code>",
            mod_line,
            f"• Path: <code>{html.escape(str(record.storage_path))}</code>",
        ],
        footer_lines=[
            f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • <i>🧭 Home › Songs › Details</i>"
        ],
    )


def format_song_history_card(
    record: LibraryTrackRecord,
    entries: list[AuditLogEntry],
    actor_labels: dict[int, str],
    uploader_label: str | None = None,
) -> str:
    """Render a chronological provenance and modification history card for a song."""
    resolved_uploader = (
        uploader_label
        if uploader_label
        else (f"User {record.uploaded_by}" if record.uploaded_by > 0 else "Library Rescan / System")
    )
    ingested_ts = record.ingested_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    context_lines: list[str] = [
        f"• Song: <b>{html.escape(record.artist)} — {html.escape(record.title)}</b>",
        f"• Added By: <code>{html.escape(resolved_uploader)}</code>",
        f"• Added Date: <code>{ingested_ts}</code>",
        f"• Total Modifications: <code>{record.modification_count}</code>",
    ]
    if record.updated_at is not None:
        mod_actor = actor_labels.get(record.modified_by or 0) or (
            f"User {record.modified_by}" if record.modified_by else "Editor"
        )
        context_lines.append(
            f"• Last Modified: <code>{record.updated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</code> "
            f"by <code>{html.escape(mod_actor)}</code>"
        )
    if record.job_id:
        context_lines.append(f"• Source Job ID: <code>{html.escape(record.job_id)}</code>")

    if entries:
        context_lines.append("📜 Chronological Event Log (Newest First):")
        for entry in entries[:15]:
            ts = entry.created_at.strftime("%Y-%m-%d %H:%M UTC")
            actor_str = actor_labels.get(entry.actor_id, f"User {entry.actor_id}")
            action_str = str(getattr(entry.action, "value", entry.action))
            detail_parts: list[str] = []
            if entry.details:
                for k in ("action_type", "title", "artist", "album", "year"):
                    if k in entry.details and entry.details[k] is not None:
                        detail_parts.append(f"{k}={entry.details[k]}")
            detail_suffix = f" ({html.escape(', '.join(detail_parts))})" if detail_parts else ""
            context_lines.append(
                f"  • <code>[{ts}]</code> <b>{html.escape(action_str)}</b> "
                f"by <code>{html.escape(actor_str)}</code>{detail_suffix}"
            )
    else:
        context_lines.append("• No subsequent edit log entries recorded yet.")

    return format_screen(
        title="📜 Song Provenance &amp; Edit History",
        context_lines=context_lines,
        footer_lines=[
            f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
            "<i>🧭 Home › Songs › Details › History</i>"
        ],
        expandable=len(entries) > 3,
    )


def format_song_edit_dry_run_card(
    record: LibraryTrackRecord,
    proposal: dict[str, object],
    organizer: LibraryOrganizer,
) -> str:
    """Render a 3-tier HTML Dry-Run Preview Report comparing current song state against `proposal`."""
    action_type = str(proposal.get("action_type") or "metadata").upper()
    new_title = str(proposal.get("new_title") or record.title)
    new_artist = str(proposal.get("new_artist") or record.artist)
    new_album = str(proposal.get("new_album") or record.album)
    raw_year = proposal.get("new_year")
    new_year = int(str(raw_year)) if raw_year is not None and str(raw_year).isdigit() else record.year
    raw_trk = proposal.get("new_track_number")
    new_trk = (
        int(str(raw_trk)) if raw_trk is not None and str(raw_trk).isdigit() else (record.track_number or 1)
    )

    target_meta = TrackMetadata(
        title=new_title,
        artist=new_artist,
        album=new_album,
        album_artist=new_artist,
        year=new_year,
        track_number=new_trk,
        duration_seconds=int(round(record.duration_seconds)),
    )
    suffix = record.storage_path.suffix or ".flac"
    new_path = organizer.build_target_audio_path(target_meta, suffix)

    context_lines: list[str] = [
        "Please review what is going to change before applying:",
    ]

    def _diff_line(label: str, old_val: object, new_val: object) -> str:
        old_s = html.escape(str(old_val if old_val is not None else "None"))
        new_s = html.escape(str(new_val if new_val is not None else "None"))
        if old_s != new_s:
            return f"• {label}: <s>{old_s}</s> ➡️ <code>{new_s}</code>"
        return f"• {label}: <code>{old_s}</code> <i>(unchanged)</i>"

    context_lines.append(_diff_line("Title", record.title, new_title))
    context_lines.append(_diff_line("Artist", record.artist, new_artist))
    context_lines.append(_diff_line("Album", record.album, new_album))
    context_lines.append(_diff_line("Year", record.year, new_year))
    context_lines.append(_diff_line("Track #", record.track_number or 1, new_trk))

    old_path_str = str(record.storage_path)
    new_path_str = str(new_path)
    if old_path_str.replace("\\", "/").lower() != new_path_str.replace("\\", "/").lower():
        context_lines.append(
            f"• File Location:\n"
            f"  <code>{html.escape(old_path_str)}</code>\n"
            f"  ➡️ <code>{html.escape(new_path_str)}</code>"
        )
    else:
        context_lines.append(f"• File Location: <code>{html.escape(old_path_str)}</code> <i>(in-place)</i>")

    new_lyrics = proposal.get("new_lyrics")
    if isinstance(new_lyrics, str) and new_lyrics.strip():
        line_count = len(new_lyrics.strip().splitlines())
        is_synced = "[" in new_lyrics and "]" in new_lyrics
        kind = "Synced LRC" if is_synced else "Plain Text"
        context_lines.append(
            f"• Lyrics: ➡️ Update (<code>{kind}, {line_count} lines</code> — "
            "embedded in audio + <code>.lrc</code> sidecar)"
        )

    cover_size = proposal.get("new_cover_size")
    if cover_size is not None:
        kb = max(1, int(str(cover_size)) // 1024)
        context_lines.append(
            f"• Cover Art: ➡️ Update <code>cover.jpg</code> &amp; embedded picture (<code>{kb} KB</code>)"
        )

    return format_screen(
        title=f"🧪 Dry-Run Preview ({html.escape(action_type)})",
        context_lines=context_lines,
        footer_lines=[
            f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
            "<i>Confirm &amp; Apply Changes or Edit Again below</i>"
        ],
    )


def format_rescan_dry_run_card(report: LibraryRescanReport, applied: bool = False) -> str:
    """Render a 3-tier HTML summary or Dry-Run Preview Report for a `storage/library` filesystem scan."""
    title = "✅ Library Rescan &amp; Sync Complete" if applied else "🧪 Library Rescan Dry-Run Preview"
    context_lines: list[str] = [
        f"• Unchanged Tracks: <code>{report.unchanged_count}</code>",
        f"• Added on Disk: <code>{len(report.added_tracks)}</code>",
        f"• Modified / Moved on Disk: <code>{len(report.updated_tracks)}</code>",
        f"• Removed from Disk: <code>{len(report.removed_tracks)}</code>",
    ]

    if report.added_tracks:
        context_lines.append("➕ Added Tracks:")
        for rec in report.added_tracks[:8]:
            context_lines.append(
                f"  • {html.escape(rec.artist)} - {html.escape(rec.title)} "
                f"(<code>{html.escape(rec.storage_path.name)}</code>)"
            )

    if report.updated_tracks:
        context_lines.append("🔄 Modified / Moved Tracks:")
        for rec in report.updated_tracks[:8]:
            context_lines.append(
                f"  • {html.escape(rec.artist)} - {html.escape(rec.title)} "
                f"➡️ <code>{html.escape(str(rec.storage_path))}</code>"
            )

    if report.removed_tracks:
        context_lines.append("🗑️ Missing Files (Will Remove from DB):")
        for rec in report.removed_tracks[:8]:
            context_lines.append(
                f"  • {html.escape(rec.artist)} - {html.escape(rec.title)} "
                f"(<code>{html.escape(str(rec.storage_path))}</code>)"
            )

    if not applied and report.has_changes:
        footers = ["<i>Confirm below to synchronize the SQLite database with the filesystem.</i>"]
    elif not report.has_changes:
        footers = ["<i>✨ Filesystem and SQLite database are completely in sync.</i>"]
    else:
        footers = ["<i>🧭 Home › Songs › Rescan</i>"]

    return format_screen(
        title=title,
        context_lines=context_lines,
        footer_lines=footers,
        expandable=bool(report.added_tracks or report.updated_tracks or report.removed_tracks),
    )


def _parse_metadata_edit_input(
    raw_text: str,
    target_field: str,
    record: LibraryTrackRecord,
) -> dict[str, object]:
    """Parse single-field or multi-field (`artist=... | title=... | album=... | year=...`) user input."""
    cleaned = raw_text.strip()
    proposal: dict[str, object] = {
        "track_id": record.id,
        "action_type": "metadata",
        "new_title": record.title,
        "new_artist": record.artist,
        "new_album": record.album,
        "new_year": record.year,
        "new_track_number": record.track_number or 1,
    }

    if target_field == "title":
        proposal["new_title"] = cleaned
        return proposal
    if target_field == "artist":
        proposal["new_artist"] = normalize_primary_artist(cleaned)
        return proposal
    if target_field == "album":
        proposal["new_album"] = cleaned
        return proposal
    if target_field == "year":
        digits = "".join(ch for ch in cleaned if ch.isdigit())
        if len(digits) == 4:
            proposal["new_year"] = int(digits)
        return proposal
    if target_field == "track":
        digits = "".join(ch for ch in cleaned if ch.isdigit())
        if digits:
            proposal["new_track_number"] = int(digits)
        return proposal

    # Multi-field parsing (`artist: ... | title: ... | album: ... | year: ... | track: ...`)
    segments = [seg.strip() for seg in cleaned.replace("\n", "|").split("|") if seg.strip()]
    matched_kv = False
    for seg in segments:
        sep = "=" if "=" in seg else (":" if ":" in seg else None)
        if sep is not None:
            k, v = seg.split(sep, maxsplit=1)
            key = k.strip().lower()
            val = v.strip()
            if not val:
                continue
            matched_kv = True
            if key in {"title", "t", "name", "song"}:
                proposal["new_title"] = val
            elif key in {"artist", "a", "singer"}:
                proposal["new_artist"] = normalize_primary_artist(val)
            elif key in {"album", "b", "release"}:
                proposal["new_album"] = val
            elif key in {"year", "date", "y"} and val.isdigit():
                proposal["new_year"] = int(val)
            elif key in {"track", "trk", "number", "n"} and val.isdigit():
                proposal["new_track_number"] = int(val)

    if not matched_kv:
        if " - " in cleaned:
            art_part, title_part = cleaned.split(" - ", maxsplit=1)
            if art_part.strip():
                proposal["new_artist"] = normalize_primary_artist(art_part.strip())
            if title_part.strip():
                proposal["new_title"] = title_part.strip()
        else:
            proposal["new_title"] = cleaned

    return proposal


@songs_router.message(Command("songs"))
@songs_router.message(F.text == BTN_MAIN_SONGS)
async def handle_songs_command(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
) -> None:
    """Open the interactive `/songs` search & library management menu."""
    if not _can_access_songs(current_user):
        await message.answer(
            format_screen(
                title="⛔ Permission Denied",
                context_lines=["You do not have permission to browse or edit the music library."],
                footer_lines=["<i>🧭 Home › Songs</i>"],
            )
        )
        return
    if governance_repository is None:
        await message.answer(
            format_screen(
                title="⚠️ Library Unavailable",
                context_lines=["Library repository is not configured."],
                footer_lines=["<i>🧭 Home › Songs</i>"],
            )
        )
        return

    raw_text = (message.text or "").strip()
    query = ""
    if raw_text.lower().startswith("/songs"):
        parts = raw_text.split(maxsplit=1)
        if len(parts) == 2:
            query = parts[1].strip()

    tracks = (
        await governance_repository.search_library_tracks(query, limit=15)
        if query
        else await governance_repository.list_library_tracks(limit=15)
    )
    await _reset_to_search_state(state)
    back_cb = await resolve_back_callback(
        state,
        None,
        "songs:list",
        default_back="menu:home",
        reset_to_home=True,
    )

    title = (
        f"🎵 Library Search Results for {html.escape(query)} ({len(tracks)})"
        if query
        else f"🎵 Music Library Browser ({len(tracks)} track(s))"
    )
    await message.answer(
        format_screen(
            title=title,
            context_lines=[
                "• Tap a song below to open its management menu "
                "(download, edit metadata/cover/lyrics, re-search, or export/import).",
                "• Or type a song title, artist, or album right now to filter the list.",
            ],
            footer_lines=["<i>🧭 Home › Songs</i>"],
        ),
        reply_markup=build_songs_search_menu_keyboard(
            tracks,
            can_rescan=True,
            is_filtered=bool(query),
            back_callback=back_cb,
        ),
    )


@songs_router.message(Command("rescan"))
async def handle_rescan_command(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Run a dry-run filesystem scan of `storage/library` and prompt to synchronize SQLite."""
    if not _can_access_songs(current_user):
        await message.answer(
            format_screen(
                title="⛔ Permission Denied",
                context_lines=["You do not have permission to rescan the library."],
                footer_lines=["<i>🧭 Home › Songs › Rescan</i>"],
            )
        )
        return
    if governance_repository is None:
        await message.answer(
            format_screen(
                title="⚠️ Library Unavailable",
                context_lines=["Library repository is not configured."],
                footer_lines=["<i>🧭 Home › Songs › Rescan</i>"],
            )
        )
        return

    await send_chat_action_safe(message)
    organizer = _resolve_organizer(pipeline, settings)
    if organizer.is_reconciling:
        await message.answer(
            format_screen(
                title="🔒 Library Sync In Progress",
                context_lines=[
                    "A background library reconciliation is currently running on the server.",
                    f"<b>Status:</b> {html.escape(organizer.reconciliation_status)}",
                    "Please wait for background synchronization to finish before starting a new scan.",
                ],
                footer_lines=["<i>🧭 Home › Songs › Rescan</i>"],
            )
        )
        return

    existing = await governance_repository.list_library_tracks(limit=10000)
    report = await organizer.scan_library_filesystem(existing)
    if not report.has_changes:
        await _reset_to_search_state(state)
        await message.answer(
            format_rescan_dry_run_card(report, applied=True),
            reply_markup=build_songs_search_menu_keyboard(existing[:12], can_rescan=True),
        )
        return

    await state.update_data(pending_song_proposal=None, active_meta_field=None)
    await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
    await message.answer(
        format_rescan_dry_run_card(report, applied=False),
        reply_markup=build_rescan_confirmation_keyboard(),
    )


async def _deliver_track_audio(
    msg: Message,
    trk: LibraryTrackRecord,
    governance_repository: GovernanceRepositoryPort,
    *,
    caption: str | None = None,
) -> None:
    """Deliver a track's audio via Telegram message forwarding, cached `file_id`, or `FSInputFile` upload."""
    bot = getattr(msg, "bot", None)
    chat_id = getattr(getattr(msg, "chat", None), "id", None)
    if (
        trk.source_chat_id is not None
        and trk.source_message_id is not None
        and bot is not None
        and isinstance(chat_id, int)
    ):
        forward_fn = getattr(bot, "forward_message", None)
        if callable(forward_fn):
            try:
                forwarded = forward_fn(
                    chat_id=chat_id,
                    from_chat_id=trk.source_chat_id,
                    message_id=trk.source_message_id,
                )
                if inspect.isawaitable(forwarded):
                    await forwarded
                return
            except Exception as exc:
                logger.debug("Forwarding track %s failed, falling back to audio send: %s", trk.id, exc)

    if trk.telegram_file_id:
        try:
            if caption is not None:
                await msg.answer_audio(
                    trk.telegram_file_id,
                    title=trk.title,
                    performer=trk.artist,
                    caption=caption,
                )
            else:
                await msg.answer_audio(
                    trk.telegram_file_id,
                    title=trk.title,
                    performer=trk.artist,
                )
            return
        except Exception as exc:
            logger.debug("Cached file_id send for %s failed, uploading from disk: %s", trk.id, exc)

    if caption is not None:
        sent = await msg.answer_audio(
            FSInputFile(trk.storage_path),
            title=trk.title,
            performer=trk.artist,
            caption=caption,
        )
    else:
        sent = await msg.answer_audio(
            FSInputFile(trk.storage_path),
            title=trk.title,
            performer=trk.artist,
        )
    file_id = getattr(getattr(sent, "audio", None), "file_id", None)
    sent_chat_id = getattr(getattr(sent, "chat", None), "id", chat_id)
    sent_msg_id = getattr(sent, "message_id", None)
    if isinstance(file_id, str) or isinstance(sent_msg_id, int):
        await governance_repository.cache_track_telegram_file(
            trk.id,
            telegram_file_id=file_id if isinstance(file_id, str) else None,
            source_chat_id=sent_chat_id if isinstance(sent_chat_id, int) else None,
            source_message_id=sent_msg_id if isinstance(sent_msg_id, int) else None,
        )


@songs_router.message(
    SongManagementStates.awaiting_search_query,
    _is_non_command_text,
)
async def handle_songs_search_text(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    catalog_repository: CatalogRepositoryPort | None = None,
) -> None:
    """Filter library songs (or inspect a pasted Job ID / Track ID) in `awaiting_search_query` state."""
    text = (message.text or "").strip()
    if not text or text in RESERVED_MENU_BUTTONS:
        return
    if not _can_access_songs(current_user) or governance_repository is None:
        return

    # Check if the user pasted a Job ID first when no song title matches
    tracks = await governance_repository.search_library_tracks(text, limit=15)
    await _reset_to_search_state(state)
    if not tracks:
        direct_track = await governance_repository.get_library_track(text)
        if direct_track is not None:
            await _reset_to_search_state(state, active_song_id=direct_track.id)
            song_screen = f"songs:view:{direct_track.id[:12]}"
            back_cb = await resolve_back_callback(
                state,
                None,
                song_screen,
                default_back="songs:list",
            )
            has_lrc = direct_track.storage_path.with_suffix(".lrc").is_file()
            has_cover = (direct_track.storage_path.parent / "cover.jpg").is_file()
            up_lbl, mod_lbl = await _resolve_song_provenance_labels(
                direct_track,
                governance_repository,
                catalog_repository,
            )
            await message.answer(
                format_song_detail_card(
                    direct_track,
                    has_lrc_file=has_lrc,
                    has_cover_file=has_cover,
                    uploader_label=up_lbl,
                    modifier_label=mod_lbl,
                ),
                reply_markup=build_song_action_keyboard(direct_track.id, back_callback=back_cb),
            )
            return

        if catalog_repository is not None and len(text) >= 6:
            matched_job = await catalog_repository.get_job(text)
            if matched_job is not None:
                job_screen = f"queue:view:{matched_job.job_id}"
                back_cb = await resolve_back_callback(
                    state,
                    None,
                    job_screen,
                    default_back="songs:list",
                )
                uploader_lbl: str | None = None
                if matched_job.user_id:
                    u_obj = await governance_repository.get_user(matched_job.user_id)
                    uploader_lbl = (
                        f"{u_obj.mention_handle} ({u_obj.telegram_id})"
                        if u_obj is not None
                        else f"User {matched_job.user_id}"
                    )
                await message.answer(
                    format_job_inspector_card(matched_job, uploader_label=uploader_lbl),
                    reply_markup=build_queued_job_keyboard(
                        matched_job.job_id,
                        state=matched_job.state,
                        back_callback=back_cb,
                        include_home=True,
                    ),
                )
                return

        await resolve_back_callback(state, None, "songs:list", default_back="menu:home")
        all_tracks = await governance_repository.list_library_tracks(limit=10)
        await message.answer(
            format_screen(
                title="🔍 No Matching Songs Found",
                context_lines=[
                    f"No songs found matching <code>{html.escape(text)}</code>.",
                    "Try another search term or pick from recent tracks below.",
                ],
                footer_lines=["<i>🧭 Home › Songs › Search</i>"],
            ),
            reply_markup=build_songs_search_menu_keyboard(
                all_tracks,
                can_rescan=True,
                is_filtered=True,
            ),
        )
        return

    await resolve_back_callback(state, None, "songs:list", default_back="menu:home")
    await message.answer(
        format_screen(
            title=f"🎵 Search Results for {html.escape(text)} ({len(tracks)})",
            context_lines=["Tap a song to manage it, or type another search query."],
            footer_lines=[
                f"<i>Query:</i> <code>{html.escape(text)}</code> • <i>🧭 Home › Songs › Search</i>"
            ],
        ),
        reply_markup=build_songs_search_menu_keyboard(
            tracks,
            can_rescan=True,
            is_filtered=True,
        ),
    )


@songs_router.callback_query(F.data.startswith("songs:"))
async def handle_songs_callback(
    callback: CallbackQuery,
    state: FSMContext,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    catalog_repository: CatalogRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Handle all inline button actions inside the `/songs` interactive menu in-place."""
    if not _can_access_songs(current_user):
        await ack_callback(callback, "Permission denied.", show_alert=True)
        return
    if governance_repository is None:
        await ack_callback(callback, "Repository unavailable.", show_alert=True)
        return

    raw_data = callback.data or ""
    parts = raw_data.split(":")
    if len(parts) < 2:
        await ack_callback(callback, "Invalid action.", show_alert=True)
        return

    action = parts[1]
    msg = callback.message
    organizer = _resolve_organizer(pipeline, settings)

    mutation_actions = {
        "rescan_dryrun",
        "rescan_apply",
        "edit_meta",
        "field",
        "edit_cover",
        "edit_lyrics",
        "auto_enrich",
        "import",
    }
    if action in mutation_actions and organizer.is_reconciling:
        await ack_callback(
            callback,
            "🔒 Library sync in progress. Track modifications and rescans are temporarily frozen.",
            show_alert=True,
        )
        return

    # 1. List / Back to Songs Menu
    if action == "list":
        await ack_callback(callback)
        tracks = await governance_repository.list_library_tracks(limit=15)
        await _reset_to_search_state(state)
        back_cb = await resolve_back_callback(
            state,
            msg,
            "songs:list",
            default_back="menu:home",
        )
        text = format_screen(
            title=f"🎵 Music Library Browser ({len(tracks)} track(s))",
            context_lines=["Tap a song below or type a song title, artist, or album to search."],
            footer_lines=["<i>🧭 Home › Songs</i>"],
        )
        await safe_edit_text(
            msg,
            text,
            reply_markup=build_songs_search_menu_keyboard(
                tracks,
                can_rescan=True,
                back_callback=back_cb,
            ),
        )
        return

    if action == "search_prompt":
        await ack_callback(callback)
        await _reset_to_search_state(state)
        await safe_edit_text(
            msg,
            format_screen(
                title="🔎 Search Music Library",
                context_lines=["Type a song title, artist, or album name in chat to filter the library."],
                footer_lines=["<i>🧭 Home › Songs › Search</i>"],
            ),
            reply_markup=build_search_prompt_keyboard(),
        )
        return

    # 2. Export entire library JSON (permanent artifact)
    if action == "export_all":
        await ack_callback(callback, "Exported library JSON.")
        await send_chat_action_safe(callback, ChatAction.UPLOAD_DOCUMENT)
        await _reset_to_search_state(state)
        tracks = await governance_repository.list_library_tracks(limit=1000)
        bundles = [await organizer.export_song_bundle(t) for t in tracks]
        json_bytes = json.dumps(bundles, indent=2, ensure_ascii=False).encode("utf-8")
        if isinstance(msg, Message):
            await msg.answer_document(
                BufferedInputFile(json_bytes, filename="library_songs_export.json"),
                caption=(
                    f"<b>📤 Library Export Complete</b>\n"
                    f"<blockquote>Exported <code>{len(bundles)}</code> song(s) from the library.</blockquote>"
                ),
            )
        return

    # 3. Rescan dry-run & apply
    if action == "rescan_dryrun":
        await ack_callback(callback)
        await send_chat_action_safe(callback)
        await safe_edit_text(
            msg,
            format_loading_card(
                "Scanning Library Folder",
                "Inspecting audio files, sidecar lyrics, and cover art on disk...",
                "🧭 Home › Songs › Rescan",
            ),
        )
        existing = await governance_repository.list_library_tracks(limit=10000)
        report = await organizer.scan_library_filesystem(existing)
        if report.has_changes:
            await state.update_data(pending_song_proposal=None, active_meta_field=None)
            await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
            await safe_edit_text(
                msg,
                format_rescan_dry_run_card(report, applied=False),
                reply_markup=build_rescan_confirmation_keyboard(),
            )
        else:
            await _reset_to_search_state(state)
            back_cb = await resolve_back_callback(
                state,
                msg,
                "songs:list",
                default_back="menu:home",
            )
            await safe_edit_text(
                msg,
                format_rescan_dry_run_card(report, applied=True),
                reply_markup=build_songs_search_menu_keyboard(
                    existing[:12],
                    can_rescan=True,
                    back_callback=back_cb,
                ),
            )
        return

    if action == "rescan_apply":
        await ack_callback(callback, "Database synchronized with library folder.")
        await send_chat_action_safe(callback)
        await safe_edit_text(
            msg,
            format_loading_card(
                "Synchronizing Library Database",
                "Applying filesystem changes to SQLite...",
                "🧭 Home › Songs › Rescan",
            ),
        )
        existing = await governance_repository.list_library_tracks(limit=10000)
        report = await organizer.scan_library_filesystem(existing)
        await organizer.apply_rescan_report(report, governance_repository)
        actor_id = current_user.telegram_id if current_user is not None else 0
        await governance_repository.append_audit_log(
            AuditLogEntry(
                actor_id=actor_id,
                action="LIBRARY_RESCANNED",
                target_entity="storage/library",
                details={
                    "added": len(report.added_tracks),
                    "updated": len(report.updated_tracks),
                    "removed": len(report.removed_tracks),
                },
            )
        )
        updated_list = await governance_repository.list_library_tracks(limit=15)
        await _reset_to_search_state(state)
        back_cb = await resolve_back_callback(
            state,
            msg,
            "songs:list",
            default_back="menu:home",
        )
        await safe_edit_text(
            msg,
            format_rescan_dry_run_card(report, applied=True),
            reply_markup=build_songs_search_menu_keyboard(
                updated_list,
                can_rescan=True,
                back_callback=back_cb,
            ),
        )
        return

    # Actions requiring a `track_id` in `parts[2]`
    if len(parts) < 3:
        await ack_callback(callback, "Missing track ID.", show_alert=True)
        return

    track_id_prefix = parts[2]
    record = await governance_repository.get_library_track(track_id_prefix)
    if record is None:
        await ack_callback(callback, "Song not found in database.", show_alert=True)
        return

    short_sid = record.id[:12]
    song_screen = f"songs:view:{short_sid}"

    # 4. View single song card
    if action == "view":
        await ack_callback(callback)
        await _reset_to_search_state(state, active_song_id=record.id)
        back_cb = await resolve_back_callback(
            state,
            msg,
            song_screen,
            default_back="songs:list",
        )
        has_lrc = record.storage_path.with_suffix(".lrc").is_file()
        has_cover = (record.storage_path.parent / "cover.jpg").is_file()
        up_lbl, mod_lbl = await _resolve_song_provenance_labels(
            record,
            governance_repository,
            catalog_repository,
        )
        card = format_song_detail_card(
            record,
            has_lrc_file=has_lrc,
            has_cover_file=has_cover,
            uploader_label=up_lbl,
            modifier_label=mod_lbl,
        )
        await safe_edit_text(
            msg,
            card,
            reply_markup=build_song_action_keyboard(record.id, back_callback=back_cb),
        )
        return

    # 4b. View chronological song provenance & modification history
    if action == "history":
        await ack_callback(callback)
        await _reset_to_search_state(state, active_song_id=record.id)
        await resolve_back_callback(
            state,
            msg,
            f"songs:history:{short_sid}",
            default_back=song_screen,
        )
        up_lbl, _ = await _resolve_song_provenance_labels(
            record,
            governance_repository,
            catalog_repository,
        )
        track_logs = await governance_repository.list_entity_audit_logs(record.id, limit=30)
        if record.job_id:
            job_logs = await governance_repository.list_entity_audit_logs(
                f"job:{record.job_id}",
                limit=10,
            )
            combined_by_key = {
                (e.created_at, e.actor_id, str(e.action), e.target_entity): e
                for e in [*track_logs, *job_logs]
            }
            track_logs = sorted(
                combined_by_key.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )
        actor_labels: dict[int, str] = {}
        actor_ids = {e.actor_id for e in track_logs if e.actor_id > 0}
        if record.modified_by and record.modified_by > 0:
            actor_ids.add(record.modified_by)
        for aid in actor_ids:
            u = await governance_repository.get_user(aid)
            actor_labels[aid] = f"{u.mention_handle} ({u.telegram_id})" if u is not None else f"User {aid}"
        await safe_edit_text(
            msg,
            format_song_history_card(
                record,
                track_logs,
                actor_labels,
                uploader_label=up_lbl,
            ),
            reply_markup=build_song_history_keyboard(record.id),
        )
        return

    # 5. Retrieve / Download Song File (permanent artifact, forwarded or cached file_id when available)
    if action == "get_track":
        await _reset_to_search_state(state, active_song_id=record.id)
        if (
            not record.storage_path.is_file()
            and not record.telegram_file_id
            and record.source_message_id is None
        ):
            await ack_callback(callback, "Audio file is missing on disk. Run Rescan.", show_alert=True)
            return
        await ack_callback(callback, "Sent song file.")
        await send_chat_action_safe(callback, ChatAction.UPLOAD_VOICE)
        if isinstance(msg, Message):
            await _deliver_track_audio(
                msg,
                record,
                governance_repository,
                caption=(
                    f"<b>🎵 {html.escape(record.artist)} - {html.escape(record.title)}</b>\n"
                    f"<blockquote>Album: <code>{html.escape(record.album)}</code></blockquote>"
                ),
            )
            lrc_path = record.storage_path.with_suffix(".lrc")
            if lrc_path.is_file():
                await msg.answer_document(
                    FSInputFile(lrc_path),
                    caption=(
                        "<b>📝 Synchronized Lyrics</b>\n"
                        "<blockquote>Sidecar <code>.lrc</code> file</blockquote>"
                    ),
                )
        return

    # 6. Retrieve / Download Full Album (permanent artifact)
    if action == "get_album":
        await _reset_to_search_state(state, active_song_id=record.id)
        album_tracks = await governance_repository.list_album_tracks(record.artist, record.album)
        if not album_tracks:
            album_tracks = [record]
        available_tracks = [
            trk
            for trk in album_tracks
            if trk.storage_path.is_file() or trk.telegram_file_id or trk.source_message_id is not None
        ]
        if not available_tracks:
            await ack_callback(callback, "Album audio files are missing on disk.", show_alert=True)
            return
        await ack_callback(callback, "Sent album tracks.")
        await send_chat_action_safe(callback, ChatAction.UPLOAD_VOICE)
        if isinstance(msg, Message):
            cover_path = record.storage_path.parent / "cover.jpg"
            if cover_path.is_file():
                await msg.answer_photo(
                    FSInputFile(cover_path),
                    caption=(
                        f"<b>💿 {html.escape(record.artist)} - {html.escape(record.album)}</b>\n"
                        f"<blockquote><code>{len(available_tracks)}</code> track(s)</blockquote>"
                    ),
                )
            for trk in available_tracks:
                await _deliver_track_audio(msg, trk, governance_repository)
        return

    # 7. Open Metadata Field Picker
    if action == "edit_meta":
        await ack_callback(callback)
        await state.update_data(
            active_song_id=record.id,
            active_meta_field="multi",
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_metadata_input)
        prompt = format_screen(
            title=f"✏️ Edit Metadata — {html.escape(record.artist)} - {html.escape(record.title)}",
            context_lines=[
                f"• Current Title: <code>{html.escape(record.title)}</code>",
                f"• Current Artist: <code>{html.escape(record.artist)}</code>",
                f"• Current Album: <code>{html.escape(record.album)}</code>",
                f"• Current Year: <code>{record.year or 'Unknown'}</code>",
                f"• Current Track #: <code>{record.track_number or 1}</code>",
                "Tap a field button below, or send a multi-field message:",
                "<code>artist: Saori Hayami | title: Orange Mint | album: Orange Mint | year: 2015</code>",
            ],
            footer_lines=[
                f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
                "<i>🧭 Home › Songs › Edit Metadata</i>"
            ],
        )
        await safe_edit_text(
            msg,
            prompt,
            reply_markup=build_metadata_field_keyboard(record.id),
        )
        return

    if action == "field" and len(parts) >= 4:
        await ack_callback(callback)
        field_name = parts[3]
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=field_name,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_metadata_input)
        field_labels = {
            "title": ("Title", record.title),
            "artist": ("Artist (Primary Singer)", record.artist),
            "album": ("Album", record.album),
            "year": ("Release Year (YYYY)", str(record.year or "")),
            "track": ("Track Number", str(record.track_number or 1)),
            "multi": (
                "Multiple Fields (artist: ... | title: ... | album: ... | year: ...)",
                f"artist: {record.artist} | title: {record.title} | album: {record.album}",
            ),
        }
        label, current_val = field_labels.get(field_name, ("Value", record.title))
        await safe_edit_text(
            msg,
            format_screen(
                title=f"✏️ Enter New {html.escape(label)}",
                context_lines=[
                    f"Current value: <code>{html.escape(current_val)}</code>",
                    "Reply with the new value below (a dry-run preview will be shown before saving).",
                ],
                footer_lines=[
                    f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
                    "<i>🧭 Home › Songs › Edit Metadata</i>"
                ],
            ),
            reply_markup=build_metadata_field_keyboard(record.id),
        )
        return

    # 8. Edit Cover Art Prompt
    if action == "edit_cover":
        await ack_callback(callback)
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=None,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_cover_input)
        await safe_edit_text(
            msg,
            format_screen(
                title=f"🖼️ Edit Cover Art — {html.escape(record.artist)} - {html.escape(record.title)}",
                context_lines=[
                    "Send a Photo, an image Document (<code>.jpg</code> / <code>.png</code>), "
                    "or paste a direct Image URL (<code>https://...</code>).",
                    "A dry-run confirmation preview will be shown before <code>cover.jpg</code> "
                    "and embedded audio tags are updated.",
                ],
                footer_lines=[
                    f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
                    "<i>🧭 Home › Songs › Edit Cover</i>"
                ],
            ),
            reply_markup=build_input_cancel_keyboard(record.id),
        )
        return

    # 9. Edit Lyrics Prompt
    if action == "edit_lyrics":
        await ack_callback(callback)
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=None,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_lyrics_input)
        existing_lyrics = await organizer.read_track_lyrics(record.storage_path)
        context_lines: list[str] = [
            "Send a <code>.lrc</code> or <code>.txt</code> file, or paste the lyrics text "
            "directly in chat (timestamped <code>[mm:ss.xx]</code> or plain text).",
            "Both embedded audio lyrics tags and the sidecar <code>.lrc</code> file "
            "will be updated after confirmation.",
        ]
        if existing_lyrics and (existing_lyrics.synced_lyrics or existing_lyrics.plain_lyrics):
            raw_lyr = existing_lyrics.synced_lyrics or existing_lyrics.plain_lyrics or ""
            first_lines = " / ".join(raw_lyr.splitlines()[:3])
            context_lines.insert(0, f"Current Preview: <code>{html.escape(first_lines)}</code>")
        await safe_edit_text(
            msg,
            format_screen(
                title=f"📝 Edit Lyrics — {html.escape(record.artist)} - {html.escape(record.title)}",
                context_lines=context_lines,
                footer_lines=[
                    f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
                    "<i>🧭 Home › Songs › Edit Lyrics</i>"
                ],
            ),
            reply_markup=build_input_cancel_keyboard(record.id),
        )
        return

    # 10. Auto / Custom Re-Search Menu
    if action == "research":
        await ack_callback(callback)
        await _reset_to_search_state(state, active_song_id=record.id)
        await safe_edit_text(
            msg,
            format_screen(
                title=f"🔎 Re-Search Song Data ({html.escape(record.artist)} - {html.escape(record.title)})",
                context_lines=[
                    "Choose whether to automatically re-query MusicBrainz + Genius + Last.fm + LRCLIB "
                    "or enter a custom search query / MusicBrainz URL / MBID."
                ],
                footer_lines=[
                    f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
                    "<i>🧭 Home › Songs › Re-Search</i>"
                ],
            ),
            reply_markup=build_research_mode_keyboard(record.id),
        )
        return

    if action == "research_custom":
        await ack_callback(callback)
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=None,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_research_query)
        await safe_edit_text(
            msg,
            format_screen(
                title=f"⌨️ Custom Re-Search — {html.escape(record.artist)} - {html.escape(record.title)}",
                context_lines=[
                    "Send any of the following:",
                    "• <code>Artist - Title | Album</code>",
                    "• <code>artist: Saori Hayami | title: Orange Mint</code>",
                    "• A MusicBrainz Release or Recording URL / UUID",
                    "• An ISRC code (<code>isrc:JP...</code>)",
                ],
                footer_lines=[
                    f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
                    "<i>🧭 Home › Songs › Custom Re-Search</i>"
                ],
            ),
            reply_markup=build_research_mode_keyboard(record.id),
        )
        return

    if action == "research_auto":
        await ack_callback(callback, "Found enriched match. Review dry-run preview.")
        await send_chat_action_safe(callback)
        await safe_edit_text(
            msg,
            format_loading_card(
                "Querying MusicBrainz, Genius, Last.fm &amp; LRCLIB",
                f"Looking up enriched metadata and lyrics for <code>{html.escape(record.title)}</code>...",
                "🧭 Home › Songs › Re-Search",
            ),
        )
        meta_svc = _resolve_metadata_service(pipeline, settings)
        lyrics_client = _resolve_lyrics_client(pipeline, settings)
        seed_meta = (
            await meta_svc.read_local_metadata(record.storage_path)
            if record.storage_path.is_file()
            else TrackMetadata(
                title=record.title,
                artist=record.artist,
                album=record.album,
                year=record.year,
                track_number=record.track_number or 1,
                duration_seconds=int(round(record.duration_seconds)),
            )
        )
        candidates = await meta_svc.find_candidates(seed_meta)
        best = candidates[0].metadata
        fetched_lyrics = await lyrics_client.fetch_lyrics(best)
        lyrics_text = (
            (fetched_lyrics.synced_lyrics or fetched_lyrics.plain_lyrics)
            if fetched_lyrics is not None
            else None
        )
        proposal: dict[str, object] = {
            "track_id": record.id,
            "action_type": "research",
            "new_title": best.title,
            "new_artist": best.artist,
            "new_album": best.album,
            "new_year": best.year,
            "new_track_number": best.track_number or record.track_number or 1,
            "new_recording_mbid": best.recording_mbid,
            "new_release_mbid": best.release_mbid,
            "new_lyrics": lyrics_text,
        }
        await state.update_data(
            active_song_id=record.id,
            pending_song_proposal=proposal,
        )
        await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
        await safe_edit_text(
            msg,
            format_song_edit_dry_run_card(record, proposal, organizer),
            reply_markup=build_dry_run_confirmation_keyboard(record.id, retry_action="research"),
        )
        return

    # 11. Export & Import single song JSON
    if action == "export":
        await ack_callback(callback, "Exported song JSON.")
        await send_chat_action_safe(callback, ChatAction.UPLOAD_DOCUMENT)
        await _reset_to_search_state(state, active_song_id=record.id)
        bundle = await organizer.export_song_bundle(record)
        json_text = json.dumps(bundle, indent=2, ensure_ascii=False)
        safe_filename = f"{record.artist} - {record.title}.json".replace("/", "_").replace("\\", "_")
        if isinstance(msg, Message):
            await msg.answer_document(
                BufferedInputFile(json_text.encode("utf-8"), filename=safe_filename),
                caption=(
                    f"<b>📤 Exported Song Data: {html.escape(record.artist)} - "
                    f"{html.escape(record.title)}</b>\n"
                    "<blockquote>Edit this JSON and re-import via "
                    "<code>📥 Import Data</code>.</blockquote>"
                ),
            )
        return

    if action == "import":
        await ack_callback(callback)
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=None,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_import_payload)
        await safe_edit_text(
            msg,
            format_screen(
                title=f"📥 Import Song Data — {html.escape(record.artist)} - {html.escape(record.title)}",
                context_lines=[
                    "Upload a <code>.json</code> file or paste a JSON object with any of these keys:",
                    '<code>{"title": "...", "artist": "...", "album": "...", '
                    '"year": 2020, "track_number": 1, "lyrics": "..."}</code>',
                    "A dry-run preview will be shown for your confirmation before applying.",
                ],
                footer_lines=[
                    f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code> • "
                    "<i>🧭 Home › Songs › Import</i>"
                ],
            ),
            reply_markup=build_input_cancel_keyboard(record.id),
        )
        return

    # 12. Confirm & Apply Dry-Run Proposal
    if action == "confirm_apply":
        if organizer.is_reconciling:
            await ack_callback(
                callback,
                "🔒 Library reconciliation is running in the background. Changes are temporarily frozen.",
                show_alert=True,
            )
            return

        fsm_data = await state.get_data()
        raw_proposal = fsm_data.get("pending_song_proposal")
        if not isinstance(raw_proposal, dict):
            await ack_callback(callback, "No pending changes to apply.", show_alert=True)
            return

        await ack_callback(callback, "Changes applied!")
        await send_chat_action_safe(callback)
        await safe_edit_text(
            msg,
            format_loading_card(
                "Applying Song Changes",
                "Writing audio tags, sidecar lyrics, and updating library catalog...",
                f"Track ID: {html.escape(record.id[:12])}",
            ),
        )

        new_title = str(raw_proposal.get("new_title") or record.title)
        new_artist = str(raw_proposal.get("new_artist") or record.artist)
        new_album = str(raw_proposal.get("new_album") or record.album)
        raw_year = raw_proposal.get("new_year")
        new_year = int(str(raw_year)) if raw_year is not None and str(raw_year).isdigit() else record.year
        raw_trk = raw_proposal.get("new_track_number")
        new_trk = (
            int(str(raw_trk))
            if raw_trk is not None and str(raw_trk).isdigit()
            else (record.track_number or 1)
        )
        new_lyrics_raw = raw_proposal.get("new_lyrics")
        lyrics_obj: LyricsData | None = None
        if isinstance(new_lyrics_raw, str) and new_lyrics_raw.strip():
            cleaned_lyr = new_lyrics_raw.strip()
            is_synced = "[" in cleaned_lyr and "]" in cleaned_lyr
            lyrics_obj = LyricsData(
                synced_lyrics=cleaned_lyr if is_synced else None,
                plain_lyrics=None if is_synced else cleaned_lyr,
            )

        updated_meta = TrackMetadata(
            title=new_title,
            artist=new_artist,
            album=new_album,
            album_artist=new_artist,
            year=new_year,
            track_number=new_trk,
            duration_seconds=int(round(record.duration_seconds)),
            recording_mbid=(
                str(raw_proposal["new_recording_mbid"]) if raw_proposal.get("new_recording_mbid") else None
            ),
            release_mbid=(
                str(raw_proposal["new_release_mbid"]) if raw_proposal.get("new_release_mbid") else None
            ),
        )

        new_path = await organizer.relocate_and_retag_track(
            record.storage_path,
            updated_meta,
            lyrics_obj,
        )

        cover_hex = raw_proposal.get("new_cover_hex")
        cover_mime = str(raw_proposal.get("new_cover_mime") or "image/jpeg")
        if isinstance(cover_hex, str) and cover_hex:
            cover_bytes = bytes.fromhex(cover_hex)
            await organizer.update_track_cover_art(new_path, cover_bytes, mime_type=cover_mime)

        final_lyrics = await organizer.read_track_lyrics(new_path)
        has_synced_flag = bool(final_lyrics and (final_lyrics.has_synced or final_lyrics.has_any))

        actor_id = current_user.telegram_id if current_user is not None else 0
        updated_record = await governance_repository.update_library_track_full(
            record.id,
            title=new_title,
            artist=new_artist,
            album=new_album,
            year=new_year,
            track_number=new_trk,
            storage_path=new_path,
            has_synced_lyrics=has_synced_flag,
            recording_mbid=updated_meta.recording_mbid,
            release_mbid=updated_meta.release_mbid,
            modified_by=actor_id,
        )
        await governance_repository.append_audit_log(
            AuditLogEntry(
                actor_id=actor_id,
                action="SONG_UPDATED_INTERACTIVE",
                target_entity=record.id,
                details={
                    "action_type": str(raw_proposal.get("action_type") or "metadata"),
                    "title": new_title,
                    "artist": new_artist,
                    "album": new_album,
                    "year": new_year,
                    "storage_path": str(new_path),
                },
            )
        )

        effective_rec = updated_record or record
        await _reset_to_search_state(state, active_song_id=effective_rec.id)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"songs:view:{effective_rec.id[:12]}",
            default_back="songs:list",
        )
        has_lrc = effective_rec.storage_path.with_suffix(".lrc").is_file()
        has_cover = (effective_rec.storage_path.parent / "cover.jpg").is_file()
        up_lbl, mod_lbl = await _resolve_song_provenance_labels(
            effective_rec,
            governance_repository,
            catalog_repository,
        )
        await safe_edit_text(
            msg,
            format_song_detail_card(
                effective_rec,
                has_lrc_file=has_lrc,
                has_cover_file=has_cover,
                changes_applied=True,
                uploader_label=up_lbl,
                modifier_label=mod_lbl,
            ),
            reply_markup=build_song_action_keyboard(effective_rec.id, back_callback=back_cb),
        )
        return

    await ack_callback(callback)


@songs_router.message(
    SongManagementStates.awaiting_metadata_input,
    _is_non_command_text,
)
async def handle_metadata_edit_input(
    message: Message,
    state: FSMContext,
    governance_repository: GovernanceRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Parse user metadata input and present a Dry-Run Preview Report for confirmation."""
    if governance_repository is None:
        return
    data = await state.get_data()
    track_id = str(data.get("active_song_id") or "")
    target_field = str(data.get("active_meta_field") or "multi")
    record = await governance_repository.get_library_track(track_id)
    if record is None:
        await message.answer(
            format_screen(
                title="⚠️ Song Not Found",
                context_lines=["Song no longer exists in the database."],
                footer_lines=["<i>🧭 Home › Songs</i>"],
            )
        )
        await state.clear()
        return

    proposal = _parse_metadata_edit_input(message.text or "", target_field, record)
    await state.update_data(pending_song_proposal=proposal)
    await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
    organizer = _resolve_organizer(pipeline, settings)
    await message.answer(
        format_song_edit_dry_run_card(record, proposal, organizer),
        reply_markup=build_dry_run_confirmation_keyboard(record.id, retry_action="edit_meta"),
    )


@songs_router.message(
    SongManagementStates.awaiting_lyrics_input,
    _is_non_media_input_message,
)
async def handle_lyrics_edit_input(
    message: Message,
    state: FSMContext,
    governance_repository: GovernanceRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Accept pasted lyrics text or an uploaded `.lrc`/`.txt` document and show a Dry-Run Preview."""
    if governance_repository is None:
        return
    data = await state.get_data()
    track_id = str(data.get("active_song_id") or "")
    record = await governance_repository.get_library_track(track_id)
    if record is None:
        await message.answer(
            format_screen(
                title="⚠️ Song Not Found",
                context_lines=["Song no longer exists in the database."],
                footer_lines=["<i>🧭 Home › Songs</i>"],
            )
        )
        await state.clear()
        return

    lyrics_text = (message.text or "").strip()
    if not lyrics_text and message.document is not None and message.bot is not None:
        tg_file = await message.bot.get_file(message.document.file_id)
        if tg_file.file_path:
            local_path = _resolve_uploaded_file_path(tg_file.file_path, settings)
            if local_path.is_file():
                lyrics_text = local_path.read_text(encoding="utf-8", errors="replace").strip()

    if not lyrics_text:
        await message.answer(
            format_screen(
                title="⚠️ Empty Lyrics Input",
                context_lines=[
                    "Please paste non-empty lyrics text or upload a <code>.lrc</code>/<code>.txt</code> file."
                ],
                footer_lines=[f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code>"],
            ),
            reply_markup=build_input_cancel_keyboard(record.id),
        )
        return

    proposal: dict[str, object] = {
        "track_id": record.id,
        "action_type": "lyrics",
        "new_title": record.title,
        "new_artist": record.artist,
        "new_album": record.album,
        "new_year": record.year,
        "new_track_number": record.track_number or 1,
        "new_lyrics": lyrics_text,
    }
    await state.update_data(pending_song_proposal=proposal)
    await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
    organizer = _resolve_organizer(pipeline, settings)
    await message.answer(
        format_song_edit_dry_run_card(record, proposal, organizer),
        reply_markup=build_dry_run_confirmation_keyboard(record.id, retry_action="edit_lyrics"),
    )


@songs_router.message(
    SongManagementStates.awaiting_cover_input,
    _is_non_media_input_message,
)
async def handle_cover_edit_input(
    message: Message,
    state: FSMContext,
    governance_repository: GovernanceRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Accept an uploaded Photo, image Document, or image URL and present a Dry-Run Preview."""
    if governance_repository is None:
        return
    data = await state.get_data()
    track_id = str(data.get("active_song_id") or "")
    record = await governance_repository.get_library_track(track_id)
    if record is None:
        await message.answer(
            format_screen(
                title="⚠️ Song Not Found",
                context_lines=["Song no longer exists in the database."],
                footer_lines=["<i>🧭 Home › Songs</i>"],
            )
        )
        await state.clear()
        return

    await send_chat_action_safe(message)
    cover_bytes: bytes | None = None
    mime_type = "image/jpeg"

    if message.photo and message.bot is not None:
        largest = message.photo[-1]
        tg_file = await message.bot.get_file(largest.file_id)
        if tg_file.file_path:
            local_path = _resolve_uploaded_file_path(tg_file.file_path, settings)
            if local_path.is_file():
                cover_bytes = local_path.read_bytes()
    elif message.document is not None and message.bot is not None:
        if message.document.mime_type:
            mime_type = message.document.mime_type
        tg_file = await message.bot.get_file(message.document.file_id)
        if tg_file.file_path:
            local_path = _resolve_uploaded_file_path(tg_file.file_path, settings)
            if local_path.is_file():
                cover_bytes = local_path.read_bytes()
    elif message.text and message.text.strip().lower().startswith(("http://", "https://")):
        url = message.text.strip()
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    cover_bytes = resp.content
                    ct = resp.headers.get("content-type", "")
                    if "png" in ct.lower():
                        mime_type = "image/png"
        except Exception as exc:
            logger.debug("Cover URL download failed: %s", exc)

    if not cover_bytes:
        await message.answer(
            format_screen(
                title="⚠️ Invalid Cover Image",
                context_lines=[
                    "Could not read cover image. Please send a Photo, an image file, "
                    "or a valid <code>https://</code> image URL."
                ],
                footer_lines=[f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code>"],
            ),
            reply_markup=build_input_cancel_keyboard(record.id),
        )
        return

    proposal: dict[str, object] = {
        "track_id": record.id,
        "action_type": "cover",
        "new_title": record.title,
        "new_artist": record.artist,
        "new_album": record.album,
        "new_year": record.year,
        "new_track_number": record.track_number or 1,
        "new_cover_hex": cover_bytes.hex(),
        "new_cover_size": len(cover_bytes),
        "new_cover_mime": mime_type,
    }
    await state.update_data(pending_song_proposal=proposal)
    await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
    organizer = _resolve_organizer(pipeline, settings)
    await message.answer(
        format_song_edit_dry_run_card(record, proposal, organizer),
        reply_markup=build_dry_run_confirmation_keyboard(record.id, retry_action="edit_cover"),
    )


@songs_router.message(
    SongManagementStates.awaiting_research_query,
    _is_non_command_text,
)
async def handle_custom_research_input(
    message: Message,
    state: FSMContext,
    governance_repository: GovernanceRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Execute a custom MusicBrainz/Genius/Last.fm/LRCLIB query and show a Dry-Run Preview."""
    if governance_repository is None:
        return
    data = await state.get_data()
    track_id = str(data.get("active_song_id") or "")
    record = await governance_repository.get_library_track(track_id)
    if record is None:
        await message.answer(
            format_screen(
                title="⚠️ Song Not Found",
                context_lines=["Song no longer exists in the database."],
                footer_lines=["<i>🧭 Home › Songs</i>"],
            )
        )
        await state.clear()
        return

    await send_chat_action_safe(message)
    base_meta = TrackMetadata(
        title=record.title,
        artist=record.artist,
        album=record.album,
        year=record.year,
        track_number=record.track_number or 1,
        duration_seconds=int(round(record.duration_seconds)),
    )
    query_meta = parse_manual_search_query(message.text or "", base_metadata=base_meta)
    meta_svc = _resolve_metadata_service(pipeline, settings)
    lyrics_client = _resolve_lyrics_client(pipeline, settings)

    candidates = await meta_svc.find_candidates(query_meta)
    best = candidates[0].metadata
    fetched_lyrics = await lyrics_client.fetch_lyrics(best)
    lyrics_text = (
        (fetched_lyrics.synced_lyrics or fetched_lyrics.plain_lyrics) if fetched_lyrics is not None else None
    )

    proposal: dict[str, object] = {
        "track_id": record.id,
        "action_type": "research",
        "new_title": best.title,
        "new_artist": best.artist,
        "new_album": best.album,
        "new_year": best.year,
        "new_track_number": best.track_number or record.track_number or 1,
        "new_recording_mbid": best.recording_mbid,
        "new_release_mbid": best.release_mbid,
        "new_lyrics": lyrics_text,
    }
    await state.update_data(pending_song_proposal=proposal)
    await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
    organizer = _resolve_organizer(pipeline, settings)
    await message.answer(
        format_song_edit_dry_run_card(record, proposal, organizer),
        reply_markup=build_dry_run_confirmation_keyboard(record.id, retry_action="research"),
    )


@songs_router.message(
    SongManagementStates.awaiting_import_payload,
    _is_non_media_input_message,
)
async def handle_import_payload_input(
    message: Message,
    state: FSMContext,
    governance_repository: GovernanceRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Parse an uploaded `.json` file or pasted JSON string and show a Dry-Run Preview."""
    if governance_repository is None:
        return
    data = await state.get_data()
    track_id = str(data.get("active_song_id") or "")
    record = await governance_repository.get_library_track(track_id)
    if record is None:
        await message.answer(
            format_screen(
                title="⚠️ Song Not Found",
                context_lines=["Song no longer exists in the database."],
                footer_lines=["<i>🧭 Home › Songs</i>"],
            )
        )
        await state.clear()
        return

    raw_json_str = (message.text or "").strip()
    if not raw_json_str and message.document is not None and message.bot is not None:
        tg_file = await message.bot.get_file(message.document.file_id)
        if tg_file.file_path:
            local_path = _resolve_uploaded_file_path(tg_file.file_path, settings)
            if local_path.is_file():
                raw_json_str = local_path.read_text(encoding="utf-8", errors="replace").strip()

    try:
        parsed = json.loads(raw_json_str)
    except Exception:
        await message.answer(
            format_screen(
                title="⚠️ Invalid JSON Payload",
                context_lines=["Please send a valid JSON object or <code>.json</code> file."],
                footer_lines=[f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code>"],
            ),
            reply_markup=build_input_cancel_keyboard(record.id),
        )
        return

    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        await message.answer(
            format_screen(
                title="⚠️ Invalid JSON Structure",
                context_lines=["JSON payload must be an object containing song metadata fields."],
                footer_lines=[f"<i>Track ID:</i> <code>{html.escape(record.id[:12])}</code>"],
            ),
            reply_markup=build_input_cancel_keyboard(record.id),
        )
        return

    raw_year = parsed.get("year")
    raw_trk = parsed.get("track_number")
    proposal: dict[str, object] = {
        "track_id": record.id,
        "action_type": "import",
        "new_title": str(parsed.get("title") or record.title).strip(),
        "new_artist": normalize_primary_artist(str(parsed.get("artist") or record.artist).strip()),
        "new_album": str(parsed.get("album") or record.album).strip(),
        "new_year": int(str(raw_year)) if raw_year is not None and str(raw_year).isdigit() else record.year,
        "new_track_number": (
            int(str(raw_trk))
            if raw_trk is not None and str(raw_trk).isdigit()
            else (record.track_number or 1)
        ),
        "new_lyrics": str(parsed["lyrics"]).strip() if isinstance(parsed.get("lyrics"), str) else None,
    }
    await state.update_data(pending_song_proposal=proposal)
    await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
    organizer = _resolve_organizer(pipeline, settings)
    await message.answer(
        format_song_edit_dry_run_card(record, proposal, organizer),
        reply_markup=build_dry_run_confirmation_keyboard(record.id, retry_action="import"),
    )


@songs_router.message(
    SongManagementStates.awaiting_dry_run_confirmation,
    _is_non_command_text,
)
async def handle_dry_run_unexpected_text(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
) -> None:
    """Remind the user to confirm/cancel a pending dry-run proposal, or fall back to library search."""
    data = await state.get_data()
    raw_proposal = data.get("pending_song_proposal")
    track_id = str(data.get("active_song_id") or "")
    if isinstance(raw_proposal, dict) and track_id:
        await message.answer(
            format_screen(
                title="⚠️ Pending Dry-Run Preview",
                context_lines=[
                    "Please use the buttons below to confirm &amp; apply or cancel the pending changes."
                ],
                footer_lines=[f"<i>Track ID:</i> <code>{html.escape(track_id[:12])}</code>"],
            ),
            reply_markup=build_dry_run_confirmation_keyboard(track_id),
        )
        return

    await handle_songs_search_text(
        message,
        state=state,
        current_user=current_user,
        governance_repository=governance_repository,
    )
