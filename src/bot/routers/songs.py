"""Interactive `/songs` & `/rescan` router for searching, editing, exporting, and reconciling tracks."""

import html
import json
import logging
from pathlib import Path

import httpx
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message

from src.bot.api_server import resolve_local_api_file_path
from src.bot.keyboards.admin import (
    BTN_MAIN_SONGS,
    RESERVED_MENU_BUTTONS,
)
from src.bot.keyboards.songs import (
    build_dry_run_confirmation_keyboard,
    build_input_cancel_keyboard,
    build_metadata_field_keyboard,
    build_rescan_confirmation_keyboard,
    build_research_mode_keyboard,
    build_song_action_keyboard,
    build_songs_search_menu_keyboard,
)
from src.bot.states import SongManagementStates
from src.config.settings import AppSettings
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
from src.core.ports import GovernanceRepositoryPort
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


def format_song_detail_card(
    record: LibraryTrackRecord,
    *,
    has_lrc_file: bool,
    has_cover_file: bool,
) -> str:
    """Render an HTML summary card for a selected library song."""
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

    return (
        "🎵 <b>Song Details &amp; Actions</b>\n"
        "────────────────────────\n"
        f"• <b>ID:</b> <code>{html.escape(record.id[:12])}</code>\n"
        f"• <b>Title:</b> {html.escape(record.title)}\n"
        f"• <b>Artist:</b> {html.escape(record.artist)}\n"
        f"• <b>Album:</b> {html.escape(record.album)} ({year_str})\n"
        f"• <b>Track #:</b> {trk_str} • <b>Duration:</b> {dur_str}\n"
        f"• <b>Lyrics:</b> {lyrics_badge}\n"
        f"• <b>Cover Art:</b> {cover_badge}\n"
        f"• <b>Path:</b> <code>{html.escape(str(record.storage_path))}</code>\n\n"
        "Select an action below:"
    )


def format_song_edit_dry_run_card(
    record: LibraryTrackRecord,
    proposal: dict[str, object],
    organizer: LibraryOrganizer,
) -> str:
    """Render a Dry-Run Preview Report comparing current song state against `proposal`."""
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

    lines: list[str] = [
        f"🧪 <b>Dry-Run Preview ({html.escape(action_type)})</b>",
        "────────────────────────",
        "Please review what is going to change before applying:",
        "",
    ]

    def _diff_line(label: str, old_val: object, new_val: object) -> str:
        old_s = html.escape(str(old_val if old_val is not None else "None"))
        new_s = html.escape(str(new_val if new_val is not None else "None"))
        if old_s != new_s:
            return f"• <b>{label}:</b> <s>{old_s}</s> ➡️ <b>{new_s}</b>"
        return f"• <b>{label}:</b> {old_s} <i>(unchanged)</i>"

    lines.append(_diff_line("Title", record.title, new_title))
    lines.append(_diff_line("Artist", record.artist, new_artist))
    lines.append(_diff_line("Album", record.album, new_album))
    lines.append(_diff_line("Year", record.year, new_year))
    lines.append(_diff_line("Track #", record.track_number or 1, new_trk))

    old_path_str = str(record.storage_path)
    new_path_str = str(new_path)
    if old_path_str.replace("\\", "/").lower() != new_path_str.replace("\\", "/").lower():
        lines.append(
            f"• <b>File Location:</b>\n"
            f"  <code>{html.escape(old_path_str)}</code>\n"
            f"  ➡️ <code>{html.escape(new_path_str)}</code>"
        )
    else:
        lines.append(f"• <b>File Location:</b> <code>{html.escape(old_path_str)}</code> <i>(in-place)</i>")

    new_lyrics = proposal.get("new_lyrics")
    if isinstance(new_lyrics, str) and new_lyrics.strip():
        line_count = len(new_lyrics.strip().splitlines())
        is_synced = "[" in new_lyrics and "]" in new_lyrics
        kind = "Synced LRC" if is_synced else "Plain Text"
        lines.append(
            f"• <b>Lyrics:</b> ➡️ <b>Update ({kind}, {line_count} lines)</b> "
            "(embedded in audio + <code>.lrc</code> sidecar)"
        )

    cover_size = proposal.get("new_cover_size")
    if cover_size is not None:
        kb = max(1, int(str(cover_size)) // 1024)
        lines.append(
            f"• <b>Cover Art:</b> ➡️ <b>Update <code>cover.jpg</code> &amp; embedded picture ({kb} KB)</b>"
        )

    lines.append("\nTap <b>Confirm &amp; Apply Changes</b> to execute or <b>Edit Again</b> to adjust.")
    return "\n".join(lines)


def format_rescan_dry_run_card(report: LibraryRescanReport, applied: bool = False) -> str:
    """Render a summary or Dry-Run Preview Report for a `storage/library` filesystem scan."""
    header = (
        "✅ <b>Library Rescan &amp; Sync Complete</b>"
        if applied
        else "🧪 <b>Library Rescan Dry-Run Preview</b>"
    )
    lines: list[str] = [
        header,
        "────────────────────────",
        f"• <b>Unchanged Tracks:</b> {report.unchanged_count}",
        f"• <b>Added on Disk:</b> {len(report.added_tracks)}",
        f"• <b>Modified / Moved on Disk:</b> {len(report.updated_tracks)}",
        f"• <b>Removed from Disk:</b> {len(report.removed_tracks)}",
    ]

    if report.added_tracks:
        lines.append("\n<b>➕ Added Tracks:</b>")
        for rec in report.added_tracks[:8]:
            lines.append(
                f"  • {html.escape(rec.artist)} - {html.escape(rec.title)} "
                f"(<code>{html.escape(rec.storage_path.name)}</code>)"
            )

    if report.updated_tracks:
        lines.append("\n<b>🔄 Modified / Moved Tracks:</b>")
        for rec in report.updated_tracks[:8]:
            lines.append(
                f"  • {html.escape(rec.artist)} - {html.escape(rec.title)} "
                f"➡️ <code>{html.escape(str(rec.storage_path))}</code>"
            )

    if report.removed_tracks:
        lines.append("\n<b>🗑️ Missing Files (Will Remove from DB):</b>")
        for rec in report.removed_tracks[:8]:
            lines.append(
                f"  • {html.escape(rec.artist)} - {html.escape(rec.title)} "
                f"(<code>{html.escape(str(rec.storage_path))}</code>)"
            )

    if not applied and report.has_changes:
        lines.append("\nConfirm below to synchronize the SQLite database with the filesystem:")
    elif not report.has_changes:
        lines.append("\n✨ <i>Filesystem and SQLite database are completely in sync.</i>")

    return "\n".join(lines)


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
        await message.answer("⛔ You do not have permission to browse or edit the music library.")
        return
    if governance_repository is None:
        await message.answer("⚠️ Library repository is not configured.")
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

    header = (
        f"🎵 <b>Library Search Results for</b> <code>{html.escape(query)}</code> ({len(tracks)})"
        if query
        else f"🎵 <b>Music Library Browser</b> ({len(tracks)} track(s))"
    )
    body = (
        f"{header}\n"
        "────────────────────────\n"
        "• Tap a song below to open its management menu "
        "(download, edit metadata/cover/lyrics, re-search, or export/import).\n"
        "• Or simply <b>type a song title, artist, or album</b> right now to filter the list:"
    )
    await message.answer(
        body,
        reply_markup=build_songs_search_menu_keyboard(tracks, can_rescan=True),
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
        await message.answer("⛔ You do not have permission to rescan the library.")
        return
    if governance_repository is None:
        await message.answer("⚠️ Library repository is not configured.")
        return

    organizer = _resolve_organizer(pipeline, settings)
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


@songs_router.message(
    SongManagementStates.awaiting_search_query,
    _is_non_command_text,
)
async def handle_songs_search_text(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
) -> None:
    """Filter library songs when the user types a search query in `awaiting_search_query` state."""
    text = (message.text or "").strip()
    if not text or text in RESERVED_MENU_BUTTONS:
        return
    if not _can_access_songs(current_user) or governance_repository is None:
        return

    tracks = await governance_repository.search_library_tracks(text, limit=15)
    await _reset_to_search_state(state)
    if not tracks:
        all_tracks = await governance_repository.list_library_tracks(limit=10)
        await message.answer(
            f"🔍 No songs found matching <code>{html.escape(text)}</code>.\n"
            "Try another search term or pick from recent tracks below:",
            reply_markup=build_songs_search_menu_keyboard(all_tracks, can_rescan=True),
        )
        return

    await message.answer(
        f"🎵 <b>Search Results for</b> <code>{html.escape(text)}</code> ({len(tracks)})\n"
        "────────────────────────\n"
        "Tap a song to manage it, or type another search query:",
        reply_markup=build_songs_search_menu_keyboard(tracks, can_rescan=True),
    )


@songs_router.callback_query(F.data.startswith("songs:"))
async def handle_songs_callback(
    callback: CallbackQuery,
    state: FSMContext,
    current_user: UserAccount | None = None,
    governance_repository: GovernanceRepositoryPort | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Handle all inline button actions inside the `/songs` interactive menu."""
    if not _can_access_songs(current_user):
        await callback.answer("Permission denied.", show_alert=True)
        return
    if governance_repository is None:
        await callback.answer("Repository unavailable.", show_alert=True)
        return

    raw_data = callback.data or ""
    parts = raw_data.split(":")
    if len(parts) < 2:
        await callback.answer("Invalid action.", show_alert=True)
        return

    action = parts[1]
    msg = callback.message if isinstance(callback.message, Message) else None
    organizer = _resolve_organizer(pipeline, settings)

    # 1. List / Back to Songs Menu
    if action == "list":
        tracks = await governance_repository.list_library_tracks(limit=15)
        await _reset_to_search_state(state)
        text = (
            f"🎵 <b>Music Library Browser</b> ({len(tracks)} track(s))\n"
            "────────────────────────\n"
            "Tap a song below or type a song title, artist, or album to search:"
        )
        if msg is not None:
            await msg.edit_text(
                text,
                reply_markup=build_songs_search_menu_keyboard(tracks, can_rescan=True),
            )
        await callback.answer()
        return

    if action == "search_prompt":
        await _reset_to_search_state(state)
        if msg is not None:
            await msg.answer("🔎 <b>Type a song title, artist, or album name</b> to search the library:")
        await callback.answer()
        return

    # 2. Export entire library JSON
    if action == "export_all":
        await _reset_to_search_state(state)
        tracks = await governance_repository.list_library_tracks(limit=1000)
        bundles = [await organizer.export_song_bundle(t) for t in tracks]
        json_bytes = json.dumps(bundles, indent=2, ensure_ascii=False).encode("utf-8")
        if msg is not None:
            await msg.answer_document(
                BufferedInputFile(json_bytes, filename="library_songs_export.json"),
                caption=f"📤 Exported <b>{len(bundles)}</b> song(s) from the library.",
            )
        await callback.answer("Exported library JSON.")
        return

    # 3. Rescan dry-run & apply
    if action == "rescan_dryrun":
        existing = await governance_repository.list_library_tracks(limit=10000)
        report = await organizer.scan_library_filesystem(existing)
        if msg is not None:
            if report.has_changes:
                await state.update_data(pending_song_proposal=None, active_meta_field=None)
                await state.set_state(SongManagementStates.awaiting_dry_run_confirmation)
                await msg.edit_text(
                    format_rescan_dry_run_card(report, applied=False),
                    reply_markup=build_rescan_confirmation_keyboard(),
                )
            else:
                await _reset_to_search_state(state)
                await msg.edit_text(
                    format_rescan_dry_run_card(report, applied=True),
                    reply_markup=build_songs_search_menu_keyboard(existing[:12], can_rescan=True),
                )
        await callback.answer()
        return

    if action == "rescan_apply":
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
        if msg is not None:
            await msg.edit_text(
                format_rescan_dry_run_card(report, applied=True),
                reply_markup=build_songs_search_menu_keyboard(updated_list, can_rescan=True),
            )
        await callback.answer("Database synchronized with library folder.")
        return

    # Actions requiring a `track_id` in `parts[2]`
    if len(parts) < 3:
        await callback.answer("Missing track ID.", show_alert=True)
        return

    track_id_prefix = parts[2]
    record = await governance_repository.get_library_track(track_id_prefix)
    if record is None:
        await callback.answer("Song not found in database.", show_alert=True)
        return

    # 4. View single song card
    if action == "view":
        await _reset_to_search_state(state, active_song_id=record.id)
        has_lrc = record.storage_path.with_suffix(".lrc").is_file()
        has_cover = (record.storage_path.parent / "cover.jpg").is_file()
        card = format_song_detail_card(record, has_lrc_file=has_lrc, has_cover_file=has_cover)
        if msg is not None:
            await msg.edit_text(
                card,
                reply_markup=build_song_action_keyboard(record.id),
            )
        await callback.answer()
        return

    # 5. Retrieve / Download Song File
    if action == "get_track":
        await _reset_to_search_state(state, active_song_id=record.id)
        if not record.storage_path.is_file():
            await callback.answer("Audio file is missing on disk. Run Rescan.", show_alert=True)
            return
        if msg is not None:
            await msg.answer_audio(
                FSInputFile(record.storage_path),
                title=record.title,
                performer=record.artist,
                caption=(
                    f"🎵 <b>{html.escape(record.artist)} - {html.escape(record.title)}</b> "
                    f"({html.escape(record.album)})"
                ),
            )
            lrc_path = record.storage_path.with_suffix(".lrc")
            if lrc_path.is_file():
                await msg.answer_document(
                    FSInputFile(lrc_path),
                    caption="📝 Synchronized <code>.lrc</code> sidecar lyrics",
                )
        await callback.answer("Sent song file.")
        return

    # 6. Retrieve / Download Full Album
    if action == "get_album":
        await _reset_to_search_state(state, active_song_id=record.id)
        album_tracks = await governance_repository.list_album_tracks(record.artist, record.album)
        if not album_tracks:
            album_tracks = [record]
        if msg is not None:
            cover_path = record.storage_path.parent / "cover.jpg"
            if cover_path.is_file():
                await msg.answer_photo(
                    FSInputFile(cover_path),
                    caption=(
                        f"💿 <b>{html.escape(record.artist)} - {html.escape(record.album)}</b> "
                        f"({len(album_tracks)} track(s))"
                    ),
                )
            sent_count = 0
            for trk in album_tracks:
                if trk.storage_path.is_file():
                    await msg.answer_audio(
                        FSInputFile(trk.storage_path),
                        title=trk.title,
                        performer=trk.artist,
                    )
                    sent_count += 1
            if sent_count == 0:
                await callback.answer("Album audio files are missing on disk.", show_alert=True)
                return
        await callback.answer("Sent album tracks.")
        return

    # 7. Open Metadata Field Picker
    if action == "edit_meta":
        await state.update_data(
            active_song_id=record.id,
            active_meta_field="multi",
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_metadata_input)
        prompt = (
            f"✏️ <b>Edit Metadata for {html.escape(record.artist)} - {html.escape(record.title)}</b>\n"
            "────────────────────────\n"
            f"• <b>Current Title:</b> <code>{html.escape(record.title)}</code>\n"
            f"• <b>Current Artist:</b> <code>{html.escape(record.artist)}</code>\n"
            f"• <b>Current Album:</b> <code>{html.escape(record.album)}</code>\n"
            f"• <b>Current Year:</b> <code>{record.year or 'Unknown'}</code>\n"
            f"• <b>Current Track #:</b> <code>{record.track_number or 1}</code>\n\n"
            "Tap a specific field button below, or send a message right now in the format:\n"
            "<code>artist: Saori Hayami | title: Orange Mint | album: Orange Mint | year: 2015</code>"
        )
        if msg is not None:
            await msg.edit_text(
                prompt,
                reply_markup=build_metadata_field_keyboard(record.id),
            )
        await callback.answer()
        return

    if action == "field" and len(parts) >= 4:
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
        if msg is not None:
            await msg.edit_text(
                f"✏️ <b>Enter new {html.escape(label)}</b>\n"
                f"Current value: <code>{html.escape(current_val)}</code>\n\n"
                "Reply with the new value below (a dry-run preview will be shown before saving):",
                reply_markup=build_metadata_field_keyboard(record.id),
            )
        await callback.answer()
        return

    # 8. Edit Cover Art Prompt
    if action == "edit_cover":
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=None,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_cover_input)
        if msg is not None:
            await msg.edit_text(
                f"🖼️ <b>Edit Cover Art for {html.escape(record.artist)} - {html.escape(record.title)}</b>\n"
                "────────────────────────\n"
                "Send a <b>Photo</b>, an image <b>Document</b> (<code>.jpg</code> / <code>.png</code>), "
                "or paste a direct <b>Image URL</b> (<code>https://...</code>).\n\n"
                "<i>You will see a dry-run confirmation preview before <code>cover.jpg</code> "
                "and embedded audio tags are updated.</i>",
                reply_markup=build_input_cancel_keyboard(record.id),
            )
        await callback.answer()
        return

    # 9. Edit Lyrics Prompt
    if action == "edit_lyrics":
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=None,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_lyrics_input)
        existing_lyrics = await organizer.read_track_lyrics(record.storage_path)
        preview_snippet = ""
        if existing_lyrics and (existing_lyrics.synced_lyrics or existing_lyrics.plain_lyrics):
            raw_lyr = existing_lyrics.synced_lyrics or existing_lyrics.plain_lyrics or ""
            first_lines = "\n".join(raw_lyr.splitlines()[:4])
            preview_snippet = f"\n<b>Current Lyrics Preview:</b>\n<pre>{html.escape(first_lines)}</pre>\n"
        if msg is not None:
            await msg.edit_text(
                f"📝 <b>Edit Lyrics for {html.escape(record.artist)} - {html.escape(record.title)}</b>\n"
                "────────────────────────"
                f"{preview_snippet}\n"
                "Send a <code>.lrc</code> or <code>.txt</code> file, or paste the lyrics text "
                "directly in chat (timestamped <code>[mm:ss.xx]</code> or plain text).\n\n"
                "<i>Both the embedded audio lyrics tags and the sidecar <code>.lrc</code> file "
                "will be updated after confirmation.</i>",
                reply_markup=build_input_cancel_keyboard(record.id),
            )
        await callback.answer()
        return

    # 10. Auto / Custom Re-Search Menu
    if action == "research":
        await _reset_to_search_state(state, active_song_id=record.id)
        if msg is not None:
            await msg.edit_text(
                f"🔎 <b>Re-Search Song Data ({html.escape(record.artist)} - "
                f"{html.escape(record.title)})</b>\n"
                "────────────────────────\n"
                "Choose whether to automatically re-query <b>MusicBrainz + Genius + Last.fm + LRCLIB</b> "
                "or enter a custom search query / MusicBrainz URL / MBID:",
                reply_markup=build_research_mode_keyboard(record.id),
            )
        await callback.answer()
        return

    if action == "research_custom":
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=None,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_research_query)
        if msg is not None:
            await msg.edit_text(
                f"⌨️ <b>Custom Re-Search for {html.escape(record.artist)} - {html.escape(record.title)}</b>\n"
                "────────────────────────\n"
                "Send any of the following:\n"
                "• <code>Artist - Title | Album</code>\n"
                "• <code>artist: Saori Hayami | title: Orange Mint</code>\n"
                "• A MusicBrainz Release or Recording URL / UUID\n"
                "• An ISRC code (<code>isrc:JP...</code>)",
                reply_markup=build_research_mode_keyboard(record.id),
            )
        await callback.answer()
        return

    if action == "research_auto":
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
        if msg is not None:
            await msg.edit_text(
                format_song_edit_dry_run_card(record, proposal, organizer),
                reply_markup=build_dry_run_confirmation_keyboard(record.id, retry_action="research"),
            )
        await callback.answer("Found enriched match. Review dry-run preview.")
        return

    # 11. Export & Import single song JSON
    if action == "export":
        await _reset_to_search_state(state, active_song_id=record.id)
        bundle = await organizer.export_song_bundle(record)
        json_text = json.dumps(bundle, indent=2, ensure_ascii=False)
        safe_filename = f"{record.artist} - {record.title}.json".replace("/", "_").replace("\\", "_")
        if msg is not None:
            await msg.answer_document(
                BufferedInputFile(json_text.encode("utf-8"), filename=safe_filename),
                caption=(
                    f"📤 <b>Exported Song Data:</b> {html.escape(record.artist)} - "
                    f"{html.escape(record.title)}\n"
                    "You can edit this JSON and re-import it via <b>📥 Import Data</b>."
                ),
            )
        await callback.answer("Exported song JSON.")
        return

    if action == "import":
        await state.update_data(
            active_song_id=record.id,
            active_meta_field=None,
            pending_song_proposal=None,
        )
        await state.set_state(SongManagementStates.awaiting_import_payload)
        if msg is not None:
            await msg.edit_text(
                f"📥 <b>Import Song Data for {html.escape(record.artist)} - {html.escape(record.title)}</b>\n"
                "────────────────────────\n"
                "Upload a <code>.json</code> file or paste a JSON object with any of these keys:\n"
                '<code>{"title": "...", "artist": "...", "album": "...", '
                '"year": 2020, "track_number": 1, "lyrics": "..."}</code>\n\n'
                "<i>A dry-run preview will be shown for your confirmation before applying.</i>",
                reply_markup=build_input_cancel_keyboard(record.id),
            )
        await callback.answer()
        return

    # 12. Confirm & Apply Dry-Run Proposal
    if action == "confirm_apply":
        fsm_data = await state.get_data()
        raw_proposal = fsm_data.get("pending_song_proposal")
        if not isinstance(raw_proposal, dict):
            await callback.answer("No pending changes to apply.", show_alert=True)
            return

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
        )
        actor_id = current_user.telegram_id if current_user is not None else 0
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
        has_lrc = effective_rec.storage_path.with_suffix(".lrc").is_file()
        has_cover = (effective_rec.storage_path.parent / "cover.jpg").is_file()
        if msg is not None:
            await msg.edit_text(
                "✅ <b>Changes Applied Successfully!</b>\n\n"
                + format_song_detail_card(
                    effective_rec,
                    has_lrc_file=has_lrc,
                    has_cover_file=has_cover,
                ),
                reply_markup=build_song_action_keyboard(effective_rec.id),
            )
        await callback.answer("Changes applied!")
        return

    await callback.answer()


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
        await message.answer("⚠️ Song no longer exists in the database.")
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
        await message.answer("⚠️ Song no longer exists in the database.")
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
            "⚠️ Please paste non-empty lyrics text or upload a <code>.lrc</code>/<code>.txt</code> file.",
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
        await message.answer("⚠️ Song no longer exists in the database.")
        await state.clear()
        return

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
            "⚠️ Could not read cover image. Please send a Photo, an image file, "
            "or a valid <code>https://</code> image URL.",
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
        await message.answer("⚠️ Song no longer exists in the database.")
        await state.clear()
        return

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
        await message.answer("⚠️ Song no longer exists in the database.")
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
            "⚠️ Invalid JSON payload. Please send a valid JSON object or <code>.json</code> file.",
            reply_markup=build_input_cancel_keyboard(record.id),
        )
        return

    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        await message.answer(
            "⚠️ JSON payload must be an object containing song metadata fields.",
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
            "⚠️ <b>Pending Dry-Run Preview</b>\n"
            "Please tap <b>✅ Confirm &amp; Apply Changes</b> or <b>❌ Cancel</b> below "
            "(or send <code>/cancel</code> to discard):",
            reply_markup=build_dry_run_confirmation_keyboard(track_id),
        )
        return

    await handle_songs_search_text(
        message,
        state=state,
        current_user=current_user,
        governance_repository=governance_repository,
    )
