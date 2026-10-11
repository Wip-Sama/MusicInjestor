"""Archive unpacking and single-file scratch staging service."""

import asyncio
import logging
import shutil
from pathlib import Path

from src.core.exceptions import ArchiveExtractionError, UnsupportedAudioFormatError

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".mp3", ".m4a", ".alac", ".ogg", ".opus", ".wav", ".wv", ".dsf"}
)
SUPPORTED_ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({".zip", ".7z", ".rar", ".tar", ".gz"})


class SevenZipArchiveExtractor:
    """Extracts incoming archives or stages individual audio tracks in ephemeral scratch directories."""

    def __init__(self, scratch_root: Path, sevenzip_binary: str = "7z") -> None:
        self._scratch_root = scratch_root
        self._sevenzip_binary = sevenzip_binary

    def get_job_scratch_dir(self, job_id: str) -> Path:
        """Return the isolated scratch directory path for a specific job."""
        return self._scratch_root / job_id

    async def extract_or_stage(self, source_path: Path, job_id: str) -> list[Path]:
        """Extract an archive via `7z` or copy a single audio file into the job scratch space."""
        if not source_path.is_file():
            msg = f"Source file does not exist: {source_path}"
            raise ArchiveExtractionError(msg)

        job_dir = self.get_job_scratch_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix.lower()

        if suffix in SUPPORTED_AUDIO_EXTENSIONS:
            staged_path = job_dir / source_path.name
            await asyncio.to_thread(shutil.copy2, source_path, staged_path)
            logger.info("Staged single audio track for job %s at %s", job_id, staged_path)
            return [staged_path]

        if suffix in SUPPORTED_ARCHIVE_EXTENSIONS:
            await self._run_7z_extract(source_path, job_dir)
            discovered = sorted(
                path
                for path in job_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
            )
            if not discovered:
                msg = f"Archive {source_path.name} contained no supported audio files"
                raise ArchiveExtractionError(msg)
            logger.info(
                "Extracted %d audio file(s) for job %s into %s",
                len(discovered),
                job_id,
                job_dir,
            )
            return discovered

        msg = f"Unsupported file extension '{suffix}' for {source_path.name}"
        raise UnsupportedAudioFormatError(msg)

    async def _run_7z_extract(self, archive_path: Path, output_dir: Path) -> None:
        """Invoke the system `7z` binary asynchronously to unpack an archive."""
        process = await asyncio.create_subprocess_exec(
            self._sevenzip_binary,
            "x",
            str(archive_path),
            f"-o{output_dir}",
            "-y",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            msg = f"7z extraction failed (exit code {process.returncode}): {stderr_text or stdout_text}"
            raise ArchiveExtractionError(msg)

    async def cleanup_job_scratch(self, job_id: str) -> None:
        """Delete the ephemeral scratch folder for a completed or aborted job."""
        job_dir = self.get_job_scratch_dir(job_id)
        if job_dir.exists():
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            logger.debug("Cleaned up scratch directory for job %s: %s", job_id, job_dir)
