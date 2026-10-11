"""Unit tests for the Telegram interface layer (`src.bot`)."""

from pathlib import Path

import pytest
from aiogram.client.telegram import PRODUCTION

from src.bot.api_server import build_telegram_api_server, resolve_local_api_file_path
from src.bot.keyboards.verification import build_verification_keyboard
from src.bot.routers import build_root_router
from src.bot.routers.files import format_ingestion_summary
from src.bot.routers.verification import parse_verification_callback
from src.config.settings import AppSettings
from src.core.models import (
    DuplicateCheckResult,
    IngestionJob,
    IngestionResult,
    JobState,
    MetadataCandidate,
    TrackMetadata,
)


def test_build_telegram_api_server_local_vs_production(sample_settings: AppSettings) -> None:
    """Verify `build_telegram_api_server` switches between `--local` server and PRODUCTION."""
    local_server = build_telegram_api_server(sample_settings)
    assert local_server.is_local is True
    assert "localhost:8081" in local_server.base

    prod_settings = sample_settings.model_copy(update={"telegram_use_local_api": False})
    prod_server = build_telegram_api_server(prod_settings)
    assert prod_server == PRODUCTION


def test_resolve_local_api_file_path_hybrid_and_fallback(sample_settings: AppSettings) -> None:
    """Verify container `/var/lib/telegram-bot-api/...` paths map to host bind-mount directory."""
    container_reported = "/var/lib/telegram-bot-api/12345:TOKEN/music/file_42.flac"
    resolved = resolve_local_api_file_path(container_reported, sample_settings)
    expected = sample_settings.telegram_local_api_host_dir / "12345:TOKEN" / "music" / "file_42.flac"
    assert resolved == expected

    unrelated_path = "/tmp/other_folder/song.flac"
    assert resolve_local_api_file_path(unrelated_path, sample_settings) == Path(unrelated_path)


def test_verification_keyboard_and_callback_parsing() -> None:
    """Verify inline keyboard construction and callback data parsing."""
    candidates = [
        MetadataCandidate(
            metadata=TrackMetadata(title="Track A", artist="Artist", album="Album A", year=2001),
            confidence=0.82,
        )
    ]
    markup = build_verification_keyboard("job99", candidates)
    assert len(markup.inline_keyboard) == 2
    first_btn = markup.inline_keyboard[0][0]
    assert first_btn.callback_data == "verify:select:job99:0"

    action, job_id, idx = parse_verification_callback("verify:select:job99:0")
    assert action == "select"
    assert job_id == "job99"
    assert idx == 0

    cancel_action, cancel_job, cancel_idx = parse_verification_callback("verify:cancel:job99")
    assert cancel_action == "cancel"
    assert cancel_job == "job99"
    assert cancel_idx is None

    with pytest.raises(ValueError, match="Malformed"):
        parse_verification_callback("invalid:data")


def test_format_ingestion_summary_variants() -> None:
    """Verify Telegram HTML summary formatting for duplicate, confirmation, and completed states."""
    job = IngestionJob(job_id="j1", source_path=Path("a.flac"))

    dup_result = IngestionResult(
        job=job,
        duplicate=DuplicateCheckResult(
            is_duplicate=True,
            existing_path=Path("/music/a.flac"),
            match_reason="fingerprint",
        ),
    )
    assert "Duplicate Rejected" in format_ingestion_summary(dup_result)

    confirm_result = IngestionResult(job=job, requires_user_confirmation=True)
    assert "Confirmation Required" in format_ingestion_summary(confirm_result)

    job.final_path = Path("/music/Artist/Album/01 - a.flac")
    job.state = JobState.COMPLETED
    done_result = IngestionResult(job=job)
    assert "Track Ingested" in format_ingestion_summary(done_result)


def test_build_root_router_attaches_sub_routers() -> None:
    """Verify root router includes commands, files, and verification sub-routers."""
    root = build_root_router()
    assert len(root.sub_routers) == 3
