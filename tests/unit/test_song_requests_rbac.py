"""Unit tests for Song Request RBAC permissions and moderation lifecycle."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.exceptions import PermissionDeniedError
from src.core.models import (
    AvailableAudioQuality,
    Permission,
    ProviderServerRecord,
    ProvisionJobStatus,
    ProvisionResponse,
    SongRequestRecord,
    SongRequestStatus,
    SongRequestType,
    TrackMetadata,
    TrackResolutionResult,
    UserAccount,
    UserRole,
)
from src.core.provider import HttpMusicProviderClient, MultiProviderService
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


@pytest.mark.anyio
async def test_execute_provision_request_downloads_with_bearer_and_enqueues(tmp_path: Path) -> None:
    """Verify end-to-end provisioning resolves, downloads relative URLs with Bearer auth, and enqueues."""
    artifact_bytes = b"fLaC-mock-audio-payload"
    artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
    captured_headers: list[str] = []

    def _download_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers.get("Authorization", ""))
        if request.url.path == "/api/v1/artifacts/song.flac":
            return httpx.Response(status_code=200, content=artifact_bytes)
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(_download_handler)
    client = HttpMusicProviderClient(
        base_url="http://127.0.0.1:8085",
        api_token="secret-prov-token",
        client=httpx.AsyncClient(transport=transport),
    )
    client.resolve_track = AsyncMock(  # type: ignore[method-assign]
        return_value=TrackResolutionResult(
            matched=True,
            provider_track_id="prov-trk-1",
            matched_source="fallback",
            metadata=TrackMetadata(title="COPERTE", artist="Andrea Cerrato", album="COPERTE"),
            available_qualities=(
                AvailableAudioQuality(quality_id="lossless", codec="flac", bitrate_kbps=1411),
            ),
        )
    )
    client.request_provision = AsyncMock(  # type: ignore[method-assign]
        return_value=ProvisionResponse(
            status="queued",
            job_id="prov-job-1",
            download_url=None,
        )
    )
    client.get_job_status = AsyncMock(  # type: ignore[method-assign]
        return_value=ProvisionJobStatus(
            job_id="prov-job-1",
            status="completed",
            progress_percent=100.0,
            download_url="/api/v1/artifacts/song.flac",
            checksum_sha256=artifact_sha,
        )
    )

    server = ProviderServerRecord(
        id="prov-default",
        name="Default Provider",
        base_url="http://127.0.0.1:8085",
        api_token_encrypted="secret-prov-token",
        priority=10,
        is_enabled=True,
    )
    req_record = SongRequestRecord(
        id=501,
        telegram_id=285978883,
        request_type=SongRequestType.TRACK,
        track_title="COPERTE",
        artist_name="Andrea Cerrato",
        status=SongRequestStatus.APPROVED,
    )

    repo = AsyncMock()
    repo.get_song_request.return_value = req_record
    repo.list_provider_servers.return_value = [server]
    completed_record = SongRequestRecord(
        id=501,
        telegram_id=285978883,
        request_type=SongRequestType.TRACK,
        track_title="COPERTE",
        artist_name="Andrea Cerrato",
        provider_id="prov-default",
        job_id="ingest-job-99",
        status=SongRequestStatus.COMPLETED,
    )
    repo.update_song_request_job_and_track.return_value = completed_record

    enqueued_paths: list[Path] = []

    async def _on_ready(path: Path, rec: SongRequestRecord, srv: ProviderServerRecord) -> str:
        enqueued_paths.append(path)
        assert path.read_bytes() == artifact_bytes
        return "ingest-job-99"

    notified: list[tuple[int, str]] = []

    async def _notify(uid: int, text: str) -> None:
        notified.append((uid, text))

    service = MultiProviderService(
        repository=repo,
        client_factory=MagicMock(return_value=client),
        staging_dir=tmp_path / "staging",
        on_artifact_ready=_on_ready,
        notify_requester=_notify,
    )

    real_async_client = httpx.AsyncClient
    with patch(
        "src.core.provider.httpx.AsyncClient",
        side_effect=lambda *args, **kwargs: real_async_client(
            transport=transport,
            timeout=kwargs.get("timeout", 30.0),
            follow_redirects=kwargs.get("follow_redirects", True),
        ),
    ):
        result = await service.execute_provision_request(501)
    assert result is not None
    assert result.status == SongRequestStatus.COMPLETED
    assert captured_headers == ["Bearer secret-prov-token"]
    assert len(enqueued_paths) == 1
    assert any(
        call.kwargs.get("status") == SongRequestStatus.COMPLETED
        and call.kwargs.get("job_id") == "ingest-job-99"
        for call in repo.update_song_request_job_and_track.call_args_list
    )
    assert notified and "COPERTE" in notified[0][1]

    # Verify failure path when no provider matches
    repo.list_provider_servers.return_value = []
    repo.update_song_request_job_and_track.reset_mock()
    with patch(
        "src.core.provider.httpx.AsyncClient",
        side_effect=lambda *args, **kwargs: real_async_client(
            transport=transport,
            timeout=kwargs.get("timeout", 30.0),
            follow_redirects=kwargs.get("follow_redirects", True),
        ),
    ):
        await service.execute_provision_request(501)
    repo.update_song_request_job_and_track.assert_awaited()
    fail_kwargs = repo.update_song_request_job_and_track.call_args.kwargs
    assert fail_kwargs["status"] == SongRequestStatus.FAILED
    assert "No configured music provider matched" in fail_kwargs["error_message"]
