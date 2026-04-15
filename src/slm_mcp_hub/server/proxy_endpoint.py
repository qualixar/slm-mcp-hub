"""Transparent proxy endpoint — one endpoint per backend MCP server.

Each /mcp/{server_name} endpoint acts as if it IS that MCP server.
Tool names are returned UNMODIFIED. Claude sees the original names.
The hub is completely invisible to the client.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from slm_mcp_hub.core.constants import VERSION
from slm_mcp_hub.federation.connection import MCPConnection
from slm_mcp_hub.federation.manager import ConnectionManager

if TYPE_CHECKING:
    from slm_mcp_hub.core.hub import HubOrchestrator

logger = logging.getLogger(__name__)


class ProxyEndpoint:
    """Transparent proxy for a single backend MCP server.

    Forwards JSON-RPC messages directly to the backend connection
    and returns responses without any transformation.
    """

    def __init__(self, conn_manager: ConnectionManager, hub: HubOrchestrator | None = None) -> None:
        self._conn_manager = conn_manager
        self._hub = hub

    def get_connection(self, server_name: str) -> MCPConnection | None:
        """Get the backend connection for a server name."""
        return self._conn_manager.connections.get(server_name)

    async def handle_jsonrpc(
        self,
        server_name: str,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Handle a JSON-RPC message for a specific backend server.

        Routes to the backend connection and returns the response
        UNMODIFIED — original tool names, original capabilities.
        """
        method = message.get("method", "")
        params = message.get("params", {})
        msg_id = message.get("id")

        # Notifications (no id)
        if msg_id is None:
            return None

        conn = self.get_connection(server_name)
        if conn is None:
            return _error_response(msg_id, -32001, f"Server not found: {server_name}")

        if not conn.is_connected:
            return _error_response(msg_id, -32002, f"Server not connected: {server_name}")

        # Handle MCP protocol methods
        if method == "initialize":
            return _success_response(msg_id, _build_init_result(conn))

        if method == "tools/list":
            tools = conn.capabilities.get("tools", [])
            return _success_response(msg_id, {"tools": tools})

        if method == "tools/call":
            return await self._proxy_tool_call(msg_id, conn, params)

        if method == "resources/list":
            resources = conn.capabilities.get("resources", [])
            return _success_response(msg_id, {"resources": resources})

        if method == "resources/read":
            return await self._proxy_resource_read(msg_id, conn, params)

        if method == "resources/templates/list":
            templates = conn.capabilities.get("resource_templates", [])
            return _success_response(msg_id, {"resourceTemplates": templates})

        if method == "prompts/list":
            prompts = conn.capabilities.get("prompts", [])
            return _success_response(msg_id, {"prompts": prompts})

        if method == "prompts/get":
            return await self._proxy_prompt_get(msg_id, conn, params)

        return _error_response(msg_id, -32601, f"Method not found: {method}")

    async def _proxy_tool_call(
        self,
        msg_id: Any,
        conn: MCPConnection,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Forward a tool call to the backend and return the result."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        start = time.time()
        success = False
        try:
            result = await conn.call_tool(tool_name, arguments)
            success = True
            return _success_response(msg_id, result)
        except Exception as exc:
            logger.error("Proxy tool call %s failed: %s", tool_name, exc)
            return _error_response(msg_id, -32603, "Internal server error")
        finally:
            duration_ms = int((time.time() - start) * 1000)
            if self._hub:
                try:
                    await self._hub.notify_plugins_tool_call_after(
                        session_id="proxy",
                        server=conn.name,
                        tool=tool_name,
                        args=arguments,
                        result=None,
                        duration_ms=duration_ms,
                        success=success,
                    )
                except Exception as exc:
                    logger.debug("Plugin notification failed: %s", exc)

    async def _proxy_resource_read(
        self,
        msg_id: Any,
        conn: MCPConnection,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Forward a resource read to the backend."""
        uri = params.get("uri", "")

        try:
            result = await conn.read_resource(uri)
            return _success_response(msg_id, result)
        except Exception as exc:
            logger.error("Proxy resource read %s failed: %s", uri, exc)
            return _error_response(msg_id, -32603, "Internal server error")

    async def _proxy_prompt_get(
        self,
        msg_id: Any,
        conn: MCPConnection,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Forward a prompt get to the backend."""
        name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            result = await conn.get_prompt(name, arguments)
            return _success_response(msg_id, result)
        except Exception as exc:
            logger.error("Proxy prompt get %s failed: %s", name, exc)
            return _error_response(msg_id, -32603, "Internal server error")

    def list_available_servers(self) -> list[dict[str, Any]]:
        """List all available backend servers and their status."""
        result = []
        for name, conn in self._conn_manager.connections.items():
            result.append({
                "name": name,
                "connected": conn.is_connected,
                "tools": len(conn.capabilities.get("tools", [])),
                "resources": len(conn.capabilities.get("resources", [])),
                "prompts": len(conn.capabilities.get("prompts", [])),
            })
        return result


def _build_init_result(conn: MCPConnection) -> dict[str, Any]:
    """Build an MCP initialize response for a proxied server."""
    caps = conn.capabilities
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {"listChanged": True} if caps.get("tools") else {},
            "resources": {"listChanged": True} if caps.get("resources") else {},
            "prompts": {"listChanged": True} if caps.get("prompts") else {},
        },
        "serverInfo": {
            "name": conn.name,
            "version": VERSION,
        },
    }


def _success_response(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
