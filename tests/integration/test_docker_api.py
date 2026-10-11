"""Optional WSL Docker smoke test verifying the local `telegram-bot-api` container."""

import asyncio
import os

import httpx
import pytest

from src.config.settings import load_settings


@pytest.mark.docker
def test_wsl_docker_telegram_bot_api_reachable() -> None:
    """Verify the local `telegram-bot-api` container is reachable when `RUN_WSL_DOCKER_TESTS=1`."""
    if os.environ.get("RUN_WSL_DOCKER_TESTS") != "1":
        pytest.skip("Skipping live WSL Docker smoke test (set RUN_WSL_DOCKER_TESTS=1 when container is up)")

    settings = load_settings()

    async def _probe() -> int:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.telegram_local_api_url}/bot000:test/getMe")
            return response.status_code

    status = asyncio.run(_probe())
    assert status in {200, 401, 404}
