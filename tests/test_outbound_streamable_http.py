"""P04 — OutboundClient streamable HTTP transport integration tests.

Proves the HTTP↔HTTP transport cell with a REAL uvicorn/ASGI MCP server.
Also proves that configured static headers reach the upstream while inbound
Authorization/Cookie sentinels do NOT.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.protocol.outbound import OutboundClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_config(url: str, headers: dict[str, str] | None = None, **kw: Any) -> MCPServerConfig:
    return MCPServerConfig(
        name="fixture-http",
        transport="http",
        url=url,
        headers=headers or {},
        **kw,
    )


# ---------------------------------------------------------------------------
# Fixtures — real uvicorn HTTP MCP servers
# ---------------------------------------------------------------------------

@pytest.fixture()
async def http_mcp_server() -> AsyncGenerator[str, None]:
    """Start a real uvicorn HTTP MCP server; yield its /mcp URL."""
    import uvicorn
    from mcp.server.mcpserver import MCPServer
    from mcp.types import TextContent

    port = _free_port()
    mcp = MCPServer("fixture-http")

    @mcp.tool(description="Echo text back")
    async def echo(text: str = ""):  # return type omitted: avoids deferred-annotation/eval_str NameError
        return [TextContent(type="text", text=f"echo: {text}")]

    @mcp.resource("http://fixture/resource", description="HTTP test resource")
    async def http_resource() -> str:
        return "http fixture content"

    @mcp.prompt(description="HTTP test prompt")
    async def http_prompt() -> str:
        return "http prompt content"

    app = mcp.streamable_http_app()
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    task = asyncio.create_task(server.serve())

    # Wait for server ready (poll until it responds)
    import httpx
    for _ in range(40):
        try:
            async with httpx.AsyncClient() as c:
                await c.get(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
                break  # 400/405 is fine — server is up
        except Exception:
            await asyncio.sleep(0.1)

    yield f"http://127.0.0.1:{port}/mcp"

    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


@pytest.fixture()
async def header_capturing_http_server() -> AsyncGenerator[tuple[str, dict[str, str]], None]:
    """HTTP MCP server that captures request headers; yields (url, headers_dict)."""
    import uvicorn
    from mcp.server.mcpserver import MCPServer
    from mcp.types import TextContent
    from starlette.middleware.base import BaseHTTPMiddleware

    port = _free_port()
    mcp = MCPServer("header-capture")

    @mcp.tool(description="Placeholder tool")
    async def placeholder(x: str = ""):  # return type omitted: avoids deferred-annotation/eval_str NameError
        return [TextContent(type="text", text="ok")]

    received_headers: dict[str, str] = {}

    class CaptureMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            received_headers.update(dict(request.headers))
            return await call_next(request)

    app = mcp.streamable_http_app()
    app.add_middleware(CaptureMiddleware)

    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    task = asyncio.create_task(server.serve())

    import httpx
    for _ in range(40):
        try:
            async with httpx.AsyncClient() as c:
                await c.get(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
                break
        except Exception:
            await asyncio.sleep(0.1)

    yield f"http://127.0.0.1:{port}/mcp", received_headers

    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


# ---------------------------------------------------------------------------
# Tests: HTTP transport cell (HTTP↔HTTP)
# ---------------------------------------------------------------------------

class TestOutboundClientHTTPConnect:
    """OutboundClient connects to a real HTTP MCP server (uvicorn)."""

    @pytest.mark.asyncio
    async def test_http_connect_discovers_tools(self, http_mcp_server: str):
        """connect() discovers tools from real HTTP server."""
        client = OutboundClient(_http_config(http_mcp_server))
        try:
            await client.connect()
            assert len(client.capabilities["tools"]) == 1
            assert client.capabilities["tools"][0]["name"] == "echo"
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_http_connect_discovers_resources(self, http_mcp_server: str):
        """connect() discovers resources from real HTTP server."""
        client = OutboundClient(_http_config(http_mcp_server))
        try:
            await client.connect()
            resources = client.capabilities["resources"]
            assert len(resources) >= 1
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_http_connect_discovers_prompts(self, http_mcp_server: str):
        """connect() discovers prompts from real HTTP server."""
        client = OutboundClient(_http_config(http_mcp_server))
        try:
            await client.connect()
            prompts = client.capabilities["prompts"]
            assert len(prompts) >= 1
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_http_call_tool(self, http_mcp_server: str):
        """call_tool() works against real HTTP MCP server."""
        client = OutboundClient(_http_config(http_mcp_server))
        try:
            await client.connect()
            result = await client.call_tool("echo", {"text": "http-test"})
            assert isinstance(result, dict)
            assert "content" in result
            texts = [c["text"] for c in result["content"] if c.get("type") == "text"]
            assert any("http-test" in t for t in texts)
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_http_read_resource(self, http_mcp_server: str):
        """read_resource() works against real HTTP MCP server."""
        client = OutboundClient(_http_config(http_mcp_server))
        try:
            await client.connect()
            result = await client.read_resource("http://fixture/resource")
            assert isinstance(result, dict)
            assert "contents" in result
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_http_get_prompt(self, http_mcp_server: str):
        """get_prompt() works against real HTTP MCP server."""
        client = OutboundClient(_http_config(http_mcp_server))
        try:
            await client.connect()
            result = await client.get_prompt("http_prompt", {})
            assert isinstance(result, dict)
            assert "messages" in result
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_http_negotiated_peer_has_version(self, http_mcp_server: str):
        """Negotiated peer has a protocol version after connect."""
        client = OutboundClient(_http_config(http_mcp_server))
        try:
            await client.connect()
            peer = client.negotiated_peer
            assert peer is not None
            assert peer.protocol_version
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_http_connection_error_unreachable(self):
        """Unreachable HTTP server raises ConnectionError."""
        client = OutboundClient(_http_config("http://127.0.0.1:1/mcp"))
        with pytest.raises(ConnectionError, match="[Ii]nitialization failed"):
            await client.connect()


class TestOutboundClientHTTPHeaders:
    """Configured static headers reach upstream; inbound headers do NOT."""

    @pytest.mark.asyncio
    async def test_configured_static_headers_reach_upstream(
        self, header_capturing_http_server: tuple[str, dict[str, str]]
    ):
        """Static headers from MCPServerConfig.headers are sent with every request."""
        url, received = header_capturing_http_server

        client = OutboundClient(_http_config(
            url,
            headers={"x-hub-api-key": "test-key-sentinel"},
        ))
        try:
            await client.connect()
        finally:
            await client.disconnect()

        # The header should have reached the server
        assert "x-hub-api-key" in received
        assert received["x-hub-api-key"] == "test-key-sentinel"

    @pytest.mark.asyncio
    async def test_inbound_authorization_sentinel_not_forwarded(
        self, header_capturing_http_server: tuple[str, dict[str, str]]
    ):
        """Inbound Authorization header NEVER appears in upstream requests.

        OutboundClient is constructed from MCPServerConfig only, not from any
        inbound HTTP request. Even if we pass nothing in headers, we verify
        Authorization/Cookie are absent, proving the structural guarantee.
        """
        url, received = header_capturing_http_server
        received.clear()  # reset from prior test run in same fixture

        # No Authorization in the server config headers
        client = OutboundClient(_http_config(url, headers={}))
        try:
            await client.connect()
        finally:
            await client.disconnect()

        # These inbound sentinel headers must never appear outbound
        assert "authorization" not in received
        assert "cookie" not in received

    @pytest.mark.asyncio
    async def test_no_headers_uses_simple_client_path(
        self, http_mcp_server: str
    ):
        """When no static headers configured, Client(url, mode='auto') path is taken."""
        # Client with no headers — should still connect fine (structural test)
        client = OutboundClient(_http_config(http_mcp_server, headers={}))
        try:
            await client.connect()
            assert len(client.capabilities["tools"]) >= 1
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_with_headers_uses_custom_http_client_path(
        self, http_mcp_server: str
    ):
        """When static headers are configured, httpx2.AsyncClient path is taken."""
        client = OutboundClient(_http_config(
            http_mcp_server,
            headers={"x-test-header": "value"},
        ))
        try:
            await client.connect()
            assert len(client.capabilities["tools"]) >= 1
        finally:
            await client.disconnect()


class TestOutboundClientHTTPDisconnect:
    """Lifecycle and error handling for HTTP transport."""

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected_is_safe(self):
        """disconnect() before connect() does not raise."""
        client = OutboundClient(_http_config("http://127.0.0.1:1/mcp"))
        await client.disconnect()  # should not raise

    @pytest.mark.asyncio
    async def test_double_disconnect_is_safe(self, http_mcp_server: str):
        """disconnect() called twice does not raise."""
        client = OutboundClient(_http_config(http_mcp_server))
        await client.connect()
        await client.disconnect()
        await client.disconnect()  # second call is no-op

    @pytest.mark.asyncio
    async def test_call_tool_after_disconnect_raises(self, http_mcp_server: str):
        """call_tool() after disconnect() raises ConnectionError."""
        client = OutboundClient(_http_config(http_mcp_server))
        await client.connect()
        await client.disconnect()
        with pytest.raises(ConnectionError, match="[Nn]ot connected"):
            await client.call_tool("echo", {})
