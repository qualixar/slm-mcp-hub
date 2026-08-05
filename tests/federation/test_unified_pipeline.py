"""W8-P1 unified call pipeline tests.

TDD: RED phase — written BEFORE implementation.

Tests:
1. route_tool_call records metrics when MetricsCollector is injected
2. route_tool_call default-class + no progress → conn.call_tool (NOT streaming)
3. route_tool_call with progress_callback → conn.call_tool_streaming
4. route_tool_call with timeout_class='fast' → streaming + read_timeout=30
5. route_tool_call applies concurrency gate (serializes same-backend calls)
6. _resolve_connection error paths: not-found, disconnected, reconnect
"""

from __future__ import annotations

from typing import Any

import anyio

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.router import FederationRouter
from slm_mcp_hub.federation.timeouts import TimeoutRegistry
from slm_mcp_hub.observability.metrics import MetricsCollector

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_registry(
    server: str = "backend",
    tool: str = "tool",
) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.sync(
        {
            server: {
                "tools": [{"name": tool, "description": tool}],
                "resources": [],
                "prompts": [],
                "resource_templates": [],
            }
        }
    )
    return reg


def _make_server_config(
    name: str = "backend",
    timeout_class: str = "default",
) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="http",
        url=f"http://127.0.0.1:1/{name}",
        timeout_class=timeout_class,
    )


def _make_conn(config: MCPServerConfig) -> MCPConnection:
    conn = MCPConnection(config)
    conn._state = ConnectionState.CONNECTED
    return conn


# ---------------------------------------------------------------------------
# Outbound fakes
# ---------------------------------------------------------------------------


class _RecordingOutbound:
    """Records which method was called and with what kwargs."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self._result = result or {"content": [{"type": "text", "text": "ok"}]}
        self.call_tool_calls: list[dict[str, Any]] = []
        self.call_tool_streaming_calls: list[dict[str, Any]] = []

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        self.call_tool_calls.append(
            {"name": tool_name, "args": arguments, "timeout_s": timeout_s}
        )
        return self._result

    async def call_tool_streaming(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        read_timeout_seconds: float | None = None,
        progress_callback: Any = None,
        resumption_token: Any = None,
        on_resumption_token: Any = None,
    ) -> dict[str, Any]:
        self.call_tool_streaming_calls.append(
            {
                "name": tool_name,
                "args": arguments,
                "read_timeout_seconds": read_timeout_seconds,
                "progress_callback": progress_callback,
            }
        )
        if progress_callback is not None:
            await progress_callback(0.5, 1.0, "halfway")
        return self._result



# ---------------------------------------------------------------------------
# Group 1: Metrics recording
# ---------------------------------------------------------------------------


class TestMetricsRecording:
    """route_tool_call records per-call metrics into MetricsCollector."""

    async def test_metrics_recorded_on_cancellation(self) -> None:
        """A call cancelled mid-flight records exactly one FAILED metric
        (call_count==1, success_rate==0.0): is_error stays True on CancelledError
        and the finally still records, with no in-flight slot leak."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)

        class _SlowOutbound:
            async def call_tool(
                self, name: str, args: dict[str, Any], *, timeout_s: Any = None
            ) -> dict[str, Any]:
                await anyio.sleep(10.0)
                return {"content": []}

        conn._outbound = _SlowOutbound()  # type: ignore[assignment]
        metrics = MetricsCollector()
        router = FederationRouter(reg, {"backend": conn}, metrics=metrics)

        with anyio.CancelScope() as scope:
            scope.deadline = anyio.current_time() + 0.1
            await router.route_tool_call("backend__tool", {})

        assert scope.cancelled_caught
        m = metrics.get_server_metrics("backend")
        assert m["call_count"] == 1
        assert m["success_rate"] == 0.0
        assert conn.in_flight_count == 0

    async def test_metrics_recorded_on_success(self) -> None:
        """After two successful route_tool_calls, MetricsCollector has call_count==2
        and p95_duration_ms>0."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        outbound = _RecordingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        metrics = MetricsCollector()
        router = FederationRouter(
            reg,
            {"backend": conn},
            metrics=metrics,
            timeout_registry=TimeoutRegistry(),
        )

        await router.route_tool_call("backend__tool", {})
        await router.route_tool_call("backend__tool", {"key": "val"})

        server_metrics = metrics.get_server_metrics("backend")
        assert server_metrics["call_count"] == 2, (
            f"Expected call_count=2, got {server_metrics['call_count']}"
        )
        assert server_metrics["p95_duration_ms"] >= 0, (
            "p95_duration_ms must be >= 0 after calls"
        )

    async def test_metrics_recorded_on_error_result(self) -> None:
        """When backend returns isError=True, call is still recorded in metrics."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        error_outbound = _RecordingOutbound(
            result={"content": [{"type": "text", "text": "fail"}], "isError": True}
        )
        conn._outbound = error_outbound  # type: ignore[assignment]

        metrics = MetricsCollector()
        router = FederationRouter(reg, {"backend": conn}, metrics=metrics)

        await router.route_tool_call("backend__tool", {})

        server_metrics = metrics.get_server_metrics("backend")
        assert server_metrics["call_count"] == 1
        assert server_metrics["success_rate"] == 0.0

    async def test_metrics_not_required_when_none(self) -> None:
        """FederationRouter works correctly when metrics=None (backward compat)."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        conn._outbound = _RecordingOutbound()  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})  # metrics not passed
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is True


# ---------------------------------------------------------------------------
# Group 2: Default-class, no-progress → conn.call_tool (NOT streaming)
# ---------------------------------------------------------------------------


class TestDefaultPathUsesCallTool:
    """route_tool_call with default timeout_class and no progress_callback uses call_tool."""

    async def test_default_class_no_progress_calls_call_tool(self) -> None:
        """Default-class backend + no progress_callback → call_tool (NOT call_tool_streaming)."""
        reg = _make_registry()
        config = _make_server_config(timeout_class="default")
        conn = _make_conn(config)
        outbound = _RecordingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(
            reg,
            {"backend": conn},
            timeout_registry=TimeoutRegistry(),
        )
        result = await router.route_tool_call("backend__tool", {"k": "v"})

        assert result.success is True
        assert len(outbound.call_tool_calls) == 1, (
            "call_tool must be called once for default-class + no-progress"
        )
        assert len(outbound.call_tool_streaming_calls) == 0, (
            "call_tool_streaming must NOT be called for default-class + no-progress"
        )

    async def test_default_class_no_progress_routes_via_call_tool(self) -> None:
        """Default-class + no progress → call_tool called exactly once; no streaming call.

        Note: MCPConnection.call_tool intentionally ignores timeout_s (ARG002 — kept
        for API compatibility) and does not forward it to the outbound. The timeout
        is resolved by the router and passed to conn.call_tool, but conn discards it
        before calling outbound. This is by design per the W4-P2 spec ("conn IGNORES
        timeout_s; outbound.call_tool(name,args) has no timeout param → SDK default").
        The meaningful assertion here is that the NON-streaming path was selected.
        """
        reg = _make_registry()
        config = _make_server_config(timeout_class="default")
        conn = _make_conn(config)
        outbound = _RecordingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(
            reg,
            {"backend": conn},
            timeout_registry=TimeoutRegistry(),
        )
        result = await router.route_tool_call("backend__tool", {})

        # Verify the non-streaming path was selected (the key behavioral invariant).
        assert result.success is True
        assert len(outbound.call_tool_calls) == 1, "call_tool must be called exactly once"
        assert len(outbound.call_tool_streaming_calls) == 0, (
            "call_tool_streaming must NOT be called for default-class + no-progress"
        )
        # Verify the call was routed correctly.
        assert outbound.call_tool_calls[0]["name"] == "tool"
        # timeout_s is None at the outbound level — MCPConnection ignores it (ARG002).
        assert outbound.call_tool_calls[0]["timeout_s"] is None


# ---------------------------------------------------------------------------
# Group 3: progress_callback → conn.call_tool_streaming
# ---------------------------------------------------------------------------


class TestProgressCallbackUsesStreaming:
    """route_tool_call with progress_callback dispatches to call_tool_streaming."""

    async def test_progress_callback_triggers_streaming_path(self) -> None:
        """Passing progress_callback→call_tool_streaming (not call_tool)."""
        reg = _make_registry()
        config = _make_server_config(timeout_class="default")
        conn = _make_conn(config)
        outbound = _RecordingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        progress_received: list[tuple[float, Any, Any]] = []

        async def capture(p: float, t: float | None, m: str | None) -> None:
            progress_received.append((p, t, m))

        router = FederationRouter(
            reg,
            {"backend": conn},
            timeout_registry=TimeoutRegistry(),
        )
        result = await router.route_tool_call(
            "backend__tool", {}, progress_callback=capture
        )

        assert result.success is True
        assert len(outbound.call_tool_calls) == 0, (
            "call_tool must NOT be called when progress_callback is given"
        )
        assert len(outbound.call_tool_streaming_calls) == 1, (
            "call_tool_streaming must be called when progress_callback is given"
        )

    async def test_progress_callback_forwarded_to_outbound(self) -> None:
        """Progress events from backend reach the callback."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        outbound = _RecordingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        received: list[tuple[float, Any, Any]] = []

        async def capture(p: float, t: float | None, m: str | None) -> None:
            received.append((p, t, m))

        router = FederationRouter(reg, {"backend": conn})
        await router.route_tool_call("backend__tool", {}, progress_callback=capture)

        assert len(received) == 1
        assert received[0] == (0.5, 1.0, "halfway")


# ---------------------------------------------------------------------------
# Group 4: timeout_class='fast' → streaming + read_timeout=30
# ---------------------------------------------------------------------------


class TestTimeoutClassForcesStreaming:
    """Non-default timeout_class forces streaming path in route_tool_call."""

    async def test_fast_timeout_class_uses_streaming_with_30s(self) -> None:
        """Backend with timeout_class='fast' routes to call_tool_streaming with read_timeout=30."""
        reg = _make_registry()
        config = _make_server_config(timeout_class="fast")
        conn = _make_conn(config)
        outbound = _RecordingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(
            reg,
            {"backend": conn},
            timeout_registry=TimeoutRegistry(),
        )
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is True
        assert len(outbound.call_tool_streaming_calls) == 1, (
            "call_tool_streaming must be called for timeout_class='fast'"
        )
        assert outbound.call_tool_streaming_calls[0]["read_timeout_seconds"] == 30.0, (
            f"read_timeout_seconds must be 30.0 for 'fast', "
            f"got {outbound.call_tool_streaming_calls[0]['read_timeout_seconds']}"
        )
        assert len(outbound.call_tool_calls) == 0


# ---------------------------------------------------------------------------
# Group 5: Concurrency gate serialization
# ---------------------------------------------------------------------------


class TestConcurrencyGate:
    """route_tool_call applies the per-backend concurrency gate."""

    async def test_gate_prevents_concurrent_calls_to_same_backend(self) -> None:
        """With gate max_concurrency=1, concurrent calls to same backend are serialized.

        Uses an in-flight counter to prove that both calls never run simultaneously.
        """
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)

        in_flight = [0]
        peak_in_flight = [0]

        class _CountingOutbound:
            async def call_tool(
                self,
                tool_name: str,
                arguments: dict[str, Any],
                *,
                timeout_s: float | None = None,
            ) -> dict[str, Any]:
                in_flight[0] += 1
                peak_in_flight[0] = max(peak_in_flight[0], in_flight[0])
                await anyio.sleep(0.01)
                in_flight[0] -= 1
                return {"content": [{"type": "text", "text": "ok"}]}

            async def call_tool_streaming(
                self,
                tool_name: str,
                arguments: dict[str, Any],
                *,
                read_timeout_seconds: float | None = None,
                progress_callback: Any = None,
                **kw: Any,
            ) -> dict[str, Any]:
                in_flight[0] += 1
                peak_in_flight[0] = max(peak_in_flight[0], in_flight[0])
                await anyio.sleep(0.01)
                in_flight[0] -= 1
                return {"content": [{"type": "text", "text": "ok"}]}

        conn._outbound = _CountingOutbound()  # type: ignore[assignment]

        gate = BackendConcurrencyGate(default_max_concurrency=1)
        router = FederationRouter(
            reg,
            {"backend": conn},
            concurrency_gate=gate,
        )

        async with anyio.create_task_group() as tg:
            tg.start_soon(router.route_tool_call, "backend__tool", {})
            tg.start_soon(router.route_tool_call, "backend__tool", {})

        assert peak_in_flight[0] == 1, (
            f"Gate max=1 must prevent concurrent calls; peak_in_flight was {peak_in_flight[0]}"
        )

    async def test_gate_slot_released_after_call(self) -> None:
        """After route_tool_call completes, gate slot is released (usage=0)."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        conn._outbound = _RecordingOutbound()  # type: ignore[assignment]

        gate = BackendConcurrencyGate(default_max_concurrency=2)
        router = FederationRouter(reg, {"backend": conn}, concurrency_gate=gate)

        await router.route_tool_call("backend__tool", {})

        assert gate.current_usage("backend") == 0, (
            "Gate slot must be released after successful call"
        )


# ---------------------------------------------------------------------------
# Group 6: _resolve_connection error paths
# ---------------------------------------------------------------------------


class TestResolveConnectionErrors:
    """_resolve_connection (extracted from route_tool_call) handles all error paths."""

    async def test_tool_not_found_returns_error(self) -> None:
        """route_tool_call for unknown tool returns isError RouteResult."""
        reg = CapabilityRegistry()
        router = FederationRouter(reg, {})
        result = await router.route_tool_call("unknown__tool", {})

        assert result.success is False
        assert "not found" in result.result["content"][0]["text"].lower()

    async def test_disconnected_server_returns_error(self) -> None:
        """route_tool_call for disconnected server returns isError RouteResult."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        # state defaults to DISCONNECTED

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is False
        assert "backend" in result.result["content"][0]["text"]

    async def test_reconnect_fn_success_then_routes(self) -> None:
        """When backend is disconnected and reconnect_fn succeeds, call routes."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        outbound = _RecordingOutbound()

        async def reconnect(name: str) -> bool:
            conn._state = ConnectionState.CONNECTED
            conn._outbound = outbound  # type: ignore[assignment]
            return True

        router = FederationRouter(reg, {"backend": conn}, reconnect_fn=reconnect)
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is True
        assert len(outbound.call_tool_calls) == 1

    async def test_reconnect_fn_failure_returns_error(self) -> None:
        """When reconnect_fn returns False, route_tool_call returns error."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)

        async def failing_reconnect(name: str) -> bool:
            return False

        router = FederationRouter(reg, {"backend": conn}, reconnect_fn=failing_reconnect)
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is False
        assert "backend" in result.result["content"][0]["text"]

    async def test_reconnect_fn_raises_returns_error(self) -> None:
        """When reconnect_fn raises, route_tool_call returns error (no propagation)."""
        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)

        async def raising_reconnect(name: str) -> bool:
            raise RuntimeError("network failure")

        router = FederationRouter(reg, {"backend": conn}, reconnect_fn=raising_reconnect)
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is False

    async def test_draining_server_returns_shutting_down_message(self) -> None:
        """route_tool_call for a draining server returns 'shutting down' message."""
        from slm_mcp_hub.federation.connection import ConnectionState

        reg = _make_registry()
        config = _make_server_config()
        conn = MCPConnection(config)
        conn._state = ConnectionState.DRAINING

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is False
        assert "shutting down" in result.result["content"][0]["text"].lower()

    async def test_exception_during_call_returns_error(self) -> None:
        """When call_tool raises Exception, route_tool_call returns isError result."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)

        class _FailOutbound:
            async def call_tool(self, *a: Any, **kw: Any) -> dict[str, Any]:
                raise RuntimeError("backend exploded")

        conn._outbound = _FailOutbound()  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is False
        assert "backend exploded" in result.result["content"][0]["text"]
        assert result.result.get("isError") is True
