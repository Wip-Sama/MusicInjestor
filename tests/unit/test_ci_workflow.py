"""Unit tests validating GitHub Actions CI/CD workflow, act runner config, and GHCR Docker setup."""

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse a YAML file into a dictionary."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_github_actions_ci_workflow_structure() -> None:
    """Verify `.github/workflows/ci.yml` defines lint, typecheck, test, and GHCR build/publish jobs."""
    workflow_path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow_path.is_file(), "Expected .github/workflows/ci.yml to exist"

    workflow = _load_yaml(workflow_path)
    # PyYAML 1.1 may parse bare `on:` key as boolean True or string "on"
    triggers = workflow.get("on") or workflow.get(True)
    assert isinstance(triggers, dict)
    assert "push" in triggers
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers

    permissions = workflow.get("permissions", {})
    assert permissions.get("packages") == "write"
    assert permissions.get("contents") == "read"

    env = workflow.get("env", {})
    assert env.get("REGISTRY") == "ghcr.io"

    jobs = workflow.get("jobs", {})
    assert "quality-and-test" in jobs
    assert "docker-build-publish" in jobs

    quality_steps = jobs["quality-and-test"]["steps"]
    quality_commands = "\n".join(str(step.get("run", "")) for step in quality_steps)
    assert "uv sync --frozen" in quality_commands
    assert "uv run ruff check" in quality_commands
    assert "uv run ruff format --check" in quality_commands
    assert "uv run ty check" in quality_commands
    assert "uv run pytest" in quality_commands

    docker_job = jobs["docker-build-publish"]
    assert "quality-and-test" in docker_job.get("needs", [])
    docker_steps = docker_job["steps"]
    uses_list = [str(step.get("uses", "")) for step in docker_steps]
    assert any(u.startswith("docker/login-action@") for u in uses_list)
    assert any(u.startswith("docker/metadata-action@") for u in uses_list)
    assert any(u.startswith("docker/build-push-action@") for u in uses_list)


def test_actrc_configures_medium_runner_image() -> None:
    """Verify `.actrc` configures the local `catthehacker/ubuntu:act-latest` medium runner image."""
    actrc_path = PROJECT_ROOT / ".actrc"
    assert actrc_path.is_file(), "Expected .actrc to exist"
    content = actrc_path.read_text(encoding="utf-8")
    assert "ubuntu-latest=catthehacker/ubuntu:act-latest" in content


def test_docker_compose_and_env_support_ghcr_image_pull_and_local_build() -> None:
    """Verify compose files and `.env.example` support `MUSIC_BOT_IMAGE` and local builds."""
    for compose_name in ("docker-compose.yml", "docker-compose.example.yaml"):
        compose_data = _load_yaml(PROJECT_ROOT / compose_name)
        bot_service = compose_data["services"]["music-ingest-bot"]
        assert "MUSIC_BOT_IMAGE" in str(bot_service.get("image", ""))
        assert "build" in bot_service

    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MUSIC_BOT_IMAGE=ghcr.io/" in env_example

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'PATH="/app/.venv/bin:${PATH}"' in dockerfile
    assert "COPY .github/ ./.github/" in dockerfile
