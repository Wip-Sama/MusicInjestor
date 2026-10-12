"""Unit tests for RBAC governance, Authentik OIDC sync, IngestionQueueWorker, and audit formatters."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.bot.notifier import TelegramAdminLogNotifier
from src.bot.oidc_server import (
    OidcCallbackCoordinator,
    OidcHttpServer,
    format_oidc_pairing_message,
    run_hourly_oidc_sync_loop,
)
from src.core.audit import (
    format_access_request_card,
    format_admin_dashboard_card,
    format_duplicate_rejected_event,
    format_guest_access_denied_card,
    format_ingest_success_event,
    format_ingesting_progress_card,
    format_oidc_sync_event,
    format_queued_job_card,
    format_role_updated_event,
    format_user_detail_card,
)
from src.core.exceptions import (
    OidcAuthenticationError,
    PermissionDeniedError,
    QueueConcurrencyError,
)
from src.core.models import (
    AudioFingerprint,
    IngestionJob,
    JobState,
    MetadataCandidate,
    Permission,
    SourceType,
    TagCorrectionStatus,
    TrackMetadata,
    UserAccount,
    UserRole,
)
from src.core.oidc import (
    AuthentikOidcService,
    decrypt_token,
    encrypt_token,
    generate_state_nonce,
    verify_state_nonce,
)
from src.core.organizer import LibraryOrganizer
from src.core.pipeline import IngestionPipeline
from src.core.queue import IngestionQueueWorker, infer_source_type
from src.core.rbac import (
    GovernanceService,
    evaluate_oidc_role,
    has_permission,
    next_demotion_role,
    next_promotion_role,
    require_permission,
)
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository
from tests.unit.test_pipeline import (
    StubExtractor,
    StubFingerprinter,
    StubLyricsProvider,
    StubMetadataProvider,
)


def test_role_hierarchy_and_permission_matrix() -> None:
    """Verify 5-tier role hierarchy ranks and Section 3.1 capability permissions."""
    assert (
        UserRole.GUEST.rank
        < UserRole.UPLOADER.rank
        < UserRole.MODERATOR.rank
        < UserRole.ADMIN.rank
        < UserRole.OWNER.rank
    )
    assert UserRole.ADMIN.has_at_least(UserRole.MODERATOR)
    assert not UserRole.UPLOADER.has_at_least(UserRole.MODERATOR)

    guest = UserAccount(telegram_id=1, display_name="Guest", role=UserRole.GUEST)
    uploader = UserAccount(telegram_id=2, display_name="Uploader", role=UserRole.UPLOADER)
    moderator = UserAccount(telegram_id=3, display_name="Moderator", role=UserRole.MODERATOR)
    admin = UserAccount(telegram_id=4, display_name="Admin", role=UserRole.ADMIN)
    owner = UserAccount(telegram_id=5, display_name="Owner", role=UserRole.OWNER)
    blocked_admin = UserAccount(
        telegram_id=6,
        display_name="Blocked",
        role=UserRole.ADMIN,
        is_blocked=True,
    )

    assert has_permission(guest, Permission.SUBMIT_ACCESS_REQUEST)
    assert not has_permission(guest, Permission.SUBMIT_INGESTION)
    assert not has_permission(uploader, Permission.SUBMIT_ACCESS_REQUEST)
    assert has_permission(uploader, Permission.SUBMIT_INGESTION)
    assert not has_permission(uploader, Permission.VIEW_GLOBAL_QUEUE)
    assert has_permission(moderator, Permission.VIEW_GLOBAL_QUEUE)
    assert has_permission(moderator, Permission.RESOLVE_OTHERS_JOBS)
    assert not has_permission(moderator, Permission.MANAGE_USERS)
    assert has_permission(admin, Permission.MANAGE_USERS)
    assert has_permission(admin, Permission.CHANGE_CONCURRENCY_LIMIT)
    assert not has_permission(admin, Permission.SYSTEM_DESTRUCTIVE_WIPE)
    assert has_permission(owner, Permission.SYSTEM_DESTRUCTIVE_WIPE)
    assert not has_permission(blocked_admin, Permission.BASIC_COMMANDS)

    with pytest.raises(PermissionDeniedError):
        require_permission(guest, Permission.SUBMIT_INGESTION)

    assert next_promotion_role(UserRole.GUEST) == UserRole.UPLOADER
    assert next_promotion_role(UserRole.ADMIN) is None
    assert next_demotion_role(UserRole.MODERATOR) == UserRole.UPLOADER
    assert next_demotion_role(UserRole.GUEST) is None


def test_evaluate_oidc_role_mapping() -> None:
    """Verify Authentik group claims map to the highest matching RBAC role."""
    assert evaluate_oidc_role(["music-uploaders"]) == UserRole.UPLOADER
    assert evaluate_oidc_role(["music-uploaders", "music-moderators"]) == UserRole.MODERATOR
    assert evaluate_oidc_role(["music-admins", "music-uploaders"]) == UserRole.ADMIN
    assert evaluate_oidc_role(["unrelated-group"]) == UserRole.GUEST
    assert evaluate_oidc_role(()) == UserRole.GUEST


def test_state_nonce_and_token_encryption_roundtrip() -> None:
    """Verify HMAC-SHA256 state nonce 10m TTL and authenticated symmetric token encryption."""
    secret = "super-secret-test-key-32-bytes-long"
    nonce = generate_state_nonce(telegram_id=987654321, secret_key=secret, now_ts=1_700_000_000)
    recovered_id = verify_state_nonce(nonce, secret_key=secret, now_ts=1_700_000_300)
    assert recovered_id == 987654321

    with pytest.raises(OidcAuthenticationError, match="expired"):
        verify_state_nonce(nonce, secret_key=secret, now_ts=1_700_000_601)

    with pytest.raises(OidcAuthenticationError, match="signature"):
        verify_state_nonce(nonce + "tampered", secret_key=secret, now_ts=1_700_000_100)

    with pytest.raises(OidcAuthenticationError, match="Malformed"):
        verify_state_nonce("bad-nonce", secret_key=secret)

    ciphertext = encrypt_token("oauth2-refresh-token-xyz", secret)
    assert ciphertext != "oauth2-refresh-token-xyz"
    assert decrypt_token(ciphertext, secret) == "oauth2-refresh-token-xyz"

    with pytest.raises(OidcAuthenticationError):
        decrypt_token(ciphertext, "wrong-secret-key-0000000000")


def test_governance_service_access_requests_and_role_changes(tmp_path: Path) -> None:
    """Verify guest access request debouncing, approval, denial, role promotion, and blocking."""
    db_manager = SqliteDatabaseManager(tmp_path / "gov.db")
    repo = SqliteCatalogRepository(db_manager)
    notifier = TelegramAdminLogNotifier(admin_log_channel_id=-100123)
    gov = GovernanceService(repository=repo, notifier=notifier, owner_telegram_ids=(10000001,))

    async def _scenario() -> None:
        await db_manager.initialize()
        owner = await gov.ensure_user(10000001, "System Owner", "owner")
        assert owner.role == UserRole.OWNER

        guest = await gov.ensure_user(987654321, "John Doe", "john_doe")
        assert guest.role == UserRole.GUEST
        assert guest.mention_handle == "@john_doe"

        req1, created1 = await gov.submit_access_request(guest)
        assert created1 is True
        assert len(notifier.sent_events) == 1
        assert "New Access Request" in notifier.sent_events[0][1]

        req2, created2 = await gov.submit_access_request(guest)
        assert created2 is False
        assert req2.id == req1.id
        assert len(notifier.sent_events) == 1

        approved_user = await gov.approve_access_request(
            actor=owner,
            request_id=req1.id,
            granted_role=UserRole.UPLOADER,
        )
        assert approved_user.role == UserRole.UPLOADER
        assert len(notifier.sent_events) == 2
        assert "USER AUDIT: ROLE_UPDATED" in notifier.sent_events[1][1]

        promoted = await gov.change_user_role(
            actor=owner,
            target_telegram_id=guest.telegram_id,
            new_role=UserRole.MODERATOR,
            reason="Manual Promotion",
        )
        assert promoted.role == UserRole.MODERATOR

        blocked = await gov.set_user_blocked(
            actor=owner,
            target_telegram_id=guest.telegram_id,
            is_blocked=True,
        )
        assert blocked.is_blocked is True
        assert blocked.role == UserRole.GUEST

        logs = await repo.list_audit_logs(limit=10)
        assert len(logs) >= 4

    asyncio.run(_scenario())


def test_authentik_oidc_pairing_sync_demotion_and_webhook(tmp_path: Path) -> None:
    """Verify Authentik OIDC code exchange, group sync, demotion job cancellation, and webhooks."""
    db_manager = SqliteDatabaseManager(tmp_path / "oidc.db")
    repo = SqliteCatalogRepository(db_manager)
    notifier = TelegramAdminLogNotifier(admin_log_channel_id=-100123)
    secret = "oidc-test-secret-key-32-bytes-long"

    current_groups: list[str] = ["music-uploaders"]
    account_active = True

    def _mock_idp(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token/"):
            return httpx.Response(
                status_code=200,
                json={
                    "access_token": "acc_tok_123",
                    "refresh_token": "ref_tok_456",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/userinfo/"):
            return httpx.Response(
                status_code=200,
                json={
                    "sub": "c849bf3c-419b-4bf1-a185-1d6ebfa77bb0",
                    "preferred_username": "alex_dev",
                    "groups": current_groups,
                    "is_active": account_active,
                },
            )
        return httpx.Response(status_code=404)

    oidc = AuthentikOidcService(
        repository=repo,
        secret_key=secret,
        authorize_url="https://auth.example.com/application/o/authorize",
        token_url="https://auth.example.com/application/o/token",
        userinfo_url="https://auth.example.com/application/o/userinfo",
        client_id="music-bot",
        client_secret="client-sec",
        redirect_uri="http://localhost:8080/api/v1/auth/callback",
        notifier=notifier,
        transport=httpx.MockTransport(_mock_idp),
    )
    coordinator = OidcCallbackCoordinator(
        oidc_service=oidc,
        webhook_secret="wh-secret-123",
    )

    async def _scenario() -> None:
        await db_manager.initialize()
        user = UserAccount(telegram_id=55443322, display_name="Alex Dev", username="alex_dev")
        await repo.upsert_user(user)

        auth_url, nonce = oidc.build_authorization_url(55443322, now_ts=1_700_000_000)
        assert "client_id=music-bot" in auth_url
        assert nonce in auth_url

        outcome, msg = await coordinator.handle_oauth_callback(
            code="auth_code_abc",
            state=nonce,
            now_ts=1_700_000_100,
        )
        assert outcome.new_role == UserRole.UPLOADER
        assert outcome.user.is_oidc_linked is True
        assert "alex_dev" in msg
        assert format_oidc_pairing_message(outcome) == msg

        # Queue a job for this user, then remove their group in Authentik and trigger sync
        queued_job = IngestionJob(
            job_id="job-queued-1",
            source_path=tmp_path / "song.flac",
            state=JobState.QUEUED,
            user_id=55443322,
        )
        await repo.save_job(queued_job)

        current_groups.clear()
        outcomes = await oidc.sync_all_linked_users()
        assert len(outcomes) == 1
        assert outcomes[0].new_role == UserRole.GUEST
        assert outcomes[0].cancelled_jobs == 1
        assert any("OIDC AUDIT: ROLE_SYNCHRONIZED" in ev[1] for ev in notifier.sent_events)

        # Test staleness check (>15m)
        stale_user = await repo.get_user(55443322)
        assert stale_user is not None
        stale_user.last_oidc_sync = datetime.now(UTC) - timedelta(minutes=20)
        current_groups.append("music-moderators")
        await repo.upsert_user(stale_user)

        refreshed = await oidc.refresh_user_if_stale(55443322)
        assert refreshed.role == UserRole.MODERATOR

        # Test webhook deactivation
        wh_outcome = await coordinator.handle_authentik_webhook(
            payload={
                "event_type": "user_deactivated",
                "sub": "c849bf3c-419b-4bf1-a185-1d6ebfa77bb0",
            },
            provided_secret="wh-secret-123",
        )
        assert wh_outcome is not None
        assert wh_outcome.was_blocked is True
        assert wh_outcome.new_role == UserRole.GUEST

        with pytest.raises(PermissionDeniedError):
            await coordinator.handle_authentik_webhook(
                payload={"event_type": "user_updated", "sub": "c849bf3c"},
                provided_secret="wrong-secret",
            )

    asyncio.run(_scenario())


def test_ingestion_queue_worker_fifo_concurrency_and_audits(tmp_path: Path) -> None:
    """Verify FIFO queue execution, concurrency limit updates, duplicate audit, and job cancellation."""
    db_manager = SqliteDatabaseManager(tmp_path / "queue.db")
    repo = SqliteCatalogRepository(db_manager)
    notifier = TelegramAdminLogNotifier(admin_log_channel_id=-100123)

    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(
                title="Wish You Were Here",
                artist="Pink Floyd",
                album="Wish You Were Here",
                year=1975,
                track_number=4,
            ),
            confidence=0.98,
        )
    ]
    pipeline = IngestionPipeline(
        extractor=StubExtractor(),
        fingerprinter=StubFingerprinter("FP_WYWH"),
        metadata_provider=StubMetadataProvider(candidates),
        lyrics_provider=StubLyricsProvider(),
        organizer=LibraryOrganizer(tmp_path / "library"),
        catalog_repository=repo,
        auto_approve_confidence=0.90,
    )
    progress_updates: list[JobState] = []

    async def _on_progress(job: IngestionJob, result: object) -> None:
        progress_updates.append(job.state)

    worker = IngestionQueueWorker(
        pipeline=pipeline,
        catalog_repository=repo,
        governance_repository=repo,
        notifier=notifier,
        concurrency_limit=1,
        progress_callback=_on_progress,
    )

    assert infer_source_type(Path("album.zip")) == SourceType.ARCHIVE_ZIP
    assert infer_source_type(Path("album.7z")) == SourceType.ARCHIVE_7Z
    assert infer_source_type(Path("track.flac")) == SourceType.DIRECT_AUDIO

    async def _scenario() -> None:
        await db_manager.initialize()
        uploader = UserAccount(
            telegram_id=11223344,
            display_name="Jane Smith",
            username="jane_smith",
            role=UserRole.UPLOADER,
        )
        admin = UserAccount(
            telegram_id=10000001,
            display_name="Admin",
            username="admin",
            role=UserRole.ADMIN,
        )
        await repo.upsert_user(uploader)
        await repo.upsert_user(admin)

        audio_a = tmp_path / "track_a.flac"
        audio_a.write_bytes(b"fLaC-track-a")
        audio_b = tmp_path / "track_b.flac"
        audio_b.write_bytes(b"fLaC-track-b")

        job_a, pos_a = await worker.enqueue_job(uploader, audio_a, "Wish_You_Were_Here.flac")
        job_b, pos_b = await worker.enqueue_job(uploader, audio_b, "Duplicate_Upload.flac")
        assert pos_a == 1
        assert pos_b == 2

        new_limit = await worker.set_concurrency_limit(admin, 2)
        assert new_limit == 2
        with pytest.raises(QueueConcurrencyError):
            await worker.set_concurrency_limit(admin, 8)

        res_a = await worker.process_next_queued_job()
        assert res_a is not None
        assert res_a.job.state == JobState.COMPLETED
        assert any("INGEST AUDIT: SUCCESS" in ev[1] for ev in notifier.sent_events)

        res_b = await worker.process_next_queued_job()
        assert res_b is not None
        assert res_b.job.state == JobState.DUPLICATE_REJECTED
        assert any("INGEST AUDIT: DUPLICATE_REJECTED" in ev[1] for ev in notifier.sent_events)

        # Enqueue and cancel a third job
        job_c, _ = await worker.enqueue_job(uploader, audio_a, "To_Cancel.flac")
        cancelled = await worker.cancel_job(job_c.job_id, uploader)
        assert cancelled.state == JobState.CANCELLED

        assert JobState.PROCESSING in progress_updates
        assert JobState.COMPLETED in progress_updates

    asyncio.run(_scenario())


def test_audit_and_ui_card_formatters(tmp_path: Path) -> None:
    """Verify all message templates in `src.core.audit` match Specification Sections 3, 4, 5, and 7."""
    user = UserAccount(telegram_id=987654321, display_name="John Doe", username="john_doe")
    actor = UserAccount(telegram_id=10000001, display_name="Owner", username="owner", role=UserRole.OWNER)

    denied_card = format_guest_access_denied_card(987654321)
    assert "Access Denied" in denied_card
    assert "987654321" in denied_card

    ev_a = format_role_updated_event(user, actor, UserRole.GUEST, UserRole.UPLOADER, "Manual Approval")
    assert "USER AUDIT: ROLE_UPDATED" in ev_a
    assert "@john_doe" in ev_a

    user.authentik_sub = "sub-uuid-1"
    ev_b = format_oidc_sync_event(user, "Removed from 'music-uploaders'", UserRole.GUEST, 1)
    assert "OIDC AUDIT: ROLE_SYNCHRONIZED" in ev_b
    assert "(Access Revoked)" in ev_b

    job = IngestionJob(
        job_id="job-123",
        source_path=tmp_path / "song.flac",
        final_path=tmp_path / "Pink Floyd" / "Wish You Were Here (1975)" / "01.flac",
        original_filename="Led_Zeppelin_IV_FLAC.zip",
        file_size_bytes=380 * 1024 * 1024,
    )
    meta = TrackMetadata(title="Shine On", artist="Pink Floyd", album="Wish You Were Here", year=1975)
    ev_c = format_ingest_success_event(job, user, meta, 5, "FLAC", 5, 14.2)
    assert "INGEST AUDIT: SUCCESS" in ev_c
    assert "14.2 seconds" in ev_c

    ev_d = format_duplicate_rejected_event(
        job,
        user,
        "Radiohead - Paranoid Android",
        Path("/music/Radiohead/02.flac"),
        "Exact AcoustID Fingerprint Hash Match",
    )
    assert "INGEST AUDIT: DUPLICATE_REJECTED" in ev_d

    q_card = format_queued_job_card(job, position=2, active_job_filename="Discovery.7z")
    assert "Position in Queue: #2" in q_card
    assert "380 MB" in q_card

    prog_card = format_ingesting_progress_card(
        "Led_Zeppelin_IV_FLAC.zip",
        80,
        "Complete (8 tracks)",
        "Matched",
        "8/8 Synced",
        ambiguity_prompt=True,
    )
    assert "[████████░░] 80%" in prog_card
    assert "Release Ambiguity" in prog_card

    dash = format_admin_dashboard_card("Proxmox LXC 104", 1, 12, 1, 0, 1)
    assert "Administrator Control Panel" in dash
    assert "1 (Serial)" in dash

    detail = format_user_detail_card(user, 14, 182)
    assert "14 albums (182 tracks)" in detail
    assert format_access_request_card is not None


def test_oidc_http_server_endpoints_and_hourly_sync_loop(tmp_path: Path) -> None:
    """Verify `OidcHttpServer` HTTP routes (`/api/v1/auth/callback` & webhook) and hourly cron loop."""
    db_manager = SqliteDatabaseManager(tmp_path / "oidc_http.db")
    repo = SqliteCatalogRepository(db_manager)
    secret = "oidc-http-test-secret-key-32-bytes"
    current_groups: list[str] = ["music-uploaders"]

    def _mock_idp(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token/"):
            return httpx.Response(
                status_code=200,
                json={
                    "access_token": "acc_http",
                    "refresh_token": "ref_http",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/userinfo/"):
            return httpx.Response(
                status_code=200,
                json={
                    "sub": "sub-http-user-1",
                    "preferred_username": "http_user",
                    "groups": current_groups,
                    "is_active": True,
                },
            )
        return httpx.Response(status_code=404)

    oidc = AuthentikOidcService(
        repository=repo,
        secret_key=secret,
        authorize_url="https://auth.example.com/application/o/authorize",
        token_url="https://auth.example.com/application/o/token",
        userinfo_url="https://auth.example.com/application/o/userinfo",
        client_id="music-bot",
        client_secret="client-sec",
        redirect_uri="http://localhost:8080/api/v1/auth/callback",
        transport=httpx.MockTransport(_mock_idp),
    )
    coordinator = OidcCallbackCoordinator(
        oidc_service=oidc,
        webhook_secret="wh-secret-http",
    )

    async def _scenario() -> None:
        await db_manager.initialize()
        await repo.upsert_user(UserAccount(telegram_id=778899, display_name="HTTP User"))

        server = OidcHttpServer(coordinator=coordinator, host="127.0.0.1", port=0)
        await server.start()
        try:
            base_url = f"http://127.0.0.1:{server.bound_port}"
            async with httpx.AsyncClient() as client:
                health = await client.get(f"{base_url}/healthz")
                assert health.status_code == 200
                assert health.json() == {"status": "ok"}

                bad_cb = await client.get(f"{base_url}/api/v1/auth/callback")
                assert bad_cb.status_code == 400

                _, nonce = oidc.build_authorization_url(778899)
                ok_cb = await client.get(
                    f"{base_url}/api/v1/auth/callback",
                    params={"code": "code_ok", "state": nonce},
                )
                assert ok_cb.status_code == 200
                assert ok_cb.headers["X-Assigned-Role"] == "UPLOADER"
                assert "http_user" in ok_cb.text

                # Webhook with invalid secret -> 403
                forbidden_wh = await client.post(
                    f"{base_url}/api/v1/webhooks/authentik",
                    json={"event_type": "user_updated", "sub": "sub-http-user-1"},
                    headers={"X-Authentik-Secret": "bad"},
                )
                assert forbidden_wh.status_code == 403

                # Webhook with Bearer token -> 200
                current_groups[:] = ["music-admins"]
                ok_wh = await client.post(
                    f"{base_url}/api/v1/webhooks/authentik",
                    json={"event_type": "user_updated", "sub": "sub-http-user-1"},
                    headers={"Authorization": "Bearer wh-secret-http"},
                )
                assert ok_wh.status_code == 200
                assert ok_wh.json()["new_role"] == "ADMIN"
        finally:
            await server.stop()

        # Verify hourly sync loop executes and exits cleanly when stop_event is set
        stop_event = asyncio.Event()
        current_groups[:] = ["music-moderators"]

        async def _trigger_stop() -> None:
            await asyncio.sleep(1.1)
            stop_event.set()

        await asyncio.gather(
            run_hourly_oidc_sync_loop(oidc, interval_seconds=1, stop_event=stop_event),
            _trigger_stop(),
        )
        synced_user = await repo.get_user(778899)
        assert synced_user is not None
        assert synced_user.role == UserRole.MODERATOR

    asyncio.run(_scenario())


def test_governance_tag_corrections_and_system_wipe(tmp_path: Path) -> None:
    """Verify `REQUEST_TAG_CORRECTION`, `APPROVE_TAG_CORRECTIONS`, and `SYSTEM_DESTRUCTIVE_WIPE`."""
    db_manager = SqliteDatabaseManager(tmp_path / "tags.db")
    repo = SqliteCatalogRepository(db_manager)
    gov = GovernanceService(repository=repo, owner_telegram_ids=(1001,))

    async def _scenario() -> None:
        await db_manager.initialize()
        owner = await gov.ensure_user(1001, "Owner")
        uploader = await gov.ensure_user(2002, "Uploader")
        uploader = await gov.change_user_role(owner, 2002, UserRole.UPLOADER)
        moderator = await gov.ensure_user(3003, "Moderator")
        moderator = await gov.change_user_role(owner, 3003, UserRole.MODERATOR)

        await repo.register_ingested_track(
            job_id="job-t1",
            metadata=TrackMetadata(
                title="Stay Alive",
                artist="EMILIA",
                album="Stay Alive",
                year=2016,
                track_number=1,
                recording_mbid="rec-stay-alive-1",
            ),
            fingerprint=AudioFingerprint(duration_seconds=267, fingerprint="FP_SA_1"),
            library_path=tmp_path / "01 - Stay Alive.flac",
            uploaded_by=uploader.telegram_id,
            has_synced_lyrics=True,
        )

        recent = await repo.list_library_tracks(limit=5)
        assert len(recent) == 1
        assert recent[0].id == "rec-stay-alive-1"

        req, track = await gov.request_tag_correction(
            actor=uploader,
            track_id="rec-stay-alive-1",
            proposed_title="Stay Alive (Remastered)",
            proposed_artist="Emilia (CV: Rie Takahashi)",
            proposed_year=2020,
            reason="Canonical credit",
        )
        assert req.id is not None
        assert req.status == TagCorrectionStatus.PENDING
        assert track.title == "Stay Alive"

        # Uploader cannot approve tag corrections
        with pytest.raises(PermissionDeniedError):
            await gov.approve_tag_correction(uploader, req.id)

        # Moderator approves tag correction
        resolved, updated_track = await gov.approve_tag_correction(moderator, req.id)
        assert resolved.status == TagCorrectionStatus.APPROVED
        assert updated_track.title == "Stay Alive (Remastered)"
        assert updated_track.artist == "Emilia (CV: Rie Takahashi)"
        assert updated_track.year == 2020

        # Submit a second request and reject it
        req2, _ = await gov.request_tag_correction(
            actor=uploader,
            track_id="rec-stay-alive-1",
            proposed_album="Wrong Album",
        )
        assert req2.id is not None
        rejected = await gov.reject_tag_correction(moderator, req2.id)
        assert rejected.status == TagCorrectionStatus.REJECTED

        # Moderator cannot execute destructive system wipe
        with pytest.raises(PermissionDeniedError):
            await gov.wipe_library_catalog(moderator)

        # Owner executes destructive system wipe
        removed = await gov.wipe_library_catalog(owner)
        assert removed >= 2
        assert await repo.list_library_tracks(limit=5) == []

    asyncio.run(_scenario())
