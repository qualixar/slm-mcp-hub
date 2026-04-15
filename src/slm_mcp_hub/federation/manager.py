"""Connection manager — spawns, monitors, and syncs all MCP server connections."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import MCPConnection

logger = logging.getLogger(__name__)

# Retry config
_INITIAL_RETRY_DELAY_S = 5.0
_MAX_RETRY_DELAY_S = 120.0
_MAX_RETRY_ATTEMPTS = 5


class ConnectionManager:
    """Manages the lifecycle of all MCP server connections.

    Features:
    - Prioritized startup: stdio first (fast), then HTTP (slower)
    - Progressive registry sync: tools available as each server connects
    - Dynamic speed tracking: records connection time per server
    - Background retry: failed servers retry with exponential backoff
    - Manual reconnect: reconnect any server on demand
    """

    def __init__(
        self,
        config: HubConfig,
        registry: CapabilityRegistry,
    ) -> None:
        self._config = config
        self._registry = registry
        self._connections: dict[str, MCPConnection] = {}
        self._failed: dict[str, str] = {}
        self._connect_times: dict[str, float] = {}
        self._retry_task: asyncio.Task | None = None
        self._shutdown = False

    @property
    def connections(self) -> dict[str, MCPConnection]:
        return self._connections

    @property
    def connected_count(self) -> int:
        return sum(1 for c in self._connections.values() if c.is_connected)

    @property
    def failed_servers(self) -> dict[str, str]:
        return dict(self._failed)

    @property
    def connect_times(self) -> dict[str, float]:
        return dict(self._connect_times)

    async def connect_all(self) -> dict[str, str]:
        """Connect to all enabled MCP servers with prioritized ordering.

        Stdio servers connect first (local, fast), then HTTP servers
        (network, potentially slower). Each server's tools become available
        immediately after it connects. Failed servers are retried in background.

        Returns dict of {server_name: error_message} for initially failed servers.
        """
        enabled = [s for s in self._config.mcp_servers if s.enabled]

        if not enabled:
            logger.info("No MCP servers configured")
            return {}

        stdio = [s for s in enabled if s.transport == "stdio"]
        http = [s for s in enabled if s.transport != "stdio"]

        logger.info(
            "Connecting to %d MCP servers (%d stdio, %d http)...",
            len(enabled), len(stdio), len(http),
        )

        # Phase 1: stdio servers — local processes, connect fast
        if stdio:
            await asyncio.gather(*(self._connect_timed(s) for s in stdio))
            logger.info(
                "Stdio phase: %d connected, %d tools",
                self.connected_count, self._registry.tool_count,
            )

        # Phase 2: HTTP servers — network, may be slower
        if http:
            await asyncio.gather(*(self._connect_timed(s) for s in http))

        logger.info(
            "All phases: %d/%d servers, %d tools",
            self.connected_count, len(enabled), self._registry.tool_count,
        )

        # Start background retry for any failed servers
        if self._failed:
            self._start_retry_loop()

        return dict(self._failed)

    async def reconnect(self, server_name: str) -> tuple[bool, str]:
        """Reconnect a single server by name. Returns (success, message)."""
        server_config = next(
            (s for s in self._config.mcp_servers if s.name == server_name),
            None,
        )
        if server_config is None:
            return False, f"Server '{server_name}' not found in config"

        # Disconnect if already connected
        existing = self._connections.get(server_name)
        if existing and existing.is_connected:
            await self._disconnect_one(server_name)

        # Connect fresh
        await self._connect_timed(server_config)

        if server_name in self._failed:
            return False, f"Failed: {self._failed[server_name]}"

        return True, f"Connected: {self._connections[server_name].capabilities.get('tools', []).__len__()} tools"

    async def connect_one(self, server_name: str) -> bool:
        """Connect to a single server by name. Returns True on success."""
        ok, _ = await self.reconnect(server_name)
        return ok

    async def disconnect_all(self) -> None:
        """Disconnect all MCP server connections and stop retry loop."""
        self._shutdown = True

        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            self._retry_task = None

        tasks = [self._disconnect_one(name) for name in list(self._connections)]
        if tasks:
            await asyncio.gather(*tasks)

        self._connections = {}
        self._failed = {}
        self._registry.clear()
        logger.info("All MCP connections closed")

    async def disconnect_one(self, server_name: str) -> None:
        """Disconnect a single server."""
        await self._disconnect_one(server_name)
        self._sync_registry()

    def get_server_status(self) -> list[dict[str, Any]]:
        """Get status of all servers including connection times."""
        result = []
        for srv in self._config.mcp_servers:
            conn = self._connections.get(srv.name)
            entry: dict[str, Any] = {
                "name": srv.name,
                "transport": srv.transport,
                "enabled": srv.enabled,
                "connected": conn.is_connected if conn else False,
                "tools": len(conn.capabilities.get("tools", [])) if conn else 0,
                "connect_time_ms": round(self._connect_times.get(srv.name, 0) * 1000),
            }
            if srv.name in self._failed:
                entry["error"] = self._failed[srv.name]
            result.append(entry)
        return result

    # -- Internal --

    async def _connect_timed(
        self,
        server_config: MCPServerConfig,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Connect to one server with timeout, track time, sync registry.

        If connection takes longer than timeout_seconds, mark as failed
        and move on — don't block other servers.
        """
        name = server_config.name
        start = time.monotonic()

        conn = MCPConnection(server_config)
        self._connections[name] = conn

        try:
            await asyncio.wait_for(conn.connect(), timeout=timeout_seconds)
            elapsed = time.monotonic() - start
            self._connect_times[name] = elapsed
            self._failed.pop(name, None)
            logger.info(
                "Connected to %s: %d tools (%.1fs)",
                name,
                len(conn.capabilities.get("tools", [])),
                elapsed,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            self._connect_times[name] = elapsed
            self._failed[name] = f"Connection timed out after {timeout_seconds:.0f}s"
            logger.warning("Timeout connecting to %s after %.0fs", name, timeout_seconds)
            try:
                await conn.disconnect()
            except Exception:
                pass
        except Exception as exc:
            elapsed = time.monotonic() - start
            self._connect_times[name] = elapsed
            self._failed[name] = str(exc)
            logger.warning("Failed to connect to %s (%.1fs): %s", name, elapsed, exc)

        self._sync_registry()

    async def _disconnect_one(self, server_name: str) -> None:
        """Disconnect a single server."""
        conn = self._connections.get(server_name)
        if conn is None:
            return
        try:
            await conn.disconnect()
        except Exception as exc:
            logger.warning("Error disconnecting %s: %s", server_name, exc)

    def _sync_registry(self) -> None:
        """Sync all connected server capabilities into the registry."""
        server_caps: dict[str, dict[str, Any]] = {}
        for name, conn in self._connections.items():
            if conn.is_connected:
                server_caps[name] = conn.capabilities
        self._registry.sync(server_caps)

    def _start_retry_loop(self) -> None:
        """Start background task to retry failed servers."""
        if self._retry_task and not self._retry_task.done():
            return
        self._retry_task = asyncio.create_task(self._retry_failed_servers())

    async def _retry_failed_servers(self) -> None:
        """Retry failed servers with exponential backoff."""
        delay = _INITIAL_RETRY_DELAY_S
        attempt = 0

        while not self._shutdown and self._failed and attempt < _MAX_RETRY_ATTEMPTS:
            attempt += 1
            failed_names = list(self._failed.keys())
            logger.info(
                "Retry attempt %d/%d for %d failed servers (delay %.0fs): %s",
                attempt, _MAX_RETRY_ATTEMPTS, len(failed_names), delay, failed_names,
            )

            await asyncio.sleep(delay)

            if self._shutdown:
                break

            for name in failed_names:
                if self._shutdown:
                    break
                server_config = next(
                    (s for s in self._config.mcp_servers if s.name == name),
                    None,
                )
                if server_config:
                    await self._connect_timed(server_config)

            # Exponential backoff capped at max
            delay = min(delay * 2, _MAX_RETRY_DELAY_S)

        if self._failed and not self._shutdown:
            logger.warning(
                "Gave up retrying %d servers after %d attempts: %s",
                len(self._failed), _MAX_RETRY_ATTEMPTS, list(self._failed.keys()),
            )
