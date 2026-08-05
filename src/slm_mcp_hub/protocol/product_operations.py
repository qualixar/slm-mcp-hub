"""Transport-neutral Hub product operations.

``HubProductOperations`` owns all federation business logic. Returns typed
neutral objects from ``protocol.models`` — never raw wire dicts or SDK types.

Exports hardened argument-parsing utilities for backward compat with existing
tests that import them from ``server/mcp_endpoint.py``.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from mcp.shared.dispatcher import ProgressFnT

from slm_mcp_hub.core.constants import (
    MCP_LEGACY_PROTOCOL_VERSIONS,
    MCP_MODERN_PROTOCOL_VERSION,
    VERSION,
)
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter
from slm_mcp_hub.protocol.models import (
    CallToolOutcome,
    DiscoverOutcome,
    InitializeOutcome,
    PromptGetOutcome,
    PromptsListOutcome,
    ResourceReadOutcome,
    ResourcesListOutcome,
    ResourceTemplatesListOutcome,
    ToolsListOutcome,
)

if TYPE_CHECKING:
    from slm_mcp_hub.core.hub import HubOrchestrator

logger = logging.getLogger(__name__)

# Single-sourced from core/constants.py (W8-P6).
_MODERN_VERSION = MCP_MODERN_PROTOCOL_VERSION
# Ordered newest-first for protocol selection (index 0 = preferred negotiated version).
_LEGACY_VERSIONS: tuple[str, ...] = tuple(
    sorted(MCP_LEGACY_PROTOCOL_VERSIONS, reverse=True)
)
_RESERVED_META_KEYS = frozenset({"tool", "arguments"})  # call_tool envelope keys
_FLAT_ARG_PREFIX = "arguments."  # dotted-arg prefix used by some model clients


class InvalidParams(ValueError):
    """Structurally invalid client params — reported as JSON-RPC -32602."""


# --- Hardened argument-repair utilities (public for re-export) ---

def _coerce_object(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Coerce a JSON-object-ish value into a dict.

    Returns (object, error_message); exactly one is non-None.
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
            return None, f"it is a string but not valid JSON ({exc})"
        if value is None:
            return {}, None
    if isinstance(value, dict):
        return value, None
    return None, f"expected an object, got {type(value).__name__}"


def _normalise_tool_arguments(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the target tool's arguments from a call_tool payload. Explicit 'arguments' always wins."""
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
    """Deeply copy nested dicts so reconstruction cannot mutate the caller."""
    return {
        k: _copy_nested_dicts(v) if isinstance(v, dict) else v
        for k, v in value.items()
    }


def _descend(root: dict[str, Any], segments: list[str]) -> dict[str, Any] | None:
    """Walk to the container for the final segment; returns None on non-object values."""
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
    """Rebuild ``arguments.``-prefixed keys into nested form. Returns (payload, repaired, skipped)."""
    dotted = sorted(k for k in payload if k.startswith(_FLAT_ARG_PREFIX))
    if not dotted:
        return payload, [], []

    stripped = {k: v for k, v in payload.items() if not k.startswith(_FLAT_ARG_PREFIX)}

    existing = payload.get("arguments")
    if existing is not None and not isinstance(existing, dict):
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
            for key in [k for k in stripped if k not in _RESERVED_META_KEYS]:
                rebuilt.setdefault(key, stripped.pop(key))
        stripped["arguments"] = rebuilt
    return stripped, repaired, skipped


# --- Meta-tool wire schemas (static; descriptions reference live counts) ---

_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "Search keyword — matches tool names and descriptions"}},
    "required": ["query"],
}
_CALL_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "description": "Full tool name from search_tools results (e.g., 'context7__query-docs')"},
        "arguments": {"type": "object", "description": "Arguments to pass to the tool — see inputSchema from search_tools", "additionalProperties": True},
    },
    "required": ["tool"],
}
_LIST_SERVERS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class HubProductOperations:
    """Federation business operations — returns typed neutral objects; never raw wire dicts or SDK types."""

    _META_TOOL_ALIASES: dict[str, str] = {
        "hub__search_tools": "search_tools",
        "hub__call_tool": "call_tool",
        "hub__list_servers": "list_servers",
    }

    def __init__(
        self,
        registry: CapabilityRegistry,
        router: FederationRouter,
        hub: HubOrchestrator | None = None,
    ) -> None:
        self._registry = registry
        self._router = router
        self._hub = hub

    # --- Capability advertisement ---

    async def discover(self) -> DiscoverOutcome:
        """Handle server/discover — MCP 2026-07-28 capability advertisement."""
        return DiscoverOutcome(
            supported_versions=(_MODERN_VERSION, *_LEGACY_VERSIONS),
            capabilities={
                "tools": {"listChanged": True},
                "resources": {"listChanged": True},
                "prompts": {"listChanged": True},
            },
            server_name="slm-mcp-hub",
            server_version=VERSION,
            instructions=(
                "Use search_tools to discover federated tools, then call_tool "
                "with the full namespaced tool name."
            ),
        )

    async def negotiate(
        self, requested_version: str | None, client_info: dict[str, Any]
    ) -> InitializeOutcome:
        """Handle initialize — negotiate the protocol version."""
        client_name = (client_info or {}).get("name", "unknown")
        logger.debug("MCP client initializing: %s, requested=%s", client_name, requested_version)
        negotiated = (
            requested_version
            if requested_version in _LEGACY_VERSIONS
            else _LEGACY_VERSIONS[0]
        )
        return InitializeOutcome(
            protocol_version=negotiated,
            capabilities={
                "tools": {"listChanged": True},
                "resources": {"listChanged": True},
                "prompts": {"listChanged": True},
            },
            server_name="slm-mcp-hub",
            server_version=VERSION,
        )

    # --- Tools ---

    async def list_tools(self) -> ToolsListOutcome:
        """Return the three hub meta-tools (Meta-MCP pattern)."""
        total_tools = self._registry.tool_count
        server_count = len({
            t["name"].split("__", 1)[0]
            for t in self._registry.list_tools()
            if "__" in t["name"]
        })
        meta_tools: tuple[dict[str, Any], ...] = (
            {
                "name": "search_tools",
                "description": (
                    f"Search across {total_tools} tools from {server_count} MCP servers. "
                    "Returns matching tool names, descriptions, server name, and full input schema. "
                    "Use this to find the right tool before calling it with call_tool. "
                    "Example queries: 'github search', 'generate image', 'database query', 'memory recall'."
                ),
                "inputSchema": copy.deepcopy(_SEARCH_SCHEMA),
            },
            {
                "name": "call_tool",
                "description": (
                    "Call any tool from any connected MCP server. "
                    "First use search_tools to find the tool name and its parameters, "
                    "then call it here. The tool name must be the full namespaced name "
                    "from the search results (e.g., 'github__search_repositories')."
                ),
                "inputSchema": copy.deepcopy(_CALL_TOOL_SCHEMA),
            },
            {
                "name": "list_servers",
                "description": (
                    f"List all {server_count} connected MCP servers with their tool counts. "
                    "Use to understand what's available before searching."
                ),
                "inputSchema": copy.deepcopy(_LIST_SERVERS_SCHEMA),
            },
        )
        return ToolsListOutcome(tools=meta_tools)

    async def search_tools(self, arguments: dict[str, Any]) -> CallToolOutcome:
        """Search federated tools by keyword."""
        query = arguments.get("query")
        if query is None:
            query = ""
        if not isinstance(query, str):
            return CallToolOutcome(
                content=({"type": "text", "text": f"Error: 'query' must be a string, got {type(query).__name__}."},),
                is_error=True,
                server_name="hub",
            )
        query = query.lower()
        query_words = query.split()
        all_tools = self._registry.list_tools()

        matches = []
        for tool in all_tools:
            name = (tool.get("name") or "").lower()
            desc = (tool.get("description") or "").lower()
            searchable = f"{name} {desc} {name.replace('__', ' ').replace('_', ' ')}"
            if all(word in searchable for word in query_words):
                server = tool["name"].split("__", 1)[0] if "__" in tool["name"] else "unknown"
                matches.append({
                    "tool": tool["name"],
                    "server": server,
                    "description": tool.get("description") or "",
                    "inputSchema": tool.get("inputSchema") or {},
                })

        result = {"found": len(matches), "query": query, "tools": matches[:30]}
        return CallToolOutcome(
            content=({"type": "text", "text": json.dumps(result, indent=2)},),
            is_error=False,
            server_name="hub",
        )

    async def list_servers(self) -> CallToolOutcome:
        """List all connected servers with tool counts."""
        server_tools: dict[str, list[str]] = {}
        for tool in self._registry.list_tools():
            parts = tool["name"].split("__", 1)
            if len(parts) == 2:
                server_tools.setdefault(parts[0], []).append(parts[1])

        servers = [
            {"server": name, "tools": len(tools), "tool_names": sorted(tools)}
            for name, tools in sorted(server_tools.items())
        ]
        result = {"server_count": len(servers), "servers": servers}
        return CallToolOutcome(
            content=({"type": "text", "text": json.dumps(result, indent=2)},),
            is_error=False,
            server_name="hub",
        )

    async def call_tool(
        self,
        payload: dict[str, Any],
        session_id: str,
        client_name: str,
        *,
        progress_callback: ProgressFnT | None = None,
    ) -> CallToolOutcome:
        """Handle the meta call_tool invocation: repair dotted keys, validate, route, notify plugins."""
        payload, repaired, skipped = _reconstruct_dotted_arguments(payload)
        if repaired or skipped:
            logger.warning(
                "Client %r flattened call_tool arguments for %r into dot-notation keys; "
                "repaired %s, skipped %s",
                client_name, payload.get("tool"), repaired, skipped,
            )

        tool_name = payload.get("tool")
        if not isinstance(tool_name, str) or not tool_name.strip():
            got = "missing" if tool_name is None else f"got {type(tool_name).__name__}"
            return CallToolOutcome(
                content=({"type": "text", "text": (
                    f"Error: 'tool' parameter is required and must be a non-empty string ({got}). "
                    "Use search_tools to find tool names."
                )},),
                is_error=True,
                server_name="hub",
            )
        tool_name = tool_name.strip()

        tool_args, arg_error = _normalise_tool_arguments(payload)
        if arg_error is not None:
            return CallToolOutcome(
                content=({"type": "text", "text": f"Error calling '{tool_name}': {arg_error}"},),
                is_error=True,
                server_name="hub",
            )
        assert tool_args is not None  # _normalise_tool_arguments contract

        # Guard: self-routing is unbounded; reject it.
        resolved = self._META_TOOL_ALIASES.get(tool_name, tool_name)
        if resolved == "call_tool":
            return CallToolOutcome(
                content=({"type": "text", "text": (
                    "Error: call_tool cannot invoke itself. Set 'tool' to the "
                    "name of the tool you want to run."
                )},),
                is_error=True,
                server_name="hub",
            )
        if resolved in ("search_tools", "list_servers"):
            return await self._dispatch_meta(
                resolved, tool_args, session_id, client_name,
                progress_callback=progress_callback,
            )

        return await self._route_tool(
            tool_name, tool_args, session_id,
            notify_plugins=True,
            progress_callback=progress_callback,
        )

    async def _dispatch_meta(
        self,
        name: str,
        arguments: dict[str, Any],
        session_id: str,
        client_name: str,
        *,
        progress_callback: ProgressFnT | None = None,
    ) -> CallToolOutcome:
        """Dispatch a resolved meta-tool name to its handler."""
        if name == "search_tools":
            return await self.search_tools(arguments)
        if name == "list_servers":
            return await self.list_servers()
        if name == "call_tool":
            return await self.call_tool(
                arguments, session_id, client_name,
                progress_callback=progress_callback,
            )
        return CallToolOutcome(
            content=({"type": "text", "text": f"Unknown meta-tool: {name}"},),
            is_error=True,
            server_name="hub",
        )

    async def handle_meta_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        session_id: str,
        client_name: str,
        *,
        progress_callback: ProgressFnT | None = None,
    ) -> CallToolOutcome:
        """Dispatch a (possibly-aliased) meta-tool name; called by MCPEndpoint.handle_tools_call."""
        resolved = self._META_TOOL_ALIASES.get(name, name)
        return await self._dispatch_meta(
            resolved, arguments, session_id, client_name,
            progress_callback=progress_callback,
        )

    async def route_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        session_id: str,
        *,
        progress_callback: ProgressFnT | None = None,
    ) -> CallToolOutcome:
        """Route a direct namespaced tool call — no plugin notification."""
        return await self._route_tool(
            name, arguments, session_id,
            notify_plugins=False,
            progress_callback=progress_callback,
        )

    async def _route_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        session_id: str,
        notify_plugins: bool,
        *,
        progress_callback: ProgressFnT | None = None,
    ) -> CallToolOutcome:
        """Internal routing helper with optional plugin notification.

        Critical invariant (W8-P1 test compat):
        When progress_callback is None, router.route_tool_call is called with
        ONLY positional args (name, arguments) — no extra kwargs. This preserves
        the test assertions in test_product_operations.py and test_call_tool_arguments.py:
            router.route_tool_call.assert_awaited_once_with(name, args)
        """
        start = time.time()
        if progress_callback is not None:
            result = await self._router.route_tool_call(
                name, arguments, progress_callback=progress_callback
            )
        else:
            result = await self._router.route_tool_call(name, arguments)
        duration_ms = int((time.time() - start) * 1000)

        logger.debug(
            "Tool call: %s → %s (%dms, success=%s)",
            name, result.server_name, duration_ms, result.success,
        )

        if notify_plugins and self._hub:
            short_name = name.split("__", 1)[-1] if "__" in name else name
            try:
                await self._hub.notify_plugins_tool_call_after(
                    session_id="federated",
                    server=result.server_name,
                    tool=short_name,
                    args=arguments,
                    result=result.result,
                    duration_ms=duration_ms,
                    success=result.success,
                )
            except Exception as exc:
                logger.debug("Plugin notification failed: %s", exc)

        raw = result.result if isinstance(result.result, dict) else None
        content = tuple(raw.get("content", [])) if raw is not None else ()
        is_error = bool(raw.get("isError", False)) if raw is not None else False
        return CallToolOutcome(
            content=content,
            is_error=is_error,
            server_name=result.server_name,
            raw=raw,
        )

    # --- Resources ---

    async def list_resources(self) -> ResourcesListOutcome:
        """Return all namespaced resources from the registry."""
        return ResourcesListOutcome(
            resources=tuple(self._registry.list_resources())
        )

    async def read_resource(self, uri: str) -> ResourceReadOutcome:
        """Route a resource read and return the raw result."""
        result = await self._router.route_resource_read(uri)
        return ResourceReadOutcome(raw=result.result)

    async def list_resource_templates(self) -> ResourceTemplatesListOutcome:
        """Return all namespaced resource templates from the registry."""
        return ResourceTemplatesListOutcome(
            resource_templates=tuple(self._registry.list_resource_templates())
        )

    # --- Prompts ---

    async def list_prompts(self) -> PromptsListOutcome:
        """Return all namespaced prompts from the registry."""
        return PromptsListOutcome(
            prompts=tuple(self._registry.list_prompts())
        )

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> PromptGetOutcome:
        """Route a prompt get and return the raw result."""
        result = await self._router.route_prompt_get(name, arguments)
        return PromptGetOutcome(raw=result.result)
