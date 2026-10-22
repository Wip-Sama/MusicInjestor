# AI Agent Context & Living Architecture Guide (`AGENTS.md`)

> **Instruction for All Future Agents:**
> Read this file before modifying any code. It serves as the **Living Architecture & Developer Guide** explaining package boundaries, invariants, testing workflows, and current capabilities.
> Whenever you complete a feature, milestone, bug fix, or architectural change:
> 1. Record the detailed, chronological changelog entry in `CHANGELOG.md`.
> 2. Update Section 5 (**System Capabilities & Living Roadmap**) in this document so subsequent agents inherit accurate architectural context.
> **Always keep both files updated.**

---

## 1. Project Overview
This repository implements a **self-hosted Automated Music Ingestion System** controlled via a **Telegram Bot** (`aiogram` 3.x) backed by the official C++ `telegram-bot-api` running in **`--local` mode** (supporting audio files and `.zip`/`.7z` archives up to **2 GB**).

The system is architected as a **Hexagonal Modular Monolith** inside `src/` so that `src/core/` can be extracted into a standalone package or microservice (or driven by a FastAPI Web UI / MTProto Userbot) with zero rewrites.

---

## 2. Package Structure & Strict Dependency Rules

```text
musicDownloader/
├── main.py                      # Composition root wiring config, SQLite, RBAC, OIDC, queue, and bot
├── Dockerfile                   # Python 3.14 + uv + fpcalc (Chromaprint) + 7z + ffmpeg
├── docker-compose.yml           # WSL/Production stack: telegram-bot-api + music-ingest-bot + test
├── src/
│   ├── config/                  # Typed environment & .env settings (AppSettings)
│   ├── core/                    # PURE DOMAIN & INGESTION ENGINE (Hexagonal Core)
│   │   ├── audit.py             # Admin Log Channel event & queue/dashboard card formatters
│   │   ├── exceptions.py        # Domain-specific exceptions (IngestionError, PermissionDeniedError, etc.)
│   │   ├── models.py            # Pure dataclasses/enums (UserAccount, UserRole, Permission, IngestionJob, etc.)
│   │   ├── ports.py             # Runtime-checkable Protocols (CatalogRepositoryPort, GovernanceRepositoryPort, etc.)
│   │   ├── rbac.py              # 5-tier RBAC permission matrix & GovernanceService
│   │   ├── oidc.py              # Authentik OIDC state nonces, token encryption & claim synchronization
│   │   ├── queue.py             # Serialized & runtime-configurable IngestionQueueWorker (1..4)
│   │   ├── extractor.py         # 7z archive unpacking & single-file scratch staging
│   │   ├── fingerprint.py       # Chromaprint (`fpcalc -json`) execution
│   │   ├── deduplicator.py      # Fingerprint & MusicBrainz recording ID deduplication
│   │   ├── metadata.py          # Local tag reader & MusicBrainz confidence scoring
│   │   ├── lyrics.py            # LRCLIB synced/plain lyrics REST client
│   │   ├── organizer.py         # Sanitized path builder & `.lrc` sidecar writer
│   │   └── pipeline.py          # 10-stage IngestionPipeline orchestrator
│   ├── db/                      # PERSISTENCE ADAPTER (SQLite WAL mode via aiosqlite)
│   │   ├── database.py          # WAL connection & DDL manager
│   │   ├── models.py            # SQLite table & index DDL statements (users, access_requests, jobs, tracks, audit_logs)
│   │   └── repository.py        # SqliteCatalogRepository implementing CatalogRepositoryPort & GovernanceRepositoryPort
│    └── bot/                     # TELEGRAM INTERFACE ADAPTER (aiogram 3.x)
        ├── api_server.py        # Local Bot API session builder & container->host path translator
        ├── notifier.py          # TelegramAdminLogNotifier implementing AdminLogNotifierPort
        ├── oidc_server.py       # OAuth2 `/api/v1/auth/callback` & Authentik webhook coordinator
        ├── states.py            # FSM states (IngestionStates, SongManagementStates)
        ├── ui.py                # Shared 3-tier HTML screen formatter, safe in-place editor & callback lifecycle helpers
        ├── middlewares/         # RbacMiddleware (role enforcement, 15m OIDC sync, guest upload discard)
        ├── keyboards/           # InlineKeyboardBuilder, CallbackData & CopyTextButton builders
        └── routers/             # /start, /help, /status, /admin, /songs, file ingress, and callback routers
└── tests/
    ├── test_architecture.py     # AST enforcement of hexagonal boundaries & explicit imports
    ├── unit/                    # Fast isolated unit tests (settings, models, pipeline, RBAC, OIDC, queue, bot)
    └── integration/             # SQLite WAL persistence & optional WSL Docker smoke tests
```

### Enforced Architectural Invariants (Tested by `tests/test_architecture.py`)
1. **`src/core/` Isolation:** `src/core/` MUST NEVER import `aiogram`, `src.bot`, `src.db`, or `src.config`. All external interactions pass through `src/core/ports.py` protocols.
2. **`src/db/` Isolation:** `src/db/` implements `CatalogRepositoryPort` and `GovernanceRepositoryPort` from `src/core/ports.py` and MUST NEVER import `aiogram` or `src.bot`.
3. **Explicit Top-Level Imports Only:** Never use wildcard imports (`from ... import *`) or nested/inline imports inside functions or classes.

---

## 3. Local `uv` & WSL Docker Testing Workflows

### 3.1 Fast Local Unit & Integration Tests (Host / WSL via `uv`)
Runs all architectural, unit, and SQLite WAL integration tests locally without requiring Docker or external network access:
```bash
uv run pytest
```

### 3.2 Hybrid Development Mode (`telegram-bot-api` in WSL Docker + Bot on Host via `uv`)
1. Copy `.env.example` to `.env` and set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_BOT_TOKEN`.
2. Start only the local Telegram Bot API server in WSL Docker:
   ```bash
   docker compose up -d telegram-bot-api
   ```
3. Run the Python bot locally with `uv`:
   ```bash
   uv run python main.py
   ```
   *Note on Path Resolution:* `telegram-bot-api` inside Docker writes files to `/var/lib/telegram-bot-api` (bind-mounted to `./storage/telegram-bot-api`). `resolve_local_api_file_path()` in `src/bot/api_server.py` automatically translates container paths to `./storage/telegram-bot-api` on the host.
4. Run the live WSL Docker smoke test:
   ```bash
   RUN_WSL_DOCKER_TESTS=1 uv run pytest -m docker
   ```

### 3.3 Full Containerized Execution, GHCR Pull & Local `act` CI Testing
- **Pull pre-built image from GHCR (set `MUSIC_BOT_IMAGE=ghcr.io/wip-sama/musicinjestor:latest` in `.env`):**
  ```bash
  docker compose pull && docker compose up -d
  ```
- **Or build and run full stack locally in Docker:**
  ```bash
  docker compose up -d --build
  ```
- **Run `pytest` inside the Docker image (where `fpcalc`, `7z`, and `ffmpeg` are pre-installed):**
  ```bash
  docker compose --profile test run --rm test
  ```
- **Run GitHub Actions CI workflow locally via `act` (`catthehacker/ubuntu:act-latest` configured in `.actrc`):**
  ```bash
  act -j quality-and-test
  ```

---

## 4. Quality Gates & Coding Standards for Agents
Before concluding any task that modifies code:
1. Run `uv run ruff check --fix` and `uv run ruff format`
2. Run `uv run ty check` (must exit `0` with zero warnings)
3. Run `uv run pytest` (must exit `0` with all tests passing)

---

## 5. System Capabilities & Living Roadmap

> **Changelog Notice:**
> The complete historical log of all completed milestones (M1 through M29+) has been moved to `CHANGELOG.md`.
> **All agents must record new completed milestones, bugfixes, and feature additions in `CHANGELOG.md`.**

### 5.1 Current System Capabilities
- **Core Ingestion Pipeline (`src/core/pipeline.py`):** 10-stage automated pipeline supporting single audio files and multi-track `.zip`/`.7z` archives up to 2 GB via local Bot API.
- **Tag Inspection & Normalization (`src/core/metadata.py`):** Deep mutagen tag extraction, character/voice-actor (`CV`) normalization to primary real singers, and multi-provider enrichment (MusicBrainz, Genius, Last.fm, AcoustID).
- **Lyrics Engine (`src/core/lyrics.py`, `src/core/organizer.py`):** Automatic retrieval and strong prioritization of synchronized `.lrc` lyrics from LRCLIB with cross-script fallback, robust multi-tag in-file embedding (`USLT` timestamped LRC, `SYLT`, `TXXX:SYNCEDLYRICS`, Vorbis `SYNCEDLYRICS`/`LYRICS`, MP4 `----:com.apple.iTunes:SYNCEDLYRICS`), stale normal lyrics frame purging, non-downgrading retention, and `.lrc` sidecar file writing.
- **In-RAM Transcoding & Caching (`src/core/transcoder.py`):** Real-time `ffmpeg` pipes for on-the-fly Opus/MP3 transcoding and in-RAM LRU caching for low-bandwidth mobile streaming.
- **Polite External API Throttling (`src/core/throttler.py`):** Rate-limited API access with token buckets and exponential backoff.
- **5-Tier RBAC & Governance (`src/core/rbac.py`):** `GUEST`, `LISTENER`, `UPLOADER`, `MODERATOR`, `ADMIN`, `OWNER` with configurable Authentik OIDC group-to-role mapping (`OIDC_ROLE_MAPPING`), local role override locks preventing background claim stomping, self-service/admin OIDC disconnection, token encryption, and administrative audit logging.
- **Queue Worker (`src/core/queue.py`):** Async FIFO queue worker with runtime concurrency (1..4), auto-polling loop, and debounced multi-file forward ingestion.
- **Interactive Telegram UI (`src/bot/`):** Single living-screen in-place UI via `aiogram` 3.x, paginated `/songs` library browser, full dry-run tag/cover/lyrics editor, `/settings` user preferences with SSO disconnect button, interactive bulk research review, live `/queue` overview with active bulk re-search progress and interactive queue resolution, and pre-commit external MusicBrainz link search & metadata overrides before final library ingestion.
- **Telegram Mini App (`webapp/`):** React 18 + Vite SPA with cryptographic `initData` validation, audio player with lock-screen media session, dynamic transcode quality switcher, tap-and-hold gesture multi-track selection with Telegram haptic feedback, full-featured Bulk Re-Search Studio with review carousel, karaoke synced lyrics, Studio editor, live SSE queue board with real-time bulk re-search progress card, accessible pre-commit tag editor modal (`JobEditModal`) with MusicBrainz URL/MBID lookup, user settings modal, and user governance.
- **Persistent User Preferences (`src/core/models.py`, `src/db/`):** Cross-platform streaming format (`auto`, `original`, `opus`, `mp3`), bitrate, and auto-lyrics search settings synced across Telegram Bot and Web Mini App.
- **Release Radar & Multi-Track Album Previews (`src/core/radar.py`, `src/core/deezer.py`, `src/bot/routers/radar.py`, `webapp/src/views/RadarView.tsx`):** 12-hour background discography polling via Deezer (zero API key), single-track and multi-track album previews (`ReleaseTrackPreview`) streaming 30-second MP3 previews in Telegram chat and Web Mini App, opt-in artist notifications, followed artists management, and automatic in-library status reconciliation.
- **External Streaming Platform Links & Clipboard Copy (`src/core/platform_links.py`, `src/bot/`, `webapp/`):** Unified external streaming link generator for Deezer, Tidal, Qobuz, Spotify, and Apple Music with user-configurable priority order persisted in SQLite (`streaming_platform_order`). Configurable via space-separated numeric input (e.g. `2 3 1`) in Telegram `/settings` and drag-and-drop / arrow buttons in Web Mini App `SettingsModal`. Instant clipboard copying via native Telegram `CopyTextButton` inline keyboards and WebApp long-press (>= 450ms) gesture with Telegram haptic feedback.

### 5.2 Active & Planned Backlog
- [ ] **Low-Priority Backlog: Admin Folder Structure Viewer & Mass Migration**
  - Allow upper roles (`ADMIN` / `OWNER`) to browse the `storage/library` folder hierarchy in Telegram and run dry-run previewed mass path/tag migrations across the catalog.



