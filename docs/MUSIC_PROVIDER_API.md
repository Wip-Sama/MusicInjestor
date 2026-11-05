# External Music Provider Server Specification & Developer Guide

## 1. Overview & Architecture

The **Music Ingestor System** supports integrating external modular microservices known as **Music Provider Servers**. These servers act as acquisition, ripping, or stream-provisioning workers for external platforms (e.g. Qobuz, Apple Music, Deezer, Tidal, Spotify, YouTube, or generic MusicBrainz/ISRC downloaders).

```text
+-----------------------+                    +---------------------------+
|  Music Ingestor Core  |                    | External Provider Server  |
|  (This Application)   |                    | (e.g. Qobuz / Deezer DL)  |
+-----------------------+                    +---------------------------+
            |                                              |
            | 1. GET /api/v1/capabilities                  |
            |--------------------------------------------->|
            |    (supported sources, audio codecs, flags)  |
            |<---------------------------------------------|
            |                                              |
            | 2. POST /api/v1/resolve                      |
            |--------------------------------------------->|
            |    (match ISRC/MBID, return quality tiers)   |
            |<---------------------------------------------|
            |                                              |
            | 3. POST /api/v1/provision                    |
            |--------------------------------------------->|
            |    (200 OK instant OR 202 Accepted async)    |
            |<---------------------------------------------|
            |                                              |
            | 4. GET /api/v1/jobs/{id} OR Webhook          |
            |--------------------------------------------->|
            |    (poll until completed with download_url)  |
            |<---------------------------------------------|
            |                                              |
            | 5. GET /api/v1/artifacts/{filename}          |
            |--------------------------------------------->|
            |    (download audio + verify SHA-256)         |
            |<=============================================|
            |                                              |
            | 6. Ingest into 10-Stage Pipeline             |
            v                                              v
```

---

## 2. Authentication & Headers

Provider servers may operate on a private Docker bridge network without authentication, or require Bearer token authentication.

- **Header Name:** `Authorization: Bearer <API_TOKEN>`
- **Content-Type:** `application/json`
- **Accept:** `application/json`

When configuring the provider in the Ingestor (`DEFAULT_MUSIC_PROVIDER_TOKEN` or via `/admin`), the configured token is automatically transmitted on all outbound requests.

---

## 3. Core REST Endpoints

### 3.1 Capabilities Handshake: `GET /api/v1/capabilities`

Executed by the Ingestor upon registration, startup, or manual cache refresh. Informs the Ingestor which platforms, audio codecs, and features the provider supports.

#### Response (200 OK)
```json
{
  "provider_name": "qobuz-dl-worker",
  "version": "1.4.0",
  "supported_sources": [
    "spotify",
    "apple_music",
    "qobuz",
    "tidal",
    "deezer",
    "youtube",
    "isrc",
    "musicbrainz"
  ],
  "audio_profiles": {
    "supported_codecs": ["flac", "alac", "aac", "mp3", "opus"],
    "min_bitrate_kbps": 128,
    "max_bitrate_kbps": 9216,
    "min_sample_rate_hz": 44100,
    "max_sample_rate_hz": 192000,
    "min_bit_depth": 16,
    "max_bit_depth": 24
  },
  "features": {
    "streaming": true,
    "stream_formats": ["audio/mpeg", "audio/flac", "audio/ogg"],
    "async_provisioning": true,
    "webhooks": true,
    "batch_provisioning": true
  }
}
```

---

### 3.2 Service Health: `GET /api/v1/health`

Monitors service readiness and upstream external connection status.

#### Response (200 OK)
```json
{
  "status": "healthy",
  "version": "1.4.0",
  "upstream_dependencies": {
    "upstream_api": "connected",
    "storage_backend": "ready"
  },
  "active_jobs_count": 0
}
```

---

### 3.3 Availability & Quality Negotiation: `POST /api/v1/resolve`

Queries whether the provider can source a specific track or album, and reports all available audio quality profiles. **This endpoint MUST NOT trigger a heavy download; it should only inspect catalog availability.**

#### Request Body (Track)
```json
{
  "item_type": "track",
  "identifiers": {
    "isrc": "USUM71703861",
    "spotify_id": "4cOdK2wGLETKBW3PvgPWqT",
    "apple_music_id": "1234567890",
    "deezer_id": "3135556",
    "tidal_id": "7891011",
    "qobuz_id": "123456",
    "youtube_id": "dQw4w9WgXcQ",
    "musicbrainz_recording_id": "863b4f62-3c22-4c28-97c7-0efc0ab43e74"
  },
  "metadata_fallback": {
    "title": "Never Gonna Give You Up",
    "artist": "Rick Astley",
    "album": "Whenever You Need Somebody",
    "duration_ms": 213000
  }
}
```

#### Response (200 OK - Matched)
```json
{
  "matched": true,
  "provider_track_id": "qobuz_trk_987654",
  "matched_source": "isrc",
  "metadata": {
    "title": "Never Gonna Give You Up",
    "artist": "Rick Astley",
    "album": "Whenever You Need Somebody",
    "duration_ms": 213500,
    "explicit": false,
    "isrc": "USUM71703861"
  },
  "available_qualities": [
    {
      "quality_id": "lossless_hi_res",
      "codec": "flac",
      "bit_depth": 24,
      "sample_rate_hz": 96000,
      "bitrate_kbps": 3000,
      "approx_file_size_bytes": 78000000
    },
    {
      "quality_id": "lossless_cd",
      "codec": "flac",
      "bit_depth": 16,
      "sample_rate_hz": 44100,
      "bitrate_kbps": 1411,
      "approx_file_size_bytes": 38000000
    },
    {
      "quality_id": "lossy_high",
      "codec": "mp3",
      "bit_depth": 16,
      "sample_rate_hz": 44100,
      "bitrate_kbps": 320,
      "approx_file_size_bytes": 8500000
    }
  ],
  "streaming_supported": true
}
```

#### Response (200 OK - Not Matched)
```json
{
  "matched": false
}
```

---

### 3.4 Provisioning Request: `POST /api/v1/provision`

Instructs the provider to download, encode, or retrieve the target audio file.

#### Request Body (Track)
```json
{
  "provider_track_id": "qobuz_trk_987654",
  "target_quality": "lossless_hi_res",
  "callback_url": "https://ingestor.local/api/v1/webhooks/provision-complete",
  "priority": "high"
}
```

#### Response Case A: Instant Download Ready (200 OK)
Returned when the artifact is cached or immediately ready on disk.
```json
{
  "status": "ready",
  "download_url": "/api/v1/artifacts/qobuz_trk_987654.flac",
  "expires_at": "2026-11-05T04:00:00Z",
  "checksum": {
    "algorithm": "sha256",
    "hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  },
  "file_info": {
    "format": "flac",
    "size_bytes": 78120340,
    "sample_rate": 96000,
    "bit_depth": 24,
    "bitrate_kbps": 3000
  }
}
```

#### Response Case B: Asynchronous Processing (202 Accepted)
Returned when the provider needs time to download, decrypt, or transcode.
```json
{
  "status": "queued",
  "job_id": "job_99a8b7c6",
  "estimated_completion_seconds": 15,
  "poll_url": "/api/v1/jobs/job_99a8b7c6"
}
```

---

### 3.5 Asynchronous Job Status: `GET /api/v1/jobs/{job_id}`

Used by the Ingestor polling loop when webhooks are disabled or behind a firewall.

#### In-Progress Response (200 OK)
```json
{
  "job_id": "job_99a8b7c6",
  "status": "downloading",
  "progress_percent": 68.5,
  "error": null
}
```

#### Completed Response (200 OK)
```json
{
  "job_id": "job_99a8b7c6",
  "status": "completed",
  "download_url": "/api/v1/artifacts/job_99a8b7c6.flac",
  "expires_at": "2026-11-05T04:30:00Z",
  "checksum": {
    "algorithm": "sha256",
    "hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"
  },
  "file_info": {
    "format": "flac",
    "size_bytes": 78120340,
    "sample_rate": 96000,
    "bit_depth": 24
  }
}
```

#### Failed Response (200 OK)
```json
{
  "job_id": "job_99a8b7c6",
  "status": "failed",
  "error": "Upstream stream encrypted or region restricted"
}
```

---

### 3.6 Cancel Job: `DELETE /api/v1/jobs/{job_id}`

Cancels an in-progress download job. Returns `200 OK` or `204 No Content`.

---

### 3.7 Instant Search-and-Play Streaming: `GET /api/v1/stream/{track_id}`

Enables immediate real-time playback in the Telegram Mini App before or during background ingestion.

- **Query Parameters:**
  - `quality`: e.g. `lossless_hi_res`, `lossy_high`
  - `format`: e.g. `mp3`, `flac`, `ogg`
- **Headers Supported:**
  - `Range: bytes=0-1048575` (Standard HTTP Range Request for browser seeking)
- **Response:**
  - `206 Partial Content` (for range requests) or `200 OK`
  - `Content-Type: audio/mpeg` or `audio/flac`

---

## 4. Reference Implementation Skeleton (FastAPI)

Below is a complete, minimal reference provider server written in Python with FastAPI (~120 lines):

```python
"""Reference Music Provider Server implementing the specification."""

import asyncio
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Example Music Provider", version="1.0.0")

JOBS: dict[str, dict] = {}


class ResolveRequest(BaseModel):
    item_type: str
    identifiers: dict[str, str]
    metadata_fallback: dict = {}


class ProvisionRequest(BaseModel):
    provider_track_id: str
    target_quality: str = "best"
    priority: str = "normal"
    callback_url: str | None = None


@app.get("/api/v1/capabilities")
async def get_capabilities():
    return {
        "provider_name": "example-provider",
        "version": "1.0.0",
        "supported_sources": ["spotify", "deezer", "isrc", "musicbrainz"],
        "audio_profiles": {
            "supported_codecs": ["flac", "mp3"],
            "min_bitrate_kbps": 320,
            "max_bitrate_kbps": 3000,
            "min_sample_rate_hz": 44100,
            "max_sample_rate_hz": 96000,
            "min_bit_depth": 16,
            "max_bit_depth": 24,
        },
        "features": {
            "streaming": True,
            "stream_formats": ["audio/mpeg", "audio/flac"],
            "async_provisioning": True,
            "webhooks": True,
            "batch_provisioning": False,
        },
    }


@app.get("/api/v1/health")
async def get_health():
    return {
        "status": "healthy",
        "version": "1.0.0",
        "upstream_dependencies": {"network": "connected"},
        "active_jobs_count": len([j for j in JOBS.values() if j["status"] == "downloading"]),
    }


@app.post("/api/v1/resolve")
async def resolve(req: ResolveRequest):
    # Match against supported identifiers or fallback title/artist
    title = req.metadata_fallback.get("title") or "Sample Song"
    artist = req.metadata_fallback.get("artist") or "Sample Artist"

    return {
        "matched": True,
        "provider_track_id": f"sample_{req.identifiers.get('isrc') or '123'}",
        "matched_source": "isrc" if "isrc" in req.identifiers else "metadata_fallback",
        "metadata": {
            "title": title,
            "artist": artist,
            "album": req.metadata_fallback.get("album") or "Sample Album",
            "duration_ms": 210000,
            "explicit": False,
        },
        "available_qualities": [
            {
                "quality_id": "lossless_hi_res",
                "codec": "flac",
                "bit_depth": 24,
                "sample_rate_hz": 96000,
                "bitrate_kbps": 3000,
                "approx_file_size_bytes": 75000000,
            },
            {
                "quality_id": "lossy_high",
                "codec": "mp3",
                "bit_depth": 16,
                "sample_rate_hz": 44100,
                "bitrate_kbps": 320,
                "approx_file_size_bytes": 8400000,
            },
        ],
        "streaming_supported": True,
    }


@app.post("/api/v1/provision", status_code=status.HTTP_202_ACCEPTED)
async def provision(req: ProvisionRequest):
    job_id = f"job_{uuid4().hex[:8]}"
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "downloading",
        "progress_percent": 10.0,
        "provider_track_id": req.provider_track_id,
    }

    # Simulate async acquisition in background
    async def _simulate_download():
        await asyncio.sleep(2.0)
        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["progress_percent"] = 100.0
        JOBS[job_id]["download_url"] = f"/api/v1/artifacts/{job_id}.mp3"

    asyncio.create_task(_simulate_download())

    return {
        "status": "queued",
        "job_id": job_id,
        "estimated_completion_seconds": 2,
        "poll_url": f"/api/v1/jobs/{job_id}",
    }


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]
```

---

## 5. Docker Integration Guide

To deploy your provider alongside `music-ingest-bot`, define a new service inside `docker-compose.yml`:

```yaml
services:
  music-provider-qobuz:
    build:
      context: ./providers/qobuz
      dockerfile: Dockerfile
    container_name: music-provider-qobuz
    restart: unless-stopped
    environment:
      - PORT=8000
      - PROVIDER_API_TOKEN=secret_token_123
    networks:
      - bot-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/health"]
      interval: 30s
      timeout: 5s
      retries: 3
```

In `.env` for `music-ingest-bot`:
```bash
DEFAULT_MUSIC_PROVIDER_URL=http://music-provider-qobuz:8000
DEFAULT_MUSIC_PROVIDER_TOKEN=secret_token_123
DEFAULT_MUSIC_PROVIDER_NAME=Qobuz Provider
```

