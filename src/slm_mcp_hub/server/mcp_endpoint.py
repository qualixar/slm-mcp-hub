"""MCP Endpoint — thin JSON-RPC transport shim over HubProductOperations.

Each connected client gets its own MCPEndpoint instance.
All instances share the same HubProductOperations (and therefore the same
federation registry and router).

Business logic lives in ``protocol.product_operations.HubProductOperations``.
Wire formatting lives in ``protocol.conversion``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter
from slm_mcp_hub.protocol.conversion import (
    call_tool_outcome_to_wire,
    discover_to_wire,
    initialize_to_wire,
    prompt_get_to_wire,
    prompts_list_to_wire,
    resource_read_to_wire,
    resource_templates_list_to_wire,
    resources_list_to_wire,
    tools_list_to_wire,
)

# Re-export hardened argument utilities so existing tests can import them
# from this module without change.
from slm_mcp_hub.protocol.product_operations import (
    HubProductOperations,
    InvalidParams,
    _coerce_object,  # noqa: F401 — re-exported for test backward compat
    _normalise_tool_arguments,  # noqa: F401
    _reconstruct_dotted_arguments,  # noqa: F401
)
from slm_mcp_hub.session.manager import SessionManager

if TYPE_CHECKING:
    from slm_mcp_hub.core.hub import HubOrchestrator

logger = logging.getLogger(__name__)


class MCPEndpoint:
    """Federated MCP endpoint — JSON-RPC dispatch and session bookkeeping.

    Delegates all business logic to ``HubProductOperations`` and converts
    typed neutral outcomes to wire-format dicts for the transport layer.
    """

    _META_TOOL_ALIASES: dict[str, str] = {
        "hub__search_tools": "search_tools",
        "hub__call_tool": "call_tool",
        "hub__list_servers": "list_servers",
    }

    def __init__(
        self,
        registry: CapabilityRegistry,
        router: FederationRouter,
        session_manager: SessionManager,
        hub: HubOrchestrator | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._router = router
        self._ops = HubProductOperations(registry=registry, router=router, hub=hub)

    # --- Properties forwarded to ops for backward compat with existing tests ---

    @property
    def _hub(self) -> HubOrchestrator | None:
        return self._ops._hub

    @_hub.setter
    def _hub(self, value: HubOrchestrator | None) -> None:
        self._ops._hub = value

    @property
    def _registry(self) -> CapabilityRegistry:
        return self._ops._registry

    # --- Internal helpers ---

    def _client_name(self, session_id: str) -> str:
        """Best-effort client name for logging; never raises."""
        session = self._session_manager.get_session(session_id) if session_id else None
        name = session.client_name if session else None
        if not isinstance(name, str):
            return "unknown"
        return name.strip() or "unknown"

    # --- Backward-compat shim used by test_audit_fixes.py ---

    async def _handle_meta_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        session_id: str = "",
    ) -> dict[str, Any]:
        """Dispatch a meta-tool by name and return a wire-format dict."""
        outcome = await self._ops.handle_meta_tool(
            name=name,
            arguments=arguments,
            session_id=session_id,
            client_name=self._client_name(session_id),
        )
        return call_tool_outcome_to_wire(outcome)

    # --- MCP handlers ---

    async def handle_initialize(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle MCP initialize request."""
        client_info = params.get("clientInfo") or {}
        session = self._session_manager.get_session(session_id)
        if session:
            client_name = client_info.get("name", "unknown")
            logger.info(
                "MCP client initialized: %s (session %s)", client_name, session_id[:8]
            )
        outcome = await self._ops.negotiate(
            requested_version=params.get("protocolVersion"),
            client_info=client_info,
        )
        return initialize_to_wire(outcome)

    async def handle_server_discover(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle server/discover — MCP 2026-07-28 capability advertisement."""
        outcome = await self._ops.discover()
        return discover_to_wire(outcome)

    async def handle_tools_list(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle tools/list — returns the three hub meta-tools."""
        self._session_manager.touch(session_id)
        outcome = await self._ops.list_tools()
        return tools_list_to_wire(outcome)

    async def handle_tools_call(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle tools/call — route to meta-tool handler or federation router."""
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
        assert arguments is not None

        name = self._META_TOOL_ALIASES.get(name, name)

        if name.startswith("hub__") or name in ("search_tools", "call_tool", "list_servers"):
            outcome = await self._ops.handle_meta_tool(
                name=name,
                arguments=arguments,
                session_id=session_id,
                client_name=self._client_name(session_id),
            )
            return call_tool_outcome_to_wire(outcome)

        # Direct namespaced tool route — no plugin notification
        outcome = await self._ops.route_tool(name, arguments, session_id)
        return call_tool_outcome_to_wire(outcome)

    async def handle_resources_list(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle resources/list."""
        self._session_manager.touch(session_id)
        return resources_list_to_wire(await self._ops.list_resources())

    async def handle_resources_read(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle resources/read."""
        self._session_manager.touch(session_id)
        uri = params.get("uri", "")
        return resource_read_to_wire(await self._ops.read_resource(uri))

    async def handle_resources_templates_list(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle resources/templates/list."""
        self._session_manager.touch(session_id)
        return resource_templates_list_to_wire(await self._ops.list_resource_templates())

    async def handle_prompts_list(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle prompts/list."""
        self._session_manager.touch(session_id)
        return prompts_list_to_wire(await self._ops.list_prompts())

    async def handle_prompts_get(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle prompts/get."""
        self._session_manager.touch(session_id)
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        return prompt_get_to_wire(await self._ops.get_prompt(name, arguments))

    async def handle_jsonrpc(
        self, session_id: str, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Dispatch a JSON-RPC message to the appropriate handler.

        Returns a JSON-RPC response dict, or None for notifications.
        """
        method = message.get("method", "")
        params = message.get("params", {})
        msg_id = message.get("id")

        if msg_id is None:
            return None  # notification — acknowledge silently

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
