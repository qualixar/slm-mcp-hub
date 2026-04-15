"""MCP Endpoint — federated MCP server facing AI clients.

Each connected client gets its own MCP Server instance.
All instances share the same federation router (shared MCP pool).
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from slm_mcp_hub.core.constants import VERSION
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter
from slm_mcp_hub.session.manager import SessionManager

if TYPE_CHECKING:
    from slm_mcp_hub.core.hub import HubOrchestrator

logger = logging.getLogger(__name__)


class MCPEndpoint:
    """Federated MCP endpoint that serves multiple AI clients.

    Handles JSON-RPC requests from clients, routes tool calls
    through the federation router, and returns results.

    This is the server-side MCP protocol handler. Transport
    (HTTP/SSE/stdio) is handled by the HTTP server layer above.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        router: FederationRouter,
        session_manager: SessionManager,
        hub: HubOrchestrator | None = None,
    ) -> None:
        self._registry = registry
        self._router = router
        self._session_manager = session_manager
        self._hub = hub

    async def handle_initialize(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle MCP initialize request."""
        client_info = params.get("clientInfo", {})
        client_name = client_info.get("name", "unknown")

        # Update session with client info
        session = self._session_manager.get_session(session_id)
        if session:
            logger.info("MCP client initialized: %s (session %s)", client_name, session_id[:8])

        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"listChanged": True},
                "prompts": {"listChanged": True},
            },
            "serverInfo": {
                "name": "slm-mcp-hub",
                "version": VERSION,
            },
        }

    async def handle_tools_list(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle tools/list — Meta-MCP pattern for massive token savings.

        Returns ONLY 3 meta-tools (~1K tokens) instead of 462 tools (~150K tokens).
        Claude discovers tools on demand via hub__search_tools,
        then calls them via hub__call_tool.

        462 tools x ~330 tokens each = ~150K tokens saved per session.
        """
        self._session_manager.touch(session_id)

        total_tools = self._registry.tool_count
        server_count = len({
            t["name"].split("__", 1)[0]
            for t in self._registry.list_tools()
            if "__" in t["name"]
        })

        meta_tools = [
            {
                "name": "hub__search_tools",
                "description": (
                    f"Search across {total_tools} tools from {server_count} MCP servers. "
                    "Returns matching tool names, descriptions, server name, and full input schema. "
                    "Use this to find the right tool before calling it with hub__call_tool. "
                    "Example queries: 'github search', 'generate image', 'database query', 'memory recall'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search keyword — matches tool names and descriptions",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "hub__call_tool",
                "description": (
                    "Call any tool from any connected MCP server. "
                    "First use hub__search_tools to find the tool name and its parameters, "
                    "then call it here. The tool name must be the full namespaced name "
                    "from the search results (e.g., 'github__search_repositories')."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "Full tool name from hub__search_tools results (e.g., 'context7__query-docs')",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments to pass to the tool — see inputSchema from hub__search_tools",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["tool"],
                },
            },
            {
                "name": "hub__list_servers",
                "description": (
                    f"List all {server_count} connected MCP servers with their tool counts. "
                    "Use to understand what's available before searching."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

        return {"tools": meta_tools}

    async def _handle_meta_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle Meta-MCP hub__ tools."""
        if name == "hub__search_tools":
            return await self._meta_search_tools(arguments)

        if name == "hub__call_tool":
            return await self._meta_call_tool(arguments)

        if name == "hub__list_servers":
            return await self._meta_list_servers()

        return {"content": [{"type": "text", "text": f"Unknown meta-tool: {name}"}], "isError": True}

    async def _meta_search_tools(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Search tools — returns names, descriptions, server, AND full inputSchema.

        Smart matching: splits query into words, matches if ALL words
        appear in the tool name, description, or server name.
        'github search' matches 'github__search_repositories'.
        """
        query = arguments.get("query", "").lower()
        query_words = query.split()
        all_tools = self._registry.list_tools()

        matches = []
        for t in all_tools:
            name = t.get("name", "").lower()
            desc = t.get("description", "").lower()
            searchable = f"{name} {desc} {name.replace('__', ' ').replace('_', ' ')}"
            if all(word in searchable for word in query_words):
                server = t["name"].split("__", 1)[0] if "__" in t["name"] else "unknown"
                matches.append({
                    "tool": t["name"],
                    "server": server,
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {}),
                })

        result = {
            "found": len(matches),
            "query": query,
            "tools": matches[:30],
        }
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

    async def _meta_call_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call any tool through the hub — the universal tool router."""
        tool_name = arguments.get("tool", "")
        tool_args = arguments.get("arguments", {})

        if not tool_name:
            return {
                "content": [{"type": "text", "text": "Error: 'tool' parameter is required. Use hub__search_tools to find tool names."}],
                "isError": True,
            }

        start = time.time()
        result = await self._router.route_tool_call(tool_name, tool_args)
        duration_ms = int((time.time() - start) * 1000)

        logger.debug(
            "Meta call: %s → %s (%dms, success=%s)",
            tool_name, result.server_name, duration_ms, result.success,
        )

        # Notify plugins (SLM learning, mesh broadcast, etc.)
        if self._hub:
            try:
                await self._hub.notify_plugins_tool_call_after(
                    session_id="federated",
                    server=result.server_name,
                    tool=tool_name.split("__", 1)[-1] if "__" in tool_name else tool_name,
                    args=tool_args,
                    result=result.result,
                    duration_ms=duration_ms,
                    success=result.success,
                )
            except Exception as exc:
                logger.debug("Plugin notification failed: %s", exc)

        return result.result

    async def _meta_list_servers(self) -> dict[str, Any]:
        """List all connected servers with tool counts."""
        server_tools: dict[str, list[str]] = {}
        for t in self._registry.list_tools():
            name = t["name"]
            parts = name.split("__", 1)
            if len(parts) == 2:
                server = parts[0]
                tool = parts[1]
                server_tools.setdefault(server, []).append(tool)

        servers = [
            {"server": name, "tools": len(tools), "tool_names": sorted(tools)}
            for name, tools in sorted(server_tools.items())
        ]

        result = {"server_count": len(servers), "servers": servers}
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

    async def handle_tools_call(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle tools/call — route to correct MCP server or handle meta-tools."""
        self._session_manager.touch(session_id)
        name = params.get("name", "")
        arguments = params.get("arguments", {})

        # Handle Meta-MCP tools locally
        if name.startswith("hub__"):
            return await self._handle_meta_tool(name, arguments)

        start = time.time()
        result = await self._router.route_tool_call(name, arguments)
        duration_ms = int((time.time() - start) * 1000)

        # Log the call for observability
        logger.debug(
            "Tool call: %s → %s (session=%s, %dms, success=%s)",
            name, result.server_name, session_id[:8], duration_ms, result.success,
        )

        return result.result

    async def handle_resources_list(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle resources/list — return all namespaced resources."""
        self._session_manager.touch(session_id)
        resources = self._registry.list_resources()
        return {"resources": resources}

    async def handle_resources_read(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle resources/read — route to correct MCP server."""
        self._session_manager.touch(session_id)
        uri = params.get("uri", "")
        result = await self._router.route_resource_read(uri)
        return result.result

    async def handle_resources_templates_list(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle resources/templates/list."""
        self._session_manager.touch(session_id)
        templates = self._registry.list_resource_templates()
        return {"resourceTemplates": templates}

    async def handle_prompts_list(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle prompts/list — return all namespaced prompts."""
        self._session_manager.touch(session_id)
        prompts = self._registry.list_prompts()
        return {"prompts": prompts}

    async def handle_prompts_get(self, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle prompts/get — route to correct MCP server."""
        self._session_manager.touch(session_id)
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = await self._router.route_prompt_get(name, arguments)
        return result.result

    async def handle_jsonrpc(self, session_id: str, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch a JSON-RPC message to the appropriate handler.

        Returns a JSON-RPC response dict, or None for notifications.
        """
        method = message.get("method", "")
        params = message.get("params", {})
        msg_id = message.get("id")

        # Notifications (no id) — acknowledge silently
        if msg_id is None:
            return None

        handler_map = {
            "initialize": self.handle_initialize,
            "tools/list": self.handle_tools_list,
            "tools/call": self.handle_tools_call,
            "resources/list": self.handle_resources_list,
            "resources/read": self.handle_resources_read,
            "resources/templates/list": self.handle_resources_templates_list,
            "prompts/list": self.handle_prompts_list,
            "prompts/get": self.handle_prompts_get,
        }

        handler = handler_map.get(method)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        try:
            result = await handler(session_id, params)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except Exception as exc:
            logger.error("Handler error for %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": "Internal server error"},
            }
