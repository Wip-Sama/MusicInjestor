"""Typed environment settings and path resolution for local and Docker runtimes."""

import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


def _parse_dotenv_file(dotenv_path: Path) -> dict[str, str]:
    """Parse a simple key=value .env file without external dependencies."""
    values: dict[str, str] = {}
    if not dotenv_path.is_file():
        return values

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_val = line.split("=", maxsplit=1)
        cleaned_key = key.strip()
        cleaned_val = raw_val.strip().strip("'").strip('"')
        if cleaned_key:
            values[cleaned_key] = cleaned_val
    return values


class AppSettings(BaseModel):
    """Centralized configuration for Telegram API, core ingestion engine, and SQLite."""

    # Telegram Bot & Local Bot API settings
    telegram_api_id: int = Field(default=12345678, ge=1)
    telegram_api_hash: str = Field(default="0123456789abcdef0123456789abcdef", min_length=8)
    telegram_bot_token: str = Field(default="123456789:AAExampleBotTokenStringHere", min_length=10)
    telegram_use_local_api: bool = Field(default=True)
    telegram_local_api_url: str = Field(default="http://localhost:8081")
    telegram_local_api_work_dir: Path = Field(default=Path("/var/lib/telegram-bot-api"))
    telegram_local_api_host_dir: Path = Field(default=Path("./storage/telegram-bot-api"))

    # External metadata providers
    acoustid_api_key: str = Field(default="")
    musicbrainz_app_name: str = Field(default="MusicIngestBot")
    musicbrainz_app_version: str = Field(default="0.1.0")
    musicbrainz_contact: str = Field(default="admin@example.com")
    lrclib_base_url: str = Field(default="https://lrclib.net/api")

    # Storage & SQLite settings
    storage_scratch_dir: Path = Field(default=Path("./storage/scratch"))
    storage_library_dir: Path = Field(default=Path("./storage/library"))
    database_path: Path = Field(default=Path("./storage/library.db"))

    # Pipeline & logging behavior
    auto_approve_confidence: float = Field(default=0.90)
    log_level: str = Field(default="INFO")
    fpcalc_binary: str = Field(default="fpcalc")
    sevenzip_binary: str = Field(default="7z")

    @field_validator("auto_approve_confidence")
    @classmethod
    def validate_confidence_range(cls, value: float) -> float:
        """Ensure confidence threshold falls within [0.0, 1.0]."""
        if not 0.0 <= value <= 1.0:
            msg = "auto_approve_confidence must be between 0.0 and 1.0"
            raise ValueError(msg)
        return value

    @field_validator("telegram_local_api_url", "lrclib_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        """Normalize base URLs by stripping trailing slashes."""
        return value.rstrip("/")

    def ensure_directories(self) -> None:
        """Create required runtime directories for scratch, library, and SQLite parent."""
        self.storage_scratch_dir.mkdir(parents=True, exist_ok=True)
        self.storage_library_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "Verified runtime storage directories: scratch=%s, library=%s, db=%s",
            self.storage_scratch_dir,
            self.storage_library_dir,
            self.database_path,
        )


def _parse_bool(raw: str) -> bool:
    """Convert standard environment string representations to boolean."""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(
    dotenv_path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> AppSettings:
    """Load application settings from an optional .env file and environment variables."""
    resolved_dotenv = dotenv_path if dotenv_path is not None else Path(".env")
    merged_env = _parse_dotenv_file(resolved_dotenv)
    source_env = environ if environ is not None else dict(os.environ)
    merged_env.update(source_env)

    raw_kwargs: dict[str, object] = {}

    str_mappings = {
        "TELEGRAM_API_HASH": "telegram_api_hash",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_LOCAL_API_URL": "telegram_local_api_url",
        "ACOUSTID_API_KEY": "acoustid_api_key",
        "MUSICBRAINZ_APP_NAME": "musicbrainz_app_name",
        "MUSICBRAINZ_APP_VERSION": "musicbrainz_app_version",
        "MUSICBRAINZ_CONTACT": "musicbrainz_contact",
        "LRCLIB_BASE_URL": "lrclib_base_url",
        "LOG_LEVEL": "log_level",
        "FPCALC_BINARY": "fpcalc_binary",
        "SEVENZIP_BINARY": "sevenzip_binary",
    }
    for env_key, field_name in str_mappings.items():
        if env_key in merged_env and merged_env[env_key] != "":
            raw_kwargs[field_name] = merged_env[env_key]

    path_mappings = {
        "TELEGRAM_LOCAL_API_WORK_DIR": "telegram_local_api_work_dir",
        "TELEGRAM_LOCAL_API_HOST_DIR": "telegram_local_api_host_dir",
        "STORAGE_SCRATCH_DIR": "storage_scratch_dir",
        "STORAGE_LIBRARY_DIR": "storage_library_dir",
        "DATABASE_PATH": "database_path",
    }
    for env_key, field_name in path_mappings.items():
        if env_key in merged_env and merged_env[env_key] != "":
            raw_kwargs[field_name] = Path(merged_env[env_key])

    if "TELEGRAM_API_ID" in merged_env and merged_env["TELEGRAM_API_ID"] != "":
        raw_kwargs["telegram_api_id"] = int(merged_env["TELEGRAM_API_ID"])

    if "TELEGRAM_USE_LOCAL_API" in merged_env and merged_env["TELEGRAM_USE_LOCAL_API"] != "":
        raw_kwargs["telegram_use_local_api"] = _parse_bool(merged_env["TELEGRAM_USE_LOCAL_API"])

    if "AUTO_APPROVE_CONFIDENCE" in merged_env and merged_env["AUTO_APPROVE_CONFIDENCE"] != "":
        raw_kwargs["auto_approve_confidence"] = float(merged_env["AUTO_APPROVE_CONFIDENCE"])

    return AppSettings.model_validate(raw_kwargs)
