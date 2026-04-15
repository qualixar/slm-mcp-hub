"""FastAPI HTTP server for SLM MCP Hub."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from slm_mcp_hub.core.constants import (
    API_PREFIX,
    MCP_ENDPOINT_PATH,
    VERSION,
)
from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
from slm_mcp_hub.server.proxy_endpoint import ProxyEndpoint
from slm_mcp_hub.session.manager import SessionManager

logger = logging.getLogger(__name__)


def create_app(
    mcp_endpoint: MCPEndpoint,
    session_manager: SessionManager,
    cors_origins: tuple[str, ...] = ("*",),
    hub_status_fn: Any = None,
    proxy_endpoint: ProxyEndpoint | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        mcp_endpoint: The federated MCP endpoint handler.
        session_manager: Session lifecycle manager.
        cors_origins: Allowed CORS origins.
        hub_status_fn: Callable returning hub status dict.
    """
    app = FastAPI(
        title="SLM MCP Hub",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )

    # ── MCP Streamable HTTP Endpoint ─────────────────────────────────────

    @app.post(MCP_ENDPOINT_PATH)
    async def mcp_post(request: Request) -> Response:
        """Handle MCP JSON-RPC requests via Streamable HTTP POST."""
        session_id = request.headers.get("mcp-session-id", "")

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )

        # If no session_id and this is an initialize request, create session
        is_initialize = body.get("method") == "initialize"
        if not session_id and is_initialize:
            client_info = body.get("params", {}).get("clientInfo", {})
            session_id = session_manager.create_session(
                client_name=client_info.get("name", "unknown"),
            )

        if not session_id:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": "Missing Mcp-Session-Id header"}},
            )

        # Verify session exists
        session = session_manager.get_session(session_id)
        if session is None and not is_initialize:
            return JSONResponse(
                status_code=404,
                content={"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32001, "message": "Session not found"}},
            )

        result = await mcp_endpoint.handle_jsonrpc(session_id, body)

        if result is None:
            return Response(status_code=204)

        headers = {"Mcp-Session-Id": session_id}
        return JSONResponse(content=result, headers=headers)

    # ── Management API ───────────────────────────────────────────────────

    @app.get(f"{API_PREFIX}/health")
    async def health() -> dict[str, Any]:
        """Health check endpoint."""
        status = hub_status_fn() if hub_status_fn else {}
        return {
            "status": "ok",
            "version": VERSION,
            **status,
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

    return app
