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
# Fast cold-start retry schedule (Phase 6, Charter Feature C1).
# Called explicitly by `slm-hub start` after the initial connect_all()
# parallel attempt. Slow background _retry_failed_servers() still runs
# afterwards for any servers that still fail.
_FAST_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.5, 4.5)
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
        # Serializes all mutation operations (add/remove/modify/sync).
        # Reads (route_tool_call, registry lookups) are lock-free — atomic swap
        # on registry means reads see consistent old-or-new state, never partial.
        self._lock: asyncio.Lock = asyncio.Lock()
        # Optional notifier — set by HubRuntime so we can fire
        # notifications/tools/list_changed when the registry actually changes.
        # When None (e.g. in unit tests), changes are silent.
        self._notifier: Any | None = None

    def set_notifier(self, notifier: Any | None) -> None:
        """Attach a ChangeNotifier. HubRuntime wires this after construction."""
        self._notifier = notifier

    @property
    def config(self) -> HubConfig:
        """Current effective config (mutable: add_server/remove_server/replace_server
        update this in place so subsequent reload diffs see the latest state)."""
        return self._config

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

        tool_count = len(self._connections[server_name].capabilities.get("tools", []))
        return True, f"Connected: {tool_count} tools"

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
        """Disconnect a single server and remove it from the live connection map."""
        async with self._lock:
            await self._disconnect_one(server_name)
            self._connections.pop(server_name, None)
            self._failed.pop(server_name, None)
            self._connect_times.pop(server_name, None)
            self._sync_registry()

    async def add_server(self, server_config: MCPServerConfig) -> tuple[bool, str]:
        """Add and connect a new server at runtime. No hub restart required.

        Returns (success, message). On success, the server's tools are
        immediately available via hub__call_tool. Other connections are
        untouched — kite SSE sessions survive this call.
        """
        async with self._lock:
            name = server_config.name
            if name in self._connections and self._connections[name].is_connected:
                return False, f"Server '{name}' is already connected"

            # Persist to in-memory config so reconnect/status pick it up
            existing_names = {s.name for s in self._config.mcp_servers}
            if name not in existing_names:
                from dataclasses import replace as dc_replace
                new_servers = tuple(self._config.mcp_servers) + (server_config,)
                self._config = dc_replace(self._config, mcp_servers=new_servers)

            await self._connect_timed(server_config)

        if name in self._failed:
            return False, f"Failed to connect: {self._failed[name]}"
        tool_count = len(self._connections[name].capabilities.get("tools", []))
        return True, f"Connected: {tool_count} tools"

    async def remove_server(self, server_name: str, drain_timeout_s: float = 30.0) -> tuple[bool, str]:
        """Drain in-flight calls then disconnect and deregister a server.

        Existing connections to OTHER servers are untouched. New tool calls
        to this server are rejected immediately (DRAINING state). In-flight
        calls are given drain_timeout_s to complete before force-disconnect.
        """
        conn = self._connections.get(server_name)
        if conn is None:
            return False, f"Server '{server_name}' not found in active connections"

        # Drain outside the lock — drain waits on asyncio events which may
        # themselves need the event loop. Holding the lock during drain would
        # deadlock any concurrent add_server call.
        await conn.drain_and_disconnect(timeout_s=drain_timeout_s)

        async with self._lock:
            self._connections.pop(server_name, None)
            self._failed.pop(server_name, None)
            self._connect_times.pop(server_name, None)
            # Remove from in-memory config
            from dataclasses import replace as dc_replace
            new_servers = tuple(s for s in self._config.mcp_servers if s.name != server_name)
            self._config = dc_replace(self._config, mcp_servers=new_servers)
            self._sync_registry()

        logger.info("Removed server: %s", server_name)
        return True, f"Removed and deregistered: {server_name}"

    async def replace_server(self, server_config: MCPServerConfig, drain_timeout_s: float = 30.0) -> tuple[bool, str]:
        """Restart a single server in-place (e.g. after env/config change).

        Drains the old connection, then connects fresh with the new config.
        Other connections are untouched.
        """
        name = server_config.name
        old_conn = self._connections.get(name)

        if old_conn is not None:
            await old_conn.drain_and_disconnect(timeout_s=drain_timeout_s)
            async with self._lock:
                self._connections.pop(name, None)
                self._failed.pop(name, None)
                # Replace config entry
                from dataclasses import replace as dc_replace
                new_servers = tuple(
                    server_config if s.name == name else s
                    for s in self._config.mcp_servers
                )
                self._config = dc_replace(self._config, mcp_servers=new_servers)

        ok, msg = await self.add_server(server_config)
        return ok, msg

    async def fast_retry_failed(self) -> dict[str, str]:
        """Quick retry pass for servers that failed in connect_all().

        Schedule: 0.5s → 1.5s → 4.5s gaps between attempts (Charter C1).
        Called explicitly by `slm-hub start` cold path so that unit tests
        which simulate connection failures aren't slowed by 6.5s of sleep.

        After this returns, the slow background _retry_failed_servers()
        loop continues to retry anything still failing.
        """
        for delay in _FAST_RETRY_DELAYS_S:
            if not self._failed or self._shutdown:
                break
            await asyncio.sleep(delay)
            failed_names = list(self._failed.keys())
            logger.info(
                "Fast cold-start retry (delay %.1fs) for %d servers: %s",
                delay, len(failed_names), failed_names,
            )
            for name in failed_names:
                srv_cfg = next(
                    (s for s in self._config.mcp_servers if s.name == name),
                    None,
                )
                if srv_cfg is None:
                    continue
                # Clean up the previous failed connection object before retrying
                old = self._connections.pop(name, None)
                if old is not None:
                    try:
                        await old.disconnect()
                    except Exception:
                        pass
                async with self._lock:
                    await self._connect_timed(srv_cfg)
        return dict(self._failed)

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
        """Sync all connected server capabilities into the registry.

        Consumes the `changed` flag CapabilityRegistry.sync() returns —
        if the namespaced tool/resource/prompt set actually changed,
        we fire the notifier so MCP clients get notifications/tools/list_changed.
        """
        server_caps: dict[str, dict[str, Any]] = {}
        for name, conn in self._connections.items():
            if conn.is_connected:
                server_caps[name] = conn.capabilities
        changed = self._registry.sync(server_caps)

        if changed and self._notifier is not None:
            # Schedule the notification — debounce inside the notifier
            # coalesces multiple syncs during startup or bulk reload.
            try:
                asyncio.create_task(self._notifier.notify_tools_changed())
            except RuntimeError:
                # No running event loop (e.g., test path that called sync
                # synchronously outside an async context). Silent skip.
                pass

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
