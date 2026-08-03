"""Stateless HTTP compatibility for legacy and MCP 2026-07-28 clients."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from slm_mcp_hub.server.http_server import create_app
from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
from slm_mcp_hub.session.manager import SessionManager

MODERN_VERSION = "2026-07-28"


def _client(*, stateless: bool) -> tuple[TestClient, MagicMock, SessionManager]:
    endpoint = MagicMock()
    endpoint.handle_jsonrpc = AsyncMock(
        return_value={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    )
    sessions = SessionManager()
    app = create_app(
        mcp_endpoint=endpoint,
        session_manager=sessions,
        stateless=stateless,
    )
    return TestClient(app), endpoint, sessions


def _modern_request(method: str = "tools/list") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "test-client",
                    "version": "1.0",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }


def test_optional_stateless_mode_accepts_legacy_request_without_session() -> None:
    client, endpoint, sessions = _client(stateless=True)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    response = client.post("/mcp", json=body)

    assert response.status_code == 200
    assert "Mcp-Session-Id" not in response.headers
    assert sessions.active_count == 0
    endpoint.handle_jsonrpc.assert_awaited_once_with("stateless", body)


def test_stateful_mode_still_requires_session_for_legacy_request() -> None:
    client, _, _ = _client(stateless=False)

    response = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Missing Mcp-Session-Id header"


def test_unknown_legacy_session_is_strict_by_default() -> None:
    client, _, sessions = _client(stateless=False)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Mcp-Session-Id": "unknown"},
    )

    assert response.status_code == 404
    assert sessions.active_count == 0


def test_opt_in_session_recovery_readopts_client_id(monkeypatch) -> None:
    monkeypatch.setenv("SLM_HUB_SESSION_RECOVERY", "1")
    client, _, sessions = _client(stateless=False)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Mcp-Session-Id": "recover-me"},
    )

    assert response.status_code == 200
    assert sessions.get_session("recover-me") is not None


def test_delete_mcp_is_idempotent_and_terminates_live_session() -> None:
    client, _, sessions = _client(stateless=False)
    sessions.create_session("test", session_id="live")

    assert client.delete(
        "/mcp", headers={"Mcp-Session-Id": "live"}
    ).status_code == 204
    assert sessions.get_session("live") is None
    assert client.delete(
        "/mcp", headers={"Mcp-Session-Id": "unknown"}
    ).status_code == 204


def test_modern_request_is_sessionless_even_when_legacy_mode_is_stateful() -> None:
    client, endpoint, sessions = _client(stateless=False)
    body = _modern_request()

    response = client.post(
        "/mcp",
        json=body,
        headers={"MCP-Protocol-Version": MODERN_VERSION},
    )

    assert response.status_code == 200
    assert "Mcp-Session-Id" not in response.headers
    assert sessions.active_count == 0
    endpoint.handle_jsonrpc.assert_awaited_once_with("stateless", body)


def test_modern_request_rejects_header_meta_mismatch() -> None:
    client, _, _ = _client(stateless=False)
    body = _modern_request()

    response = client.post(
        "/mcp",
        json=body,
        headers={"MCP-Protocol-Version": "2025-11-25"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


def test_modern_request_requires_per_request_client_metadata() -> None:
    client, _, _ = _client(stateless=False)
    body = _modern_request()
    del body["params"]["_meta"]["io.modelcontextprotocol/clientInfo"]

    response = client.post(
        "/mcp",
        json=body,
        headers={"MCP-Protocol-Version": MODERN_VERSION},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


def test_unknown_modern_protocol_lists_supported_versions() -> None:
    client, _, _ = _client(stateless=False)
    body = _modern_request()
    body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2099-01-01"

    response = client.post(
        "/mcp",
        json=body,
        headers={"MCP-Protocol-Version": "2099-01-01"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": -32022,
        "message": "Unsupported protocol version",
        "data": {
            "supported": [MODERN_VERSION],
            "requested": "2099-01-01",
        },
    }


async def test_server_discover_advertises_modern_and_legacy_support() -> None:
    endpoint = MCPEndpoint(
        registry=MagicMock(),
        router=MagicMock(),
        session_manager=SessionManager(),
    )

    response = await endpoint.handle_jsonrpc(
        "stateless",
        _modern_request(method="server/discover"),
    )

    assert response is not None
    result = response["result"]
    assert result["supportedVersions"][0] == MODERN_VERSION
    assert "2025-11-25" in result["supportedVersions"]
    assert result["capabilities"]["tools"] == {"listChanged": True}
    assert result["serverInfo"]["name"] == "slm-mcp-hub"


async def test_legacy_initialize_negotiates_supported_client_version() -> None:
    sessions = SessionManager()
    session_id = sessions.create_session("test")
    endpoint = MCPEndpoint(
        registry=MagicMock(),
        router=MagicMock(),
        session_manager=sessions,
    )

    response = await endpoint.handle_jsonrpc(
        session_id,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "legacy", "version": "1.0"},
                "capabilities": {},
            },
        },
    )

    assert response is not None
    assert response["result"]["protocolVersion"] == "2025-11-25"
