"""Cloudflare Quick Tunnel process manager for automated ephemeral public URL derivation."""

import asyncio
import contextlib
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_QUICK_TUNNEL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
_USE_PATH = object()


def find_cloudflared_binary() -> str | None:
    """Locate the cloudflared executable in PATH or standard system paths."""
    return shutil.which("cloudflared") or shutil.which("cloudflared.exe")


def extract_quick_tunnel_url(text: str) -> str | None:
    """Extract a *.trycloudflare.com URL from console output or log text."""
    match = _QUICK_TUNNEL_PATTERN.search(text)
    return match.group(0) if match else None


class CloudflareQuickTunnel:
    """Manages the lifecycle of an automated `cloudflared` Quick Tunnel subprocess."""

    def __init__(
        self,
        target_url: str = "http://127.0.0.1:8080",
        *,
        cloudflared_path: str | None | object = _USE_PATH,
        log_file: Path | None = None,
    ) -> None:
        self._target_url = target_url
        if cloudflared_path is _USE_PATH:
            self._cloudflared_path = find_cloudflared_binary()
        else:
            self._cloudflared_path = str(cloudflared_path) if cloudflared_path else None
        self._log_file = log_file
        self._proc: asyncio.subprocess.Process | None = None
        self._tunnel_url: str | None = None
        self._url_event = asyncio.Event()
        self._read_tasks: list[asyncio.Task[None]] = []

    @property
    def tunnel_url(self) -> str | None:
        """Return the discovered public trycloudflare.com URL."""
        return self._tunnel_url

    @property
    def is_running(self) -> bool:
        """Return True if the cloudflared subprocess is currently active."""
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        """Return the OS process identifier for the active tunnel."""
        return self._proc.pid if self._proc is not None else None

    async def _read_stream(self, stream: asyncio.StreamReader | None, stream_name: str) -> None:
        """Read output stream line-by-line, extract quick tunnel URL, and forward informative logs."""
        if stream is None:
            return

        while True:
            try:
                line = await stream.readline()
            except Exception:
                break

            if not line:
                break

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            if self._tunnel_url is None:
                detected = extract_quick_tunnel_url(text)
                if detected:
                    self._tunnel_url = detected
                    self._url_event.set()
                    logger.info("Cloudflare Quick Tunnel established: %s", detected)

            # Log warnings or errors from cloudflared
            if "ERR" in text or "error" in text.lower():
                logger.warning("[cloudflared %s] %s", stream_name, text)
            else:
                logger.debug("[cloudflared %s] %s", stream_name, text)

    async def start(self, timeout_seconds: float = 25.0) -> str | None:
        """Launch the `cloudflared` subprocess and wait until the quick tunnel URL is announced."""
        # 1. Check if an external log file already has a recent quick tunnel URL
        if self._log_file and self._log_file.is_file():
            try:
                content = self._log_file.read_text(encoding="utf-8", errors="replace")
                existing_url = extract_quick_tunnel_url(content)
                if existing_url:
                    self._tunnel_url = existing_url
                    logger.info("Discovered active Cloudflare Quick Tunnel from log file: %s", existing_url)
                    return existing_url
            except Exception as exc:
                logger.debug("Could not read tunnel log file %s: %s", self._log_file, exc)

        # 2. Check binary availability
        if not self._cloudflared_path:
            logger.warning(
                "Cannot start automated quick tunnel: 'cloudflared' binary was not found in PATH. "
                "Please install cloudflared or specify CLOUDFLARED_TUNNEL_URL in your .env file."
            )
            return None

        # 3. Spawn cloudflared quick tunnel
        logger.info(
            "Launching automated Cloudflare Quick Tunnel targeting %s via %s...",
            self._target_url,
            self._cloudflared_path,
        )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._cloudflared_path,
                "tunnel",
                "--url",
                self._target_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error("Failed to execute cloudflared subprocess: %s", exc)
            return None

        # 4. Stream reader tasks
        self._url_event.clear()
        self._read_tasks = [
            asyncio.create_task(self._read_stream(self._proc.stdout, "stdout")),
            asyncio.create_task(self._read_stream(self._proc.stderr, "stderr")),
        ]

        # 5. Wait for the URL to be parsed
        try:
            await asyncio.wait_for(self._url_event.wait(), timeout=timeout_seconds)
            return self._tunnel_url
        except TimeoutError:
            logger.warning(
                "Timed out waiting for Cloudflare Quick Tunnel URL after %.1f seconds.",
                timeout_seconds,
            )
            return None

    async def stop(self) -> None:
        """Gracefully terminate the cloudflared process and cancel reader tasks."""
        if self._proc is not None and self._proc.returncode is None:
            logger.info("Stopping Cloudflare Quick Tunnel (PID %d)...", self._proc.pid)
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                with contextlib.suppress(Exception):
                    self._proc.kill()
            self._proc = None

        for task in self._read_tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._read_tasks.clear()
