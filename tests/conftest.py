"""Shared pytest fixtures for unit and integration testing."""

from pathlib import Path

import pytest

from src.config.settings import AppSettings


@pytest.fixture
def sample_settings(tmp_path: Path) -> AppSettings:
    """Provide isolated AppSettings backed by a temporary directory."""
    return AppSettings(
        telegram_api_id=999999,
        telegram_api_hash="abcdef0123456789abcdef0123456789",
        telegram_bot_token="123456789:AAFakeTokenForTestingPurposesOnly",
        telegram_use_local_api=True,
        telegram_local_api_url="http://localhost:8081",
        telegram_local_api_work_dir=Path("/var/lib/telegram-bot-api"),
        telegram_local_api_host_dir=tmp_path / "telegram-bot-api",
        storage_scratch_dir=tmp_path / "scratch",
        storage_library_dir=tmp_path / "library",
        database_path=tmp_path / "test_library.db",
        auto_approve_confidence=0.90,
    )
