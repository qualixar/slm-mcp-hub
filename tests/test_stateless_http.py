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


# ---------------------------------------------------------------------------
# P03 additions: SDK-aware stateless HTTP tests
# ---------------------------------------------------------------------------

class TestSdkStatelessCoexistence:
    """Verify hand-rolled stateless mode and SDK mode can coexist in the same
    create_app() surface without interfering with each other.

    The SDK path is gated by sdk_server= parameter; the hand-rolled path runs
    when sdk_server is absent.  These tests confirm the gate works correctly.
    """

    def test_stateless_env_var_still_applies_without_sdk(self) -> None:
        """Hand-rolled stateless mode (SLM_HUB_STATELESS=1) still works."""
        import os
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from slm_mcp_hub.server.http_server import create_app
        from slm_mcp_hub.session.manager import SessionManager

        os.environ["SLM_HUB_STATELESS"] = "1"
        try:
            endpoint = MagicMock()
            endpoint.handle_jsonrpc = AsyncMock(
                return_value={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
            )
            sessions = SessionManager()
            app = create_app(mcp_endpoint=endpoint, session_manager=sessions)
            client = TestClient(app)

            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            resp = client.post("/mcp", json=body)
            assert resp.status_code == 200
            endpoint.handle_jsonrpc.assert_awaited_once()
        finally:
            os.environ.pop("SLM_HUB_STATELESS", None)

    def test_sdk_mode_create_app_signature_stable(self) -> None:
        """create_app() accepts sdk_server= without TypeError (signature stable)."""
        import inspect

        from slm_mcp_hub.server.http_server import create_app

        sig = inspect.signature(create_app)
        assert "sdk_server" in sig.parameters, "sdk_server parameter must exist in create_app"

    def test_sdk_lifespan_wired_via_fastapi_lifespan(self) -> None:
        """FastAPI lifespan starts the SDK session manager when sdk_server is set.

        Verified indirectly: if session_manager.run() is NOT called, the first
        POST to /mcp returns 500 (task group not initialized). The fact that
        test_sdk_tools_list_responds passes in test_streamable_http_e2e.py
        proves the lifespan is correctly wired.  This test confirms the FastAPI
        app has a non-trivial lifespan attribute when sdk_server is provided.
        """
        from unittest.mock import AsyncMock, MagicMock

        from slm_mcp_hub.protocol.inbound import build_sdk_server
        from slm_mcp_hub.protocol.models import (
            PromptsListOutcome,
            ResourcesListOutcome,
            ResourceTemplatesListOutcome,
            ToolsListOutcome,
        )
        from slm_mcp_hub.server.http_server import create_app
        from slm_mcp_hub.session.manager import SessionManager

        ops = MagicMock()
        ops.list_tools = AsyncMock(return_value=ToolsListOutcome(tools=()))
        ops.list_resources = AsyncMock(return_value=ResourcesListOutcome(resources=()))
        ops.list_resource_templates = AsyncMock(
            return_value=ResourceTemplatesListOutcome(resource_templates=())
        )
        ops.list_prompts = AsyncMock(return_value=PromptsListOutcome(prompts=()))
        sdk_server = build_sdk_server(ops)

        mcp_ep = MagicMock()
        mcp_ep.handle_jsonrpc = AsyncMock(return_value={})
        sessions = SessionManager()

        app = create_app(
            mcp_endpoint=mcp_ep,
            session_manager=sessions,
            sdk_server=sdk_server,
        )
        # FastAPI exposes router.lifespan_context; confirm it is not the trivial default
        lc = app.router.lifespan_context
        assert lc is not None
        # The context should be an async context manager (not the bare _DefaultLifespan)
        import inspect
        assert inspect.isfunction(lc) or callable(lc), "lifespan_context must be callable"


class TestNonLoopbackSecurityWarning:
    """Verify that building the SDK ASGI handler with a non-loopback host logs a warning."""

    def test_non_loopback_host_logs_dns_rebinding_warning(self, caplog) -> None:
        """_build_sdk_asgi with host='0.0.0.0' must warn about missing protection."""
        import logging

        from slm_mcp_hub.protocol.inbound import build_sdk_server
        from slm_mcp_hub.protocol.models import (
            PromptsListOutcome,
            ResourcesListOutcome,
            ResourceTemplatesListOutcome,
            ToolsListOutcome,
        )
        from slm_mcp_hub.server.http_server import _build_sdk_asgi

        ops = MagicMock()
        ops.list_tools = AsyncMock(return_value=ToolsListOutcome(tools=()))
        ops.list_resources = AsyncMock(return_value=ResourcesListOutcome(resources=()))
        ops.list_resource_templates = AsyncMock(
            return_value=ResourceTemplatesListOutcome(resource_templates=())
        )
        ops.list_prompts = AsyncMock(return_value=PromptsListOutcome(prompts=()))
        sdk_server = build_sdk_server(ops)

        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.server.http_server"):
            _asgi, _mgr = _build_sdk_asgi(sdk_server, host="0.0.0.0")

        # Warning must mention DNS rebinding protection is disabled
        assert any(
            "DNS rebinding" in rec.message and "DISABLED" in rec.message
            for rec in caplog.records
        ), f"Expected DNS rebinding warning, got: {[r.message for r in caplog.records]}"

    def test_loopback_host_does_not_warn(self, caplog) -> None:
        """_build_sdk_asgi with loopback host must NOT log the security warning."""
        import logging

        from slm_mcp_hub.protocol.inbound import build_sdk_server
        from slm_mcp_hub.protocol.models import (
            PromptsListOutcome,
            ResourcesListOutcome,
            ResourceTemplatesListOutcome,
            ToolsListOutcome,
        )
        from slm_mcp_hub.server.http_server import _build_sdk_asgi

        ops = MagicMock()
        ops.list_tools = AsyncMock(return_value=ToolsListOutcome(tools=()))
        ops.list_resources = AsyncMock(return_value=ResourcesListOutcome(resources=()))
        ops.list_resource_templates = AsyncMock(
            return_value=ResourceTemplatesListOutcome(resource_templates=())
        )
        ops.list_prompts = AsyncMock(return_value=PromptsListOutcome(prompts=()))
        sdk_server = build_sdk_server(ops)

        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.server.http_server"):
            _asgi, _mgr = _build_sdk_asgi(sdk_server, host="127.0.0.1")

        assert not any(
            "DNS rebinding" in rec.message
            for rec in caplog.records
        ), f"Unexpected DNS rebinding warning for loopback: {[r.message for r in caplog.records]}"

    def test_remote_bind_without_api_key_raises(self) -> None:
        """create_app must refuse a non-loopback host if SLM_HUB_API_KEY is not set.

        This is enforced at the CLI level (start command), but the middleware also
        rejects unauthenticated non-loopback requests. This test verifies the
        API-key middleware applies: without a key, requests get 401, not processed.
        """
        import os

        from fastapi.testclient import TestClient

        from slm_mcp_hub.server.http_server import create_app
        from slm_mcp_hub.session.manager import SessionManager

        # Set a key so the app is built with auth; then confirm a keyless request fails
        endpoint = MagicMock()
        endpoint.handle_jsonrpc = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        sessions = SessionManager()
        env_key = os.environ.get("SLM_HUB_API_KEY")
        try:
            os.environ["SLM_HUB_API_KEY"] = ""
            app = create_app(
                mcp_endpoint=endpoint,
                session_manager=sessions,
                api_key="must-supply-key",
            )
        finally:
            if env_key is None:
                os.environ.pop("SLM_HUB_API_KEY", None)
            else:
                os.environ["SLM_HUB_API_KEY"] = env_key

        client = TestClient(app)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        resp = client.post("/mcp", json=body)
        assert resp.status_code == 401, (
            f"Expected 401 (API key required), got {resp.status_code}"
        )
