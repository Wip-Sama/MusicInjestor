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
- **Full Track Playback vs. Previews & Bot Deep-Linking (`src/core/radar.py`, `src/bot/`, `webapp/`):** Automatic reconciliation between discovered releases and ingested library tracks (`library_track_id`). Offers side-by-side full track playback and 30-second preview options in both Telegram Bot and Web Mini App. Seamless cross-platform deep linking supporting `/start song_<id>`, `/start track_<id>`, and `/start release_<id>` in Telegram chat and `startapp=song_` / `startapp=track_` Mini App deep navigation with automatic playback.
- **External Streaming Platform Links & Clipboard Copy (`src/core/platform_links.py`, `src/bot/`, `webapp/`):** Unified external streaming link generator for Deezer, Tidal, Qobuz, Spotify, and Apple Music with user-configurable priority order persisted in SQLite (`streaming_platform_order`). Configurable via space-separated numeric input (e.g. `2 3 1`) in Telegram `/settings` and drag-and-drop / arrow buttons in Web Mini App `SettingsModal`. Instant clipboard copying via native Telegram `CopyTextButton` inline keyboards and WebApp long-press (>= 450ms) gesture with Telegram haptic feedback.
- **Album Search, Library Gap Analysis & Missing Track Previews (`src/core/search.py`, `src/bot/routers/search.py`, `webapp/src/components/catalog/AlbumGapModal.tsx`):** Unified album search and gap calculation reconciling library-owned tracks against external release tracklists (Deezer). Provides coverage percentages, track possession indicators, and on-demand 30-second audio preview streaming for missing tracks directly in Telegram chat and Web Mini App modal. Plain text in chat is routed to search while URL links are recognized with informative ingestion notices; search scope (`global` vs `library`) is persisted per-user and configurable in Telegram `/settings` and WebApp `SettingsModal`. Supports Cloudflare Tunnel configuration in `AppSettings` with dynamic endpoint URL derivation and automated `cloudflared` Quick Tunnel lifecycle management (`src/bot/tunnel.py`) when `CLOUDFLARED_TUNNEL_URL` is left blank.
- **Dynamic Tunnel URL Cache-Busting, Mini App Song Delivery & Deep-Linking (`src/bot/routers/commands.py`, `src/bot/webapp_server.py`, `webapp/`):** Proactive client-side Telegram `MenuButtonWebApp` URL updating on `/start` to immediately bust stale client URL caches upon bot/tunnel restart. Bot deep-linking for `/start album_<id>` and `/start gap_<id>`. Mini App actions to request audio files and synced lyrics sidecars directly to the user's private Telegram chat (`POST /api/v1/app/tracks/{id}/send-to-chat`, `POST /api/v1/app/albums/{id}/send-to-chat`) with persistent SQLite `file_id` caching, alongside cross-platform deep links navigating between WebApp and Telegram Bot chat cards.
- **Radar Scan Concurrency & Live SSE Progress Bar, Artist Dossier & Telegram Radar Fix (`src/core/radar.py`, `src/bot/webapp_server.py`, `src/bot/routers/radar.py`, `webapp/`):** Multi-client synchronized release scanning with `asyncio.Lock` concurrency guard, HTTP 409 rejection of duplicate scans, live per-artist progress reporting (`RadarScanProgress`) broadcasted via SSE (`radar_scan_progress`), semantic `<progress>` UI card (`1/n artists done`, percentage, current artist name), and pagination ("Load More Drops"). Full Artist Dossier modal (`ArtistModal.tsx`, `GET /api/v1/app/artists/{artist_name}`) accessible by clicking artist names across all cards and players (showing owned library songs vs upcoming radar drops, follow status, playback, and streaming links). Fixed Telegram Bot Radar navigation callbacks (`radar~page~~1`).

- **Single Queue Item Link Guidance, Real-Time Radar Scan Tracking, Concurrency Deduplication Fix, WebApp Volume Control, & Admin Debug Menu (`webapp/`, `src/core/`, `src/bot/`):** Single queue items can be guided with specific MusicBrainz/streaming links in WebApp and Telegram Bot (`/queue`). Real-time radar scanning tracking with start-of-scan artist progress, Deezer HTTP connection pooling, cache-busting, and SSE automatic reconnection. Concurrently executed forwarded audio deduplication guard preventing dual execution. Cleaned UI duplicate inputs, removed user-facing test buttons into `/admin -> 🧪 Debug & Tests`, added direct Admin Log Channel button, and added volume slider in WebApp player.
- **Audio Previews CDN Fix, Album Ownership Isolation & Bot Parity (`webapp/`, `src/bot/`, `src/db/repository.py`):** External Deezer preview URLs are passed untouched to HTML5 audio players avoiding CDN 403 Forbidden errors. Release-level `library_track_id` is restricted strictly to single-track releases (`track_count == 1`), ensuring multi-track albums present individual owned full tracks vs 30s previews without false playback attempts. Enforced full bot feature parity: added Telegram chat artist management (`/follow`, `/unfollow`, followed artists list, and interactive in-chat Artist Dossier), `/search <query>` with 30s preview audio delivery in chat, interactive release drop notification testing (`/radar_test`, Mini App `POST /api/v1/app/radar/test-notification`), release drop alert user preferences toggle (`notifications_enabled`) across bot `/settings` and Web Mini App `SettingsModal`, and dedicated admin log channel button in `/admin` dashboard.
- **Streaming Platform Links in Song Search Previews & Quality-Aware Song Upgrades (`src/core/quality.py`, `src/core/pipeline.py`, `src/core/deduplicator.py`, `src/db/repository.py`, `src/bot/`, `webapp/`):** External streaming platform links (Deezer, Spotify, Tidal, Apple Music, Qobuz) added to 30-second audio preview deliveries in Telegram song search and Web Mini App `AlbumGapModal` (with tap-to-open, native Telegram `CopyTextButton`, and long-press copying). Quality inspection and comparison engine evaluating lossless (FLAC, WAV, ALAC, AIFF) vs lossy, bit depth, sample rate, and bitrate differentials. When an uploaded song is a duplicate of a library track, quality is automatically evaluated: lower/equal quality files are rejected with quality summaries, while higher quality files trigger an upgrade flow (`is_upgrade=True`) prompting confirmation or auto-replacing the existing track and unlinking obsolete files when `auto_upgrade_quality` user preference is enabled.

### 5.2 Active & Planned Backlog
- [ ] **Low-Priority Backlog: Admin Folder Structure Viewer & Mass Migration**
  - Allow upper roles (`ADMIN` / `OWNER`) to browse the `storage/library` folder hierarchy in Telegram and run dry-run previewed mass path/tag migrations across the catalog.



