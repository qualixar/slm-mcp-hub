"""Federation router — routes tool/resource/prompt calls to the correct MCP server."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import MCPConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteResult:
    """Immutable result of a routed tool call."""

    result: dict[str, Any]
    server_name: str
    tool_name: str
    duration_ms: int
    success: bool
    cached: bool = False


class FederationRouter:
    """Routes requests to the correct MCP server via the capability registry.

    Holds a reference to the shared connection pool (dict of MCPConnections)
    and the capability registry.  Does NOT own these — the Hub does.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        connections: dict[str, MCPConnection],
    ) -> None:
        self._registry = registry
        self._connections = connections

    async def route_tool_call(
        self,
        namespaced_name: str,
        arguments: dict[str, Any],
    ) -> RouteResult:
        """Route a tool call to the correct MCP server."""
        cap = self._registry.lookup_tool(namespaced_name)
        if cap is None:
            return RouteResult(
                result={"content": [{"type": "text", "text": f"Tool not found: {namespaced_name}"}], "isError": True},
                server_name="unknown",
                tool_name=namespaced_name,
                duration_ms=0,
                success=False,
            )

        conn = self._connections.get(cap.server_name)
        if conn is None or not conn.is_connected:
            return RouteResult(
                result={"content": [{"type": "text", "text": f"Server not connected: {cap.server_name}"}], "isError": True},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=0,
                success=False,
            )

        start = time.monotonic()
        try:
            result = await conn.call_tool(cap.original_name, arguments)
            duration = int((time.monotonic() - start) * 1000)
            is_error = result.get("isError", False)
            return RouteResult(
                result=result,
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=not is_error,
            )
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            logger.error("Tool call %s failed: %s", namespaced_name, exc)
            return RouteResult(
                result={"content": [{"type": "text", "text": str(exc)}], "isError": True},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=False,
            )

    async def route_resource_read(
        self,
        namespaced_uri: str,
    ) -> RouteResult:
        """Route a resource read to the correct MCP server."""
        cap = self._registry.lookup_resource(namespaced_uri)
        if cap is None:
            return RouteResult(
                result={},
                server_name="unknown",
                tool_name=namespaced_uri,
                duration_ms=0,
                success=False,
            )

        conn = self._connections.get(cap.server_name)
        if conn is None or not conn.is_connected:
            return RouteResult(
                result={},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=0,
                success=False,
            )

        start = time.monotonic()
        try:
            result = await conn.read_resource(cap.original_name)
            duration = int((time.monotonic() - start) * 1000)
            return RouteResult(
                result=result,
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=True,
            )
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            logger.error("Resource read %s failed: %s", namespaced_uri, exc)
            return RouteResult(
                result={},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=False,
            )

    async def route_prompt_get(
        self,
        namespaced_name: str,
        arguments: dict[str, Any],
    ) -> RouteResult:
        """Route a prompt get to the correct MCP server."""
        cap = self._registry.lookup_prompt(namespaced_name)
        if cap is None:
            return RouteResult(
                result={},
                server_name="unknown",
                tool_name=namespaced_name,
                duration_ms=0,
                success=False,
            )

        conn = self._connections.get(cap.server_name)
        if conn is None or not conn.is_connected:
            return RouteResult(
                result={},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=0,
                success=False,
            )

        start = time.monotonic()
        try:
            result = await conn.get_prompt(cap.original_name, arguments)
            duration = int((time.monotonic() - start) * 1000)
            return RouteResult(
                result=result,
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=True,
            )
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            logger.error("Prompt get %s failed: %s", namespaced_name, exc)
            return RouteResult(
                result={},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=False,
            )
