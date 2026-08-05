"""W4-P1 tests — route_streaming_call + outbound call_tool_streaming.

TDD: written BEFORE implementation. Verifies:
1. Progress notifications from backend are forwarded through the router.
2. Cancellation via anyio.CancelScope propagates structurally, in-flight count resets to 0.
3. Normal calls without progress_callback still succeed (regression).
4. W3 activity_fn still fires on streaming calls.
5. OutboundClient.call_tool_streaming invokes client.session.send_request with correct params.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.router import FederationRouter
from slm_mcp_hub.protocol.outbound import OutboundClient

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_registry() -> CapabilityRegistry:
    """Registry with one backend 'backend' exposing 'slow_tool'."""
    reg = CapabilityRegistry()
    reg.sync({
        "backend": {
            "tools": [{"name": "slow_tool", "description": "A slow tool"}],
            "resources": [],
            "prompts": [],
            "resource_templates": [],
        }
    })
    return reg


def _make_server_config(name: str = "backend") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="http",
        url=f"http://127.0.0.1:1/{name}",
    )


class _SlowOutbound:
    """Fake outbound that sleeps to simulate a long call; records cancellation."""

    def __init__(self, sleep_s: float = 10.0) -> None:
        self._sleep_s = sleep_s
        self.was_cancelled: bool = False
        self.progress_callback_received: bool = False

    async def call_tool_streaming(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        read_timeout_seconds: float | None = None,
        progress_callback: Any = None,
        resumption_token: str | None = None,
        on_resumption_token: Any = None,
    ) -> dict[str, Any]:
        """Sleeps for self._sleep_s; records if cancelled."""
        self.progress_callback_received = progress_callback is not None
        try:
            await asyncio.sleep(self._sleep_s)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        return {"content": [{"type": "text", "text": "done"}]}


class _ProgressOutbound:
    """Fake outbound that emits one progress event then returns."""

    async def call_tool_streaming(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        read_timeout_seconds: float | None = None,
        progress_callback: Any = None,
        resumption_token: str | None = None,
        on_resumption_token: Any = None,
    ) -> dict[str, Any]:
        if progress_callback is not None:
            await progress_callback(0.5, 1.0, "halfway")
        return {"content": [{"type": "text", "text": "result"}]}


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestRouteStreamingCallProgress:
    """Progress forwarding via route_streaming_call."""

    async def test_route_streaming_call_progress_forwarded(self) -> None:
        """route_streaming_call passes progress_callback to conn.call_tool_streaming;
        backend emits a synthetic progress event; assert callback received
        progress=0.5, total=1.0, message='halfway'."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED
        conn._outbound = _ProgressOutbound()  # type: ignore[assignment]

        received: list[tuple[float, Any, Any]] = []

        async def capture(progress: float, total: float | None, message: str | None) -> None:
            received.append((progress, total, message))

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_streaming_call(
            "backend__slow_tool",
            {},
            progress_callback=capture,
        )

        assert result.success is True
        assert result.server_name == "backend"
        assert result.tool_name == "slow_tool"
        assert len(received) == 1
        assert received[0] == (0.5, 1.0, "halfway")

    async def test_route_streaming_call_no_progress_callback(self) -> None:
        """progress_callback=None → call succeeds, no ProgressBridge built, result correct."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED
        conn._outbound = _ProgressOutbound()  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_streaming_call("backend__slow_tool", {})

        assert result.success is True
        assert result.result["content"][0]["text"] == "result"

    async def test_route_streaming_call_passes_progress_callback_to_outbound(self) -> None:
        """Verify the outbound receives the progress_callback (not None)."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED
        slow = _SlowOutbound(sleep_s=0.0)  # fast — no sleep
        conn._outbound = slow  # type: ignore[assignment]

        async def noop_progress(p: float, t: float | None, m: str | None) -> None:
            pass

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_streaming_call(
            "backend__slow_tool",
            {},
            progress_callback=noop_progress,
        )

        assert result.success is True
        assert slow.progress_callback_received is True


class TestRouteStreamingCallCancellation:
    """Structural cancellation via anyio.CancelScope."""

    async def test_cancel_aborts_backend_call(self) -> None:
        """HARD CASE: CancelScope with 0.15s deadline aborts a 10s backend call.

        Asserts:
        (a) call is cancelled within 0.5s (wall-clock bound),
        (b) in_flight_count returns to 0 (no slot leak),
        (c) the outbound stub recorded cancellation (structural cancel proved).
        """
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED
        slow = _SlowOutbound(sleep_s=10.0)
        conn._outbound = slow  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})

        start = time.monotonic()
        with anyio.CancelScope() as scope:
            scope.deadline = anyio.current_time() + 0.15
            await router.route_streaming_call("backend__slow_tool", {})

        elapsed = time.monotonic() - start

        # (a) cancelled within bound
        assert scope.cancelled_caught, "CancelScope should have caught the cancellation"
        assert elapsed < 0.5, f"Should cancel within 0.5s, took {elapsed:.3f}s"
        # (b) no in-flight slot leak
        assert conn.in_flight_count == 0, "in_flight_count must return to 0 after cancellation"
        # (c) outbound stub was structurally cancelled
        assert slow.was_cancelled is True, "Backend stub must have received CancelledError"

    async def test_cancelled_scope_does_not_leave_in_flight_leak(self) -> None:
        """Second independent cancellation test: in_flight_count is verified to be 0
        both before and after the call (boundary condition check)."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED
        conn._outbound = _SlowOutbound(sleep_s=5.0)  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})

        assert conn.in_flight_count == 0, "should start at 0"

        with anyio.CancelScope() as scope:
            scope.deadline = anyio.current_time() + 0.05
            await router.route_streaming_call("backend__slow_tool", {})

        assert scope.cancelled_caught
        assert conn.in_flight_count == 0, "must be back to 0 after cancel"


class TestRouteStreamingCallRegression:
    """Regression — W3 activity/in-flight semantics preserved."""

    async def test_w3_activity_fn_fires_on_success(self) -> None:
        """activity_fn is called after a successful streaming call (W3 regression)."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED
        conn._outbound = _ProgressOutbound()  # type: ignore[assignment]

        activity_log: list[str] = []
        router = FederationRouter(reg, {"backend": conn}, activity_fn=activity_log.append)

        await router.route_streaming_call("backend__slow_tool", {})

        assert "backend" in activity_log, "activity_fn must be called with server name"

    async def test_w3_in_flight_incremented_and_decremented(self) -> None:
        """in_flight_count is 1 during the call and 0 after completion."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED

        in_flight_during_call: list[int] = []

        class _InspectOutbound:
            async def call_tool_streaming(self, *a: Any, **kw: Any) -> dict[str, Any]:
                in_flight_during_call.append(conn.in_flight_count)
                return {"content": [{"type": "text", "text": "ok"}]}

        conn._outbound = _InspectOutbound()  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_streaming_call("backend__slow_tool", {})

        assert result.success is True
        assert in_flight_during_call == [1], "in_flight must be 1 during the call"
        assert conn.in_flight_count == 0, "must be 0 after completion"

    async def test_route_streaming_call_tool_not_found(self) -> None:
        """route_streaming_call for an unknown tool returns isError result."""
        reg = CapabilityRegistry()
        router = FederationRouter(reg, {})
        result = await router.route_streaming_call("unknown__tool", {})
        assert result.success is False
        assert "not found" in result.result["content"][0]["text"]

    async def test_route_streaming_call_server_disconnected(self) -> None:
        """route_streaming_call for a disconnected server returns isError result."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        # state = DISCONNECTED (default)

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_streaming_call("backend__slow_tool", {})
        assert result.success is False

    async def test_normal_route_tool_call_still_works(self) -> None:
        """route_tool_call (W3) still works after W4-P1 modifications (regression)."""
        reg = _make_registry()
        mock_conn = AsyncMock(spec=MCPConnection)
        mock_conn.is_connected = True
        mock_conn.call_tool = AsyncMock(
            return_value={"content": [{"type": "text", "text": "legacy"}]}
        )
        router = FederationRouter(reg, {"backend": mock_conn})
        result = await router.route_tool_call("backend__slow_tool", {})
        assert result.success is True
        assert result.result["content"][0]["text"] == "legacy"


class TestOutboundCallToolStreaming:
    """OutboundClient.call_tool_streaming unit tests."""

    async def test_outbound_call_tool_streaming_invokes_session_send_request(self) -> None:
        """OutboundClient.call_tool_streaming calls self._client.session.send_request
        with CallToolRequest, CallToolResult result_type, and progress_callback kwarg."""
        from mcp import types

        config = _make_server_config("test_server")
        outbound = OutboundClient(config)

        # Build mock result
        mock_content = MagicMock()
        mock_content.model_dump.return_value = {"type": "text", "text": "streamed_result"}
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_result.is_error = False

        mock_session = AsyncMock()
        mock_session.send_request = AsyncMock(return_value=mock_result)

        mock_client = MagicMock()
        mock_client.session = mock_session

        outbound._client = mock_client
        outbound._connected = True

        async def noop_progress(p: float, t: float | None, m: str | None) -> None:
            pass

        result = await outbound.call_tool_streaming(
            "my_tool",
            {"key": "value"},
            read_timeout_seconds=45.0,
            progress_callback=noop_progress,
        )

        # Verify send_request was called
        assert mock_session.send_request.call_count == 1
        call_args = mock_session.send_request.call_args

        # First positional arg: CallToolRequest
        req = call_args[0][0]
        assert isinstance(req, types.CallToolRequest)
        assert req.params.name == "my_tool"
        assert req.params.arguments == {"key": "value"}

        # Second positional arg: result type
        result_type = call_args[0][1]
        assert result_type is types.CallToolResult

        # Keyword args
        assert call_args.kwargs.get("request_read_timeout_seconds") == 45.0
        assert call_args.kwargs.get("progress_callback") is noop_progress

        # Return value is a plain dict
        assert result == {"content": [{"type": "text", "text": "streamed_result"}]}

    async def test_outbound_call_tool_streaming_not_connected_raises(self) -> None:
        """call_tool_streaming raises ConnectionError when not connected."""
        config = _make_server_config("test")
        outbound = OutboundClient(config)
        # Do not connect

        with pytest.raises(ConnectionError, match="Not connected"):
            await outbound.call_tool_streaming("tool", {})

    async def test_outbound_call_tool_streaming_with_metadata(self) -> None:
        """call_tool_streaming passes ClientMessageMetadata when resumption_token given."""
        from mcp.client.streamable_http import ClientMessageMetadata

        config = _make_server_config("test")
        outbound = OutboundClient(config)

        mock_content = MagicMock()
        mock_content.model_dump.return_value = {"type": "text", "text": "ok"}
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_result.is_error = False

        mock_session = AsyncMock()
        mock_session.send_request = AsyncMock(return_value=mock_result)
        mock_client = MagicMock()
        mock_client.session = mock_session

        outbound._client = mock_client
        outbound._connected = True

        async def token_update(token: str) -> None:
            pass

        await outbound.call_tool_streaming(
            "tool",
            {},
            resumption_token="tok_abc",
            on_resumption_token=token_update,
        )

        call_kwargs = mock_session.send_request.call_args.kwargs
        metadata = call_kwargs.get("metadata")
        assert metadata is not None
        assert isinstance(metadata, ClientMessageMetadata)
        assert metadata.resumption_token == "tok_abc"

    async def test_outbound_call_tool_streaming_no_metadata_when_no_token(self) -> None:
        """call_tool_streaming passes metadata=None when no resumption_token given."""

        config = _make_server_config("test")
        outbound = OutboundClient(config)

        mock_content = MagicMock()
        mock_content.model_dump.return_value = {"type": "text", "text": "ok"}
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_result.is_error = False

        mock_session = AsyncMock()
        mock_session.send_request = AsyncMock(return_value=mock_result)
        mock_client = MagicMock()
        mock_client.session = mock_session

        outbound._client = mock_client
        outbound._connected = True

        await outbound.call_tool_streaming("tool", {})

        call_kwargs = mock_session.send_request.call_args.kwargs
        assert call_kwargs.get("metadata") is None


class TestConnectionCallToolStreaming:
    """MCPConnection.call_tool_streaming — delegates to _dispatch and outbound."""

    async def test_connection_call_tool_streaming_delegates_to_outbound(self) -> None:
        """MCPConnection.call_tool_streaming calls outbound.call_tool_streaming
        and returns its result."""
        config = _make_server_config("test")
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED

        class _FastOutbound:
            async def call_tool_streaming(self, name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
                return {"content": [{"type": "text", "text": f"result:{name}"}]}

        conn._outbound = _FastOutbound()  # type: ignore[assignment]

        result = await conn.call_tool_streaming("my_tool", {"a": 1})

        assert result == {"content": [{"type": "text", "text": "result:my_tool"}]}

    async def test_connection_call_tool_streaming_tracks_in_flight(self) -> None:
        """in_flight_count is correct during and after call_tool_streaming."""
        config = _make_server_config("test")
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED

        snapshots: list[int] = []

        class _SnapshotOutbound:
            async def call_tool_streaming(self, *a: Any, **kw: Any) -> dict[str, Any]:
                snapshots.append(conn.in_flight_count)
                return {"content": []}

        conn._outbound = _SnapshotOutbound()  # type: ignore[assignment]

        assert conn.in_flight_count == 0
        await conn.call_tool_streaming("tool", {})
        assert snapshots == [1]
        assert conn.in_flight_count == 0

    async def test_connection_call_tool_streaming_raises_when_draining(self) -> None:
        """call_tool_streaming raises ConnectionError when connection is draining."""
        config = _make_server_config("test")
        conn = MCPConnection(config)
        conn._state = ConnectionState.DRAINING
        conn._outbound = MagicMock()

        with pytest.raises(ConnectionError, match="draining"):
            await conn.call_tool_streaming("tool", {})

    async def test_connection_call_tool_streaming_raises_when_no_outbound(self) -> None:
        """call_tool_streaming raises ConnectionError when _outbound is None."""
        config = _make_server_config("test")
        conn = MCPConnection(config)
        conn._state = ConnectionState.DISCONNECTED

        with pytest.raises(ConnectionError):
            await conn.call_tool_streaming("tool", {})


class TestRouteStreamingCallCoverageGaps:
    """Additional tests to close router.py coverage gaps in route_streaming_call."""

    async def test_route_streaming_reconnect_fn_raises_returns_error(self) -> None:
        """When reconnect_fn raises, reconnected=False; returns server-unavailable error.

        Covers lines 364-373: reconnect_fn try/except + unavailable-after-reconnect path.
        """
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        # state = DISCONNECTED → triggers reconnect path

        async def failing_reconnect(name: str) -> bool:
            raise RuntimeError("reconnect network error")

        router = FederationRouter(
            reg,
            {"backend": conn},
            reconnect_fn=failing_reconnect,
        )

        result = await router.route_streaming_call("backend__slow_tool", {})

        assert result.success is False
        text = result.result["content"][0]["text"]
        # Either "unavailable after reconnect" or "not connected" — both valid
        assert "backend" in text

    async def test_route_streaming_reconnect_fn_returns_false_returns_error(self) -> None:
        """When reconnect_fn returns False, returns server-unavailable error.

        Covers line 373: not reconnected → return unavailable RouteResult.
        """
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        # state = DISCONNECTED

        async def unsuccessful_reconnect(name: str) -> bool:
            return False

        router = FederationRouter(
            reg,
            {"backend": conn},
            reconnect_fn=unsuccessful_reconnect,
        )

        result = await router.route_streaming_call("backend__slow_tool", {})

        assert result.success is False
        assert "backend" in result.result["content"][0]["text"]

    async def test_route_streaming_call_exception_returns_is_error(self) -> None:
        """When conn.call_tool_streaming raises a non-cancel Exception, returns isError.

        Covers lines 429-431: except Exception block in route_streaming_call.
        """
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED

        class _FailingOutbound:
            async def call_tool_streaming(self, *a: Any, **kw: Any) -> dict[str, Any]:
                raise RuntimeError("backend internal error")

        conn._outbound = _FailingOutbound()  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_streaming_call("backend__slow_tool", {})

        assert result.success is False
        assert "backend internal error" in result.result["content"][0]["text"]
        assert result.result.get("isError") is True

    async def test_route_streaming_timeout_override_passed_through(self) -> None:
        """timeout_override_s is passed as read_timeout_seconds to call_tool_streaming."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED

        received_timeout: list[float | None] = []

        class _TimeoutCapture:
            async def call_tool_streaming(
                self, name: str, args: dict[str, Any],
                *, read_timeout_seconds: float | None = None, **kw: Any
            ) -> dict[str, Any]:
                received_timeout.append(read_timeout_seconds)
                return {"content": []}

        conn._outbound = _TimeoutCapture()  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        await router.route_streaming_call(
            "backend__slow_tool", {}, timeout_override_s=300.0
        )

        assert received_timeout == [300.0]

    async def test_route_streaming_is_error_flag_propagates(self) -> None:
        """isError flag in backend result propagates to RouteResult.success=False."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.CONNECTED

        class _ErrorOutbound:
            async def call_tool_streaming(self, *a: Any, **kw: Any) -> dict[str, Any]:
                return {"content": [{"type": "text", "text": "fail"}], "isError": True}

        conn._outbound = _ErrorOutbound()  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_streaming_call("backend__slow_tool", {})

        assert result.success is False
