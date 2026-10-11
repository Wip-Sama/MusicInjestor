"""Async SQLite connection and schema manager configured with Write-Ahead Logging (WAL)."""

import logging
from pathlib import Path

import aiosqlite

from src.db.models import (
    INDEX_DDL_STATEMENTS,
    INGESTION_JOBS_MIGRATION_COLUMNS,
    TABLE_DDL_STATEMENTS,
)

logger = logging.getLogger(__name__)


async def _migrate_ingestion_jobs_table(conn: aiosqlite.Connection) -> None:
    """Add any missing M3.5 governance/queue columns to an existing `ingestion_jobs` table."""
    cursor = await conn.execute("PRAGMA table_info(ingestion_jobs);")
    rows = await cursor.fetchall()
    existing_columns = {str(row[1]) for row in rows}

    added_columns: list[str] = []
    for col_name, col_ddl in INGESTION_JOBS_MIGRATION_COLUMNS:
        if col_name not in existing_columns:
            await conn.execute(f"ALTER TABLE ingestion_jobs ADD COLUMN {col_name} {col_ddl};")
            added_columns.append(col_name)

    if added_columns:
        await conn.execute("UPDATE ingestion_jobs SET id = job_id WHERE id IS NULL;")
        await conn.execute("UPDATE ingestion_jobs SET status = state WHERE status = 'PENDING';")
        logger.info("Migrated legacy ingestion_jobs table; added columns: %s", ", ".join(added_columns))


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
            await _migrate_ingestion_jobs_table(conn)
            for statement in INDEX_DDL_STATEMENTS:
                await conn.execute(statement)
            await conn.commit()
        logger.info("Initialized SQLite database (WAL mode) at %s", self._database_path)

    def connect(self) -> aiosqlite.Connection:
        """Return an async context-managed `aiosqlite.Connection`."""
        return aiosqlite.connect(self._database_path)
