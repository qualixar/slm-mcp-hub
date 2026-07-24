"""Phase 2 Session & Transport Tests — 100% coverage target."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from slm_mcp_hub.core.constants import VERSION

import pytest
from fastapi.testclient import TestClient

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.server.http_server import create_app
from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
from slm_mcp_hub.session.coordination import SessionCoordinator
from slm_mcp_hub.session.manager import SessionInfo, SessionManager


# ===========================================================================
# Session Manager Tests
# ===========================================================================

class TestSessionManager:
    def test_create_session(self):
        sm = SessionManager()
        sid = sm.create_session(client_name="Claude Code")
        assert len(sid) == 36  # UUID format
        assert sm.active_count == 1

    def test_get_session(self):
        sm = SessionManager()
        sid = sm.create_session(client_name="Claude")
        info = sm.get_session(sid)
        assert info is not None
        assert info.client_name == "Claude"
        assert info.session_id == sid

    def test_get_session_not_found(self):
        sm = SessionManager()
        assert sm.get_session("nonexistent") is None

    def test_get_session_expired(self):
        sm = SessionManager(timeout_seconds=0)  # Immediate expiry
        sid = sm.create_session()
        time.sleep(0.01)
        assert sm.get_session(sid) is None

    def test_touch_updates_activity(self):
        sm = SessionManager()
        sid = sm.create_session()
        before = sm.get_session(sid).last_activity
        time.sleep(0.01)
        assert sm.touch(sid) is True
        after = sm.get_session(sid).last_activity
        assert after > before

    def test_touch_not_found(self):
        sm = SessionManager()
        assert sm.touch("nonexistent") is False

    def test_touch_expired(self):
        sm = SessionManager(timeout_seconds=0)
        sid = sm.create_session()
        time.sleep(0.01)
        assert sm.touch(sid) is False

    def test_destroy_session(self):
        sm = SessionManager()
        sid = sm.create_session()
        assert sm.destroy_session(sid) is True
        assert sm.active_count == 0

    def test_destroy_nonexistent(self):
        sm = SessionManager()
        assert sm.destroy_session("nope") is False

    def test_max_sessions_limit(self):
        sm = SessionManager(max_sessions=2)
        sm.create_session()
        sm.create_session()
        with pytest.raises(ValueError, match="Max sessions"):
            sm.create_session()

    def test_list_sessions(self):
        sm = SessionManager()
        sm.create_session(client_name="A")
        sm.create_session(client_name="B")
        sessions = sm.list_sessions()
        assert len(sessions) == 2
        names = {s.client_name for s in sessions}
        assert names == {"A", "B"}

    def test_get_stats(self):
        sm = SessionManager()
        sm.create_session(client_name="Claude")
        stats = sm.get_stats()
        assert stats["active_sessions"] == 1
        assert len(stats["sessions"]) == 1
        assert stats["sessions"][0]["client_name"] == "Claude"

    def test_cleanup_expired_on_create(self):
        sm = SessionManager(timeout_seconds=0, max_sessions=1)
        sm.create_session()
        time.sleep(0.01)
        # Expired session cleaned up, new one can be created
        sid2 = sm.create_session()
        assert sm.active_count == 1

    def test_create_with_project_and_permissions(self):
        sm = SessionManager()
        sid = sm.create_session(
            client_name="Claude",
            project_path="/my/project",
            permissions={"role": "admin"},
        )
        info = sm.get_session(sid)
        assert info.project_path == "/my/project"
        assert info.permissions == {"role": "admin"}

    def test_max_sessions_property(self):
        """max_sessions property returns the configured limit (line 50)."""
        sm = SessionManager(max_sessions=42)
        assert sm.max_sessions == 42


class TestSessionInfo:
    def test_immutable(self):
        info = SessionInfo(
            session_id="abc", client_name="test",
            connected_at=1.0, last_activity=1.0,
        )
        with pytest.raises(AttributeError):
            info.client_name = "changed"  # type: ignore


# ===========================================================================
# Session Coordinator Tests
# ===========================================================================

class TestSessionCoordinator:
    def test_acquire_lock(self):
        sc = SessionCoordinator()
        assert sc.acquire("sqlite:write", "session-1") is True
        assert sc.is_locked("sqlite:write") is True

    def test_acquire_same_session_reentrant(self):
        sc = SessionCoordinator()
        sc.acquire("sqlite:write", "session-1")
        assert sc.acquire("sqlite:write", "session-1") is True

    def test_acquire_different_session_blocked(self):
        sc = SessionCoordinator()
        sc.acquire("sqlite:write", "session-1")
        assert sc.acquire("sqlite:write", "session-2") is False

    def test_release_lock(self):
        sc = SessionCoordinator()
        sc.acquire("sqlite:write", "session-1")
        assert sc.release("sqlite:write", "session-1") is True
        assert sc.is_locked("sqlite:write") is False

    def test_release_not_held(self):
        sc = SessionCoordinator()
        assert sc.release("sqlite:write", "session-1") is False

    def test_release_wrong_session(self):
        sc = SessionCoordinator()
        sc.acquire("sqlite:write", "session-1")
        assert sc.release("sqlite:write", "session-2") is False

    def test_get_lock_holder(self):
        sc = SessionCoordinator()
        sc.acquire("sqlite:write", "session-1")
        assert sc.get_lock_holder("sqlite:write") == "session-1"

    def test_get_lock_holder_none(self):
        sc = SessionCoordinator()
        assert sc.get_lock_holder("sqlite:write") is None

    def test_get_locks(self):
        sc = SessionCoordinator()
        sc.acquire("a", "s1")
        sc.acquire("b", "s2")
        locks = sc.get_locks()
        assert len(locks) == 2

    def test_release_all_for_session(self):
        sc = SessionCoordinator()
        sc.acquire("a", "s1")
        sc.acquire("b", "s1")
        sc.acquire("c", "s2")
        count = sc.release_all_for_session("s1")
        assert count == 2
        assert sc.is_locked("a") is False
        assert sc.is_locked("c") is True

    def test_release_all_empty(self):
        sc = SessionCoordinator()
        assert sc.release_all_for_session("nope") == 0

    def test_expired_lock_auto_cleanup(self):
        sc = SessionCoordinator()
        sc.acquire("sqlite:write", "session-1", timeout_seconds=0)
        time.sleep(0.01)
        assert sc.is_locked("sqlite:write") is False

    def test_expired_lock_allows_new_acquire(self):
        sc = SessionCoordinator()
        sc.acquire("sqlite:write", "session-1", timeout_seconds=0)
        time.sleep(0.01)
        assert sc.acquire("sqlite:write", "session-2") is True


# ===========================================================================
# MCP Endpoint Tests
# ===========================================================================

class TestMCPEndpoint:
    def _make_endpoint(self):
        reg = CapabilityRegistry()
        reg.sync({
            "github": {
                "tools": [{"name": "search", "description": "Search code", "inputSchema": {}}],
                "resources": [{"uri": "repo://main", "name": "Main"}],
                "resource_templates": [],
                "prompts": [{"name": "review", "description": "Review PR"}],
            },
        })
        mock_router = AsyncMock(spec=FederationRouter)
        mock_router.route_tool_call = AsyncMock(return_value=RouteResult(
            result={"content": [{"type": "text", "text": "ok"}]},
            server_name="github", tool_name="search", duration_ms=10, success=True,
        ))
        mock_router.route_resource_read = AsyncMock(return_value=RouteResult(
            result={"contents": [{"text": "data"}]},
            server_name="github", tool_name="repo://main", duration_ms=5, success=True,
        ))
        mock_router.route_prompt_get = AsyncMock(return_value=RouteResult(
            result={"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]},
            server_name="github", tool_name="review", duration_ms=3, success=True,
        ))
        sm = SessionManager()
        sid = sm.create_session(client_name="test")
        endpoint = MCPEndpoint(reg, mock_router, sm)
        return endpoint, sid, mock_router

    @pytest.mark.asyncio
    async def test_handle_initialize(self):
        ep, sid, _ = self._make_endpoint()
        result = await ep.handle_initialize(sid, {"clientInfo": {"name": "Claude Code"}})
        assert result["protocolVersion"] == "2024-11-05"
        assert "tools" in result["capabilities"]

    @pytest.mark.asyncio
    async def test_handle_tools_list(self):
        ep, sid, _ = self._make_endpoint()
        result = await ep.handle_tools_list(sid, {})
        names = [t["name"] for t in result["tools"]]
        # Meta-tools + 1 federated tool
        assert "search_tools" in names
        assert "list_servers" in names
        # In federated mode, only meta-tools are exposed (not individual server tools)
        assert "call_tool" in names

    @pytest.mark.asyncio
    async def test_handle_tools_call(self):
        ep, sid, router = self._make_endpoint()
        result = await ep.handle_tools_call(sid, {"name": "github__search", "arguments": {"q": "test"}})
        assert result["content"][0]["text"] == "ok"
        router.route_tool_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_resources_list(self):
        ep, sid, _ = self._make_endpoint()
        result = await ep.handle_resources_list(sid, {})
        assert len(result["resources"]) == 1

    @pytest.mark.asyncio
    async def test_handle_resources_read(self):
        ep, sid, router = self._make_endpoint()
        result = await ep.handle_resources_read(sid, {"uri": "github__repo://main"})
        assert "contents" in result

    @pytest.mark.asyncio
    async def test_handle_resources_templates_list(self):
        ep, sid, _ = self._make_endpoint()
        result = await ep.handle_resources_templates_list(sid, {})
        assert "resourceTemplates" in result

    @pytest.mark.asyncio
    async def test_handle_prompts_list(self):
        ep, sid, _ = self._make_endpoint()
        result = await ep.handle_prompts_list(sid, {})
        assert len(result["prompts"]) == 1

    @pytest.mark.asyncio
    async def test_handle_prompts_get(self):
        ep, sid, router = self._make_endpoint()
        result = await ep.handle_prompts_get(sid, {"name": "github__review", "arguments": {}})
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_handle_jsonrpc_dispatch(self):
        ep, sid, _ = self._make_endpoint()
        result = await ep.handle_jsonrpc(sid, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        assert result["id"] == 1
        assert "tools" in result["result"]

    @pytest.mark.asyncio
    async def test_handle_jsonrpc_method_not_found(self):
        ep, sid, _ = self._make_endpoint()
        result = await ep.handle_jsonrpc(sid, {
            "jsonrpc": "2.0", "id": 1, "method": "fake/method", "params": {},
        })
        assert result["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_handle_jsonrpc_notification_returns_none(self):
        ep, sid, _ = self._make_endpoint()
        result = await ep.handle_jsonrpc(sid, {
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        })
        assert result is None

    @pytest.mark.asyncio
    async def test_handle_jsonrpc_handler_exception(self):
        ep, sid, router = self._make_endpoint()
        router.route_tool_call.side_effect = RuntimeError("crash")
        result = await ep.handle_jsonrpc(sid, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "github__search", "arguments": {}},
        })
        assert result["error"]["code"] == -32603
        assert result["error"]["message"] == "Internal server error"


# ===========================================================================
# HTTP Server Tests
# ===========================================================================

class TestHTTPServer:
    def _make_client(self):
        reg = CapabilityRegistry()
        reg.sync({
            "github": {
                "tools": [{"name": "search", "description": "Search", "inputSchema": {}}],
                "resources": [], "resource_templates": [], "prompts": [],
            },
        })
        mock_router = AsyncMock(spec=FederationRouter)
        mock_router.route_tool_call = AsyncMock(return_value=RouteResult(
            result={"content": [{"type": "text", "text": "found"}]},
            server_name="github", tool_name="search", duration_ms=5, success=True,
        ))
        sm = SessionManager()
        endpoint = MCPEndpoint(reg, mock_router, sm)
        app = create_app(
            mcp_endpoint=endpoint,
            session_manager=sm,
            hub_status_fn=lambda: {"state": "ready", "uptime_seconds": 42},
        )
        return TestClient(app), sm

    def test_health(self):
        client, _ = self._make_client()
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["version"] == VERSION
        assert resp.json()["state"] == "ready"

    def test_status(self):
        client, _ = self._make_client()
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert "hub" in resp.json()
        assert "sessions" in resp.json()

    def test_list_sessions_empty(self):
        client, _ = self._make_client()
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json()["active_sessions"] == 0

    def test_mcp_initialize(self):
        client, _ = self._make_client()
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "TestClient", "version": "1.0"}},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert data["result"]["serverInfo"]["name"] == "slm-mcp-hub"
        assert "Mcp-Session-Id" in resp.headers
        session_id = resp.headers["Mcp-Session-Id"]
        assert len(session_id) > 0

    def test_mcp_tools_list(self):
        client, _ = self._make_client()
        # First initialize
        init_resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "Test"}},
        })
        sid = init_resp.headers["Mcp-Session-Id"]

        # Then list tools
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, headers={"Mcp-Session-Id": sid})
        assert resp.status_code == 200
        tools = resp.json()["result"]["tools"]
        names = [t["name"] for t in tools]
        assert "search_tools" in names
        # In federated mode, only meta-tools are exposed (not individual server tools)
        assert "call_tool" in names

    def test_mcp_tools_call(self):
        client, _ = self._make_client()
        init_resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "Test"}},
        })
        sid = init_resp.headers["Mcp-Session-Id"]

        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "github__search", "arguments": {"q": "hello"}},
        }, headers={"Mcp-Session-Id": sid})
        assert resp.status_code == 200
        assert resp.json()["result"]["content"][0]["text"] == "found"

    def test_mcp_missing_session_id(self):
        client, _ = self._make_client()
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        assert resp.status_code == 400
        assert "Missing" in resp.json()["error"]["message"]

    def test_mcp_unknown_session_id_is_recovered(self):
        # Contract change (see tests/test_session_recovery.py): an unknown
        # session id is re-adopted rather than rejected with 404, so a client
        # survives a hub restart. Strict-spec 404 is still reachable via
        # SLM_HUB_SESSION_RECOVERY=0.
        client, sm = self._make_client()
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        }, headers={"Mcp-Session-Id": "invalid-uuid"})
        assert resp.status_code == 200
        assert sm.get_session("invalid-uuid") is not None

    def test_mcp_parse_error(self):
        client, _ = self._make_client()
        resp = client.post("/mcp", content=b"not json", headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32700

    def test_mcp_notification_returns_204(self):
        client, _ = self._make_client()
        init_resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "Test"}},
        })
        sid = init_resp.headers["Mcp-Session-Id"]

        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }, headers={"Mcp-Session-Id": sid})
        assert resp.status_code == 204

    def test_delete_session(self):
        client, sm = self._make_client()
        sid = sm.create_session(client_name="test")
        resp = client.delete(f"/api/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

    def test_no_hub_status_fn(self):
        """App works without hub_status_fn."""
        reg = CapabilityRegistry()
        sm = SessionManager()
        mock_router = AsyncMock(spec=FederationRouter)
        endpoint = MCPEndpoint(reg, mock_router, sm)
        app = create_app(mcp_endpoint=endpoint, session_manager=sm)
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    # ── Regression tests for v0.2.4 session ID bug ──────────────────────────

    def test_mcp_initialize_with_client_provided_session_id(self):
        """Regression: client sends its own mcp-session-id on initialize.

        Bug (v0.2.3): the hub only created a session when NO header was present.
        If a client (e.g. Antigravity IDE) sent its own ID, the session was never
        registered and every subsequent call failed with 404 Session Not Found.

        Fix (v0.2.4): always create/register a session on initialize, honouring
        any client-supplied session ID.
        """
        client, _ = self._make_client()
        client_session_id = "antigravity-fixed-session-abc123"

        # Initialize WITH a client-provided session ID
        init_resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "Antigravity IDE"}},
        }, headers={"Mcp-Session-Id": client_session_id})
        assert init_resp.status_code == 200, f"Initialize failed: {init_resp.json()}"
        # Hub must echo back the session ID
        assert init_resp.headers.get("Mcp-Session-Id") == client_session_id

        # Immediately call tools/list with the SAME session ID — must NOT 404
        tools_resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, headers={"Mcp-Session-Id": client_session_id})
        assert tools_resp.status_code == 200, (
            f"tools/list failed with status {tools_resp.status_code}: {tools_resp.json()}"
        )
        tools = tools_resp.json()["result"]["tools"]
        names = [t["name"] for t in tools]
        assert "search_tools" in names
        assert "call_tool" in names

    def test_mcp_initialize_creates_session_when_id_provided(self):
        """Session is stored in the manager when client provides its own ID."""
        client, sm = self._make_client()
        client_id = "my-explicit-session-id"

        client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "TestIDE"}},
        }, headers={"Mcp-Session-Id": client_id})

        # Session must exist in the manager
        session = sm.get_session(client_id)
        assert session is not None, "Session not found in manager after initialize with client-provided ID"
        assert session.client_name == "TestIDE"

    def test_mcp_reinitialize_same_id_is_idempotent(self):
        """Re-initializing with the same session ID doesn't duplicate or error."""
        client, sm = self._make_client()
        client_id = "reused-session-id"

        for i in range(2):
            resp = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": i + 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "TestIDE"}},
            }, headers={"Mcp-Session-Id": client_id})
            assert resp.status_code == 200

        # Still only one session with this ID
        assert sm.active_count == 1


# ===========================================================================
# Session Manager — explicit session_id tests
# ===========================================================================

class TestSessionManagerExplicitId:
    def test_create_session_with_explicit_id(self):
        """create_session(session_id=...) stores and returns the provided ID."""
        sm = SessionManager()
        explicit_id = "my-client-session-xyz"
        returned = sm.create_session(client_name="IDE", session_id=explicit_id)
        assert returned == explicit_id
        info = sm.get_session(explicit_id)
        assert info is not None
        assert info.session_id == explicit_id
        assert info.client_name == "IDE"

    def test_create_session_without_explicit_id_generates_uuid(self):
        """Without session_id, a UUID is auto-generated (backward compat)."""
        sm = SessionManager()
        sid = sm.create_session(client_name="Test")
        assert len(sid) == 36  # standard UUID length
        assert sm.get_session(sid) is not None
