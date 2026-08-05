"""W5-P1 — Status enrichment: uptime, P95 latency, RAM.

Adds three new fields to each entry returned by `build_server_status`:
  uptime_seconds  float  — seconds since last successful connect (0.0 when not connected)
  p95_latency_ms  float  — P95 call duration from MetricsCollector (0.0 when no metrics)
  ram_bytes       int | None — RSS bytes of subprocess; None for HTTP backends
                               or when psutil is unavailable

Design contracts:
- IMMUTABLE: returns a NEW list of NEW dicts; input is NEVER mutated.
- NEVER RAISES: all internal errors are caught and logged; degraded values used.
  enrich_server_status() is safe to call in a hot path or from a route handler.
- psutil is OPTIONAL: ImportError → ram_bytes = None (graceful degradation).
- HTTP/SSE backends have no subprocess PID → ram_bytes = None (by design).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from slm_mcp_hub.federation.connection import MCPConnection
    from slm_mcp_hub.observability.metrics import MetricsCollector

logger = logging.getLogger(__name__)


def _get_ram_bytes(conn: "MCPConnection | None") -> int | None:
    """Return RSS bytes for the connection's subprocess (stdio only).

    Returns None for HTTP/SSE backends, disconnected backends, or when psutil
    is not installed. Never raises — all exceptions are caught internally.

    Args:
        conn: An MCPConnection or None.

    Returns:
        Integer RSS in bytes, or None when unavailable.
    """
    if conn is None or not conn.is_connected:
        return None
    try:
        pid = conn.process_pid  # property added to MCPConnection in W5-P1
    except Exception:
        return None
    if pid is None:
        return None
    try:
        import psutil  # type: ignore[import-untyped]  # optional; graceful degradation on ImportError
        return psutil.Process(pid).memory_info().rss
    except ImportError:
        return None  # psutil not installed — graceful degradation, no warn needed
    except Exception:
        # psutil.NoSuchProcess, psutil.AccessDenied, or any unexpected exception
        logger.debug("psutil RAM read failed for pid=%s — returning None", pid)
        return None


def _get_uptime(conn: "MCPConnection | None") -> float:
    """Return uptime_seconds from the connection, defaulting to 0.0 on any error.

    Never raises. Returns 0.0 when not connected or on any exception.
    """
    if conn is None:
        return 0.0
    try:
        uptime = conn.uptime_seconds
        return max(0.0, float(uptime))  # never negative
    except Exception:
        logger.debug("uptime_seconds read failed — returning 0.0")
        return 0.0


def _get_p95(name: str, metrics: "MetricsCollector | None") -> float:
    """Return P95 latency in ms from metrics, defaulting to 0.0 on any error.

    Never raises. Returns 0.0 when metrics is None or on any exception.
    """
    if metrics is None:
        return 0.0
    try:
        server_metrics = metrics.get_server_metrics(name)
        return float(server_metrics.get("p95_duration_ms", 0.0))
    except Exception:
        logger.debug("p95 latency read failed for %s — returning 0.0", name)
        return 0.0


def enrich_server_status(
    status_entries: list[dict[str, Any]],
    connections: "dict[str, MCPConnection]",
    metrics: "MetricsCollector | None" = None,
) -> list[dict[str, Any]]:
    """Add uptime_seconds, p95_latency_ms, ram_bytes to each status entry.

    Immutable pattern: returns a NEW list of NEW dicts (does not mutate inputs).
    Never-raises contract: all internal errors are caught and logged; the returned
    list always has valid enriched entries (degraded to 0.0/None on error).

    Args:
        status_entries: Output of build_server_status() — list of backend dicts.
        connections: MCPConnection map (e.g. from ConnectionManager._connections).
        metrics: Optional MetricsCollector; if None, p95_latency_ms is 0.0.

    Returns:
        New list with three new fields added to each entry dict:
          uptime_seconds  (float, >= 0.0)
          p95_latency_ms  (float, >= 0.0)
          ram_bytes       (int or None)
    """
    enriched: list[dict[str, Any]] = []
    for entry in status_entries:
        name: str = entry.get("name", "")
        conn = connections.get(name) if name else None

        uptime = _get_uptime(conn)
        p95 = _get_p95(name, metrics)
        ram = _get_ram_bytes(conn)

        # Immutable: create a NEW dict — never mutate the input entry.
        new_entry: dict[str, Any] = {
            **entry,
            "uptime_seconds": round(uptime, 1),
            "p95_latency_ms": round(p95, 1),
            "ram_bytes": ram,
        }
        enriched.append(new_entry)
    return enriched
