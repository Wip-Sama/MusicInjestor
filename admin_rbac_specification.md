# Technical Specification: Music Ingestion System — Governance, RBAC, OIDC & Ingestion Queue

## 1. Document Scope & Overview

This specification defines the security architecture, role-based access control (RBAC), Authentik (OIDC) identity synchronization, audit logging, and job queue orchestration for the **Automated Music Ingestion System**.

It serves as the authoritative blueprint for:
1. Gating access to the private music library.
2. Managing the lifecycle of users via both manual Telegram-native approvals and external federated SSO (Authentik).
3. Handling dynamic identity state changes (e.g., role adjustments or account deactivation in Authentik reflected back to Telegram).
4. Running a serialized, resource-conscious ingestion queue (default concurrency: `1`).
5. Directing operational observability and audit records to a dedicated Telegram Log Channel.

---

## 2. Identity, Authentication & Authentik OIDC Synchronization

The system employs a dual-authentication strategy:
* **Telegram Native:** Numeric `user_id` authenticated and authorized via local database records.
* **Authentik OIDC Federation (Optional SSO):** Seamless deep-linking that maps an Authentik identity to a Telegram account.

```
                              ┌─────────────────────────────┐
                              │    Authentik Identity IdP   │
                              │  (OAuth2 / OIDC Provider)   │
                              └──────────────┬──────────────┘
                                             │
                       Token Refresh / Claim Sync (Periodic / Webhook)
                                             │
                                             ▼
┌──────────────────┐               ┌───────────────────┐               ┌──────────────────┐
│ Telegram Client  │──(Deep Link)─►│ Ingestion Core    │◄─(Log Events)─│ Telegram Admin   │
│ (User Interface) │◄─(Live Edit)──│ (FastAPI/aiogram) │──────────────►│ Log Channel      │
└──────────────────┘               └─────────┬─────────┘               └──────────────────┘
                                             │
                                             ▼
                                   ┌───────────────────┐
                                   │ SQLite (WAL Mode) │
                                   │  - users          │
                                   │  - audit_logs     │
                                   │  - jobs_queue     │
                                   └───────────────────┘
```

### 2.1 Deep-Link Authentik Pairing Flow

1. An unauthorized or guest user triggers `/start` in the Telegram bot.
2. The engine generates a transient cryptographic state nonce stored in SQLite/in-memory with a 10-minute TTL:
   $$\text{State Nonce} = \text{HMAC-SHA256}(\text{telegram\_id} \parallel \text{timestamp}, \text{SECRET\_KEY})$$
3. The bot presents an inline button:
   `[ 🔐 Link via Authentik ]` $\to$ `https://auth.example.com/application/o/authorize/?client_id=...&response_type=code&scope=openid+profile+email+groups&state=<nonce>`
4. Upon browser login, Authentik redirects the browser to the backend's local redirect URI:
   `GET /api/v1/auth/callback?code=<code>&state=<nonce>`
5. The backend validates the `state` nonce, exchanges the authorization code for tokens (`access_token`, `refresh_token`, `id_token`), parses the user claims, and updates the local user profile.
6. The bot uses Telegram's `edit_message_text` to notify the user in-chat:
   *"✅ Account successfully paired with Authentik as **username** (Role: UPLOADER)."*

### 2.2 Dynamic OIDC State & Role Synchronization

Permissions in Authentik can change (e.g., an administrator revokes a user's `music-uploaders` group or disables their account). The engine maintains synchronization to avoid stale permissions:

1. **Token Persistence:**
   Store encrypted `refresh_token`, `token_expires_at`, and the latest `authentik_sub` (subject identifier) inside the `users` table.
2. **Sync Triggers:**
   * **On Ingestion Action:** Before any non-idempotent operation (e.g., starting an upload or processing a queue item), if the user is Authentik-linked and their last sync occurred $> 15\text{ minutes}$ prior, refresh the profile.
   * **Scheduled Re-check:** A background cron task (every 1 hour) re-evaluates all active Authentik-linked users via the `/userinfo` endpoint.
   * **Authentik Webhooks (Optional Ingress):** Provide a `POST /api/v1/webhooks/authentik` endpoint. When Authentik fires an event (e.g., `user_updated`, `user_deactivated`), the backend immediately synchronizes the specific account.
3. **De-synchronization & Demotion Logic:**
   * If an Authentik account is disabled or deleted: immediately set `is_blocked = 1` and reset the role to `GUEST`.
   * If group membership changes in Authentik:
     * Group `music-admins` $\implies$ Update role to `ADMIN`.
     * Group `music-moderators` $\implies$ Update role to `MODERATOR`.
     * Group `music-uploaders` $\implies$ Update role to `UPLOADER`.
     * No matching groups $\implies$ Demote to `GUEST` and revoke active queue submissions.
   * Every automated role shift writes an entry into `audit_logs` and posts an alert to the Admin Log Channel.

---

## 3. Role-Based Access Control (RBAC) Specification

Access control is enforced via five distinct tiers:

```
[ GUEST ] ──► [ UPLOADER ] ──► [ MODERATOR ] ──► [ ADMIN ] ──► [ OWNER ]
```

### 3.1 Role Hierarchy & Capabilities

| Permission / Action | GUEST | UPLOADER | MODERATOR | ADMIN | OWNER |
|---|:---:|:---:|:---:|:---:|:---:|
| Run `/start`, view help, check status | ✅ | ✅ | ✅ | ✅ | ✅ |
| Link Authentik SSO profile | ✅ | ✅ | ✅ | ✅ | ✅ |
| Submit access request to admins | ✅ | ❌ | ❌ | ❌ | ❌ |
| Submit files/archives for ingestion | ❌ | ✅ | ✅ | ✅ | ✅ |
| View own queued jobs & cancel them | ❌ | ✅ | ✅ | ✅ | ✅ |
| Resolve disambiguation on own uploads | ❌ | ✅ | ✅ | ✅ | ✅ |
| Request tag corrections on library tracks | ❌ | ✅ | ✅ | ✅ | ✅ |
| View global ingestion queue | ❌ | ❌ | ✅ | ✅ | ✅ |
| Resolve / Reject others' pending jobs | ❌ | ❌ | ✅ | ✅ | ✅ |
| Approve / Apply library tag corrections | ❌ | ❌ | ✅ | ✅ | ✅ |
| Manage users (Promote, Demote, Block) | ❌ | ❌ | ❌ | ✅ | ✅ |
| Review system audit logs & metrics | ❌ | ❌ | ❌ | ✅ | ✅ |
| Change queue concurrency limit | ❌ | ❌ | ❌ | ✅ | ✅ |
| Transfer ownership / System destructive wipe | ❌ | ❌ | ❌ | ❌ | ✅ |

### 3.2 Guest Onboarding & Manual Access Requests

When an unauthenticated user interacts with the bot:
1. All file uploads are discarded immediately without saving to disk.
2. The bot responds with the guest screen:
   ```
   🔒 Access Denied
   This is a private music ingestion gateway.
   
   Your Telegram ID: 987654321
   
   [ 🔐 Link via Authentik ]  [ 🙋 Request Access ]
   ```
3. Clicking **`[ 🙋 Request Access ]`**:
   * Registers a `PENDING` access request in the database.
   * Debounces subsequent clicks to prevent button spam.
   * Posts an interactive card directly to the **Admin Log Channel**:
     ```
     🔔 New Access Request
     • User: @john_doe (ID: 987654321)
     • Name: John Doe
     • Timestamp: 2026-10-11 17:30 UTC
     
     [ Approve: Uploader ] [ Approve: Moderator ] [ ❌ Deny ]
     ```
4. Clicking an approval button sets the role, updates the message in the channel with the approver's name, and notifies the user in their direct message:
   *"🎉 Your access request has been approved! You now have UPLOADER permissions."*

---

## 4. Operational Ingestion Queue & Concurrency Management

To prevent resource exhaustion (RAM/CPU spikes) on the Proxmox LXC container during heavy 7z/FLAC processing, jobs pass through a strict queuing system.

### 4.1 Concurrency & Execution Rules

1. **Default Concurrency:** Fixed at `1` (strict serialization). Only one archive is unpacked, fingerprinted, and tagged at a time.
2. **Configurable Runtime Limit:** Administrators can adjust concurrency via `/admin` between `1` and `4` depending on available host resources.
3. **Priority & Ordering:** First-In, First-Out (FIFO) queue order based on submission timestamp.
4. **State Persistence:** Queued and in-flight tasks are persisted in the `ingestion_jobs` SQLite table so that container restarts recover gracefully.

```
Incoming Upload ──► Write to Disk Scratch
                          │
                          ▼
                  Insert into DB (Status: QUEUED)
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                 Async Queue Worker Engine                   │
│                                                             │
│  asyncio.Semaphore(concurrency_limit = 1)                   │
│                                                             │
│  Job Acquired:                                              │
│  1. Unpack archive (7z/zip subprocess)                      │
│  2. Compute AcoustID / Chromaprint fingerprints             │
│  3. Query MusicBrainz & LRCLIB                              │
│  4. Duplicate Check (compare existing library hashes)       │
│                                                             │
│  High Confidence (≥90%)?                                    │
│   ├── YES ──► Tag with Mutagen, move files, mark COMPLETED  │
│   └── NO  ──► Status: AWAITING_INPUT (prompt uploader)      │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 In-Chat Queue Feedback

While a user waits for their turn in the pipeline, the bot provides dynamic updates:

```
⏳ Ingestion Queued
────────────────────────
📦 Payload: Led_Zeppelin_IV_FLAC.zip (380 MB)
📊 Position in Queue: #2
⚙️ Status: Waiting for active job (Discovery.7z) to finish...

[ ❌ Cancel Job ]
```

When processing begins:

```
⚙️ Ingesting: Led_Zeppelin_IV_FLAC.zip
────────────────────────
[████████░░] 80%
• Extraction: Complete (8 tracks)
• AcoustID: Matched (Led Zeppelin - Untitled / IV)
• Lyrics: 8/8 Synced found on LRCLIB

⚠️ Release Ambiguity:
Which version is this upload?

[ 1️⃣ 1971 Atlantic Standard (8 Trk) ]
[ 2️⃣ 2014 Remastered Deluxe (16 Trk) ]
[ ✏️ Manual Search ]  [ ❌ Abort ]
```

---

## 5. Centralized Admin Log Channel Architecture

All operational, security, and administrative events avoid direct-message noise and are dispatched to a dedicated **Private Telegram Channel** (`ADMIN_LOG_CHANNEL_ID`).

### 5.1 Event Schemas & Message Layouts

#### Event A: Access Granted / Role Modified
```
🛡 USER AUDIT: ROLE_UPDATED
• Target User: @jane_smith (ID: 11223344)
• Actor: @owner (ID: 10000001)
• Previous Role: GUEST
• New Role: UPLOADER
• Reason: Manual Approval via Button
• Timestamp: 2026-10-11 17:35:10 UTC
```

#### Event B: Authentik Sync & Shift
```
🔄 OIDC AUDIT: ROLE_SYNCHRONIZED
• Target User: @alex_dev (ID: 55443322)
• Authentik Subject: c849bf3c-419b-4bf1-a185-1d6ebfa77bb0
• Group Changes Detected: Removed from 'music-uploaders'
• New Assigned Role: GUEST (Access Revoked)
• Active Queued Jobs Cancelled: 1
• Timestamp: 2026-10-11 17:40:02 UTC
```

#### Event C: Successful Ingest Completion
```
✅ INGEST AUDIT: SUCCESS
• Job ID: eb42a981-d249-41a4-927b-28a110d7a04f
• Uploader: @jane_smith (ID: 11223344)
• Album: Pink Floyd - Wish You Were Here (1975)
• Tracks Written: 5 files (FLAC, 24-bit / 96kHz)
• Target Path: /music/Pink Floyd/Wish You Were Here (1975)/
• Lyrics: 5/5 embedded (LRCLIB synced)
• Processing Duration: 14.2 seconds
```

#### Event D: Duplicate Detected & Aborted
```
⚠️ INGEST AUDIT: DUPLICATE_REJECTED
• Job ID: 310fca44-110a-4c40-9a21-9988ffaa0011
• Uploader: @bob (ID: 99887766)
• Detected Track: Radiohead - Paranoid Android
• Conflict Path: /music/Radiohead/OK Computer/02 - Paranoid Android.flac
• Match Source: Exact AcoustID Fingerprint Hash Match
• Action Taken: Temporary files purged from scratch space
```

---

## 6. Database Schema Design (SQLite + WAL)

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 1. Users & Identity Mappings
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'GUEST',     -- GUEST, UPLOADER, MODERATOR, ADMIN, OWNER
    is_blocked INTEGER NOT NULL DEFAULT 0,  -- 0 = Active, 1 = Banned/Blocked
    
    -- Authentik OIDC Linking Fields
    authentik_sub TEXT UNIQUE,              -- UUID from Authentik
    authentik_username TEXT,
    authentik_groups TEXT,                  -- JSON string of group memberships
    access_token_encrypted TEXT,            -- Encrypted token for refresh flows
    refresh_token_encrypted TEXT,
    token_expires_at TIMESTAMP,
    last_oidc_sync TIMESTAMP,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Access Request Records
CREATE TABLE IF NOT EXISTS access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, APPROVED, REJECTED
    reviewed_by INTEGER REFERENCES users(telegram_id),
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Ingestion Jobs & Processing Queue
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id TEXT PRIMARY KEY,                   -- UUIDv4 string
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    status TEXT NOT NULL,                  -- QUEUED, PROCESSING, AWAITING_INPUT, COMPLETED, FAILED, CANCELLED
    source_type TEXT NOT NULL,             -- ARCHIVE_ZIP, ARCHIVE_7Z, DIRECT_AUDIO, URL
    original_filename TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    scratch_dir TEXT NOT NULL,             -- Path on disk during extraction
    sha256_hash TEXT,
    confidence_score REAL,
    matched_release_mbid TEXT,             -- MusicBrainz Release ID
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

-- 4. Ingested Library Tracks (For Auditing & Deduplication)
CREATE TABLE IF NOT EXISTS library_tracks (
    id TEXT PRIMARY KEY,                   -- UUIDv4 or MBID Recording ID
    job_id TEXT REFERENCES ingestion_jobs(id),
    uploaded_by INTEGER NOT NULL REFERENCES users(telegram_id),
    storage_path TEXT NOT NULL UNIQUE,     -- Path inside /mnt/music
    fingerprint TEXT NOT NULL,             -- Chromaprint raw hash
    duration_seconds REAL NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    track_number INTEGER,
    has_synced_lyrics INTEGER DEFAULT 0,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. Audit Log Records
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER NOT NULL REFERENCES users(telegram_id),
    action TEXT NOT NULL,                  -- ROLE_UPDATED, OIDC_SYNC, INGEST_SUCCESS, DUPLICATE_BLOCKED, etc.
    target_entity TEXT,                    -- user:12345, job:uuid-str, track:uuid-str
    details_json TEXT,                     -- Full structured context payload
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON ingestion_jobs(status);
CREATE INDEX IF NOT EXISTS idx_tracks_fingerprint ON library_tracks(fingerprint);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
```

---

## 7. Interactive Telegram UI State Machines

### 7.1 Admin Control Dashboard (`/admin`)

Administrators interact with a single auto-updating message panel:

```
┌────────────────────────────────────────────────────────┐
│ 🎛 Administrator Control Panel                         │
│ Node: Proxmox LXC 104 | Ingestion Core Engine          │
│ Health: 🟢 OK | Memory: 84 MB / 1024 MB                │
│ Concurrency Limit: 1 (Serial)                          │
├────────────────────────────────────────────────────────┤
│ 👥 Users: 12 Active | 1 Pending                        │
│ ⏳ Queue: 0 Active | 1 Queued                          │
├────────────────────────────────────────────────────────┤
│ [ 👥 User Management ]     [ ⏳ View Queue (1) ]       │
│ [ 🔄 Run Authentik Sync ]  [ ⚙️ Concurrency: [1] ]     │
│ [ 📜 Audit Logs ]          [ ❌ Close Panel ]          │
└────────────────────────────────────────────────────────┘
```

#### User Detail Management Sub-Screen:
```
┌────────────────────────────────────────────────────────┐
│ 👤 Managing User: @john_doe                            │
│ ID: 987654321                                          │
│ Current Role: UPLOADER                                 │
│ Authentik Status: Linked (john.doe@example.com)        │
│ Total Uploads: 14 albums (182 tracks)                  │
├────────────────────────────────────────────────────────┤
│ [ ⬆️ Promote to Moderator ] [ ⬇️ Demote to Guest ]    │
│ [ 🚫 Block / Ban User ]     [ 🔗 Force OIDC Re-sync ]  │
│ [ 🔙 Back to User List ]                               │
└────────────────────────────────────────────────────────┘
```

---

## 8. Implementation Phases & Milestones

1. **Phase 1: Persistence & RBAC Foundation**
   * Implement SQLite schema with WAL mode and schema migration scripts.
   * Build `aiogram 3.x` middleware to enforce role permissions before handler execution.
   * Implement the `/start` guest interceptor and the Admin Log Channel notifications.

2. **Phase 2: Authentik OIDC Integration & Synchronization**
   * Add lightweight FastAPI callback endpoints for OAuth2 code exchange.
   * Implement token storage, group claim evaluation, and periodic/webhook-triggered role sync.

3. **Phase 3: Serialized Ingestion Queue**
   * Build the SQLite-backed task queue with `asyncio.Semaphore(1)`.
   * Implement live progress updates in Telegram via message editing.
   * Wire Chromaprint fingerprint matching against the `library_tracks` index for duplicate rejection.

4. **Phase 4: Admin Dashboard & Observability**
   * Build the `/admin` interactive keyboard menu and user management flows.
   * Route formatted audit events directly to the private Admin Log Channel.