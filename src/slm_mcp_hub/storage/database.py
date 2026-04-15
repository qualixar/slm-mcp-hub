"""SQLite storage manager for SLM MCP Hub."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from slm_mcp_hub.core.constants import DATABASE_FILE, DATABASE_WAL_MODE
from slm_mcp_hub.storage.schema import MIGRATIONS, SCHEMA_VERSION

logger = logging.getLogger(__name__)


class HubDatabase:
    """Synchronous SQLite storage manager with WAL mode and migrations.

    Designed for multi-thread access: WAL mode allows concurrent reads.
    Writes are serialized by SQLite's internal locking.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DATABASE_FILE
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """Open database connection, apply migrations."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        if DATABASE_WAL_MODE:
            self._conn.execute("PRAGMA journal_mode=WAL")

        self._conn.execute("PRAGMA foreign_keys=ON")
        self._apply_migrations()
        logger.info("Database opened at %s (schema v%d)", self._db_path, SCHEMA_VERSION)

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("Database closed")

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not opened. Call open() first.")
        return self._conn

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute a SQL statement."""
        return self.connection.execute(sql, params)

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]]) -> sqlite3.Cursor:
        """Execute a SQL statement with multiple parameter sets."""
        return self.connection.executemany(sql, params_seq)

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """Execute query and return first row."""
        cursor = self.execute(sql, params)
        return cursor.fetchone()

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Execute query and return all rows."""
        cursor = self.execute(sql, params)
        return cursor.fetchall()

    def insert(self, table: str, data: dict[str, Any]) -> int:
        """Insert a row and return lastrowid."""
        ALLOWED_TABLES = frozenset({
            "hub_config", "mcp_servers", "sessions", "tool_calls",
            "cache", "cost_budgets", "audit_log", "metrics", "schema_version",
        })
        if table not in ALLOWED_TABLES:
            raise ValueError(f"Invalid table name: {table}")
        import re as _re
        for col in data.keys():
            if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col):
                raise ValueError(f"Invalid column name: {col}")
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
        cursor = self.execute(sql, tuple(data.values()))
        self.connection.commit()
        return cursor.lastrowid or 0

    def commit(self) -> None:
        """Commit current transaction."""
        self.connection.commit()

    def _get_current_schema_version(self) -> int:
        """Get the current schema version from database."""
        try:
            row = self.fetch_one(
                "SELECT MAX(version) as v FROM schema_version"
            )
            return row["v"] if row and row["v"] is not None else 0
        except sqlite3.OperationalError:
            return 0

    def _apply_migrations(self) -> None:
        """Apply any pending schema migrations."""
        current = self._get_current_schema_version()

        for version in sorted(MIGRATIONS.keys()):
            if version <= current:
                continue

            logger.info("Applying migration v%d", version)
            for sql in MIGRATIONS[version]:
                self.execute(sql)

            self.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )
            self.commit()
            logger.info("Migration v%d applied", version)

    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists."""
        row = self.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return row is not None
