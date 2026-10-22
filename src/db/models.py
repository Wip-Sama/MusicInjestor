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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    oidc_role_override INTEGER NOT NULL DEFAULT 0
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
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,
    modified_by INTEGER,
    modification_count INTEGER NOT NULL DEFAULT 0,
    telegram_file_id TEXT,
    source_chat_id INTEGER,
    source_message_id INTEGER
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

CREATE_TAG_CORRECTION_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS tag_correction_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    requested_by INTEGER NOT NULL REFERENCES users(telegram_id),
    proposed_title TEXT,
    proposed_artist TEXT,
    proposed_album TEXT,
    proposed_year INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    reviewed_by INTEGER REFERENCES users(telegram_id),
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_USER_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS user_settings (
    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    stream_format TEXT NOT NULL DEFAULT 'auto',
    stream_bitrate INTEGER NOT NULL DEFAULT 128,
    auto_lyrics_search INTEGER NOT NULL DEFAULT 1,
    theme TEXT NOT NULL DEFAULT 'system',
    streaming_platform_order TEXT NOT NULL DEFAULT 'deezer,tidal,qobuz,spotify,apple_music',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_ARTIST_FOLLOWS_TABLE = """
CREATE TABLE IF NOT EXISTS artist_follows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
    artist_name TEXT NOT NULL,
    provider_artist_id TEXT,
    notify_telegram INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_DISCOVERED_RELEASES_TABLE = """
CREATE TABLE IF NOT EXISTS discovered_releases (
    id TEXT PRIMARY KEY,
    artist_name TEXT NOT NULL,
    title TEXT NOT NULL,
    release_type TEXT NOT NULL DEFAULT 'album',
    release_date TEXT NOT NULL DEFAULT '',
    cover_url TEXT,
    preview_url TEXT,
    external_url TEXT,
    provider TEXT NOT NULL DEFAULT 'deezer',
    provider_release_id TEXT NOT NULL DEFAULT '',
    track_count INTEGER NOT NULL DEFAULT 1,
    in_library INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_RELEASE_NOTIFICATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS release_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
    release_id TEXT NOT NULL REFERENCES discovered_releases(id),
    is_read INTEGER NOT NULL DEFAULT 0,
    delivered_at TIMESTAMP,
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

CREATE_ARTIST_FOLLOWS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_artist_follows_user_artist
ON artist_follows(telegram_id, LOWER(artist_name));
"""

CREATE_DISCOVERED_RELEASES_ARTIST_INDEX = """
CREATE INDEX IF NOT EXISTS idx_discovered_releases_artist
ON discovered_releases(artist_name);
"""

CREATE_DISCOVERED_RELEASES_DATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_discovered_releases_date
ON discovered_releases(release_date DESC);
"""

CREATE_RELEASE_NOTIFICATIONS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_release_notifications_user_release
ON release_notifications(telegram_id, release_id);
"""

TABLE_DDL_STATEMENTS: tuple[str, ...] = (
    CREATE_USERS_TABLE,
    CREATE_ACCESS_REQUESTS_TABLE,
    CREATE_JOBS_TABLE,
    CREATE_LIBRARY_TRACKS_TABLE,
    CREATE_TRACKS_TABLE,
    CREATE_AUDIT_LOGS_TABLE,
    CREATE_TAG_CORRECTION_REQUESTS_TABLE,
    CREATE_USER_SETTINGS_TABLE,
    CREATE_ARTIST_FOLLOWS_TABLE,
    CREATE_DISCOVERED_RELEASES_TABLE,
    CREATE_RELEASE_NOTIFICATIONS_TABLE,
)

INDEX_DDL_STATEMENTS: tuple[str, ...] = (
    CREATE_USERS_ROLE_INDEX,
    CREATE_JOBS_STATUS_INDEX,
    CREATE_LIBRARY_TRACKS_FP_INDEX,
    CREATE_AUDIT_CREATED_INDEX,
    CREATE_TRACKS_MBID_INDEX,
    CREATE_ARTIST_FOLLOWS_INDEX,
    CREATE_DISCOVERED_RELEASES_ARTIST_INDEX,
    CREATE_DISCOVERED_RELEASES_DATE_INDEX,
    CREATE_RELEASE_NOTIFICATIONS_INDEX,
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

LIBRARY_TRACKS_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("updated_at", "TIMESTAMP"),
    ("modified_by", "INTEGER"),
    ("modification_count", "INTEGER NOT NULL DEFAULT 0"),
    ("telegram_file_id", "TEXT"),
    ("source_chat_id", "INTEGER"),
    ("source_message_id", "INTEGER"),
)

USERS_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (("oidc_role_override", "INTEGER NOT NULL DEFAULT 0"),)

USER_SETTINGS_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("streaming_platform_order", "TEXT NOT NULL DEFAULT 'deezer,tidal,qobuz,spotify,apple_music'"),
)
