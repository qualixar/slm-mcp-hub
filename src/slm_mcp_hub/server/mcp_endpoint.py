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
MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

# Keys that belong to the call_tool envelope itself rather than to the
# arguments of the tool being invoked.
_RESERVED_META_KEYS = frozenset({"tool", "arguments"})

# Some models flatten nested tool-call parameters into dotted top-level keys,
# so `{"arguments": {"issue_key": "A-1"}}` arrives as `{"arguments.issue_key":
# "A-1"}` (confirmed on Gemini 3.6 Flash).
_FLAT_ARG_PREFIX = "arguments."

class InvalidParams(ValueError):
    """Structurally invalid client params — reported as JSON-RPC -32602.

    Distinct from a tool that ran and failed, which is reported as an
    ``isError`` result rather than a protocol error.
    """


def _coerce_object(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Coerce a JSON-object-ish value into a dict.

    Clients vary: some send a real object, some send it JSON-encoded as a
    string. Returns (object, error_message); exactly one is non-None.
    """
    if value is None:
        return {}, None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}, None
        try:
            value = json.loads(text)
        except (ValueError, RecursionError) as exc:
            # JSONDecodeError subclasses ValueError; a bare ValueError also
            # escapes json.loads for syntactically valid but unrepresentable
            # input (e.g. Python 3.11+ integer digit limits), and pathological
            # nesting raises RecursionError.
            return None, f"it is a string but not valid JSON ({exc})"
        if value is None:
            return {}, None

    if isinstance(value, dict):
        return value, None

    return None, f"expected an object, got {type(value).__name__}"


def _normalise_tool_arguments(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the target tool's arguments from a call_tool payload.

    Accepts the documented nested form, a JSON-encoded string, and the
    flattened form some clients emit (arguments hoisted to the top level
    alongside 'tool'). Returns (arguments, error_message).

    An explicit 'arguments' key always wins, even when it is empty or null.
    Selecting on emptiness instead of presence would let unrelated sibling
    keys silently redefine a deliberately argument-less call.
    """
    if "arguments" in payload:
        nested, error = _coerce_object(payload["arguments"])
        if error is not None:
            return None, f"'arguments' is invalid: {error}"
        stray = sorted(k for k in payload if k not in _RESERVED_META_KEYS)
        if stray:
            logger.info(
                "call_tool for %r supplied both 'arguments' and top-level keys %s; "
                "ignoring the top-level keys",
                payload.get("tool"), stray,
            )
        return nested, None

    flattened = {k: v for k, v in payload.items() if k not in _RESERVED_META_KEYS}
    if flattened:
        logger.info(
            "call_tool for %r used the flattened argument form (keys %s); "
            "the documented form nests them under 'arguments'",
            payload.get("tool"), sorted(flattened),
        )
    return flattened, None


def _copy_nested_dicts(value: dict[str, Any]) -> dict[str, Any]:
    """Copy a mapping deeply enough that reconstruction cannot mutate the caller."""
    return {
        k: _copy_nested_dicts(v) if isinstance(v, dict) else v
        for k, v in value.items()
    }


def _descend(root: dict[str, Any], segments: list[str]) -> dict[str, Any] | None:
    """Walk (creating as needed) to the container holding the final segment.

    Returns None when the path runs through a value that is not an object;
    that branch is unreconstructable and is discarded rather than raising.
    Intermediate containers are only created for absent keys, so a discarded
    branch leaves no debris behind.
    """
    cursor = root
    for segment in segments:
        if segment not in cursor:
            cursor[segment] = {}
        branch = cursor[segment]
        if not isinstance(branch, dict):
            return None
        cursor = branch
    return cursor


def _reconstruct_dotted_arguments(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Rebuild ``arguments.``-prefixed top-level keys into a nested object.

    Returns (payload, repaired_paths, skipped_paths). This runs on the raw
    envelope *before* :func:`_normalise_tool_arguments`: a dot-notation-only
    payload has no 'arguments' key, so that function's sibling hoist would
    otherwise forward a literal key named 'arguments.issue_key' to a backend.

    A payload with no dotted keys is returned unchanged, by identity. An
    explicitly supplied value always wins over a flattened key that would
    overwrite it; losing keys are reported as skipped, never applied.
    """
    dotted = sorted(k for k in payload if k.startswith(_FLAT_ARG_PREFIX))
    if not dotted:
        return payload, [], []

    stripped = {k: v for k, v in payload.items() if not k.startswith(_FLAT_ARG_PREFIX)}

    existing = payload.get("arguments")
    if existing is not None and not isinstance(existing, dict):
        # A JSON-encoded or otherwise non-object 'arguments' wins outright;
        # _normalise_tool_arguments parses it or reports it. Merging into an
        # opaque value would be guesswork.
        return stripped, [], dotted

    rebuilt = _copy_nested_dicts(existing) if isinstance(existing, dict) else {}
    repaired: list[str] = []
    skipped: list[str] = []

    for key in dotted:
        segments = key[len(_FLAT_ARG_PREFIX):].split(".")
        if not all(segments):
            skipped.append(key)
            continue
        container = _descend(rebuilt, segments[:-1])
        leaf = segments[-1]
        if container is None or leaf in container:
            skipped.append(key)
            continue
        container[leaf] = payload[key]
        repaired.append(key)

    if repaired or "arguments" in payload:
        if "arguments" not in payload:
            # _normalise_tool_arguments hoists bare top-level siblings only
            # while 'arguments' is absent. Synthesising it here would suppress
            # that hoist and silently drop them, so fold them in. Dotted keys
            # win: they name their target namespace explicitly.
            for key in [k for k in stripped if k not in _RESERVED_META_KEYS]:
                rebuilt.setdefault(key, stripped.pop(key))
        stripped["arguments"] = rebuilt
    return stripped, repaired, skipped


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

        requested_version = params.get("protocolVersion")
        negotiated_version = (
            requested_version
            if requested_version in LEGACY_PROTOCOL_VERSIONS
            else LEGACY_PROTOCOL_VERSIONS[0]
        )
        return {
            "protocolVersion": negotiated_version,
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

    async def handle_server_discover(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Advertise capabilities for stateless MCP 2026-07-28 clients."""
        return {
            "supportedVersions": [
                MODERN_PROTOCOL_VERSION,
                *LEGACY_PROTOCOL_VERSIONS,
            ],
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"listChanged": True},
                "prompts": {"listChanged": True},
            },
            "serverInfo": {"name": "slm-mcp-hub", "version": VERSION},
            "instructions": (
                "Use search_tools to discover federated tools, then call_tool "
                "with the full namespaced tool name."
            ),
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
                "name": "search_tools",
                "description": (
                    f"Search across {total_tools} tools from {server_count} MCP servers. "
                    "Returns matching tool names, descriptions, server name, and full input schema. "
                    "Use this to find the right tool before calling it with call_tool. "
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
                "name": "call_tool",
                "description": (
                    "Call any tool from any connected MCP server. "
                    "First use search_tools to find the tool name and its parameters, "
                    "then call it here. The tool name must be the full namespaced name "
                    "from the search results (e.g., 'github__search_repositories')."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "Full tool name from search_tools results (e.g., 'context7__query-docs')",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments to pass to the tool — see inputSchema from search_tools",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["tool"],
                },
            },
            {
                "name": "list_servers",
                "description": (
                    f"List all {server_count} connected MCP servers with their tool counts. "
                    "Use to understand what's available before searching."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

        return {"tools": meta_tools}

    _META_TOOL_ALIASES = {
        "hub__search_tools": "search_tools",
        "hub__call_tool": "call_tool",
        "hub__list_servers": "list_servers",
    }

    async def _handle_meta_tool(
        self, name: str, arguments: dict[str, Any], session_id: str = "",
    ) -> dict[str, Any]:
        """Handle Meta-MCP hub meta-tools."""
        name = self._META_TOOL_ALIASES.get(name, name)
        if name == "search_tools":
            return await self._meta_search_tools(arguments)

        if name == "call_tool":
            return await self._meta_call_tool(arguments, session_id)

        if name == "list_servers":
            return await self._meta_list_servers()

        return {"content": [{"type": "text", "text": f"Unknown meta-tool: {name}"}], "isError": True}

    def _client_name(self, session_id: str) -> str:
        """Best-effort client name for operator-facing logs.

        Nothing validates clientInfo.name, so a session can carry a non-string
        name. This runs on the repair path, which must never fail a call.
        """
        session = self._session_manager.get_session(session_id) if session_id else None
        name = session.client_name if session else None
        if not isinstance(name, str):
            return "unknown"
        return name.strip() or "unknown"

    async def _meta_search_tools(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Search tools — returns names, descriptions, server, AND full inputSchema.

        Smart matching: splits query into words, matches if ALL words
        appear in the tool name, description, or server name.
        'github search' matches 'github__search_repositories'.
        """
        query = arguments.get("query")
        if query is None:
            query = ""
        if not isinstance(query, str):
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error: 'query' must be a string, got {type(query).__name__}.",
                }],
                "isError": True,
            }
        query = query.lower()
        query_words = query.split()
        all_tools = self._registry.list_tools()

        matches = []
        for t in all_tools:
            name = (t.get("name") or "").lower()
            desc = (t.get("description") or "").lower()
            searchable = f"{name} {desc} {name.replace('__', ' ').replace('_', ' ')}"
            if all(word in searchable for word in query_words):
                server = t["name"].split("__", 1)[0] if "__" in t["name"] else "unknown"
                matches.append({
                    "tool": t["name"],
                    "server": server,
                    "description": t.get("description") or "",
                    "inputSchema": t.get("inputSchema") or {},
                })

        result = {
            "found": len(matches),
            "query": query,
            "tools": matches[:30],
        }
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

    async def _meta_call_tool(
        self, arguments: dict[str, Any], session_id: str = "",
    ) -> dict[str, Any]:
        """Call any tool through the hub — the universal tool router."""
        arguments, repaired, skipped = _reconstruct_dotted_arguments(arguments)
        if repaired or skipped:
            logger.warning(
                "Client %r flattened call_tool arguments for %r into dot-notation keys; "
                "repaired %s, skipped %s",
                self._client_name(session_id), arguments.get("tool"), repaired, skipped,
            )

        tool_name = arguments.get("tool")

        if not isinstance(tool_name, str) or not tool_name.strip():
            got = "missing" if tool_name is None else f"got {type(tool_name).__name__}"
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error: 'tool' parameter is required and must be a non-empty string ({got}). "
                            "Use search_tools to find tool names.",
                }],
                "isError": True,
            }
        tool_name = tool_name.strip()

        tool_args, arg_error = _normalise_tool_arguments(arguments)
        if arg_error is not None:
            return {
                "content": [{"type": "text", "text": f"Error calling '{tool_name}': {arg_error}"}],
                "isError": True,
            }

        # Bug C fix: handle meta-tool calls locally instead of routing
        # through the federation router (which doesn't know about meta-tools).
        resolved = self._META_TOOL_ALIASES.get(tool_name, tool_name)
        if resolved == "call_tool":
            # Self-routing adds no capability and is unbounded if allowed.
            return {
                "content": [{
                    "type": "text",
                    "text": "Error: call_tool cannot invoke itself. Set 'tool' to the "
                            "name of the tool you want to run.",
                }],
                "isError": True,
            }
        if resolved in ("search_tools", "list_servers"):
            return await self._handle_meta_tool(resolved, tool_args)

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
        if not isinstance(name, str):
            raise InvalidParams(f"'name' must be a string, got {type(name).__name__}")
        if not name.strip():
            raise InvalidParams("'name' is required")
        name = name.strip()

        arguments, arg_error = _coerce_object(params.get("arguments"))
        if arg_error is not None:
            return {
                "content": [{"type": "text", "text": f"Error calling '{name}': 'arguments' is invalid: {arg_error}"}],
                "isError": True,
            }
        name = self._META_TOOL_ALIASES.get(name, name)

        # Handle Meta-MCP tools locally (including unknown hub__ names)
        if name.startswith("hub__") or name in ("search_tools", "call_tool", "list_servers"):
            return await self._handle_meta_tool(name, arguments, session_id)

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
            "server/discover": self.handle_server_discover,
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

        if params is None:
            params = {}
        if not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32602,
                    "message": f"Invalid params: 'params' must be an object, got {type(params).__name__}",
                },
            }

        try:
            result = await handler(session_id, params)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except InvalidParams as exc:
            logger.debug("Invalid params for %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": f"Invalid params: {exc}"},
            }
        except Exception as exc:
            logger.error("Handler error for %s (%s)", method, type(exc).__name__)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": "Internal server error"},
            }
