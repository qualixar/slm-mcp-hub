"""Constants for SLM MCP Hub."""

from __future__ import annotations

import os
from pathlib import Path

# Version
VERSION = "0.3.2"


def get_config_dir() -> Path:
    """Resolve the active config directory at the point of use."""
    return Path(os.environ.get("SLM_HUB_CONFIG_DIR", Path.home() / ".slm-mcp-hub"))


def get_config_file(config_dir: Path | None = None) -> Path:
    return (config_dir or get_config_dir()) / "config.json"


def get_database_file(config_dir: Path | None = None) -> Path:
    return (config_dir or get_config_dir()) / "hub.db"


def get_pid_file(config_dir: Path | None = None) -> Path:
    return (config_dir or get_config_dir()) / "hub.pid"


def get_log_file(config_dir: Path | None = None) -> Path:
    return (config_dir or get_config_dir()) / "hub.log"


def get_permissions_file(config_dir: Path | None = None) -> Path:
    return (config_dir or get_config_dir()) / "permissions.json"


def get_fallback_config_file(config_dir: Path | None = None) -> Path:
    return (config_dir or get_config_dir()) / "fallback-config.json"


def get_snapshots_dir(config_dir: Path | None = None) -> Path:
    return (config_dir or get_config_dir()) / "snapshots"

# Network
DEFAULT_PORT = 52414
DEFAULT_HOST = "127.0.0.1"

# Federation
NAMESPACE_DELIMITER = "__"

# Database
DATABASE_WAL_MODE = True

# Session
SESSION_TIMEOUT_SECONDS = 3600  # 1 hour
MAX_SESSIONS = 50

# Cache
CACHE_DEFAULT_TTL_SECONDS = 300  # 5 minutes
CACHE_MAX_ENTRIES = 1000

# Lifecycle
IDLE_SHUTDOWN_SECONDS = 1800  # 30 minutes
MCP_REQUEST_TIMEOUT_MS = 3_600_000  # 60 minutes (video gen, deep research, long-running AI tasks)
DEFAULT_TOOL_TIMEOUT_S = 120  # 2 minutes default for tool calls (overridable per-server)

# Resilience
REQUEST_BUFFER_MAX = 100
REQUEST_BUFFER_TIMEOUT_SECONDS = 30
HEALTH_CHECK_INTERVAL_SECONDS = 30

# Observability
TRACE_RING_BUFFER_SIZE = 1000
METRICS_WINDOWS = ("1h", "24h", "7d")

# Audit
AUDIT_RETENTION_DAYS = 30

# MCP endpoint path
MCP_ENDPOINT_PATH = "/mcp"
API_PREFIX = "/api"

# W4: timeout class names (mirrored in federation/timeouts.py for the registry)
TIMEOUT_CLASS_FAST = "fast"
TIMEOUT_CLASS_DEFAULT = "default"
TIMEOUT_CLASS_EXTENDED = "extended"
TIMEOUT_CLASS_UNBOUNDED = "unbounded"

# W4: event store defaults (used by InMemoryEventStore / P3)
EVENT_STORE_MAX_EVENTS = 500
EVENT_STORE_MAX_STREAMS = 200
EVENT_STORE_STREAM_TTL_S = 7200.0  # 2 hours — covers UNBOUNDED class calls

# W4: keepalive default (EXTENDED + UNBOUNDED classes)
KEEPALIVE_INTERVAL_S = 55.0

# MCP protocol versions — single-source definition (W8-P6).
# Referenced by http_server.py and protocol/product_operations.py.
MCP_MODERN_PROTOCOL_VERSION = "2026-07-28"
MCP_LEGACY_PROTOCOL_VERSIONS = frozenset({
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
})
