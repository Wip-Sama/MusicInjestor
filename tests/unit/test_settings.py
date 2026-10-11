"""Unit tests for `src.config.settings`."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config.settings import AppSettings, load_settings


def test_default_settings_and_url_normalization() -> None:
    """Verify URL trailing slashes are stripped and defaults are valid."""
    settings = AppSettings(
        telegram_local_api_url="http://localhost:8081/",
        lrclib_base_url="https://lrclib.net/api/",
    )
    assert settings.telegram_local_api_url == "http://localhost:8081"
    assert settings.lrclib_base_url == "https://lrclib.net/api"


def test_invalid_confidence_raises_validation_error() -> None:
    """Verify confidence outside [0.0, 1.0] is rejected."""
    with pytest.raises(ValidationError):
        AppSettings(auto_approve_confidence=1.25)

    with pytest.raises(ValidationError):
        AppSettings(auto_approve_confidence=-0.1)


def test_load_settings_merges_dotenv_and_environ(tmp_path: Path) -> None:
    """Verify .env file values are loaded and overridden by explicit environment variables."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "# Comment line\n"
        "TELEGRAM_API_ID=111222\n"
        "TELEGRAM_USE_LOCAL_API=false\n"
        "AUTO_APPROVE_CONFIDENCE='0.85'\n"
        "STORAGE_SCRATCH_DIR=./custom/scratch\n",
        encoding="utf-8",
    )

    override_env = {
        "TELEGRAM_API_ID": "333444",
        "TELEGRAM_USE_LOCAL_API": "true",
    }
    loaded = load_settings(dotenv_path=dotenv_file, environ=override_env)
    assert loaded.telegram_api_id == 333444
    assert loaded.telegram_use_local_api is True
    assert loaded.auto_approve_confidence == 0.85
    assert loaded.storage_scratch_dir == Path("./custom/scratch")


def test_ensure_directories_creates_paths(sample_settings: AppSettings) -> None:
    """Verify `ensure_directories` creates scratch, library, and DB parent folders."""
    sample_settings.ensure_directories()
    assert sample_settings.storage_scratch_dir.is_dir()
    assert sample_settings.storage_library_dir.is_dir()
    assert sample_settings.database_path.parent.is_dir()
