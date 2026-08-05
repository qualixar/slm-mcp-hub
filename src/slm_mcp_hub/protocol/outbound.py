"""P04/P06 — SDK-backed outbound MCP client.

Wraps ``mcp.Client(mode="auto")`` to connect to upstream MCP servers via
stdio or Streamable HTTP transports.  Replaces the hub's hand-rolled
JSON-RPC wire logic (request IDs, pending-future maps, SSE parsing) with
the official SDK for all upstream connections.

P06 extends this module to support OAuth Streamable HTTP:
- ``auth.mode == "oauth"`` builds an ``OAuthClientProvider`` via
  ``slm_mcp_hub.auth.broker.build_oauth_http_client``.
- When the SDK raises ``OAuthFlowError`` (runtime mode — no redirect handler),
  ``connect()`` raises ``OAuthAuthRequiredError`` instead of a generic
  ``ConnectionError``.  ``MCPConnection`` converts this to
  ``ConnectionState.AUTH_REQUIRED``.

Design contract:
- ``OutboundClient`` is stateless until ``connect()`` is called.
- ``connect()`` opens the SDK transport and holds it open via AsyncExitStack.
- ``disconnect()`` closes the stack cleanly.
- All public methods (call_tool, read_resource, get_prompt) raise
  ``ConnectionError`` if called before connect() or after disconnect().
- ``authorization_state`` reflects the OAuth mode/status (P06).
- SDK Pydantic result objects are converted to plain ``dict`` before return so
  callers never see SDK types.
- Inbound request headers (Authorization, Cookie) are NEVER forwarded upstream.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client
from mcp.client.auth import OAuthFlowError
from mcp.client.sse import sse_client
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from mcp.client.streamable_http import streamable_http_client

from slm_mcp_hub.auth.broker import OAuthAuthRequiredError, build_oauth_http_client
from slm_mcp_hub.auth.models import AuthOAuthConfig
from slm_mcp_hub.auth.token_store import KeyringTokenStorage
from slm_mcp_hub.core.config import MCPServerConfig, materialize_server_config
from slm_mcp_hub.protocol.models import AuthorizationState, NegotiatedPeer, ProtocolEra

logger = logging.getLogger(__name__)

_MODERN_PROTOCOL_VERSION = ProtocolEra.MODERN_2026.value  # "2026-07-28"
_NOT_REQUIRED_AUTH = AuthorizationState(
    mode="none",
    status="not_required",
    issuer=None,
    resource=None,
    scopes=(),
)
_AUTH_REQUIRED_STATE = AuthorizationState(
    mode="oauth",
    status="auth_required",
    issuer=None,
    resource=None,
    scopes=(),
)

# W6-P1: SSE default read timeout. Legacy SSE streams are long-lived; 300 s is
# consistent with the mcp SDK's own default for sse_client.
# TODO (W7 integration): resolve from TimeoutRegistry.get_policy(runtime.timeout_class)
#      when timeout_class == "unbounded", pass sse_read_timeout=None.
_SSE_DEFAULT_READ_TIMEOUT_S: float = 300.0


class OutboundClient:
    """SDK-backed outbound MCP client for one upstream server.

    Lifecycle::

        client = OutboundClient(config)
        await client.connect()           # opens transport + performs handshake
        result = await client.call_tool(...)
        await client.disconnect()        # closes transport cleanly

    Attributes exposed after connect():
        capabilities  — dict with tools/resources/resource_templates/prompts
        negotiated_peer — NegotiatedPeer frozen dataclass (era, version, caps)
        authorization_state — always "none/not_required" in P04
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self._connected = False
        self._capabilities: dict[str, Any] = {
            "tools": [],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }
        self._negotiated_peer: NegotiatedPeer | None = None
        self._auth_required = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def capabilities(self) -> dict[str, Any]:
        """Discovered tools/resources/resource_templates/prompts."""
        return self._capabilities

    @property
    def negotiated_peer(self) -> NegotiatedPeer | None:
        """Protocol era and version negotiated during connect()."""
        return self._negotiated_peer

    @property
    def process_pid(self) -> int | None:
        """Return the subprocess PID for stdio backends; None for HTTP/SSE.

        W5-P1 addition for RAM measurement via psutil in enrich_server_status().

        IMPORTANT: This reads through SDK internals (_client._transport._process.pid).
        This is a BEST-EFFORT path — the attribute chain may change across MCP SDK
        versions. Wrapped in try/except AttributeError so it always degrades to None
        gracefully.

        [SDK-VERIFIED-2.0.0]: The mcp.Client dataclass does not expose a _transport
        attribute directly in mcp==2.0.0. The AttributeError is caught and None is
        returned. This property will activate when the SDK exposes the PID through
        a stable path.

        Returns:
            int: PID of the stdio subprocess when available.
            None: For HTTP/SSE backends, when not connected, or when the SDK
                  internal path is unavailable.
        """
        if self._config.transport != "stdio" or self._client is None:
            return None
        try:
            return self._client._transport._process.pid  # type: ignore[attr-defined]
        except AttributeError:
            return None

    @property
    def authorization_state(self) -> AuthorizationState:
        """Return the current OAuth authorization state.

        Returns ``auth_required`` if the last ``connect()`` attempt ended
        because interactive OAuth was needed.  Returns ``not_required`` for
        non-OAuth servers or when a valid token is already cached.
        """
        if self._auth_required:
            return _AUTH_REQUIRED_STATE
        if isinstance(self._config.auth, AuthOAuthConfig):
            return AuthorizationState(
                mode="oauth",
                status="authorized" if self._connected else "not_required",
                issuer=None,
                resource=None,
                scopes=self._config.auth.scopes,
            )
        return _NOT_REQUIRED_AUTH

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open transport and perform MCP handshake via SDK Client.

        Raises:
            ConnectionError: If the server cannot be reached or the
                handshake fails.  Specific sub-cases:
                - "Command not found" when the stdio executable is missing.
                - "initialization failed" for protocol/network errors.
        """
        if self._connected:
            return

        runtime = materialize_server_config(self._config)

        # Build the SDK Client object (synchronous — no I/O here yet).
        try:
            client = self._build_client(runtime)
        except FileNotFoundError as exc:
            raise ConnectionError(
                f"Command not found for MCP {self._config.name}"
            ) from exc
        except Exception as exc:
            raise ConnectionError(
                f"MCP {self._config.name} initialization failed"
                f" ({type(exc).__name__})"
            ) from exc

        # Enter Client as async context manager — opens transport + handshake.
        stack = AsyncExitStack()
        try:
            await stack.__aenter__()
            connected_client = await stack.enter_async_context(client)
        except OAuthFlowError as exc:
            # SDK raised OAuthFlowError because redirect_handler is None
            # (runtime mode, interactive auth required).
            await stack.aclose()
            self._auth_required = True
            raise OAuthAuthRequiredError(
                f"OAuth authorization required for MCP {self._config.name}; "
                f"run: slm-hub auth login {self._config.name}"
            ) from exc
        except FileNotFoundError as exc:
            await stack.aclose()
            raise ConnectionError(
                f"Command not found for MCP {self._config.name}"
            ) from exc
        except OSError as exc:
            await stack.aclose()
            raise ConnectionError(
                f"MCP {self._config.name} initialization failed"
                f" ({type(exc).__name__})"
            ) from exc
        except Exception as exc:
            # Re-check: if the inner exception chain contains OAuthFlowError,
            # treat it as auth_required rather than a generic error.
            if isinstance(exc.__cause__, OAuthFlowError) or isinstance(
                exc.__context__, OAuthFlowError
            ):
                await stack.aclose()
                self._auth_required = True
                raise OAuthAuthRequiredError(
                    f"OAuth authorization required for MCP {self._config.name}; "
                    f"run: slm-hub auth login {self._config.name}"
                ) from exc
            await stack.aclose()
            raise ConnectionError(
                f"MCP {self._config.name} initialization failed"
                f" ({type(exc).__name__})"
            ) from exc

        self._client = connected_client
        self._stack = stack

        # Discover capabilities gated on server-advertised features.
        try:
            await self._discover_capabilities()
        except Exception as exc:  # discovery must not abort an otherwise good connection
            logger.warning(
                "Capability discovery failed for %s: %s", self._config.name, exc
            )

        self._negotiated_peer = self._capture_negotiated_peer()
        self._connected = True
        logger.info(
            "OutboundClient connected to %s via SDK (%d tools, %d resources, %d prompts)",
            self._config.name,
            len(self._capabilities["tools"]),
            len(self._capabilities["resources"]),
            len(self._capabilities["prompts"]),
        )

    async def disconnect(self) -> None:
        """Close the SDK transport and release all resources.

        Safe to call even if connect() was never called or already failed.
        """
        self._connected = False
        self._client = None

        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:
                logger.debug(
                    "Error during OutboundClient disconnect for %s: %s",
                    self._config.name, exc,
                )
            self._stack = None

    # ------------------------------------------------------------------
    # MCP operations
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a tool on the upstream server.

        Returns:
            Plain dict with ``content`` list and optional ``isError`` key.
        Raises:
            ConnectionError: If not connected.
        """
        if not self._connected or self._client is None:
            raise ConnectionError(
                f"Not connected to MCP {self._config.name}"
            )
        result = await self._client.call_tool(tool_name, arguments)
        return _serialize_call_tool_result(result)

    async def call_tool_streaming(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        read_timeout_seconds: float | None = None,
        progress_callback: Any | None = None,
        resumption_token: str | None = None,
        on_resumption_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Call a tool with progress, timeout, and resumption support.

        Uses client.session.send_request directly (NOT client.call_tool) so that
        ClientMessageMetadata (resumption_token, on_resumption_token_update) can be
        passed.  client.call_tool has no metadata parameter in mcp==2.0.0.

        SDK symbols used (verified in .venv):
          - ClientSession.send_request(request, result_type,
                request_read_timeout_seconds=None, metadata=None,
                progress_callback=None)
          - mcp.client.streamable_http.ClientMessageMetadata(
                resumption_token=None, on_resumption_token_update=None,
                headers=None)
          - mcp.types.CallToolRequest / CallToolResult / CallToolRequestParams

        Args:
            tool_name: Tool name on the upstream server (not namespaced).
            arguments: Tool arguments dict.
            read_timeout_seconds: Per-call read timeout.  None = wait forever.
            progress_callback: ProgressFnT-compatible callback invoked for each
                notifications/progress event from the backend.  None disables
                progress forwarding.
            resumption_token: Resumption token from a prior partial call (P3).
            on_resumption_token: Callback invoked by the SDK when the backend
                issues a new resumption token (P3).

        Raises:
            ConnectionError: If not connected.
            anyio.get_cancelled_exc_class(): If the enclosing CancelScope fires.
        """
        if not self._connected or self._client is None:
            raise ConnectionError(f"Not connected to MCP {self._config.name}")

        from mcp import types  # noqa: PLC0415

        metadata = None
        if resumption_token is not None or on_resumption_token is not None:
            from mcp.client.streamable_http import (
                ClientMessageMetadata,  # noqa: PLC0415
            )
            metadata = ClientMessageMetadata(
                resumption_token=resumption_token,
                on_resumption_token_update=on_resumption_token,
            )

        result = await self._client.session.send_request(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name=tool_name,
                    arguments=arguments,
                )
            ),
            types.CallToolResult,
            request_read_timeout_seconds=read_timeout_seconds,
            metadata=metadata,
            progress_callback=progress_callback,
        )
        return _serialize_call_tool_result(result)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read a resource from the upstream server.

        Returns:
            Plain dict with ``contents`` list.
        Raises:
            ConnectionError: If not connected.
        """
        if not self._connected or self._client is None:
            raise ConnectionError(
                f"Not connected to MCP {self._config.name}"
            )
        result = await self._client.read_resource(uri)
        return _serialize_read_resource_result(result)

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Get a prompt from the upstream server.

        Returns:
            Plain dict with ``messages`` list and optional ``description``.
        Raises:
            ConnectionError: If not connected.
        """
        if not self._connected or self._client is None:
            raise ConnectionError(
                f"Not connected to MCP {self._config.name}"
            )
        result = await self._client.get_prompt(name, dict(arguments))
        return _serialize_get_prompt_result(result)

    # ------------------------------------------------------------------
    # Internal: transport construction
    # ------------------------------------------------------------------

    def _build_client(self, runtime: MCPServerConfig) -> Client:
        """Construct the SDK Client for the configured transport.

        Dispatch:
          stdio → _build_stdio_client  (subprocess + process stdio)
          sse   → _build_sse_client    (legacy SSE: GET /sse + POST /messages/)
          http  → _build_http_client   (Streamable HTTP: POST /mcp; optional OAuth)
        """
        if runtime.transport == "stdio":
            return self._build_stdio_client(runtime)
        if runtime.transport == "sse":
            return self._build_sse_client(runtime)
        return self._build_http_client(runtime)

    def _build_stdio_client(self, runtime: MCPServerConfig) -> Client:
        """Build a Client wrapping stdio_client transport.

        Child processes receive only the restricted SDK default environment
        (HOME, LOGNAME, PATH, SHELL, USER) plus any explicit env in the
        server config.  Hub credentials and other process env vars are
        excluded to prevent inadvertent credential leakage.
        """
        env = dict(get_default_environment())
        env.update(runtime.env)
        params = StdioServerParameters(
            command=runtime.command,
            args=list(runtime.args),
            env=env,
        )
        return Client(stdio_client(params), mode="auto")

    def _build_sse_client(self, runtime: MCPServerConfig) -> Client:
        """Build a Client wrapping the legacy SSE transport (mcp.client.sse.sse_client).

        Legacy SSE protocol (2024-11-05):
          - GET {url}           → opens the SSE stream; server sends ``endpoint`` event
          - POST {messages_url} → client-to-server messages (URL from the endpoint event)

        The ``url`` in MCPServerConfig must point to the SSE stream endpoint
        (e.g., ``http://host:port/sse``), NOT the Streamable HTTP endpoint (``/mcp``).

        Static headers are forwarded to both the SSE GET and the POST messages requests.
        OAuth mode is NOT supported for legacy SSE (the mcp SDK's OAuth provider is
        Streamable-HTTP-only; the ``sse + oauth`` combination is rejected at config
        validation time by ``_validate_auth_transport_compatibility``). Use
        ``transport="http"`` for OAuth-authenticated backends.

        ``sse_read_timeout`` defaults to ``_SSE_DEFAULT_READ_TIMEOUT_S`` (300 s),
        matching the SDK default. The real SDK signature (mcp==2.0.0) is::

            sse_client(url, headers=None, timeout=5.0, sse_read_timeout=300.0, ...)

        TODO (W7): when ``TimeoutClass.UNBOUNDED`` is configured, pass
                   ``sse_read_timeout=None`` after resolving via
                   ``TimeoutRegistry.get_policy(runtime.timeout_class)``.
        """
        url = runtime.url
        headers: dict[str, Any] | None = dict(runtime.headers) if runtime.headers else None
        transport = sse_client(
            url=url,
            headers=headers,
            sse_read_timeout=_SSE_DEFAULT_READ_TIMEOUT_S,
        )
        return Client(transport, mode="auto")

    def _build_http_client(self, runtime: MCPServerConfig) -> Client:
        """Build a Client for Streamable HTTP transport.

        Three cases based on auth policy:
        1. **OAuth** (``auth.mode == "oauth"``): build an
           ``httpx2.AsyncClient`` with ``OAuthClientProvider`` as auth
           (runtime mode — no redirect handler). No static headers are
           allowed alongside OAuth (validated at config parse time).
        2. **Static headers**: construct ``httpx2.AsyncClient`` with the
           configured headers.
        3. **No auth, no headers**: use the simpler ``Client(url)`` path.

        Inbound Authorization/Cookie headers from downstream clients are
        NEVER forwarded — ``OutboundClient`` reads only from
        ``MCPServerConfig``, which contains no inbound request data.
        """
        url = runtime.url
        auth = runtime.auth

        if isinstance(auth, AuthOAuthConfig):
            # Build a KeyringTokenStorage for this server.
            # The redirect_uri in the account key uses port=0 (placeholder) so
            # the key is stable before a real CallbackServer is started.
            redirect_uri = f"http://{auth.callback_host}:{auth.callback_port}/callback"
            storage = KeyringTokenStorage(
                endpoint=url,
                redirect_uri=redirect_uri,
            )
            http_client = build_oauth_http_client(
                server_url=url,
                auth_config=auth,
                storage=storage,
            )
            transport = streamable_http_client(url, http_client=http_client)
            return Client(transport, mode="auto")

        if runtime.headers:
            import httpx2  # SDK dependency; already present via mcp[http] extra
            http_client = httpx2.AsyncClient(headers=dict(runtime.headers))
            transport = streamable_http_client(url, http_client=http_client)
            return Client(transport, mode="auto")

        return Client(url, mode="auto")

    # ------------------------------------------------------------------
    # Internal: post-connect discovery
    # ------------------------------------------------------------------

    async def _discover_capabilities(self) -> None:
        """Probe tools, resources, and prompts from the connected server.

        Resources and prompts probes are gated on what the server advertised
        in its initialize response (server_capabilities).  This mirrors the
        MCPConnection._discover_capabilities() gating logic and prevents
        hanging on servers that don't support optional methods.
        """
        assert self._client is not None

        # Tools: always probed (all MCP servers must expose tools capability).
        try:
            tools_result = await self._client.list_tools()
            self._capabilities["tools"] = [
                t.model_dump(by_alias=True, exclude_none=True)
                for t in tools_result.tools
            ]
        except Exception as exc:
            logger.warning(
                "Failed to list tools for %s: %s", self._config.name, exc
            )

        server_caps = self._client.server_capabilities
        caps_dict = server_caps.model_dump(by_alias=True, exclude_none=True)

        if "resources" in caps_dict:
            try:
                res_result = await self._client.list_resources()
                self._capabilities["resources"] = [
                    r.model_dump(by_alias=True, exclude_none=True)
                    for r in res_result.resources
                ]
            except Exception as exc:
                logger.debug("No resources for %s: %s", self._config.name, exc)

            try:
                tmpl_result = await self._client.list_resource_templates()
                self._capabilities["resource_templates"] = [
                    t.model_dump(by_alias=True, exclude_none=True)
                    for t in tmpl_result.resource_templates
                ]
            except Exception as exc:
                logger.debug(
                    "No resource templates for %s: %s", self._config.name, exc
                )
        else:
            logger.debug(
                "%s did not advertise resources — skipping discovery",
                self._config.name,
            )

        if "prompts" in caps_dict:
            try:
                prompts_result = await self._client.list_prompts()
                self._capabilities["prompts"] = [
                    p.model_dump(by_alias=True, exclude_none=True)
                    for p in prompts_result.prompts
                ]
            except Exception as exc:
                logger.debug("No prompts for %s: %s", self._config.name, exc)
        else:
            logger.debug(
                "%s did not advertise prompts — skipping discovery",
                self._config.name,
            )

    def _capture_negotiated_peer(self) -> NegotiatedPeer:
        """Build NegotiatedPeer from the SDK Client's post-handshake state.

        The negotiated protocol version is authoritative after a successful
        connect and is always reported as-is. A transient failure to serialize
        the (non-critical) capabilities must NEVER downgrade a modern peer to a
        fabricated "unknown"/legacy version — that would make the Hub advertise
        the wrong protocol era to downstream clients for a perfectly good peer.
        """
        assert self._client is not None
        proto_ver = self._client.protocol_version
        era = (
            ProtocolEra.MODERN_2026
            if proto_ver == _MODERN_PROTOCOL_VERSION
            else ProtocolEra.LEGACY
        )
        try:
            capabilities = self._client.server_capabilities.model_dump(
                by_alias=True, exclude_none=True
            )
        except Exception as exc:
            logger.debug(
                "Could not serialize server capabilities for %s: %s",
                self._config.name, exc,
            )
            capabilities = {}
        return NegotiatedPeer(
            era=era, protocol_version=proto_ver, capabilities=capabilities
        )


# ---------------------------------------------------------------------------
# SDK result serializers — convert Pydantic models to plain dicts
# ---------------------------------------------------------------------------

def _serialize_call_tool_result(result: Any) -> dict[str, Any]:
    """Convert SDK CallToolResult to a plain dict.

    Output shape: ``{"content": [...], "isError": True}`` (isError omitted when False).
    """
    content = [
        block.model_dump(by_alias=True, exclude_none=True)
        for block in result.content
    ]
    out: dict[str, Any] = {"content": content}
    if result.is_error:
        out["isError"] = True
    return out


def _serialize_read_resource_result(result: Any) -> dict[str, Any]:
    """Convert SDK ReadResourceResult to a plain dict.

    Output shape: ``{"contents": [...]}``.
    """
    contents = [
        c.model_dump(by_alias=True, exclude_none=True)
        for c in result.contents
    ]
    return {"contents": contents}


def _serialize_get_prompt_result(result: Any) -> dict[str, Any]:
    """Convert SDK GetPromptResult to a plain dict.

    Output shape: ``{"messages": [...], "description": "..."}`` (description omitted when None).
    """
    messages = [
        m.model_dump(by_alias=True, exclude_none=True)
        for m in result.messages
    ]
    out: dict[str, Any] = {"messages": messages}
    if result.description:
        out["description"] = result.description
    return out
