"""Async SQLite connection and schema manager configured with Write-Ahead Logging (WAL)."""

import logging
from pathlib import Path

import aiosqlite

from src.db.models import (
    DISCOVERED_RELEASES_MIGRATION_COLUMNS,
    INDEX_DDL_STATEMENTS,
    INGESTION_JOBS_MIGRATION_COLUMNS,
    LIBRARY_TRACKS_MIGRATION_COLUMNS,
    PROVIDER_SERVERS_MIGRATION_COLUMNS,
    SONG_REQUESTS_MIGRATION_COLUMNS,
    TABLE_DDL_STATEMENTS,
    USER_SETTINGS_MIGRATION_COLUMNS,
    USERS_MIGRATION_COLUMNS,
)

logger = logging.getLogger(__name__)


async def _migrate_table_columns(
    conn: aiosqlite.Connection,
    table_name: str,
    columns: tuple[tuple[str, str], ...],
) -> list[str]:
    """Add any missing columns declared in `columns` to `table_name` and return added names."""
    cursor = await conn.execute(f"PRAGMA table_info({table_name});")
    rows = await cursor.fetchall()
    existing_columns = {str(row[1]) for row in rows}

    added_columns: list[str] = []
    for col_name, col_ddl in columns:
        if col_name not in existing_columns:
            await conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_ddl};")
            added_columns.append(col_name)

    if added_columns:
        logger.info("Migrated %s table; added columns: %s", table_name, ", ".join(added_columns))
    return added_columns


async def _migrate_ingestion_jobs_table(conn: aiosqlite.Connection) -> None:
    """Add any missing M3.5 governance/queue columns to an existing `ingestion_jobs` table."""
    added = await _migrate_table_columns(conn, "ingestion_jobs", INGESTION_JOBS_MIGRATION_COLUMNS)
    if added:
        await conn.execute("UPDATE ingestion_jobs SET id = job_id WHERE id IS NULL;")
        await conn.execute("UPDATE ingestion_jobs SET status = state WHERE status = 'PENDING';")


async def _migrate_library_tracks_table(conn: aiosqlite.Connection) -> None:
    """Add missing provenance/modification/cache columns to `library_tracks` and backfill `uploaded_by`."""
    await _migrate_table_columns(conn, "library_tracks", LIBRARY_TRACKS_MIGRATION_COLUMNS)
    await conn.execute(
        """
        UPDATE library_tracks
        SET uploaded_by = COALESCE(
            (SELECT j.user_id FROM ingestion_jobs j
             WHERE (j.job_id = library_tracks.job_id OR j.final_path = library_tracks.storage_path)
               AND j.user_id IS NOT NULL AND j.user_id > 0
             LIMIT 1),
            0
        )
        WHERE uploaded_by = 0
        """
    )


class SqliteDatabaseManager:
    """Manages SQLite connections in WAL mode and initializes/migrates schema tables."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @property
    def database_path(self) -> Path:
        """Return the configured path to the SQLite database file."""
        return self._database_path

    async def initialize(self) -> None:
        """Ensure parent directory exists, enable WAL mode, apply table DDL, migrate columns, and index."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._database_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            for statement in TABLE_DDL_STATEMENTS:
                await conn.execute(statement)
            await _migrate_table_columns(conn, "users", USERS_MIGRATION_COLUMNS)
            await _migrate_table_columns(conn, "user_settings", USER_SETTINGS_MIGRATION_COLUMNS)
            await _migrate_table_columns(conn, "discovered_releases", DISCOVERED_RELEASES_MIGRATION_COLUMNS)
            await _migrate_table_columns(conn, "song_requests", SONG_REQUESTS_MIGRATION_COLUMNS)
            await _migrate_table_columns(conn, "provider_servers", PROVIDER_SERVERS_MIGRATION_COLUMNS)
            await _migrate_ingestion_jobs_table(conn)
            await _migrate_library_tracks_table(conn)
            for statement in INDEX_DDL_STATEMENTS:
                await conn.execute(statement)
            await conn.commit()
        logger.info("Initialized SQLite database (WAL mode) at %s", self._database_path)

    def connect(self) -> aiosqlite.Connection:
        """Return an async context-managed `aiosqlite.Connection`."""
        return aiosqlite.connect(self._database_path)
