"""Typed environment, .env, and YAML configuration settings for local and Docker runtimes."""

import contextlib
import json
import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_YAML_SECTION_KEY_MAP: dict[str, dict[str, str]] = {
    "telegram": {
        "api_id": "TELEGRAM_API_ID",
        "api_hash": "TELEGRAM_API_HASH",
        "bot_token": "TELEGRAM_BOT_TOKEN",
        "use_local_api": "TELEGRAM_USE_LOCAL_API",
        "local_api_url": "TELEGRAM_LOCAL_API_URL",
        "local_api_work_dir": "TELEGRAM_LOCAL_API_WORK_DIR",
        "local_api_host_dir": "TELEGRAM_LOCAL_API_HOST_DIR",
    },
    "storage": {
        "scratch_dir": "STORAGE_SCRATCH_DIR",
        "library_dir": "STORAGE_LIBRARY_DIR",
        "database_path": "DATABASE_PATH",
        "umask": "UMASK",
    },
    "metadata": {
        "acoustid_api_key": "ACOUSTID_API_KEY",
        "musicbrainz_app_name": "MUSICBRAINZ_APP_NAME",
        "musicbrainz_app_version": "MUSICBRAINZ_APP_VERSION",
        "musicbrainz_contact": "MUSICBRAINZ_CONTACT",
        "genius_access_token": "GENIUS_ACCESS_TOKEN",
        "lastfm_api_key": "LASTFM_API_KEY",
        "lastfm_api_secret": "LASTFM_API_SECRET",
        "lrclib_base_url": "LRCLIB_BASE_URL",
    },
    "pipeline": {
        "auto_approve_confidence": "AUTO_APPROVE_CONFIDENCE",
        "log_level": "LOG_LEVEL",
        "fpcalc_binary": "FPCALC_BINARY",
        "sevenzip_binary": "SEVENZIP_BINARY",
    },
    "governance": {
        "owner_telegram_ids": "OWNER_TELEGRAM_IDS",
        "admin_log_channel_id": "ADMIN_LOG_CHANNEL_ID",
        "queue_concurrency_limit": "QUEUE_CONCURRENCY_LIMIT",
        "secret_key": "SECRET_KEY",
        "node_name": "NODE_NAME",
    },
    "oidc": {
        "enabled": "OIDC_ENABLED",
        "authorize_url": "OIDC_AUTHORIZE_URL",
        "token_url": "OIDC_TOKEN_URL",
        "userinfo_url": "OIDC_USERINFO_URL",
        "client_id": "OIDC_CLIENT_ID",
        "client_secret": "OIDC_CLIENT_SECRET",
        "redirect_uri": "OIDC_REDIRECT_URI",
        "webhook_secret": "OIDC_WEBHOOK_SECRET",
        "listen_host": "OIDC_LISTEN_HOST",
        "listen_port": "OIDC_LISTEN_PORT",
        "sync_interval_seconds": "OIDC_SYNC_INTERVAL_SECONDS",
        "role_mapping": "OIDC_ROLE_MAPPING",
    },
    "webapp": {
        "enabled": "WEB_APP_ENABLED",
        "url": "WEB_APP_URL",
        "static_dir": "WEB_APP_STATIC_DIR",
        "dev_mode": "WEB_APP_DEV_MODE",
        "listen_host": "WEB_LISTEN_HOST",
        "listen_port": "WEB_LISTEN_PORT",
    },
}


def _stringify_yaml_scalar(value: object) -> str:
    """Convert a YAML scalar or list (e.g. `owner_telegram_ids`) into an environment string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _parse_yaml_config_file(yaml_path: Path) -> dict[str, str]:
    """Parse an optional YAML configuration file into canonical uppercase setting keys."""
    values: dict[str, str] = {}
    if not yaml_path.is_file():
        return values

    try:
        loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse YAML config file %s: %s", yaml_path, exc)
        return values

    if not isinstance(loaded, dict):
        return values

    for raw_top_key, top_val in loaded.items():
        top_key = str(raw_top_key).strip()
        lower_section = top_key.lower()
        if lower_section in _YAML_SECTION_KEY_MAP and isinstance(top_val, dict):
            section_map = _YAML_SECTION_KEY_MAP[lower_section]
            for sub_key, sub_val in top_val.items():
                if sub_val is None:
                    continue
                norm_sub = str(sub_key).strip().lower()
                env_key = section_map.get(norm_sub, str(sub_key).strip().upper())
                str_val = _stringify_yaml_scalar(sub_val)
                if str_val != "":
                    values[env_key] = str_val
        elif top_val is not None and not isinstance(top_val, dict):
            str_val = _stringify_yaml_scalar(top_val)
            if str_val != "":
                values[top_key.upper()] = str_val

    return values


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
    genius_access_token: str = Field(default="")
    lastfm_api_key: str = Field(default="")
    lastfm_api_secret: str = Field(default="")
    lrclib_base_url: str = Field(default="https://lrclib.net/api")

    # Storage & SQLite settings
    storage_scratch_dir: Path = Field(default=Path("./storage/scratch"))
    storage_library_dir: Path = Field(default=Path("./storage/library"))
    database_path: Path = Field(default=Path("./storage/library.db"))
    umask: str = Field(default="0022")

    # Pipeline & logging behavior
    auto_approve_confidence: float = Field(default=0.90)
    log_level: str = Field(default="INFO")
    fpcalc_binary: str = Field(default="fpcalc")
    sevenzip_binary: str = Field(default="7z")

    # Governance, RBAC, Queue & Admin Log Channel settings
    owner_telegram_ids: tuple[int, ...] = Field(default=())
    admin_log_channel_id: int | None = Field(default=None)
    queue_concurrency_limit: int = Field(default=1, ge=1, le=4)
    secret_key: str = Field(default="change-me-in-production-secret-key", min_length=16)
    node_name: str = Field(default="Proxmox LXC 104")

    # Authentik OIDC Federation settings
    oidc_enabled: bool = Field(default=False)
    oidc_authorize_url: str = Field(default="https://auth.example.com/application/o/authorize")
    oidc_token_url: str = Field(default="https://auth.example.com/application/o/token")
    oidc_userinfo_url: str = Field(default="https://auth.example.com/application/o/userinfo")
    oidc_client_id: str = Field(default="")
    oidc_client_secret: str = Field(default="")
    oidc_redirect_uri: str = Field(default="http://localhost:8080/api/v1/auth/callback")
    oidc_webhook_secret: str = Field(default="")
    oidc_listen_host: str = Field(default="0.0.0.0")
    oidc_listen_port: int = Field(default=8080, ge=0, le=65535)
    oidc_sync_interval_seconds: int = Field(default=3600, ge=60)
    oidc_role_mapping: dict[str, str] = Field(default_factory=dict)

    # Telegram Mini App & Web Server settings
    web_app_enabled: bool = Field(default=True)
    web_app_url: str = Field(default="")
    web_app_static_dir: Path = Field(default=Path("./webapp/dist"))
    web_app_dev_mode: bool = Field(default=True)
    web_listen_host: str = Field(default="0.0.0.0")
    web_listen_port: int = Field(default=8080, ge=0, le=65535)

    @field_validator("auto_approve_confidence")
    @classmethod
    def validate_confidence_range(cls, value: float) -> float:
        """Ensure confidence threshold falls within [0.0, 1.0]."""
        if not 0.0 <= value <= 1.0:
            msg = "auto_approve_confidence must be between 0.0 and 1.0"
            raise ValueError(msg)
        return value

    @field_validator(
        "telegram_local_api_url",
        "lrclib_base_url",
        "oidc_authorize_url",
        "oidc_token_url",
        "oidc_userinfo_url",
        "web_app_url",
    )
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        """Normalize base URLs by stripping trailing slashes."""
        return value.rstrip("/")

    def ensure_directories(self) -> None:
        """Apply optional process umask and create required scratch, library, and DB directories."""
        if self.umask.strip():
            with contextlib.suppress(ValueError, OSError):
                os.umask(int(self.umask.strip(), 8))
        self.storage_scratch_dir.mkdir(parents=True, exist_ok=True)
        self.storage_library_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "Verified runtime storage directories: scratch=%s, library=%s, db=%s (umask=%s)",
            self.storage_scratch_dir,
            self.storage_library_dir,
            self.database_path,
            self.umask,
        )


def _parse_bool(raw: str) -> bool:
    """Convert standard environment string representations to boolean."""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_tuple(raw: str) -> tuple[int, ...]:
    """Parse a comma-separated string of integers into a tuple."""
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(int(item) for item in parts)


def _parse_role_mapping(raw: object) -> dict[str, str]:
    """Parse OIDC role mapping from dict, JSON string, or comma-separated pairs."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}
    raw_str = str(raw).strip()
    if raw_str.startswith("{") and raw_str.endswith("}"):
        try:
            parsed = json.loads(raw_str)
            if isinstance(parsed, dict):
                return {str(k).strip(): str(v).strip() for k, v in parsed.items() if str(k).strip()}
        except Exception as exc:
            logger.warning("Failed to parse OIDC_ROLE_MAPPING as JSON: %s", exc)
    result: dict[str, str] = {}
    for item in raw_str.split(","):
        item = item.strip()
        if "=" in item:
            grp, role_name = item.split("=", maxsplit=1)
            grp = grp.strip()
            role_name = role_name.strip()
            if grp and role_name:
                result[grp] = role_name
        elif ":" in item:
            grp, role_name = item.split(":", maxsplit=1)
            grp = grp.strip()
            role_name = role_name.strip()
            if grp and role_name:
                result[grp] = role_name
    return result


def load_settings(
    dotenv_path: Path | None = None,
    environ: dict[str, str] | None = None,
    yaml_path: Path | None = None,
) -> AppSettings:
    """Load application settings from optional `config.yaml`, `.env`, and environment variables."""
    resolved_dotenv = dotenv_path if dotenv_path is not None else Path(".env")
    dotenv_env = _parse_dotenv_file(resolved_dotenv)
    source_env = environ if environ is not None else dict(os.environ)

    if yaml_path is not None:
        resolved_yaml = yaml_path
    else:
        configured_yaml = source_env.get("CONFIG_FILE") or dotenv_env.get("CONFIG_FILE") or "config.yaml"
        resolved_yaml = Path(configured_yaml)

    merged_env = _parse_yaml_config_file(resolved_yaml)
    merged_env.update(dotenv_env)
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
        "GENIUS_ACCESS_TOKEN": "genius_access_token",
        "LASTFM_API_KEY": "lastfm_api_key",
        "LASTFM_API_SECRET": "lastfm_api_secret",
        "LRCLIB_BASE_URL": "lrclib_base_url",
        "UMASK": "umask",
        "LOG_LEVEL": "log_level",
        "FPCALC_BINARY": "fpcalc_binary",
        "SEVENZIP_BINARY": "sevenzip_binary",
        "SECRET_KEY": "secret_key",
        "NODE_NAME": "node_name",
        "OIDC_AUTHORIZE_URL": "oidc_authorize_url",
        "OIDC_TOKEN_URL": "oidc_token_url",
        "OIDC_USERINFO_URL": "oidc_userinfo_url",
        "OIDC_CLIENT_ID": "oidc_client_id",
        "OIDC_CLIENT_SECRET": "oidc_client_secret",
        "OIDC_REDIRECT_URI": "oidc_redirect_uri",
        "OIDC_WEBHOOK_SECRET": "oidc_webhook_secret",
        "OIDC_LISTEN_HOST": "oidc_listen_host",
        "WEB_APP_URL": "web_app_url",
        "WEB_LISTEN_HOST": "web_listen_host",
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
        "WEB_APP_STATIC_DIR": "web_app_static_dir",
    }
    for env_key, field_name in path_mappings.items():
        if env_key in merged_env and merged_env[env_key] != "":
            raw_kwargs[field_name] = Path(merged_env[env_key])

    if "TELEGRAM_API_ID" in merged_env and merged_env["TELEGRAM_API_ID"] != "":
        raw_kwargs["telegram_api_id"] = int(merged_env["TELEGRAM_API_ID"])

    if "TELEGRAM_USE_LOCAL_API" in merged_env and merged_env["TELEGRAM_USE_LOCAL_API"] != "":
        raw_kwargs["telegram_use_local_api"] = _parse_bool(merged_env["TELEGRAM_USE_LOCAL_API"])

    if "WEB_APP_ENABLED" in merged_env and merged_env["WEB_APP_ENABLED"] != "":
        raw_kwargs["web_app_enabled"] = _parse_bool(merged_env["WEB_APP_ENABLED"])

    if "WEB_APP_DEV_MODE" in merged_env and merged_env["WEB_APP_DEV_MODE"] != "":
        raw_kwargs["web_app_dev_mode"] = _parse_bool(merged_env["WEB_APP_DEV_MODE"])

    if "AUTO_APPROVE_CONFIDENCE" in merged_env and merged_env["AUTO_APPROVE_CONFIDENCE"] != "":
        raw_kwargs["auto_approve_confidence"] = float(merged_env["AUTO_APPROVE_CONFIDENCE"])

    if "OWNER_TELEGRAM_IDS" in merged_env and merged_env["OWNER_TELEGRAM_IDS"] != "":
        raw_kwargs["owner_telegram_ids"] = _parse_int_tuple(merged_env["OWNER_TELEGRAM_IDS"])

    if "ADMIN_LOG_CHANNEL_ID" in merged_env and merged_env["ADMIN_LOG_CHANNEL_ID"] != "":
        raw_kwargs["admin_log_channel_id"] = int(merged_env["ADMIN_LOG_CHANNEL_ID"])

    if "QUEUE_CONCURRENCY_LIMIT" in merged_env and merged_env["QUEUE_CONCURRENCY_LIMIT"] != "":
        raw_kwargs["queue_concurrency_limit"] = int(merged_env["QUEUE_CONCURRENCY_LIMIT"])

    if "OIDC_ENABLED" in merged_env and merged_env["OIDC_ENABLED"] != "":
        raw_kwargs["oidc_enabled"] = _parse_bool(merged_env["OIDC_ENABLED"])

    if "OIDC_LISTEN_PORT" in merged_env and merged_env["OIDC_LISTEN_PORT"] != "":
        raw_kwargs["oidc_listen_port"] = int(merged_env["OIDC_LISTEN_PORT"])

    if "WEB_LISTEN_PORT" in merged_env and merged_env["WEB_LISTEN_PORT"] != "":
        raw_kwargs["web_listen_port"] = int(merged_env["WEB_LISTEN_PORT"])

    if "OIDC_SYNC_INTERVAL_SECONDS" in merged_env and merged_env["OIDC_SYNC_INTERVAL_SECONDS"] != "":
        raw_kwargs["oidc_sync_interval_seconds"] = int(merged_env["OIDC_SYNC_INTERVAL_SECONDS"])

    if "OIDC_ROLE_MAPPING" in merged_env and merged_env["OIDC_ROLE_MAPPING"] != "":
        raw_kwargs["oidc_role_mapping"] = _parse_role_mapping(merged_env["OIDC_ROLE_MAPPING"])

    return AppSettings.model_validate(raw_kwargs)
