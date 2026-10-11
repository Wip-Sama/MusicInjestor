# Project Specification: Automated Music Ingestion System (Telegram & Core Engine)

## 1. Executive Summary & Problem Statement
Managing a personal, self-hosted music server (e.g., Navidrome, Jellyfin, LMS, Plex) presents operational friction during ingestion. Currently, acquiring audio, downloading, tagging, formatting directory paths, fetching lyrics, and transferring files involves multiple context switches across different tools, web interfaces, and manual file operations.

This project implements an **automated, self-hosted ingestion system** centered around a **Telegram Bot interface**. The system accepts raw audio tracks, compressed archives, or links, inspects and enriches their metadata (via MusicBrainz, AcoustID, and LRCLIB), checks for library duplicates using audio fingerprinting, interactively resolves ambiguities via Telegram UI buttons, and cleanly deposits finalized assets into the music library storage.

To avoid premature distributed complexity while preserving future architectural agility, the system is designed as a **Modular Monolith in Python**. It runs inside a single Proxmox LXC container with strict separation of concerns, enabling future Web UI implementations without needing to rewrite ingestion logic.

---

## 2. Architecture & Design Principles

### 2.1 The Modular Monolith Concept
The application runs as a unified Python async runtime using a Hexagonal `src/` layout. A strict boundary is enforced (and verified automatically by `tests/test_architecture.py`):
* **Interface Layer (`src/bot/`):** Contains Telegram-specific logic (aiogram 3.x), routers, command handlers, local API path translation, and interactive Finite State Machines (FSM). It *never* directly performs file manipulation or external metadata polling.
* **Core Business Logic (`src/core/`):** Houses pure domain modules (archive unpacking, audio tagging, acoustic fingerprinting, duplicate detection, lyrics retrieval, storage movement, and `Protocol` ports). **The `src/core/` package must NEVER import `aiogram`, `src.bot`, `src.db`, or `src.config`.**
* **Persistence Layer (`src/db/`):** Manages SQLite (WAL mode) containing track fingerprints, ingestion job states, and match history, implementing `CatalogRepositoryPort` from `src/core/ports.py`.
* **Configuration Layer (`src/config/`):** Centralizes environment variable and `.env` loading across local `uv` and Docker runtimes.

This design guarantees that when an external Web UI or userbot service is added later—or if `src/core/` is extracted into an independent microservice—it interacts purely through `src/core/` ports without refactoring the ingestion engine.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Proxmox LXC Container                         │
│                                                                         │
│  ┌───────────────────────────────┐     ┌─────────────────────────────┐  │
│  │   telegram-bot-api (Docker)   │     │    Music Storage Volume     │  │
│  │   --local mode (2GB limit)    │     │      /mnt/music/library     │  │
│  └───────────────▲───────────────┘     └──────────────▲──────────────┘  │
│                  │ Local Disk I/O                     │ Atomic Move     │
│                  ▼                                    │                 │
│  ┌────────────────────────────────────────────────────┴──────────────┐  │
│  │                    Ingestion Service (Python)                     │  │
│  │                                                                   │  │
│  │  ┌───────────────────────┐         ┌───────────────────────────┐  │  │
│  │  │   Telegram Frontend   │         │     Ingestion Pipeline    │  │  │
│  │  │   (aiogram 3.x FSM)   ├────────►│        (src/core/)        │  │  │
│  │  └───────────────────────┘         └─────────────┬─────────────┘  │  │
│  │                                                  │                │  │
│  │                      ┌───────────────────────────┼─────────────┐  │  │
│  │                      ▼                           ▼             ▼  │  │
│  │               ┌──────────────┐             ┌──────────┐ ┌──────┐  │  │
│  │               │ Fingerprint  │             │ Metadata │ │Lyrics│  │  │
│  │               │  (Chromaprint│             │ (MBrainz)│ │LRCLIB│  │  │
│  │               │   / fpcalc)  │             └──────────┘ └──────┘  │  │
│  │               └──────┬───────┘                                    │  │
│  │                      ▼                                            │  │
│  │             ┌─────────────────┐                                   │  │
│  │             │ SQLite (WAL) DB │                                   │  │
│  │             │ Deduplication & │                                   │  │
│  │             │    Job States   │                                   │  │
│  │             └─────────────────┘                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Detailed Component Breakdown

### 3.1 Network & Ingress: Local Telegram Bot API
Standard Telegram Bot API limits file downloads to 20MB. To accommodate high-resolution audio (FLAC, ALAC, DSD) and large archives (multi-disc 7z/ZIP files up to 2GB), the official `telegram-bot-api` binary is deployed in **`--local` mode**.

* **Operation:** When configured locally, files sent to the bot are written directly to local disk volumes rather than streamed through Telegram’s external HTTP proxies.
* **Path Resolution:** The bot receives absolute local file paths from the API server, eliminating network download overhead within the LXC (and mapping container volume paths to host paths when running in hybrid WSL Docker + local `uv` mode).
* **Limits:** Increases bot upload/download ceiling to **2000 MB (2 GB)**.

### 3.2 Ingestion Engine & Verification Pipeline

```
1. Receive Payload (Audio file, .zip, .7z)
   │
   ▼
2. Extract to Scratch Directory (/tmp/scratch/{job_id})
   │
   ▼
3. Audio Validation & Format Verification (mutagen)
   │
   ▼
4. Acoustic Fingerprinting (fpcalc / pyacoustid)
   │
   ▼
5. Deduplication Check (SQLite lookup by fingerprint & MBID)
   ├─► [Match Found] ──► Abort & Notify user with existing file path
   │
   ▼
6. Metadata Enrichment (MusicBrainz NGS)
   │
   ▼
7. Confidence Scoring
   ├─► [Confidence < 90% OR Ambiguity Detected]
   │         │
   │         └─► Transition Telegram FSM to AWAITING_CONFIRMATION
   │             Send inline options: [Match A] [Match B] [Manual Search] [Cancel]
   │
   ▼
8. Fetch Lyrics (LRCLIB REST API)
   ├─ Attempt Exact Match (Artist, Title, Album, Duration)
   └─ Save synced (.lrc) or embed synchronized/unsynchronized lyrics
   │
   ▼
9. Write Tags & Reorganize Target Storage
   └─ Rename & Move: /music/{Artist}/{Album} ({Year})/{Track} - {Title}.{ext}
   │
   ▼
10. Update SQLite Catalog & Cleanup Scratch Space
```

#### Detailed Pipeline Stages:
1. **Extraction:** Incoming archives are extracted via system utilities (`7z x`) offloaded to asynchronous background threads to avoid blocking the event loop.
2. **Fingerprinting & Deduplication:**
   * Compute a Chromaprint audio fingerprint using `fpcalc`.
   * Compare against existing database entries. If an exact fingerprint match exists, reject the file to prevent library bloat.
   * If metadata is missing or corrupted, query AcoustID web service with the fingerprint to identify the track canonical MBID.
3. **Metadata Matching:**
   * Query MusicBrainz for release data, disc number, track number, original release year, and genre.
   * Calculate a confidence score between local tags/filenames and MusicBrainz responses.
4. **Interactive Verification (User-in-the-Loop):**
   * If the match confidence is high ($\ge 90\%$), proceed automatically (configurable).
   * If ambiguous (e.g., standard vs. deluxe edition, live album, or low confidence), generate an `aiogram` InlineKeyboardMarkup displaying top candidates. The job halts until the user taps a selection or issues an override command.
5. **Lyrics Integration:**
   * Query LRCLIB (`https://lrclib.net/api/get`) using normalized metadata and track duration.
   * If found: write `syncedLyrics` (timestamped text) to sidecar `.lrc` files or directly into ID3/Vorbis tags (`USLT` / `LYRICS` tags) based on user preference.
6. **Filing & Placement:**
   * Use `pathvalidate` to sanitize artist/album strings against filesystem restrictions.
   * Move and structure the final files into target directory format:
     ```
     /music/
       └── {Album Artist}/
           └── {Album} ({Year})/
               ├── cover.jpg
               ├── 01 - {Title}.flac
               └── 01 - {Title}.lrc
     ```

---

## 4. Technical Stack

| Layer | Selected Tool | Justification |
|---|---|---|
| **Runtime & Language** | Python 3.11+ / 3.14 (`uv`) | Optimal ecosystem support for media tagging, Telegram bot libraries, and async I/O. |
| **Telegram Framework** | `aiogram` (v3.x) | Modern async framework with built-in FSM, native callback query routing, and support for custom local API server endpoints. |
| **Telegram API Server** | `telegram-bot-api` (Docker) | Official C++ server run in `--local` mode; bypasses the 20MB file limit up to 2GB. |
| **Audio Metadata** | `mutagen` | Handles ID3v2, Vorbis Comments (FLAC/Opus), and MP4 metadata without external C bindings. |
| **Audio Fingerprinting** | `pyacoustid` + `fpcalc` | Waveform-based perceptual hashing to detect duplicates and identify untagged tracks. |
| **Canonical Metadata** | `musicbrainzngs` | Official Python wrapper for the MusicBrainz XML/JSON Web Service (WS/2). |
| **Lyrics Provider** | `httpx` $\to$ LRCLIB API | Simple REST client fetching timestamped synced lyrics from lrclib.net. |
| **Archive Unpacking** | System `7z` binary via `asyncio` | High performance, low memory overhead compared to pure-Python decompression libraries. |
| **Database** | SQLite + `aiosqlite` | Embedded, zero-maintenance database running in WAL mode to track library fingerprints and state queues. |
| **Sanitization** | `pathvalidate` | Safe sanitization of cross-platform path strings. |

---

## 5. Proxmox LXC Resource Allocations & Hosting

### Recommended Container Configuration (Debian 12 LXC)
* **CPU:** 2 vCPUs (spikes during archive decompression and `fpcalc` waveform analysis; idle baseline is near 0%).
* **RAM:** **1024 MB (1 GB)**.
  * Idle: ~120 MB (Telegram Bot API container ~40MB, Python engine ~60MB, SQLite <10MB).
  * Peak: ~450–650 MB during heavy 7z extractions or multi-track fingerprint calculations.
* **Swap:** 512 MB.
* **Disk Storage:**
  * System/Rootfs: **10 GB - 20 GB** SSD storage (provides temp scratch space for decompressing multi-hundred megabyte archives before moving them to the destination volume).
  * Music Storage: Mounted directory (NFS, SMB, or Proxmox bind-mount from host ZFS pool directly to `/mnt/music`).

---

## 6. Directory Structure

```text
musicDownloader/
├── Dockerfile                   # Python 3.14 + uv + fpcalc + 7z + ffmpeg
├── docker-compose.yml           # Local telegram-bot-api + music-ingest-bot + test profile
├── .env.example                 # Environment variable template
├── AGENTS.md                    # Context, architecture rules & progress tracker for AI agents
├── main.py                      # Application entrypoint & dependency wiring
├── pyproject.toml               # uv dependencies, ruff, ty, and pytest configuration
├── src/
│   ├── config/                  # ENVIRONMENT & PATH SETTINGS
│   │   ├── __init__.py
│   │   └── settings.py          # AppSettings & .env loader
│   │
│   ├── bot/                     # TELEGRAM INTERFACE ONLY (aiogram 3.x)
│   │   ├── __init__.py
│   │   ├── api_server.py        # Local Bot API session & container-to-host path resolver
│   │   ├── states.py            # aiogram FSM states (IngestionStates)
│   │   ├── keyboards/           # InlineKeyboardMarkup builders
│   │   └── routers/
│   │       ├── commands.py      # /start, /help, /status
│   │       ├── files.py         # Audio & archive document handlers
│   │       └── verification.py  # Callback handlers for user confirmation
│   │
│   ├── core/                    # PURE BUSINESS LOGIC (Zero aiogram/db/config imports)
│   │   ├── __init__.py
│   │   ├── exceptions.py        # Domain exceptions
│   │   ├── models.py            # Pure domain models & job states
│   │   ├── ports.py             # Hexagonal Protocol interfaces
│   │   ├── extractor.py         # Archive extraction logic (7z, zip)
│   │   ├── fingerprint.py       # fpcalc execution & AcoustID interaction
│   │   ├── metadata.py          # MusicBrainz querying & confidence scoring
│   │   ├── lyrics.py            # LRCLIB API client
│   │   ├── organizer.py         # Tag writing & filesystem operations
│   │   ├── deduplicator.py      # Duplicate detection algorithms
│   │   └── pipeline.py          # 10-stage IngestionPipeline orchestrator
│   │
│   └── db/                      # PERSISTENCE ADAPTER (SQLite WAL)
│       ├── __init__.py
│       ├── database.py          # SQLite WAL engine & connection management
│       ├── models.py            # Job and catalog track DDL schemas
│       └── repository.py        # SqliteCatalogRepository implementing CatalogRepositoryPort
│
├── storage/
│   ├── scratch/                 # Ephemeral extraction directory
│   └── library/                 # Local development target music library
└── tests/
    ├── test_architecture.py     # AST boundary tests enforcing hexagonal isolation
    ├── unit/                    # Unit tests for config, core, pipeline, and bot
    └── integration/             # SQLite WAL repository & WSL Docker smoke tests
```

---

## 7. Future Extensibility Roadmap

1. **Userbot (MTProto) Integration:**
   * If automated channel monitoring or user-account media forwarding is required, integrate an MTProto listener (e.g., `Hydrogram` or `Telethon`) in a dedicated ingestion module. The listener forwards downloaded files directly to `app/core/`.
2. **Web Management Dashboard:**
   * Spin up a lightweight FastAPI server that mounts alongside the bot.
   * Expose endpoints (`/api/jobs/pending`, `/api/jobs/{id}/confirm`) that invoke the same `app/core/` functions, allowing users to verify albums in a rich browser interface.
3. **Downloader Integrations:**
   * Add modular scrapers or fetchers (e.g., `yt-dlp`, direct URL downloaders, or FlareSolverr-assisted scrapers) wrapped under an abstract `BaseDownloader` interface, passing results directly to the standard ingestion pipeline.