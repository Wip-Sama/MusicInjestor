"""Unit tests for `src.config.settings`."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.config.settings import AppSettings, _parse_role_mapping, load_settings


def test_default_settings_and_url_normalization() -> None:
    """Verify URL trailing slashes are stripped and defaults are valid."""
    settings = AppSettings(
        telegram_local_api_url="http://localhost:8081/",
        lrclib_base_url="https://lrclib.net/api/",
        oidc_authorize_url="https://auth.example.com/application/o/authorize/",
        oidc_token_url="https://auth.example.com/application/o/token/",
        oidc_userinfo_url="https://auth.example.com/application/o/userinfo/",
        web_app_url="https://music.example.com/",
    )
    assert settings.telegram_local_api_url == "http://localhost:8081"
    assert settings.lrclib_base_url == "https://lrclib.net/api"
    assert settings.oidc_authorize_url == "https://auth.example.com/application/o/authorize"
    assert settings.oidc_token_url == "https://auth.example.com/application/o/token"
    assert settings.oidc_userinfo_url == "https://auth.example.com/application/o/userinfo"
    assert settings.web_app_url == "https://music.example.com"
    assert settings.web_app_enabled is True


def test_invalid_confidence_and_concurrency_raise_validation_error() -> None:
    """Verify confidence outside [0.0, 1.0] and concurrency outside [1, 4] are rejected."""
    with pytest.raises(ValidationError):
        AppSettings(auto_approve_confidence=1.25)

    with pytest.raises(ValidationError):
        AppSettings(auto_approve_confidence=-0.1)

    with pytest.raises(ValidationError):
        AppSettings(queue_concurrency_limit=0)

    with pytest.raises(ValidationError):
        AppSettings(queue_concurrency_limit=5)


def test_load_settings_merges_dotenv_and_environ(tmp_path: Path) -> None:
    """Verify .env file values are loaded and overridden by explicit environment variables."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "# Comment line\n"
        "TELEGRAM_API_ID=111222\n"
        "TELEGRAM_USE_LOCAL_API=false\n"
        "AUTO_APPROVE_CONFIDENCE='0.85'\n"
        "STORAGE_SCRATCH_DIR=./custom/scratch\n"
        "OWNER_TELEGRAM_IDS=1001, 1002\n"
        "ADMIN_LOG_CHANNEL_ID=-100999888\n"
        "QUEUE_CONCURRENCY_LIMIT=2\n"
        "OIDC_ENABLED=true\n",
        encoding="utf-8",
    )

    override_env = {
        "TELEGRAM_API_ID": "333444",
        "TELEGRAM_USE_LOCAL_API": "true",
    }
    loaded = load_settings(
        dotenv_path=dotenv_file,
        environ=override_env,
        yaml_path=tmp_path / "missing.yaml",
    )
    assert loaded.telegram_api_id == 333444
    assert loaded.telegram_use_local_api is True
    assert loaded.auto_approve_confidence == 0.85
    assert loaded.storage_scratch_dir == Path("./custom/scratch")
    assert loaded.owner_telegram_ids == (1001, 1002)
    assert loaded.admin_log_channel_id == -100999888
    assert loaded.queue_concurrency_limit == 2
    assert loaded.oidc_enabled is True


def test_load_settings_yaml_config_and_precedence(tmp_path: Path) -> None:
    """Verify `config.yaml` is loaded and overridden by `.env` and environment variables."""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "telegram:\n"
        "  api_id: 999000\n"
        "  use_local_api: true\n"
        "storage:\n"
        "  library_dir: /mnt/music/library\n"
        "  scratch_dir: /app/storage/scratch\n"
        "  umask: '0002'\n"
        "governance:\n"
        "  owner_telegram_ids:\n"
        "    - 7001\n"
        "    - 7002\n"
        "  node_name: Docker Container\n",
        encoding="utf-8",
    )
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "NODE_NAME=FromDotEnv\n",
        encoding="utf-8",
    )
    loaded = load_settings(
        dotenv_path=dotenv_file,
        environ={"CONFIG_FILE": str(yaml_file)},
    )
    assert loaded.telegram_api_id == 999000
    assert loaded.storage_library_dir == Path("/mnt/music/library")
    assert loaded.storage_scratch_dir == Path("/app/storage/scratch")
    assert loaded.umask == "0002"
    assert loaded.owner_telegram_ids == (7001, 7002)
    assert loaded.node_name == "FromDotEnv"


def test_example_yaml_files_are_valid() -> None:
    """Verify `config.example.yaml`, `docker-compose.yml`, and `docker-compose.example.yaml` parse cleanly."""
    project_root = Path(__file__).resolve().parent.parent.parent
    config_example = project_root / "config.example.yaml"
    compose_main = project_root / "docker-compose.yml"
    compose_example = project_root / "docker-compose.example.yaml"

    assert config_example.is_file()
    assert compose_main.is_file()
    assert compose_example.is_file()

    loaded_settings = load_settings(
        dotenv_path=project_root / ".nonexistent.env",
        environ={},
        yaml_path=config_example,
    )
    assert loaded_settings.storage_library_dir == Path("/mnt/music/library")

    compose_data = yaml.safe_load(compose_example.read_text(encoding="utf-8"))
    assert isinstance(compose_data, dict)
    assert "services" in compose_data
    assert "music-ingest-bot" in compose_data["services"]


def test_ensure_directories_creates_paths(sample_settings: AppSettings) -> None:
    """Verify `ensure_directories` creates scratch, library, and DB parent folders."""
    sample_settings.ensure_directories()
    assert sample_settings.storage_scratch_dir.is_dir()
    assert sample_settings.storage_library_dir.is_dir()
    assert sample_settings.database_path.parent.is_dir()


def test_parse_role_mapping() -> None:
    """Verify role mapping parsing handles dicts, JSON strings, and comma-separated pairs."""
    # Dict input
    assert _parse_role_mapping({"music-admins": "ADMIN", "music-dj": "MODERATOR"}) == {
        "music-admins": "ADMIN",
        "music-dj": "MODERATOR",
    }

    # JSON string input
    json_str = '{"admins": "ADMIN", "vip-listeners": "LISTENER"}'
    assert _parse_role_mapping(json_str) == {
        "admins": "ADMIN",
        "vip-listeners": "LISTENER",
    }

    # Comma-separated pairs with '=' and ':'
    comma_str = "music-admins=ADMIN, music-mods:MODERATOR, music-uploaders=UPLOADER"
    assert _parse_role_mapping(comma_str) == {
        "music-admins": "ADMIN",
        "music-mods": "MODERATOR",
        "music-uploaders": "UPLOADER",
    }

    # Empty and invalid cases
    assert _parse_role_mapping("") == {}
    assert _parse_role_mapping(None) == {}
    assert _parse_role_mapping("invalid_entry_without_delimiter, valid=ADMIN") == {"valid": "ADMIN"}


def test_load_settings_oidc_role_mapping(tmp_path: Path) -> None:
    """Verify OIDC_ROLE_MAPPING is correctly loaded from environment and yaml."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "OIDC_ROLE_MAPPING=authentik-admins=ADMIN,authentik-curators=UPLOADER\n",
        encoding="utf-8",
    )
    loaded = load_settings(
        dotenv_path=dotenv_file,
        environ={},
        yaml_path=tmp_path / "missing.yaml",
    )
    assert loaded.oidc_role_mapping == {
        "authentik-admins": "ADMIN",
        "authentik-curators": "UPLOADER",
    }
