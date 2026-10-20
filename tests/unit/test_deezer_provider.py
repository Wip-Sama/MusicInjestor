"""Unit tests for the Deezer API release radar provider."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.core.deezer import DeezerReleaseProvider


@pytest.mark.anyio
async def test_deezer_search_artist_exact_match() -> None:
    """Test searching an artist where an exact case-insensitive match is found."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"id": 101, "name": "Someone Else"},
            {"id": 102, "name": "YOASOBI"},
        ]
    }
    mock_client.get.return_value = mock_resp

    provider = DeezerReleaseProvider(client=mock_client)
    artist_id = await provider.search_artist("yoasobi")
    assert artist_id == "102"


@pytest.mark.anyio
async def test_deezer_search_artist_fallback_and_empty() -> None:
    """Test searching an artist with fallback to top result or empty data."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"id": 555, "name": "Similar Artist"},
        ]
    }
    mock_client.get.return_value = mock_resp

    provider = DeezerReleaseProvider(client=mock_client)
    artist_id = await provider.search_artist("Target Artist")
    assert artist_id == "555"

    # Empty data
    mock_resp.json.return_value = {"data": []}
    assert await provider.search_artist("Unknown Artist") is None

    # HTTP Error
    mock_resp.status_code = 500
    assert await provider.search_artist("Error Artist") is None

    # Exception
    mock_client.get.side_effect = httpx.ConnectError("Network down")
    assert await provider.search_artist("Exception Artist") is None


@pytest.mark.anyio
async def test_deezer_get_release_preview_track() -> None:
    """Test fetching the 30-second preview stream track from Deezer album endpoint."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"id": 1, "title": "Track 1", "preview": ""},
            {"id": 2, "title": "Track 2", "preview": "https://cdn.deezer.com/preview.mp3"},
        ]
    }
    mock_client.get.return_value = mock_resp

    provider = DeezerReleaseProvider(client=mock_client)
    res = await provider.get_release_preview_track("12345")
    assert res == ("Track 2", "https://cdn.deezer.com/preview.mp3")

    # When no preview track is found
    mock_resp.json.return_value = {"data": [{"id": 3, "title": "Track 3", "preview": ""}]}
    assert await provider.get_release_preview_track("12345") is None


@pytest.mark.anyio
async def test_deezer_get_artist_recent_releases() -> None:
    """Test fetching recent albums, EPs, and singles for an artist."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    def _mock_get(url: str, **_kwargs: object) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        if "/albums" in url:
            resp.json.return_value = {
                "data": [
                    {
                        "id": 901,
                        "title": "New Single",
                        "record_type": "single",
                        "release_date": "2026-03-15",
                        "cover_xl": "https://cdn.deezer.com/cover.jpg",
                        "link": "https://deezer.com/album/901",
                        "nb_tracks": 1,
                    },
                    {
                        "id": 902,
                        "title": "Studio Album",
                        "record_type": "album",
                        "release_date": "2026-01-20",
                        "cover_big": "https://cdn.deezer.com/album.jpg",
                        "nb_tracks": 10,
                    },
                ]
            }
        elif "/tracks" in url:
            resp.json.return_value = {
                "data": [
                    {"id": 1, "title": "Preview Song", "preview": "https://cdn.deezer.com/preview901.mp3"}
                ]
            }
        else:
            resp.status_code = 404
            resp.json.return_value = {}
        return resp

    mock_client.get.side_effect = _mock_get

    provider = DeezerReleaseProvider(client=mock_client)
    releases = await provider.get_artist_recent_releases("102", "YOASOBI", limit=5)
    assert len(releases) == 2
    assert releases[0].id == "deezer:901"
    assert releases[0].artist_name == "YOASOBI"
    assert releases[0].title == "New Single"
    assert releases[0].release_type == "single"
    assert releases[0].preview_url == "https://cdn.deezer.com/preview901.mp3"
    assert releases[1].id == "deezer:902"
    assert releases[1].track_count == 10
