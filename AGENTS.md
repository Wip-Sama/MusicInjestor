# AI Agent Context & Living Architecture Guide (`AGENTS.md`)

> **Instruction for All Future Agents:**
> Read this file before modifying any code. Whenever you complete a feature, alter architectural boundaries, or change requirements, **update Section 5 (Project Progress & Backlog)** of this document so subsequent agents inherit accurate context.

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
│   └── bot/                     # TELEGRAM INTERFACE ADAPTER (aiogram 3.x)
│       ├── api_server.py        # Local Bot API session builder & container->host path translator
│       ├── notifier.py          # TelegramAdminLogNotifier implementing AdminLogNotifierPort
│       ├── oidc_server.py       # OAuth2 `/api/v1/auth/callback` & Authentik webhook coordinator
│       ├── states.py            # FSM states (IngestionStates)
│       ├── middlewares/         # RbacMiddleware (role enforcement, 15m OIDC sync, guest upload discard)
│       ├── keyboards/           # InlineKeyboardMarkup builders for verification, onboarding & /admin
│       └── routers/             # /start, /help, /status, /admin, file ingress, and callback routers
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

### 3.3 Full Containerized Execution & Containerized Test Suite (with Linux `fpcalc` & `7z`)
- **Run full stack in Docker:**
  ```bash
  docker compose up -d --build
  ```
- **Run `pytest` inside the Docker image (where `fpcalc`, `7z`, and `ffmpeg` are pre-installed):**
  ```bash
  docker compose --profile test run --rm test
  ```

---

## 4. Quality Gates & Coding Standards for Agents
Before concluding any task that modifies code:
1. Run `uv run ruff check --fix` and `uv run ruff format`
2. Run `uv run ty check` (must exit `0` with zero warnings)
3. Run `uv run pytest` (must exit `0` with all tests passing)

---

## 5. Project Progress & Backlog

### Completed Milestones
- [x] **M1: Modular Monolith `src/` Layout & Hexagonal Boundaries**
  - Created isolated `src/core`, `src/db`, `src/bot`, and `src/config` packages.
  - Defined `Protocol` ports in `src/core/ports.py` for future microservice extraction.
  - Added AST boundary enforcement in `tests/test_architecture.py`.
- [x] **M2: WSL Docker & Local Telegram Bot API Configuration**
  - Added `Dockerfile` with Python 3.14, `uv`, `libchromaprint-tools` (`fpcalc`), `7zip` (`7z`), and `ffmpeg`.
  - Added `docker-compose.yml` supporting hybrid (`telegram-bot-api` only), full stack, and `test` profile modes.
  - Implemented `resolve_local_api_file_path` for seamless container-to-host file access in `--local` mode.
- [x] **M3: Core Ingestion Pipeline Skeleton & SQLite WAL Persistence**
  - Implemented `IngestionPipeline` stages (staging/extraction, fingerprinting, deduplication, confidence check, confirmation pause/resume, lyrics, filing).
  - Implemented `SqliteDatabaseManager` (WAL mode) and `SqliteCatalogRepository`.
  - Established unit and integration test suites with coverage reporting.
- [x] **M3.5: Governance, 5-Tier RBAC, Authentik OIDC Synchronization, Ingestion Queue & `/admin` Dashboard (`admin_rbac_specification.md`)**
  - Extended SQLite WAL schema with `users`, `access_requests`, `ingestion_jobs`, `library_tracks`, `audit_logs`, and indexes.
  - Implemented 5-tier RBAC (`GUEST`, `UPLOADER`, `MODERATOR`, `ADMIN`, `OWNER`), `GovernanceService`, and `RbacMiddleware` (discarding unauthorized guest uploads and refreshing stale OIDC profiles >15m).
  - Implemented `AuthentikOidcService` (HMAC-SHA256 state nonces with 10m TTL, encrypted tokens at rest, group-to-role evaluation, demotion queue revocation, periodic sync, and webhook handler).
  - Implemented `IngestionQueueWorker` with FIFO persistence and runtime-configurable concurrency (`1..4`).
  - Implemented `TelegramAdminLogNotifier` (Events A, B, C, D and interactive access request cards) and `/admin` Control Dashboard.

- [x] **M4: Deep `mutagen` Tag Inspection & Writing**
  - Implemented embedded tag inspection (`mutagen.File(..., easy=True)`, including `isrc`) and smart `[disc-]track - Artist - Title` filename stem parsing in `MusicBrainzMetadataService.read_local_metadata`.
  - Implemented enriched tag writing (Artist, Album Artist, Album, Year, Track/Disc numbers, Genre, MBIDs, and embedded `LYRICS`/`\xa9lyr` tags) alongside `.lrc` sidecars in `LibraryOrganizer.finalize_track`.
- [x] **M5: Live AcoustID & MusicBrainz (`musicbrainzngs`) Query Implementation**
  - Added AcoustID v2 REST lookup (`_lookup_acoustid_mbid`) in `ChromaprintFingerprinter` when `acoustid_api_key` is configured, plus SHA-256 fingerprint fallback in `IngestionPipeline` when `fpcalc` is unavailable on host.
  - Implemented `musicbrainzngs` recording/release lookups with ISRC queries, `Character (CV: Voice Actor)` decomposition (`extract_artist_search_variants`), English/Romaji alias reconstruction (`_extract_english_artist_credit`), artist alias -> `arid` resolution, Lucene escaping, and full `release-list` scoring (demoting unrelated compilations and rewarding year/track matches).
- [x] **M6: Multi-Track Archive Batch Processing & End-to-End Telegram FSM Wiring**
  - Wired `files_router` (`handle_incoming_media`) to fetch files via `bot.get_file(file_id)`, map `--local` container paths via `resolve_local_api_file_path` (including WSL/Windows `\uf03a` colon directory translation), enqueue and execute jobs via `IngestionQueueWorker`, and prompt interactive confirmation (`IngestionStates.awaiting_confirmation`) with leading confidence labels (`1. [98%] ...`), full candidate details, and clickable MusicBrainz links.
  - Wired `verification_router` (`handle_verification_callback` & `handle_manual_search_query`) for release selection (`verify:select`), manual MusicBrainz re-query (`verify:manual` supporting MusicBrainz URLs, raw MBIDs, ISRCs, key-value overrides, `Artist - Title | Album`, or single voice actor/artist overrides), and cancellation (`verify:cancel`).



