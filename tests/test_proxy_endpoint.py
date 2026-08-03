"""Behavioral and boundary tests for transparent MCP proxy mode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from slm_mcp_hub.server.proxy_endpoint import ProxyEndpoint


def _connection(*, connected: bool = True) -> MagicMock:
    connection = MagicMock()
    connection.name = "backend"
    connection.is_connected = connected
    connection.capabilities = {
        "tools": [{"name": "echo", "inputSchema": {"type": "object"}}],
        "resources": [{"uri": "file:///one"}],
        "resource_templates": [{"uriTemplate": "file:///{name}"}],
        "prompts": [{"name": "review"}],
    }
    connection.call_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
    connection.read_resource = AsyncMock(return_value={"contents": [{"text": "data"}]})
    connection.get_prompt = AsyncMock(return_value={"messages": [{"role": "user"}]})
    return connection


def _endpoint(connection: MagicMock | None = None, hub: MagicMock | None = None) -> ProxyEndpoint:
    manager = MagicMock()
    manager.connections = {} if connection is None else {"backend": connection}
    return ProxyEndpoint(manager, hub=hub)


@pytest.mark.asyncio
async def test_notification_has_no_response() -> None:
    result = await _endpoint().handle_jsonrpc(
        "backend",
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [None, [], "invalid"])
async def test_non_object_message_is_invalid_request(message: object) -> None:
    result = await _endpoint().handle_jsonrpc("backend", message)  # type: ignore[arg-type]
    assert result == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }


@pytest.mark.asyncio
async def test_non_object_params_are_invalid() -> None:
    result = await _endpoint(_connection()).handle_jsonrpc(
        "backend",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": "bad"},
    )
    assert result["error"] == {"code": -32602, "message": "Invalid params"}


@pytest.mark.asyncio
async def test_missing_method_is_invalid_request() -> None:
    result = await _endpoint(_connection()).handle_jsonrpc(
        "backend", {"jsonrpc": "2.0", "id": 1, "params": {}}
    )
    assert result["error"] == {"code": -32600, "message": "Invalid Request"}


@pytest.mark.asyncio
async def test_missing_and_disconnected_servers_return_protocol_errors() -> None:
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    missing = await _endpoint().handle_jsonrpc("missing", request)
    disconnected = await _endpoint(_connection(connected=False)).handle_jsonrpc(
        "backend", request
    )
    assert missing["error"]["code"] == -32001
    assert disconnected["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_initialize_and_all_discovery_methods_preserve_backend_names() -> None:
    endpoint = _endpoint(_connection())
    initialize = await endpoint.handle_jsonrpc(
        "backend", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    tools = await endpoint.handle_jsonrpc(
        "backend", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    resources = await endpoint.handle_jsonrpc(
        "backend", {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}
    )
    templates = await endpoint.handle_jsonrpc(
        "backend",
        {"jsonrpc": "2.0", "id": 4, "method": "resources/templates/list", "params": {}},
    )
    prompts = await endpoint.handle_jsonrpc(
        "backend", {"jsonrpc": "2.0", "id": 5, "method": "prompts/list", "params": {}}
    )

    assert initialize["result"]["serverInfo"]["name"] == "backend"
    assert initialize["result"]["capabilities"] == {
        "tools": {"listChanged": True},
        "resources": {"listChanged": True},
        "prompts": {"listChanged": True},
    }
    assert tools["result"]["tools"][0]["name"] == "echo"
    assert resources["result"]["resources"][0]["uri"] == "file:///one"
    assert templates["result"]["resourceTemplates"][0]["uriTemplate"] == "file:///{name}"
    assert prompts["result"]["prompts"][0]["name"] == "review"


@pytest.mark.asyncio
async def test_tool_resource_and_prompt_calls_are_forwarded() -> None:
    connection = _connection()
    endpoint = _endpoint(connection)

    tool = await endpoint.handle_jsonrpc(
        "backend",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hello"}},
        },
    )
    resource = await endpoint.handle_jsonrpc(
        "backend",
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": "file:///one"},
        },
    )
    prompt = await endpoint.handle_jsonrpc(
        "backend",
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "prompts/get",
            "params": {"name": "review", "arguments": {"tone": "strict"}},
        },
    )

    connection.call_tool.assert_awaited_once_with("echo", {"text": "hello"})
    connection.read_resource.assert_awaited_once_with("file:///one")
    connection.get_prompt.assert_awaited_once_with("review", {"tone": "strict"})
    assert tool["result"]["content"][0]["text"] == "ok"
    assert resource["result"]["contents"][0]["text"] == "data"
    assert prompt["result"]["messages"][0]["role"] == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("tools/call", {"name": 42, "arguments": {}}),
        ("tools/call", {"name": "echo", "arguments": "bad"}),
        ("resources/read", {"uri": 42}),
        ("prompts/get", {"name": 42, "arguments": {}}),
        ("prompts/get", {"name": "review", "arguments": "bad"}),
    ],
)
async def test_forwarded_methods_validate_params(method: str, params: dict) -> None:
    result = await _endpoint(_connection()).handle_jsonrpc(
        "backend",
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert result["error"] == {"code": -32602, "message": "Invalid params"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tools/call", "resources/read", "prompts/get"])
async def test_backend_failure_returns_sanitized_internal_error(method: str, caplog) -> None:
    connection = _connection()
    connection.call_tool.side_effect = RuntimeError("secret-sentinel")
    connection.read_resource.side_effect = RuntimeError("secret-sentinel")
    connection.get_prompt.side_effect = RuntimeError("secret-sentinel")
    params = {
        "tools/call": {"name": "echo", "arguments": {}},
        "resources/read": {"uri": "file:///one"},
        "prompts/get": {"name": "review", "arguments": {}},
    }[method]

    result = await _endpoint(connection).handle_jsonrpc(
        "backend",
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )

    assert result["error"] == {"code": -32603, "message": "Internal server error"}
    assert "secret-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_plugin_notification_records_success_and_ignores_plugin_failure() -> None:
    connection = _connection()
    hub = MagicMock()
    hub.notify_plugins_tool_call_after = AsyncMock(side_effect=RuntimeError("plugin failed"))
    result = await _endpoint(connection, hub=hub).handle_jsonrpc(
        "backend",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {}},
        },
    )
    assert "result" in result
    hub.notify_plugins_tool_call_after.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found() -> None:
    result = await _endpoint(_connection()).handle_jsonrpc(
        "backend",
        {"jsonrpc": "2.0", "id": 1, "method": "unknown/method", "params": {}},
    )
    assert result["error"]["code"] == -32601


def test_list_available_servers_reports_capability_counts() -> None:
    servers = _endpoint(_connection()).list_available_servers()
    assert servers == [{
        "name": "backend",
        "connected": True,
        "tools": 1,
        "resources": 1,
        "prompts": 1,
    }]
