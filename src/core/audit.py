"""Pure domain formatters for Admin Log Channel events, queue status cards, and admin panels."""

import html
from datetime import UTC, datetime
from pathlib import Path

from src.core.models import (
    AccessRequestRecord,
    IngestionJob,
    JobState,
    TrackMetadata,
    UserAccount,
    UserRole,
)


def _format_utc_timestamp(dt: datetime | None = None, include_seconds: bool = True) -> str:
    """Format a UTC datetime into `YYYY-MM-DD HH:MM[:SS] UTC`."""
    resolved = (dt or datetime.now(UTC)).astimezone(UTC)
    fmt = "%Y-%m-%d %H:%M:%S UTC" if include_seconds else "%Y-%m-%d %H:%M UTC"
    return resolved.strftime(fmt)


def format_guest_access_denied_card(telegram_id: int) -> str:
    """Format the Guest Onboarding & Access Denied message (Specification Section 3.2)."""
    return (
        "<b>🔒 Access Denied</b>\n"
        "<blockquote>This is a private music ingestion gateway. "
        "Authenticate via Authentik SSO or submit an access request below.</blockquote>\n"
        f"<i>Telegram ID:</i> <code>{telegram_id}</code>"
    )


def format_access_request_card(user: UserAccount, request: AccessRequestRecord) -> str:
    """Format the Admin Log Channel card for a new manual access request (Section 3.2)."""
    ts = _format_utc_timestamp(request.created_at, include_seconds=False)
    return (
        "<b>🔔 New Access Request</b>\n"
        "<blockquote>"
        f"• User: {html.escape(user.mention_handle)} (ID: <code>{user.telegram_id}</code>)\n"
        f"• Name: <code>{html.escape(user.display_name)}</code>"
        "</blockquote>\n"
        f"<i>Timestamp: {ts}</i>"
    )


def format_role_updated_event(
    target_user: UserAccount,
    actor: UserAccount,
    previous_role: UserRole,
    new_role: UserRole,
    reason: str,
    timestamp: datetime | None = None,
) -> str:
    """Format Event A (`USER AUDIT: ROLE_UPDATED`) for the Admin Log Channel (Section 5.1)."""
    ts = _format_utc_timestamp(timestamp)
    return (
        "<b>🛡 USER AUDIT: ROLE_UPDATED</b>\n"
        "<blockquote expandable>"
        f"• Target User: {html.escape(target_user.mention_handle)} "
        f"(ID: <code>{target_user.telegram_id}</code>)\n"
        f"• Actor: {html.escape(actor.mention_handle)} (ID: <code>{actor.telegram_id}</code>)\n"
        f"• Previous Role: <code>{previous_role.value}</code>\n"
        f"• New Role: <code>{new_role.value}</code>\n"
        f"• Reason: {html.escape(reason)}"
        "</blockquote>\n"
        f"<i>Timestamp: {ts}</i>"
    )


def format_oidc_sync_event(
    target_user: UserAccount,
    group_changes: str,
    new_role: UserRole,
    cancelled_jobs_count: int,
    timestamp: datetime | None = None,
) -> str:
    """Format Event B (`OIDC AUDIT: ROLE_SYNCHRONIZED`) for the Admin Log Channel (Section 5.1)."""
    ts = _format_utc_timestamp(timestamp)
    role_suffix = " (Access Revoked)" if new_role == UserRole.GUEST or target_user.is_blocked else ""
    subject = target_user.authentik_sub or "unlinked"
    return (
        "<b>🔄 OIDC AUDIT: ROLE_SYNCHRONIZED</b>\n"
        "<blockquote expandable>"
        f"• Target User: {html.escape(target_user.mention_handle)} "
        f"(ID: <code>{target_user.telegram_id}</code>)\n"
        f"• Authentik Subject: <code>{html.escape(subject)}</code>\n"
        f"• Group Changes Detected: {html.escape(group_changes)}\n"
        f"• New Assigned Role: <code>{new_role.value}</code>{role_suffix}\n"
        f"• Active Queued Jobs Cancelled: <code>{cancelled_jobs_count}</code>"
        "</blockquote>\n"
        f"<i>Timestamp: {ts}</i>"
    )


def format_ingest_success_event(
    job: IngestionJob,
    uploader: UserAccount,
    metadata: TrackMetadata,
    tracks_written: int,
    audio_format: str,
    lyrics_embedded: int,
    duration_seconds: float,
) -> str:
    """Format Event C (`INGEST AUDIT: SUCCESS`) for the Admin Log Channel (Section 5.1)."""
    year_str = f" ({metadata.year})" if metadata.year else ""
    album_label = html.escape(f"{metadata.effective_album_artist} - {metadata.album}{year_str}")
    target_dir = html.escape(str(job.final_path.parent) if job.final_path is not None else "unknown")
    return (
        "<b>✅ INGEST AUDIT: SUCCESS</b>\n"
        "<blockquote expandable>"
        f"• Uploader: {html.escape(uploader.mention_handle)} (ID: <code>{uploader.telegram_id}</code>)\n"
        f"• Album: {album_label}\n"
        f"• Tracks Written: <code>{tracks_written}</code> files (<code>{html.escape(audio_format)}</code>)\n"
        f"• Target Path: <code>{target_dir}</code>\n"
        f"• Lyrics: <code>{lyrics_embedded}/{tracks_written}</code> embedded (LRCLIB synced)"
        "</blockquote>\n"
        f"<i>Job ID:</i> <code>{html.escape(job.job_id)}</code> • "
        f"<i>Processing Duration: {duration_seconds:.1f} seconds</i>"
    )


def format_duplicate_rejected_event(
    job: IngestionJob,
    uploader: UserAccount,
    detected_track: str,
    conflict_path: Path,
    match_source: str,
) -> str:
    """Format Event D (`INGEST AUDIT: DUPLICATE_REJECTED`) for the Admin Log Channel (Section 5.1)."""
    return (
        "<b>⚠️ INGEST AUDIT: DUPLICATE_REJECTED</b>\n"
        "<blockquote expandable>"
        f"• Uploader: {html.escape(uploader.mention_handle)} (ID: <code>{uploader.telegram_id}</code>)\n"
        f"• Detected Track: {html.escape(detected_track)}\n"
        f"• Conflict Path: <code>{html.escape(str(conflict_path))}</code>\n"
        f"• Match Source: {html.escape(match_source)}\n"
        "• Action Taken: Temporary files purged from scratch space"
        "</blockquote>\n"
        f"<i>Job ID:</i> <code>{html.escape(job.job_id)}</code>"
    )


def _format_file_size_mb(size_bytes: int) -> str:
    """Format byte size into human-readable MB or KB."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.0f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


def format_queued_job_card(
    job: IngestionJob,
    position: int,
    active_job_filename: str | None = None,
) -> str:
    """Format the in-chat queue waiting or paused feedback message (Specification Section 4.2)."""
    filename = html.escape(job.original_filename or job.source_path.name)
    size_label = _format_file_size_mb(job.file_size_bytes)
    if job.state == JobState.PAUSED:
        return (
            "<b>⏸️ Ingestion Paused (Held for Review)</b>\n"
            "<blockquote>"
            f"📦 Payload: <code>{filename}</code> ({size_label})\n"
            "📊 Position in Queue: <code>PAUSED</code> (Held)\n"
            "⚙️ Status: Held for manual review — tap <b>Manual Review</b> to inspect candidates "
            "or <b>Unlock</b> to resume."
            "</blockquote>\n"
            f"<i>Job ID:</i> <code>{html.escape(job.job_id)}</code>"
        )
    status_line = (
        f"Waiting for active job ({html.escape(active_job_filename)}) to finish..."
        if active_job_filename
        else "Queued for next available worker slot..."
    )
    return (
        "<b>⏳ Ingestion Queued</b>\n"
        "<blockquote>"
        f"📦 Payload: <code>{filename}</code> ({size_label})\n"
        f"📊 Position in Queue: #{position}\n"
        f"⚙️ Status: {status_line}"
        "</blockquote>\n"
        f"<i>Job ID:</i> <code>{html.escape(job.job_id)}</code>"
    )


def format_job_inspector_card(
    job: IngestionJob,
    uploader_label: str | None = None,
    position: int = 0,
) -> str:
    """Format an interactive Job Inspector & Control card for a specific `job_id`."""
    filename = html.escape(job.original_filename or job.source_path.name)
    size_label = _format_file_size_mb(job.file_size_bytes)
    created_ts = _format_utc_timestamp(job.created_at, include_seconds=False)
    lines = [
        f"• State: <code>{html.escape(job.state.value)}</code>",
        f"• Payload: <code>{filename}</code> ({size_label})",
        f"• Source Type: <code>{html.escape(job.source_type.value)}</code>",
    ]
    if uploader_label:
        lines.append(f"• Uploader: {html.escape(uploader_label)}")
    if position > 0 and job.state == JobState.QUEUED:
        lines.append(f"• Queue Position: <code>#{position}</code>")
    if job.selected_metadata is not None:
        meta = job.selected_metadata
        lines.append(f"• Selected Match: <b>{html.escape(meta.artist)} — {html.escape(meta.title)}</b>")
        lines.append(f"• Album: {html.escape(meta.album)}")
    elif job.candidates:
        top = job.candidates[0]
        lines.append(
            f"• Top Candidate: {html.escape(top.metadata.artist)} — {html.escape(top.metadata.title)} "
            f"(<code>{round(top.confidence * 100)}%</code>)"
        )
    if job.final_path is not None:
        lines.append(f"• Final Path: <code>{html.escape(str(job.final_path))}</code>")
    if job.error_message:
        lines.append(f"• Details / Error: <code>{html.escape(job.error_message)}</code>")
    lines.append(f"• Created At: <code>{created_ts}</code>")
    if job.completed_at is not None:
        lines.append(
            f"• Completed At: <code>{_format_utc_timestamp(job.completed_at, include_seconds=False)}</code>"
        )

    body = "\n".join(lines)
    return (
        "<b>🔎 Ingestion Job Inspector</b>\n"
        f"<blockquote>{body}</blockquote>\n"
        f"<i>Job ID:</i> <code>{html.escape(job.job_id)}</code> • <i>🧭 Home › Queue › Job Inspector</i>"
    )


def format_ingesting_progress_card(
    filename: str,
    percent: int,
    extraction_status: str,
    acoustid_status: str,
    lyrics_status: str,
    ambiguity_prompt: bool = False,
) -> str:
    """Format the live in-chat progress message while a job is actively processing (Section 4.2)."""
    clamped = max(0, min(100, percent))
    filled = round(clamped / 10)
    bar = "█" * filled + "░" * (10 - filled)
    base = (
        f"<b>⚙️ Ingesting: {filename}</b>\n"
        "<blockquote>"
        f"<code>[{bar}] {clamped}%</code>\n"
        f"• Extraction: {extraction_status}\n"
        f"• AcoustID: {acoustid_status}\n"
        f"• Lyrics: {lyrics_status}"
        "</blockquote>"
    )
    if ambiguity_prompt:
        base += "\n<i>⚠️ Release Ambiguity: Which version is this upload?</i>"
    return base


def format_admin_dashboard_card(
    node_name: str,
    concurrency_limit: int,
    active_users_count: int,
    pending_requests_count: int,
    active_jobs_count: int,
    queued_jobs_count: int,
    memory_used_mb: int = 84,
    memory_total_mb: int = 1024,
) -> str:
    """Format the Administrator Control Panel screen (`/admin`, Specification Section 7.1)."""
    mode_label = "Serial" if concurrency_limit == 1 else "Parallel"
    return (
        "<b>🎛 Administrator Control Panel</b>\n"
        "<blockquote>"
        f"Node: <code>{html.escape(node_name)}</code> | Ingestion Core Engine\n"
        f"Health: 🟢 OK | Memory: <code>{memory_used_mb} MB / {memory_total_mb} MB</code>\n"
        f"Concurrency Limit: <code>{concurrency_limit} ({mode_label})</code>\n"
        f"👥 Users: <code>{active_users_count}</code> Active | "
        f"<code>{pending_requests_count}</code> Pending\n"
        f"⏳ Queue: <code>{active_jobs_count}</code> Active | "
        f"<code>{queued_jobs_count}</code> Queued"
        "</blockquote>\n"
        "<i>🧭 Home › Admin Panel</i>"
    )


def format_user_detail_card(
    user: UserAccount,
    albums_count: int,
    tracks_count: int,
    changes_count: int | None = None,
) -> str:
    """Format the User Detail Management Sub-Screen (Specification Section 7.1)."""
    oidc_status = (
        f"Linked (<code>{html.escape(user.authentik_username or user.authentik_sub or '')}</code>)"
        if user.is_oidc_linked
        else "Not Linked"
    )
    blocked_badge = " (🚫 BLOCKED)" if user.is_blocked else ""
    changes_line = (
        f"\n• Recorded Actions / Edits: <code>{changes_count}</code>" if changes_count is not None else ""
    )
    return (
        f"<b>👤 Managing User: {html.escape(user.mention_handle)}</b>\n"
        "<blockquote>"
        f"• Current Role: <code>{user.role.value}</code>{blocked_badge}\n"
        f"• Authentik Status: {oidc_status}\n"
        f"• Total Uploads: {albums_count} albums ({tracks_count} tracks)"
        f"{changes_line}"
        "</blockquote>\n"
        f"<i>User ID:</i> <code>{user.telegram_id}</code> • <i>🧭 Home › Admin › Users › Detail</i>"
    )
