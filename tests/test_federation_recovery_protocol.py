"""Protocol and recovery regressions for the public federation boundary.

These tests exercise behavior that normal happy-path federation tests do not:
HTTP session handling, malformed upstream responses, lifecycle shutdown, and
partial reload failures.  They deliberately use realistic MCP JSON-RPC frames
rather than inspecting implementation-only state.
"""

from __future__ import annotations

import asyncio
import io
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.manager import ConnectionManager
from slm_mcp_hub.lifecycle.notifier import ChangeNotifier
from slm_mcp_hub.lifecycle.reloader import ConfigReloader
from slm_mcp_hub.plugins.slm_plugin import SLMPlugin


def _server(name: str = "srv", **kwargs: object) -> MCPServerConfig:
    values: dict[str, object] = {
        "name": name,
        "transport": "stdio",
        "command": "echo",
        "args": (),
    }
    values.update(kwargs)
    return MCPServerConfig(**values)  # type: ignore[arg-type]


def _connection(**kwargs: object) -> MCPConnection:
    return MCPConnection(_server(**kwargs))


def _connected_mock(tools: list[dict] | None = None) -> MagicMock:
    connection = MagicMock()
    connection.is_connected = True
    connection.capabilities = {
        "tools": tools or [], "resources": [], "resource_templates": [], "prompts": [],
    }
    connection.connect = AsyncMock()
    connection.disconnect = AsyncMock()
    connection.drain_and_disconnect = AsyncMock()
    return connection


class TestHttpMcpProtocol:
    @pytest.mark.asyncio
    async def test_http_response_carries_session_id_to_next_request(self) -> None:
        connection = _connection(transport="http", url="https://mcp.example.test")
        client = AsyncMock()
        first = MagicMock(
            status_code=200,
            headers={"mcp-session-id": "session-42", "content-type": "application/json"},
        )
        first.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        second = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
        )
        second.json.return_value = {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
        client.post = AsyncMock(side_effect=[first, second])
        connection._http_client = client
        connection._http_url = "https://mcp.example.test"

        assert await connection._send_request_http("tools/list", {}) == {"tools": []}
        assert await connection._send_request_http("tools/call", {"name": "ping"}) == {"ok": True}
        assert client.post.await_args_list[1].kwargs["headers"]["Mcp-Session-Id"] == "session-42"

    @pytest.mark.asyncio
    async def test_http_notification_uses_existing_session_and_tolerates_network_failure(self) -> None:
        connection = _connection(transport="http", url="https://mcp.example.test")
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("offline"))
        connection._http_client = client
        connection._http_url = "https://mcp.example.test"
        connection._http_session_id = "existing-session"

        await connection._send_notification("notifications/initialized", {})

        assert client.post.await_args.kwargs["headers"]["Mcp-Session-Id"] == "existing-session"

    @pytest.mark.asyncio
    async def test_http_204_is_a_valid_empty_jsonrpc_response(self) -> None:
        connection = _connection(transport="http", url="https://mcp.example.test")
        client = AsyncMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=204, headers={}))
        connection._http_client = client
        connection._http_url = "https://mcp.example.test"

        assert await connection._send_request_http("notifications/initialized", {}) == {}

    @pytest.mark.asyncio
    async def test_http_sse_and_error_responses_preserve_protocol_semantics(self) -> None:
        connection = _connection(transport="http", url="https://mcp.example.test")
        sse = MagicMock(status_code=200, headers={"content-type": "text/event-stream"})
        sse.text = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}\n\n"
        rpc_error = MagicMock(status_code=200, headers={"content-type": "application/json"})
        rpc_error.json.return_value = {"error": {"code": -32601, "message": "unknown method"}}
        connection._http_client = AsyncMock(post=AsyncMock(side_effect=[sse, rpc_error]))
        connection._http_url = "https://mcp.example.test"

        assert await connection._send_request_http("tools/list", {}) == {"ok": True}
        with pytest.raises(RuntimeError, match=r"\[-32601\] unknown method"):
            await connection._send_request_http("bad/method", {})

    @pytest.mark.asyncio
    async def test_http_connect_discovers_only_advertised_tools(self) -> None:
        connection = _connection(transport="http", url="https://mcp.example.test")
        client = AsyncMock()
        connection._send_request = AsyncMock(side_effect=[
            {"capabilities": {"tools": {}}},
            {"tools": [{"name": "search"}]},
        ])
        connection._send_notification = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            await connection._connect_http()

        assert connection.state is ConnectionState.CONNECTED
        assert connection.capabilities["tools"] == [{"name": "search"}]
        assert connection._send_request.await_count == 2
        await connection.disconnect()

    def test_sse_parser_skips_bad_event_data_then_falls_back_to_json(self) -> None:
        assert MCPConnection._parse_sse_response("data: not-json\ndata: {\"result\": {\"x\": 1}}") == {"result": {"x": 1}}
        assert MCPConnection._parse_sse_response('{"result": {"fallback": true}}') == {"result": {"fallback": True}}
        assert MCPConnection._parse_sse_response("data: still-not-json") == {
            "error": {"code": -32700, "message": "Could not parse SSE response"},
        }


class TestConnectionRecovery:
    @pytest.mark.asyncio
    async def test_disconnect_cleans_stderr_and_http_client_even_when_close_fails(self) -> None:
        connection = _connection()
        connection._stderr_task = asyncio.create_task(asyncio.sleep(60))
        http_client = AsyncMock()
        http_client.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
        connection._http_client = http_client

        await connection.disconnect()

        assert connection._stderr_task is None
        assert connection._http_client is None
        http_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stderr_drain_retains_decoded_tail_and_exit_diagnostic_is_actionable(self) -> None:
        connection = _connection(command="server", args=("a", "b", "c", "d", "e"))
        stderr = AsyncMock()
        stderr.readline = AsyncMock(side_effect=[b"first\n", b"\xffsecond\n", b""])
        process = MagicMock(returncode=17)
        process.stderr = stderr
        connection._process = process

        await connection._drain_stderr()
        diagnostic = connection._exit_diagnostic()

        assert "exit code 17" in diagnostic
        assert "server a b c d ..." in diagnostic
        assert "first" in diagnostic and "second" in diagnostic

    @pytest.mark.asyncio
    async def test_stderr_drain_handles_closed_or_broken_pipe(self) -> None:
        connection = _connection()
        await connection._drain_stderr()  # no process/stdout pipe is an expected shutdown state
        broken = AsyncMock()
        broken.readline = AsyncMock(side_effect=RuntimeError("pipe closed"))
        connection._process = MagicMock(stderr=broken)
        await connection._drain_stderr()


class TestManagerRecovery:
    @pytest.mark.asyncio
    async def test_connect_all_prioritizes_local_then_http_and_registers_both(self) -> None:
        local = _server("local")
        remote = _server("remote", transport="http", url="https://mcp.example.test")
        manager = ConnectionManager(HubConfig(mcp_servers=(local, remote)), CapabilityRegistry())
        created: list[str] = []

        def factory(config: MCPServerConfig) -> MagicMock:
            created.append(config.name)
            return _connected_mock([{"name": config.name}])

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=factory):
            assert await manager.connect_all() == {}

        assert created == ["local", "remote"]
        assert manager.connected_count == 2
        assert manager._registry.tool_count == 2
        await manager.disconnect_all()

    @pytest.mark.asyncio
    async def test_reconnect_replaces_live_connection_and_reports_failure(self) -> None:
        config = _server("alpha")
        manager = ConnectionManager(HubConfig(mcp_servers=(config,)), CapabilityRegistry())
        old = _connected_mock()
        manager._connections["alpha"] = old
        manager._connect_timed = AsyncMock()

        assert await manager.reconnect("alpha") == (True, "Connected: 0 tools")
        old.disconnect.assert_awaited_once()
        manager._failed["alpha"] = "refused"
        assert await manager.reconnect("alpha") == (False, "Failed: refused")
        assert await manager.reconnect("missing") == (False, "Server 'missing' not found in config")

    @pytest.mark.asyncio
    async def test_timeout_disconnects_child_and_exposes_failed_server_status(self) -> None:
        config = _server("slow")
        manager = ConnectionManager(HubConfig(mcp_servers=(config,)), CapabilityRegistry())
        slow = _connected_mock()

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        slow.connect = wait_forever
        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=slow):
            await manager._connect_timed(config, timeout_seconds=0.001)

        assert manager.failed_servers["slow"] == "Connection timed out after 0s"
        slow.disconnect.assert_awaited_once()
        status = manager.get_server_status()
        assert status[0]["error"] == "Connection timed out after 0s"
        assert status[0]["connected"] is True

    @pytest.mark.asyncio
    async def test_disconnect_all_cancels_retry_and_clears_registry(self) -> None:
        config = _server("alpha")
        registry = CapabilityRegistry()
        manager = ConnectionManager(HubConfig(mcp_servers=(config,)), registry)
        connection = _connected_mock([{"name": "tool"}])
        manager._connections["alpha"] = connection
        manager._sync_registry()
        manager._failed["alpha"] = "temporary"
        manager._retry_task = asyncio.create_task(asyncio.sleep(60))

        await manager.disconnect_all()

        assert manager.connections == {}
        assert manager.failed_servers == {}
        assert registry.tool_count == 0
        connection.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fast_retry_ignores_removed_config_and_isolates_old_disconnect_error(self) -> None:
        config = _server("present")
        manager = ConnectionManager(HubConfig(mcp_servers=(config,)), CapabilityRegistry())
        stale = _connected_mock()
        stale.disconnect = AsyncMock(side_effect=RuntimeError("already gone"))
        manager._connections["present"] = stale
        manager._failed = {"present": "fail", "removed": "fail"}

        async def successful_retry(_: MCPServerConfig) -> None:
            manager._failed.clear()

        manager._connect_timed = AsyncMock(side_effect=successful_retry)
        with patch("slm_mcp_hub.federation.manager.asyncio.sleep", AsyncMock()):
            assert await manager.fast_retry_failed() == {}

        stale.disconnect.assert_awaited_once()
        manager._connect_timed.assert_awaited_once_with(config)


class TestLifecycleRecovery:
    @pytest.mark.asyncio
    async def test_notifier_shutdown_cancels_debounce_and_clears_subscribers(self) -> None:
        notifier = ChangeNotifier(debounce_seconds=60)
        notifier.subscribe("client", lambda _: None)
        await notifier.notify_tools_changed()

        await notifier.shutdown()

        assert notifier.subscriber_count == 0
        assert notifier._pending_task is None

    @pytest.mark.asyncio
    async def test_notifier_keeps_async_subscriber_after_its_callback_fails(self) -> None:
        notifier = ChangeNotifier(debounce_seconds=0)

        async def failing(_: dict) -> None:
            raise RuntimeError("consumer disconnected")

        notifier.subscribe("client", failing)
        await notifier._broadcast({"method": "notifications/tools/list_changed"})
        assert notifier.subscriber_count == 1
        await notifier.shutdown()

    @pytest.mark.asyncio
    async def test_reloader_continues_each_operation_after_false_or_exception(self) -> None:
        old = _server("remove")
        modify_old = _server("modify", args=("old",))
        modify_new = _server("modify", args=("new",))
        added = _server("add")
        manager = MagicMock()
        manager.config = HubConfig(mcp_servers=(old, modify_old))
        manager.remove_server = AsyncMock(return_value=(False, "already absent"))
        manager.replace_server = AsyncMock(side_effect=RuntimeError("bad replacement"))
        manager.add_server = AsyncMock(return_value=(False, "refused"))
        notifier = MagicMock(notify_tools_changed=AsyncMock())
        reloader = ConfigReloader(manager, notifier, drain_timeout_s=0.25)

        diff = await reloader.apply_config(HubConfig(mcp_servers=(modify_new, added)))

        assert diff.change_count == 3
        manager.remove_server.assert_awaited_once_with("remove", drain_timeout_s=0.25)
        manager.replace_server.assert_awaited_once_with(modify_new, drain_timeout_s=0.25)
        manager.add_server.assert_awaited_once_with(added)
        notifier.notify_tools_changed.assert_awaited_once()


class TestStdioJsonRpcBoundary:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", ["[]", "null", '"not-a-request"'])
    async def test_valid_json_but_non_object_request_returns_invalid_request(self, payload: str) -> None:
        """A public stdio server must reject JSON values that are not requests.

        This is deliberately distinct from malformed JSON: a syntactically valid
        array/string/null is a JSON-RPC invalid request (-32600), not a parse
        error (-32700), and must never crash the serving loop.
        """
        from slm_mcp_hub.server.stdio_server import StdioServer

        class Reader:
            def __init__(self) -> None:
                self._lines = [(payload + "\n").encode(), b""]

            async def readline(self) -> bytes:
                return self._lines.pop(0)

        class Writer:
            def __init__(self) -> None:
                self.frames: list[dict] = []

            def write(self, data: str) -> None:
                import json
                self.frames.append(json.loads(data))

        writer = Writer()
        sessions = MagicMock(create_session=MagicMock(return_value="session"))
        server = StdioServer(AsyncMock(), sessions)
        await server.serve(stdin=Reader(), stdout=writer)

        assert writer.frames == [{
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }]

    @pytest.mark.asyncio
    async def test_reader_errors_and_blank_frames_do_not_crash_transport(self) -> None:
        from slm_mcp_hub.server.stdio_server import StdioServer

        class Reader:
            async def readline(self) -> bytes:
                raise RuntimeError("stdin closed unexpectedly")

        server = StdioServer(AsyncMock(), MagicMock())
        await server._serve_loop(Reader(), MagicMock())

        class BlankThenEof:
            def __init__(self) -> None:
                self.lines = [b" \n", b""]

            async def readline(self) -> bytes:
                return self.lines.pop(0)

        await server._serve_loop(BlankThenEof(), MagicMock())

    @pytest.mark.asyncio
    async def test_async_writer_gets_bytes_and_flush_writer_gets_text(self) -> None:
        from slm_mcp_hub.server.stdio_server import StdioServer

        class AsyncWriter:
            def __init__(self) -> None:
                self.writes: list[bytes] = []
                self.drained = False

            def write(self, data: bytes) -> None:
                self.writes.append(data)

            async def drain(self) -> None:
                self.drained = True

        class FailingWriter:
            def write(self, _: str) -> None:
                raise OSError("broken stdout")

        server = StdioServer(AsyncMock(), MagicMock())
        async_writer = AsyncWriter()
        await server._write_message(async_writer, {"jsonrpc": "2.0", "id": 1})
        assert async_writer.writes == [b'{"jsonrpc":"2.0","id":1}\n']
        assert async_writer.drained is True
        await server._write_message(FailingWriter(), {"jsonrpc": "2.0", "id": 2})

    @pytest.mark.asyncio
    async def test_notification_callback_error_isolated_from_server_loop(self) -> None:
        from slm_mcp_hub.server.stdio_server import StdioServer

        server = StdioServer(AsyncMock(), MagicMock())
        server._write_message = AsyncMock(side_effect=RuntimeError("writer replaced"))
        await server._send_notification(MagicMock(), {"method": "notifications/tools/list_changed"})

    @pytest.mark.asyncio
    async def test_default_stdio_wrapper_wires_event_loop_and_redirects_stdout(self, monkeypatch) -> None:
        from slm_mcp_hub.server.stdio_server import StdioServer

        server = StdioServer(AsyncMock(), MagicMock())
        loop = MagicMock()
        loop.connect_read_pipe = AsyncMock()
        transport = MagicMock()
        protocol = MagicMock()
        loop.connect_write_pipe = AsyncMock(return_value=(transport, protocol))
        writer = MagicMock()
        original_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", original_stdout)
        monkeypatch.setattr(sys, "__stdout__", original_stdout)

        with (
            patch("slm_mcp_hub.server.stdio_server.asyncio.get_running_loop", return_value=loop),
            patch("slm_mcp_hub.server.stdio_server.asyncio.StreamWriter", return_value=writer),
        ):
            reader, returned_writer = await server._wrap_real_stdio()

        assert isinstance(reader, asyncio.StreamReader)
        assert returned_writer is writer
        loop.connect_read_pipe.assert_awaited_once()
        loop.connect_write_pipe.assert_awaited_once()
        assert sys.stdout is sys.stderr
        assert sys.__stdout__ is sys.stderr


class TestSlmPluginAuthRecovery:
    @pytest.mark.asyncio
    async def test_auth_rejection_disables_plugin_for_status_recall_and_event_write(self) -> None:
        plugin = SLMPlugin(slm_url="http://secured.example.test")
        client = AsyncMock()
        client.get = AsyncMock(return_value=httpx.Response(401, json={"detail": "denied"}))
        with patch("slm_mcp_hub.plugins.slm_plugin.create_slm_http_client", return_value=client):
            await plugin.on_hub_start(MagicMock())
        assert plugin.available is False

        plugin._available = True
        plugin._client = client
        client.post = AsyncMock(return_value=httpx.Response(403, json={"detail": "denied"}))
        await plugin.on_session_start("s1", {"project_path": "/project"})
        assert plugin.available is False

        plugin._available = True
        await plugin._post_tool_event("hub__tool", session_id="s1")
        assert plugin.available is False
