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
- [x] **M7: Synced Lyrics Embedding, `cover.jpg` Extraction/CAA Fetching, Release `medium-list` Mapping, Live In-Place Progress, OIDC HTTP/Cron & RBAC Workflows**
  - Implemented direct synchronized lyrics embedding into audio files (`SYNCEDLYRICS`/`UNSYNCEDLYRICS`/`LYRICS` on FLAC/Ogg/Opus, `SYLT` parsed into millisecond tuples + `USLT`/`TXXX` on MP3/ID3, and `\xa9lyr` on MP4/M4A) alongside `.lrc` sidecars.
  - Implemented `cover.jpg` staging in `SevenZipArchiveExtractor` and extraction/fetching in `LibraryOrganizer.finalize_track` (staged cover image -> embedded `mutagen` `Picture`/`APIC`/`covr`/`metadata_block_picture` -> MusicBrainz Cover Art Archive `coverartarchive.org/release/{mbid}/front`).
  - Implemented `MusicBrainzMetadataService.fetch_release_tracks` and `IngestionPipeline._complete_ingestion` `medium-list` tracklist mapping so multi-track archives populate per-track canonical titles, artist credits, track/disc numbers, and `recording_mbid`s.
  - Wired in-place live progress bar updates (`render_job_stage_progress`) in `handle_incoming_media` and `handle_verification_callback`, transitioning the initial queued message through pipeline stages into the confirmation or completion card.
  - Added `OidcHttpServer` (`GET /api/v1/auth/callback` and `POST /api/v1/webhooks/authentik` via `aiohttp.web`) and `run_hourly_oidc_sync_loop` background task in `src/bot/oidc_server.py` and wired both into `main.py`.
  - Wired `REQUEST_TAG_CORRECTION` (`/fixtags`), `APPROVE_TAG_CORRECTIONS` (`admin:tags`, `admin:tag_approve`, `admin:tag_reject` rewriting audio tags via `LibraryOrganizer.apply_tag_correction`), `RESOLVE_OTHERS_JOBS` (`admin:resolve_job` and `/queue` paused-job picker for moderators), and `SYSTEM_DESTRUCTIVE_WIPE` (`/wipe CONFIRM` and `admin:wipe_confirm` for `OWNER`).
- [x] **M8 (Phase 1): Primary Singer & Voice-Actor (`CV`) Normalization, Multi-Provider English Enrichment (MusicBrainz + Genius + Last.fm), High-Confidence Auto-Approval, and Always-On `.lrc` + Embedded Lyrics**
  - Implemented `normalize_primary_artist` and `extract_bilingual_english` in `src/core/metadata.py` to strip fictional character prefixes (`Yotsugi Ononoki(Saori Hayami)` -> `Saori Hayami`, `Ononoki Yotsugi (CV: Hayami Saori)` -> `Hayami Saori`), discard MusicBrainz `type == "Character"` credits in favor of the real singer, extract the single primary singer from collaborations (`Rkomi con Elodie` -> `Rkomi`), and avoid overwriting ASCII stage names with legal-name aliases.
  - Added Genius (`GENIUS_ACCESS_TOKEN` / public search fallback) and Last.fm (`LASTFM_API_KEY`) metadata enrichment in `MusicBrainzMetadataService` plus cross-release Latin `medium-list` title resolution to translate non-ASCII/CJK titles and albums into English/Romaji (`オレンジミント` -> `Orange Mint`).
  - Deduplicated multi-pressing MusicBrainz candidates by normalized `(artist, title, album, year)` and updated `IngestionPipeline._requires_confirmation` so `>= 98%` confidence matches auto-approve immediately without halting for manual confirmation.
  - Enhanced `LrclibLyricsClient` (`src/core/lyrics.py`) with `/api/search` fallback and cross-script artist+duration matching, and updated `LibraryOrganizer` (`src/core/organizer.py`) to always embed lyrics in audio tags (`LYRICS`, `SYNCEDLYRICS`, `UNSYNCEDLYRICS`, `SYLT`, `USLT`, `\xa9lyr`) and always write a `.lrc` sidecar file alongside the audio track.
  - Documented API key registration URLs for all external services in `.env.example`.

- [x] **M9 (Phase 2A): Interactive `/songs` Telegram Menu, Guided Tag/Cover/Lyrics Editor & Mandatory Dry-Run Confirmation**
  - Added interactive `/songs` menu and `🎵 Songs` bottom menu button (`src/bot/routers/songs.py`, `src/bot/keyboards/songs.py`, `SongManagementStates` in `src/bot/states.py`) replacing raw text editing commands with inline keyboards and guided FSM prompts:
    - Live search across library tracks by title, artist, or album -> tap result -> open rich Song Action Card (`📥 Get Song File`, `💿 Get Full Album`, `✏️ Edit Name / Metadata`, `🖼️ Edit Cover`, `📝 Edit Lyrics`, `🔎 Auto Re-Search`, `📤 Export Data`, `📥 Import Data`).
    - Enforced the **Mandatory Dry-Run & Confirmation Policy** (`SongEditProposal`): every metadata edit, file move/rename, cover art replacement (photo upload, image document, or `https://` URL), lyrics edit (text or `.lrc`/`.txt` upload), auto re-search (MusicBrainz + Genius + Last.fm + LRCLIB), or JSON import renders a `Before -> After` & `Old Path -> New Path` preview card requiring explicit `✅ Confirm & Apply Changes` confirmation before touching disk or SQLite.
    - Implemented `LibraryOrganizer.relocate_and_retag_track`, `update_track_cover_art`, `update_track_lyrics`, `read_track_lyrics`, and `export_song_bundle` in `src/core/organizer.py`.
- [x] **M10 (Phase 2B): Library Folder Rescan & Database Reconciliation**
  - Implemented `LibraryOrganizer.scan_library_filesystem`, `apply_rescan_report`, and `reconcile_library_with_repository` (`LibraryRescanReport` in `src/core/models.py`) plus `SqliteCatalogRepository.upsert_scanned_library_track` and `delete_library_track` to synchronize `storage/library` filesystem changes (newly added, modified/retagged, moved, or deleted audio tracks, `.lrc` sidecars, and `cover.jpg` files) with SQLite.
  - Wired automatic library reconciliation on bot startup and periodic background cron (`run_periodic_library_rescan_loop` in `main.py`), plus interactive `/rescan` (`🔄 Rescan Library Folder`) with Dry-Run Preview Report and confirmation for `MODERATOR`+ roles.
  - Added `<artist>/<album>/<filename>` tail-key matching (`_extract_tail_library_key`) during filesystem rescan so migrating `library.db` between a host path and a Docker container mount (`/mnt/music/library`) automatically updates `storage_path` without losing track IDs or Chromaprint fingerprints.
  - Added layered YAML configuration loading (`config.yaml` / `CONFIG_FILE` < `.env` < environment variables) and `UMASK` / `PUID` / `PGID` support in `src/config/settings.py`, plus `config.example.yaml` and `docker-compose.example.yaml` for external bind-mounted music folders (`HOST_MUSIC_DIR`).
- [x] **M11: GitHub Actions CI/CD, GHCR Container Registry Publishing & Local `act` Runner Support**
  - Added `.github/workflows/ci.yml` with `quality-and-test` (`ruff check`, `ruff format --check`, `ty check`, `pytest`) and `docker-build-publish` (publishing tagged images to `ghcr.io/wip-sama/musicinjestor` on branch/tag pushes with automatic lowercasing and running local Docker build+test verification when invoked via `act`).
  - Added `.actrc` targeting `catthehacker/ubuntu:act-latest` and updated `docker-compose.yml`, `docker-compose.example.yaml`, and `.env.example` with `MUSIC_BOT_IMAGE=ghcr.io/wip-sama/musicinjestor:latest` so deployments can either pull pre-built GHCR images or build locally.
- [x] **M12: Telegram UI/UX Architecture Modernization (Single Living Screen, 3-Tier HTML Typography & `InlineKeyboardBuilder` Ergonomics)**
  - Added `src/bot/ui.py` (`format_screen`, `format_loading_card`, `ack_callback`, `safe_edit_text`, `safe_answer`, `strip_copy_text_buttons`, `send_chat_action_safe`) and refactored all routers (`commands`, `files`, `verification`, `admin`, `songs`) and `src/core/audit.py`.
  - Eliminated vertical chat spam in callback handlers (`verify:manual`, `admin:add_user`, `queue:mine`, `songs:search_prompt`, `menu:*`) by mutating messages in-place via `safe_edit_text` (with `TelegramBadRequest: message is not modified` suppression, `edit_caption` fallback, `InaccessibleMessage` support via `bot.edit_message_text`, and automatic `CopyTextButton` fallback for older local Bot API builds).
  - Handled real `aiogram` 3.x synchronous `TelegramMethod`-returning methods (`Message.edit_text`, `CallbackQuery.answer`, `Bot.send_chat_action`) via `callable(...)` + `inspect.isawaitable(...)` instead of `inspect.iscoroutinefunction(...)`, backed by end-to-end `aiogram.Dispatcher` behavioral tests (`test_real_aiogram_dispatcher_living_screen_navigation_and_fallbacks`).
  - Enforced the 3-tier Telegram HTML hierarchy (`<b>Header</b>`, native `<blockquote>` / `<blockquote expandable>` context blocks, and `<code>` / `<i>` breadcrumb/timestamp footers) and removed legacy CLI command lists from chat text.
  - Converted all keyboard builders to `InlineKeyboardBuilder` / `ReplyKeyboardBuilder` with typed `CallbackData` schemas, native `CopyTextButton` (`copy_text=CopyTextButton(...)`) for IDs/tokens, consistent `✅ [Active]` / `⬜ [Disabled]` toggle states, and 2-tier escape navigation (`◀ Back` on Level 1; `◀ Back` + `🏠 Menu` on Level >= 2, including filtered search results returning to `songs:list`).
  - Added immediate callback acknowledgment (`ack_callback`) and >500ms latency feedback (`format_loading_card` + `send_chat_action_safe`) before long-running ingestion, rescan, OIDC sync, and metadata/lyrics lookup operations.
- [x] **M13: Ordered Song Menu, Provenance & Edit History, Chronological User Activity, Interactive Job Pause/Unlock/Review & Audio Forwarding**
  - Reordered `build_song_action_keyboard` into dedicated Retrieval/History rows (`📥 Get Song File`, `💿 Get Full Album`, `📤 Export Data`, `📜 Song History`) above Edit/Mutation rows (`✏️ Edit Name / Metadata`, `🖼️ Edit Cover`, `📝 Edit Lyrics`, `🔎 Auto Re-Search`, `📥 Import Data`, `📋 Copy Track ID`).
  - Added full song provenance & modification tracking (`updated_at`, `modified_by`, `modification_count`, `telegram_file_id`, `source_chat_id`, `source_message_id` with SQLite auto-migration and `uploaded_by` backfill from `ingestion_jobs`), displaying who added each song, when, how many times it was modified, when last modified, and by whom, plus an interactive `📜 Song History` screen (`songs:history:<track_id>`).
  - Extended User Management (`admin:user:<id>`) with chronological `📤 User Uploads` (`admin:user_uploads:<id>`) and `📝 User Changes & History` (`admin:user_changes:<id>`) views and accurate upload/change counts.
  - Added `JobState.PAUSED` and interactive queued-job controls (`⏸️ Pause / Hold`, `▶️ Unlock & Resume`, `🔎 Manual Review`, `❌ Cancel Job`) in `IngestionQueueWorker` and `admin_router`, plus actionable Job ID / Track ID lookup via `/job <id>` or pasting an ID directly in chat.
  - Implemented smart song delivery (`_deliver_track_audio`) supporting message forwarding (`bot.forward_message`), cached `telegram_file_id` reuse, and disk `FSInputFile` fallback with automatic cache invalidation on retag/cover/lyrics edits.
- [x] **M14: Interactive Help Topics (`help:<topic>`), Structured Queue Item Layout & File Retrieval (`queue:get_file`), Full-Pipeline Queued Job Tag Editor (`verify:edit`), and Per-Message Context-Aware `◀ Back` Navigation**
  - Replaced direct module links in `ℹ️ Help` (`/help` / `menu:help`) with dedicated in-place Help topic screens (`help:ingestion`, `help:songs`, `help:queue`, `help:sso`, `help:status`, `help:admin`) explaining how each workflow operates, with optional `🚀 Open ...` launchers and `◀ Back` returning to Help.
  - Structured the Queue Overview (`render_queue_overview_screen` + `build_paused_jobs_keyboard`) with 1 row per queue item (`🔍 Resolve`, `⏸️ Held`, `⏳ #1`, `⚙️ Active`) and organized singular Queue Item controls (`build_queued_job_keyboard`) into logical rows: Row 1 (`📥 Get Queued File` | `🔎 Manual Review & Tag`), Row 2 (`⏸️ Pause / Hold` or `▶️ Unlock & Resume` | `❌ Cancel Job`), Row 3 (`📋 Copy Job ID`), Row 4 (`◀ Back` | `🏠 Menu`).
  - Added `queue:get_file:<job_id>` (`_deliver_queued_job_file`) so users and moderators can download/listen to the queued audio or archive before or during manual review.
  - Added `IngestionPipeline.prepare_for_review` and `IngestionPipeline.complete_custom_ingestion` plus the full-pipeline Manual Tag & Dry-Run Editor for queued jobs (`verify:edit:<job_id>`, `verify:field:<job_id>:<field>`, `verify:cover:<job_id>`, `verify:lyrics:<job_id>`, `verify:auto_enrich:<job_id>`, `verify:apply_edit:<job_id>`).
  - Implemented per-message navigation stack tracking (`resolve_back_callback` and `reset_fsm_state` in `src/bot/ui.py`) so `◀ Back` dynamically returns to the actual parent screen when a view is reachable from multiple entry points (e.g., `songs:view` from `admin:user_uploads` vs `songs:list`; `queue:view` from `admin:queue` vs `queue:mine`; modules opened from `help:<topic>` vs `menu:home`).

- [x] **M15: Non-Blocking Remote Startup, Resilient Directory Scanning, Chunked Streaming Hashing & Batch SQLite Reconciliation**
  - Eliminated synchronous startup blocking in `main.py` where initial library reconciliation ran before bot initialization; the bot now starts polling and listening to Telegram commands immediately (<1 second), while library reconciliation runs safely in a concurrent background task.
  - Replaced unconstrained `Path.rglob("*")` traversal in `src/core/organizer.py` with `os.walk(..., followlinks=False)` with directory `OSError` resilience, preventing infinite recursion on circular symlinks and blocking on special files.
  - Replaced full-file `audio_path.read_bytes()` with streaming 64KB chunked hashing (`_compute_file_sha256_fingerprint_sync`) and error fallbacks, eliminating container OOM (Out Of Memory) kills on large remote library mounts.
  - Optimized file matching by evaluating mount-point migration tail-keys (`_extract_tail_library_key`) first and only computing hashes when unresolved records exist; added per-file exception handlers so corrupted files never abort the scan.
  - Implemented batch track persistence (`upsert_scanned_library_tracks_batch` in `src/core/ports.py` and `src/db/repository.py`), committing records in 200-item chunks within single transactions instead of individual connection handshakes.

- [x] **M16: Robust Metadata Similarity Deduplication & Background Reconciliation Lockdown Mode**
  - Implemented multi-factor metadata similarity deduplication in `SqliteCatalogRepository.check_duplicate` (`src/db/repository.py`), comparing incoming tracks against indexed library tracks by normalized primary singer (`normalize_primary_artist`), title variants (`extract_title_search_variants` stripping leading disc/track numbers and artist prefixes), year-stripped album titles (`_best_album_similarity`), and audio duration tolerances (within 18s).
  - Fixed duplicate ingestion when identical songs exist under differing folder paths, casings, or tag schemes (e.g. `/Rkomi/Taxi Driver (2021)/02 - La coda del diavolo.flac` vs `/Rkomi/Rkomi - TAXI DRIVER/02 Rkomi - LA CODA DEL DIAVOLO.flac`), preventing duplicate tracks from being added twice.
  - Integrated metadata similarity checks across `IngestionPipeline` (`src/core/pipeline.py`) at Stage 3 (local tags), post-enrichment (MusicBrainz candidate match), final completion prior to file organization, and extra track batches.
  - Added background reconciliation state tracking (`is_reconciling` and `reconciliation_status` on `LibraryOrganizer` in `src/core/organizer.py`) and implemented a safe lockdown mode freezing song mutations, cover/lyrics updates, manual tag edits, and concurrent `/rescan` triggers while background filesystem synchronization is running.
  - Updated `/status` System Diagnostics (`src/bot/routers/commands.py`) to report live library synchronization state (`🟡 In Progress` vs `🟢 Ready`).

- [x] **M17: Guaranteed Read Permissions (`0o644` / `0o755`), Recursive Permission Repair, and Paginated/Sorted `/songs` Browser**
  - **Strict Permission Enforcement:** Fixed restrictive `0600` file modes caused by `shutil.copy2` copying metadata from local Bot API containers. Added `apply_file_permissions` (`((0o666 & ~umask) | 0o644)`) and `apply_dir_permissions` (`((0o777 & ~umask) | 0o755)`) across `src/core/organizer.py` and `src/core/extractor.py`. Guaranteed read permissions on ingested audio tracks, `.lrc` sidecars, `cover.jpg` art, staging scratches, and archive extractions so external media servers (Jellyfin, Plex, Navidrome) can read files.
  - **Recursive Permission Repair:** Added `fix_library_permissions` to `LibraryOrganizer` and wired it into library reconciliation so existing files with restrictive permissions are automatically repaired.
  - **Library Browser Pagination & Sorting:** Overhauled `/songs` interactive menu (`src/bot/routers/songs.py`, `src/bot/keyboards/songs.py`, `src/db/repository.py`, and `src/core/ports.py`) with 8-item-per-page pagination (`◀ Prev`, `📄 {page} / {total_pages}`, `Next ▶`), track counter indicators (`Tracks X–Y of Z`), sort selection menu (`🔀 Sort: {label} ▾` with options: Recently Added, Artist A–Z, Title A–Z, Album A–Z, Release Year), clear search filter button, and in-place navigation without chat spam.

- [x] **M18 (TMA Phase 1): Unified Web & API Server, Zero-Trust `initData` Cryptographic Validation, RBAC Bridge, Bot Menu Button & React/Vite `@telegram-apps/sdk-react` Scaffold**
  - Implemented zero-trust `HMAC-SHA256` signature verification in `src/bot/auth.py` strictly adhering to `mini_app_guidelineù.md` Section 4.2 (single URL-decode pass, lexicographical sorting, secret key derivation from `WebAppData` and bot token, constant-time `hmac.compare_digest` verification, and timestamp freshness check).
  - Built unified `BotWebServer` in `src/bot/webapp_server.py` running on `aiohttp.web`, integrating `/healthz`, `/api/v1/app/auth/me`, Authentik OIDC OAuth/webhooks, and SPA static file serving with `/dist/index.html` fallback.
  - Added typed settings (`WEB_APP_ENABLED`, `WEB_APP_URL`, `WEB_APP_STATIC_DIR`, `WEB_LISTEN_HOST`, `WEB_LISTEN_PORT`) in `src/config/settings.py` with YAML and `.env` precedence.
  - Configured persistent Telegram Menu Button (`setChatMenuButton` with `MenuButtonWebApp`) and inline launch buttons (`build_home_menu_inline_keyboard` in `src/bot/keyboards/admin.py` and `src/bot/routers/commands.py`).
  - Scaffolded modern Telegram Mini App frontend in `webapp/` using React 18, Vite, TypeScript, `@telegram-apps/sdk-react`, `@telegram-apps/telegram-ui`, client-side Telegram bootstrap (`miniApp.ready()`, `miniApp.mount()`, `viewport.expand()`, `swipeBehavior.disableVertical()`), dynamic CSS theme variables (`--tg-theme-*`), and resilient `initData` capture (`sessionStorage`).
  - Built production bundle (`webapp/dist`) and created unit test suites (`tests/unit/test_webapp_auth.py` and `tests/unit/test_webapp_server.py`), achieving 100% pass rate across all 86 unit and integration tests.

- [x] **M19 (TMA Phase 2): Telegram Mini App Music Catalog, HTTP 206 Partial Content Audio Streaming, Interactive Player & Synced Karaoke Lyrics**
  - Extended `src/bot/webapp_server.py` with authenticated catalog and streaming endpoints:
    - `GET /api/v1/app/tracks`: Paginated library tracks with debounced search, sort ordering (`recent`, `artist`, `title`, `album`, `year`), and count metadata.
    - `GET /api/v1/app/tracks/{id}`: Single track details, technical specs (duration, bitrate, file size, audio container).
    - `GET /api/v1/app/tracks/{id}/cover`: Cover art streaming with `Cache-Control` (`public, max-age=604800, immutable`), extracting `cover.jpg`/`cover.png` or embedded mutagen pictures with clean SVG fallback.
    - `GET /api/v1/app/tracks/{id}/audio`: HTTP 206 Partial Content range-enabled audio streaming via `aiohttp.web.FileResponse` (`Accept-Ranges: bytes`, `Content-Range`) supporting FLAC, MP3, AAC, and Opus.
    - `GET /api/v1/app/tracks/{id}/lyrics`: Structured raw and synced LRC sidecar extraction via `LibraryOrganizer`.
  - Built full music player frontend inside `webapp/`:
    - `AudioPlayerContext`: Global playback state, queue management, repeat/shuffle modes, Telegram haptic feedback (`hapticFeedback.impactOccurred('light')`), and `navigator.mediaSession` integration for native lock-screen controls and media keys.
    - `MiniPlayer`: Docked bottom bar (64px) with track thumbnail, progress line, and play/skip buttons.
    - `FullscreenPlayer`: Expandable player sheet with large high-res album art, interactive scrubber/seekbar, format chip, shuffle, repeat, and drawer triggers.
    - `SyncedLyrics`: Real-time karaoke `.lrc` scroller highlighting the current line, auto-centering playback, and supporting tap-to-seek to jump to any lyric timestamp.
    - `QueueDrawer`: Reorderable and clickable "Up Next" playlist drawer.
    - `CatalogView`: Search bar with 300ms debouncing, sort dropdown, track cards with format and LRC chips, and pagination controls.
  - Rebuilt production bundle (`webapp/dist/`) and added unit test coverage in `tests/unit/test_webapp_server.py`, achieving 100% pass rate across 90 unit and integration tests.

- [x] **M20 (TMA Phase 3): Metadata, Cover & Synced Lyrics Studio with Mandatory Dry-Run Confirmation**
  - Extended `src/bot/webapp_server.py` with authenticated Studio API endpoints:
    - `POST /api/v1/app/tracks/{id}/propose-edit`: Computes `SongEditProposal` diff, target filesystem path preview (`LibraryOrganizer.build_target_audio_path`), and change summary.
    - `POST /api/v1/app/tracks/{id}/apply-edit`: Commits confirmed changes to audio tags, relocates audio file and sidecars on disk (`LibraryOrganizer.relocate_and_retag_track`), updates SQLite `library_tracks` via `update_library_track_full`, and writes immutable audit log entry (`SONG_UPDATED_MINIAPP`).
    - `POST /api/v1/app/tracks/{id}/cover`: Supports multipart/form-data image upload and JSON base64; supports dry-run preview (`apply=false`) returning image hex for confirmation diff or live commit (`apply=true`) writing `cover.jpg` and embedding picture tags.
    - `POST /api/v1/app/tracks/{id}/lyrics`: Supports dry-run preview comparing old vs new lyrics and live commit (`apply=true`) writing `.lrc` sidecar, embedding audio lyric tags, and updating SQLite `has_synced_lyrics` flag.
    - `POST /api/v1/app/tracks/{id}/auto-enrich`: Re-queries MusicBrainz (`find_candidates`), Genius, Last.fm, and LRCLIB, returning candidate release matches with confidence percentages and best auto-fill values.
  - Enforced strict RBAC permission validation across all mutation endpoints (`has_permission(user, Permission.REQUEST_TAG_CORRECTION)`), denying `GUEST` users with HTTP 403 Forbidden.
  - Built comprehensive interactive Studio frontend in `webapp/src/components/studio/`:
    - `DryRunConfirmModal`: Strict implementation of the **Mandatory Dry-Run & Confirmation Policy**, displaying before/after diffs (`<s>Old</s> ➡️ <b color="#30d158">New</b>`), disk path relocation previews (`Old Path ➡️ New Path`), cover art and lyrics change badges, and requiring explicit `✅ Confirm & Apply Changes` confirmation before touching disk or database.
    - `EditMetadataModal`: Visual tag editor for Title, Artist, Album, Year, Track Number, plus interactive `🔎 Auto Search` querying MusicBrainz/Genius candidates for one-tap auto-filling.
    - `CoverUploaderModal`: Drag-and-drop or device file picker with current vs new artwork preview comparison.
    - `LrcEditorModal`: Dual-mode synced karaoke editor (interactive line editor with playback time-stamping `[mm:ss.xx]`, timing nudges `±0.5s`, and raw text editor).
    - `SongStudioModal`: Unified tabbed dialog coordinator embedded directly in `CatalogView` and `FullscreenPlayer`.
  - Built production bundle (`webapp/dist/`) and added unit test coverage in `tests/unit/test_webapp_server.py`, achieving 100% pass rate across all 95 unit and integration tests.

- [x] **M21 (TMA Phase 4): Web Ingestion Hub, Multipart Audio/Archive Upload, Real-Time SSE Event Stream & Interactive Live Queue Board**
  - Extended `src/bot/webapp_server.py` with authenticated Ingestion Hub and Queue Board endpoints:
    - `POST /api/v1/app/ingest/upload`: Multipart stream parser for audio (`.mp3`, `.flac`, `.m4a`, `.ogg`, `.opus`, `.wav`) and multi-track archives (`.zip`, `.7z`), writing directly to staging scratch directories, enforcing `Permission.SUBMIT_INGESTION` (rejecting `GUEST` with HTTP 403), enqueuing into `IngestionQueueWorker`, and asynchronously executing jobs under the worker semaphore.
    - `GET /api/v1/app/queue`: Fetches active, queued, paused, and recent jobs; differentiates owner scope (`?mine=true` or `VIEW_OWN_JOBS`) and moderator global scope (`VIEW_GLOBAL_QUEUE`); includes worker active count and concurrency limit.
    - `POST /api/v1/app/queue/{id}/action`: Comprehensive queue control endpoint supporting `pause`, `resume` / `unlock`, `cancel`, `select_candidate` (resolving disambiguation via `pipeline.confirm_candidate` and `queue_worker.record_confirmed_completion`), and `manual_search` (re-querying MusicBrainz via `pipeline.search_manual_candidates`).
    - `GET /api/v1/app/queue/events`: Server-Sent Events (SSE) streaming live updates (`job_enqueued`, `stage_changed`, `confirmation_required`, `job_completed`, `job_failed`, `job_state_changed`) and periodic keepalive pings (`: ping\n\n`) via `QueueEventBus`.
  - Built real-time Queue & Ingestion frontend in `webapp/`:
    - `FileDropzone`: Drag-and-drop / file picker upload component with format chips, file size calculation, and live `XMLHttpRequest` percentage progress bar (0..100%).
    - `StageVisualizer`: 6-stage visual pipeline tracker (Staging ➡️ Fingerprinting ➡️ Deduplication ➡️ Metadata ➡️ Lyrics ➡️ Library Organization) with animated active pulses and completion indicators.
    - `DisambiguationCard`: Candidate release selector rendering confidence match badges (`98% MATCH`), album/year/type details, MusicBrainz links, one-tap release confirmation, and manual query search input.
    - `JobCard`: Interactive job aggregate with status badges, controls (Pause, Resume, Cancel), and copyable job IDs.
    - `IngestQueueView`: Unified view combining `FileDropzone` with `Live Queue Board`, worker status indicator, SSE connection status pill, moderator scope toggle (My Uploads vs Global Queue), and active/history filter tabs.
  - Integrated third navigation tab (`⚡ Ingest & Queue`) into `webapp/src/App.tsx` and compiled production bundle (`webapp/dist/`).

- [x] **M22 (TMA Phase 5): Mini App User Governance, Role-Based Feature Locking, Deep-Linking & Offline Resilience**
  - Extended `src/bot/webapp_server.py` with authenticated Governance endpoints:
    - `GET /api/v1/app/admin/users`: Lists registered users with optional role and query search filters, enriched with upload stats (`albums` and `tracks` counts), requiring `Permission.MANAGE_USERS` (403 for unauthorized users).
    - `POST /api/v1/app/admin/users/{id}/role`: Updates a user's role tier via `governance_service.change_user_role`, preventing self-modification and unauthorized OWNER changes.
    - `POST /api/v1/app/admin/users/{id}/block`: Blocks or unblocks accounts via `governance_service.set_user_blocked`, preventing self-blocking.
    - `GET /api/v1/app/admin/access-requests`: Lists pending access requests awaiting review.
    - `POST /api/v1/app/admin/access-requests/{id}/resolve`: Approves (promoting to Uploader) or denies requests via `governance_service.approve_access_request` / `deny_access_request`.
  - Enforced strict **Role-Based Feature Locking** across the Mini App:
    - **Guest Users:** File upload dropzone is replaced with a locked access card; track editing slider in `TrackCard` is completely hidden; candidate release selection and job actions are locked.
    - **Non-Admins:** The Governance / Admin navigation tab and view are completely omitted and return access restriction cards if navigated to directly.
    - **Queue Actions:** Pause, resume, and cancel buttons on `JobCard` and release disambiguation selection on `DisambiguationCard` are only visible to the job submitter or moderators (`RESOLVE_OTHERS_JOBS`).
  - Implemented Telegram **Deep-Linking (`start_param`)**:
    - `track_{id}`: Automatically opens and streams the audio track in the player with synced lyrics.
    - `job_{id}` / `queue`: Switches directly to the Ingest & Queue view.
    - `admin` / `governance`: Switches to the Governance view for authorized admins.
    - Added `🚀 Open in Mini App` deep-link buttons in song action cards (`build_song_action_keyboard`), queued job cards (`build_queued_job_keyboard`), and `/app` command (`handle_cmd_app`).
  - Added **Offline Resilience & Service Worker**:
    - Created `webapp/public/manifest.json` and `webapp/public/sw.js` with app shell precaching, cache-first strategy for static assets, network-first for API requests, and registered in `main.tsx`.
- [x] **M23 (TMA Phase 6): Granular Catalog Access Control (`LISTENER` Role Tier), Low-Level Catalog Lockdown, Audio Picture Demuxer Repair & Range Stream Auth**
  - **Granular RBAC Split & Catalog Lockdown:**
    - Split unprivileged users into `GUEST` (default onboarding tier: no catalog viewing or streaming) and `LISTENER` (approved read-only tier with catalog browsing and streaming rights, but no upload/mutation permissions).
    - Added `UserRole.LISTENER = "LISTENER"` (Rank 1: `GUEST: 0 < LISTENER: 1 < UPLOADER: 2 < MODERATOR: 3 < ADMIN: 4 < OWNER: 5`) and `Permission.VIEW_CATALOG = "VIEW_CATALOG"`.
    - Protected all catalog endpoints in `src/bot/webapp_server.py` (`/api/v1/app/tracks`, `/audio`, `/lyrics`, `/cover`) and Telegram `/songs` menu with `Permission.VIEW_CATALOG`, denying `GUEST` users with HTTP 403 Forbidden.
    - Updated promotion/demotion hierarchies and Authentik OIDC group mapping (`music-listeners` / `music-viewers` -> `LISTENER`) in `src/core/rbac.py`.
    - Added `GuestCatalogLockedCard` in `webapp/src/App.tsx` and updated `GovernanceView.tsx` with `LISTENER` badge, filter pills, and access approval actions.
  - **Audio Streaming Failure Diagnosis & FLAC Header Sanitization:**
    - Diagnosed audio streaming failure on FLAC tracks (`Stay Alive` by Rie Takahashi and `La coda del diavolo` by Rkomi): embedded `METADATA_BLOCK_PICTURE` blocks contained empty MIME strings (`mime_len == 0`), which causes Chromium and FFmpeg demuxers (`libavformat/flac_picture.c`) to abort decoding immediately with `AVERROR_INVALIDDATA`.
    - Repaired picture headers on disk and added automatic FLAC picture sanitization (`_sanitize_audio_pictures_sync` in `src/core/organizer.py`), inspecting magic bytes to guarantee valid non-empty MIME types (`image/jpeg` / `image/png`) across tag writing and library rescanning.
  - **HTML5 `<audio>` Authenticated Range Streaming:**
    - HTML5 `<audio>` elements cannot set custom `Authorization` request headers; added `?tgWebAppData=${encodeURIComponent(rawInit)}` query parameter support in `webapp/src/context/AudioPlayerContext.tsx` and query extraction in `extract_raw_init_data_from_request` (`src/bot/webapp_server.py`).
    - Added playback error handling in `AudioPlayerContext.tsx` to prevent UI play state desynchronization.
    - Achieved 100% test pass rate across all 103 unit and integration tests (`exit code 0`), zero type errors (`ty check`), and zero lint warnings (`ruff`).
- [x] **M24: Autonomous Queue Worker Loop, Resilient Local/HTTP File Resolution & Multi-File Forward Debouncing**
  - **Autonomous Background Ingestion Loop:** Added `IngestionQueueWorker.run_worker_loop(stop_event)` backed by an `asyncio.Event` wake-up trigger (`_wake_event`), continuously polling oldest `QUEUED` jobs up to the concurrency limit (`concurrency_limit` 1..4) and processing them asynchronously. Wired worker loop lifecycle into `main.py` with graceful shutdown handling.
  - **Resilient File Ingress & Ghost Job Elimination:** Updated `_resolve_incoming_file_path` in `src/bot/routers/files.py` to return `(Path | None, str, str | None)`. Added direct HTTP streaming fallback via `telegram_local_api_url` if local disk resolution fails. Prevented phantom queued jobs when file fetching fails (e.g. Telegram Cloud API 20 MB file size limit when `--local` API is unavailable), reporting descriptive errors directly to users.
  - **Queue Stalling Guard:** Updated `execute_job` so if a job's `source_path` does not exist on disk, it immediately transitions to `FAILED`, notifies the progress handler, logs the incident, and frees the worker slot without stalling other jobs in the queue.
  - **Multi-File Forward & Album Debouncing:** Implemented a debounce buffer (`_BATCH_BUFFERS`, `_BATCH_TIMERS`, `_BATCH_LOCK`) grouping multiple forwarded files or media group album bursts. Aggregates all files into the queue concurrently and renders a single consolidated batch status card (`format_batch_queued_card` + `build_batch_queued_keyboard`) instead of posting fragmented individual messages.
- [x] **M25: Transparent Vite Dev Proxy, Auto-Launcher & Port 8080 Docker Conflict Resolution**
  - **Transparent Vite Dev Reverse-Proxy:** Enhanced `handle_spa_fallback` in `src/bot/webapp_server.py`. In `dev_mode` (`web_app_dev_mode=True`), whenever the Vite dev server is running on `http://127.0.0.1:5173`, `BotWebServer` (listening on port 8080) transparently proxies all SPA assets, Vite scripts, and WebSocket HMR upgrades to Vite while handling all `/api/v1/...` routes natively. When Vite is not running, it automatically falls back to serving compiled `webapp/dist/` assets.
  - **Vite Dev Server Auto-Launcher:** Added `start_vite_dev_server_if_needed` to `main.py`, automatically detecting whether Vite is already active on port 5173 or spawning `npm run dev` in the background when running on the host in `dev_mode`, with clean process termination on bot shutdown.
  - **Docker Port 8080 Conflict Diagnosis:** Diagnosed `502 Bad Gateway` error when using `cloudflared tunnel --url http://localhost:8080`: a stale `music-ingest-bot` container running in Docker had bound host port 8080 without an active webapp listener, dropping incoming tunnel connections. Stopped the conflicting container and documented hybrid development workflow (`docker compose up -d telegram-bot-api` only).
- [x] **M26: External Web App Port Exposure, Ingestion Finalization Exception Recovery, Real-Time Disambiguation SSE & Live Auto-Sync**
  - **Docker Compose Port Exposure:** Added `WEB_LISTEN_HOST: ${WEB_LISTEN_HOST:-0.0.0.0}` and `WEB_LISTEN_PORT: ${WEB_LISTEN_PORT:-8080}` to `docker-compose.yml` and `docker-compose.example.yaml`, exposing `${WEB_LISTEN_PORT:-${OIDC_LISTEN_PORT:-8080}}:${WEB_LISTEN_PORT:-8080}` so the Web App / Telegram Mini App can be accessed externally via reverse proxy, Cloudflare tunnel, or domain URL.
  - **Cross-Environment Staging Path Resolution:** Upgraded `_discover_staged_audio_files` in `src/core/pipeline.py` with multi-directory lookups translating container `/app/storage/scratch` to host `./storage/scratch` and resolving `telegram-bot-api` local paths. Added strict staged file existence checks (`primary_audio.is_file()`) in `confirm_candidate`, `complete_custom_ingestion`, and `search_manual_candidates`, eliminating unhandled crashes when jobs move between container and host environments.
  - **Ingestion Finalization Recovery:** Wrapped `_complete_ingestion` in `try...except Exception as exc:`, transitioning aborted jobs to `JobState.FAILED`, setting `error_message` and `completed_at`, saving to SQLite, and notifying stage listeners. Eliminated permanent stalls at `JobState.ORGANIZING`.
  - **Telegram Bot In-Place Failure UI:** Wired `stage_callback` into `handle_verification_callback` (`select` and `apply_edit`) in `src/bot/routers/verification.py`, rendering in-place progress during lyrics fetch and file organization, and rendering `⚠️ Ingestion Finalization Failed` with back navigation rather than leaving the user stuck on the loading card.
  - **Mini App Real-Time SSE & Fallback Auto-Sync:**
    - Provided `stage_callback` to `pipeline.confirm_candidate` in `src/bot/webapp_server.py`, streaming `stage_changed` SSE events during confirmation and publishing `job_failed` upon failure.
    - Updated `webapp/src/lib/queueApi.ts` to ensure `initData` is resolved before connecting `EventSource`.
    - Updated `webapp/src/views/IngestQueueView.tsx` with immediate `await loadQueue()` after candidate selection, pause, resume, cancel, or manual search, plus a 3.5s auto-polling fallback whenever active jobs exist or SSE disconnects, preventing the UI from freezing on selection.

- [x] **M27: Automated Lyrics Research (LRCLIB) & Selective Review (Accept All, Select Changes, Discard) in Bot & Mini App**
  - **Mini App Lyrics Research & Fine-Grained Dry-Run Selection:**
    - Added `POST /api/v1/app/tracks/{track_id}/auto-lyrics` on `BotWebServer` to research synced and unsynced lyrics via `LrclibLyricsClient` using track metadata or optional query overrides (`artist`, `title`, `query`).
    - Extended `POST /api/v1/app/tracks/{track_id}/apply-edit` to accept `accepted_fields: list[str]`, applying only the selected field modifications and preserving existing database and disk tags/sidecars for unselected fields.
    - Updated `LrcEditorModal` in Mini App Studio with an interactive Auto-Search Lyrics toolbar, live search status, query input override, and one-tap options to load into the synced editor or review and propose directly.
    - Upgraded `DryRunConfirmModal` with interactive checkboxes for each changed metadata field, cover art, and lyrics, quick filter pills (`Select All`, `Deselect All`, `Only Lyrics`, `Only Metadata`), a `❌ Discard Changes` action, and dynamic `✅ Accept All Changes` / `✅ Accept Selected Changes (X/Y)` submission.
  - **Telegram Bot Auto-Lyrics & Selective Dry-Run Workflow:**
    - Added `⚡ Auto-Search Lyrics` button to `build_lyrics_input_keyboard` (`songs:lyrics_menu`) and `build_job_lyrics_prompt_keyboard` (`verify:lyrics`), instantly fetching lyrics via LRCLIB and transitioning to the dry-run confirmation card or attaching lyrics to the queued job.
    - Updated `build_dry_run_confirmation_keyboard` in `src/bot/keyboards/songs.py` to offer 3 clear post-search actions:
      1. `✅ Accept All Changes`
      2. Quick filters: `🏷️ Metadata Only` and `📝 Lyrics Only`
      3. `🎛️ Select Changes...` to toggle individual fields via `SongsToggleFieldCallback` (`songstog`) in `build_selective_changes_keyboard` with live `[x]` / `[ ]` states and dynamic `Apply Selected (X/Y)`
      4. `❌ Discard Changes` (`songs:discard_changes`) safely resetting FSM state and preserving the original track intact.
    - Added comprehensive unit test suites in `tests/unit/test_bot_auto_lyrics_and_selective_review.py` and extended `tests/unit/test_webapp_server.py`, passing all 115 unit and integration tests.

- [x] **M28: Multi-Stage Container Packaging, Mini App Static Assets Baking & CI Build Verification**
  - Diagnosed Telegram Mini App placeholder fallback (`Frontend build in progress or static directory not found`): `Dockerfile` previously only copied Python sources and omitted `webapp/`, leaving `/app/webapp/dist` absent in production images pulled from GHCR.
  - Implemented multi-stage Docker build in `Dockerfile` using `node:22-bookworm-slim` (`webapp-builder` stage) running `npm ci` and `npm run build`, copying the compiled bundle into `/app/webapp/dist` of the final Python 3.14 runtime image.
  - Updated `.dockerignore` to ignore `webapp/node_modules/` and `webapp/dist/`, ensuring host artifacts do not bleed into the container build context.
  - Added `WEB_APP_DEV_MODE: false` and `WEB_APP_STATIC_DIR: /app/webapp/dist` environment defaults and optional local volume mounting comments in `docker-compose.yml` and `docker-compose.example.yaml`.
  - Added Node.js setup and `npm run build` verification steps to GitHub Actions CI (`.github/workflows/ci.yml`) and verified end-to-end container image creation (`docker run --rm music-ingest-bot:test ls -la /app/webapp/dist`).

- [x] **M29: Global ≥80% Test Coverage, In-RAM Audio Transcoding & Caching, Polite API Throttling, and Planned Bulk Re-Search**
  - **Global ≥80% Test Coverage:** Expanded unit and integration test suites so every single file in `src/` achieves ≥ 80% branch and statement coverage (total project test coverage: 87%, 234 passing tests, 0 failures), strictly enforcing hexagonal architecture boundaries in `tests/test_architecture.py`.
  - **In-RAM On-The-Fly Audio Transcoding & Range Streaming (`src/core/transcoder.py`):**
    - Added high-performance in-memory audio transcoder using `ffmpeg` pipes converting high-bitrate FLAC, WAV, and ALAC to Opus (`libopus` at 128 kbps) or MP3 (`libmp3lame` at 192 kbps) without touching disk.
    - Integrated in-RAM chunked caching and HTTP 206 Partial Content (Range request) support in `src/bot/webapp_server.py` (`/api/v1/app/tracks/{id}/audio?format=opus`), drastically accelerating audio playback start times on mobile devices and low-bandwidth connections.
  - **Fast Cover Art LRU Caching & Sidecar Write-Through:**
    - Implemented in-RAM LRU cache for album artwork with fast thumbnailing, extracting embedded `METADATA_BLOCK_PICTURE`/`APIC` tags and writing through to sidecar `cover.jpg` files.
    - Added HTTP `ETag` and `304 Not Modified` conditional response headers in `BotWebServer` cover endpoints, cutting repeated cover fetch latency to near-zero.
  - **Polite External API Throttling (`src/core/throttler.py`):**
    - Built thread-safe, async leaky-bucket / token-bucket rate throttler enforcing external API consumption limits: MusicBrainz (strict 1.0 req/s), AcoustID (3.0 req/s), Genius (4.0 req/s), Last.fm (5.0 req/s), LRCLIB (3.0 req/s), and Cover Art Archive (2.0 req/s).
    - Integrated automatic retry with exponential backoff on HTTP 429 Too Many Requests and HTTP 503 Service Unavailable, preventing IP rate bans during heavy ingestion or bulk re-searches.
  - **Planned Bulk Metadata & Lyrics Re-Search (`src/core/bulk_research.py`):**
    - Built `BulkResearchService` allowing users and moderators to trigger automated re-search across multiple selected tracks or the entire music catalog.
    - Features dry-run execution with duration/timing estimates, batch progress tracking, configurable confidence threshold auto-application (default: ≥ 98% auto-applied), and full audit logging (`AuditAction.BULK_METADATA_ENRICHED`).
    - Integrated with Telegram `/songs` menu (`songs:bulk_research`, `src/bot/routers/songs.py`) with confirmation previews and live progress updates.

### Planned Backlog (Future Enhancements)
- [x] **Phase 4: Telegram Mini App Web Ingestion Hub & Live Queue Board** (Completed in M21)
- [x] **Phase 5: Telegram Mini App Governance, Deep-Linking & Offline Resilience** (Completed in M22)
- [x] **Phase 6: Granular Catalog Access Control, Guest Lockdown & Audio Streaming Repair** (Completed in M23)
- [ ] **Low-Priority Backlog: Admin Folder Structure Viewer & Mass Migration**
  - Allow upper roles (`ADMIN` / `OWNER`) to browse the `storage/library` folder hierarchy in Telegram and run dry-run previewed mass path/tag migrations across the catalog.


