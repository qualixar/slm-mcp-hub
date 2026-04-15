"""MCPConnection tests — unit tests by method, no hanging reader loops."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection


def _cfg(**kw):
    defaults = dict(name="test", transport="stdio", command="echo", args=("hi",))
    defaults.update(kw)
    return MCPServerConfig(**defaults)


class TestMCPConnectionProperties:
    def test_initial_state(self):
        c = MCPConnection(_cfg())
        assert c.name == "test"
        assert c.state == ConnectionState.DISCONNECTED
        assert c.is_connected is False
        assert c.uptime_seconds == 0.0
        assert c.capabilities == {"tools": [], "resources": [], "resource_templates": [], "prompts": []}


class TestMCPConnectionDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_noop_when_not_connected(self):
        c = MCPConnection(_cfg())
        await c.disconnect()
        assert c.state == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_cancels_reader(self):
        c = MCPConnection(_cfg())
        c._reader_task = asyncio.create_task(asyncio.sleep(999))
        await c.disconnect()
        assert c._reader_task is None

    @pytest.mark.asyncio
    async def test_disconnect_terminates_process(self):
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        c._process = proc
        await c.disconnect()
        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_kills_on_timeout(self):
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        c._process = proc
        await c.disconnect()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_kills_on_process_lookup_error(self):
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=ProcessLookupError)
        c._process = proc
        await c.disconnect()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_fails_pending_futures(self):
        c = MCPConnection(_cfg())
        future = asyncio.get_event_loop().create_future()
        c._pending[1] = future
        await c.disconnect()
        assert future.done()
        with pytest.raises(ConnectionError):
            future.result()


class TestMCPConnectionErrors:
    @pytest.mark.asyncio
    async def test_connect_command_not_found(self):
        c = MCPConnection(_cfg(command="/no/such/binary_xyz_999"))
        with pytest.raises(ConnectionError, match="Command not found"):
            await c.connect()

    @pytest.mark.asyncio
    async def test_connect_os_error(self):
        with patch("slm_mcp_hub.federation.connection.asyncio.create_subprocess_exec",
                    AsyncMock(side_effect=OSError("Permission denied"))):
            c = MCPConnection(_cfg())
            with pytest.raises(ConnectionError, match="Failed to start"):
                await c.connect()
            assert c.state == ConnectionState.ERROR

    @pytest.mark.asyncio
    async def test_connect_http_connection_error(self):
        """HTTP transport raises ConnectionError on unreachable server."""
        c = MCPConnection(_cfg(transport="http", url="http://127.0.0.1:1/mcp"))
        with pytest.raises(ConnectionError, match="initialization failed"):
            await c.connect()

    @pytest.mark.asyncio
    async def test_call_tool_not_connected(self):
        c = MCPConnection(_cfg())
        with pytest.raises(ConnectionError, match="not connected"):
            await c.call_tool("x", {})

    @pytest.mark.asyncio
    async def test_send_notification_not_connected(self):
        c = MCPConnection(_cfg())
        with pytest.raises(ConnectionError, match="not connected"):
            await c._send_notification("x", {})


class TestMCPConnectionSendRequest:
    """Test _send_request directly with a mocked process (no reader loop)."""

    @pytest.mark.asyncio
    async def test_send_request_writes_json(self):
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        c._process = proc

        # Pre-resolve the future so _send_request doesn't hang
        async def _resolve():
            await asyncio.sleep(0.01)
            future = c._pending.get(1)
            if future and not future.done():
                future.set_result({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

        asyncio.create_task(_resolve())
        result = await c._send_request("test_method", {"arg": 1})
        assert result == {"ok": True}
        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode().strip())
        assert msg["method"] == "test_method"
        assert msg["id"] == 1

    @pytest.mark.asyncio
    async def test_send_request_error_response(self):
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        c._process = proc

        async def _resolve_error():
            await asyncio.sleep(0.01)
            future = c._pending.get(1)
            if future and not future.done():
                future.set_result({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}})

        asyncio.create_task(_resolve_error())
        with pytest.raises(RuntimeError, match="boom"):
            await c._send_request("bad", {})

    @pytest.mark.asyncio
    async def test_send_request_timeout(self):
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        c._process = proc

        # Patch timeout to be very short
        with patch("slm_mcp_hub.federation.connection.MCP_REQUEST_TIMEOUT_MS", 50):
            with pytest.raises(TimeoutError, match="timed out"):
                await c._send_request("slow", {})

    @pytest.mark.asyncio
    async def test_send_notification_writes_no_id(self):
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        c._process = proc

        await c._send_notification("notifications/initialized", {})
        written = proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode().strip())
        assert "id" not in msg
        assert msg["method"] == "notifications/initialized"


class TestMCPConnectionReader:
    """Test _read_stdout directly."""

    @pytest.mark.asyncio
    async def test_reader_resolves_pending(self):
        c = MCPConnection(_cfg())
        response = {"jsonrpc": "2.0", "id": 42, "result": {"data": "ok"}}
        lines = [(json.dumps(response) + "\n").encode(), b""]

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=lines)

        proc = MagicMock()
        proc.stdout = mock_stdout
        c._process = proc

        future = asyncio.get_event_loop().create_future()
        c._pending[42] = future

        await c._read_stdout()
        assert future.done()
        assert future.result()["result"]["data"] == "ok"

    @pytest.mark.asyncio
    async def test_reader_ignores_non_json(self):
        c = MCPConnection(_cfg())
        lines = [b"not json at all\n", b""]

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=lines)

        proc = MagicMock()
        proc.stdout = mock_stdout
        c._process = proc

        await c._read_stdout()  # Should not raise

    @pytest.mark.asyncio
    async def test_reader_handles_notification(self):
        c = MCPConnection(_cfg())
        notification = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        lines = [(json.dumps(notification) + "\n").encode(), b""]

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=lines)

        proc = MagicMock()
        proc.stdout = mock_stdout
        c._process = proc

        await c._read_stdout()  # Notifications are logged, not errors

    @pytest.mark.asyncio
    async def test_reader_skips_empty_lines(self):
        c = MCPConnection(_cfg())
        lines = [b"\n", b"  \n", b""]

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=lines)

        proc = MagicMock()
        proc.stdout = mock_stdout
        c._process = proc

        await c._read_stdout()  # Should not raise

    @pytest.mark.asyncio
    async def test_reader_handles_exception(self):
        c = MCPConnection(_cfg())
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=RuntimeError("read failure"))

        proc = MagicMock()
        proc.stdout = mock_stdout
        c._process = proc

        await c._read_stdout()  # Error caught, state set
        assert c.state == ConnectionState.ERROR

    @pytest.mark.asyncio
    async def test_reader_cancellation(self):
        c = MCPConnection(_cfg())
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=asyncio.CancelledError)

        proc = MagicMock()
        proc.stdout = mock_stdout
        c._process = proc

        await c._read_stdout()  # CancelledError caught


class TestMCPConnectionUptime:
    """Test uptime and connect edge cases."""

    def test_uptime_zero_when_not_connected(self):
        """uptime_seconds returns 0.0 when connected_at is 0."""
        c = MCPConnection(_cfg())
        assert c._connected_at == 0
        assert c.uptime_seconds == 0.0

    def test_uptime_positive_when_connected(self):
        """uptime_seconds returns positive value when connected_at is set."""
        c = MCPConnection(_cfg())
        c._connected_at = 1000.0
        with patch("slm_mcp_hub.federation.connection.time.time", return_value=1005.0):
            assert c.uptime_seconds == 5.0

    @pytest.mark.asyncio
    async def test_connect_already_connected_returns_early(self):
        """connect() returns immediately when already connected."""
        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        await c.connect()  # Should not raise, should be a no-op
        assert c.state == ConnectionState.CONNECTED


class TestMCPConnectionGetPrompt:
    """Test get_prompt method."""

    @pytest.mark.asyncio
    async def test_get_prompt_success(self):
        """get_prompt sends prompts/get request and returns result."""
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        c._process = proc

        async def _resolve():
            await asyncio.sleep(0.01)
            future = c._pending.get(1)
            if future and not future.done():
                future.set_result({
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"messages": [{"role": "user", "content": "hello"}]},
                })

        asyncio.create_task(_resolve())
        result = await c.get_prompt("test_prompt", {"arg": "value"})
        assert "messages" in result

        written = proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode().strip())
        assert msg["method"] == "prompts/get"
        assert msg["params"]["name"] == "test_prompt"

    @pytest.mark.asyncio
    async def test_read_resource_sends_request(self):
        """read_resource sends resources/read request."""
        c = MCPConnection(_cfg())
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        c._process = proc

        async def _resolve():
            await asyncio.sleep(0.01)
            future = c._pending.get(1)
            if future and not future.done():
                future.set_result({
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"contents": [{"text": "data"}]},
                })

        asyncio.create_task(_resolve())
        result = await c.read_resource("file:///test.txt")
        assert "contents" in result

        written = proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode().strip())
        assert msg["method"] == "resources/read"


class TestMCPConnectionStdioHandshake:
    """Test _connect_stdio happy path by mocking send/discover internals."""

    @pytest.mark.asyncio
    async def test_connect_stdio_happy_path(self):
        """_connect_stdio completes initialization handshake."""
        c = MCPConnection(_cfg())

        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()

        # Reader needs an empty line to terminate
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")
        mock_stderr = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()

        # Mock _send_request and _send_notification to skip the real JSON-RPC wire
        original_send_request = c._send_request

        call_count = 0

        async def mock_send_request(method, params):
            nonlocal call_count
            call_count += 1
            if method == "initialize":
                return {"protocolVersion": "2024-11-05"}
            elif method == "tools/list":
                return {"tools": [{"name": "t1"}]}
            elif method == "resources/list":
                return {"resources": []}
            elif method == "resources/templates/list":
                return {"resourceTemplates": []}
            elif method == "prompts/list":
                return {"prompts": []}
            return {}

        async def mock_send_notification(method, params):
            pass

        with patch(
            "slm_mcp_hub.federation.connection.asyncio.create_subprocess_exec",
            AsyncMock(return_value=mock_proc),
        ):
            c._send_request = mock_send_request
            c._send_notification = mock_send_notification
            await c._connect_stdio()

        assert c.state == ConnectionState.CONNECTED
        assert c.is_connected is True
        assert len(c.capabilities["tools"]) == 1
        assert c._connected_at > 0
        assert call_count >= 5  # init + 4 discover calls

        await c.disconnect()

    @pytest.mark.asyncio
    async def test_connect_stdio_init_exception_transitions_to_error(self):
        """_connect_stdio sets ERROR and disconnects on handshake failure."""
        c = MCPConnection(_cfg())

        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()

        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")
        mock_stderr = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        async def mock_send_fail(method, params):
            raise RuntimeError("MCP process died")

        with patch(
            "slm_mcp_hub.federation.connection.asyncio.create_subprocess_exec",
            AsyncMock(return_value=mock_proc),
        ):
            c._send_request = mock_send_fail
            with pytest.raises(ConnectionError, match="initialization failed"):
                await c._connect_stdio()

        assert c.state == ConnectionState.DISCONNECTED


class TestMCPConnectionDiscovery:
    """Test _discover_capabilities with pre-resolved futures."""

    @pytest.mark.asyncio
    async def test_discover_all_capabilities(self):
        c = MCPConnection(_cfg())
        c._send_request = AsyncMock(side_effect=[
            {"tools": [{"name": "a"}]},
            {"resources": [{"uri": "b"}]},
            {"resourceTemplates": [{"uriTemplate": "c"}]},
            {"prompts": [{"name": "d"}]},
        ])
        await c._discover_capabilities()
        assert len(c.capabilities["tools"]) == 1
        assert len(c.capabilities["resources"]) == 1
        assert len(c.capabilities["resource_templates"]) == 1
        assert len(c.capabilities["prompts"]) == 1

    @pytest.mark.asyncio
    async def test_discover_tools_failure(self):
        c = MCPConnection(_cfg())
        c._send_request = AsyncMock(side_effect=[
            RuntimeError("no tools"),
            {"resources": []},
            {"resourceTemplates": []},
            {"prompts": []},
        ])
        await c._discover_capabilities()
        assert c.capabilities["tools"] == []

    @pytest.mark.asyncio
    async def test_discover_resources_failure(self):
        c = MCPConnection(_cfg())
        c._send_request = AsyncMock(side_effect=[
            {"tools": []},
            RuntimeError("no resources"),
            {"resourceTemplates": []},
            {"prompts": []},
        ])
        await c._discover_capabilities()
        assert c.capabilities["resources"] == []

    @pytest.mark.asyncio
    async def test_discover_templates_failure(self):
        c = MCPConnection(_cfg())
        c._send_request = AsyncMock(side_effect=[
            {"tools": []},
            {"resources": []},
            RuntimeError("no templates"),
            {"prompts": []},
        ])
        await c._discover_capabilities()
        assert c.capabilities["resource_templates"] == []

    @pytest.mark.asyncio
    async def test_discover_prompts_failure(self):
        c = MCPConnection(_cfg())
        c._send_request = AsyncMock(side_effect=[
            {"tools": []},
            {"resources": []},
            {"resourceTemplates": []},
            RuntimeError("no prompts"),
        ])
        await c._discover_capabilities()
        assert c.capabilities["prompts"] == []
