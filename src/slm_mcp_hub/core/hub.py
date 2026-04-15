"""Hub Orchestrator — central coordinator for SLM MCP Hub."""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any

from slm_mcp_hub.core.config import HubConfig, load_config
from slm_mcp_hub.core.constants import VERSION
from slm_mcp_hub.plugins.base import HubPlugin
from slm_mcp_hub.storage.database import HubDatabase

logger = logging.getLogger(__name__)

# Singleton guard
_hub_instance: HubOrchestrator | None = None


class HubState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class HubOrchestrator:
    """Central coordinator for the MCP Hub.

    Manages configuration, storage, plugins, and the overall lifecycle.
    Use as an async context manager:

        async with HubOrchestrator(config) as hub:
            await hub.serve()
    """

    def __init__(self, config: HubConfig | None = None, config_path: Path | None = None) -> None:
        global _hub_instance
        if _hub_instance is not None:
            raise RuntimeError("Only one HubOrchestrator per process. Use get_hub().")
        _hub_instance = self

        self._config = config or load_config(config_path)
        self._db = HubDatabase(self._config.config_dir / "hub.db")
        self._state = HubState.STOPPED
        self._started_at: float = 0.0
        self._plugins: list[HubPlugin] = []
        self._event_handlers: dict[str, list[Any]] = {}

    @property
    def config(self) -> HubConfig:
        return self._config

    @property
    def db(self) -> HubDatabase:
        return self._db

    @property
    def state(self) -> str:
        return self._state

    @property
    def uptime_seconds(self) -> float:
        if self._started_at == 0:
            return 0.0
        return time.time() - self._started_at

    @property
    def plugins(self) -> list[HubPlugin]:
        return list(self._plugins)

    async def start(self) -> None:
        """Start the hub: open database, discover plugins, emit ready."""
        self._state = HubState.STARTING
        self._emit("hub_starting")

        try:
            self._db.open()
            self._discover_plugins()
            await self._init_plugins()
            self._started_at = time.time()
            self._state = HubState.READY
            self._emit("hub_ready")
            logger.info(
                "SLM MCP Hub v%s ready on %s:%d (%d MCP servers, %d plugins)",
                VERSION,
                self._config.host,
                self._config.port,
                len(self._config.mcp_servers),
                len(self._plugins),
            )
        except Exception as exc:
            self._state = HubState.ERROR
            self._emit("hub_error", error=str(exc))
            logger.error("Hub start failed: %s", exc)
            raise

    async def stop(self) -> None:
        """Stop the hub: close plugins, close database."""
        self._state = HubState.STOPPING
        self._emit("hub_stopping")

        for plugin in reversed(self._plugins):
            try:
                await plugin.on_hub_stop()
            except Exception as exc:
                logger.warning("Plugin %s stop error: %s", plugin.name, exc)

        self._db.close()
        self._state = HubState.STOPPED
        self._emit("hub_stopped")

        global _hub_instance
        _hub_instance = None
        logger.info("Hub stopped")

    async def __aenter__(self) -> HubOrchestrator:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    def on(self, event: str, handler: Any) -> None:
        """Register an event handler."""
        self._event_handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, **kwargs: Any) -> None:
        """Emit an event to registered handlers."""
        for handler in self._event_handlers.get(event, []):
            try:
                handler(**kwargs)
            except Exception as exc:
                logger.warning("Event handler error for %s: %s", event, exc)

    def _discover_plugins(self) -> None:
        """Discover plugins via Python entry_points."""
        self._plugins = []
        try:
            eps = importlib.metadata.entry_points()
            # Python 3.12+ uses select(), older versions use dict-like get()
            if hasattr(eps, "select"):
                hub_plugins = list(eps.select(group="slm_mcp_hub.plugins"))
            else:
                hub_plugins = eps.get("slm_mcp_hub.plugins", [])
        except Exception:
            hub_plugins = []

        for ep in hub_plugins:
            if self._config.plugins_enabled and ep.name not in self._config.plugins_enabled:
                logger.debug("Plugin %s not in enabled list, skipping", ep.name)
                continue
            try:
                plugin_cls = ep.load()
                plugin = plugin_cls()
                if isinstance(plugin, HubPlugin):
                    self._plugins.append(plugin)
                    logger.info("Discovered plugin: %s v%s", plugin.name, plugin.version)
                else:
                    logger.warning("Entry point %s is not a HubPlugin, skipping", ep.name)
            except Exception as exc:
                logger.warning("Failed to load plugin %s: %s", ep.name, exc)

    async def _init_plugins(self) -> None:
        """Initialize all discovered plugins."""
        for plugin in self._plugins:
            try:
                await plugin.on_hub_start(self)
                logger.info("Plugin %s initialized", plugin.name)
            except Exception as exc:
                logger.error("Plugin %s init failed: %s", plugin.name, exc)

    async def notify_plugins_tool_call_before(
        self,
        session_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Call before-hooks on all plugins. Return modified args from first responder."""
        for plugin in self._plugins:
            try:
                result = await plugin.on_tool_call_before(session_id, server, tool, args)
                if result is not None:
                    return result
            except Exception as exc:
                logger.warning(
                    "Plugin %s on_tool_call_before error: %s", plugin.name, exc
                )
        return None

    async def notify_plugins_tool_call_after(
        self,
        session_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
        result: Any,
        duration_ms: int,
        success: bool,
    ) -> None:
        """Notify all plugins after a tool call. Error-isolated."""
        for plugin in self._plugins:
            try:
                await plugin.on_tool_call_after(
                    session_id, server, tool, args, result, duration_ms, success
                )
            except Exception as exc:
                logger.warning(
                    "Plugin %s on_tool_call_after error: %s", plugin.name, exc
                )

    async def notify_plugins_session_start(
        self,
        session_id: str,
        client_info: dict[str, Any],
    ) -> None:
        """Notify all plugins of session start. Error-isolated."""
        for plugin in self._plugins:
            try:
                await plugin.on_session_start(session_id, client_info)
            except Exception as exc:
                logger.warning(
                    "Plugin %s on_session_start error: %s", plugin.name, exc
                )

    async def notify_plugins_session_end(self, session_id: str) -> None:
        """Notify all plugins of session end. Error-isolated."""
        for plugin in self._plugins:
            try:
                await plugin.on_session_end(session_id)
            except Exception as exc:
                logger.warning(
                    "Plugin %s on_session_end error: %s", plugin.name, exc
                )

    async def notify_plugins_mcp_connect(self, server_name: str) -> None:
        """Notify all plugins of MCP server connection. Error-isolated."""
        for plugin in self._plugins:
            try:
                await plugin.on_mcp_connect(server_name)
            except Exception as exc:
                logger.warning(
                    "Plugin %s on_mcp_connect error: %s", plugin.name, exc
                )

    async def notify_plugins_mcp_disconnect(self, server_name: str) -> None:
        """Notify all plugins of MCP server disconnection. Error-isolated."""
        for plugin in self._plugins:
            try:
                await plugin.on_mcp_disconnect(server_name)
            except Exception as exc:
                logger.warning(
                    "Plugin %s on_mcp_disconnect error: %s", plugin.name, exc
                )

    def get_plugin(self, name: str) -> HubPlugin | None:
        """Get a specific plugin by name. Returns None if not found."""
        for plugin in self._plugins:
            if plugin.name == name:
                return plugin
        return None

    def get_status(self) -> dict[str, Any]:
        """Return hub status as a dict."""
        return {
            "state": self._state,
            "version": VERSION,
            "host": self._config.host,
            "port": self._config.port,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "mcp_servers_configured": len(self._config.mcp_servers),
            "plugins_loaded": [p.name for p in self._plugins],
        }


def get_hub() -> HubOrchestrator | None:
    """Get the singleton hub instance, or None if not started."""
    return _hub_instance


def reset_hub() -> None:
    """Reset singleton (for testing only)."""
    global _hub_instance
    _hub_instance = None
