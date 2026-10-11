"""Pure domain formatters for Admin Log Channel events, queue status cards, and admin panels."""

from datetime import UTC, datetime
from pathlib import Path

from src.core.models import (
    AccessRequestRecord,
    IngestionJob,
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
        "🔒 <b>Access Denied</b>\n"
        "This is a private music ingestion gateway.\n\n"
        f"Your Telegram ID: <code>{telegram_id}</code>"
    )


def format_access_request_card(user: UserAccount, request: AccessRequestRecord) -> str:
    """Format the Admin Log Channel card for a new manual access request (Section 3.2)."""
    ts = _format_utc_timestamp(request.created_at, include_seconds=False)
    return (
        "🔔 <b>New Access Request</b>\n"
        f"• User: {user.mention_handle} (ID: <code>{user.telegram_id}</code>)\n"
        f"• Name: {user.display_name}\n"
        f"• Timestamp: {ts}"
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
        "🛡 <b>USER AUDIT: ROLE_UPDATED</b>\n"
        f"• Target User: {target_user.mention_handle} (ID: <code>{target_user.telegram_id}</code>)\n"
        f"• Actor: {actor.mention_handle} (ID: <code>{actor.telegram_id}</code>)\n"
        f"• Previous Role: {previous_role.value}\n"
        f"• New Role: {new_role.value}\n"
        f"• Reason: {reason}\n"
        f"• Timestamp: {ts}"
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
        "🔄 <b>OIDC AUDIT: ROLE_SYNCHRONIZED</b>\n"
        f"• Target User: {target_user.mention_handle} (ID: <code>{target_user.telegram_id}</code>)\n"
        f"• Authentik Subject: <code>{subject}</code>\n"
        f"• Group Changes Detected: {group_changes}\n"
        f"• New Assigned Role: {new_role.value}{role_suffix}\n"
        f"• Active Queued Jobs Cancelled: {cancelled_jobs_count}\n"
        f"• Timestamp: {ts}"
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
    album_label = f"{metadata.effective_album_artist} - {metadata.album}{year_str}"
    target_dir = str(job.final_path.parent) if job.final_path is not None else "unknown"
    return (
        "✅ <b>INGEST AUDIT: SUCCESS</b>\n"
        f"• Job ID: <code>{job.job_id}</code>\n"
        f"• Uploader: {uploader.mention_handle} (ID: <code>{uploader.telegram_id}</code>)\n"
        f"• Album: {album_label}\n"
        f"• Tracks Written: {tracks_written} files ({audio_format})\n"
        f"• Target Path: <code>{target_dir}</code>\n"
        f"• Lyrics: {lyrics_embedded}/{tracks_written} embedded (LRCLIB synced)\n"
        f"• Processing Duration: {duration_seconds:.1f} seconds"
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
        "⚠️ <b>INGEST AUDIT: DUPLICATE_REJECTED</b>\n"
        f"• Job ID: <code>{job.job_id}</code>\n"
        f"• Uploader: {uploader.mention_handle} (ID: <code>{uploader.telegram_id}</code>)\n"
        f"• Detected Track: {detected_track}\n"
        f"• Conflict Path: <code>{conflict_path}</code>\n"
        f"• Match Source: {match_source}\n"
        "• Action Taken: Temporary files purged from scratch space"
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
    """Format the in-chat queue waiting feedback message (Specification Section 4.2)."""
    filename = job.original_filename or job.source_path.name
    size_label = _format_file_size_mb(job.file_size_bytes)
    status_line = (
        f"Waiting for active job ({active_job_filename}) to finish..."
        if active_job_filename
        else "Queued for next available worker slot..."
    )
    return (
        "⏳ <b>Ingestion Queued</b>\n"
        "────────────────────────\n"
        f"📦 Payload: <code>{filename}</code> ({size_label})\n"
        f"📊 Position in Queue: #{position}\n"
        f"⚙️ Status: {status_line}"
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
        f"⚙️ <b>Ingesting: {filename}</b>\n"
        "────────────────────────\n"
        f"[{bar}] {clamped}%\n"
        f"• Extraction: {extraction_status}\n"
        f"• AcoustID: {acoustid_status}\n"
        f"• Lyrics: {lyrics_status}"
    )
    if ambiguity_prompt:
        base += "\n\n⚠️ <b>Release Ambiguity:</b>\nWhich version is this upload?"
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
        "🎛 <b>Administrator Control Panel</b>\n"
        f"Node: {node_name} | Ingestion Core Engine\n"
        f"Health: 🟢 OK | Memory: {memory_used_mb} MB / {memory_total_mb} MB\n"
        f"Concurrency Limit: {concurrency_limit} ({mode_label})\n"
        "────────────────────────\n"
        f"👥 Users: {active_users_count} Active | {pending_requests_count} Pending\n"
        f"⏳ Queue: {active_jobs_count} Active | {queued_jobs_count} Queued"
    )


def format_user_detail_card(
    user: UserAccount,
    albums_count: int,
    tracks_count: int,
) -> str:
    """Format the User Detail Management Sub-Screen (Specification Section 7.1)."""
    oidc_status = (
        f"Linked ({user.authentik_username or user.authentik_sub})" if user.is_oidc_linked else "Not Linked"
    )
    blocked_badge = " (🚫 BLOCKED)" if user.is_blocked else ""
    return (
        f"👤 <b>Managing User: {user.mention_handle}</b>\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Current Role: {user.role.value}{blocked_badge}\n"
        f"Authentik Status: {oidc_status}\n"
        f"Total Uploads: {albums_count} albums ({tracks_count} tracks)"
    )
