"""Unit tests for Song Request RBAC permissions and moderation lifecycle."""

from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import PermissionDeniedError
from src.core.models import (
    Permission,
    SongRequestRecord,
    SongRequestStatus,
    SongRequestType,
    UserAccount,
    UserRole,
)
from src.core.provider import MultiProviderService
from src.core.rbac import has_permission


def test_permission_matrix_for_song_requests() -> None:
    guest = UserAccount(telegram_id=1, display_name="Guest", role=UserRole.GUEST)
    listener = UserAccount(telegram_id=2, display_name="Listener", role=UserRole.LISTENER)
    uploader = UserAccount(telegram_id=3, display_name="Uploader", role=UserRole.UPLOADER)
    moderator = UserAccount(telegram_id=4, display_name="Mod", role=UserRole.MODERATOR)
    admin = UserAccount(telegram_id=5, display_name="Admin", role=UserRole.ADMIN)

    # REQUEST_SONG
    assert not has_permission(guest, Permission.REQUEST_SONG)
    assert has_permission(listener, Permission.REQUEST_SONG)
    assert has_permission(uploader, Permission.REQUEST_SONG)
    assert has_permission(moderator, Permission.REQUEST_SONG)
    assert has_permission(admin, Permission.REQUEST_SONG)

    # APPROVE_SONG_REQUEST
    assert not has_permission(listener, Permission.APPROVE_SONG_REQUEST)
    assert not has_permission(uploader, Permission.APPROVE_SONG_REQUEST)
    assert has_permission(moderator, Permission.APPROVE_SONG_REQUEST)
    assert has_permission(admin, Permission.APPROVE_SONG_REQUEST)

    # MANAGE_PROVIDERS
    assert not has_permission(moderator, Permission.MANAGE_PROVIDERS)
    assert has_permission(admin, Permission.MANAGE_PROVIDERS)


@pytest.mark.anyio
async def test_guest_cannot_submit_song_request() -> None:
    guest = UserAccount(telegram_id=10, display_name="Guest", role=UserRole.GUEST)
    repo = AsyncMock()
    service = MultiProviderService(repository=repo)

    with pytest.raises(PermissionDeniedError):
        await service.submit_song_request(
            user=guest,
            request_type=SongRequestType.TRACK,
            track_title="Test Song",
            artist_name="Test Artist",
        )


@pytest.mark.anyio
async def test_listener_submission_enters_pending_and_notifies_admin() -> None:
    listener = UserAccount(telegram_id=20, display_name="Listener", role=UserRole.LISTENER)
    repo = AsyncMock()
    notifier = AsyncMock()

    # Simulate saving in repo
    def mock_create(req: SongRequestRecord) -> SongRequestRecord:
        return SongRequestRecord(
            id=101,
            telegram_id=req.telegram_id,
            request_type=req.request_type,
            track_title=req.track_title,
            artist_name=req.artist_name,
            album_name=req.album_name,
            identifiers=req.identifiers,
            target_quality=req.target_quality,
            status=req.status,
            reviewed_by=req.reviewed_by,
            reviewed_at=req.reviewed_at,
        )

    repo.create_song_request.side_effect = mock_create
    service = MultiProviderService(repository=repo, notifier=notifier)

    record, is_auto_approved = await service.submit_song_request(
        user=listener,
        request_type=SongRequestType.TRACK,
        track_title="Never Gonna Give You Up",
        artist_name="Rick Astley",
        album_name="Whenever You Need Somebody",
    )

    assert is_auto_approved is False
    assert record.id == 101
    assert record.status == SongRequestStatus.PENDING_APPROVAL
    repo.create_song_request.assert_awaited_once()
    notifier.send_song_request_card.assert_awaited_once_with(record, listener)


@pytest.mark.anyio
async def test_uploader_submission_is_auto_approved_without_admin_card() -> None:
    uploader = UserAccount(telegram_id=30, display_name="Uploader", role=UserRole.UPLOADER)
    repo = AsyncMock()
    notifier = AsyncMock()

    def mock_create(req: SongRequestRecord) -> SongRequestRecord:
        return SongRequestRecord(
            id=102,
            telegram_id=req.telegram_id,
            request_type=req.request_type,
            track_title=req.track_title,
            artist_name=req.artist_name,
            album_name=req.album_name,
            identifiers=req.identifiers,
            target_quality=req.target_quality,
            status=req.status,
            reviewed_by=req.reviewed_by,
            reviewed_at=req.reviewed_at,
        )

    repo.create_song_request.side_effect = mock_create
    service = MultiProviderService(repository=repo, notifier=notifier)

    record, is_auto_approved = await service.submit_song_request(
        user=uploader,
        request_type=SongRequestType.TRACK,
        track_title="Take On Me",
        artist_name="a-ha",
    )

    assert is_auto_approved is True
    assert record.id == 102
    assert record.status == SongRequestStatus.APPROVED
    assert record.reviewed_by == uploader.telegram_id
    notifier.send_song_request_card.assert_not_awaited()


@pytest.mark.anyio
async def test_moderator_resolves_song_request() -> None:
    listener = UserAccount(telegram_id=20, display_name="Listener", role=UserRole.LISTENER)
    mod = UserAccount(telegram_id=40, display_name="Mod", role=UserRole.MODERATOR)
    repo = AsyncMock()
    notifier = AsyncMock()

    service = MultiProviderService(repository=repo, notifier=notifier)

    # 1. Listener cannot resolve
    with pytest.raises(PermissionDeniedError):
        await service.resolve_song_request_by_moderator(
            request_id=101,
            reviewer=listener,
            approved=True,
        )

    # 2. Moderator approves
    mock_approved = SongRequestRecord(
        id=101,
        telegram_id=20,
        request_type=SongRequestType.TRACK,
        track_title="Test",
        artist_name="Test",
        status=SongRequestStatus.APPROVED,
        reviewed_by=mod.telegram_id,
    )
    repo.resolve_song_request.return_value = mock_approved

    res = await service.resolve_song_request_by_moderator(
        request_id=101,
        reviewer=mod,
        approved=True,
    )
    assert res is not None
    assert res.status == SongRequestStatus.APPROVED
    notifier.send_song_request_resolved_audit.assert_awaited_once_with(
        mock_approved, mod, SongRequestStatus.APPROVED
    )
