"""MCP lifecycle manager — lazy start, idle shutdown, always-on.

Gap 7: Lifecycle Intelligence — don't run 100 MCPs at boot.
Start on demand, stop when idle, keep critical ones warm.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from slm_mcp_hub.core.constants import IDLE_SHUTDOWN_SECONDS

logger = logging.getLogger(__name__)


class LifecycleManager:
    """Manages MCP server lifecycle: lazy start, idle shutdown.

    Works with the federation connection pool. Does NOT own connections —
    the Hub does. This manager tracks timing and decides what to start/stop.
    """

    def __init__(
        self,
        idle_shutdown_seconds: int = IDLE_SHUTDOWN_SECONDS,
    ) -> None:
        self._idle_shutdown = idle_shutdown_seconds
        self._last_call: dict[str, float] = {}  # server_name → last call time
        self._always_on: set[str] = set()
        self._started: set[str] = set()

    def mark_always_on(self, server_name: str) -> None:
        """Mark a server as always-on (never idle-stopped)."""
        self._always_on.add(server_name)

    def is_always_on(self, server_name: str) -> bool:
        return server_name in self._always_on

    def is_started(self, server_name: str) -> bool:
        return server_name in self._started

    def record_start(self, server_name: str) -> None:
        """Record that a server has been started."""
        self._started.add(server_name)
        self._last_call[server_name] = time.time()

    def record_stop(self, server_name: str) -> None:
        """Record that a server has been stopped."""
        self._started.discard(server_name)
        self._last_call.pop(server_name, None)

    def record_call(self, server_name: str) -> None:
        """Record a tool call to a server (updates idle timer)."""
        self._last_call[server_name] = time.time()

    def needs_start(self, server_name: str) -> bool:
        """Check if a server needs to be started (not currently running)."""
        return server_name not in self._started

    def get_idle_servers(self) -> list[str]:
        """Get list of servers that are idle and should be stopped.

        Returns server names that:
        - Are currently started
        - Are NOT always_on
        - Have exceeded idle_shutdown_seconds since last call
        """
        now = time.time()
        idle = []
        for name in list(self._started):
            if name in self._always_on:
                continue
            last = self._last_call.get(name, 0)
            if (now - last) > self._idle_shutdown:
                idle.append(name)
        return idle

    def get_status(self) -> dict[str, Any]:
        """Return lifecycle status for all tracked servers."""
        now = time.time()
        servers = {}
        for name in self._started:
            last = self._last_call.get(name, 0)
            idle_secs = now - last if last > 0 else 0
            servers[name] = {
                "started": True,
                "always_on": name in self._always_on,
                "idle_seconds": round(idle_secs, 1),
                "will_shutdown_in": max(0, round(self._idle_shutdown - idle_secs, 1))
                if name not in self._always_on else None,
            }
        return {
            "started_count": len(self._started),
            "always_on_count": len(self._always_on),
            "idle_shutdown_seconds": self._idle_shutdown,
            "servers": servers,
        }
