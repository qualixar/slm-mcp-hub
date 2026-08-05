"""Hub runtime — shared object graph for HTTP and stdio transports.

Extracts the runtime construction that previously lived inside
cli/main.py:start()._run() (lines 136-163). Both HTTP serve and
stdio serve share one HubRuntime instance, avoiding boot-logic
duplication and enabling hot-reload to mutate the graph centrally.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.concurrency import (
    DEFAULT_PER_BACKEND_CONCURRENCY,
    BackendConcurrencyGate,
)
from slm_mcp_hub.federation.manager import ConnectionManager
from slm_mcp_hub.federation.router import FederationRouter
from slm_mcp_hub.federation.timeouts import TimeoutRegistry
from slm_mcp_hub.lifecycle.notifier import ChangeNotifier
from slm_mcp_hub.lifecycle.reloader import ConfigReloader
from slm_mcp_hub.observability.metrics import MetricsCollector
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

        # W4-P2: build per-backend concurrency gate from server configs.
        per_server_overrides = {
            srv.name: srv.max_concurrency
            for srv in config.mcp_servers
            if srv.max_concurrency != DEFAULT_PER_BACKEND_CONCURRENCY
        }
        concurrency_gate = BackendConcurrencyGate(
            default_max_concurrency=DEFAULT_PER_BACKEND_CONCURRENCY,
            per_server_overrides=per_server_overrides,
        )

        # W4-P2: build timeout registry (no overrides at runtime level for now;
        # operator overrides can be wired via HubConfig in a future patch).
        timeout_registry = TimeoutRegistry()

        # W8-P5: MetricsCollector records per-server call_count/p95/success_rate.
        # Created here so the same instance is shared between the router (which
        # records) and create_app (which reads via the dashboard).
        self._metrics = MetricsCollector()

        self._router = FederationRouter(
            self._registry,
            self._conn_manager.connections,
            activity_fn=self._conn_manager.mark_activity,    # W3-P2
            reconnect_fn=self._conn_manager.ensure_connected, # W3-P3
            concurrency_gate=concurrency_gate,               # W4-P2
            timeout_registry=timeout_registry,               # W4-P2
            metrics=self._metrics,                           # W8-P5
        )
        self._session_manager = SessionManager(
            max_sessions=config.max_sessions,
            timeout_seconds=config.session_timeout_seconds,
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

        # W2-P2: tracked background connect task + stopped flag for idempotent stop.
        self._bg_connect_task: asyncio.Task[None] | None = None
        self._stopped: bool = False

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

    @property
    def metrics(self) -> MetricsCollector:
        """W8-P5: Shared MetricsCollector — same instance the router feeds."""
        return self._metrics

    # -- Lifecycle operations --

    async def connect_all(self) -> dict[str, str]:
        """Connect to all federated MCPs. Returns {name: error} for failures.

        Awaitable: existing programmatic and test callers use ``await`` directly.
        This signature is intentionally unchanged from pre-W2-P2.
        """
        return await self._conn_manager.connect_all()

    def start_background_connect(
        self,
        *,
        post_connect: Callable[[dict[str, str]], Awaitable[None]] | None = None,
    ) -> None:
        """Launch connect_all as a tracked background asyncio.Task and return immediately.

        The hub MCP endpoint can start serving immediately after this call.
        Backends register their tools in the registry as they become ready;
        clients see the tool set grow — no request blocks on a still-connecting
        backend.

        Idempotent: if a background connect task is already in flight, this call
        is a no-op and returns without spawning a second task.  A new task IS
        created when the previous task has already finished (restart case).

        If the runtime is already stopped, the call is silently ignored.

        Args:
            post_connect: Optional coroutine called with the failure dict once
                connect_all completes.  Exceptions from the hook are caught,
                logged, and swallowed — they never propagate out of the task.
        """
        if self._stopped:
            logger.debug("start_background_connect: runtime already stopped, ignoring")
            return
        if self._bg_connect_task is not None and not self._bg_connect_task.done():
            logger.debug("start_background_connect: task already in flight, no-op")
            return

        async def _bg_task() -> None:
            try:
                failed = await self._conn_manager.connect_all()
                if post_connect is not None:
                    try:
                        await post_connect(failed)
                    except Exception:
                        logger.exception("post_connect hook raised an exception")
            except asyncio.CancelledError:
                logger.debug("Background connect task cancelled")
                raise
            except Exception:
                logger.exception("Background connect task raised unexpectedly")

        self._bg_connect_task = asyncio.create_task(
            _bg_task(), name="hub-bg-connect"
        )
        logger.debug("Background connect task started")

    async def stop(self) -> None:
        """Cancel the background connect task, then disconnect all backends.

        Call this instead of ``disconnect_all()`` from the CLI serve paths.
        Idempotent: the second and subsequent calls are no-ops.  Safe to call
        even if ``start_background_connect()`` was never called.
        """
        if self._stopped:
            return
        self._stopped = True

        if self._bg_connect_task is not None and not self._bg_connect_task.done():
            logger.debug("stop: cancelling in-flight background connect task")
            self._bg_connect_task.cancel()
            try:
                await self._bg_connect_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug(
                    "Background connect task raised on cancel", exc_info=True
                )

        await self.disconnect_all()

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
