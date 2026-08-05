"""FastAPI HTTP server for SLM MCP Hub."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from slm_mcp_hub.core.constants import (
    API_PREFIX,
    MCP_ENDPOINT_PATH,
    MCP_LEGACY_PROTOCOL_VERSIONS,
    MCP_MODERN_PROTOCOL_VERSION,
    VERSION,
)
from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
from slm_mcp_hub.server.proxy_endpoint import ProxyEndpoint
from slm_mcp_hub.server.transport_mode import resolve_stateful
from slm_mcp_hub.session.manager import SessionManager

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server as SdkServer
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from slm_mcp_hub.core.config import HubConfig

logger = logging.getLogger(__name__)
# Single-sourced from core/constants.py (W8-P6); local aliases preserve existing usages.
MODERN_PROTOCOL_VERSION = MCP_MODERN_PROTOCOL_VERSION
LEGACY_PROTOCOL_VERSIONS = MCP_LEGACY_PROTOCOL_VERSIONS
LOOPBACK_ORIGIN_REGEX = r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$"


def _jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
    *,
    status_code: int = 400,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        status_code=status_code,
        content={"jsonrpc": "2.0", "id": request_id, "error": error},
    )


def _build_sdk_asgi(
    sdk_server: SdkServer,  # type: ignore[type-arg]
    *,
    host: str = "127.0.0.1",
    hub_config: HubConfig | None = None,  # W4-P3: pass to wire InMemoryEventStore
) -> tuple[Any, StreamableHTTPSessionManager]:
    """Create the SDK ASGI handler and session manager for the hub.

    Returns ``(asgi_handler, session_manager)`` where:
    - ``asgi_handler`` is a bare ``StreamableHTTPASGIApp`` (no Starlette wrapper,
      no embedded lifespan) that can be mounted at ``/mcp``.
    - ``session_manager`` is the ``StreamableHTTPSessionManager`` whose ``run()``
      context manager MUST be started in the parent FastAPI lifespan before any
      request is served.

    We bypass ``sdk_server.streamable_http_app()`` deliberately: that helper
    returns a *Starlette* sub-app whose lifespan starts the session manager, but
    Starlette does not propagate lifespan events to mounted sub-apps. Calling
    ``run()`` directly from the parent FastAPI lifespan avoids the double-start
    restriction (``run()`` raises ``RuntimeError`` if called twice).
    """
    from mcp.server.streamable_http_manager import (
        StreamableHTTPASGIApp,
        StreamableHTTPSessionManager,
    )
    from mcp.server.transport_security import TransportSecuritySettings

    # CRIT fix 1: always enable DNS rebinding protection for loopback hosts.
    # Non-loopback hosts (e.g. 0.0.0.0) get no security settings; the caller is
    # responsible for adding network-level protection (reverse proxy, firewall).
    # We log a warning so the security gap is never silent.
    _LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
    transport_security: TransportSecuritySettings | None = None
    if host in _LOOPBACK_HOSTS:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )
    else:
        logger.warning(
            "SDK ASGI built with non-loopback host %r — DNS rebinding protection "
            "is DISABLED. Ensure the endpoint is protected by a reverse proxy or "
            "network firewall.",
            host,
        )

    # W4-P3: wire InMemoryEventStore when hub_config enables it.
    # When hub_config is None (backward compat / existing tests), event_store=None
    # preserves pre-W4 behaviour.
    event_store = None
    if hub_config is not None and hub_config.event_store_enabled:
        from slm_mcp_hub.streaming.event_store import (  # noqa: PLC0415
            InMemoryEventStore,
        )
        event_store = InMemoryEventStore(
            max_events_per_stream=hub_config.event_store_max_events_per_stream,
            max_streams=hub_config.event_store_max_streams,
            stream_ttl_s=hub_config.event_store_stream_ttl_s,
        )

    session_manager: StreamableHTTPSessionManager = StreamableHTTPSessionManager(
        app=sdk_server,
        event_store=event_store,
        json_response=False,
        stateless=not resolve_stateful(hub_config),
        security_settings=transport_security,
    )
    asgi_handler = StreamableHTTPASGIApp(session_manager)
    return asgi_handler, session_manager


def create_app(
    mcp_endpoint: MCPEndpoint,
    session_manager: SessionManager,
    cors_origins: tuple[str, ...] = ("http://127.0.0.1", "http://localhost"),
    hub_status_fn: Any = None,
    proxy_endpoint: ProxyEndpoint | None = None,
    registry: Any = None,
    reloader: Any = None,
    conn_manager: Any = None,
    api_key: str | None = None,
    stateless: bool | None = None,
    sdk_server: SdkServer | None = None,  # type: ignore[type-arg]
    metrics: Any = None,  # W5-P1: MetricsCollector — typed Any to avoid cycles
    event_stream_bridge: Any = None,  # W5-P2: EventStreamBridge — typed Any
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        mcp_endpoint: The federated MCP endpoint handler (used for legacy transport
            and management routes; required even when sdk_server is provided).
        session_manager: Session lifecycle manager.
        cors_origins: Allowed CORS origins.
        hub_status_fn: Callable returning hub status dict.
        sdk_server: Optional SDK ``Server`` for the MCP endpoint.  When provided,
            the SDK's Streamable HTTP ASGI app is mounted at ``/mcp`` and the
            hand-rolled POST/DELETE handlers are skipped.  When ``None`` (default),
            the existing hand-rolled endpoint is used for full backward compat.
    """
    # P03: build SDK ASGI handler + session manager BEFORE the FastAPI app so we
    # can wire session_manager.run() into the FastAPI lifespan.  Starlette does NOT
    # propagate lifespan events to mounted sub-apps, so we bypass
    # streamable_http_app() and own the session manager lifecycle ourselves.
    sdk_asgi: Any = None
    sdk_session_mgr: StreamableHTTPSessionManager | None = None
    if sdk_server is not None:
        # W8-P3: thread the live HubConfig so the InMemoryEventStore is built +
        # attached. Honored in stateful mode (SLM_HUB_STATEFUL=1 or config
        # transport_stateful=true); default is stateless (modern MCP 2026-07-28).
        hub_config = conn_manager.config if conn_manager is not None else None
        sdk_asgi, sdk_session_mgr = _build_sdk_asgi(sdk_server, hub_config=hub_config)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        """FastAPI lifespan: start SDK session manager when SDK mode is active."""
        if sdk_session_mgr is not None:
            async with sdk_session_mgr.run():
                yield
        else:
            yield

    # redirect_slashes=False: prevents Starlette from issuing a 307 redirect for
    # POST /mcp → /mcp/ before the SDK's StreamableHTTPASGIApp transport security
    # can inspect the Host header.  Without this, DNS rebinding protection is
    # bypassed because the 307 response echoes the attacker-controlled Host in
    # the Location header, and the conformance harness sees a 3xx instead of 4xx.
    app = FastAPI(
        title="SLM MCP Hub",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
        redirect_slashes=False,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_origin_regex=LOOPBACK_ORIGIN_REGEX,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )

    effective_api_key = api_key or os.environ.get("SLM_HUB_API_KEY")
    if stateless is None:
        stateless = os.environ.get("SLM_HUB_STATELESS", "").lower() in {
            "1", "true", "yes", "on",
        }

    @app.middleware("http")
    async def require_api_key(request: Request, call_next: Any) -> Response:
        """Authenticate MCP and management routes when hub auth is enabled."""
        if not effective_api_key or request.url.path == f"{API_PREFIX}/health":
            return await call_next(request)

        supplied = request.headers.get("x-slm-hub-api-key", "")
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if not supplied or not secrets.compare_digest(supplied, effective_api_key):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        return await call_next(request)

    # P03 DNS-rebinding fix: in SDK mode, POST /mcp (no trailing slash) must NOT
    # produce a 307 Temporary Redirect.  With redirect_slashes=False, Starlette no
    # longer issues the redirect, but the Mount regex (^/mcp/.*$) only matches paths
    # that START with "/mcp/" — so bare "/mcp" would 404.
    # This middleware normalises "/mcp" → "/mcp/" before routing so the Mount matches,
    # while keeping the full middleware chain (auth, CORS) intact.
    # In FastAPI, @app.middleware inserts at position-0 (outermost); adding this AFTER
    # require_api_key means it actually runs BEFORE auth — path normalisation runs
    # first, auth runs second, both see the corrected path.
    if sdk_asgi is not None:
        @app.middleware("http")
        async def _normalize_mcp_path(request: Request, call_next: Any) -> Response:
            if request.scope.get("path") == "/mcp":
                request.scope["path"] = "/mcp/"
                if "raw_path" in request.scope:
                    request.scope["raw_path"] = b"/mcp/"
            return await call_next(request)

    # ── MCP Streamable HTTP Endpoint ─────────────────────────────────────
    # P03: when sdk_server is provided, mount the bare SDK ASGI handler at /mcp.
    # The session manager is started in the FastAPI lifespan above (not inside
    # a Starlette sub-app), because Starlette does not propagate lifespan events
    # to mounted sub-apps and StreamableHTTPSessionManager.run() may only be
    # called once per instance.
    #
    # When sdk_server is None (default), the proven hand-rolled POST/DELETE
    # handlers run unchanged — all 1028+ existing tests stay green because
    # they never supply sdk_server.
    if sdk_asgi is not None:
        # Mounting at /mcp: the bare StreamableHTTPASGIApp handles scope routing
        # internally (path="/" matches the stripped sub-path after /mcp is consumed).
        app.mount("/mcp", sdk_asgi)
    else:
        @app.post(MCP_ENDPOINT_PATH)
        async def mcp_post(request: Request) -> Response:
            """Handle MCP JSON-RPC requests via Streamable HTTP POST."""
            session_id = request.headers.get("mcp-session-id", "")

            try:
                body = await request.json()
            except Exception:
                return _jsonrpc_error(None, -32700, "Parse error")
            if not isinstance(body, dict):
                return _jsonrpc_error(None, -32600, "Invalid Request")

            request_id = body.get("id")
            params = body.get("params")
            params = params if isinstance(params, dict) else {}
            meta = params.get("_meta")
            meta = meta if isinstance(meta, dict) else {}
            header_version = request.headers.get("mcp-protocol-version", "")
            meta_version = meta.get("io.modelcontextprotocol/protocolVersion")

            modern_request = meta_version is not None or header_version == MODERN_PROTOCOL_VERSION
            if modern_request:
                if not isinstance(meta_version, str) or not meta_version:
                    return _jsonrpc_error(request_id, -32602, "Invalid params")
                if not header_version or header_version != meta_version:
                    return _jsonrpc_error(request_id, -32020, "Header mismatch")
                if meta_version != MODERN_PROTOCOL_VERSION:
                    return _jsonrpc_error(
                        request_id,
                        -32022,
                        "Unsupported protocol version",
                        data={
                            "supported": [MODERN_PROTOCOL_VERSION],
                            "requested": meta_version,
                        },
                    )
                client_info = meta.get("io.modelcontextprotocol/clientInfo")
                client_capabilities = meta.get(
                    "io.modelcontextprotocol/clientCapabilities"
                )
                if not isinstance(client_info, dict) or not isinstance(
                    client_capabilities, dict
                ):
                    return _jsonrpc_error(request_id, -32602, "Invalid params")
            elif header_version and header_version not in LEGACY_PROTOCOL_VERSIONS:
                return _jsonrpc_error(
                    request_id,
                    -32022,
                    "Unsupported protocol version",
                    data={
                        "supported": [MODERN_PROTOCOL_VERSION],
                        "requested": header_version,
                    },
                )

            sessionless_request = bool(stateless or modern_request)
            if body.get("method") == "server/discover" and not modern_request:
                return _jsonrpc_error(
                    request_id, -32601, "Method not found", status_code=404
                )

            # Always create/register a session on initialize.
            # Some clients (e.g. Antigravity IDE) send their own mcp-session-id header on
            # initialize. Previously the hub only created a session when NO header was
            # provided, so the client-supplied ID was never registered — causing every
            # subsequent call to fail with "Session not found".
            is_initialize = body.get("method") == "initialize"
            if is_initialize and not sessionless_request:
                # Defensive: a malformed client may send a non-object 'params' or
                # 'clientInfo'. This runs before handle_jsonrpc's validation, so it
                # must not assume either is a dict.
                raw_params = body.get("params")
                raw_client_info = raw_params.get("clientInfo") if isinstance(raw_params, dict) else None
                client_info = raw_client_info if isinstance(raw_client_info, dict) else {}
                raw_name = client_info.get("name")
                client_name = raw_name if isinstance(raw_name, str) and raw_name.strip() else "unknown"
                existing = session_manager.get_session(session_id) if session_id else None
                if existing is None:
                    # Create session, honouring any client-supplied ID
                    session_id = session_manager.create_session(
                        client_name=client_name,
                        session_id=session_id or None,
                    )

            if sessionless_request:
                session_id = "stateless"
            elif not session_id:
                return _jsonrpc_error(
                    request_id, -32000, "Missing Mcp-Session-Id header"
                )

            # Verify session exists (for non-initialize requests)
            session = session_manager.get_session(session_id)
            if session is None and not is_initialize and not sessionless_request:
                recovery_enabled = os.environ.get(
                    "SLM_HUB_SESSION_RECOVERY", ""
                ).strip().lower() in {"1", "true", "yes", "on"}
                if not recovery_enabled:
                    return JSONResponse(
                        status_code=404,
                        content={
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32001, "message": "Session not found"},
                        },
                    )
                try:
                    session_manager.create_session(
                        client_name="recovered", session_id=session_id
                    )
                except ValueError:
                    return _jsonrpc_error(
                        request_id,
                        -32003,
                        "Session capacity reached",
                        status_code=429,
                    )
                logger.warning("Recovered unknown client session after restart")

            result = await mcp_endpoint.handle_jsonrpc(session_id, body)

            if result is None:
                return Response(status_code=204)

            headers = {} if sessionless_request else {"Mcp-Session-Id": session_id}
            return JSONResponse(content=result, headers=headers)

        @app.delete(MCP_ENDPOINT_PATH)
        async def mcp_delete(request: Request) -> Response:
            """Idempotently terminate a legacy Streamable HTTP session."""
            session_id = request.headers.get("mcp-session-id", "")
            if session_id:
                session_manager.destroy_session(session_id)
            return Response(status_code=204)

    # ── Management API ───────────────────────────────────────────────────

    @app.get(f"{API_PREFIX}/health")
    async def health() -> dict[str, Any]:
        """Liveness/readiness probe — intentionally MINIMAL.

        This is the ONLY route exempted from the api-key middleware, so it must
        NOT leak topology (host/port/plugins/server counts) to unauthenticated
        callers (SEC-M-01). It exposes only status, version, and the non-sensitive
        lifecycle ``state`` (readiness). Operational detail (host/port/plugins/
        counts) lives on the api-key-protected ``/api/status`` route.
        """
        status = hub_status_fn() if hub_status_fn else {}
        return {"status": "ok", "version": VERSION, "state": status.get("state", "unknown")}

    @app.get(f"{API_PREFIX}/session-greeting")
    async def session_greeting() -> dict[str, Any]:
        """Tool inventory for session initialization.

        Returns a compact summary of available servers and tool categories
        that can be injected into Claude session context at startup.
        """
        hub_info = hub_status_fn() if hub_status_fn else {}
        tool_list = registry.list_tools() if registry else []

        servers: dict[str, list[str]] = {}
        for tool_def in tool_list:
            name = tool_def.get("name", "")
            if "__" in name:
                server, tool_name = name.split("__", 1)
                servers.setdefault(server, []).append(tool_name)

        server_summary = {
            srv: {"tool_count": len(tools), "tools": tools[:5]}
            for srv, tools in sorted(servers.items())
        }

        return {
            "hub_version": VERSION,
            "state": hub_info.get("state", "unknown"),
            "total_servers": len(servers),
            "total_tools": len(tool_list),
            "invocation": {
                "search": 'search_tools(query="keyword")',
                "call": 'call_tool(tool="server__tool_name", arguments={...})',
                "list": "list_servers()",
            },
            "servers": server_summary,
        }

    @app.get(f"{API_PREFIX}/status")
    async def status() -> dict[str, Any]:
        """Detailed hub status."""
        hub_info = hub_status_fn() if hub_status_fn else {}
        session_info = session_manager.get_stats()
        return {
            "hub": hub_info,
            "sessions": session_info,
        }

    @app.get(f"{API_PREFIX}/sessions")
    async def list_sessions() -> dict[str, Any]:
        """List active sessions."""
        return session_manager.get_stats()

    @app.delete(API_PREFIX + "/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        """Destroy a session."""
        removed = session_manager.destroy_session(session_id)
        return {"removed": removed, "session_id": session_id}

    # ── Transparent Proxy Endpoints ────────────────────────────────

    if proxy_endpoint is not None:
        @app.post("/mcp/{server_name}")
        async def mcp_server_proxy(server_name: str, request: Request) -> Response:
            """Transparent proxy — forwards to a specific backend MCP server.

            Tool names are returned UNMODIFIED. Claude sees original names.
            The hub is invisible to the client.
            """
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                )

            result = await proxy_endpoint.handle_jsonrpc(server_name, body)

            if result is None:
                return Response(status_code=204)

            session_id = request.headers.get("mcp-session-id", "")
            headers = {"Mcp-Session-Id": session_id} if session_id else {}
            return JSONResponse(content=result, headers=headers)

        @app.get(f"{API_PREFIX}/servers")
        async def list_servers() -> dict[str, Any]:
            """List all backend MCP servers available via transparent proxy."""
            return {"servers": proxy_endpoint.list_available_servers()}

        @app.post(API_PREFIX + "/servers/{server_name}/reconnect")
        async def reconnect_server(server_name: str) -> dict[str, Any]:
            """Reconnect a failed or disconnected MCP server."""
            conn_manager = proxy_endpoint._conn_manager
            success, message = await conn_manager.reconnect(server_name)
            return {"success": success, "server": server_name, "message": message}

    # ── Lifecycle (Phase 3+4) ──────────────────────────────────────

    if conn_manager is not None:
        @app.get(f"{API_PREFIX}/servers/detail")
        async def servers_detail() -> dict[str, Any]:
            """Per-server detail: configured | connected | tools | error."""
            return {"servers": conn_manager.get_server_status()}

    if reloader is not None:
        @app.post(f"{API_PREFIX}/reload")
        async def reload_config() -> dict[str, Any]:
            """Re-read config.json from disk and apply the diff via reloader.

            Single source of truth for the live hub state is the on-disk
            config.json. CLI commands edit the file, then POST here.
            """
            from slm_mcp_hub.core.config import load_config
            from slm_mcp_hub.lifecycle.reloader import ReloadError
            try:
                new_config = load_config()
                diff = await reloader.apply_config(new_config)
                return {
                    "success": True,
                    "summary": diff.summary(),
                    "added": [s.name for s in diff.added],
                    "removed": list(diff.removed),
                    "modified": [s.name for s in diff.modified],
                    "unchanged": list(diff.unchanged),
                }
            except ReloadError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                logger.error("Reload crashed (%s)", type(exc).__name__)
                return {"success": False, "error": "Reload failed unexpectedly"}

    # ── W5-P1/P2: Admin routes (observability + control) ───────────────────
    # make_admin_router() is always called; enriched route is only registered
    # when conn_manager is not None. All new params default to None for full
    # backward compatibility — all existing tests that don't pass metrics or
    # event_stream_bridge continue to work unchanged.
    from slm_mcp_hub.server.admin_routes import make_admin_router  # noqa: PLC0415
    hub_config = getattr(conn_manager, "config", None) if conn_manager is not None else None
    dashboard_enabled = bool(getattr(hub_config, "dashboard_enabled", True))

    # W5-P2: Build EventStreamBridge from conn_manager's event bus when not
    # pre-supplied. When event_stream_bridge is already provided (e.g. in
    # tests), use it as-is for backward compatibility.
    if event_stream_bridge is None and conn_manager is not None:
        _event_bus = getattr(conn_manager, "_event_bus", None)
        if _event_bus is not None and hasattr(_event_bus, "register_consumer"):
            from slm_mcp_hub.observability.event_stream import (  # noqa: PLC0415
                EventStreamBridge,
            )
            _queue_maxsize: int = int(
                getattr(hub_config, "event_queue_maxsize", 256)
            )
            event_stream_bridge = EventStreamBridge(
                bus=_event_bus,
                queue_maxsize=_queue_maxsize,
            )

    admin_router = make_admin_router(
        conn_manager=conn_manager,
        event_stream_bridge=event_stream_bridge,
        metrics=metrics,
        dashboard_enabled=dashboard_enabled,
    )
    app.include_router(admin_router)

    return app
