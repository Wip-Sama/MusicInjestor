"""Targeted coverage tests for SevenZipArchiveExtractor."""

import tarfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.core.exceptions import ArchiveExtractionError
from src.core.extractor import (
    SevenZipArchiveExtractor,
    _extract_archive_fallback,
    find_best_cover_image,
)


def test_find_best_cover_image_edge_cases(tmp_path: Path) -> None:
    """Verify non-directory returns None and fallback to images[0]."""
    assert find_best_cover_image(tmp_path / "ghost") is None

    # Directory with image not matching preferred stems
    img_dir = tmp_path / "images"
    img_dir.mkdir()
    random_img = img_dir / "photo.png"
    random_img.write_bytes(b"PNG")
    assert find_best_cover_image(img_dir) == random_img


def test_extract_archive_fallback_tar_and_errors(tmp_path: Path) -> None:
    """Verify tar archive extraction and error handling."""
    tar_path = tmp_path / "archive.tar"
    with tarfile.open(tar_path, "w") as tf:
        dummy_file = tmp_path / "sample.mp3"
        dummy_file.write_bytes(b"MP3_DATA")
        tf.add(dummy_file, arcname="sample.mp3")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _extract_archive_fallback(tar_path, out_dir)
    assert (out_dir / "sample.mp3").is_file()

    # Unsupported format in fallback
    unsupported = tmp_path / "test.rar"
    unsupported.write_bytes(b"RAR")
    with pytest.raises(ArchiveExtractionError, match="7z binary is not installed"):
        _extract_archive_fallback(unsupported, out_dir)


@pytest.mark.anyio
async def test_extractor_sibling_cover_and_tar_flow(tmp_path: Path) -> None:
    """Verify single file with sibling cover and archive cover extraction."""
    extractor = SevenZipArchiveExtractor(scratch_root=tmp_path / "scratch")

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    audio = source_dir / "track.flac"
    audio.write_bytes(b"FLAC")
    cover = source_dir / "front.jpg"
    cover.write_bytes(b"JPEG")

    staged = await extractor.extract_or_stage(audio, "job_sibling")
    assert len(staged) == 1
    job_scratch = extractor.get_job_scratch_dir("job_sibling")
    assert (job_scratch / "cover.jpg").is_file()

    # Archive with cover image inside
    tar_path = tmp_path / "album.tar"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(audio, arcname="sub/track.flac")
        tf.add(cover, arcname="sub/artwork.jpg")

    with patch.object(extractor, "_run_7z_extract") as mock_7z:
        # Simulate extraction into job scratch
        async def fake_extract(archive_path: Path, output_dir: Path) -> None:
            _extract_archive_fallback(archive_path, output_dir)

        mock_7z.side_effect = fake_extract
        extracted = await extractor.extract_or_stage(tar_path, "job_tar")
        assert len(extracted) == 1
        assert (extracted[0].parent / "cover.jpg").is_file()


@pytest.mark.anyio
async def test_extractor_no_audio_files_and_failure(tmp_path: Path) -> None:
    """Verify ArchiveExtractionError when archive has no audio or 7z returns non-zero."""
    extractor = SevenZipArchiveExtractor(scratch_root=tmp_path / "scratch")

    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("readme.txt", "No audio here")

    with pytest.raises(ArchiveExtractionError, match="contained no supported audio files"):
        await extractor.extract_or_stage(empty_zip, "job_empty")

    # 7z non-zero returncode
    mock_proc = AsyncMock()
    mock_proc.returncode = 2
    mock_proc.communicate.return_value = (b"", b"Fatal error in 7z")

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(ArchiveExtractionError, match="7z extraction failed"),
    ):
        await extractor._run_7z_extract(empty_zip, tmp_path / "out")
