"""Hub runtime — shared object graph for HTTP and stdio transports.

Extracts the runtime construction that previously lived inside
cli/main.py:start()._run() (lines 136-163). Both HTTP serve and
stdio serve share one HubRuntime instance, avoiding boot-logic
duplication and enabling hot-reload to mutate the graph centrally.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.manager import ConnectionManager
from slm_mcp_hub.federation.router import FederationRouter
from slm_mcp_hub.lifecycle.notifier import ChangeNotifier
from slm_mcp_hub.lifecycle.reloader import ConfigReloader
from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
from slm_mcp_hub.server.proxy_endpoint import ProxyEndpoint
from slm_mcp_hub.session.manager import SessionManager

if TYPE_CHECKING:
    from slm_mcp_hub.core.config import HubConfig
    from slm_mcp_hub.core.hub import HubOrchestrator

logger = logging.getLogger(__name__)


class HubRuntime:
    """Owns the shared runtime object graph.

    Constructed once per process inside ``async with HubOrchestrator()``.
    Both the HTTP transport (``slm-hub start``) and the stdio transport
    (``slm-hub mcp``) receive this same instance — only the transport
    wrapper differs.

    Future phases add hot-reload, config-diff, and notification
    capabilities to this class without touching transport code.
    """

    def __init__(self, hub: HubOrchestrator) -> None:
        config = hub.config

        self._hub = hub
        self._config = config

        self._registry = CapabilityRegistry()
        self._conn_manager = ConnectionManager(config, self._registry)
        self._router = FederationRouter(
            self._registry, self._conn_manager.connections,
        )
        self._session_manager = SessionManager(
            max_sessions=config.max_sessions,
            timeout_seconds=config.session_timeout_seconds,
            overflow_policy=config.session_overflow,
        )
        self._mcp_endpoint = MCPEndpoint(
            self._registry, self._router, self._session_manager, hub=hub,
        )
        self._proxy = ProxyEndpoint(self._conn_manager, hub=hub)

        # Phase 3: lifecycle plumbing — notifier broadcasts capability
        # changes to subscribed clients; reloader applies new HubConfig
        # via the manager's add/remove/replace lifecycle methods.
        self._notifier = ChangeNotifier()
        self._conn_manager.set_notifier(self._notifier)
        self._reloader = ConfigReloader(self._conn_manager, self._notifier)

        logger.info(
            "HubRuntime initialized (%d configured servers, notifier+reloader wired)",
            len(config.mcp_servers),
        )

    # -- Public read-only accessors (immutable references) --

    @property
    def hub(self) -> HubOrchestrator:
        return self._hub

    @property
    def config(self) -> HubConfig:
        return self._config

    @property
    def registry(self) -> CapabilityRegistry:
        return self._registry

    @property
    def conn_manager(self) -> ConnectionManager:
        return self._conn_manager

    @property
    def router(self) -> FederationRouter:
        return self._router

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def mcp_endpoint(self) -> MCPEndpoint:
        return self._mcp_endpoint

    @property
    def proxy(self) -> ProxyEndpoint:
        return self._proxy

    @property
    def notifier(self) -> ChangeNotifier:
        return self._notifier

    @property
    def reloader(self) -> ConfigReloader:
        return self._reloader

    # -- Lifecycle operations --

    async def connect_all(self) -> dict[str, str]:
        """Connect to all federated MCPs. Returns {name: error} for failures."""
        return await self._conn_manager.connect_all()

    async def disconnect_all(self) -> None:
        """Disconnect all federated MCPs. Called during shutdown."""
        await self._conn_manager.disconnect_all()
        await self._notifier.shutdown()

    def get_status(self) -> dict[str, Any]:
        """Combined runtime + hub status for API endpoints."""
        hub_status = self._hub.get_status()
        return {
            **hub_status,
            "servers_connected": self._conn_manager.connected_count,
            "tools_registered": self._registry.tool_count,
            "sessions_active": self._session_manager.active_count,
        }
