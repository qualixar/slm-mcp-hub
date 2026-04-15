"""Constants for SLM MCP Hub."""

from __future__ import annotations

import os
from pathlib import Path

# Version
VERSION = "0.1.2"

# Network
DEFAULT_PORT = 52414
DEFAULT_HOST = "127.0.0.1"

# Paths
CONFIG_DIR = Path(os.environ.get("SLM_HUB_CONFIG_DIR", Path.home() / ".slm-mcp-hub"))
CONFIG_FILE = CONFIG_DIR / "config.json"
DATABASE_FILE = CONFIG_DIR / "hub.db"
PID_FILE = CONFIG_DIR / "hub.pid"
LOG_FILE = CONFIG_DIR / "hub.log"
PERMISSIONS_FILE = CONFIG_DIR / "permissions.json"
FALLBACK_CONFIG_FILE = CONFIG_DIR / "fallback-config.json"

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
MCP_REQUEST_TIMEOUT_MS = 1_800_000  # 30 minutes (Gemini deep-research takes 5-20 min)

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
