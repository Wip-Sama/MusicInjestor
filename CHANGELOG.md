# Project Changelog & Milestone History (`CHANGELOG.md`)

> **Instruction for All Future Agents & Developers:**
> Whenever you complete a feature, milestone, bug fix, or architectural modification, **update this file** with a dedicated entry detailing what was added, modified, or repaired. Keep entries chronological, clear, and comprehensive.

---

## [Unreleased]
- None

---

## Completed Milestones

### M35: Synchronized Lyrics Preference & Robust In-File Audio Tag Embedding
- **LRCLIB Search Client Synchronized Priority (`src/core/lyrics.py`):**
  - Updated `LrclibLyricsClient.fetch_lyrics` to prioritize synchronized lyrics over plain text:
    - If `/get` with `album_name` returns plain lyrics (`syncedLyrics: null`), it retains it as `plain_fallback` and queries `/get` without `album_name` and `/search` to find a synchronized version.
    - Updated `_score_search_hit` to boost synchronized lyrics (`synced_bonus = 0.35`). Separated candidate evaluation in `/search` into `best_synced_hit` and `best_plain_hit`, returning the synchronized match whenever available and only falling back to plain lyrics if no synced lyrics exist on LRCLIB.
- **In-File Audio Tag Lyrics Embedding (`src/core/organizer.py`):**
  - **ID3 / MP3 / WAV (`_write_id3_lyrics_frames`):** Purges all pre-existing stale `USLT` frames (including different languages/descriptions like `USLT:desc:XXX`), `SYLT`, `ULT`, and case-insensitive `TXXX` lyrics frames (`LYRICS`, `SYNCEDLYRICS`, `UNSYNCEDLYRICS`). When synced lyrics are available, writes timestamped LRC into `USLT` (enabling synced playback on players like Symfonium, Poweramp, and Samsung Music), `TXXX:SYNCEDLYRICS`, `TXXX:LYRICS`, and parsed `(text, timestamp_ms)` into `SYLT` frames.
  - **FLAC / Ogg / Opus (`_embed_lyrics_into_audio_sync`):** Embeds synced timestamped lyrics into `raw_audio["LYRICS"]` and `raw_audio["SYNCEDLYRICS"]`, preserving plain lyrics in `UNSYNCEDLYRICS` if available.
  - **MP4 / M4A / ALAC (`_embed_lyrics_into_audio_sync`):** Writes timestamped lyrics to `\xa9lyr` and `----:com.apple.iTunes:SYNCEDLYRICS`.
  - **Pre-Existing Synced Lyrics Retention (`finalize_track`, `relocate_and_retag_track`):** Prevents downgrading of pre-existing embedded synced lyrics when external providers return only plain lyrics or none.
  - **Extraction from Audio Tags (`_extract_existing_lyrics_sync`):** Added support for reading M4A `----:com.apple.iTunes:SYNCEDLYRICS`, iterating all `USLT` frames to find timestamped LRC, and reconstructing LRC from ID3 `SYLT` frames.
- **Accurate Synced Classification & Re-Search Targeting:**
  - Fixed `_scan_track_file_sync` and `bulk_research.py` where tracks with plain lyrics were falsely flagged as `has_synced_lyrics = True`.
  - Added reusable `is_synced_lyrics_text` helper in `src/core/organizer.py` verifying standard `[mm:ss.xx]` timestamp tags (preventing bracketed text like `[Verse 1]` from being misclassified as synced lyrics).
  - Updated Telegram Bot (`songs.py`, `verification.py`) and Web Mini App (`webapp_server.py`) to accurately report and preserve `has_synced_lyrics`.
- **Quality Assurance & Verification:**
  - Added dedicated unit test suite `tests/unit/test_synced_lyrics_preference.py` covering LRCLIB synced priority, ID3 frame wiping, FLAC/M4A tag embedding, SYLT reconstruction, non-downgrading retention, and bulk re-search upgrade.
  - Verified 100% pass across all 271 tests in the repository, with zero warnings on `uv run ty check`, and clean `uv run ruff check` and `uv run ruff format`.

### M34: Release Radar & Artist Notification System (Deezer Integration & 30s Previews)
- **Architecture & Provider Selection:**
  - Evaluated external metadata & streaming data providers (Deezer, Spotify, MusicBrainz, ListenBrainz, Last.fm). Selected Deezer as the primary Release Radar provider because it requires no API key, incurs zero additional third-party dependencies (driven natively by `httpx`), and serves direct 30-second MP3 audio preview streams out of the box.
- **Core Domain & Ingestion Engine (`src/core/`):**
  - **Models & Enums (`src/core/models.py`):** Added `FollowedArtist`, `DiscoveredRelease`, and `ReleaseNotification` pure domain dataclasses with SQLite roundtrip compatibility and typing.
  - **Port Definitions (`src/core/ports.py`):** Defined `ReleaseRadarProviderPort` with `search_artist`, `get_artist_recent_releases`, and `get_release_preview_track`. Added persistence protocols to `GovernanceRepositoryPort` for following/unfollowing, listing monitored artists, upserting discovered releases, and managing notifications.
  - **Deezer Provider (`src/core/deezer.py`):** Implemented `DeezerReleaseProvider` supporting exact artist matching, discography crawling (`/artist/{id}/albums`), album track inspection (`/album/{id}/tracks`), and high-res cover art resolution.
  - **Radar Domain Orchestrator (`src/core/radar.py`):** Built `ReleaseRadarService` managing artist follow/unfollow lifecycle, date parsing/recency filtering (`is_release_recent`), background scanning across all monitored artists (library tracks + user follows), and binary audio preview byte streaming (`get_preview_audio_bytes`).
- **Persistence Layer (`src/db/models.py`, `src/db/repository.py`):**
  - Added SQLite tables and indexes: `artist_follows` (`(telegram_id, artist_name)` unique constraint), `discovered_releases` (`(provider, provider_release_id)` unique constraint), and `release_notifications`.
  - Added repository methods: `follow_artist`, `unfollow_artist`, `list_user_followed_artists`, `is_artist_followed`, `list_all_monitored_artists`, `upsert_discovered_releases` (with automatic `in_library` reconciliation against `library_tracks`), `list_discovered_releases`, `get_discovered_release`, `create_release_notifications`, `list_user_release_notifications`, and `mark_notification_read`.
- **Telegram Bot Interface (`src/bot/`):**
  - **Command & Navigation (`src/bot/routers/radar.py`, `src/bot/keyboards/radar.py`):** Added `/radar` command, paginated recent drops list, single release inspection card, and followed artists management list with one-tap unfollow controls.
  - **30-Second Audio Delivery:** Implemented `handle_radar_preview_audio` streaming 30-second audio files directly into Telegram chats via `BufferedInputFile` with deep-links into the Web Mini App and Deezer.
  - **Cross-Router Integration:** Added `📡 Artist on Radar` action in `/songs` track details and registered `/radar` in the bot commands menu.
  - **Callback Safety:** Configured `RadarCallback` with `sep="~"` to prevent collision with colons in external identifiers and artist names.
- **Song Artist Following & Responsive Navigation UX:**
  - **Mini App Follow from Song (`FullscreenPlayer.tsx`, `TrackCard.tsx`, `SongStudioModal.tsx`, `AudioPlayerContext.tsx`):** Added global `isArtistFollowed` and `toggleFollowArtist` methods in `AudioPlayerContext` with instant optimistic caching and Telegram haptic feedback. Added sleek follow pill buttons (`🔔 Follow` / `✓ Following`) in the Fullscreen Player next to the artist name, in the Song Studio header, and as a quick one-tap bell icon next to Studio sliders on Catalog track rows. Synchronized artist follows seamlessly across Player, Catalog, and Radar views.
  - **Scrollable Top Navigation Bar (`webapp/src/App.tsx`):** Fixed top navigation tab bar overflow on mobile viewports by enabling smooth horizontal touch scrolling (`overflowX: auto`, hidden scrollbars) and `flex: 1 0 auto` with `minWidth: max-content` on tab buttons, preventing icon/text truncation.
  - **Responsive Search & Sort Layout (`webapp/src/views/CatalogView.tsx`):** Protected sort selector with `flexShrink: 0` and `minWidth: max-content`, added `minWidth: 0` and flexible shrinking to the search input container with concise placeholder, preventing the sort dropdown from being pushed out of the viewport.
  - **Telegram Bot Follow from Song (`src/bot/routers/songs.py`, `src/bot/keyboards/songs.py`):** Enhanced `/songs` action menu to display follow state on the `📡 Artist on Radar` button and present an interactive artist follow screen with one-tap toggle when no prior releases are cached in the database.
- **Web Mini App (`webapp/`, `src/bot/webapp_server.py`):**
  - **REST Endpoints:** Added `GET /api/v1/app/radar/releases`, `GET /api/v1/app/radar/follows`, `POST /api/v1/app/radar/follow`, and `POST /api/v1/app/radar/scan`.
  - **Radar View (`webapp/src/views/RadarView.tsx`):** Built a responsive Release Radar tab in the Mini App featuring release cards with cover art, release type badges, library status badges, 30s audio preview streaming integrated with the global `useAudioPlayer`, follow/unfollow buttons with Telegram haptic feedback, and a Followed Artists modal.
  - **App Routing (`webapp/src/App.tsx`):** Added bottom navigation "Radar" tab and deep linking support (`startapp=radar`, `startapp=release_{id}`).
- **Configuration & Worker Scheduling (`src/config/settings.py`, `main.py`):**
  - Added `radar_enabled` (default `True`) and `radar_sync_interval_seconds` (default `43200` = 12h).
  - Wired periodic background loop `run_periodic_release_radar_loop` with graceful cancellation in `main.py`.
- **Test Coverage & Quality Assurance:**
  - Added isolated unit tests: `test_deezer_provider.py`, `test_release_radar.py`, `test_bot_radar.py`, `test_webapp_radar.py`, and integration tests in `test_db_radar.py`.
  - Executed full test suite: 260 tests passed, 0 failures, 100% architecture boundary compliance. Clean `ty` check and `npm run build`.

### M33: Pre-Commit External Link Search & Queue Tag Editing (Telegram Bot & Mini App)
- **Telegram Bot Pre-Commit Link Search & Tag Editor:**
  - **Link Search Action & Keyboard (`src/bot/keyboards/verification.py`):** Added `🔗 Search by Link` button (`VerifyActionCallback(action="link_search", job_id=job_id)`) positioned alongside `⚡ Auto Re-Search` in `build_job_edit_keyboard`.
  - **Interactive URL/MBID Resolution (`src/bot/routers/verification.py`):** Added callback handler prompting the user with a level-2 escape keyboard to supply MusicBrainz URLs (recording, release, release-group), MBIDs, or search queries. Updated `handle_job_metadata_input` to intercept link inputs or any pasted MusicBrainz URLs (`_MB_URL_RE`), query `pipeline.search_manual_candidates`, update candidate selections and proposal fields (`new_title`, `new_artist`, `new_album`, `new_year`, `new_track_number`, `new_recording_mbid`, `new_release_mbid`), auto-fetch LRCLIB lyrics, and refresh the live dry-run tag editor card.
- **Mini App Pre-Commit Tag & Metadata Editor:**
  - **REST API Queue Action (`src/bot/webapp_server.py`):** Extended `POST /api/v1/app/queue/{job_id}/action` to handle `apply_edit` / `apply_custom_metadata`, supporting custom overrides for title, artist, album, year, track number, cover art, and lyrics. Finalizes ingestion via `pipeline.complete_custom_ingestion`, unblocks the queue worker, and emits real-time SSE updates.
  - **Queue Client API (`webapp/src/lib/queueApi.ts`):** Added `applyJobEdit(jobId, payload)` supporting custom metadata, lyrics, and cover base64 payloads.
  - **Accessible Tag Editor Modal (`webapp/src/components/queue/JobEditModal.tsx`):** Created a modal component supporting external link/query fetching, selectable candidate chips with confidence scoring, editable metadata fields (`Title`, `Artist`, `Album`, `Year`, `Track #`), IME-safe Enter handling (`!event.nativeEvent.isComposing`), and one-click "Confirm & Ingest Track".
  - **Queue UI Integration (`webapp/src/components/queue/JobCard.tsx`, `webapp/src/components/queue/DisambiguationCard.tsx`, `webapp/src/views/IngestQueueView.tsx`):** Added "Edit Tags" button to active queued/paused/awaiting jobs and per-candidate "Edit & Ingest" actions in disambiguation cards.
- **Test Coverage & Quality Assurance:**
  - Added unit test `test_verification_callback_link_search_and_input` in `tests/unit/test_verification_router_coverage.py`.
  - Added unit test coverage for `apply_edit` queue action in `tests/unit/test_webapp_server.py`.
  - Verified 100% pass across all 246 test suite tests, zero `ruff` errors, clean `ty` checks on `src`, and successful Vite production build.

### M32: Mobile Header Optimization, Tap-and-Hold Song Multi-Select & Live In-Queue Bulk Re-Search Telemetry
- **Mobile Header Optimization & Clutter Elimination (`webapp/src/views/CatalogView.tsx`):**
  - Removed overflowing "Select" and "Bulk" buttons from the top search row, leaving only the Search input and Sort selector for optimal mobile viewport responsiveness without button clipping.
- **Tap-and-Hold Touch UX for Song Selection (`webapp/src/components/catalog/TrackCard.tsx`, `webapp/src/views/CatalogView.tsx`):**
  - Implemented cross-platform pointer-based press-and-hold (~450ms) gesture (`onPointerDown`, `onPointerMove`, `onPointerUp`, `onPointerCancel`) on `TrackCard` that enters selection mode and selects the pressed track.
  - Added movement threshold cancellation (> 8px) to allow natural touch scrolling without accidental activation.
  - Added click suppression (`didLongPressRef`) preventing accidental audio playback or track toggling upon gesture release.
  - Disabled native callouts and selection highlights via `userSelect: 'none'` and `WebkitTouchCallout: 'none'`.
  - Added haptic feedback trigger via Telegram WebApp `HapticFeedback.impactOccurred('medium')`.
  - Maintained full bulk re-search workflow via the persistent floating Selection Control Bar (`Bulk Check (N)` and `Exit`).
- **Live In-Queue Bulk Re-Search Telemetry (Telegram Bot & Web Mini App):**
  - **Core Progress Tracking (`src/core/bulk_research.py`):** Added `BulkResearchProgress` pure dataclass with calculated percentage property, module-level progress registry (`get_active_bulk_research`, `set_active_bulk_research`), real-time status updates per track, and guaranteed `finally:` cleanup back to `None`.
  - **Mini App Ingestion Queue Live Progress Card (`webapp/src/views/IngestQueueView.tsx`, `webapp/src/types/queue.ts`):** Added real-time progress card with percentage badge, progress track, current track title, and initiator name. Integrated live SSE updates on `bulk_research_progress` events and updated `hasActiveJobs` to maintain live board monitoring while a bulk job is executing.
  - **Webapp REST API & Event Bus (`src/bot/webapp_server.py`):** Extended `GET /api/v1/app/queue` to return `active_bulk_research` snapshot, and updated `POST /api/v1/app/tracks/bulk-research/execute` to publish live `bulk_research_progress` events via `QueueEventBus`.
  - **Telegram Bot In-Flight Telemetry (`src/bot/routers/commands.py`, `src/bot/routers/songs.py`):** Integrated active bulk re-search progress into Telegram `/queue` overview screen (preventing empty queue rendering during bulk jobs) and `/status` diagnostics card. Added throttled 2-second live message updating during Telegram `/songs` bulk execution.
- **Test Coverage & Quality Assurance:**
  - Added unit tests for `BulkResearchProgress` percent calculations, lifecycle tracking, and cleanup (`tests/unit/test_bulk_research.py`).
  - Added unit tests for `/queue` and `/status` active bulk re-search rendering (`tests/unit/test_commands_coverage.py`).
  - Verified all 245 test suite tests passing with exit code 0 and full frontend build passing.

### M31: Configurable OIDC Role Mapping, Local Role Overrides & OIDC Account Disconnection
- **OIDC Configuration & `SECRET_KEY` Documentation (`.env.example`):**
  - Added comprehensive step-by-step guide for generating a secure `SECRET_KEY` (via `openssl`, Python `secrets`, or PowerShell) and detailed its multi-layered cryptographic roles: HMAC-SHA256 state nonce signing, token encryption (AES/HMAC keystream in SQLite), and Mini App dev/session HMAC signing.
  - Documented Authentik OAuth2/OIDC provider configuration, client credentials, redirect URI (`http://<host>:8080/api/v1/auth/callback`), scopes (`openid`, `profile`, `email`, `groups`), and automatic role mapping syntax.
- **Configurable OIDC Group-to-Role Mapping (`OIDC_ROLE_MAPPING`):**
  - Added `oidc_role_mapping` in `AppSettings` (`src/config/settings.py`) with support for both JSON (`{"group": "ROLE"}`) and comma-separated pairs (`group=ROLE,group2=ROLE2`).
  - Updated `evaluate_oidc_role(groups, role_mapping=None)` (`src/core/rbac.py`) and `AuthentikOidcService` (`src/core/oidc.py`) to resolve IDP groups to local RBAC roles (`GUEST`, `LISTENER`, `UPLOADER`, `MODERATOR`, `ADMIN`).
- **Local Role Overrides & IDP Sync Protection:**
  - Added `oidc_role_override` boolean flag to `UserAccount` (`src/core/models.py`), SQLite schema (`src/db/models.py`), auto-migration `_migrate_users_table` (`src/db/database.py`), and repository queries (`src/db/repository.py`).
  - When an admin updates a user's role manually (`change_user_role` in `src/core/rbac.py`), `oidc_role_override` is automatically set to `True`, locking the assigned role against background 15-minute OIDC claim stomping.
  - Added `GovernanceService.set_user_oidc_override()` allowing admins to toggle the local override lock on/off with audit logging (`AuditAction.OIDC_OVERRIDE_UPDATED`).
- **OIDC Account Disconnection (Self-Service & Admin):**
  - Implemented self-service unlinking in Telegram bot `/settings` menu (`SettingsCallback(action="oidc", value="disconnect")`) and SSO screen (`AccessReqSimpleCallback(action="oidc_disconnect_self")`).
  - Implemented administrative unlinking in Telegram `/admin` user details view (`AdminActionCallback(action="oidc_disconnect")`) with audit logging (`AuditAction.OIDC_UNLINKED`).
  - Added REST endpoints in `BotWebServer` (`src/bot/webapp_server.py`):
    - `POST /api/v1/app/auth/oidc/disconnect` (self-service unlinking).
    - `POST /api/v1/app/admin/users/{user_id}/oidc/disconnect` (administrative unlinking).
    - `POST /api/v1/app/admin/users/{user_id}/oidc/override` (administrative role lock toggling).
    - Extended `/api/v1/app/auth/me` and `/api/v1/app/admin/users` to expose `authentik_linked`, `authentik_groups`, and `oidc_role_override`.

### M30: Interactive Bulk Operations UI, In-RAM Stream Transcoding Controls, Persistent User Preferences & Lyrics Year Bugfix
- **Interactive Bulk Operations UI (Mini App & Telegram Bot):**
  - **Mini App Multi-Select & Modal (`webapp/src/views/CatalogView.tsx`, `webapp/src/components/bulk/BulkResearchModal.tsx`):** Added multi-selection mode with circular check indicators on `TrackCard`, select/deselect all on current page, and floating action bar. Added interactive `BulkResearchModal` supporting 5 explicit scopes (Selected Tracks, Filtered Search Query, Missing Lyrics Only, Missing MBID Only, Entire Library), dry-run plan inspection with duration estimates, progress bar, and an interactive review carousel for ambiguous candidate resolution with Accept, Skip, Discard, Accept All, and Discard All actions.
  - **Telegram Bot Scope & Review Flow (`src/bot/routers/songs.py`, `src/bot/keyboards/songs.py`):** Added explicit scope selection buttons and interactive pagination review workflow (`SongsBulkReviewCallback`) allowing single-track inspection, relocation, retagging, or bulk application/discarding with audit logging.
- **In-RAM Stream Transcoding Controls & Quality Switcher:**
  - Added support for `format=original` (direct HTTP 206 partial content streaming without ffmpeg CPU load), `format=opus`, `format=mp3`, and user-configurable bitrates (64, 96, 128, 160, 192, 256, 320 kbps).
  - Integrated interactive streaming format badge and quality picker bottom sheet in `FullscreenPlayer.tsx` allowing seamless hot-swapping during audio playback without losing track progress.
- **Persistent User Preferences in SQLite & Cross-Platform Sync:**
  - Added `UserPreferences` pure domain dataclass (`src/core/models.py`) and protocol methods (`src/core/ports.py`).
  - Created `user_settings` SQLite table with index (`src/db/models.py`) and implemented WAL persistence in `SqliteCatalogRepository` (`src/db/repository.py`).
  - Added `/settings` Telegram bot command and `SettingsCallback` handlers (`src/bot/routers/commands.py`).
  - Added REST endpoints `GET /api/v1/app/user/settings` and `POST /api/v1/app/user/settings` in `BotWebServer` (`src/bot/webapp_server.py`) and built `SettingsModal.tsx` in the Mini App with audio preferences and theme settings.
- **Lyrics Search Release Date (Year) Bugfix:**
  - Repaired issue where lyrics search was proposing `year -> None`. Fixed across `src/bot/webapp_server.py`, `src/core/bulk_research.py`, `src/bot/routers/songs.py`, and `webapp/src/components/studio/LrcEditorModal.tsx` to preserve existing track release dates when LRCLIB returns no release date.
- **Test Coverage & Quality Gates:**
  - Added unit test suites for `UserPreferences` SQLite persistence, `/settings` bot handlers, and webapp server settings and streaming format endpoints (`tests/unit/test_user_preferences_and_settings.py`, `tests/unit/test_webapp_server.py`).
  - Maintained ≥ 80% coverage across every file in `src/` (234 passing unit/architecture tests, 86% global coverage). Clean type checking (`ty check`) and linting (`ruff check`). Clean frontend production bundle (`npm run build`).

### M29: Global ≥80% Test Coverage, In-RAM Audio Transcoding & Caching, Polite API Throttling, and Planned Bulk Re-Search
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

### M28: Multi-Stage Container Packaging, Mini App Static Assets Baking & CI Build Verification
- Diagnosed Telegram Mini App placeholder fallback (`Frontend build in progress or static directory not found`): `Dockerfile` previously only copied Python sources and omitted `webapp/`, leaving `/app/webapp/dist` absent in production images pulled from GHCR.
- Implemented multi-stage Docker build in `Dockerfile` using `node:22-bookworm-slim` (`webapp-builder` stage) running `npm ci` and `npm run build`, copying the compiled bundle into `/app/webapp/dist` of the final Python 3.14 runtime image.
- Updated `.dockerignore` to ignore `webapp/node_modules/` and `webapp/dist/`, ensuring host artifacts do not bleed into the container build context.
- Added `WEB_APP_DEV_MODE: false` and `WEB_APP_STATIC_DIR: /app/webapp/dist` environment defaults and optional local volume mounting comments in `docker-compose.yml` and `docker-compose.example.yaml`.
- Added Node.js setup and `npm run build` verification steps to GitHub Actions CI (`.github/workflows/ci.yml`) and verified end-to-end container image creation (`docker run --rm music-ingest-bot:test ls -la /app/webapp/dist`).

### M27: Automated Lyrics Research (LRCLIB) & Selective Review (Accept All, Select Changes, Discard) in Bot & Mini App
- **Mini App Lyrics Research & Fine-Grained Dry-Run Selection:**
  - Added `POST /api/v1/app/tracks/{track_id}/auto-lyrics` on `BotWebServer` to research synced and unsynced lyrics via `LrclibLyricsClient` using track metadata or optional query overrides (`artist`, `title`, `query`).
  - Extended `POST /api/v1/app/tracks/{track_id}/apply-edit` to accept `accepted_fields: list[str]`, applying only the selected field modifications and preserving existing database and disk tags/sidecars for unselected fields.
  - Updated `LrcEditorModal` in Mini App Studio with an interactive Auto-Search Lyrics toolbar, live search status, query input override, and one-tap options to load into the synced editor or review and propose directly.
  - Upgraded `DryRunConfirmModal` with interactive checkboxes for each changed metadata field, cover art, and lyrics, quick filter pills (`Select All`, `Deselect All`, `Only Lyrics`, `Only Metadata`), a `❌ Discard Changes` action, and dynamic `✅ Accept All Changes` / `✅ Accept Selected Changes (X/Y)` submission.
- **Telegram Bot Auto-Lyrics & Selective Dry-Run Workflow:**
  - Added `⚡ Auto-Search Lyrics` button to `build_lyrics_input_keyboard` (`songs:lyrics_menu`) and `build_job_lyrics_prompt_keyboard` (`verify:lyrics`), instantly fetching lyrics via LRCLIB and transitioning to the dry-run confirmation card or attaching lyrics to the queued job.
  - Updated `build_dry_run_confirmation_keyboard` in `src/bot/keyboards/songs.py` to offer clear post-search actions (`Accept All`, `Metadata Only`, `Lyrics Only`, `Select Changes...`, `Discard Changes`).
  - Added comprehensive unit test suites in `tests/unit/test_bot_auto_lyrics_and_selective_review.py` and extended `tests/unit/test_webapp_server.py`.

### M26: External Web App Port Exposure, Ingestion Finalization Exception Recovery, Real-Time Disambiguation SSE & Live Auto-Sync
- **Docker Compose Port Exposure:** Added `WEB_LISTEN_HOST: ${WEB_LISTEN_HOST:-0.0.0.0}` and `WEB_LISTEN_PORT: ${WEB_LISTEN_PORT:-8080}` to `docker-compose.yml` and `docker-compose.example.yaml`, exposing `${WEB_LISTEN_PORT:-${OIDC_LISTEN_PORT:-8080}}:${WEB_LISTEN_PORT:-8080}` so the Web App / Telegram Mini App can be accessed externally via reverse proxy, Cloudflare tunnel, or domain URL.
- **Cross-Environment Staging Path Resolution:** Upgraded `_discover_staged_audio_files` in `src/core/pipeline.py` with multi-directory lookups translating container `/app/storage/scratch` to host `./storage/scratch` and resolving `telegram-bot-api` local paths. Added strict staged file existence checks (`primary_audio.is_file()`) in `confirm_candidate`, `complete_custom_ingestion`, and `search_manual_candidates`, eliminating unhandled crashes when jobs move between container and host environments.
- **Ingestion Finalization Recovery:** Wrapped `_complete_ingestion` in `try...except Exception as exc:`, transitioning aborted jobs to `JobState.FAILED`, setting `error_message` and `completed_at`, saving to SQLite, and notifying stage listeners. Eliminated permanent stalls at `JobState.ORGANIZING`.
- **Telegram Bot In-Place Failure UI:** Wired `stage_callback` into `handle_verification_callback` (`select` and `apply_edit`) in `src/bot/routers/verification.py`, rendering in-place progress during lyrics fetch and file organization, and rendering `⚠️ Ingestion Finalization Failed` with back navigation rather than leaving the user stuck on the loading card.
- **Mini App Real-Time SSE & Fallback Auto-Sync:** Provided `stage_callback` to `pipeline.confirm_candidate` in `src/bot/webapp_server.py`, streaming `stage_changed` SSE events during confirmation and publishing `job_failed` upon failure. Updated `webapp/src/views/IngestQueueView.tsx` with immediate queue reload after actions and 3.5s auto-polling fallback.

### M25: Transparent Vite Dev Proxy, Auto-Launcher & Port 8080 Docker Conflict Resolution
- **Transparent Vite Dev Reverse-Proxy:** Enhanced `handle_spa_fallback` in `src/bot/webapp_server.py`. In `dev_mode` (`web_app_dev_mode=True`), whenever the Vite dev server is running on `http://127.0.0.1:5173`, `BotWebServer` (listening on port 8080) transparently proxies all SPA assets, Vite scripts, and WebSocket HMR upgrades to Vite while handling all `/api/v1/...` routes natively. When Vite is not running, it automatically falls back to serving compiled `webapp/dist/` assets.
- **Vite Dev Server Auto-Launcher:** Added `start_vite_dev_server_if_needed` to `main.py`, automatically detecting whether Vite is already active on port 5173 or spawning `npm run dev` in the background when running on the host in `dev_mode`, with clean process termination on bot shutdown.
- **Docker Port 8080 Conflict Diagnosis:** Diagnosed `502 Bad Gateway` error when using `cloudflared tunnel --url http://localhost:8080`: a stale `music-ingest-bot` container running in Docker had bound host port 8080 without an active webapp listener, dropping incoming tunnel connections. Stopped the conflicting container and documented hybrid development workflow.

### M24: Autonomous Queue Worker Loop, Resilient Local/HTTP File Resolution & Multi-File Forward Debouncing
- **Autonomous Background Ingestion Loop:** Added `IngestionQueueWorker.run_worker_loop(stop_event)` backed by an `asyncio.Event` wake-up trigger (`_wake_event`), continuously polling oldest `QUEUED` jobs up to the concurrency limit (`concurrency_limit` 1..4) and processing them asynchronously.
- **Resilient File Ingress & Ghost Job Elimination:** Updated `_resolve_incoming_file_path` in `src/bot/routers/files.py` to return `(Path | None, str, str | None)`. Added direct HTTP streaming fallback via `telegram_local_api_url` if local disk resolution fails. Prevented phantom queued jobs when file fetching fails.
- **Queue Stalling Guard:** Updated `execute_job` so if a job's `source_path` does not exist on disk, it immediately transitions to `FAILED`, notifies the progress handler, logs the incident, and frees the worker slot without stalling other jobs in the queue.
- **Multi-File Forward & Album Debouncing:** Implemented a debounce buffer (`_BATCH_BUFFERS`, `_BATCH_TIMERS`, `_BATCH_LOCK`) grouping multiple forwarded files or media group album bursts. Aggregates all files into the queue concurrently and renders a single consolidated batch status card instead of posting fragmented individual messages.

### M23: Granular Catalog Access Control (`LISTENER` Role Tier), Low-Level Catalog Lockdown, Audio Picture Demuxer Repair & Range Stream Auth
- **Granular RBAC Split & Catalog Lockdown:** Split unprivileged users into `GUEST` (default onboarding tier: no catalog viewing or streaming) and `LISTENER` (approved read-only tier with catalog browsing and streaming rights, but no upload/mutation permissions). Added `UserRole.LISTENER = "LISTENER"` and `Permission.VIEW_CATALOG = "VIEW_CATALOG"`.
- **Audio Streaming Failure Diagnosis & FLAC Header Sanitization:** Diagnosed audio streaming failure on FLAC tracks: embedded `METADATA_BLOCK_PICTURE` blocks contained empty MIME strings (`mime_len == 0`), which causes Chromium and FFmpeg demuxers to abort decoding with `AVERROR_INVALIDDATA`. Repaired picture headers on disk and added automatic FLAC picture sanitization (`_sanitize_audio_pictures_sync`).
- **HTML5 `<audio>` Authenticated Range Streaming:** Added `?tgWebAppData=${encodeURIComponent(rawInit)}` query parameter support in `webapp/src/context/AudioPlayerContext.tsx` and query extraction in `extract_raw_init_data_from_request`.

### M22: Mini App User Governance, Role-Based Feature Locking, Deep-Linking & Offline Resilience
- **Mini App User Governance Endpoints:** `/api/v1/app/admin/users`, `/role`, `/block`, `/access-requests`, `/resolve` with strict RBAC enforcement.
- **Role-Based Feature Locking:** Guest users locked from uploading and mutating tags; non-admins completely hidden from admin/governance views.
- **Telegram Deep-Linking (`start_param`):** Deep links `track_{id}`, `job_{id}`, `queue`, `admin` directly navigating within the Mini App.
- **Offline Resilience:** App shell precaching and Service Worker cache strategies (`sw.js`).

### M21: Web Ingestion Hub, Multipart Audio/Archive Upload, Real-Time SSE Event Stream & Interactive Live Queue Board
- **Web Ingestion Hub:** `POST /api/v1/app/ingest/upload` multipart stream parser for single audio and `.zip`/`.7z` archives.
- **Queue Management & Actions:** `GET /api/v1/app/queue`, `POST /api/v1/app/queue/{id}/action` (pause, resume, cancel, select_candidate, manual_search).
- **Server-Sent Events (SSE):** `GET /api/v1/app/queue/events` real-time progress streaming via `QueueEventBus`.
- **Frontend Components:** `FileDropzone`, `StageVisualizer`, `DisambiguationCard`, `JobCard`, `IngestQueueView`.

### M20: Metadata, Cover & Synced Lyrics Studio with Mandatory Dry-Run Confirmation
- **Studio Endpoints:** `/propose-edit`, `/apply-edit`, `/cover`, `/lyrics`, `/auto-enrich`.
- **Interactive Studio UI:** `DryRunConfirmModal`, `EditMetadataModal`, `CoverUploaderModal`, `LrcEditorModal`, `SongStudioModal`.

### M19: Telegram Mini App Music Catalog, HTTP 206 Partial Content Audio Streaming, Interactive Player & Synced Karaoke Lyrics
- **Streaming & Catalog Endpoints:** `/tracks`, `/audio` (HTTP 206 Partial Content), `/cover`, `/lyrics`.
- **Player Frontend:** `AudioPlayerContext`, `MiniPlayer`, `FullscreenPlayer`, `SyncedLyrics` (tap-to-seek, auto-scroll), `QueueDrawer`, `CatalogView`.

### M18: Unified Web & API Server, Zero-Trust `initData` Cryptographic Validation, RBAC Bridge, Bot Menu Button & React/Vite Scaffold
- HMAC-SHA256 signature verification in `src/bot/auth.py`.
- Unified `BotWebServer` in `src/bot/webapp_server.py`.
- Scaffolded modern Telegram Mini App frontend in `webapp/` using React 18, Vite, TypeScript, `@telegram-apps/sdk-react`.

### M17: Guaranteed Read Permissions (`0o644` / `0o755`), Recursive Permission Repair, and Paginated/Sorted `/songs` Browser
- Fixed restrictive `0600` file modes; guaranteed read permissions on ingested audio tracks, `.lrc` sidecars, `cover.jpg` art, and extractions.
- Overhauled `/songs` interactive menu with 8-item-per-page pagination, sort ordering selection, and in-place navigation.

### M16: Robust Metadata Similarity Deduplication & Background Reconciliation Lockdown Mode
- Multi-factor metadata similarity deduplication in `SqliteCatalogRepository.check_duplicate`, comparing normalized primary singer, title variants, album similarity, and duration tolerances.
- Lockdown mode freezing song mutations and concurrent `/rescan` triggers while background filesystem synchronization is active.

### M15: Non-Blocking Remote Startup, Resilient Directory Scanning, Chunked Streaming Hashing & Batch SQLite Reconciliation
- Eliminated startup blocking; background library reconciliation loop.
- `os.walk(..., followlinks=False)` with directory `OSError` resilience.
- 64KB chunked streaming hashing and batch SQLite commits (`upsert_scanned_library_tracks_batch`).

### M14: Interactive Help Topics, Structured Queue Item Layout, Full-Pipeline Queued Job Tag Editor & Context-Aware Navigation
- Dedicated in-place Help topic screens (`help:ingestion`, `help:songs`, `help:queue`, `help:sso`, `help:status`, `help:admin`).
- Singular queue item controls and `queue:get_file:<job_id>`.
- Queued job tag editor (`verify:edit:<job_id>`).
- Per-message navigation stack tracking (`resolve_back_callback`).

### M13: Ordered Song Menu, Provenance & Edit History, Chronological User Activity, Interactive Job Pause/Unlock/Review & Audio Forwarding
- Song provenance & modification tracking (`updated_at`, `modified_by`, `modification_count`, `source_chat_id`, `source_message_id`).
- Chronological user upload and edit history views.
- Smart audio delivery with message forwarding and cached file ID reuse.

### M12: Telegram UI/UX Architecture Modernization (Single Living Screen, 3-Tier HTML Typography & `InlineKeyboardBuilder` Ergonomics)
- Single living screen in-place message mutation via `safe_edit_text`.
- 3-tier Telegram HTML typography (`<b>Header</b>`, native `<blockquote>`, `<code>` footers).
- `InlineKeyboardBuilder` / `ReplyKeyboardBuilder` with typed `CallbackData` schemas and native `CopyTextButton`.

### M11: GitHub Actions CI/CD, GHCR Container Registry Publishing & Local `act` Runner Support
- `.github/workflows/ci.yml` with `quality-and-test` and `docker-build-publish`.
- Local `act` CI execution support.

### M10: Library Folder Rescan & Database Reconciliation
- `LibraryOrganizer.scan_library_filesystem` and `reconcile_library_with_repository`.
- Background reconciliation loop and interactive `/rescan`.
- Layered YAML configuration (`config.yaml`) and `UMASK`/`PUID`/`PGID` support.

### M9: Interactive `/songs` Telegram Menu, Guided Tag/Cover/Lyrics Editor & Mandatory Dry-Run Confirmation
- Live search across library tracks -> Song Action Card.
- Mandatory Dry-Run & Confirmation Policy (`SongEditProposal`).
- Relocation, tag editing, cover art update, lyrics update.

### M8: Primary Singer & Voice-Actor (`CV`) Normalization, Multi-Provider English Enrichment, High-Confidence Auto-Approval, Always-On `.lrc`
- `normalize_primary_artist` and `extract_bilingual_english`.
- Genius and Last.fm metadata enrichment.
- Automatic approval for >= 98% confidence matches.
- Always-on `.lrc` sidecar and embedded lyrics.

### M7: Synced Lyrics Embedding, `cover.jpg` Extraction/CAA Fetching, Release `medium-list` Mapping, Live In-Place Progress, OIDC HTTP/Cron & RBAC Workflows
- Synced lyrics embedding across FLAC, MP3 (SYLT), and MP4 (`\xa9lyr`).
- Cover art extraction and Cover Art Archive fallback.
- Multi-track release tracklist mapping.
- In-place progress bars and Authentik OIDC sync loop.

### M6: Multi-Track Archive Batch Processing & End-to-End Telegram FSM Wiring
- Archive extraction (`.zip`, `.7z`), batch job processing.
- Verification router, release candidate disambiguation, and manual query search.

### M5: Live AcoustID & MusicBrainz Query Implementation
- AcoustID v2 REST lookups.
- `musicbrainzngs` recording/release queries with ISRC, Lucene escaping, and scoring.

### M4: Deep `mutagen` Tag Inspection & Writing
- Embedded tag inspection (`mutagen.File(..., easy=True)`).
- Enriched tag writing and `.lrc` sidecar generation.

### M3.5: Governance, 5-Tier RBAC, Authentik OIDC Synchronization, Ingestion Queue & `/admin` Dashboard
- SQLite schema with users, access requests, jobs, tracks, audit logs.
- 5-tier RBAC (`GUEST`, `UPLOADER`, `MODERATOR`, `ADMIN`, `OWNER`) and GovernanceService.
- Authentik OIDC state nonces, encrypted tokens at rest, group synchronization.
- Ingestion queue worker with concurrency limit (1..4).

### M3: Core Ingestion Pipeline Skeleton & SQLite WAL Persistence
- Core pipeline stages (staging, fingerprinting, deduplication, metadata, lyrics, library organization).
- `SqliteDatabaseManager` with WAL mode.

### M2: WSL Docker & Local Telegram Bot API Configuration
- `Dockerfile` with Python 3.14, `uv`, `libchromaprint-tools`, `7zip`, `ffmpeg`.
- `docker-compose.yml` supporting hybrid and full stack modes.
- Container-to-host path translation (`resolve_local_api_file_path`).

### M1: Modular Monolith `src/` Layout & Hexagonal Boundaries
- Isolated `src/core`, `src/db`, `src/bot`, `src/config` packages.
- Protocol ports in `src/core/ports.py`.
- AST boundary enforcement in `tests/test_architecture.py`.

