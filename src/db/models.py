"""SQLite DDL schema definitions for track fingerprints and ingestion jobs."""

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

CREATE_TRACKS_MBID_INDEX = """
CREATE INDEX IF NOT EXISTS idx_catalog_tracks_recording_mbid
ON catalog_tracks (recording_mbid)
WHERE recording_mbid IS NOT NULL;
"""

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    state TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    selected_metadata_json TEXT,
    fingerprint_json TEXT,
    final_path TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);
"""

ALL_SCHEMA_STATEMENTS: tuple[str, ...] = (
    CREATE_TRACKS_TABLE,
    CREATE_TRACKS_MBID_INDEX,
    CREATE_JOBS_TABLE,
)
