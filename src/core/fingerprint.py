"""Chromaprint (`fpcalc`) acoustic fingerprinting and AcoustID lookup service."""

import asyncio
import json
import logging
from pathlib import Path

from src.core.exceptions import FingerprintingError
from src.core.models import AudioFingerprint

logger = logging.getLogger(__name__)


class ChromaprintFingerprinter:
    """Computes acoustic fingerprints via the `fpcalc` CLI binary."""

    def __init__(self, fpcalc_binary: str = "fpcalc", acoustid_api_key: str = "") -> None:
        self._fpcalc_binary = fpcalc_binary
        self._acoustid_api_key = acoustid_api_key

    async def compute_fingerprint(self, audio_path: Path) -> AudioFingerprint:
        """Execute `fpcalc -json` asynchronously to extract duration and Chromaprint hash."""
        if not audio_path.is_file():
            msg = f"Cannot fingerprint missing file: {audio_path}"
            raise FingerprintingError(msg)

        try:
            process = await asyncio.create_subprocess_exec(
                self._fpcalc_binary,
                "-json",
                str(audio_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            msg = (
                f"Chromaprint binary '{self._fpcalc_binary}' was not found on PATH. "
                "Install libchromaprint-tools or run inside the Docker container."
            )
            raise FingerprintingError(msg) from exc

        stdout_bytes, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            msg = f"fpcalc failed for {audio_path.name} (exit {process.returncode}): {err_msg}"
            raise FingerprintingError(msg)

        try:
            payload = json.loads(stdout_bytes.decode("utf-8"))
            duration = int(round(float(payload["duration"])))
            fingerprint = str(payload["fingerprint"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            msg = f"Invalid JSON output from fpcalc for {audio_path.name}"
            raise FingerprintingError(msg) from exc

        logger.debug(
            "Computed fingerprint for %s (duration=%ds, len=%d)",
            audio_path.name,
            duration,
            len(fingerprint),
        )
        return AudioFingerprint(
            duration_seconds=duration,
            fingerprint=fingerprint,
            acoustid_mbid=None,
        )
