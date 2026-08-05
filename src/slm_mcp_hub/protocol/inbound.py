"""Inbound SDK server adapter.

Wraps ``HubProductOperations`` in an official ``mcp.server.lowlevel.Server``
so the Hub can speak the MCP 2026-07-28 wire protocol without hand-rolling
JSON-RPC dispatch. Handlers delegate entirely to ``HubProductOperations`` for
business logic; this module only converts between SDK types and the transport-
neutral models from ``protocol.models``.

Usage (HTTP)::

    sdk_server = build_sdk_server(ops)
    asgi_app = sdk_server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        host="127.0.0.1",
    )
    fastapi_app.mount("/mcp", asgi_app)

Usage (stdio)::

    sdk_server = build_sdk_server(ops)
    async with mcp.server.stdio.stdio_server() as (read_s, write_s):
        await sdk_server.run(read_s, write_s, sdk_server.create_initialization_options())

Security invariants:
- No tokens, secrets, or auth headers stored in this module.
- Handler errors are surfaced as SDK ``CallToolResult(is_error=True)`` — no
  internal stack traces are forwarded to the client.
- Session IDs are ephemeral UUIDs derived per-request; no persistent mapping.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

import mcp.types as t
from mcp.server.lowlevel import Server

from slm_mcp_hub.core.constants import VERSION
from slm_mcp_hub.protocol.conversion import (
    call_tool_outcome_to_sdk,
    prompt_get_to_sdk,
    resource_read_to_sdk,
    tools_list_to_sdk,
)
from slm_mcp_hub.protocol.product_operations import HubProductOperations
from slm_mcp_hub.streaming.progress import make_progress_bridge

if TYPE_CHECKING:
    from mcp.server.context import ServerRequestContext

logger = logging.getLogger(__name__)

_SERVER_NAME = "slm-mcp-hub"

# Meta-tool aliases mirror MCPEndpoint so behaviour is equivalent across transports.
_META_TOOL_ALIASES: dict[str, str] = {
    "hub__search_tools": "search_tools",
    "hub__call_tool": "call_tool",
    "hub__list_servers": "list_servers",
}
_META_TOOL_NAMES: frozenset[str] = frozenset({"search_tools", "call_tool", "list_servers"})


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _extract_session_id(ctx: ServerRequestContext) -> str:  # type: ignore[type-arg]
    """Return a stable session identifier from the request context.

    Prefers the SDK connection's session_id (set by StreamableHTTP manager).
    Falls back to a per-call UUID so callers always receive a non-empty string.
    """
    try:
        conn_id: str | None = ctx.session._connection.session_id
        if conn_id:
            return conn_id
    except AttributeError:
        pass
    return f"sdk-{uuid.uuid4().hex[:8]}"


def _extract_client_name(ctx: ServerRequestContext) -> str:  # type: ignore[type-arg]
    """Return the client's self-reported name or a safe default.

    ``client_params`` is populated by the handshake (legacy) or by the
    request envelope's ``clientInfo`` field (2026 modern). Either may be
    absent for genuinely anonymous clients.
    """
    try:
        params = ctx.session.client_params  # InitializeRequestParams | None
        if params is not None and params.client_info is not None:
            name = params.client_info.name
            if name:
                return name
    except Exception:  # noqa: BLE001 — defensive; never crash on attribution
        pass
    return "sdk-client"


# ---------------------------------------------------------------------------
# Resource / template / prompt converters (inline — not in conversion.py)
# ---------------------------------------------------------------------------

def _resources_list_to_sdk(
    resources: tuple[dict[str, Any], ...],
) -> t.ListResourcesResult:
    """Convert neutral resource dicts to an SDK ``ListResourcesResult``."""
    sdk_resources = [
        t.Resource(
            name=r.get("name") or r.get("uri", "unknown"),
            uri=r.get("uri", ""),
            title=r.get("title"),
            description=r.get("description"),
            mime_type=r.get("mimeType"),
        )
        for r in resources
    ]
    return t.ListResourcesResult(resources=sdk_resources)


def _resource_templates_list_to_sdk(
    templates: tuple[dict[str, Any], ...],
) -> t.ListResourceTemplatesResult:
    """Convert neutral template dicts to an SDK ``ListResourceTemplatesResult``."""
    sdk_templates = [
        t.ResourceTemplate(
            name=tmpl.get("name") or tmpl.get("uriTemplate", "unknown"),
            uri_template=tmpl.get("uriTemplate", ""),
            title=tmpl.get("title"),
            description=tmpl.get("description"),
            mime_type=tmpl.get("mimeType"),
        )
        for tmpl in templates
    ]
    return t.ListResourceTemplatesResult(resource_templates=sdk_templates)


def _prompt_arguments_to_sdk(
    arguments: list[dict[str, Any]] | None,
) -> list[t.PromptArgument] | None:
    """Convert upstream prompt-argument dicts to SDK ``PromptArgument`` objects.

    Preserving ``arguments`` is required: without it a downstream client cannot
    discover which parameters a federated prompt needs.
    """
    if not arguments:
        return None
    return [
        t.PromptArgument(
            name=arg["name"],
            title=arg.get("title"),
            description=arg.get("description"),
            required=arg.get("required"),
        )
        for arg in arguments
    ]


def _prompts_list_to_sdk(
    prompts: tuple[dict[str, Any], ...],
) -> t.ListPromptsResult:
    """Convert neutral prompt dicts to an SDK ``ListPromptsResult``."""
    sdk_prompts = [
        t.Prompt(
            name=p["name"],
            title=p.get("title"),
            description=p.get("description"),
            arguments=_prompt_arguments_to_sdk(p.get("arguments")),
        )
        for p in prompts
    ]
    return t.ListPromptsResult(prompts=sdk_prompts)


# ---------------------------------------------------------------------------
# Progress token extraction (W8-P2)
# ---------------------------------------------------------------------------

def _extract_progress_token(
    meta: dict[str, Any] | None,
) -> str | int | None:
    """Extract progress_token from RequestParamsMeta, or None.

    Supports both snake_case (``progress_token``, documented in mcp/shared/peer.py:52)
    and camelCase (``progressToken``, alias for clients that send camelCase).
    snake_case takes precedence when both are present.

    bool values are explicitly rejected — bool is a subclass of int in Python, so
    ``isinstance(True, int)`` is True. The bool check MUST come before the int check
    to prevent treating True/False as valid integer tokens.

    Args:
        meta: The raw _meta dict from the MCP request context, or None.

    Returns:
        A str or int progress token, or None if absent/invalid.
    """
    if not meta:
        return None
    tok = meta.get("progress_token")
    if tok is None:
        tok = meta.get("progressToken")
    if tok is None:
        return None
    # bool ⊂ int: reject booleans explicitly before the int check.
    if isinstance(tok, bool):
        return None
    # MCP spec: progressToken is str | int only. Reject any other type (float,
    # list, dict, object) and empty strings — a client-supplied value must not
    # reach send_progress_notification unvalidated.
    if isinstance(tok, int):
        return tok
    if isinstance(tok, str) and tok:
        return tok
    return None


# ---------------------------------------------------------------------------
# build_sdk_server — public API
# ---------------------------------------------------------------------------

def build_sdk_server(ops: HubProductOperations) -> Server:  # type: ignore[type-arg]
    """Build an ``mcp.server.lowlevel.Server`` that delegates to *ops*.

    All business logic (tool routing, federation, meta-tools) lives in *ops*.
    This factory only wires handler callables and converts SDK↔neutral types.

    Args:
        ops: Transport-neutral product operations instance.

    Returns:
        A configured ``Server`` ready to be passed to
        ``streamable_http_app()`` or ``server.run()``.
    """

    async def on_list_tools(
        ctx: ServerRequestContext,  # type: ignore[type-arg]
        params: t.PaginatedRequestParams | None,
    ) -> t.ListToolsResult:
        outcome = await ops.list_tools()
        return tools_list_to_sdk(outcome)

    async def on_call_tool(
        ctx: ServerRequestContext,  # type: ignore[type-arg]
        params: t.CallToolRequestParams,
    ) -> t.CallToolResult:
        name: str = params.name
        arguments: dict[str, Any] = dict(params.arguments or {})
        session_id = _extract_session_id(ctx)
        client_name = _extract_client_name(ctx)

        # W8-P2: Extract progress token from request meta and build a ProgressBridge
        # so backend progress events are forwarded to the hub client.
        # Returns None when client did not send a progressToken (common case).
        # Guard: ctx.meta must be a real dict (RequestParamsMeta TypedDict).
        # In test mocks (MagicMock) ctx.meta auto-creates a MagicMock, not a dict.
        # isinstance(mock, dict) is False, so we treat non-dict meta as None.
        _raw_meta = getattr(ctx, "meta", None)
        raw_meta: dict[str, Any] | None = _raw_meta if isinstance(_raw_meta, dict) else None
        progress_token = _extract_progress_token(raw_meta)
        server_session = getattr(ctx, "session", None)
        request_id = getattr(ctx, "request_id", None)
        progress_cb = make_progress_bridge(server_session, progress_token, request_id)

        resolved = _META_TOOL_ALIASES.get(name, name)
        if resolved in _META_TOOL_NAMES:
            # Only pass progress_callback kwarg when not None.
            # This preserves backward-compat mock assertions that check
            # handle_meta_tool was called without extra kwargs.
            if progress_cb is not None:
                outcome = await ops.handle_meta_tool(
                    name=resolved,
                    arguments=arguments,
                    session_id=session_id,
                    client_name=client_name,
                    progress_callback=progress_cb,
                )
            else:
                outcome = await ops.handle_meta_tool(
                    name=resolved,
                    arguments=arguments,
                    session_id=session_id,
                    client_name=client_name,
                )
        else:
            # Only pass progress_callback kwarg when not None.
            # This preserves the backward-compat mock assertion:
            #   ops.route_tool.assert_awaited_once_with(name, args, session_id)
            # which uses no extra kwargs (test_mcp_2026_contract.py).
            if progress_cb is not None:
                outcome = await ops.route_tool(
                    name, arguments, session_id, progress_callback=progress_cb
                )
            else:
                outcome = await ops.route_tool(name, arguments, session_id)

        return call_tool_outcome_to_sdk(outcome)

    async def on_list_resources(
        ctx: ServerRequestContext,  # type: ignore[type-arg]
        params: t.PaginatedRequestParams | None,
    ) -> t.ListResourcesResult:
        outcome = await ops.list_resources()
        return _resources_list_to_sdk(outcome.resources)

    async def on_list_resource_templates(
        ctx: ServerRequestContext,  # type: ignore[type-arg]
        params: t.PaginatedRequestParams | None,
    ) -> t.ListResourceTemplatesResult:
        outcome = await ops.list_resource_templates()
        return _resource_templates_list_to_sdk(outcome.resource_templates)

    async def on_read_resource(
        ctx: ServerRequestContext,  # type: ignore[type-arg]
        params: t.ReadResourceRequestParams,
    ) -> t.ReadResourceResult:
        outcome = await ops.read_resource(str(params.uri))
        return resource_read_to_sdk(outcome)

    async def on_list_prompts(
        ctx: ServerRequestContext,  # type: ignore[type-arg]
        params: t.PaginatedRequestParams | None,
    ) -> t.ListPromptsResult:
        outcome = await ops.list_prompts()
        return _prompts_list_to_sdk(outcome.prompts)

    async def on_get_prompt(
        ctx: ServerRequestContext,  # type: ignore[type-arg]
        params: t.GetPromptRequestParams,
    ) -> t.GetPromptResult:
        outcome = await ops.get_prompt(
            params.name,
            dict(params.arguments or {}),
        )
        return prompt_get_to_sdk(outcome)

    return Server(
        name=_SERVER_NAME,
        version=VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_list_resource_templates=on_list_resource_templates,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )
