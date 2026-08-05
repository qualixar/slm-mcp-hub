"""Server-status introspection — pure builder for the per-backend status list.

Extracted from ``ConnectionManager.get_server_status`` (W3-P2) to keep
``manager.py`` under the 800-line cap and to give the read-only status shape a
single, testable home.  This is a behaviour-preserving move: no locks, no
mutation, no awaits — it reads the manager's state maps and returns plain dicts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from slm_mcp_hub.federation.connection import ConnectionState

if TYPE_CHECKING:
    from slm_mcp_hub.core.config import MCPServerConfig
    from slm_mcp_hub.federation.connection import MCPConnection
    from slm_mcp_hub.resilience.supervisor import ConnectionSupervisor


def build_server_status(
    servers: Iterable[MCPServerConfig],
    connections: Mapping[str, MCPConnection],
    supervisors: Mapping[str, ConnectionSupervisor],
    evicted_caps: Mapping[str, dict[str, Any]],
    connect_times: Mapping[str, float],
    failed: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Build the status list for all configured servers.

    W1-P1: adds ``lifecycle`` field. W1-P2: adds supervisor health fields.
    W3-P1: evicted backends report connected=False / lifecycle="stopped" with
    their cached tool count; failed (non-evicted) backends report 0.
    """
    result: list[dict[str, Any]] = []
    for srv in servers:
        conn = connections.get(srv.name)
        supervisor = supervisors.get(srv.name)
        is_evicted = srv.name in evicted_caps
        auth_required = conn.is_auth_required if conn is not None else False
        # W3-P1 lifecycle: evicted-not-live → stopped; else use conn state.
        is_live = conn is not None and conn.is_connected
        if is_live:
            lifecycle_value: str = conn.state.value  # type: ignore[union-attr]
        elif is_evicted:
            lifecycle_value = ConnectionState.STOPPED.value
        elif conn is not None:
            lifecycle_value = conn.state.value
        else:
            lifecycle_value = ConnectionState.DISCONNECTED.value

        # W3-P1 tool count: live → live caps; evicted → cached; else 0.
        if is_live:
            tool_count = len(conn.capabilities.get("tools", []))  # type: ignore[union-attr]
        elif is_evicted:
            tool_count = len(evicted_caps[srv.name].get("tools", []))
        else:
            tool_count = 0

        sup = supervisor  # local alias for readability
        entry: dict[str, Any] = {
            "name": srv.name,
            "transport": srv.transport,
            "enabled": srv.enabled,
            "connected": is_live,
            "auth_required": auth_required,
            "tools": tool_count,
            "connect_time_ms": round(connect_times.get(srv.name, 0) * 1000),
            "lifecycle": lifecycle_value,
            "evicted": is_evicted and not is_live,  # W3-P1
            "consecutive_failures": sup.consecutive_failures if sup else 0,
            "needs_attention": sup.needs_attention if sup else False,
            "restart_count": sup.restart_count if sup else 0,
            "last_error": sup.last_error if sup else None,
            "breaker_open": sup.breaker_open if sup else False,
            "breaker_open_cycles": sup.breaker_open_cycles if sup else 0,
            "last_transition_ts": sup.last_transition_ts if sup else 0.0,
            # W5-P1: uptime in seconds; 0.0 when not connected or no MCPConnection.
            "uptime_seconds": round(conn.uptime_seconds if conn is not None else 0.0, 1),
        }
        if srv.name in failed:
            entry["error"] = failed[srv.name]
        if auth_required:
            # P07: guide the user to the login command (safe — no token material)
            entry["next_action"] = f"slm-hub auth login {srv.name}"
        result.append(entry)
    return result
