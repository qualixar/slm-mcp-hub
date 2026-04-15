"""Database schema definitions for SLM MCP Hub."""

from __future__ import annotations

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, list[str]] = {
    1: [
        # Hub configuration key-value store
        """CREATE TABLE IF NOT EXISTS hub_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )""",

        # MCP server definitions and status
        """CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT PRIMARY KEY,
            config TEXT NOT NULL,
            transport TEXT NOT NULL DEFAULT 'stdio',
            status TEXT NOT NULL DEFAULT 'disconnected',
            last_connected REAL,
            enabled INTEGER NOT NULL DEFAULT 1
        )""",

        # Active client sessions
        """CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            client_name TEXT NOT NULL DEFAULT 'unknown',
            connected_at REAL NOT NULL,
            last_activity REAL NOT NULL,
            permissions TEXT NOT NULL DEFAULT '{}',
            project_path TEXT NOT NULL DEFAULT ''
        )""",

        # Tool call history (for learning, cost tracking, observability)
        """CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            server_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_hash TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 1,
            cost_cents REAL NOT NULL DEFAULT 0.0,
            cached INTEGER NOT NULL DEFAULT 0,
            timestamp REAL NOT NULL
        )""",

        # Intelligent cache
        """CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            result TEXT NOT NULL,
            created_at REAL NOT NULL,
            ttl_seconds INTEGER NOT NULL DEFAULT 300,
            hit_count INTEGER NOT NULL DEFAULT 0
        )""",

        # Cost budgets
        """CREATE TABLE IF NOT EXISTS cost_budgets (
            scope TEXT PRIMARY KEY,
            budget_cents REAL NOT NULL DEFAULT 0.0,
            spent_cents REAL NOT NULL DEFAULT 0.0,
            period_start REAL NOT NULL
        )""",

        # Audit log
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            timestamp REAL NOT NULL
        )""",

        # Performance metrics (aggregated)
        """CREATE TABLE IF NOT EXISTS metrics (
            server_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL DEFAULT 0.0,
            window TEXT NOT NULL DEFAULT '1h',
            updated_at REAL NOT NULL,
            PRIMARY KEY (server_name, metric, window)
        )""",

        # Schema version tracking
        """CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        )""",

        # Indexes for common queries
        "CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_tool_calls_server ON tool_calls(server_name)",
        "CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp ON tool_calls(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_session ON audit_log(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created_at)",
    ],
}
