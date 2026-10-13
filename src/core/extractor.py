"""Archive unpacking and single-file scratch staging service."""

import asyncio
import contextlib
import logging
import shutil
import tarfile
import zipfile
from pathlib import Path

from src.core.exceptions import ArchiveExtractionError, UnsupportedAudioFormatError

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".mp3", ".m4a", ".alac", ".ogg", ".opus", ".wav", ".wv", ".dsf"}
)
SUPPORTED_ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({".zip", ".7z", ".rar", ".tar", ".gz"})
SUPPORTED_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
PREFERRED_COVER_STEMS: tuple[str, ...] = ("cover", "folder", "front", "album", "art", "artwork")


def find_best_cover_image(search_dir: Path, recursive: bool = False) -> Path | None:
    """Locate the best candidate cover art image inside `search_dir`."""
    if not search_dir.is_dir():
        return None
    iterator = search_dir.rglob("*") if recursive else search_dir.iterdir()
    images = sorted(
        path for path in iterator if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    )
    if not images:
        return None
    for preferred in PREFERRED_COVER_STEMS:
        for img in images:
            if img.stem.lower() == preferred or img.stem.lower().startswith(preferred):
                return img
    return images[0]


def _extract_archive_fallback(archive_path: Path, output_dir: Path) -> None:
    """Extract `.zip` or `.tar`/`.gz` archives using Python's standard library when `7z` is unavailable."""
    suffix = archive_path.suffix.lower()
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(output_dir)
            return
        if suffix in {".tar", ".gz"}:
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(output_dir, filter="data")
            return
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        msg = f"Archive extraction failed for {archive_path.name}: {exc}"
        raise ArchiveExtractionError(msg) from exc

    msg = (
        f"7z binary is not installed on PATH and stdlib fallback does not support '{suffix}' "
        f"for {archive_path.name}."
    )
    raise ArchiveExtractionError(msg)


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
            with contextlib.suppress(OSError):
                staged_path.chmod(0o644)
            sibling_cover = find_best_cover_image(source_path.parent, recursive=False)
            if sibling_cover is not None:
                staged_cover = job_dir / "cover.jpg"
                if not staged_cover.exists():
                    await asyncio.to_thread(shutil.copy2, sibling_cover, staged_cover)
                    with contextlib.suppress(OSError):
                        staged_cover.chmod(0o644)
            logger.info("Staged single audio track for job %s at %s", job_id, staged_path)
            return [staged_path]

        if suffix in SUPPORTED_ARCHIVE_EXTENSIONS:
            await self._run_7z_extract(source_path, job_dir)
            for p in job_dir.rglob("*"):
                with contextlib.suppress(OSError):
                    if p.is_file():
                        p.chmod(0o644)
                    elif p.is_dir():
                        p.chmod(0o755)
            discovered = sorted(
                path
                for path in job_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
            )
            if not discovered:
                msg = f"Archive {source_path.name} contained no supported audio files"
                raise ArchiveExtractionError(msg)

            best_image = find_best_cover_image(job_dir, recursive=True)
            if best_image is not None:
                audio_dir = discovered[0].parent
                target_cover = audio_dir / "cover.jpg"
                if not target_cover.exists() and best_image.resolve() != target_cover.resolve():
                    await asyncio.to_thread(shutil.copy2, best_image, target_cover)
                    with contextlib.suppress(OSError):
                        target_cover.chmod(0o644)

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
        """Invoke the system `7z` binary asynchronously to unpack an archive, with stdlib fallback."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._sevenzip_binary,
                "x",
                str(archive_path),
                f"-o{output_dir}",
                "-y",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.info(
                "Binary '%s' not found on PATH; using Python stdlib fallback for %s",
                self._sevenzip_binary,
                archive_path.name,
            )
            await asyncio.to_thread(_extract_archive_fallback, archive_path, output_dir)
            return

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
