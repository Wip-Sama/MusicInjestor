"""Chromaprint (`fpcalc`) acoustic fingerprinting and AcoustID lookup service."""

import asyncio
import json
import logging
from pathlib import Path

import httpx

from src.core.exceptions import FingerprintingError
from src.core.models import AudioFingerprint

logger = logging.getLogger(__name__)


class ChromaprintFingerprinter:
    """Computes acoustic fingerprints via the `fpcalc` CLI binary and optional AcoustID lookups."""

    def __init__(
        self,
        fpcalc_binary: str = "fpcalc",
        acoustid_api_key: str = "",
        acoustid_base_url: str = "https://api.acoustid.org/v2/lookup",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._fpcalc_binary = fpcalc_binary
        self._acoustid_api_key = acoustid_api_key.strip()
        self._acoustid_base_url = acoustid_base_url
        self._timeout = timeout_seconds
        self._transport = transport

    async def compute_fingerprint(self, audio_path: Path) -> AudioFingerprint:
        """Execute `fpcalc -json` asynchronously to extract duration, Chromaprint hash, and AcoustID MBID."""
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

        acoustid_mbid: str | None = None
        if self._acoustid_api_key:
            acoustid_mbid = await self._lookup_acoustid_mbid(duration, fingerprint)

        logger.debug(
            "Computed fingerprint for %s (duration=%ds, len=%d, acoustid_mbid=%s)",
            audio_path.name,
            duration,
            len(fingerprint),
            acoustid_mbid,
        )
        return AudioFingerprint(
            duration_seconds=duration,
            fingerprint=fingerprint,
            acoustid_mbid=acoustid_mbid,
        )

    async def _lookup_acoustid_mbid(self, duration: int, fingerprint: str) -> str | None:
        """Query the AcoustID API v2 for the top matching MusicBrainz recording MBID."""
        params: dict[str, str | int] = {
            "client": self._acoustid_api_key,
            "duration": duration,
            "fingerprint": fingerprint,
            "meta": "recordings releasegroups compress",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.get(self._acoustid_base_url, params=params)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("AcoustID lookup failed: %s", exc)
            return None

        results = data.get("results")
        if not isinstance(results, list):
            return None
        for result in results:
            if not isinstance(result, dict):
                continue
            recordings = result.get("recordings")
            if isinstance(recordings, list):
                for rec in recordings:
                    if isinstance(rec, dict) and rec.get("id"):
                        return str(rec["id"])
        return None
