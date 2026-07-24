"""Regression tests for session recovery + MCP session-termination verbs.

Fix A — the hub re-adopts an unknown `mcp-session-id` instead of returning
`404 Session not found`, so a client survives a hub restart without having to
re-initialize. Guarded by `SLM_HUB_SESSION_RECOVERY` (default on).

Fix B — the hub implements `DELETE /mcp` (idempotent session termination) so a
client's cleanup path succeeds instead of getting `405`.

All exercised at the highest existing seam: FastAPI `TestClient` over
`create_app`, asserting externally-observable HTTP behaviour (status code,
`Mcp-Session-Id` header, session registered/gone afterwards) — never internal
call counts. Prior art: tests/test_phase2_sessions.py::TestHTTPServer and
tests/test_http_initialize_validation.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.server.http_server import create_app
from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
from slm_mcp_hub.session.manager import SessionManager

TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _client(sessions: SessionManager | None = None) -> tuple[TestClient, SessionManager]:
    registry = CapabilityRegistry()
    registry.sync({
        "jira": {
            "tools": [{"name": "get_issue", "description": "Get an issue", "inputSchema": {}}],
            "resources": [], "resource_templates": [], "prompts": [],
        },
    })
    router = AsyncMock(spec=FederationRouter)
    router.route_tool_call = AsyncMock(return_value=RouteResult(
        result={"content": [{"type": "text", "text": "ok"}]},
        server_name="jira", tool_name="get_issue", duration_ms=1, success=True,
    ))
    sessions = sessions or SessionManager()
    endpoint = MCPEndpoint(registry, router, sessions)
    return TestClient(create_app(mcp_endpoint=endpoint, session_manager=sessions)), sessions


class TestSessionRecovery:
    def test_unknown_session_is_re_adopted(self):
        """An unknown session id on a non-initialize request is recovered:
        the request succeeds AND the id is registered afterwards."""
        client, sessions = _client()
        resp = client.post("/mcp", json=TOOLS_LIST, headers={"Mcp-Session-Id": "ghost-abc"})
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("Mcp-Session-Id") == "ghost-abc"
        assert sessions.get_session("ghost-abc") is not None

    def test_recovered_session_id_is_preserved(self):
        """The client's own id is honoured, so its subsequent calls keep working."""
        client, sessions = _client()
        client.post("/mcp", json=TOOLS_LIST, headers={"Mcp-Session-Id": "keep-me"})
        again = client.post("/mcp", json=TOOLS_LIST, headers={"Mcp-Session-Id": "keep-me"})
        assert again.status_code == 200, again.text
        assert sessions.active_count == 1

    def test_recovery_disabled_via_env_still_404(self, monkeypatch):
        """Strict-spec mode remains reachable via the env guard."""
        monkeypatch.setenv("SLM_HUB_SESSION_RECOVERY", "0")
        client, sessions = _client()
        resp = client.post("/mcp", json=TOOLS_LIST, headers={"Mcp-Session-Id": "ghost"})
        assert resp.status_code == 404
        assert sessions.get_session("ghost") is None

    def test_recovery_at_capacity_evicts_instead_of_500(self):
        """When MAX_SESSIONS is reached, recovery evicts the LRU session rather
        than surfacing the ValueError as a 500."""
        sessions = SessionManager(max_sessions=2)
        sessions.create_session(client_name="a", session_id="old")
        sessions.create_session(client_name="b", session_id="mid")
        client, _ = _client(sessions)
        resp = client.post("/mcp", json=TOOLS_LIST, headers={"Mcp-Session-Id": "new"})
        assert resp.status_code == 200, resp.text
        assert sessions.get_session("new") is not None
        assert sessions.active_count <= 2
        # The least-recently-active ("old") was the one evicted.
        assert sessions.get_session("old") is None

    def test_missing_session_header_still_400(self):
        """A request with no session header at all is still malformed."""
        client, _ = _client()
        resp = client.post("/mcp", json=TOOLS_LIST)
        assert resp.status_code == 400
        assert "Missing" in resp.json()["error"]["message"]


class TestEvictOldest:
    """Secondary seam: SessionManager.evict_oldest LRU policy directly."""

    def test_evict_oldest_on_empty_returns_none(self):
        assert SessionManager().evict_oldest() is None

    def test_evict_oldest_removes_least_recently_active(self):
        sm = SessionManager()
        sm.create_session(client_name="a", session_id="first")
        sm.create_session(client_name="b", session_id="second")
        sm.touch("first")  # first is now the most-recently-active
        assert sm.evict_oldest() == "second"
        assert sm.get_session("second") is None
        assert sm.get_session("first") is not None


class TestSessionTermination:
    def _init(self, client: TestClient) -> str:
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "Test"}},
        })
        return resp.headers["Mcp-Session-Id"]

    def test_delete_live_session_returns_204_and_removes(self):
        client, sessions = _client()
        sid = self._init(client)
        assert sessions.get_session(sid) is not None
        resp = client.delete("/mcp", headers={"Mcp-Session-Id": sid})
        assert resp.status_code == 204
        assert sessions.get_session(sid) is None

    def test_delete_unknown_session_is_idempotent_204(self):
        client, _ = _client()
        resp = client.delete("/mcp", headers={"Mcp-Session-Id": "never-existed"})
        assert resp.status_code == 204

    def test_delete_without_session_header_is_204(self):
        client, _ = _client()
        resp = client.delete("/mcp")
        assert resp.status_code == 204

    def test_get_mcp_returns_405(self):
        """The hub pushes no server-initiated SSE messages; GET is not routed."""
        client, _ = _client()
        resp = client.get("/mcp")
        assert resp.status_code == 405
