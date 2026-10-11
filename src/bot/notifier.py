"""Telegram Admin Log Channel adapter implementing `AdminLogNotifierPort` (Specification Section 5)."""

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from src.bot.keyboards.admin import build_access_request_approval_keyboard
from src.core.audit import (
    format_access_request_card,
    format_duplicate_rejected_event,
    format_ingest_success_event,
    format_oidc_sync_event,
    format_role_updated_event,
)
from src.core.models import (
    AccessRequestRecord,
    IngestionJob,
    TrackMetadata,
    UserAccount,
    UserRole,
)

logger = logging.getLogger(__name__)


class TelegramAdminLogNotifier:
    """Dispatches formatted security, OIDC, and ingestion audit messages to `admin_log_channel_id`."""

    def __init__(
        self,
        bot: Bot | None = None,
        admin_log_channel_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._channel_id = admin_log_channel_id
        self.sent_events: list[tuple[int | None, str, InlineKeyboardMarkup | None]] = []

    async def _dispatch(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Record the event and send it to the configured Telegram Admin Log Channel if enabled."""
        self.sent_events.append((self._channel_id, text, reply_markup))
        if self._bot is None or self._channel_id is None:
            logger.info("Admin Log Channel Event (local log):\n%s", text)
            return

        try:
            await self._bot.send_message(
                chat_id=self._channel_id,
                text=text,
                reply_markup=reply_markup,
            )
        except Exception as exc:
            logger.warning(
                "Failed to dispatch event to Admin Log Channel %s: %s",
                self._channel_id,
                exc,
            )

    async def send_access_request_card(
        self,
        user: UserAccount,
        request: AccessRequestRecord,
    ) -> None:
        """Post an interactive access request card with Approve/Deny buttons (Section 3.2)."""
        card = format_access_request_card(user, request)
        keyboard = build_access_request_approval_keyboard(request.id)
        await self._dispatch(card, reply_markup=keyboard)

    async def send_role_updated_audit(
        self,
        target_user: UserAccount,
        actor: UserAccount,
        previous_role: UserRole,
        new_role: UserRole,
        reason: str,
    ) -> None:
        """Post Event A (`USER AUDIT: ROLE_UPDATED`) to the Admin Log Channel."""
        text = format_role_updated_event(
            target_user=target_user,
            actor=actor,
            previous_role=previous_role,
            new_role=new_role,
            reason=reason,
        )
        await self._dispatch(text)

    async def send_oidc_sync_audit(
        self,
        target_user: UserAccount,
        group_changes: str,
        new_role: UserRole,
        cancelled_jobs_count: int,
    ) -> None:
        """Post Event B (`OIDC AUDIT: ROLE_SYNCHRONIZED`) to the Admin Log Channel."""
        text = format_oidc_sync_event(
            target_user=target_user,
            group_changes=group_changes,
            new_role=new_role,
            cancelled_jobs_count=cancelled_jobs_count,
        )
        await self._dispatch(text)

    async def send_ingest_success_audit(
        self,
        job: IngestionJob,
        uploader: UserAccount,
        metadata: TrackMetadata,
        tracks_written: int,
        audio_format: str,
        lyrics_embedded: int,
        duration_seconds: float,
    ) -> None:
        """Post Event C (`INGEST AUDIT: SUCCESS`) to the Admin Log Channel."""
        text = format_ingest_success_event(
            job=job,
            uploader=uploader,
            metadata=metadata,
            tracks_written=tracks_written,
            audio_format=audio_format,
            lyrics_embedded=lyrics_embedded,
            duration_seconds=duration_seconds,
        )
        await self._dispatch(text)

    async def send_duplicate_rejected_audit(
        self,
        job: IngestionJob,
        uploader: UserAccount,
        detected_track: str,
        conflict_path: Path,
        match_source: str,
    ) -> None:
        """Post Event D (`INGEST AUDIT: DUPLICATE_REJECTED`) to the Admin Log Channel."""
        text = format_duplicate_rejected_event(
            job=job,
            uploader=uploader,
            detected_track=detected_track,
            conflict_path=conflict_path,
            match_source=match_source,
        )
        await self._dispatch(text)
