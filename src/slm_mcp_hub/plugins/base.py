"""Plugin interface for SLM MCP Hub."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from slm_mcp_hub.core.hub import HubOrchestrator


class HubPlugin(ABC):
    """Base class for hub plugins.

    Plugins are discovered via Python entry_points at startup.
    Each plugin receives lifecycle hooks for tool calls, sessions, and MCPs.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin name."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Plugin version string."""

    async def on_hub_start(self, hub: HubOrchestrator) -> None:
        """Called when hub starts. Initialize plugin resources here."""

    async def on_hub_stop(self) -> None:
        """Called when hub is stopping. Clean up plugin resources."""

    async def on_tool_call_before(
        self,
        session_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Called before routing a tool call. Return modified args, or None."""
        return None

    async def on_tool_call_after(
        self,
        session_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
        result: Any,
        duration_ms: int,
        success: bool,
    ) -> None:
        """Called after a tool call completes. Observe, learn, log."""

    async def on_session_start(self, session_id: str, client_info: dict[str, Any]) -> None:
        """Called when a new client session is created."""

    async def on_session_end(self, session_id: str) -> None:
        """Called when a client session ends."""

    async def on_mcp_connect(self, server_name: str) -> None:
        """Called when an MCP server connects."""

    async def on_mcp_disconnect(self, server_name: str) -> None:
        """Called when an MCP server disconnects."""
