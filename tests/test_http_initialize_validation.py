"""Regression tests for malformed `initialize` requests over HTTP.

The HTTP transport reads `params.clientInfo` to name the session before
`MCPEndpoint.handle_jsonrpc` ever sees the message. A client sending a
non-object `params` or `clientInfo` therefore raised `AttributeError`
inside the request handler and surfaced as an opaque HTTP 500 rather than
a JSON-RPC error the client could act on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.server.http_server import create_app
from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
from slm_mcp_hub.session.manager import SessionManager


def _client() -> TestClient:
    registry = CapabilityRegistry()
    registry.sync({
        "jira": {
            "tools": [{"name": "get_issue", "description": "Get an issue", "inputSchema": {}}],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        },
    })
    router = AsyncMock(spec=FederationRouter)
    router.route_tool_call = AsyncMock(return_value=RouteResult(
        result={"content": [{"type": "text", "text": "ok"}]},
        server_name="jira", tool_name="get_issue", duration_ms=1, success=True,
    ))
    sessions = SessionManager()
    endpoint = MCPEndpoint(registry, router, sessions)
    return TestClient(create_app(mcp_endpoint=endpoint, session_manager=sessions))


class TestMalformedInitialize:
    @pytest.mark.parametrize("bad_params", ["bad", ["bad"], 7, None])
    def test_non_object_params_does_not_500(self, bad_params):
        response = _client().post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": bad_params,
        })
        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("bad_client_info", ["bad", ["bad"], 7])
    def test_non_object_client_info_does_not_500(self, bad_client_info):
        response = _client().post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": bad_client_info},
        })
        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("bad_name", [123, ["x"], {"x": 1}, None, "", "   "])
    def test_unusable_client_name_falls_back_to_unknown(self, bad_name):
        client = _client()
        response = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": bad_name}},
        })
        assert response.status_code == 200, response.text
        session_id = response.headers.get("Mcp-Session-Id")
        assert session_id
        listed = client.get("/api/sessions").json()
        names = {s["client_name"] for s in listed["sessions"]}
        assert names == {"unknown"}


class TestWellFormedInitializeStillWorks:
    def test_client_name_is_recorded(self):
        client = _client()
        response = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "Claude Code", "version": "1.0"}},
        })
        assert response.status_code == 200, response.text
        listed = client.get("/api/sessions").json()
        assert {s["client_name"] for s in listed["sessions"]} == {"Claude Code"}
