"""Integration tests for transparent proxy and lifecycle HTTP routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.lifecycle.reloader import ReloadError
from slm_mcp_hub.server.http_server import create_app
from slm_mcp_hub.session.manager import SessionManager


def _client() -> tuple[TestClient, MagicMock, MagicMock]:
    endpoint = MagicMock()
    proxy = MagicMock()
    proxy.handle_jsonrpc = AsyncMock(return_value={
        "jsonrpc": "2.0", "id": 1, "result": {"tools": []},
    })
    proxy.list_available_servers.return_value = [{
        "name": "backend", "connected": True, "tools": 1,
    }]
    proxy._conn_manager = MagicMock()
    proxy._conn_manager.reconnect = AsyncMock(return_value=(True, "reconnected"))

    conn_manager = MagicMock()
    conn_manager.get_server_status.return_value = [{
        "name": "backend", "connected": True,
    }]

    registry = MagicMock()
    registry.list_tools.return_value = [
        {"name": "github__search"},
        {"name": "github__issues"},
        {"name": "slack__send"},
    ]

    reloader = MagicMock()
    app = create_app(
        mcp_endpoint=endpoint,
        session_manager=SessionManager(),
        hub_status_fn=lambda: {"state": "ready"},
        proxy_endpoint=proxy,
        registry=registry,
        reloader=reloader,
        conn_manager=conn_manager,
    )
    return TestClient(app), proxy, reloader


def test_session_greeting_summarizes_tools_by_server() -> None:
    client, _, _ = _client()
    response = client.get("/api/session-greeting")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["total_servers"] == 2
    assert body["total_tools"] == 3
    assert body["servers"]["github"] == {
        "tool_count": 2,
        "tools": ["search", "issues"],
    }


def test_transparent_proxy_parse_error_success_and_notification() -> None:
    client, proxy, _ = _client()
    invalid = client.post(
        "/mcp/backend", content=b"not json", headers={"content-type": "application/json"}
    )
    success = client.post(
        "/mcp/backend",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Mcp-Session-Id": "session-1"},
    )
    proxy.handle_jsonrpc.return_value = None
    notification = client.post(
        "/mcp/backend",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )

    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == -32700
    assert success.status_code == 200
    assert success.headers["Mcp-Session-Id"] == "session-1"
    assert notification.status_code == 204


def test_server_inventory_reconnect_and_detail_routes() -> None:
    client, proxy, _ = _client()
    inventory = client.get("/api/servers")
    reconnect = client.post("/api/servers/backend/reconnect")
    detail = client.get("/api/servers/detail")

    assert inventory.json()["servers"][0]["name"] == "backend"
    assert reconnect.json() == {
        "success": True,
        "server": "backend",
        "message": "reconnected",
    }
    proxy._conn_manager.reconnect.assert_awaited_once_with("backend")
    assert detail.json()["servers"][0]["connected"] is True


def test_reload_success_returns_applied_diff() -> None:
    client, _, reloader = _client()
    diff = MagicMock()
    diff.summary.return_value = "+1 ~1 -1 =1 unchanged"
    diff.added = (MCPServerConfig(name="new", transport="stdio", command="echo"),)
    diff.removed = ("old",)
    diff.modified = (MCPServerConfig(name="changed", transport="stdio", command="echo"),)
    diff.unchanged = ("same",)
    reloader.apply_config = AsyncMock(return_value=diff)

    with patch("slm_mcp_hub.core.config.load_config", return_value=HubConfig()):
        response = client.post("/api/reload")

    assert response.json() == {
        "success": True,
        "summary": "+1 ~1 -1 =1 unchanged",
        "added": ["new"],
        "removed": ["old"],
        "modified": ["changed"],
        "unchanged": ["same"],
    }


def test_reload_validation_error_is_reported() -> None:
    client, _, reloader = _client()
    reloader.apply_config = AsyncMock(side_effect=ReloadError("invalid config"))
    with patch("slm_mcp_hub.core.config.load_config", return_value=HubConfig()):
        response = client.post("/api/reload")
    assert response.json() == {"success": False, "error": "invalid config"}


def test_unexpected_reload_error_is_sanitized(caplog) -> None:
    client, _, reloader = _client()
    reloader.apply_config = AsyncMock(side_effect=RuntimeError("secret-sentinel"))
    with patch("slm_mcp_hub.core.config.load_config", return_value=HubConfig()):
        response = client.post("/api/reload")
    assert response.json() == {
        "success": False,
        "error": "Reload failed unexpectedly",
    }
    assert "secret-sentinel" not in caplog.text
