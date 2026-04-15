"""Connection manager — spawns, monitors, and syncs all MCP server connections."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import MCPConnection

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages the lifecycle of all MCP server connections.

    Responsibilities:
    - Create MCPConnection for each configured server
    - Connect to enabled servers (lazy or eager)
    - Sync discovered capabilities into the registry
    - Disconnect all on shutdown
    - Provide the connections dict for the FederationRouter
    """

    def __init__(
        self,
        config: HubConfig,
        registry: CapabilityRegistry,
    ) -> None:
        self._config = config
        self._registry = registry
        self._connections: dict[str, MCPConnection] = {}
        self._failed: dict[str, str] = {}  # server_name -> error message

    @property
    def connections(self) -> dict[str, MCPConnection]:
        return self._connections

    @property
    def connected_count(self) -> int:
        return sum(1 for c in self._connections.values() if c.is_connected)

    @property
    def failed_servers(self) -> dict[str, str]:
        return dict(self._failed)

    async def connect_all(self) -> dict[str, str]:
        """Connect to all enabled MCP servers concurrently.

        Returns dict of {server_name: error_message} for servers that failed.
        Servers that connect successfully are registered in the capability registry.
        """
        enabled_servers = [s for s in self._config.mcp_servers if s.enabled]

        if not enabled_servers:
            logger.info("No MCP servers configured")
            return {}

        logger.info("Connecting to %d MCP servers...", len(enabled_servers))

        # Connect concurrently with individual error isolation
        tasks = [
            self._connect_one(server_config)
            for server_config in enabled_servers
        ]
        await asyncio.gather(*tasks)

        # Sync all capabilities into registry
        self._sync_registry()

        logger.info(
            "Connected: %d/%d servers, %d tools registered",
            self.connected_count,
            len(enabled_servers),
            self._registry.tool_count,
        )

        return dict(self._failed)

    async def connect_one(self, server_name: str) -> bool:
        """Connect to a single server by name. Returns True on success."""
        server_config = next(
            (s for s in self._config.mcp_servers if s.name == server_name),
            None,
        )
        if server_config is None:
            return False

        await self._connect_one(server_config)
        self._sync_registry()
        return server_name in self._connections and self._connections[server_name].is_connected

    async def disconnect_all(self) -> None:
        """Disconnect all MCP server connections."""
        tasks = [
            self._disconnect_one(name)
            for name in list(self._connections.keys())
        ]
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

    async def _connect_one(self, server_config: MCPServerConfig) -> None:
        """Connect to a single MCP server. Error-isolated."""
        name = server_config.name
        conn = MCPConnection(server_config)
        self._connections[name] = conn

        try:
            await conn.connect()
            self._failed.pop(name, None)
            logger.info(
                "Connected to %s: %d tools, %d resources, %d prompts",
                name,
                len(conn.capabilities.get("tools", [])),
                len(conn.capabilities.get("resources", [])),
                len(conn.capabilities.get("prompts", [])),
            )
        except Exception as exc:
            error_msg = str(exc)
            self._failed[name] = error_msg
            logger.warning("Failed to connect to %s: %s", name, error_msg)

    async def _disconnect_one(self, server_name: str) -> None:
        """Disconnect a single server. Error-isolated."""
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
