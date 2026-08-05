"""W1-P2 — ConnectionSupervisor resilience tests (circuit-breaker, auth, terminal, HOL).

Continues from test_supervisor.py — that file covers classes 1–4 (hooks, backoff,
lifecycle, connect behaviour).  This file covers classes 5–10.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.manager import ConnectionManager
from slm_mcp_hub.resilience.supervisor import ConnectionSupervisor

# ---------------------------------------------------------------------------
# Helpers (mirrored from test_supervisor.py)
# ---------------------------------------------------------------------------


def _cfg(name: str = "test-srv", **kw: Any) -> MCPServerConfig:
    defaults: dict[str, Any] = dict(transport="stdio", command="echo", args=("hi",))
    defaults.update(kw)
    return MCPServerConfig(name=name, **defaults)


def _bare_conn(name: str = "test-srv") -> MCPConnection:
    return MCPConnection(_cfg(name=name))


def _noop_sleep() -> Any:
    async def _sleep(_s: float) -> None:
        await asyncio.sleep(0)

    return _sleep


def _make_supervisor(
    conn: MCPConnection,
    *,
    failure_threshold: int = 5,
    escalation_after: int = 3,
    backoff_base: float = 1.0,
    backoff_factor: float = 2.0,
    backoff_max: float = 60.0,
    rng: random.Random | None = None,
    sleep_fn: Any = None,
) -> ConnectionSupervisor:
    return ConnectionSupervisor(
        conn,
        failure_threshold=failure_threshold,
        escalation_after=escalation_after,
        backoff_base=backoff_base,
        backoff_factor=backoff_factor,
        backoff_max=backoff_max,
        rng=rng or random.Random(0),
        sleep_fn=sleep_fn or _noop_sleep(),
    )


class _PatchedConn:
    """Wraps a real MCPConnection, patches connect() for test control."""

    def __init__(
        self,
        name: str = "srv",
        *,
        fail_n: int = 0,
        always_fail: bool = False,
        terminal_on: int | None = None,
        auth_on: int | None = None,
        hang_connect: asyncio.Event | None = None,
    ) -> None:
        self._inner = MCPConnection(_cfg(name=name))
        self.attempt = 0
        self._fail_n = fail_n
        self._always_fail = always_fail
        self._terminal_on = terminal_on
        self._auth_on = auth_on
        self._hang_connect = hang_connect
        self._connected_event: asyncio.Event = asyncio.Event()
        self._inner.connect = self._connect  # type: ignore[method-assign]

    async def _connect(self) -> None:
        self.attempt += 1

        if self._hang_connect is not None:
            await self._hang_connect.wait()
            raise ConnectionError("hang connect resolved to fail")

        if self._auth_on is not None and self.attempt == self._auth_on:
            self._inner._transition(ConnectionState.CONNECTING, "attempt")
            self._inner._transition(
                ConnectionState.AUTH_REQUIRED,
                "auth required",
                failure_class="AUTH",
            )
            return

        if self._terminal_on is not None and self.attempt == self._terminal_on:
            self._inner._transition(ConnectionState.CONNECTING, "attempt")
            self._inner._transition(
                ConnectionState.ERROR, "terminal error", failure_class="TERMINAL"
            )
            # W1-P3: raise a realistic TERMINAL exception — a ConnectionError
            # whose __cause__ is FileNotFoundError, mirroring what
            # MCPConnection.connect() does when it re-wraps a non-ConnectionError.
            # This ensures classify_failure() classifies the exception as TERMINAL
            # via cause-chain inspection (not via _last_failure_class lookup).
            _root = FileNotFoundError("terminal: binary not found or bad config")
            _wrap = ConnectionError(
                f"MCP {self._inner.name} initialization failed (FileNotFoundError)"
            )
            _wrap.__cause__ = _root
            raise _wrap

        if self._always_fail or self.attempt <= self._fail_n:
            self._inner._transition(ConnectionState.CONNECTING, "attempt")
            self._inner._transition(
                ConnectionState.ERROR, "transient fail", failure_class="TRANSIENT"
            )
            raise ConnectionError(f"transient fail #{self.attempt}")

        self._inner._transition(ConnectionState.CONNECTING, "attempt")
        self._inner._transition(ConnectionState.CONNECTED, "success")
        self._inner._connected_at = time.time()
        self._connected_event.set()

    @property
    def conn(self) -> MCPConnection:
        return self._inner


# ---------------------------------------------------------------------------
# 5. Circuit breaker — open / half-open / close
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """After failure_threshold failures → CIRCUIT_OPEN; probe closes it on success."""

    async def test_breaker_opens_after_threshold(self) -> None:
        THRESHOLD = 3
        pc = _PatchedConn(always_fail=True)
        slept: list[float] = []

        async def recording_sleep(s: float) -> None:
            slept.append(s)
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, failure_threshold=THRESHOLD, sleep_fn=recording_sleep)
        await sup.start()
        for _ in range(20):
            await asyncio.sleep(0)

        assert sup.breaker_open is True
        assert sup.consecutive_failures >= THRESHOLD
        assert pc.conn.state in (
            ConnectionState.CIRCUIT_OPEN,
            ConnectionState.RECONNECTING,
            ConnectionState.ERROR,
        )
        await sup.stop()

    async def test_breaker_sleep_is_max_after_open(self) -> None:
        THRESHOLD = 2
        MAX_BACKOFF = 5.0
        pc = _PatchedConn(always_fail=True)
        slept: list[float] = []

        async def recording_sleep(s: float) -> None:
            slept.append(s)
            await asyncio.sleep(0)

        sup = _make_supervisor(
            pc.conn,
            failure_threshold=THRESHOLD,
            backoff_max=MAX_BACKOFF,
            sleep_fn=recording_sleep,
        )
        await sup.start()
        for _ in range(30):
            await asyncio.sleep(0)

        assert sup.breaker_open is True
        breaker_sleeps = [s for s in slept if s == MAX_BACKOFF]
        assert len(breaker_sleeps) >= 1
        await sup.stop()

    async def test_breaker_closes_on_successful_probe(self) -> None:
        THRESHOLD = 2
        pc = _PatchedConn(fail_n=THRESHOLD)

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, failure_threshold=THRESHOLD, sleep_fn=fast_sleep)
        await sup.start()
        await asyncio.wait_for(pc._connected_event.wait(), timeout=2.0)

        assert pc.conn.state == ConnectionState.CONNECTED
        assert sup.breaker_open is False
        assert sup.consecutive_failures == 0
        await sup.stop()

    async def test_breaker_cycles_increment_on_open(self) -> None:
        THRESHOLD = 2
        pc = _PatchedConn(always_fail=True)
        slept: list[float] = []

        async def recording_sleep(s: float) -> None:
            slept.append(s)
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, failure_threshold=THRESHOLD, sleep_fn=recording_sleep)
        await sup.start()
        for _ in range(20):
            await asyncio.sleep(0)

        assert sup.breaker_open_cycles >= 1
        await sup.stop()

    async def test_needs_attention_after_escalation_threshold(self) -> None:
        THRESHOLD = 1
        ESCALATION = 2
        pc = _PatchedConn(fail_n=0, always_fail=True)
        slept: list[float] = []

        async def recording_sleep(s: float) -> None:
            slept.append(s)
            await asyncio.sleep(0)

        sup = _make_supervisor(
            pc.conn,
            failure_threshold=THRESHOLD,
            escalation_after=ESCALATION,
            sleep_fn=recording_sleep,
        )
        await sup.start()
        for _ in range(50):
            await asyncio.sleep(0)

        assert sup.needs_attention is True
        await sup.stop()

    async def test_needs_attention_resets_on_connect(self) -> None:
        """needs_attention clears when the connection eventually succeeds."""
        THRESHOLD = 1
        ESCALATION = 1
        pc = _PatchedConn(fail_n=2)

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(
            pc.conn,
            failure_threshold=THRESHOLD,
            escalation_after=ESCALATION,
            sleep_fn=fast_sleep,
        )
        await sup.start()
        await asyncio.wait_for(pc._connected_event.wait(), timeout=2.0)

        assert sup.needs_attention is False
        assert sup.breaker_open is False
        await sup.stop()


# ---------------------------------------------------------------------------
# 6. AUTH_REQUIRED — stops retrying (no storm)
# ---------------------------------------------------------------------------


class TestAuthRequiredBehavior:
    """AUTH_REQUIRED → supervisor stops retrying; waits for external re-trigger."""

    async def test_auth_required_stops_supervisor(self) -> None:
        pc = _PatchedConn(auth_on=1)
        sleeps: list[float] = []

        async def recording_sleep(s: float) -> None:
            sleeps.append(s)
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=recording_sleep)
        await sup.start()
        for _ in range(10):
            await asyncio.sleep(0)

        assert pc.conn.state == ConnectionState.AUTH_REQUIRED
        assert len(sleeps) == 0
        await sup.stop()

    async def test_auth_required_no_connection_storm(self) -> None:
        pc = _PatchedConn(auth_on=1)

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=fast_sleep)
        await sup.start()
        for _ in range(20):
            await asyncio.sleep(0)

        assert pc.attempt == 1
        await sup.stop()


# ---------------------------------------------------------------------------
# 7. Terminal failure — mark_failed, no retry
# ---------------------------------------------------------------------------


class TestTerminalFailure:
    """Non-retryable failures → mark_failed() → FAILED state, no further retry."""

    async def test_terminal_transitions_to_failed(self) -> None:
        pc = _PatchedConn(terminal_on=1)
        sleeps: list[float] = []

        async def recording_sleep(s: float) -> None:
            sleeps.append(s)
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=recording_sleep)
        await sup.start()
        for _ in range(10):
            await asyncio.sleep(0)

        assert pc.conn.state == ConnectionState.FAILED
        assert len(sleeps) == 0
        await sup.stop()

    async def test_terminal_does_not_retry(self) -> None:
        pc = _PatchedConn(terminal_on=1)

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=fast_sleep)
        await sup.start()
        for _ in range(20):
            await asyncio.sleep(0)

        assert pc.attempt == 1
        await sup.stop()


# ---------------------------------------------------------------------------
# 8. No-HOL-block (CRITICAL — must use real concurrent tasks, not mocks)
# ---------------------------------------------------------------------------


class TestNoHOLBlock:
    """A slow/hanging backend must NEVER stall another backend's supervisor."""

    async def test_fast_backend_connects_while_slow_hangs(self) -> None:
        HANG_TIME = 0.2
        hang_gate = asyncio.Event()

        slow_pc = _PatchedConn(name="slow-backend", hang_connect=hang_gate)
        fast_pc = _PatchedConn(name="fast-backend", fail_n=0)

        async def _real_sleep(s: float) -> None:
            await asyncio.sleep(s)

        slow_sup = _make_supervisor(slow_pc.conn, sleep_fn=_real_sleep)
        fast_sup = _make_supervisor(fast_pc.conn, sleep_fn=_real_sleep)

        start = time.monotonic()
        await slow_sup.start()
        await fast_sup.start()

        try:
            await asyncio.wait_for(fast_pc._connected_event.wait(), timeout=HANG_TIME)
        except asyncio.TimeoutError:
            pytest.fail(
                f"HOL block detected — fast backend did not connect in {HANG_TIME}s "
                f"(elapsed: {time.monotonic() - start:.3f}s)"
            )

        elapsed = time.monotonic() - start
        assert elapsed < HANG_TIME
        assert fast_pc.conn.state == ConnectionState.CONNECTED
        assert slow_pc.conn.state != ConnectionState.CONNECTED

        hang_gate.set()
        await asyncio.sleep(0)
        await slow_sup.stop()
        await fast_sup.stop()

    async def test_two_failing_backends_run_independently(self) -> None:
        slept_a: list[float] = []
        slept_b: list[float] = []

        pc_a = _PatchedConn(name="backend-a", always_fail=True)
        pc_b = _PatchedConn(name="backend-b", always_fail=True)

        async def sleep_a(s: float) -> None:
            slept_a.append(s)
            await asyncio.sleep(0)

        async def sleep_b(s: float) -> None:
            slept_b.append(s)
            await asyncio.sleep(0)

        sup_a = _make_supervisor(pc_a.conn, sleep_fn=sleep_a)
        sup_b = _make_supervisor(pc_b.conn, sleep_fn=sleep_b)
        await sup_a.start()
        await sup_b.start()

        for _ in range(20):
            await asyncio.sleep(0)

        assert len(slept_a) > 0
        assert len(slept_b) > 0
        assert sup_a.consecutive_failures > 0
        assert sup_b.consecutive_failures > 0

        await sup_a.stop()
        await sup_b.stop()


# ---------------------------------------------------------------------------
# 9. Manager integration — get_server_status() supervisor fields
# ---------------------------------------------------------------------------


class TestManagerSupervisorIntegration:
    """ConnectionManager.get_server_status() gains supervisor health fields additively."""

    def _make_manager(self, *servers: MCPServerConfig, tmp_path: Any) -> ConnectionManager:
        cfg = HubConfig(config_dir=tmp_path, mcp_servers=tuple(servers))
        registry = CapabilityRegistry()
        return ConnectionManager(cfg, registry)

    def test_status_has_consecutive_failures_field(self, tmp_path: Any) -> None:
        mgr = self._make_manager(_cfg("test-srv"), tmp_path=tmp_path)
        assert "consecutive_failures" in mgr.get_server_status()[0]

    def test_status_has_needs_attention_field(self, tmp_path: Any) -> None:
        mgr = self._make_manager(_cfg("test-srv"), tmp_path=tmp_path)
        assert "needs_attention" in mgr.get_server_status()[0]

    def test_status_has_restart_count_field(self, tmp_path: Any) -> None:
        mgr = self._make_manager(_cfg("test-srv"), tmp_path=tmp_path)
        assert "restart_count" in mgr.get_server_status()[0]

    def test_status_has_breaker_open_field(self, tmp_path: Any) -> None:
        mgr = self._make_manager(_cfg("test-srv"), tmp_path=tmp_path)
        assert "breaker_open" in mgr.get_server_status()[0]

    def test_existing_status_fields_still_present(self, tmp_path: Any) -> None:
        mgr = self._make_manager(_cfg("test-srv"), tmp_path=tmp_path)
        entry = mgr.get_server_status()[0]
        for field in ("name", "transport", "enabled", "connected", "tools", "lifecycle"):
            assert field in entry, f"Missing original field: {field}"

    def test_status_defaults_when_no_supervisor(self, tmp_path: Any) -> None:
        mgr = self._make_manager(_cfg("no-sup-srv"), tmp_path=tmp_path)
        entry = mgr.get_server_status()[0]
        assert entry["consecutive_failures"] == 0
        assert entry["needs_attention"] is False
        assert entry["restart_count"] == 0
        assert entry["breaker_open"] is False

    async def test_supervisor_health_reflected_in_status(self, tmp_path: Any) -> None:
        srv = _cfg("supervised-srv")
        mgr = self._make_manager(srv, tmp_path=tmp_path)
        conn = MCPConnection(srv)
        mgr._connections[srv.name] = conn
        pc = _PatchedConn(name=srv.name, always_fail=True)
        sup = _make_supervisor(pc.conn)
        mgr._supervisors[srv.name] = sup
        sup.consecutive_failures = 3
        sup.needs_attention = True
        sup.restart_count = 1
        entry = next(s for s in mgr.get_server_status() if s["name"] == srv.name)
        assert entry["consecutive_failures"] == 3
        assert entry["needs_attention"] is True
        assert entry["restart_count"] == 1


# ---------------------------------------------------------------------------
# 10. Coverage completions — defensive / edge-case paths
# ---------------------------------------------------------------------------


class TestCoverageEdgePaths:
    """Targeted tests for defensive paths in ConnectionSupervisor."""

    async def test_drop_from_connected_triggers_reconnect_loop(self) -> None:
        """CONNECTED drop fires _capture_event → _drop_event.set() → supervisor loops."""
        pc = _PatchedConn("srv", fail_n=0)
        second_connect = asyncio.Event()
        original_set = pc._connected_event.set
        call_count = [0]

        def counting_set() -> None:
            call_count[0] += 1
            original_set()
            if call_count[0] >= 2:
                second_connect.set()

        pc._connected_event.set = counting_set  # type: ignore[method-assign]

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=fast_sleep)
        await sup.start()
        await asyncio.wait_for(pc._connected_event.wait(), timeout=2.0)
        assert pc.conn.state == ConnectionState.CONNECTED

        for _ in range(3):
            await asyncio.sleep(0)

        # Trigger drop from CONNECTED → _drop_event fires
        pc.conn._transition(ConnectionState.DISCONNECTED, "simulated drop")
        await asyncio.wait_for(second_connect.wait(), timeout=2.0)
        assert pc.attempt >= 2
        await sup.stop()

    async def test_cancelled_during_connect_propagates(self) -> None:
        """CancelledError during connect() is re-raised, not swallowed (line 260)."""
        hang_gate: asyncio.Event = asyncio.Event()
        pc = _PatchedConn("srv", hang_connect=hang_gate)

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=fast_sleep)
        await sup.start()
        await asyncio.sleep(0)  # supervisor enters connect() and hangs
        assert not hang_gate.is_set()
        await sup.stop()  # cancels task while inside connect()
        assert sup._task is None or sup._task.done()

    async def test_unexpected_exception_in_connect_marks_failed(self) -> None:
        """Non-ConnectionError from connect() → mark_failed, no retry (lines 264-274)."""
        conn = _bare_conn("srv")
        call_count = [0]

        async def bad_connect() -> None:
            call_count[0] += 1
            conn._transition(ConnectionState.CONNECTING, "attempt")
            raise ValueError("unexpected internal error")

        conn.connect = bad_connect  # type: ignore[method-assign]

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(conn, sleep_fn=fast_sleep)
        await sup.start()
        for _ in range(10):
            await asyncio.sleep(0)

        assert conn.state == ConnectionState.FAILED
        assert call_count[0] == 1
        await sup.stop()

    async def test_unexpected_state_after_connect_retries(self) -> None:
        """connect() returns but state is not CONNECTED → supervisor retries (lines 290-297)."""
        conn = _bare_conn("srv")
        attempt_count = [0]
        connected = asyncio.Event()

        async def weird_connect() -> None:
            attempt_count[0] += 1
            conn._transition(ConnectionState.CONNECTING, "attempt")
            if attempt_count[0] <= 2:
                return  # leave state as CONNECTING — unexpected
            conn._transition(ConnectionState.CONNECTED, "success")
            conn._connected_at = time.time()
            connected.set()

        conn.connect = weird_connect  # type: ignore[method-assign]

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(conn, sleep_fn=fast_sleep)
        await sup.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)
        assert attempt_count[0] == 3
        await sup.stop()

    async def test_cancel_during_wait_cleans_up_subtasks(self) -> None:
        """Direct task cancel in _wait_for_drop_or_stop hits finally cleanup (lines 452-456)."""
        pc = _PatchedConn("srv", fail_n=0)

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=fast_sleep)
        await sup.start()
        await asyncio.wait_for(pc._connected_event.wait(), timeout=2.0)

        for _ in range(3):
            await asyncio.sleep(0)

        assert sup._task is not None
        sup._task.cancel()
        try:
            await sup._task
        except asyncio.CancelledError:
            pass
        sup._task = None  # prevent double-cancel in stop()
        await sup.stop()


# ---------------------------------------------------------------------------
# 11. W1-P3 — supervisor wiring: classify_failure drives transitions
# ---------------------------------------------------------------------------


class TestW1P3ClassifierWiring:
    """Verify that classify_failure() is used to route exceptions to the right transition.

    The key behaviour change from W1-P2: instead of relying solely on the
    ``failure_class`` string embedded in lifecycle events, the supervisor now
    passes the caught exception to ``classify_failure()`` and uses the returned
    :class:`FailureClass` to decide the next transition.

    These tests use raw exception injection (not _PatchedConn) so that the
    supervisor gets exceptions that differ from the simple TRANSIENT/TERMINAL
    lifecycle-event signals that _PatchedConn injects.
    """

    async def test_file_not_found_cause_triggers_terminal(self) -> None:
        """ConnectionError wrapping FileNotFoundError → TERMINAL via classify_failure.

        This is the primary W1-P3 wiring scenario: the lifecycle event does NOT
        carry failure_class (simulating a raw raise without connection.py's classify
        hook).  The supervisor must call classify_failure(exc) on the caught
        ConnectionError to detect the terminal root cause via __cause__ inspection,
        rather than falling back to the ``_last_failure_class or "TRANSIENT"``
        default and retrying a bad binary path forever.

        With W1-P2 code (no classify_failure): _last_failure_class is None →
        defaults to TRANSIENT → supervisor retries (wrong, attempt_count > 1).
        With W1-P3 code (classify_failure): TERMINAL detected → mark_failed,
        stop immediately (attempt_count == 1).
        """
        conn = _bare_conn("stdio-bad")
        attempt_count = [0]

        async def bad_connect() -> None:
            attempt_count[0] += 1
            conn._transition(ConnectionState.CONNECTING, "attempt")
            root = FileNotFoundError("No such file: /opt/bad-server")
            wrapped = ConnectionError(
                "MCP stdio-bad initialization failed (FileNotFoundError)"
            )
            wrapped.__cause__ = root
            # Intentionally omit failure_class in the event — the supervisor must
            # classify via the exception itself, NOT the lifecycle event string.
            conn._transition(
                ConnectionState.ERROR,
                "ConnectionError during connect",
                # No failure_class here: _last_failure_class stays None
            )
            raise wrapped

        conn.connect = bad_connect  # type: ignore[method-assign]

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(conn, sleep_fn=fast_sleep)
        await sup.start()
        for _ in range(10):
            await asyncio.sleep(0)

        # Terminal: supervisor must call mark_failed and stop — no repeated retries
        assert conn.state == ConnectionState.FAILED, (
            f"Expected FAILED after terminal classification, got {conn.state.value}"
        )
        assert attempt_count[0] == 1, (
            f"Terminal failure must not retry: expected 1 attempt, got {attempt_count[0]}"
        )

    async def test_transient_connection_error_retries(self) -> None:
        """Plain ConnectionError (no terminal cause) → TRANSIENT → supervisor retries."""
        conn = _bare_conn("http-flaky")
        attempt_count = [0]
        connected = asyncio.Event()

        async def flaky_connect() -> None:
            attempt_count[0] += 1
            conn._transition(ConnectionState.CONNECTING, "attempt")
            if attempt_count[0] < 3:
                conn._transition(ConnectionState.ERROR, "transient fail", failure_class="TRANSIENT")
                raise ConnectionError("Connection refused")
            conn._transition(ConnectionState.CONNECTED, "success")
            conn._connected_at = time.time()
            connected.set()

        conn.connect = flaky_connect  # type: ignore[method-assign]

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(conn, sleep_fn=fast_sleep)
        await sup.start()
        await asyncio.wait_for(connected.wait(), timeout=2.0)

        assert conn.state == ConnectionState.CONNECTED
        assert attempt_count[0] == 3, (
            f"TRANSIENT must retry until success; expected 3 attempts, got {attempt_count[0]}"
        )
        await sup.stop()

    async def test_non_connection_error_terminal_classified_by_classifier(self) -> None:
        """Non-ConnectionError exception (NotImplementedError) → TERMINAL via classifier.

        In W1-P2 the supervisor's outer ``except Exception`` always marked TERMINAL.
        In W1-P3 it calls classify_failure() — NotImplementedError is TERMINAL so
        the outcome is the same, but the routing is now through the classifier.
        """
        conn = _bare_conn("bad-transport")
        attempt_count = [0]

        async def bad_transport_connect() -> None:
            attempt_count[0] += 1
            conn._transition(ConnectionState.CONNECTING, "attempt")
            raise NotImplementedError("Transport 'grpc' is not supported")

        conn.connect = bad_transport_connect  # type: ignore[method-assign]

        async def fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(conn, sleep_fn=fast_sleep)
        await sup.start()
        for _ in range(10):
            await asyncio.sleep(0)

        assert conn.state == ConnectionState.FAILED
        assert attempt_count[0] == 1
        await sup.stop()
