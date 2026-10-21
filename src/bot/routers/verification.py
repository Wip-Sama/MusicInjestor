"""Callback query and FSM handlers for metadata confirmation, manual search, and full-pipeline job tagging."""

import contextlib
import html
import logging
from collections.abc import Mapping
from pathlib import Path

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.api_server import resolve_local_api_file_path
from src.bot.keyboards.admin import RESERVED_MENU_BUTTONS, build_back_to_home_keyboard
from src.bot.keyboards.verification import (
    build_job_edit_keyboard,
    build_job_input_cancel_keyboard,
    build_job_lyrics_prompt_keyboard,
    build_manual_search_keyboard,
    build_verification_keyboard,
)
from src.bot.routers.files import format_ingestion_summary, render_job_stage_progress
from src.bot.routers.songs import (
    _is_non_command_text,
    _is_non_media_input_message,
    _resolve_lyrics_client,
    _resolve_metadata_service,
    _resolve_organizer,
)
from src.bot.states import IngestionStates
from src.bot.ui import (
    _read_fsm_dict,
    ack_callback,
    format_loading_card,
    format_screen,
    reset_fsm_state,
    resolve_back_callback,
    safe_edit_text,
    send_chat_action_safe,
)
from src.config.settings import AppSettings
from src.core.exceptions import PermissionDeniedError
from src.core.metadata import _MB_URL_RE, normalize_primary_artist
from src.core.models import (
    IngestionJob,
    IngestionResult,
    LyricsData,
    Permission,
    TrackMetadata,
    UserAccount,
)
from src.core.organizer import LibraryOrganizer, is_synced_lyrics_text
from src.core.pipeline import IngestionPipeline
from src.core.queue import IngestionQueueWorker
from src.core.rbac import has_permission

logger = logging.getLogger(__name__)

verification_router = Router(name="verification")


def _can_resolve_job(user: UserAccount | None, job: IngestionJob | None) -> bool:
    """Return True if `user` is permitted to disambiguate `job` (own vs others' jobs)."""
    if user is None:
        return True
    if job is not None and job.user_id is not None and job.user_id != user.telegram_id:
        return has_permission(user, Permission.RESOLVE_OTHERS_JOBS)
    return has_permission(user, Permission.RESOLVE_OWN_DISAMBIGUATION)


def parse_verification_callback(data: str) -> tuple[str, str, int | None]:
    """Parse callback strings of the form `verify:<action>:<job_id>[:<candidate_index>]`."""
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "verify":
        msg = f"Malformed verification callback data: {data}"
        raise ValueError(msg)

    action = parts[1]
    job_id = parts[2]
    if action == "select":
        if len(parts) != 4:
            msg = f"Missing candidate index in callback data: {data}"
            raise ValueError(msg)
        return action, job_id, int(parts[3])
    return action, job_id, None


def _seed_metadata_for_job(job: IngestionJob) -> TrackMetadata:
    """Resolve the best available seed `TrackMetadata` from `job`."""
    if job.selected_metadata is not None:
        return job.selected_metadata
    if job.candidates:
        return job.candidates[0].metadata
    stem = Path(job.original_filename or job.source_path.name).stem
    return TrackMetadata(
        title=stem or "Unknown Title",
        artist="Unknown Artist",
        album="Unknown Album",
        track_number=1,
    )


def _ensure_job_proposal(
    job: IngestionJob,
    existing: object,
) -> dict[str, object]:
    """Return a normalized `job_edit_proposal` dict for `job.job_id`."""
    if isinstance(existing, dict) and str(existing.get("job_id")) == job.job_id:
        return dict(existing)
    seed = _seed_metadata_for_job(job)
    return {
        "job_id": job.job_id,
        "new_title": seed.title,
        "new_artist": seed.artist,
        "new_album": seed.album,
        "new_year": seed.year,
        "new_track_number": seed.track_number or 1,
        "new_duration_seconds": seed.duration_seconds,
        "new_recording_mbid": seed.recording_mbid,
        "new_release_mbid": seed.release_mbid,
        "new_lyrics": None,
        "new_cover_hex": None,
        "new_cover_size": None,
        "new_cover_mime": "image/jpeg",
    }


def _parse_job_metadata_input(
    raw_text: str,
    target_field: str,
    proposal: dict[str, object],
) -> dict[str, object]:
    """Update `proposal` from single-field or multi-field (`artist: ... | title: ...`) user input."""
    updated = dict(proposal)
    cleaned = raw_text.strip()
    if target_field == "title":
        updated["new_title"] = cleaned
        return updated
    if target_field == "artist":
        updated["new_artist"] = normalize_primary_artist(cleaned)
        return updated
    if target_field == "album":
        updated["new_album"] = cleaned
        return updated
    if target_field == "year":
        digits = "".join(ch for ch in cleaned if ch.isdigit())
        if len(digits) == 4:
            updated["new_year"] = int(digits)
        return updated
    if target_field == "track":
        digits = "".join(ch for ch in cleaned if ch.isdigit())
        if digits:
            updated["new_track_number"] = int(digits)
        return updated

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
                updated["new_title"] = val
            elif key in {"artist", "a", "singer"}:
                updated["new_artist"] = normalize_primary_artist(val)
            elif key in {"album", "b", "release"}:
                updated["new_album"] = val
            elif key in {"year", "date", "y"} and val.isdigit():
                updated["new_year"] = int(val)
            elif key in {"track", "trk", "number", "n"} and val.isdigit():
                updated["new_track_number"] = int(val)

    if not matched_kv:
        if " - " in cleaned:
            art_part, title_part = cleaned.split(" - ", maxsplit=1)
            if art_part.strip():
                updated["new_artist"] = normalize_primary_artist(art_part.strip())
            if title_part.strip():
                updated["new_title"] = title_part.strip()
        else:
            updated["new_title"] = cleaned
    return updated


def format_job_edit_dry_run_card(
    job: IngestionJob,
    proposal: Mapping[str, object],
    organizer: LibraryOrganizer,
) -> str:
    """Render the full-pipeline Dry-Run Preview & Tag Editor card for a queued/verification job."""
    seed = _seed_metadata_for_job(job)
    new_title = str(proposal.get("new_title") or seed.title)
    new_artist = str(proposal.get("new_artist") or seed.artist)
    new_album = str(proposal.get("new_album") or seed.album)
    raw_year = proposal.get("new_year")
    new_year = int(str(raw_year)) if raw_year is not None and str(raw_year).isdigit() else seed.year
    raw_trk = proposal.get("new_track_number")
    new_trk = (
        int(str(raw_trk)) if raw_trk is not None and str(raw_trk).isdigit() else (seed.track_number or 1)
    )

    target_meta = TrackMetadata(
        title=new_title,
        artist=new_artist,
        album=new_album,
        album_artist=new_artist,
        year=new_year,
        track_number=new_trk,
        duration_seconds=seed.duration_seconds,
    )
    suffix = job.source_path.suffix if job.source_path.suffix else ".flac"
    target_path = organizer.build_target_audio_path(target_meta, suffix)
    payload_name = html.escape(job.original_filename or job.source_path.name)

    new_lyrics = proposal.get("new_lyrics")
    if isinstance(new_lyrics, str) and new_lyrics.strip():
        line_count = len(new_lyrics.strip().splitlines())
        kind = "Synced LRC" if ("[" in new_lyrics and "]" in new_lyrics) else "Plain Text"
        lyrics_status = f"<code>Custom ({kind}, {line_count} lines)</code>"
    else:
        lyrics_status = "<code>Auto-fetch via LRCLIB + .lrc sidecar</code>"

    cover_size = proposal.get("new_cover_size")
    if cover_size is not None:
        kb = max(1, int(str(cover_size)) // 1024)
        cover_status = f"<code>Custom image ({kb} KB)</code>"
    elif job.scratch_dir and (job.scratch_dir / "cover.jpg").is_file():
        cover_status = "<code>Staged archive cover.jpg</code>"
    else:
        cover_status = "<code>Embedded picture / Cover Art Archive</code>"

    context_lines = [
        f"• Source Payload: <code>{payload_name}</code>",
        f"• Title: <code>{html.escape(new_title)}</code>",
        f"• Artist: <code>{html.escape(new_artist)}</code>",
        f"• Album: <code>{html.escape(new_album)}</code>",
        f"• Year: <code>{new_year or 'Unknown'}</code> • Track #: <code>{new_trk:02d}</code>",
        f"• Lyrics: {lyrics_status}",
        f"• Cover Art: {cover_status}",
        f"• Target Library Path:\n  ➡️ <code>{html.escape(str(target_path))}</code>",
    ]
    return format_screen(
        title="🧪 Queued Job Tag Editor &amp; Dry-Run Preview",
        context_lines=context_lines,
        footer_lines=[
            f"<i>Job ID:</i> <code>{html.escape(job.job_id)}</code> • "
            "<i>Edit any field or tap Confirm &amp; Ingest Track below</i>"
        ],
    )


@verification_router.callback_query(F.data.startswith("verify:"))
async def handle_verification_callback(
    callback: CallbackQuery,
    state: FSMContext | None = None,
    current_user: UserAccount | None = None,
    pipeline: IngestionPipeline | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Process user button selections for metadata candidates and full-pipeline manual tagging in-place."""
    if not callback.data:
        await ack_callback(callback, "Invalid selection.", show_alert=True)
        return

    action, job_id, candidate_index = parse_verification_callback(callback.data)
    logger.info(
        "Received verification callback: action=%s, job_id=%s, candidate_index=%s",
        action,
        job_id,
        candidate_index,
    )

    if pipeline is None:
        await ack_callback(callback, f"Action '{action}' registered for job {job_id}.")
        return

    target_job = await pipeline.get_job(job_id)
    if not _can_resolve_job(current_user, target_job):
        await ack_callback(
            callback,
            "🔒 You do not have permission to resolve another user's job.",
            show_alert=True,
        )
        return

    msg = callback.message
    organizer = _resolve_organizer(pipeline, settings)

    if action == "select" and candidate_index is not None:
        if target_job is None or candidate_index < 0 or candidate_index >= len(target_job.candidates):
            await ack_callback(
                callback, "Could not confirm release: candidate out of range.", show_alert=True
            )
            return

        await ack_callback(callback, "Release confirmed and ingested!")
        await send_chat_action_safe(callback)
        await safe_edit_text(
            msg,
            format_loading_card(
                "Finalizing Ingestion",
                "Fetching synchronized lyrics and writing audio tags &amp; cover art...",
                f"Job ID: {html.escape(job_id)}",
            ),
        )

        async def _update_bot_stage(staged_job: IngestionJob) -> None:
            with contextlib.suppress(Exception):
                await safe_edit_text(msg, render_job_stage_progress(staged_job))

        try:
            result = await pipeline.confirm_candidate(
                job_id=job_id,
                candidate_index=candidate_index,
                stage_callback=_update_bot_stage,
            )
        except (KeyError, IndexError) as exc:
            await ack_callback(callback, f"Could not confirm release: {exc}", show_alert=True)
            return
        except Exception as exc:
            logger.error("Failed to finalize job %s: %s", job_id, exc, exc_info=True)
            back_cb = await resolve_back_callback(
                state,
                msg,
                f"queue:review:{job_id}",
                default_back="menu:home",
            )
            await reset_fsm_state(state)
            await safe_edit_text(
                msg,
                format_screen(
                    title="⚠️ Ingestion Finalization Failed",
                    context_lines=[
                        "An error occurred while finalizing track ingestion:\n"
                        f"<code>{html.escape(str(exc))}</code>",
                        "The job has been marked as failed.",
                    ],
                    footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
                ),
                reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
            )
            return

        if queue_worker is not None:
            await queue_worker.record_confirmed_completion(result)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:review:{job_id}",
            default_back="menu:home",
        )
        await reset_fsm_state(state)

        summary = format_ingestion_summary(result)
        await safe_edit_text(msg, summary, reply_markup=build_back_to_home_keyboard(back_callback=back_cb))
        return

    if action == "cancel":
        try:
            if queue_worker is not None and current_user is not None:
                await queue_worker.cancel_job(job_id, current_user)
            else:
                await pipeline.cancel_job(job_id)
        except (KeyError, PermissionDeniedError) as exc:
            await ack_callback(callback, str(exc), show_alert=True)
            return

        await ack_callback(callback, "Job cancelled.")
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:review:{job_id}",
            default_back="menu:home",
        )
        await reset_fsm_state(state)
        await safe_edit_text(
            msg,
            format_screen(
                title="🛑 Job Cancelled",
                context_lines=["Temporary staging files have been cleaned up."],
                footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
            ),
            reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
        )
        return

    if action == "manual":
        await ack_callback(callback, "Enter manual search query or link.")
        if state is not None:
            await state.set_state(IngestionStates.awaiting_manual_query)
            await state.update_data(job_id=job_id)
        await safe_edit_text(
            msg,
            format_screen(
                title="🔎 Manual Metadata Search",
                context_lines=[
                    "Reply with any of the following:",
                    "• MusicBrainz URL or MBID (<code>https://musicbrainz.org/release/...</code>)",
                    "• <code>Artist - Title</code> or <code>Artist - Title | Album</code>",
                    "• Primary voice actor / singer name (e.g. <code>Rie Takahashi</code>)",
                    "• <code>isrc:JPD731600777</code> or <code>artist: Name | album: Title</code>",
                ],
                footer_lines=[
                    f"<i>Job ID:</i> <code>{html.escape(job_id)}</code> • "
                    "<i>🧭 Home › Verification › Manual Search</i>"
                ],
            ),
            reply_markup=build_manual_search_keyboard(job_id),
        )
        return

    if action == "edit":
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        await ack_callback(callback)
        fsm_data = await _read_fsm_dict(state)
        proposal = _ensure_job_proposal(target_job, fsm_data.get("job_edit_proposal"))
        if state is not None:
            await state.set_state(IngestionStates.awaiting_job_metadata_input)
            await state.update_data(
                job_id=job_id,
                job_edit_field="multi",
                job_edit_proposal=proposal,
            )
        await safe_edit_text(
            msg,
            format_job_edit_dry_run_card(target_job, proposal, organizer),
            reply_markup=build_job_edit_keyboard(job_id),
        )
        return

    if action == "field":
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        parts = callback.data.split(":", maxsplit=3)
        field_name = parts[3] if len(parts) >= 4 else "multi"
        await ack_callback(callback)
        fsm_data = await _read_fsm_dict(state)
        proposal = _ensure_job_proposal(target_job, fsm_data.get("job_edit_proposal"))
        if state is not None:
            await state.set_state(IngestionStates.awaiting_job_metadata_input)
            await state.update_data(
                job_id=job_id,
                job_edit_field=field_name,
                job_edit_proposal=proposal,
            )
        field_labels = {
            "title": ("Title", str(proposal.get("new_title") or "")),
            "artist": ("Artist (Primary Singer)", str(proposal.get("new_artist") or "")),
            "album": ("Album", str(proposal.get("new_album") or "")),
            "year": ("Release Year (YYYY)", str(proposal.get("new_year") or "")),
            "track": ("Track Number", str(proposal.get("new_track_number") or 1)),
            "multi": (
                "Multiple Fields (artist: ... | title: ... | album: ... | year: ...)",
                f"artist: {proposal.get('new_artist')} | title: {proposal.get('new_title')} "
                f"| album: {proposal.get('new_album')}",
            ),
            "link": ("MusicBrainz URL, MBID, or Query", "https://musicbrainz.org/release/..."),
        }
        label, current_val = field_labels.get(field_name, ("Value", str(proposal.get("new_title") or "")))
        await safe_edit_text(
            msg,
            format_screen(
                title=f"✏️ Enter New {html.escape(label)}",
                context_lines=[
                    f"Current value: <code>{html.escape(current_val)}</code>",
                    "Reply with the new value below (a dry-run preview will be shown before ingesting).",
                ],
                footer_lines=[
                    f"<i>Job ID:</i> <code>{html.escape(job_id)}</code> • <i>🧭 Home › Queue › Tag Editor</i>"
                ],
            ),
            reply_markup=build_job_input_cancel_keyboard(job_id),
        )
        return

    if action == "cover":
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        await ack_callback(callback)
        fsm_data = await _read_fsm_dict(state)
        proposal = _ensure_job_proposal(target_job, fsm_data.get("job_edit_proposal"))
        if state is not None:
            await state.set_state(IngestionStates.awaiting_job_cover_input)
            await state.update_data(job_id=job_id, job_edit_proposal=proposal)
        await safe_edit_text(
            msg,
            format_screen(
                title="🖼️ Set Cover Art for Queued Job",
                context_lines=[
                    "Send a Photo, an image Document (<code>.jpg</code> / <code>.png</code>), "
                    "or paste a direct Image URL (<code>https://...</code>).",
                    "It will be written to <code>cover.jpg</code> and embedded in the audio "
                    "file upon ingestion.",
                ],
                footer_lines=[
                    f"<i>Job ID:</i> <code>{html.escape(job_id)}</code> • "
                    "<i>🧭 Home › Queue › Tag Editor › Cover</i>"
                ],
            ),
            reply_markup=build_job_input_cancel_keyboard(job_id),
        )
        return

    if action == "lyrics":
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        await ack_callback(callback)
        fsm_data = await _read_fsm_dict(state)
        proposal = _ensure_job_proposal(target_job, fsm_data.get("job_edit_proposal"))
        if state is not None:
            await state.set_state(IngestionStates.awaiting_job_lyrics_input)
            await state.update_data(job_id=job_id, job_edit_proposal=proposal)
        await safe_edit_text(
            msg,
            format_screen(
                title="📝 Set Lyrics for Queued Job",
                context_lines=[
                    "Send a <code>.lrc</code> or <code>.txt</code> file, or paste lyrics text "
                    "(timestamped <code>[mm:ss.xx]</code> or plain text).",
                    "Both embedded audio lyrics tags and the sidecar <code>.lrc</code> file "
                    "will be written upon ingestion.",
                ],
                footer_lines=[
                    f"<i>Job ID:</i> <code>{html.escape(job_id)}</code> • "
                    "<i>🧭 Home › Queue › Tag Editor › Lyrics</i>"
                ],
            ),
            reply_markup=build_job_lyrics_prompt_keyboard(job_id),
        )
        return

    if action == "auto_lyrics":
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        await ack_callback(callback, "Searching LRCLIB...")
        await send_chat_action_safe(callback)
        fsm_data = await _read_fsm_dict(state)
        proposal = _ensure_job_proposal(target_job, fsm_data.get("job_edit_proposal"))
        escaped_title = html.escape(str(proposal.get("new_title") or ""))
        await safe_edit_text(
            msg,
            format_loading_card(
                "Querying LRCLIB Lyrics",
                f"Looking up lyrics for <code>{escaped_title}</code>...",
                f"Job ID: {html.escape(job_id)}",
            ),
        )
        lyrics_client = _resolve_lyrics_client(pipeline, settings)
        search_meta = TrackMetadata(
            title=str(proposal.get("new_title") or ""),
            artist=str(proposal.get("new_artist") or ""),
            album=str(proposal.get("new_album") or ""),
            year=(
                int(str(proposal["new_year"]))
                if proposal.get("new_year") is not None and str(proposal.get("new_year")).isdigit()
                else None
            ),
            track_number=int(str(proposal.get("new_track_number") or 1)),
        )
        fetched_lyrics = None
        try:
            fetched_lyrics = await lyrics_client.fetch_lyrics(search_meta)
        except Exception as exc:
            logger.debug("Failed to auto-search lyrics for job %s: %s", job_id, exc)

        lyrics_text = (
            (fetched_lyrics.synced_lyrics or fetched_lyrics.plain_lyrics)
            if fetched_lyrics is not None
            else None
        )
        if lyrics_text and lyrics_text.strip():
            proposal["new_lyrics"] = lyrics_text
            if state is not None:
                await state.set_state(IngestionStates.awaiting_job_metadata_input)
                await state.update_data(job_id=job_id, job_edit_field="multi", job_edit_proposal=proposal)
            await safe_edit_text(
                msg,
                format_job_edit_dry_run_card(target_job, proposal, organizer),
                reply_markup=build_job_edit_keyboard(job_id),
            )
            return

        await safe_edit_text(
            msg,
            format_screen(
                title="⚠️ No Lyrics Found",
                context_lines=[
                    f"No matching lyrics were found on LRCLIB for <code>{escaped_title}</code>.",
                    "You can paste lyrics manually or upload a <code>.lrc</code>/<code>.txt</code> file.",
                ],
                footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
            ),
            reply_markup=build_job_lyrics_prompt_keyboard(job_id),
        )
        return

    if action == "auto_enrich":
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        await ack_callback(callback, "⚡ Enriched tags & lyrics loaded into preview.")
        await send_chat_action_safe(callback)
        fsm_data = await _read_fsm_dict(state)
        proposal = _ensure_job_proposal(target_job, fsm_data.get("job_edit_proposal"))
        escaped_title = html.escape(str(proposal.get("new_title") or ""))
        await safe_edit_text(
            msg,
            format_loading_card(
                "Querying MusicBrainz, Genius, Last.fm &amp; LRCLIB",
                f"Enriching metadata and lyrics for <code>{escaped_title}</code>...",
                f"Job ID: {html.escape(job_id)}",
            ),
        )
        meta_svc = _resolve_metadata_service(pipeline, settings)
        lyrics_client = _resolve_lyrics_client(pipeline, settings)
        seed_meta = TrackMetadata(
            title=str(proposal.get("new_title") or ""),
            artist=str(proposal.get("new_artist") or ""),
            album=str(proposal.get("new_album") or ""),
            year=(
                int(str(proposal["new_year"]))
                if proposal.get("new_year") is not None and str(proposal.get("new_year")).isdigit()
                else None
            ),
            track_number=int(str(proposal.get("new_track_number") or 1)),
        )
        candidates = await meta_svc.find_candidates(seed_meta)
        if candidates:
            target_job.candidates = candidates
            best = candidates[0].metadata
            proposal["new_title"] = best.title
            proposal["new_artist"] = best.artist
            proposal["new_album"] = best.album
            proposal["new_year"] = best.year
            proposal["new_track_number"] = best.track_number or proposal.get("new_track_number") or 1
            proposal["new_recording_mbid"] = best.recording_mbid
            proposal["new_release_mbid"] = best.release_mbid
            fetched_lyrics = await lyrics_client.fetch_lyrics(best)
            if fetched_lyrics is not None:
                proposal["new_lyrics"] = fetched_lyrics.synced_lyrics or fetched_lyrics.plain_lyrics
        if state is not None:
            await state.set_state(IngestionStates.awaiting_job_metadata_input)
            await state.update_data(
                job_id=job_id,
                job_edit_field="multi",
                job_edit_proposal=proposal,
            )
        await safe_edit_text(
            msg,
            format_job_edit_dry_run_card(target_job, proposal, organizer),
            reply_markup=build_job_edit_keyboard(job_id),
        )
        return

    if action == "link_search":
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        await ack_callback(callback)
        fsm_data = await _read_fsm_dict(state)
        proposal = _ensure_job_proposal(target_job, fsm_data.get("job_edit_proposal"))
        if state is not None:
            await state.set_state(IngestionStates.awaiting_job_metadata_input)
            await state.update_data(
                job_id=job_id,
                job_edit_field="link",
                job_edit_proposal=proposal,
            )
        await safe_edit_text(
            msg,
            format_screen(
                title="🔗 Search Metadata by Link or Query",
                context_lines=[
                    "Reply with any of the following to update tags &amp; lyrics:",
                    "• MusicBrainz URL (<code>https://musicbrainz.org/release/...</code>)",
                    "• Recording URL (<code>https://musicbrainz.org/recording/...</code>)",
                    "• <code>Artist - Title</code> or <code>Artist - Title | Album</code>",
                    "• <code>isrc:JPD731600777</code> or MBID UUID",
                    "Resolved metadata &amp; lyrics will be loaded directly into your Tag Editor.",
                ],
                footer_lines=[
                    f"<i>Job ID:</i> <code>{html.escape(job_id)}</code> • "
                    "<i>🧭 Home › Queue › Tag Editor › Link Search</i>"
                ],
            ),
            reply_markup=build_job_input_cancel_keyboard(job_id),
        )
        return

    if action == "apply_edit":
        if organizer.is_reconciling:
            await ack_callback(
                callback,
                "🔒 Library reconciliation is running in the background. Finalizing is temporarily paused.",
                show_alert=True,
            )
            return
        if target_job is None:
            await ack_callback(callback, "Job not found.", show_alert=True)
            return
        fsm_data = await _read_fsm_dict(state)
        proposal = _ensure_job_proposal(target_job, fsm_data.get("job_edit_proposal"))
        await ack_callback(callback, "✅ Ingesting track with custom tags!")
        await send_chat_action_safe(callback)
        await safe_edit_text(
            msg,
            format_loading_card(
                "Finalizing Custom Ingestion",
                "Writing custom metadata, lyrics, and cover art to library...",
                f"Job ID: {html.escape(job_id)}",
            ),
        )
        seed = _seed_metadata_for_job(target_job)
        raw_year = proposal.get("new_year")
        new_year = int(str(raw_year)) if raw_year is not None and str(raw_year).isdigit() else seed.year
        raw_trk = proposal.get("new_track_number")
        new_trk = (
            int(str(raw_trk)) if raw_trk is not None and str(raw_trk).isdigit() else (seed.track_number or 1)
        )
        custom_meta = TrackMetadata(
            title=str(proposal.get("new_title") or seed.title),
            artist=str(proposal.get("new_artist") or seed.artist),
            album=str(proposal.get("new_album") or seed.album),
            album_artist=str(proposal.get("new_artist") or seed.artist),
            year=new_year,
            track_number=new_trk,
            duration_seconds=seed.duration_seconds,
            recording_mbid=(
                str(proposal["new_recording_mbid"]) if proposal.get("new_recording_mbid") else None
            ),
            release_mbid=(str(proposal["new_release_mbid"]) if proposal.get("new_release_mbid") else None),
        )
        lyrics_obj: LyricsData | None = None
        raw_lyr = proposal.get("new_lyrics")
        if isinstance(raw_lyr, str) and raw_lyr.strip():
            cleaned_lyr = raw_lyr.strip()
            is_synced = is_synced_lyrics_text(cleaned_lyr)
            lyrics_obj = LyricsData(
                synced_lyrics=cleaned_lyr if is_synced else None,
                plain_lyrics=None if is_synced else cleaned_lyr,
            )
        cover_bytes: bytes | None = None
        cover_hex = proposal.get("new_cover_hex")
        if isinstance(cover_hex, str) and cover_hex:
            cover_bytes = bytes.fromhex(cover_hex)
        cover_mime = str(proposal.get("new_cover_mime") or "image/jpeg")

        async def _update_custom_stage(staged_job: IngestionJob) -> None:
            with contextlib.suppress(Exception):
                await safe_edit_text(msg, render_job_stage_progress(staged_job))

        try:
            result = await pipeline.complete_custom_ingestion(
                job_id=job_id,
                metadata=custom_meta,
                custom_lyrics=lyrics_obj,
                custom_cover_bytes=cover_bytes,
                custom_cover_mime=cover_mime,
                stage_callback=_update_custom_stage,
            )
        except Exception as exc:
            logger.error("Failed to finalize custom job %s: %s", job_id, exc, exc_info=True)
            back_cb = await resolve_back_callback(
                state,
                msg,
                f"queue:review:{job_id}",
                default_back="menu:home",
            )
            await reset_fsm_state(state)
            await safe_edit_text(
                msg,
                format_screen(
                    title="⚠️ Ingestion Finalization Failed",
                    context_lines=[
                        "An error occurred while finalizing custom track ingestion:\n"
                        f"<code>{html.escape(str(exc))}</code>",
                        "The job has been marked as failed.",
                    ],
                    footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
                ),
                reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
            )
            return

        if queue_worker is not None:
            await queue_worker.record_confirmed_completion(result)
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:review:{job_id}",
            default_back="menu:home",
        )
        await reset_fsm_state(state)
        await safe_edit_text(
            msg,
            format_ingestion_summary(result),
            reply_markup=build_back_to_home_keyboard(back_callback=back_cb),
        )
        return

    if action == "back":
        await ack_callback(callback)
        if target_job is None:
            await reset_fsm_state(state)
            await safe_edit_text(
                msg,
                format_screen(
                    title="⚠️ Job Unavailable",
                    context_lines=["This verification job is no longer active."],
                    footer_lines=["<i>🧭 Home › Verification</i>"],
                ),
                reply_markup=build_back_to_home_keyboard(),
            )
            return
        back_cb = await resolve_back_callback(
            state,
            msg,
            f"queue:review:{job_id}",
            default_back=f"queue:view:{job_id}",
        )
        if state is not None:
            await state.set_state(IngestionStates.awaiting_confirmation)
            await state.update_data(job_id=job_id)
        summary = format_ingestion_summary(IngestionResult(job=target_job, requires_user_confirmation=True))
        await safe_edit_text(
            msg,
            summary,
            reply_markup=build_verification_keyboard(
                job_id,
                target_job.candidates,
                back_callback=back_cb,
            ),
        )
        return

    await ack_callback(callback, f"Action '{action}' registered for job {job_id}.")


@verification_router.message(
    IngestionStates.awaiting_manual_query,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(RESERVED_MENU_BUTTONS),
)
async def handle_manual_search_query(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    pipeline: IngestionPipeline | None = None,
) -> None:
    """Re-query MusicBrainz candidates using the user's manual search query or link."""
    data = await state.get_data()
    job_id = str(data.get("job_id", "")).strip()
    if not job_id or pipeline is None:
        await reset_fsm_state(state)
        await message.answer(
            format_screen(
                title="⚠️ Verification Session Expired",
                context_lines=["No active verification job found for manual search."],
                footer_lines=["<i>🧭 Home › Verification</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(),
        )
        return

    target_job = await pipeline.get_job(job_id)
    if not _can_resolve_job(current_user, target_job):
        await reset_fsm_state(state)
        await message.answer(
            format_screen(
                title="🔒 Permission Denied",
                context_lines=["You do not have permission to resolve another user's job."],
                footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
            )
        )
        return

    query_text = (message.text or "").strip()
    if not query_text:
        await message.answer(
            format_screen(
                title="⚠️ Empty Search Query",
                context_lines=[
                    "Please reply with a MusicBrainz URL/MBID, <code>Artist - Title</code>, "
                    "or <code>Artist - Title | Album</code>."
                ],
                footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
            ),
            reply_markup=build_manual_search_keyboard(job_id),
        )
        return

    await send_chat_action_safe(message)
    try:
        result = await pipeline.search_manual_candidates(job_id, query_text)
    except KeyError:
        await reset_fsm_state(state)
        await message.answer(
            format_screen(
                title="⚠️ Job Unavailable",
                context_lines=[f"Job <code>{html.escape(job_id)}</code> is no longer available."],
                footer_lines=["<i>🧭 Home › Verification</i>"],
            ),
            reply_markup=build_back_to_home_keyboard(),
        )
        return

    back_cb = await resolve_back_callback(
        state,
        None,
        f"queue:review:{job_id}",
        default_back=f"queue:view:{job_id}",
    )
    await state.set_state(IngestionStates.awaiting_confirmation)
    await message.answer(
        format_ingestion_summary(result),
        reply_markup=build_verification_keyboard(
            job_id,
            result.job.candidates,
            back_callback=back_cb,
        ),
    )


@verification_router.message(
    IngestionStates.awaiting_job_metadata_input,
    _is_non_command_text,
)
async def handle_job_metadata_input(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Update the queued job's dry-run metadata proposal and display the updated preview card."""
    data = await state.get_data()
    job_id = str(data.get("job_id") or "").strip()
    target_field = str(data.get("job_edit_field") or "multi")
    if not job_id or pipeline is None:
        await reset_fsm_state(state)
        return
    target_job = await pipeline.get_job(job_id)
    if target_job is None or not _can_resolve_job(current_user, target_job):
        await reset_fsm_state(state)
        return

    proposal = _ensure_job_proposal(target_job, data.get("job_edit_proposal"))
    raw_text = (message.text or "").strip()
    is_link_target = target_field == "link"
    is_mb_link = bool(_MB_URL_RE.search(raw_text))

    if is_link_target or is_mb_link:
        await send_chat_action_safe(message)
        try:
            result = await pipeline.search_manual_candidates(job_id, raw_text)
            if result.job.candidates:
                best = result.job.candidates[0].metadata
                proposal["new_title"] = best.title
                proposal["new_artist"] = best.artist
                proposal["new_album"] = best.album
                proposal["new_year"] = best.year
                proposal["new_track_number"] = best.track_number or proposal.get("new_track_number") or 1
                proposal["new_recording_mbid"] = best.recording_mbid
                proposal["new_release_mbid"] = best.release_mbid
                target_job.candidates = result.job.candidates
                target_job.selected_metadata = best

                lyrics_client = _resolve_lyrics_client(pipeline, settings)
                try:
                    fetched_lyrics = await lyrics_client.fetch_lyrics(best)
                    if fetched_lyrics is not None:
                        proposal["new_lyrics"] = fetched_lyrics.synced_lyrics or fetched_lyrics.plain_lyrics
                except Exception as lex:
                    logger.debug("Lyrics fetch during link search failed: %s", lex)

                await state.update_data(job_edit_field="multi", job_edit_proposal=proposal)
                organizer = _resolve_organizer(pipeline, settings)
                await message.answer(
                    format_job_edit_dry_run_card(target_job, proposal, organizer),
                    reply_markup=build_job_edit_keyboard(job_id),
                )
                return
            if is_link_target:
                await message.answer(
                    format_screen(
                        title="⚠️ No Matches Found",
                        context_lines=[
                            f"No MusicBrainz matches were found for:\n<code>{html.escape(raw_text)}</code>",
                            "Please check the URL or try searching with <code>Artist - Title</code>.",
                        ],
                        footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
                    ),
                    reply_markup=build_job_input_cancel_keyboard(job_id),
                )
                return
        except Exception as exc:
            logger.warning("Link search failed for job %s: %s", job_id, exc)
            if is_link_target:
                await message.answer(
                    format_screen(
                        title="⚠️ Search Error",
                        context_lines=[
                            f"Failed to lookup metadata: <code>{html.escape(str(exc))}</code>",
                            "Please try again or tap Back.",
                        ],
                        footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
                    ),
                    reply_markup=build_job_input_cancel_keyboard(job_id),
                )
                return

    proposal = _parse_job_metadata_input(raw_text, target_field, proposal)
    await state.update_data(job_edit_field="multi", job_edit_proposal=proposal)
    organizer = _resolve_organizer(pipeline, settings)
    await message.answer(
        format_job_edit_dry_run_card(target_job, proposal, organizer),
        reply_markup=build_job_edit_keyboard(job_id),
    )


@verification_router.message(
    IngestionStates.awaiting_job_lyrics_input,
    _is_non_media_input_message,
)
async def handle_job_lyrics_input(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Accept pasted lyrics or a `.lrc`/`.txt` document for a queued job and return to the Dry-Run Editor."""
    data = await state.get_data()
    job_id = str(data.get("job_id") or "").strip()
    if not job_id or pipeline is None:
        await reset_fsm_state(state)
        return
    target_job = await pipeline.get_job(job_id)
    if target_job is None or not _can_resolve_job(current_user, target_job):
        await reset_fsm_state(state)
        return

    lyrics_text = (message.text or "").strip()
    if not lyrics_text and message.document is not None and message.bot is not None:
        tg_file = await message.bot.get_file(message.document.file_id)
        if tg_file.file_path:
            local_path = (
                resolve_local_api_file_path(tg_file.file_path, settings)
                if settings is not None
                else Path(tg_file.file_path)
            )
            if local_path.is_file():
                lyrics_text = local_path.read_text(encoding="utf-8", errors="replace").strip()

    if not lyrics_text:
        await message.answer(
            format_screen(
                title="⚠️ Empty Lyrics Input",
                context_lines=[
                    "Please paste non-empty lyrics text or upload a <code>.lrc</code>/<code>.txt</code> file."
                ],
                footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
            ),
            reply_markup=build_job_input_cancel_keyboard(job_id),
        )
        return

    proposal = _ensure_job_proposal(target_job, data.get("job_edit_proposal"))
    proposal["new_lyrics"] = lyrics_text
    await state.set_state(IngestionStates.awaiting_job_metadata_input)
    await state.update_data(job_edit_field="multi", job_edit_proposal=proposal)
    organizer = _resolve_organizer(pipeline, settings)
    await message.answer(
        format_job_edit_dry_run_card(target_job, proposal, organizer),
        reply_markup=build_job_edit_keyboard(job_id),
    )


@verification_router.message(
    IngestionStates.awaiting_job_cover_input,
    _is_non_media_input_message,
)
async def handle_job_cover_input(
    message: Message,
    state: FSMContext,
    current_user: UserAccount | None = None,
    pipeline: IngestionPipeline | None = None,
    settings: AppSettings | None = None,
) -> None:
    """Accept a Photo, image Document, or image URL for a queued job and return to the Dry-Run Editor."""
    data = await state.get_data()
    job_id = str(data.get("job_id") or "").strip()
    if not job_id or pipeline is None:
        await reset_fsm_state(state)
        return
    target_job = await pipeline.get_job(job_id)
    if target_job is None or not _can_resolve_job(current_user, target_job):
        await reset_fsm_state(state)
        return

    await send_chat_action_safe(message)
    cover_bytes: bytes | None = None
    mime_type = "image/jpeg"

    if message.photo and message.bot is not None:
        largest = message.photo[-1]
        tg_file = await message.bot.get_file(largest.file_id)
        if tg_file.file_path:
            local_path = (
                resolve_local_api_file_path(tg_file.file_path, settings)
                if settings is not None
                else Path(tg_file.file_path)
            )
            if local_path.is_file():
                cover_bytes = local_path.read_bytes()
    elif message.document is not None and message.bot is not None:
        if message.document.mime_type:
            mime_type = message.document.mime_type
        tg_file = await message.bot.get_file(message.document.file_id)
        if tg_file.file_path:
            local_path = (
                resolve_local_api_file_path(tg_file.file_path, settings)
                if settings is not None
                else Path(tg_file.file_path)
            )
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
                footer_lines=[f"<i>Job ID:</i> <code>{html.escape(job_id)}</code>"],
            ),
            reply_markup=build_job_input_cancel_keyboard(job_id),
        )
        return

    proposal = _ensure_job_proposal(target_job, data.get("job_edit_proposal"))
    proposal["new_cover_hex"] = cover_bytes.hex()
    proposal["new_cover_size"] = len(cover_bytes)
    proposal["new_cover_mime"] = mime_type
    await state.set_state(IngestionStates.awaiting_job_metadata_input)
    await state.update_data(job_edit_field="multi", job_edit_proposal=proposal)
    organizer = _resolve_organizer(pipeline, settings)
    await message.answer(
        format_job_edit_dry_run_card(target_job, proposal, organizer),
        reply_markup=build_job_edit_keyboard(job_id),
    )
