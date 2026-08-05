"""Coverage gap tests for http_server.py.

Targets lines NOT reached by test_stateless_http.py or test_streamable_http_e2e.py:
  155   — else-yield in non-SDK lifespan
  241-242 — JSON parse error in mcp_post
  244   — non-dict JSON body
  257   — modern request with null/missing meta_version
  279   — unknown legacy header version
  291   — server/discover rejected for legacy clients
  305-316 — legacy initialize creates/reuses session
  344-345 — session recovery at capacity → 429
  356   — endpoint returns None → 204
  388-403 — session_greeting endpoint
  416-424 — status endpoint
  426-429 — sessions list endpoint
  434-435 — delete_session endpoint
  440-474 — proxy endpoint routes
  479-482 — conn_manager servers/detail
  485-509 — reloader reload_config
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from slm_mcp_hub.server.http_server import create_app
from slm_mcp_hub.session.manager import SessionManager

MODERN_VERSION = "2026-07-28"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_endpoint(return_value=None) -> MagicMock:
    ep = MagicMock()
    ep.handle_jsonrpc = AsyncMock(
        return_value=return_value
        if return_value is not None
        else {"jsonrpc": "2.0", "id": 1, "result": {}}
    )
    return ep


def _stateless_app():
    """App in stateless mode — no SDK server."""
    app = create_app(
        mcp_endpoint=_make_endpoint(),
        session_manager=SessionManager(),
        stateless=True,
    )
    return TestClient(app)


def _stateful_app():
    """App in stateful (session) mode — no SDK server."""
    sessions = SessionManager()
    ep = _make_endpoint()
    app = create_app(
        mcp_endpoint=ep,
        session_manager=sessions,
        stateless=False,
    )
    return TestClient(app), sessions, ep


# ---------------------------------------------------------------------------
# Lifespan (non-SDK path)  →  line 155
# ---------------------------------------------------------------------------

class TestNonSdkLifespan:
    def test_lifespan_yields_cleanly_when_no_sdk_server(self) -> None:
        """The else-yield branch in _lifespan must be reached when sdk_server
        is absent.  We use TestClient as a context manager so lifespan fires."""
        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=SessionManager(),
            stateless=True,
        )
        with TestClient(app) as client:
            resp = client.get("/api/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# mcp_post error paths  →  lines 241-242, 244, 257, 279, 291
# ---------------------------------------------------------------------------

class TestMcpPostErrorPaths:
    def test_invalid_json_body_returns_parse_error(self) -> None:
        """Malformed JSON bytes → -32700 Parse error (lines 241-242)."""
        client = _stateless_app()
        resp = client.post(
            "/mcp",
            content=b"not-json{{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32700

    def test_json_array_body_returns_invalid_request(self) -> None:
        """JSON array body (not a dict) → -32600 Invalid Request (line 244)."""
        client = _stateless_app()
        resp = client.post(
            "/mcp",
            content=b"[1, 2, 3]",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32600

    def test_modern_header_with_missing_meta_version_key_rejected(self) -> None:
        """Modern header present but meta has no protocolVersion → -32602 (line 257).

        modern_request = True (header matches) but meta_version is None (key
        absent). The isinstance guard fires and rejects the request.
        """
        client, _, _ = _stateful_app()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    # NO protocolVersion key — meta_version resolves to None
                    "io.modelcontextprotocol/clientInfo": {},
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
        resp = client.post(
            "/mcp",
            json=body,
            headers={"MCP-Protocol-Version": MODERN_VERSION},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32602

    def test_unknown_legacy_header_version_rejected(self) -> None:
        """An unrecognised non-modern header → -32022 (line 279).

        The version is not in LEGACY_PROTOCOL_VERSIONS and != MODERN_PROTOCOL_VERSION,
        and no modern meta key is present, so the elif branch fires.
        """
        client = _stateless_app()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        resp = client.post(
            "/mcp",
            json=body,
            headers={"MCP-Protocol-Version": "9999-01-01"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32022

    def test_server_discover_rejected_for_non_modern_client(self) -> None:
        """server/discover without modern headers → 404 -32601 (line 291)."""
        client = _stateless_app()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {},
        }
        resp = client.post("/mcp", json=body)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Legacy initialize + session handling  →  lines 305-316
# ---------------------------------------------------------------------------

class TestLegacyInitializeSession:
    def test_legacy_initialize_creates_session(self) -> None:
        """POST initialize in stateful mode creates a session (lines 305-316)."""
        client, sessions, _ = _stateful_app()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "test-cli", "version": "1.0"},
                "capabilities": {},
            },
        }
        resp = client.post("/mcp", json=body)
        assert resp.status_code == 200
        assert sessions.active_count == 1
        assert "mcp-session-id" in resp.headers

    def test_legacy_initialize_with_client_id_header_reuses_id(self) -> None:
        """initialize honouring a client-supplied mcp-session-id (lines 310-316)."""
        client, sessions, _ = _stateful_app()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "cli", "version": "1"},
                "capabilities": {},
            },
        }
        resp = client.post(
            "/mcp",
            json=body,
            headers={"Mcp-Session-Id": "client-chosen-id"},
        )
        assert resp.status_code == 200
        assert sessions.get_session("client-chosen-id") is not None

    def test_legacy_initialize_with_non_dict_params(self) -> None:
        """Defensive handling: params is None/non-dict → falls back to unknown (line 306-308)."""
        client, sessions, _ = _stateful_app()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": None,  # not a dict
        }
        resp = client.post("/mcp", json=body)
        # Should still create a session with "unknown" as client_name
        assert resp.status_code == 200
        assert sessions.active_count == 1


# ---------------------------------------------------------------------------
# Session recovery at capacity  →  lines 344-345
# ---------------------------------------------------------------------------

class TestSessionRecoveryAtCapacity:
    def test_recovery_at_capacity_returns_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session recovery enabled but create_session raises ValueError → 429 (lines 344-345)."""
        monkeypatch.setenv("SLM_HUB_SESSION_RECOVERY", "1")

        sessions = MagicMock(spec=SessionManager)
        sessions.get_session = MagicMock(return_value=None)
        sessions.create_session = MagicMock(side_effect=ValueError("at capacity"))

        ep = _make_endpoint()
        app = create_app(
            mcp_endpoint=ep,
            session_manager=sessions,
            stateless=False,
        )
        client = TestClient(app)

        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Mcp-Session-Id": "missing-id"},
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == -32003


# ---------------------------------------------------------------------------
# Endpoint returns None  →  line 356
# ---------------------------------------------------------------------------

class TestEndpointNoneResponse:
    def test_none_result_produces_204(self) -> None:
        """When endpoint returns None (notification), response is 204 (line 356)."""
        ep = MagicMock()
        ep.handle_jsonrpc = AsyncMock(return_value=None)
        app = create_app(
            mcp_endpoint=ep,
            session_manager=SessionManager(),
            stateless=True,
        )
        client = TestClient(app)
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
                # No id — notification
            },
        )
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Management API endpoints  →  lines 388-435
# ---------------------------------------------------------------------------

class TestManagementEndpoints:
    def test_session_greeting_returns_hub_inventory(self) -> None:
        """GET /api/session-greeting returns hub metadata (lines 388-403)."""
        registry = MagicMock()
        registry.list_tools = MagicMock(return_value=[
            {"name": "server1__tool_a"},
            {"name": "server1__tool_b"},
            {"name": "server2__tool_c"},
            {"name": "no_namespace"},  # no __ → skipped
        ])
        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=SessionManager(),
            stateless=True,
            registry=registry,
        )
        client = TestClient(app)
        resp = client.get("/api/session-greeting")
        assert resp.status_code == 200
        data = resp.json()
        assert "hub_version" in data
        assert "total_tools" in data
        assert "servers" in data
        assert data["total_servers"] == 2  # server1 and server2

    def test_session_greeting_without_registry(self) -> None:
        """session_greeting works with no registry (empty tool list fallback)."""
        client = _stateless_app()
        resp = client.get("/api/session-greeting")
        assert resp.status_code == 200
        assert resp.json()["total_tools"] == 0

    def test_status_endpoint_returns_hub_and_sessions(self) -> None:
        """GET /api/status returns hub status + session stats (lines 416-424)."""
        hub_status = MagicMock(return_value={"state": "ready", "tools": 42})
        sessions = SessionManager()
        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=sessions,
            stateless=True,
            hub_status_fn=hub_status,
        )
        client = TestClient(app)
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "hub" in data
        assert "sessions" in data

    def test_sessions_list_endpoint(self) -> None:
        """GET /api/sessions returns session stats (lines 426-429)."""
        client = _stateless_app()
        resp = client.get("/api/sessions")
        assert resp.status_code == 200

    def test_delete_session_endpoint(self) -> None:
        """DELETE /api/sessions/{id} destroys a session (lines 434-435)."""
        sessions = SessionManager()
        sid = sessions.create_session("test-client")
        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=sessions,
            stateless=True,
        )
        client = TestClient(app)

        resp = client.delete(f"/api/sessions/{sid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["removed"] is True
        assert body["session_id"] == sid
        assert sessions.get_session(sid) is None


# ---------------------------------------------------------------------------
# Proxy endpoint routes  →  lines 440-474
# ---------------------------------------------------------------------------

def _proxy_app():
    """App with a mock ProxyEndpoint."""
    proxy = MagicMock()
    proxy.handle_jsonrpc = AsyncMock(
        return_value={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    )
    proxy.list_available_servers = MagicMock(return_value=["backend1"])
    cm = MagicMock()
    cm.reconnect = AsyncMock(return_value=(True, "reconnected"))
    proxy._conn_manager = cm
    app = create_app(
        mcp_endpoint=_make_endpoint(),
        session_manager=SessionManager(),
        stateless=True,
        proxy_endpoint=proxy,
    )
    return TestClient(app), proxy


class TestProxyEndpoints:
    def test_proxy_post_routes_to_backend(self) -> None:
        """POST /mcp/{server} forwards to proxy (lines 440-462)."""
        client, proxy = _proxy_app()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        resp = client.post("/mcp/backend1", json=body)
        assert resp.status_code == 200
        proxy.handle_jsonrpc.assert_awaited_once_with("backend1", body)

    def test_proxy_post_invalid_json_returns_400(self) -> None:
        """POST /mcp/{server} with malformed body → 400 (lines 449-453)."""
        client, _ = _proxy_app()
        resp = client.post(
            "/mcp/backend1",
            content=b"bad json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_proxy_post_none_result_returns_204(self) -> None:
        """ProxyEndpoint returns None → 204 (lines 457-458)."""
        proxy = MagicMock()
        proxy.handle_jsonrpc = AsyncMock(return_value=None)
        proxy.list_available_servers = MagicMock(return_value=[])
        proxy._conn_manager = MagicMock()
        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=SessionManager(),
            stateless=True,
            proxy_endpoint=proxy,
        )
        client = TestClient(app)
        resp = client.post("/mcp/srv", json={"jsonrpc": "2.0", "id": 1, "method": "x"})
        assert resp.status_code == 204

    def test_proxy_list_servers_endpoint(self) -> None:
        """GET /api/servers returns available backend servers (lines 464-467)."""
        client, proxy = _proxy_app()
        resp = client.get("/api/servers")
        assert resp.status_code == 200
        assert resp.json() == {"servers": ["backend1"]}

    def test_proxy_reconnect_endpoint(self) -> None:
        """POST /api/servers/{name}/reconnect triggers reconnect (lines 469-474)."""
        client, proxy = _proxy_app()
        resp = client.post("/api/servers/backend1/reconnect")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["server"] == "backend1"

    def test_proxy_session_header_forwarded(self) -> None:
        """Mcp-Session-Id header is echoed in the proxy response (lines 460-462)."""
        client, _ = _proxy_app()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        resp = client.post(
            "/mcp/srv",
            json=body,
            headers={"Mcp-Session-Id": "abc-123"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("mcp-session-id") == "abc-123"


# ---------------------------------------------------------------------------
# conn_manager servers/detail  →  lines 479-482
# ---------------------------------------------------------------------------

class TestConnManagerEndpoints:
    def test_servers_detail_endpoint(self) -> None:
        """GET /api/servers/detail returns server status (lines 479-482)."""
        cm = MagicMock()
        cm.get_server_status = MagicMock(return_value=[
            {"name": "srv1", "connected": True, "tool_count": 5},
        ])
        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=SessionManager(),
            stateless=True,
            conn_manager=cm,
        )
        client = TestClient(app)
        resp = client.get("/api/servers/detail")
        assert resp.status_code == 200
        data = resp.json()
        assert "servers" in data
        cm.get_server_status.assert_called_once()


# ---------------------------------------------------------------------------
# Reloader reload_config  →  lines 485-509
# ---------------------------------------------------------------------------

class TestReloaderEndpoints:
    def test_reload_config_success(self) -> None:
        """POST /api/reload on success returns diff (lines 485-504)."""
        diff = MagicMock()
        diff.summary.return_value = "1 added, 0 removed"
        diff.added = [MagicMock(name="new-server")]
        diff.removed = []
        diff.modified = []
        diff.unchanged = ["existing"]

        reloader = MagicMock()
        reloader.apply_config = AsyncMock(return_value=diff)

        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=SessionManager(),
            stateless=True,
            reloader=reloader,
        )
        with TestClient(app) as client:
            with (
                pytest.MonkeyPatch()
                .context() as mp
            ):
                mp.setattr(
                    "slm_mcp_hub.server.http_server.create_app.__code__",
                    create_app.__code__,
                    raising=False,
                )
                resp = client.post("/api/reload")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "summary" in data

    def test_reload_config_reload_error(self) -> None:
        """POST /api/reload when ReloadError raised → success=False (lines 505-506)."""
        from slm_mcp_hub.lifecycle.reloader import ReloadError

        reloader = MagicMock()
        reloader.apply_config = AsyncMock(side_effect=ReloadError("bad config"))

        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=SessionManager(),
            stateless=True,
            reloader=reloader,
        )
        client = TestClient(app)
        resp = client.post("/api/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "bad config" in data["error"]

    def test_reload_config_unexpected_exception(self) -> None:
        """POST /api/reload on unhandled exception → generic error (lines 507-509)."""
        reloader = MagicMock()
        reloader.apply_config = AsyncMock(side_effect=RuntimeError("internal"))

        app = create_app(
            mcp_endpoint=_make_endpoint(),
            session_manager=SessionManager(),
            stateless=True,
            reloader=reloader,
        )
        client = TestClient(app)
        resp = client.post("/api/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "Reload failed" in data["error"]
