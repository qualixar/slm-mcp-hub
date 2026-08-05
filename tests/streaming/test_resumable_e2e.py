"""W8-P4 tests — Resumable streaming, end-to-end.

TDD: RED phase — cross-audit found the primary transient error is
MCPError(code=CONNECTION_CLOSED), NOT ResumptionError. Tests updated to
exercise the real exception path.

CLIENT-LEG (hub→backend, deterministic, no network):
1. MCPError(CONNECTION_CLOSED) + token captured → exactly ONE retry carrying
   the token, final result success, context cleared.
2. ResumptionError + token captured → retry (defensive catch also works).
3. MCPError(CONNECTION_CLOSED) with NO token → NO retry, tool executed once.
   Safety invariant: we NEVER retry without a token (non-idempotency guard).
4. MCPError with non-CONNECTION_CLOSED code (INVALID_PARAMS) → propagates
   unchanged, NO retry. Only transport drops are transient.
5. RuntimeError (non-transient) → propagates, NO retry.
6. asyncio.CancelledError → propagates unconditionally, NOT retried.
7. Retry bounded: both attempts raise CONNECTION_CLOSED, first captures token
   → exactly 2 calls total, exception from second propagates.
8. M-01 (stale token): both attempts raise CONNECTION_CLOSED, first captures
   token → after propagation ctx.get_token() returns None (always cleared).

SERVER-LEG (stateful transport wiring, no network):
9.  _build_sdk_asgi with transport_stateful=True + event_store_enabled=True →
    session_manager.stateless is False AND event_store is InMemoryEventStore.
10. Default stateful=False → stateless=True, event_store=None.
11. hub_config=None → stateless=True, event_store=None (backward compat).
12-15. InMemoryEventStore replay: no-gap, no-dup, sentinel, post-last, wired.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp.client.streamable_http import ResumptionError
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, INVALID_PARAMS

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.constants import TIMEOUT_CLASS_DEFAULT
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.server.http_server import _build_sdk_asgi
from slm_mcp_hub.streaming.event_store import InMemoryEventStore
from slm_mcp_hub.streaming.resumable import ResumableCallContext
from slm_mcp_hub.streaming.resume import run_with_safe_resume

# ---------------------------------------------------------------------------
# Shared test helpers
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
    timeout_class: str = TIMEOUT_CLASS_DEFAULT,
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


async def _noop_progress(progress: float, total: float, message: str) -> None:  # noqa: ARG001
    """No-op progress callback — activates the streaming path without side effects."""


def _make_sdk_server() -> Any:
    """Minimal MagicMock SDK server for _build_sdk_asgi tests."""
    mock = MagicMock()
    mock.lifespan = MagicMock()
    return mock


def _connection_closed_error() -> MCPError:
    """The REAL exception the SDK raises on hub→backend stream drop."""
    return MCPError(code=CONNECTION_CLOSED, message="Connection closed")


# ---------------------------------------------------------------------------
# Fake outbound implementations
# ---------------------------------------------------------------------------


class _ConnectionClosedThenSucceedOutbound:
    """PRIMARY real-world fake: first call captures token then raises
    MCPError(CONNECTION_CLOSED). Second call returns success.

    This is what happens on a real mid-stream drop when the backend had
    already acknowledged progress (emitted a resumption token).
    """

    def __init__(self) -> None:
        self.call_count: int = 0
        self.calls: list[dict[str, Any]] = []

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

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
        self.call_count += 1
        self.calls.append(
            {
                "name": tool_name,
                "resumption_token": resumption_token,
                "on_resumption_token_provided": on_resumption_token is not None,
            }
        )
        if self.call_count == 1:
            if on_resumption_token is not None:
                await on_resumption_token("tok-real-1")
            raise _connection_closed_error()
        return {"content": [{"type": "text", "text": "resumed ok"}]}


class _ResumptionErrorThenSucceedOutbound:
    """Defensive-catch fake: ResumptionError (SDK client-side resumption failure).

    Although CONNECTION_CLOSED is the primary production path, ResumptionError
    is still caught defensively. This fake proves the defensive catch works.
    """

    def __init__(self) -> None:
        self.call_count: int = 0
        self.calls: list[dict[str, Any]] = []

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

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
        self.call_count += 1
        self.calls.append({"resumption_token": resumption_token})
        if self.call_count == 1:
            if on_resumption_token is not None:
                await on_resumption_token("tok-resumption-1")
            raise ResumptionError("client-side resumption failure")
        return {"content": [{"type": "text", "text": "defensive resumed"}]}


class _ConnectionClosedNoTokenOutbound:
    """Safety-invariant fake: raises MCPError(CONNECTION_CLOSED) without
    ever calling on_resumption_token. No token → no retry allowed.
    """

    def __init__(self) -> None:
        self.call_count: int = 0

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

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
        self.call_count += 1
        raise _connection_closed_error()


class _NonConnectionClosedMCPErrorOutbound:
    """Non-transport MCPError: INVALID_PARAMS. Must NOT be retried.

    Only CONNECTION_CLOSED is a transient transport drop. Any other MCPError
    code means the server rejected the request (protocol/tool error).
    """

    def __init__(self) -> None:
        self.call_count: int = 0

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

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
        self.call_count += 1
        raise MCPError(code=INVALID_PARAMS, message="bad argument: missing required field")


class _RuntimeErrorOutbound:
    """Non-transient RuntimeError: propagates unchanged."""

    def __init__(self) -> None:
        self.call_count: int = 0

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

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
        self.call_count += 1
        raise RuntimeError("tool crashed with non-transient error")


class _CancelledOutbound:
    """CancelledError: BaseException, never caught by except-MCPError."""

    def __init__(self) -> None:
        self.call_count: int = 0

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

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
        self.call_count += 1
        raise asyncio.CancelledError("cancelled during streaming")


class _ConnectionClosedBothAttemptsOutbound:
    """M-01 / bounded-retry fake: BOTH attempts raise MCPError(CONNECTION_CLOSED).
    First call captures a token; second call (retry) also fails.

    Proves:
    - Retry is bounded to 1 (exactly 2 total calls).
    - ctx is cleared even when both attempts fail (M-01 fix).
    """

    def __init__(self) -> None:
        self.call_count: int = 0

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

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
        self.call_count += 1
        if self.call_count == 1 and on_resumption_token is not None:
            await on_resumption_token("tok-both-fail")
        raise _connection_closed_error()


# ---------------------------------------------------------------------------
# Helper-level tests: run_with_safe_resume directly
# ---------------------------------------------------------------------------


class TestRunWithSafeResume:
    """Unit tests for run_with_safe_resume — no router, no network."""

    async def test_connection_closed_token_captured_retry_succeeds(self) -> None:
        """MCPError(CONNECTION_CLOSED) + token captured → one retry → success → ctx cleared.

        This is the PRIMARY real-world path: backend drops the connection after
        acknowledging progress (token emitted), hub resumes once with that token.
        """
        outbound = _ConnectionClosedThenSucceedOutbound()
        ctx = ResumableCallContext(call_id="test-real-1")

        result = await run_with_safe_resume(
            outbound,
            "tool",
            {},
            ctx=ctx,
            effective_timeout=None,
            progress_callback=None,
        )

        assert outbound.call_count == 2, (
            f"Expected 2 calls (original + 1 resume), got {outbound.call_count}"
        )
        assert outbound.calls[0]["resumption_token"] is None
        assert outbound.calls[0]["on_resumption_token_provided"] is True
        assert outbound.calls[1]["resumption_token"] == "tok-real-1", (
            f"Resume call must carry 'tok-real-1', got {outbound.calls[1]['resumption_token']!r}"
        )
        assert result == {"content": [{"type": "text", "text": "resumed ok"}]}
        # M-01: ctx cleared on success
        assert await ctx.get_token() is None, (
            "ctx must be cleared after successful resume"
        )

    async def test_resumption_error_token_captured_retry_succeeds(self) -> None:
        """ResumptionError + token captured → one retry (defensive catch works).

        ResumptionError is not the primary production path but is still caught
        defensively. The retry mechanic must work identically.
        """
        outbound = _ResumptionErrorThenSucceedOutbound()
        ctx = ResumableCallContext(call_id="test-defensive-1")

        result = await run_with_safe_resume(
            outbound,
            "tool",
            {},
            ctx=ctx,
            effective_timeout=None,
            progress_callback=None,
        )

        assert outbound.call_count == 2
        assert outbound.calls[1]["resumption_token"] == "tok-resumption-1"
        assert result == {"content": [{"type": "text", "text": "defensive resumed"}]}
        assert await ctx.get_token() is None

    async def test_connection_closed_no_token_re_raises(self) -> None:
        """MCPError(CONNECTION_CLOSED) with no token → re-raises, tool called once.

        Safety invariant: retrying without a token could double-execute a
        non-idempotent tool. Re-raise so router converts to soft RouteResult.
        """
        outbound = _ConnectionClosedNoTokenOutbound()
        ctx = ResumableCallContext(call_id="test-no-token-1")

        with pytest.raises(MCPError) as exc_info:
            await run_with_safe_resume(
                outbound,
                "tool",
                {},
                ctx=ctx,
                effective_timeout=None,
                progress_callback=None,
            )

        assert exc_info.value.code == CONNECTION_CLOSED
        assert outbound.call_count == 1, (
            f"Safety: tool must be called ONCE when no token; got {outbound.call_count}"
        )
        # M-01: ctx cleared even on failure
        assert await ctx.get_token() is None

    async def test_non_connection_closed_mcp_error_propagates(self) -> None:
        """MCPError with code≠CONNECTION_CLOSED (e.g. INVALID_PARAMS) → propagates.

        Only CONNECTION_CLOSED is a transient transport drop. Any other MCPError
        code indicates a protocol/tool rejection — must NOT be retried.
        """
        outbound = _NonConnectionClosedMCPErrorOutbound()
        ctx = ResumableCallContext(call_id="test-non-cc-1")

        with pytest.raises(MCPError) as exc_info:
            await run_with_safe_resume(
                outbound,
                "tool",
                {},
                ctx=ctx,
                effective_timeout=None,
                progress_callback=None,
            )

        assert exc_info.value.code == INVALID_PARAMS, (
            "Non-CONNECTION_CLOSED MCPError must propagate with its original code"
        )
        assert outbound.call_count == 1, (
            f"INVALID_PARAMS must NOT trigger a retry; got {outbound.call_count}"
        )
        # M-01: ctx cleared even on non-transient MCPError
        assert await ctx.get_token() is None

    async def test_runtime_error_propagates(self) -> None:
        """RuntimeError (not MCPError, not ResumptionError) propagates unchanged."""
        outbound = _RuntimeErrorOutbound()
        ctx = ResumableCallContext(call_id="test-runtime-1")

        with pytest.raises(RuntimeError, match="non-transient error"):
            await run_with_safe_resume(
                outbound,
                "tool",
                {},
                ctx=ctx,
                effective_timeout=None,
                progress_callback=None,
            )

        assert outbound.call_count == 1
        # M-01: ctx cleared even on RuntimeError
        assert await ctx.get_token() is None

    async def test_cancelled_error_propagates_not_retried(self) -> None:
        """asyncio.CancelledError propagates unconditionally.

        CancelledError is BaseException. The except-(MCPError, ResumptionError)
        clause does NOT catch it. It propagates through the finally (ctx still cleared).
        """
        outbound = _CancelledOutbound()
        ctx = ResumableCallContext(call_id="test-cancel-1")

        with pytest.raises(asyncio.CancelledError):
            await run_with_safe_resume(
                outbound,
                "tool",
                {},
                ctx=ctx,
                effective_timeout=None,
                progress_callback=None,
            )

        assert outbound.call_count == 1, "CancelledError must NOT trigger a retry"
        # M-01: ctx cleared even on CancelledError (finally runs before propagation)
        assert await ctx.get_token() is None

    async def test_retry_bounded_to_one_both_fail(self) -> None:
        """Both attempts raise CONNECTION_CLOSED (first captures token).

        Proves the retry is bounded to exactly 1: total calls == 2, never 3+.
        Exception from the retry propagates.
        """
        outbound = _ConnectionClosedBothAttemptsOutbound()
        ctx = ResumableCallContext(call_id="test-bounded-1")

        with pytest.raises(MCPError) as exc_info:
            await run_with_safe_resume(
                outbound,
                "tool",
                {},
                ctx=ctx,
                effective_timeout=None,
                progress_callback=None,
            )

        assert exc_info.value.code == CONNECTION_CLOSED
        assert outbound.call_count == 2, (
            f"Retry bounded to 1 attempt; got {outbound.call_count} total calls"
        )

    async def test_m01_ctx_cleared_when_both_attempts_fail(self) -> None:
        """M-01: ctx.get_token() returns None after both attempts fail.

        When a caller-provided (injected) ctx is used and both attempts fail,
        the stale token must NOT remain in the context. ctx.clear() must run
        on ALL terminal paths including double-failure.
        """
        outbound = _ConnectionClosedBothAttemptsOutbound()
        ctx = ResumableCallContext(call_id="test-m01-1")

        with pytest.raises(MCPError):
            await run_with_safe_resume(
                outbound,
                "tool",
                {},
                ctx=ctx,
                effective_timeout=None,
                progress_callback=None,
            )

        # Confirm token is gone despite failure — no stale token left
        assert await ctx.get_token() is None, (
            "M-01: ctx must be cleared on terminal failure to prevent stale token reuse"
        )


# ---------------------------------------------------------------------------
# Router-level tests: verify RouteResult outcomes
# ---------------------------------------------------------------------------


class TestRouterResumablePipeline:
    """Integration: router auto-creates ResumableCallContext and calls run_with_safe_resume."""

    async def test_router_connection_closed_resume_success(self) -> None:
        """CONNECTION_CLOSED + token → router returns RouteResult(success=True)."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        outbound = _ConnectionClosedThenSucceedOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_tool_call(
            "backend__tool",
            {},
            progress_callback=_noop_progress,
        )

        assert isinstance(result, RouteResult)
        assert result.success is True, (
            f"Expected success after resume; success={result.success}"
        )
        assert outbound.call_count == 2

    async def test_router_no_token_soft_error(self) -> None:
        """CONNECTION_CLOSED no-token → soft error RouteResult, tool called once."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        outbound = _ConnectionClosedNoTokenOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_tool_call(
            "backend__tool",
            {},
            progress_callback=_noop_progress,
        )

        assert isinstance(result, RouteResult)
        assert result.success is False
        assert result.result.get("isError") is True
        assert outbound.call_count == 1, (
            f"Non-idempotency safety: tool called once, got {outbound.call_count}"
        )

    async def test_router_non_connection_closed_mcp_error_soft_result(self) -> None:
        """MCPError(INVALID_PARAMS) → soft error RouteResult, NOT retried."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        outbound = _NonConnectionClosedMCPErrorOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_tool_call(
            "backend__tool",
            {},
            progress_callback=_noop_progress,
        )

        assert result.success is False
        assert result.result.get("isError") is True
        assert outbound.call_count == 1

    async def test_router_runtime_error_soft_result(self) -> None:
        """RuntimeError → soft error RouteResult (existing behavior unchanged)."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        outbound = _RuntimeErrorOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_tool_call(
            "backend__tool",
            {},
            progress_callback=_noop_progress,
        )

        assert result.success is False
        assert result.result.get("isError") is True
        assert outbound.call_count == 1

    async def test_router_cancelled_error_propagates(self) -> None:
        """asyncio.CancelledError propagates out of route_tool_call."""
        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        outbound = _CancelledOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})

        with pytest.raises(asyncio.CancelledError):
            await router.route_tool_call(
                "backend__tool",
                {},
                progress_callback=_noop_progress,
            )

        assert outbound.call_count == 1

    async def test_router_non_streaming_path_unchanged(self) -> None:
        """Default-class + no progress_callback → call_tool, NOT call_tool_streaming."""

        class _RecordCallTool:
            def __init__(self) -> None:
                self.call_tool_count = 0
                self.streaming_count = 0

            async def call_tool(
                self, name: str, args: dict[str, Any], *, timeout_s: Any = None
            ) -> dict[str, Any]:
                self.call_tool_count += 1
                return {"content": [{"type": "text", "text": "ok"}]}

            async def call_tool_streaming(
                self, name: str, args: dict[str, Any], **kwargs: Any
            ) -> dict[str, Any]:
                self.streaming_count += 1
                return {"content": [{"type": "text", "text": "streaming ok"}]}

        reg = _make_registry()
        config = _make_server_config()
        conn = _make_conn(config)
        outbound = _RecordCallTool()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn})
        result = await router.route_tool_call("backend__tool", {})

        assert result.success is True
        assert outbound.call_tool_count == 1
        assert outbound.streaming_count == 0


# ---------------------------------------------------------------------------
# Server-leg: stateful transport wiring
# ---------------------------------------------------------------------------


class TestStatefulTransportWiring:
    """_build_sdk_asgi wires stateless=False + InMemoryEventStore for stateful mode."""

    def test_stateful_with_event_store_wires_correctly(self) -> None:
        """transport_stateful=True, event_store_enabled=True →
        stateless=False AND event_store is InMemoryEventStore.
        """
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(transport_stateful=True, event_store_enabled=True)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.stateless is False
        assert isinstance(session_manager.event_store, InMemoryEventStore)

    def test_stateless_default_with_event_store_disabled(self) -> None:
        """Default (transport_stateful=False) → stateless=True, event_store=None."""
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(transport_stateful=False, event_store_enabled=False)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.stateless is True
        assert session_manager.event_store is None

    def test_stateful_no_hub_config_falls_through_to_stateless(self) -> None:
        """hub_config=None → stateless=True (backward compat)."""
        sdk_server = _make_sdk_server()

        _, session_manager = _build_sdk_asgi(sdk_server)

        assert session_manager.stateless is True
        assert session_manager.event_store is None


# ---------------------------------------------------------------------------
# Server-leg: InMemoryEventStore replay contract
# ---------------------------------------------------------------------------


class TestEventStoreReplayContract:
    """InMemoryEventStore no-gap, no-dup replay — the contract the SDK relies on."""

    async def test_replay_after_mid_stream_drop(self) -> None:
        """Store 10 events; client got 0–3 (last_event_id='3').
        Replay returns exactly [4,5,6,7,8,9] — no gap, no dup.
        """
        store = InMemoryEventStore(max_events_per_stream=500)
        for _ in range(10):
            await store.store_event("stream-A", MagicMock())

        received_ids: list[int] = []

        async def _cb(em: Any) -> None:
            received_ids.append(int(em.event_id))

        stream_id = await store.replay_events_after("3", _cb)

        assert stream_id == "stream-A"
        assert received_ids == list(range(4, 10))

    async def test_replay_from_sentinel_returns_all(self) -> None:
        """Sentinel last_event_id='-1' → all stored events replayed."""
        store = InMemoryEventStore(max_events_per_stream=500)
        for _ in range(5):
            await store.store_event("stream-B", MagicMock())

        received: list[Any] = []

        async def _cb(em: Any) -> None:
            received.append(em)

        stream_id = await store.replay_events_after("-1", _cb)

        assert stream_id == "stream-B"
        assert len(received) == 5

    async def test_replay_unknown_id_returns_none(self) -> None:
        """Non-integer last_event_id → None (ambiguous, cannot replay)."""
        store = InMemoryEventStore(max_events_per_stream=500)
        stream_id = await store.replay_events_after("NOT_AN_INT", MagicMock())
        assert stream_id is None

    async def test_replay_after_all_events_no_events_sent(self) -> None:
        """Replay after the LAST event → sends 0 events (client is current)."""
        store = InMemoryEventStore(max_events_per_stream=500)
        for _ in range(3):
            await store.store_event("stream-C", MagicMock())

        received: list[Any] = []

        async def _cb(em: Any) -> None:
            received.append(em)

        stream_id = await store.replay_events_after("2", _cb)
        assert stream_id == "stream-C"
        assert len(received) == 0

    async def test_stateful_session_manager_uses_this_store(self) -> None:
        """Stateful session manager's event_store instance passes replay contract."""
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(
            transport_stateful=True,
            event_store_enabled=True,
            event_store_max_events_per_stream=200,
        )

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        store = session_manager.event_store
        assert isinstance(store, InMemoryEventStore)

        msg = MagicMock()
        await store.store_event("probe-stream", msg)
        await store.store_event("probe-stream", msg)

        received: list[Any] = []

        async def _cb(em: Any) -> None:
            received.append(em)

        stream_id = await store.replay_events_after("0", _cb)
        assert stream_id == "probe-stream"
        assert len(received) == 1
