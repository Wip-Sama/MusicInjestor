"""Unit tests for the unified Telegram Mini App HTTP and API server (`BotWebServer`)."""

import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from aiohttp import web

from src.bot.webapp_server import BotWebServer
from src.core.lyrics import LrclibLyricsClient
from src.core.metadata import MusicBrainzMetadataService
from src.core.models import (
    IngestionJob,
    IngestionResult,
    JobState,
    LibraryTrackRecord,
    LyricsData,
    MetadataCandidate,
    SourceType,
    TrackMetadata,
    UserAccount,
    UserRole,
)
from src.core.organizer import LibraryOrganizer
from src.core.queue import IngestionQueueWorker
from src.core.rbac import GovernanceService
from src.db.database import SqliteDatabaseManager
from src.db.repository import SqliteCatalogRepository
from tests.unit.test_webapp_auth import TEST_BOT_TOKEN, _build_signed_init_data


async def _setup_test_server(
    tmp_path: Path,
    owner_telegram_ids: tuple[int, ...] = (),
    static_dir: Path | None = None,
    dev_mode: bool = False,
    organizer: LibraryOrganizer | None = None,
    metadata_service: MusicBrainzMetadataService | None = None,
    lyrics_client: LrclibLyricsClient | None = None,
    queue_worker: IngestionQueueWorker | None = None,
    pipeline: Any = None,
    scratch_dir: Path | None = None,
) -> tuple[BotWebServer, SqliteCatalogRepository]:
    """Helper bootstrapping SQLite persistence, GovernanceService, and BotWebServer on an ephemeral port."""
    db_manager = SqliteDatabaseManager(tmp_path / "webapp_test.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    gov_service = GovernanceService(repository=repo, owner_telegram_ids=owner_telegram_ids)

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov_service,
        coordinator=None,
        static_dir=static_dir,
        host="127.0.0.1",
        port=0,
        owner_telegram_ids=owner_telegram_ids,
        dev_mode=dev_mode,
        catalog_repository=repo,
        organizer=organizer,
        metadata_service=metadata_service,
        lyrics_client=lyrics_client,
        queue_worker=queue_worker,
        pipeline=pipeline,
        scratch_dir=scratch_dir,
    )
    await server.start()
    return server, repo


@pytest.mark.anyio
async def test_healthz_endpoint(tmp_path: Path) -> None:
    """Verify that GET /healthz returns status ok."""
    server, _ = await _setup_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/healthz")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["app"] == "musicDownloader"
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_get_me_unauthorized(tmp_path: Path) -> None:
    """Verify that requests missing TMA authorization headers return 401."""
    server, _ = await _setup_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/api/v1/app/auth/me")
            assert resp.status_code == 401
            data = resp.json()
            assert "error" in data
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_get_me_authorized_success(tmp_path: Path) -> None:
    """Verify that valid Authorization: tma <initData> successfully resolves the user profile."""
    server, _ = await _setup_test_server(tmp_path, owner_telegram_ids=(123456789,))
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        now = int(time.time())
        user_json = json.dumps(
            {"id": 123456789, "first_name": "Admin", "last_name": "Boss", "username": "adminboss"},
            separators=(",", ":"),
        )
        init_data = _build_signed_init_data(
            {
                "auth_date": str(now),
                "start_param": "track_99",
                "user": user_json,
            }
        )

        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"tma {init_data}"}
            resp = await client.get(f"{base_url}/api/v1/app/auth/me", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["telegram_id"] == 123456789
            assert data["role"] == "OWNER"
            assert data["display_name"] == "Admin Boss"
            assert data["username"] == "adminboss"
            assert data["is_owner"] is True
            assert data["start_param"] == "track_99"
            assert "SYSTEM_DESTRUCTIVE_WIPE" in data["permissions"]
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_static_spa_placeholder_fallback(tmp_path: Path) -> None:
    """Verify that unknown routes render the SPA fallback."""
    server, _ = await _setup_test_server(tmp_path, static_dir=tmp_path / "empty_dir")
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/songs/42")
            assert resp.status_code == 200
            assert "Music Ingestion Mini App" in resp.text
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_static_spa_serves_built_files(tmp_path: Path) -> None:
    """Verify that when index.html and assets exist, the server serves real files."""
    static_folder = tmp_path / "dist"
    static_folder.mkdir(parents=True)
    index_file = static_folder / "index.html"
    index_file.write_text("<!DOCTYPE html><html><body><h1>Built App</h1></body></html>", encoding="utf-8")
    asset_file = static_folder / "style.css"
    asset_file.write_text("body { color: red; }", encoding="utf-8")

    server, _ = await _setup_test_server(tmp_path, static_dir=static_folder)
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            # Direct asset request
            resp_asset = await client.get(f"{base_url}/style.css")
            assert resp_asset.status_code == 200
            assert "color: red" in resp_asset.text

            # SPA route fallback to index.html
            resp_spa = await client.get(f"{base_url}/catalog/view/123")
            assert resp_spa.status_code == 200
            assert "Built App" in resp_spa.text
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_dev_token_endpoint(tmp_path: Path) -> None:
    """Verify that GET /api/v1/app/dev/token yields a valid initData token when dev_mode is active."""
    # 1. When dev_mode is False, dev endpoint returns fallback (or 404)
    server_prod, _ = await _setup_test_server(tmp_path / "prod", dev_mode=False)
    try:
        base_url_prod = f"http://127.0.0.1:{server_prod.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url_prod}/api/v1/app/dev/token")
            # In SPA fallback, unknown paths return 200 html placeholder if no route matched
            assert "application/json" not in resp.headers.get("Content-Type", "")
    finally:
        await server_prod.stop()

    # 2. When dev_mode is True, dev endpoint returns a signed token usable with /api/v1/app/auth/me
    server_dev, _ = await _setup_test_server(tmp_path / "dev", owner_telegram_ids=(998877,), dev_mode=True)
    try:
        base_url_dev = f"http://127.0.0.1:{server_dev.bound_port}"
        async with httpx.AsyncClient() as client:
            resp_token = await client.get(f"{base_url_dev}/api/v1/app/dev/token")
            assert resp_token.status_code == 200
            data_token = resp_token.json()
            assert "init_data" in data_token
            dev_init_data = data_token["init_data"]

            # Use the acquired token to authenticate against /api/v1/app/auth/me
            headers = {"Authorization": f"tma {dev_init_data}"}
            resp_me = await client.get(f"{base_url_dev}/api/v1/app/auth/me", headers=headers)
            assert resp_me.status_code == 200
            data_me = resp_me.json()
            assert data_me["telegram_id"] == 998877
            assert data_me["role"] == "OWNER"
            assert data_me["is_owner"] is True
    finally:
        await server_dev.stop()


@pytest.mark.anyio
async def test_catalog_tracks_endpoint(tmp_path: Path) -> None:
    """Verify that GET /api/v1/app/tracks returns paginated, sortable track records."""
    server, repo = await _setup_test_server(tmp_path, owner_telegram_ids=(123456,), dev_mode=True)
    try:
        # Create 2 mock track files and records
        track_dir = tmp_path / "music"
        track_dir.mkdir(parents=True)
        file1 = track_dir / "01 - Alpha - Song A.flac"
        file2 = track_dir / "02 - Beta - Song B.mp3"
        file1.write_bytes(b"FLAC" + b"\x00" * 100)
        file2.write_bytes(b"ID3" + b"\x00" * 100)

        record1 = LibraryTrackRecord(
            id="trk_alpha1",
            job_id="job_1",
            uploaded_by=123456,
            storage_path=file1,
            fingerprint="fp1",
            duration_seconds=180.0,
            artist="Artist Alpha",
            album="Album One",
            title="Song A",
            year=2021,
            track_number=1,
            has_synced_lyrics=True,
        )
        record2 = LibraryTrackRecord(
            id="trk_beta2",
            job_id="job_2",
            uploaded_by=123456,
            storage_path=file2,
            fingerprint="fp2",
            duration_seconds=210.0,
            artist="Artist Beta",
            album="Album Two",
            title="Song B",
            year=2023,
            track_number=2,
            has_synced_lyrics=False,
        )
        await repo.upsert_scanned_library_track(record1)
        await repo.upsert_scanned_library_track(record2)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            # 1. List all tracks
            resp = await client.get(f"{base_url}/api/v1/app/tracks", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 2
            assert len(data["items"]) == 2
            assert data["page"] == 1

            # 2. Search filtering
            resp_search = await client.get(f"{base_url}/api/v1/app/tracks?query=Alpha", headers=headers)
            assert resp_search.status_code == 200
            data_search = resp_search.json()
            assert data_search["total"] == 1
            assert data_search["items"][0]["title"] == "Song A"
            assert data_search["items"][0]["format"] == "flac"

            # 3. Single track detail
            resp_single = await client.get(f"{base_url}/api/v1/app/tracks/trk_alpha1", headers=headers)
            assert resp_single.status_code == 200
            data_single = resp_single.json()
            assert data_single["id"] == "trk_alpha1"
            assert data_single["has_synced_lyrics"] is True
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_track_cover_and_audio_streaming(tmp_path: Path) -> None:
    """Verify that cover streaming and audio HTTP 206 range requests function as expected."""
    server, repo = await _setup_test_server(tmp_path, owner_telegram_ids=(123456,), dev_mode=True)
    try:
        album_dir = tmp_path / "album_stream"
        album_dir.mkdir(parents=True)
        audio_file = album_dir / "track.flac"
        # Write 1024 bytes of dummy audio data
        dummy_audio_bytes = b"FLAC_STREAM_HEADER" + b"\xaa" * 1000
        audio_file.write_bytes(dummy_audio_bytes)

        # Write cover.jpg
        cover_file = album_dir / "cover.jpg"
        cover_file.write_bytes(b"\xff\xd8\xff\xe0dummy_jpeg_bytes")

        record = LibraryTrackRecord(
            id="trk_stream1",
            job_id="job_stream",
            uploaded_by=123456,
            storage_path=audio_file,
            fingerprint="fp_stream",
            duration_seconds=120.0,
            artist="Streamer",
            album="Stream Album",
            title="Stream Track",
        )
        await repo.upsert_scanned_library_track(record)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            # 1. Cover retrieval
            resp_cover = await client.get(f"{base_url}/api/v1/app/tracks/trk_stream1/cover")
            assert resp_cover.status_code == 200
            assert resp_cover.headers["Content-Type"].startswith("image/")
            assert len(resp_cover.content) > 0

            # 2. Cover fallback for non-existent track
            resp_cover_none = await client.get(f"{base_url}/api/v1/app/tracks/nonexistent/cover")
            assert resp_cover_none.status_code == 200
            assert "image/svg+xml" in resp_cover_none.headers["Content-Type"]

            # 3. Audio retrieval without auth -> 401 Unauthorized
            resp_unauth = await client.get(f"{base_url}/api/v1/app/tracks/trk_stream1/audio")
            assert resp_unauth.status_code == 401

            # 4. Audio full retrieval with TMA authorization (HTTP 200)
            resp_audio_full = await client.get(
                f"{base_url}/api/v1/app/tracks/trk_stream1/audio", headers=headers
            )
            assert resp_audio_full.status_code == 200
            assert resp_audio_full.headers["Accept-Ranges"] == "bytes"
            assert resp_audio_full.headers["Content-Type"] == "audio/flac"
            assert len(resp_audio_full.content) == len(dummy_audio_bytes)

            # 5. Audio Range Request via ?tgWebAppData= query parameter (HTTP 206)
            headers_range = {"Range": "bytes=0-15"}
            resp_audio_range = await client.get(
                f"{base_url}/api/v1/app/tracks/trk_stream1/audio?tgWebAppData={quote(dev_token)}",
                headers=headers_range,
            )
            assert resp_audio_range.status_code == 206
            assert "Content-Range" in resp_audio_range.headers
            assert resp_audio_range.headers["Content-Range"].startswith("bytes 0-15/")
            assert len(resp_audio_range.content) == 16
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_track_lyrics_endpoint(tmp_path: Path) -> None:
    """Verify that GET /api/v1/app/tracks/{id}/lyrics returns LRC sidecar data."""
    organizer = LibraryOrganizer(library_root=tmp_path / "lib", enable_network=False)
    server, repo = await _setup_test_server(
        tmp_path, owner_telegram_ids=(123456,), dev_mode=True, organizer=organizer
    )
    try:
        song_dir = tmp_path / "lib" / "Singer" / "Album"
        song_dir.mkdir(parents=True)
        audio_path = song_dir / "song.mp3"
        audio_path.write_bytes(b"ID3" + b"\x00" * 50)
        lrc_path = song_dir / "song.lrc"
        lrc_content = "[00:10.00] Line one\n[00:20.00] Line two\n"
        lrc_path.write_text(lrc_content, encoding="utf-8")

        record = LibraryTrackRecord(
            id="trk_lyrics1",
            job_id="job_lrc",
            uploaded_by=123456,
            storage_path=audio_path,
            fingerprint="fp_lrc",
            duration_seconds=30.0,
            artist="Singer",
            album="Album",
            title="Song",
            has_synced_lyrics=True,
        )
        await repo.upsert_scanned_library_track(record)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            resp = await client.get(f"{base_url}/api/v1/app/tracks/trk_lyrics1/lyrics", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["track_id"] == "trk_lyrics1"
            assert data["has_synced"] is True
            assert "Line one" in data["synced_lyrics"]
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_propose_edit_guest_forbidden(tmp_path: Path) -> None:
    """Verify that a GUEST user cannot propose edits (HTTP 403 Forbidden)."""
    organizer = LibraryOrganizer(library_root=tmp_path / "lib", enable_network=False)
    server, repo = await _setup_test_server(tmp_path, organizer=organizer)
    try:
        # Create a guest user (ID 999999)
        guest_user = UserAccount(telegram_id=999999, display_name="Guest User", role=UserRole.GUEST)
        await repo.upsert_user(guest_user)

        # Create dummy track
        audio_file = tmp_path / "lib" / "Artist" / "Album" / "track.mp3"
        audio_file.parent.mkdir(parents=True)
        audio_file.write_bytes(b"ID3" + b"\x00" * 30)
        track = LibraryTrackRecord(
            id="trk_guest_test",
            job_id="job1",
            uploaded_by=111,
            storage_path=audio_file,
            fingerprint="fp1",
            duration_seconds=10.0,
            artist="Artist",
            album="Album",
            title="Old Title",
        )
        await repo.upsert_scanned_library_track(track)

        now = int(time.time())
        user_json = json.dumps(
            {"id": 999999, "first_name": "Guest", "last_name": "User"},
            separators=(",", ":"),
        )
        init_data = _build_signed_init_data({"auth_date": str(now), "user": user_json})

        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"tma {init_data}"}
            resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_guest_test/propose-edit",
                headers=headers,
                json={"title": "New Title"},
            )
            assert resp.status_code == 403
            assert "Permission denied" in resp.json()["error"]
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_propose_and_apply_edit_flow(tmp_path: Path) -> None:
    """Verify full Dry-Run Proposal and Apply Edit lifecycle."""
    lib_dir = tmp_path / "lib"
    organizer = LibraryOrganizer(library_root=lib_dir, enable_network=False)
    server, repo = await _setup_test_server(
        tmp_path, owner_telegram_ids=(123456,), dev_mode=True, organizer=organizer
    )
    try:
        song_dir = lib_dir / "Original Artist" / "Original Album"
        song_dir.mkdir(parents=True)
        audio_path = song_dir / "01. Original Title.mp3"
        audio_path.write_bytes(b"ID3" + b"\x00" * 50)

        record = LibraryTrackRecord(
            id="trk_studio1",
            job_id="job_studio",
            uploaded_by=123456,
            storage_path=audio_path,
            fingerprint="fp_studio",
            duration_seconds=120.0,
            artist="Original Artist",
            album="Original Album",
            title="Original Title",
            year=2020,
            track_number=1,
            has_synced_lyrics=False,
        )
        await repo.upsert_scanned_library_track(record)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            # 1. Propose Edit (Dry-run preview)
            propose_resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_studio1/propose-edit",
                headers=headers,
                json={
                    "title": "Renamed Title",
                    "artist": "Updated Artist",
                    "album": "Updated Album",
                    "year": 2024,
                    "track_number": 2,
                },
            )
            assert propose_resp.status_code == 200
            prop_data = propose_resp.json()
            assert prop_data["track_id"] == "trk_studio1"
            assert prop_data["path_changed"] is True
            assert any(d["field"] == "title" and d["changed"] for d in prop_data["diff"])
            assert any(d["field"] == "artist" and d["changed"] for d in prop_data["diff"])
            proposal = prop_data["proposal"]
            assert proposal["new_title"] == "Renamed Title"

            # 2. Apply Edit
            apply_resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_studio1/apply-edit",
                headers=headers,
                json={"proposal": proposal},
            )
            assert apply_resp.status_code == 200
            apply_data = apply_resp.json()
            assert apply_data["status"] == "applied"
            assert apply_data["track"]["title"] == "Renamed Title"
            assert apply_data["track"]["artist"] == "Updated Artist"

            # Verify SQLite catalog updated
            updated_record = await repo.get_library_track("trk_studio1")
            assert updated_record is not None
            assert updated_record.title == "Renamed Title"
            assert updated_record.artist == "Updated Artist"
            assert updated_record.year == 2024
            assert updated_record.storage_path.is_file()
            assert "Updated Artist" in str(updated_record.storage_path)

            # Verify audit log recorded
            logs = await repo.list_entity_audit_logs("trk_studio1")
            assert len(logs) >= 1
            assert logs[0].action == "SONG_UPDATED_MINIAPP"
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_cover_art_upload_preview_and_apply(tmp_path: Path) -> None:
    """Verify cover art upload with dry-run preview and apply flag."""
    lib_dir = tmp_path / "lib"
    organizer = LibraryOrganizer(library_root=lib_dir, enable_network=False)
    server, repo = await _setup_test_server(
        tmp_path, owner_telegram_ids=(123456,), dev_mode=True, organizer=organizer
    )
    try:
        song_dir = lib_dir / "Artist" / "Album"
        song_dir.mkdir(parents=True)
        audio_path = song_dir / "track.mp3"
        audio_path.write_bytes(b"ID3" + b"\x00" * 30)

        record = LibraryTrackRecord(
            id="trk_cover1",
            job_id="job_cover",
            uploaded_by=123456,
            storage_path=audio_path,
            fingerprint="fp_cover",
            duration_seconds=60.0,
            artist="Artist",
            album="Album",
            title="Track",
        )
        await repo.upsert_scanned_library_track(record)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        dummy_image = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 40
        b64_img = base64.b64encode(dummy_image).decode("ascii")

        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            # 1. Preview without applying
            prev_resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_cover1/cover",
                headers=headers,
                json={"image_base64": b64_img, "mime_type": "image/jpeg", "apply": False},
            )
            assert prev_resp.status_code == 200
            assert prev_resp.json()["status"] == "preview"
            assert prev_resp.json()["cover_hex"] == dummy_image.hex()
            assert not (song_dir / "cover.jpg").is_file()

            # 2. Apply cover art
            apply_resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_cover1/cover",
                headers=headers,
                json={"image_base64": b64_img, "mime_type": "image/jpeg", "apply": True},
            )
            assert apply_resp.status_code == 200
            assert apply_resp.json()["status"] == "applied"
            assert (song_dir / "cover.jpg").is_file()
            assert (song_dir / "cover.jpg").read_bytes() == dummy_image
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_lyrics_post_preview_and_apply(tmp_path: Path) -> None:
    """Verify lyrics preview diff and apply committing .lrc and updating DB."""
    lib_dir = tmp_path / "lib"
    organizer = LibraryOrganizer(library_root=lib_dir, enable_network=False)
    server, repo = await _setup_test_server(
        tmp_path, owner_telegram_ids=(123456,), dev_mode=True, organizer=organizer
    )
    try:
        song_dir = lib_dir / "Artist" / "Album"
        song_dir.mkdir(parents=True)
        audio_path = song_dir / "track.mp3"
        audio_path.write_bytes(b"ID3" + b"\x00" * 30)

        record = LibraryTrackRecord(
            id="trk_lyrics_post",
            job_id="job_lrc_p",
            uploaded_by=123456,
            storage_path=audio_path,
            fingerprint="fp_lrc_p",
            duration_seconds=60.0,
            artist="Artist",
            album="Album",
            title="Track",
            has_synced_lyrics=False,
        )
        await repo.upsert_scanned_library_track(record)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        lrc_text = "[00:05.00] Hello World\n[00:15.00] Studio Lyrics\n"

        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            # 1. Preview lyrics diff
            prev_resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_lyrics_post/lyrics",
                headers=headers,
                json={"lyrics": lrc_text, "apply": False},
            )
            assert prev_resp.status_code == 200
            prev_data = prev_resp.json()
            assert prev_data["status"] == "preview"
            assert prev_data["new_has_synced"] is True
            assert prev_data["changed"] is True

            # 2. Apply lyrics
            apply_resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_lyrics_post/lyrics",
                headers=headers,
                json={"lyrics": lrc_text, "apply": True},
            )
            assert apply_resp.status_code == 200
            assert apply_resp.json()["status"] == "applied"
            assert apply_resp.json()["has_synced"] is True

            # Verify sidecar written
            lrc_file = audio_path.with_suffix(".lrc")
            assert lrc_file.is_file()
            assert "Hello World" in lrc_file.read_text(encoding="utf-8")

            # Verify SQLite record updated
            rec = await repo.get_library_track("trk_lyrics_post")
            assert rec is not None
            assert rec.has_synced_lyrics is True
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_auto_enrich_endpoint(tmp_path: Path) -> None:
    """Verify auto-enrich endpoint returns candidate matches and lyrics."""
    lib_dir = tmp_path / "lib"
    organizer = LibraryOrganizer(library_root=lib_dir, enable_network=False)

    class StubMetadataService:
        async def read_local_metadata(self, _path: Path) -> TrackMetadata:
            return TrackMetadata(title="Local Title", artist="Local Artist", album="Local Album")

        async def find_candidates(self, _seed: TrackMetadata) -> list[MetadataCandidate]:
            return [
                MetadataCandidate(
                    metadata=TrackMetadata(
                        title="Enriched Title",
                        artist="Enriched Artist",
                        album="Enriched Album",
                        year=2023,
                        track_number=1,
                    ),
                    confidence=0.98,
                )
            ]

    server, repo = await _setup_test_server(
        tmp_path,
        owner_telegram_ids=(123456,),
        dev_mode=True,
        organizer=organizer,
        metadata_service=StubMetadataService(),  # type: ignore
    )
    try:
        song_dir = lib_dir / "Artist" / "Album"
        song_dir.mkdir(parents=True)
        audio_path = song_dir / "track.mp3"
        audio_path.write_bytes(b"ID3" + b"\x00" * 30)

        record = LibraryTrackRecord(
            id="trk_enrich1",
            job_id="job_enr",
            uploaded_by=123456,
            storage_path=audio_path,
            fingerprint="fp_enr",
            duration_seconds=90.0,
            artist="Artist",
            album="Album",
            title="Track",
        )
        await repo.upsert_scanned_library_track(record)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_enrich1/auto-enrich",
                headers=headers,
                json={},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["candidates"]) == 1
            assert data["candidates"][0]["title"] == "Enriched Title"
            assert data["best"]["artist"] == "Enriched Artist"
    finally:
        await server.stop()


def _make_init_data(user_id: int, username: str = "user") -> str:
    user_json = json.dumps(
        {"id": user_id, "first_name": username.title(), "username": username},
        separators=(",", ":"),
    )
    return _build_signed_init_data(
        {"auth_date": str(int(time.time())), "user": user_json},
        bot_token=TEST_BOT_TOKEN,
    )


class StubQueuePipeline:
    def __init__(self, repo: SqliteCatalogRepository) -> None:
        self.repo = repo

    async def process_file(self, *args: Any, **kwargs: Any) -> IngestionResult:
        job_id = kwargs.get("job_id", "test_job")
        job = await self.repo.get_job(job_id)
        if job is None:
            job = IngestionJob(
                job_id=job_id,
                source_path=Path("dummy.mp3"),
                state=JobState.COMPLETED,
            )
        else:
            job.state = JobState.COMPLETED
            await self.repo.save_job(job)
        return IngestionResult(job=job)

    async def cancel_job(self, job_id: str) -> IngestionJob:
        job = await self.repo.get_job(job_id)
        if job is not None:
            job.state = JobState.CANCELLED
            await self.repo.save_job(job)
            return job
        raise KeyError(job_id)

    async def confirm_candidate(
        self,
        job_id: str,
        candidate_index: int,
        staged_audio_path: Path | None = None,
        stage_callback: Any = None,
    ) -> IngestionResult:
        job = await self.repo.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if not (0 <= candidate_index < len(job.candidates)):
            raise IndexError(candidate_index)
        if job_id == "job_fail":
            job.state = JobState.FAILED
            job.error_message = "Simulated pipeline organizer failure"
            await self.repo.save_job(job)
            raise RuntimeError("Simulated pipeline organizer failure")
        if stage_callback is not None:
            job.state = JobState.ORGANIZING
            await stage_callback(job)
        chosen = job.candidates[candidate_index]
        job.selected_metadata = chosen.metadata
        job.state = JobState.COMPLETED
        await self.repo.save_job(job)
        return IngestionResult(job=job, tracks_ingested=1)

    async def search_manual_candidates(self, job_id: str, query: str) -> IngestionResult:
        job = await self.repo.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        job.candidates = [
            MetadataCandidate(
                metadata=TrackMetadata(title=f"Result for {query}", artist="Artist", album="Album"),
                confidence=0.88,
            )
        ]
        job.state = JobState.AWAITING_CONFIRMATION
        await self.repo.save_job(job)
        return IngestionResult(job=job)


@pytest.mark.anyio
async def test_ingest_upload_audio_file(tmp_path: Path) -> None:
    """Verify multipart audio upload enqueues job and begins execution."""
    db_manager = SqliteDatabaseManager(tmp_path / "queue_test.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    pipeline = StubQueuePipeline(repo)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))
    worker = IngestionQueueWorker(pipeline=pipeline, catalog_repository=repo, governance_repository=repo)  # type: ignore

    user = UserAccount(telegram_id=1001, display_name="Owner", role=UserRole.OWNER)
    await repo.upsert_user(user)

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov,
        catalog_repository=repo,
        queue_worker=worker,
        pipeline=pipeline,  # type: ignore
        scratch_dir=tmp_path / "scratch",
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        init_data = _make_init_data(1001, "owner")
        headers = {"Authorization": f"tma {init_data}"}

        audio_content = b"ID3" + b"\x00" * 40
        async with httpx.AsyncClient() as client:
            files = {"file": ("uploaded_song.mp3", audio_content, "audio/mpeg")}
            resp = await client.post(
                f"{base_url}/api/v1/app/ingest/upload",
                headers=headers,
                files=files,
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["success"] is True
            assert data["job"]["original_filename"] == "uploaded_song.mp3"
            assert data["job"]["job_id"]
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_ingest_upload_unsupported_format(tmp_path: Path) -> None:
    """Verify upload rejects unsupported non-audio file extensions with 400 Bad Request."""
    db_manager = SqliteDatabaseManager(tmp_path / "unsupported_test.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    pipeline = StubQueuePipeline(repo)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))
    worker = IngestionQueueWorker(pipeline=pipeline, catalog_repository=repo, governance_repository=repo)  # type: ignore

    user = UserAccount(telegram_id=1001, display_name="Owner", role=UserRole.OWNER)
    await repo.upsert_user(user)

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov,
        catalog_repository=repo,
        queue_worker=worker,
        pipeline=pipeline,  # type: ignore
        scratch_dir=tmp_path / "scratch",
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        init_data = _make_init_data(1001, "owner")
        headers = {"Authorization": f"tma {init_data}"}

        async with httpx.AsyncClient() as client:
            files = {"file": ("malicious.exe", b"binarycontent", "application/octet-stream")}
            resp = await client.post(
                f"{base_url}/api/v1/app/ingest/upload",
                headers=headers,
                files=files,
            )
            assert resp.status_code == 400
            assert "Unsupported file format" in resp.json()["error"]
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_ingest_upload_forbidden_for_guest(tmp_path: Path) -> None:
    """Verify GUEST role is rejected with 403 Forbidden when attempting to upload."""
    db_manager = SqliteDatabaseManager(tmp_path / "guest_test.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    pipeline = StubQueuePipeline(repo)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))
    worker = IngestionQueueWorker(pipeline=pipeline, catalog_repository=repo, governance_repository=repo)  # type: ignore

    guest = UserAccount(telegram_id=9999, display_name="Guest User", role=UserRole.GUEST)
    await repo.upsert_user(guest)

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov,
        catalog_repository=repo,
        queue_worker=worker,
        pipeline=pipeline,  # type: ignore
        scratch_dir=tmp_path / "scratch",
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        init_data = _make_init_data(9999, "guest")
        headers = {"Authorization": f"tma {init_data}"}

        async with httpx.AsyncClient() as client:
            files = {"file": ("track.mp3", b"ID3" + b"\x00" * 30, "audio/mpeg")}
            resp = await client.post(
                f"{base_url}/api/v1/app/ingest/upload",
                headers=headers,
                files=files,
            )
            assert resp.status_code == 403
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_queue_list_and_actions(tmp_path: Path) -> None:
    """Verify queue listing, pause, unlock, cancel, select_candidate, and manual_search actions."""
    db_manager = SqliteDatabaseManager(tmp_path / "queue_actions.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    pipeline = StubQueuePipeline(repo)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))
    worker = IngestionQueueWorker(pipeline=pipeline, catalog_repository=repo, governance_repository=repo)  # type: ignore

    owner = UserAccount(telegram_id=1001, display_name="Owner", role=UserRole.OWNER)
    await repo.upsert_user(owner)

    job1 = IngestionJob(
        job_id="job_q1",
        source_path=tmp_path / "q1.mp3",
        original_filename="q1.mp3",
        state=JobState.QUEUED,
        user_id=1001,
        source_type=SourceType.DIRECT_AUDIO,
    )
    job2 = IngestionJob(
        job_id="job_q2",
        source_path=tmp_path / "q2.mp3",
        original_filename="q2.mp3",
        state=JobState.AWAITING_CONFIRMATION,
        user_id=1001,
        source_type=SourceType.DIRECT_AUDIO,
        candidates=[
            MetadataCandidate(
                metadata=TrackMetadata(title="Candidate Song", artist="Artist", album="Album", year=2024),
                confidence=0.92,
                release_type="Single",
                disambiguation="Radio Edit",
            )
        ],
    )
    await repo.save_job(job1)
    await repo.save_job(job2)

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov,
        catalog_repository=repo,
        queue_worker=worker,
        pipeline=pipeline,  # type: ignore
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        init_data = _make_init_data(1001, "owner")
        headers = {"Authorization": f"tma {init_data}"}

        async with httpx.AsyncClient() as client:
            # 1. List queue
            list_resp = await client.get(f"{base_url}/api/v1/app/queue", headers=headers)
            assert list_resp.status_code == 200
            q_data = list_resp.json()
            assert len(q_data["jobs"]) == 2
            assert q_data["can_view_global"] is True

            # 2. Pause job_q1
            p_resp = await client.post(
                f"{base_url}/api/v1/app/queue/job_q1/action",
                headers=headers,
                json={"action": "pause"},
            )
            assert p_resp.status_code == 200
            assert p_resp.json()["job"]["state"] == "PAUSED"

            # 3. Resume job_q1
            r_resp = await client.post(
                f"{base_url}/api/v1/app/queue/job_q1/action",
                headers=headers,
                json={"action": "resume"},
            )
            assert r_resp.status_code == 200
            assert r_resp.json()["job"]["state"] in {"QUEUED", "PROCESSING", "COMPLETED"}

            # 4. Cancel job_q1
            c_resp = await client.post(
                f"{base_url}/api/v1/app/queue/job_q1/action",
                headers=headers,
                json={"action": "cancel"},
            )
            assert c_resp.status_code == 200
            assert c_resp.json()["job"]["state"] == "CANCELLED"

            # 5. Manual search on job_q2
            ms_resp = await client.post(
                f"{base_url}/api/v1/app/queue/job_q2/action",
                headers=headers,
                json={"action": "manual_search", "query": "New Query"},
            )
            assert ms_resp.status_code == 200
            assert len(ms_resp.json()["job"]["candidates"]) == 1

            # 6. Select candidate on job_q2
            sc_resp = await client.post(
                f"{base_url}/api/v1/app/queue/job_q2/action",
                headers=headers,
                json={"action": "select_candidate", "candidate_index": 0},
            )
            assert sc_resp.status_code == 200
            assert sc_resp.json()["job"]["state"] == "COMPLETED"
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_queue_events_sse(tmp_path: Path) -> None:
    """Verify Server-Sent Events endpoint connects and emits stream."""
    db_manager = SqliteDatabaseManager(tmp_path / "sse_test.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))

    user = UserAccount(telegram_id=1001, display_name="Owner", role=UserRole.OWNER)
    await repo.upsert_user(user)

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov,
        catalog_repository=repo,
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        init_data = _make_init_data(1001, "owner")
        headers = {"Authorization": f"tma {init_data}"}

        async with (
            httpx.AsyncClient() as client,
            client.stream("GET", f"{base_url}/api/v1/app/queue/events", headers=headers) as resp,
        ):
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            # Read the initial "event: connected" line
            lines = []
            async for line in resp.aiter_lines():
                if line:
                    lines.append(line)
                if len(lines) >= 2:
                    break
            assert any("event: connected" in line_str for line_str in lines)
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_admin_users_list_and_role_change(tmp_path: Path) -> None:
    """Verify admin can list users and update roles, while non-admins are forbidden."""
    server, repo = await _setup_test_server(tmp_path, owner_telegram_ids=(1001,))
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"

        # 1. Setup admin and guest users
        admin_user = UserAccount(telegram_id=1001, display_name="Admin", role=UserRole.ADMIN)
        guest_user = UserAccount(telegram_id=2002, display_name="Guest User", role=UserRole.GUEST)
        await repo.upsert_user(admin_user)
        await repo.upsert_user(guest_user)

        admin_headers = {"Authorization": f"tma {_make_init_data(1001, 'admin')}"}
        guest_headers = {"Authorization": f"tma {_make_init_data(2002, 'guest')}"}

        async with httpx.AsyncClient() as client:
            # Guest attempts to access admin users -> 403 Forbidden
            forbidden_resp = await client.get(f"{base_url}/api/v1/app/admin/users", headers=guest_headers)
            assert forbidden_resp.status_code == 403

            # Admin lists users -> 200 OK
            list_resp = await client.get(f"{base_url}/api/v1/app/admin/users", headers=admin_headers)
            assert list_resp.status_code == 200
            users = list_resp.json()["users"]
            assert len(users) >= 2
            telegram_ids = [u["telegram_id"] for u in users]
            assert 1001 in telegram_ids
            assert 2002 in telegram_ids

            # Admin attempts to modify own role -> 400 Bad Request
            self_resp = await client.post(
                f"{base_url}/api/v1/app/admin/users/1001/role",
                headers=admin_headers,
                json={"role": "GUEST"},
            )
            assert self_resp.status_code == 400

            # Admin promotes guest to UPLOADER -> 200 OK
            promote_resp = await client.post(
                f"{base_url}/api/v1/app/admin/users/2002/role",
                headers=admin_headers,
                json={"role": "UPLOADER", "reason": "Mini App test promotion"},
            )
            assert promote_resp.status_code == 200
            assert promote_resp.json()["user"]["role"] == "UPLOADER"

            # Verify persistent change in repo
            updated = await repo.get_user(2002)
            assert updated is not None
            assert updated.role == UserRole.UPLOADER
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_admin_users_block_and_unblock(tmp_path: Path) -> None:
    """Verify admin can block and unblock users, preventing self-blocking."""
    server, repo = await _setup_test_server(tmp_path, owner_telegram_ids=(1001,))
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"

        admin_user = UserAccount(telegram_id=1001, display_name="Admin", role=UserRole.ADMIN)
        target_user = UserAccount(telegram_id=3003, display_name="Target", role=UserRole.UPLOADER)
        await repo.upsert_user(admin_user)
        await repo.upsert_user(target_user)

        admin_headers = {"Authorization": f"tma {_make_init_data(1001, 'admin')}"}

        async with httpx.AsyncClient() as client:
            # Self block -> 400
            self_block = await client.post(
                f"{base_url}/api/v1/app/admin/users/1001/block",
                headers=admin_headers,
                json={"is_blocked": True},
            )
            assert self_block.status_code == 400

            # Block target user -> 200
            block_resp = await client.post(
                f"{base_url}/api/v1/app/admin/users/3003/block",
                headers=admin_headers,
                json={"is_blocked": True, "reason": "Test block"},
            )
            assert block_resp.status_code == 200
            assert block_resp.json()["user"]["is_blocked"] is True

            db_user = await repo.get_user(3003)
            assert db_user is not None
            assert db_user.is_blocked is True

            # Unblock target user -> 200
            unblock_resp = await client.post(
                f"{base_url}/api/v1/app/admin/users/3003/block",
                headers=admin_headers,
                json={"is_blocked": False, "reason": "Test unblock"},
            )
            assert unblock_resp.status_code == 200
            assert unblock_resp.json()["user"]["is_blocked"] is False

            db_user_after = await repo.get_user(3003)
            assert db_user_after is not None
            assert db_user_after.is_blocked is False
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_admin_access_requests_flow(tmp_path: Path) -> None:
    """Verify listing and approving pending access requests via Mini App."""
    server, repo = await _setup_test_server(tmp_path, owner_telegram_ids=(1001,))
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"

        admin_user = UserAccount(telegram_id=1001, display_name="Admin", role=UserRole.ADMIN)
        guest_user = UserAccount(telegram_id=4004, display_name="Applicant", role=UserRole.GUEST)
        await repo.upsert_user(admin_user)
        await repo.upsert_user(guest_user)

        # Create access request
        req = await repo.create_access_request(4004)
        assert req.id is not None

        admin_headers = {"Authorization": f"tma {_make_init_data(1001, 'admin')}"}

        async with httpx.AsyncClient() as client:
            # List access requests
            reqs_resp = await client.get(
                f"{base_url}/api/v1/app/admin/access-requests",
                headers=admin_headers,
            )
            assert reqs_resp.status_code == 200
            req_list = reqs_resp.json()["requests"]
            assert any(r["id"] == req.id for r in req_list)

            # Approve access request
            approve_resp = await client.post(
                f"{base_url}/api/v1/app/admin/access-requests/{req.id}/resolve",
                headers=admin_headers,
                json={"action": "approve"},
            )
            assert approve_resp.status_code == 200
            assert approve_resp.json()["status"] == "APPROVED"

            # Target user is now UPLOADER
            promoted_user = await repo.get_user(4004)
            assert promoted_user is not None
            assert promoted_user.role == UserRole.UPLOADER
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_spa_fallback_vite_dev_proxy(tmp_path: Path) -> None:
    """Verify that in dev_mode, non-API routes are proxied to Vite dev server when running on 5173."""
    mock_vite_app = web.Application()

    async def mock_vite_handler(request: web.Request) -> web.Response:
        return web.Response(
            text="<html><head><title>Vite App</title></head><body>Vite Live HMR</body></html>",
            content_type="text/html",
        )

    mock_vite_app.router.add_route("*", "/{tail:.*}", mock_vite_handler)
    vite_runner = web.AppRunner(mock_vite_app)
    await vite_runner.setup()
    vite_site = web.TCPSite(vite_runner, host="127.0.0.1", port=5173)
    await vite_site.start()

    server, _ = await _setup_test_server(tmp_path, dev_mode=True)
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/catalog")
            assert resp.status_code == 200
            assert "Vite Live HMR" in resp.text
    finally:
        await server.stop()
        await vite_runner.cleanup()


@pytest.mark.anyio
async def test_queue_action_select_candidate_failure_broadcasts_sse(tmp_path: Path) -> None:
    """Verify that when select_candidate pipeline fails, 500 is returned and job_failed is published."""
    db_manager = SqliteDatabaseManager(tmp_path / "fail_sse.db")
    await db_manager.initialize()
    repo = SqliteCatalogRepository(db_manager)
    pipeline = StubQueuePipeline(repo)
    gov = GovernanceService(repo, owner_telegram_ids=(1001,))
    worker = IngestionQueueWorker(pipeline=pipeline, catalog_repository=repo, governance_repository=repo)  # type: ignore

    owner = UserAccount(telegram_id=1001, display_name="Owner", role=UserRole.OWNER)
    await repo.upsert_user(owner)

    job_fail = IngestionJob(
        job_id="job_fail",
        source_path=tmp_path / "fail.mp3",
        original_filename="fail.mp3",
        state=JobState.AWAITING_CONFIRMATION,
        user_id=1001,
        source_type=SourceType.DIRECT_AUDIO,
        candidates=[
            MetadataCandidate(
                metadata=TrackMetadata(title="Fail Song", artist="Artist", album="Album"),
                confidence=0.85,
            )
        ],
    )
    await repo.save_job(job_fail)

    server = BotWebServer(
        bot_token=TEST_BOT_TOKEN,
        governance_service=gov,
        catalog_repository=repo,
        queue_worker=worker,
        pipeline=pipeline,  # type: ignore
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    try:
        base_url = f"http://127.0.0.1:{server.bound_port}"
        init_data = _make_init_data(1001, "owner")
        headers = {"Authorization": f"tma {init_data}"}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/v1/app/queue/job_fail/action",
                headers=headers,
                json={"action": "select_candidate", "candidate_index": 0},
            )
            assert resp.status_code == 500
            err_data = resp.json()
            assert "Ingestion failed" in err_data["error"]

        saved_job = await repo.get_job("job_fail")
        assert saved_job is not None
        assert saved_job.state == JobState.FAILED
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_auto_lyrics_endpoint(tmp_path: Path) -> None:
    """Verify POST /api/v1/app/tracks/{track_id}/auto-lyrics queries LRCLIB and returns lyrics."""

    class StubLyricsProvider:
        def __init__(self, result: LyricsData | None) -> None:
            self.result = result
            self.searches: list[TrackMetadata] = []

        async def fetch_lyrics(self, meta: TrackMetadata) -> LyricsData | None:
            self.searches.append(meta)
            return self.result

    lyrics_data = LyricsData(
        synced_lyrics="[00:01.50] Hello auto world\n[00:04.00] Second line\n",
        plain_lyrics="Hello auto world\nSecond line\n",
    )
    stub_lyrics = StubLyricsProvider(lyrics_data)
    server, repo = await _setup_test_server(
        tmp_path,
        owner_telegram_ids=(123456,),
        dev_mode=True,
        lyrics_client=stub_lyrics,  # type: ignore
    )
    try:
        audio_file = tmp_path / "song.mp3"
        audio_file.write_bytes(b"ID3" + b"\x00" * 30)
        track = LibraryTrackRecord(
            id="trk_lyrics_auto",
            job_id="job_l_auto",
            uploaded_by=123456,
            storage_path=audio_file,
            fingerprint="fp_l_auto",
            duration_seconds=120.0,
            artist="Auto Singer",
            album="Auto Album",
            title="Auto Song",
        )
        await repo.upsert_scanned_library_track(track)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            # 1. Standard auto-lyrics lookup
            resp = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_lyrics_auto/auto-lyrics",
                headers=headers,
                json={},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["found"] is True
            assert data["synced"] is True
            assert "Hello auto world" in data["lyrics"]
            assert data["line_count"] == 2
            assert stub_lyrics.searches[-1].title == "Auto Song"
            assert stub_lyrics.searches[-1].artist == "Auto Singer"

            # 2. Custom query override
            resp_custom = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_lyrics_auto/auto-lyrics",
                headers=headers,
                json={"query": "Custom Artist - Custom Title"},
            )
            assert resp_custom.status_code == 200
            assert stub_lyrics.searches[-1].artist == "Custom Artist"
            assert stub_lyrics.searches[-1].title == "Custom Title"

            # 3. When no lyrics found
            stub_lyrics.result = None
            resp_none = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_lyrics_auto/auto-lyrics",
                headers=headers,
                json={},
            )
            assert resp_none.status_code == 200
            data_none = resp_none.json()
            assert data_none["found"] is False
            assert data_none["lyrics"] is None
            assert data_none["synced"] is False
            assert data_none["line_count"] == 0
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_apply_edit_selective_accepted_fields(tmp_path: Path) -> None:
    """Verify that accepted_fields selectively updates only designated fields, reverting others."""
    lib_dir = tmp_path / "lib"
    organizer = LibraryOrganizer(library_root=lib_dir, enable_network=False)
    server, repo = await _setup_test_server(
        tmp_path,
        owner_telegram_ids=(123456,),
        dev_mode=True,
        organizer=organizer,
    )
    try:
        song_dir = lib_dir / "Original Singer" / "Original Disc"
        song_dir.mkdir(parents=True)
        audio_path = song_dir / "01. Original Song.mp3"
        audio_path.write_bytes(b"ID3" + b"\x00" * 50)
        lrc_path = song_dir / "01. Original Song.lrc"
        lrc_path.write_text("[00:01.00] Original lyrics\n", encoding="utf-8")

        record = LibraryTrackRecord(
            id="trk_selective",
            job_id="job_sel",
            uploaded_by=123456,
            storage_path=audio_path,
            fingerprint="fp_sel",
            duration_seconds=150.0,
            artist="Original Singer",
            album="Original Disc",
            title="Original Song",
            year=2018,
            track_number=1,
            has_synced_lyrics=True,
        )
        await repo.upsert_scanned_library_track(record)

        base_url = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            token_resp = await client.get(f"{base_url}/api/v1/app/dev/token")
            dev_token = token_resp.json()["init_data"]
            headers = {"Authorization": f"tma {dev_token}"}

            # Proposal modifying title, artist, year, and lyrics
            proposal = {
                "track_id": "trk_selective",
                "original_title": "Original Song",
                "new_title": "New Song Title",
                "original_artist": "Original Singer",
                "new_artist": "New Singer Name",
                "original_album": "Original Disc",
                "new_album": "Original Disc",
                "original_year": 2018,
                "new_year": 2026,
                "original_track_number": 1,
                "new_track_number": 1,
                "original_path": str(audio_path),
                "new_path": str(audio_path),
                "diff": [],
                "new_lyrics": "[00:01.00] New auto searched lyrics\n",
                "has_lyrics_change": True,
            }

            # 1. Apply ONLY 'title': artist, year, and lyrics must remain unchanged
            resp_title_only = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_selective/apply-edit",
                headers=headers,
                json={"proposal": proposal, "accepted_fields": ["title"]},
            )
            assert resp_title_only.status_code == 200
            assert resp_title_only.json()["status"] == "applied"

            track_after_title = await repo.get_library_track("trk_selective")
            assert track_after_title is not None
            assert track_after_title.title == "New Song Title"
            assert track_after_title.artist == "Original Singer"  # Preserved!
            assert track_after_title.year == 2018  # Preserved!

            # Check lyrics sidecar still has original lyrics at relocated path
            relocated_lrc = track_after_title.storage_path.with_suffix(".lrc")
            assert "Original lyrics" in relocated_lrc.read_text(encoding="utf-8")

            # 2. Apply ONLY 'lyrics': title stays as is, lyrics updated
            proposal["original_title"] = "New Song Title"
            proposal["new_title"] = "Should Not Change"
            resp_lyrics_only = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_selective/apply-edit",
                headers=headers,
                json={"proposal": proposal, "accepted_fields": ["lyrics"]},
            )
            assert resp_lyrics_only.status_code == 200

            track_after_lyrics = await repo.get_library_track("trk_selective")
            assert track_after_lyrics is not None
            assert track_after_lyrics.title == "New Song Title"  # Preserved!
            # The .lrc sidecar should now contain the new lyrics
            current_lrc = track_after_lyrics.storage_path.with_suffix(".lrc")
            assert current_lrc.is_file()
            assert "New auto searched lyrics" in current_lrc.read_text(encoding="utf-8")

            # 3. Discard all changes by passing empty accepted_fields
            proposal["new_title"] = "Completely Discarded Title"
            resp_none = await client.post(
                f"{base_url}/api/v1/app/tracks/trk_selective/apply-edit",
                headers=headers,
                json={"proposal": proposal, "accepted_fields": []},
            )
            assert resp_none.status_code == 200
            track_after_none = await repo.get_library_track("trk_selective")
            assert track_after_none is not None
            assert track_after_none.title == "New Song Title"  # Preserved!
    finally:
        await server.stop()
