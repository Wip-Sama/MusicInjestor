"""Container entrypoint running both the music provider and main ingestion bot."""

import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [COMBINED] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_combined")


def wait_for_provider_health(
    base_url: str,
    timeout_seconds: float = 20.0,
    provider_proc: subprocess.Popen[Any] | None = None,
) -> bool:
    """Poll the provider health endpoint until HTTP 200, early exit, or timeout."""
    health_url = f"{base_url.rstrip('/')}/api/v1/health"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if provider_proc is not None and provider_proc.poll() is not None:
            return False
        try:
            req = urllib.request.Request(health_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def terminate_processes(procs: list[subprocess.Popen[Any] | None], grace_seconds: float = 5.0) -> None:
    """Gracefully terminate child processes, escalating to SIGKILL after grace_seconds."""
    for proc in procs:
        if proc is not None and proc.poll() is None:
            logger.info("Terminating process PID %d...", proc.pid)
            with contextlib.suppress(Exception):
                proc.terminate()

    deadline = time.monotonic() + grace_seconds
    for proc in procs:
        if proc is not None and proc.poll() is None:
            remaining = max(0.1, deadline - time.monotonic())
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=remaining)

    for proc in procs:
        if proc is not None and proc.poll() is None:
            logger.warning("Force killing process PID %d...", proc.pid)
            with contextlib.suppress(Exception):
                proc.kill()


def main() -> int:
    """Start the provider microservice, wait for readiness, then launch the main bot."""
    provider_port = os.getenv("PROVIDER_PORT", "8085")
    provider_url = f"http://127.0.0.1:{provider_port}"

    provider_proc: subprocess.Popen[Any] | None = None
    bot_proc: subprocess.Popen[Any] | None = None
    shutting_down = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        logger.info("Received signal %d; shutting down provider and bot...", signum)
        terminate_processes([bot_proc, provider_proc])

    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    exit_code = 0
    try:
        logger.info("Starting Music Provider on port %s...", provider_port)
        provider_proc = subprocess.Popen([sys.executable, "-m", "provider.main"])

        if not wait_for_provider_health(provider_url, timeout_seconds=20.0, provider_proc=provider_proc):
            if provider_proc.poll() is not None:
                logger.error("Provider exited during startup with code %d.", provider_proc.returncode)
                return provider_proc.returncode or 1
            logger.warning(
                "Provider health check did not return 200 within 20s; continuing to launch main bot."
            )
        else:
            logger.info("Music Provider is healthy at %s.", provider_url)

        if shutting_down:
            return 0

        bot_env = os.environ.copy()
        bot_env.setdefault("DEFAULT_MUSIC_PROVIDER_URL", provider_url)
        if not bot_env.get("DEFAULT_MUSIC_PROVIDER_TOKEN") and bot_env.get("PROVIDER_API_TOKEN"):
            bot_env["DEFAULT_MUSIC_PROVIDER_TOKEN"] = bot_env["PROVIDER_API_TOKEN"]

        logger.info("Starting Music Ingestion Bot...")
        bot_proc = subprocess.Popen([sys.executable, "main.py"], env=bot_env)

        while not shutting_down:
            if provider_proc.poll() is not None:
                logger.error("Provider exited unexpectedly with code %d.", provider_proc.returncode)
                exit_code = provider_proc.returncode or 1
                break
            if bot_proc.poll() is not None:
                logger.info("Main bot exited with code %d.", bot_proc.returncode)
                exit_code = bot_proc.returncode or 0
                break
            time.sleep(0.5)
    finally:
        terminate_processes([bot_proc, provider_proc])

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
