"""Async SQLite connection and schema manager configured with Write-Ahead Logging (WAL)."""

import logging
from pathlib import Path

import aiosqlite

from src.db.models import ALL_SCHEMA_STATEMENTS

logger = logging.getLogger(__name__)


class SqliteDatabaseManager:
    """Manages SQLite connections in WAL mode and initializes schema tables."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @property
    def database_path(self) -> Path:
        """Return the configured path to the SQLite database file."""
        return self._database_path

    async def initialize(self) -> None:
        """Ensure parent directory exists, enable WAL mode, and apply DDL schema."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._database_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            for statement in ALL_SCHEMA_STATEMENTS:
                await conn.execute(statement)
            await conn.commit()
        logger.info("Initialized SQLite database (WAL mode) at %s", self._database_path)

    def connect(self) -> aiosqlite.Connection:
        """Return an async context-managed `aiosqlite.Connection`."""
        return aiosqlite.connect(self._database_path)
