"""SQLite DDL schema definitions and idempotent column migrations."""

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'GUEST',
    is_blocked INTEGER NOT NULL DEFAULT 0,
    authentik_sub TEXT UNIQUE,
    authentik_username TEXT,
    authentik_groups TEXT,
    access_token_encrypted TEXT,
    refresh_token_encrypted TEXT,
    token_expires_at TIMESTAMP,
    last_oidc_sync TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_ACCESS_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
    status TEXT NOT NULL DEFAULT 'PENDING',
    reviewed_by INTEGER REFERENCES users(telegram_id),
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id TEXT PRIMARY KEY,
    id TEXT,
    user_id INTEGER,
    state TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    source_type TEXT NOT NULL DEFAULT 'DIRECT_AUDIO',
    source_path TEXT NOT NULL,
    original_filename TEXT NOT NULL DEFAULT '',
    file_size_bytes INTEGER NOT NULL DEFAULT 0,
    scratch_dir TEXT NOT NULL DEFAULT '',
    sha256_hash TEXT,
    confidence_score REAL,
    matched_release_mbid TEXT,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    selected_metadata_json TEXT,
    fingerprint_json TEXT,
    final_path TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
"""

CREATE_LIBRARY_TRACKS_TABLE = """
CREATE TABLE IF NOT EXISTS library_tracks (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    uploaded_by INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    track_number INTEGER,
    has_synced_lyrics INTEGER DEFAULT 0,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_TRACKS_TABLE = """
CREATE TABLE IF NOT EXISTS catalog_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    album_artist TEXT,
    year INTEGER,
    track_number INTEGER,
    disc_number INTEGER,
    duration_seconds INTEGER NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    recording_mbid TEXT,
    release_mbid TEXT,
    library_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

CREATE_AUDIT_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_entity TEXT,
    details_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_USERS_ROLE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
"""

CREATE_JOBS_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_jobs_status ON ingestion_jobs(status);
"""

CREATE_LIBRARY_TRACKS_FP_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tracks_fingerprint ON library_tracks(fingerprint);
"""

CREATE_AUDIT_CREATED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
"""

CREATE_TRACKS_MBID_INDEX = """
CREATE INDEX IF NOT EXISTS idx_catalog_tracks_recording_mbid
ON catalog_tracks (recording_mbid)
WHERE recording_mbid IS NOT NULL;
"""

TABLE_DDL_STATEMENTS: tuple[str, ...] = (
    CREATE_USERS_TABLE,
    CREATE_ACCESS_REQUESTS_TABLE,
    CREATE_JOBS_TABLE,
    CREATE_LIBRARY_TRACKS_TABLE,
    CREATE_TRACKS_TABLE,
    CREATE_AUDIT_LOGS_TABLE,
)

INDEX_DDL_STATEMENTS: tuple[str, ...] = (
    CREATE_USERS_ROLE_INDEX,
    CREATE_JOBS_STATUS_INDEX,
    CREATE_LIBRARY_TRACKS_FP_INDEX,
    CREATE_AUDIT_CREATED_INDEX,
    CREATE_TRACKS_MBID_INDEX,
)

ALL_SCHEMA_STATEMENTS: tuple[str, ...] = (
    *TABLE_DDL_STATEMENTS,
    *INDEX_DDL_STATEMENTS,
)

INGESTION_JOBS_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "TEXT"),
    ("user_id", "INTEGER"),
    ("status", "TEXT NOT NULL DEFAULT 'PENDING'"),
    ("source_type", "TEXT NOT NULL DEFAULT 'DIRECT_AUDIO'"),
    ("original_filename", "TEXT NOT NULL DEFAULT ''"),
    ("file_size_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("scratch_dir", "TEXT NOT NULL DEFAULT ''"),
    ("sha256_hash", "TEXT"),
    ("confidence_score", "REAL"),
    ("matched_release_mbid", "TEXT"),
    ("started_at", "TEXT"),
    ("completed_at", "TEXT"),
)
