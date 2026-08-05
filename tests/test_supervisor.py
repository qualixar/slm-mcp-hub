"""W1-P2 — ConnectionSupervisor tests (classes 1-4).

TDD: write RED first, then implement GREEN.

Covers:
- MCPConnection supervisor hooks (enter_reconnecting, enter_circuit_open, mark_failed)
- Backoff schedule (seeded / injected RNG, full-jitter formula)
- Supervisor lifecycle (start / stop / task isolation)
- Connect success / failure / health surface

Classes 5-10 (circuit-breaker, auth, terminal, HOL, manager integration, coverage edges)
are in test_supervisor_resilience.py to stay under the 800-line hard cap.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.resilience.supervisor import ConnectionSupervisor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(name: str = "test-srv", **kw: Any) -> MCPServerConfig:
    defaults: dict[str, Any] = dict(transport="stdio", command="echo", args=("hi",))
    defaults.update(kw)
    return MCPServerConfig(name=name, **defaults)


def _bare_conn(name: str = "test-srv") -> MCPConnection:
    """Real MCPConnection with no mock — for hook tests."""
    return MCPConnection(_cfg(name=name))


def _noop_sleep() -> Any:
    """Return an injected sleep that completes immediately."""

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


# Fake connection for supervisor tests that patches connect() on a real MCPConnection
class _PatchedConn:
    """Wraps a real MCPConnection, patches connect() for test control."""

    def __init__(
        self,
        name: str = "srv",
        *,
        fail_n: int = 0,          # fail first N times, then succeed
        always_fail: bool = False,
        terminal_on: int | None = None,  # raise terminal on this attempt (1-indexed)
        auth_on: int | None = None,      # raise auth-required on this attempt
        hang_connect: asyncio.Event | None = None,  # block until event set
    ) -> None:
        self._inner = MCPConnection(_cfg(name=name))
        self.attempt = 0
        self._fail_n = fail_n
        self._always_fail = always_fail
        self._terminal_on = terminal_on
        self._auth_on = auth_on
        self._hang_connect = hang_connect
        self._connected_event: asyncio.Event = asyncio.Event()
        # Patch connect on the inner instance
        self._inner.connect = self._connect  # type: ignore[method-assign]

    async def _connect(self) -> None:
        self.attempt += 1

        if self._hang_connect is not None:
            await self._hang_connect.wait()
            raise ConnectionError("hang connect resolved to fail")

        if self._auth_on is not None and self.attempt == self._auth_on:
            # Simulate what real connect() does for auth
            self._inner._transition(
                ConnectionState.CONNECTING, "attempt"
            )
            self._inner._transition(
                ConnectionState.AUTH_REQUIRED,
                "auth required",
                failure_class="AUTH",
            )
            return  # real connect() returns without raising for AUTH_REQUIRED

        if self._terminal_on is not None and self.attempt == self._terminal_on:
            self._inner._transition(ConnectionState.CONNECTING, "attempt")
            self._inner._transition(
                ConnectionState.ERROR, "terminal error", failure_class="TERMINAL"
            )
            raise ConnectionError("terminal: bad config")

        if self._always_fail or self.attempt <= self._fail_n:
            self._inner._transition(ConnectionState.CONNECTING, "attempt")
            self._inner._transition(
                ConnectionState.ERROR, "transient fail", failure_class="TRANSIENT"
            )
            raise ConnectionError(f"transient fail #{self.attempt}")

        # Success
        self._inner._transition(ConnectionState.CONNECTING, "attempt")
        self._inner._transition(ConnectionState.CONNECTED, "success")
        self._inner._connected_at = time.time()
        self._connected_event.set()

    @property
    def conn(self) -> MCPConnection:
        return self._inner


# ---------------------------------------------------------------------------
# 1. MCPConnection supervisor hooks
# ---------------------------------------------------------------------------


class TestMCPConnectionSupervisorHooks:
    """Three thin public hooks added to MCPConnection for supervisor use."""

    def test_enter_reconnecting_exists(self) -> None:
        conn = _bare_conn()
        assert callable(getattr(conn, "enter_reconnecting", None))

    def test_enter_circuit_open_exists(self) -> None:
        conn = _bare_conn()
        assert callable(getattr(conn, "enter_circuit_open", None))

    def test_mark_failed_exists(self) -> None:
        conn = _bare_conn()
        assert callable(getattr(conn, "mark_failed", None))

    def test_enter_reconnecting_sets_reconnecting_state(self) -> None:
        conn = _bare_conn()
        conn._transition(ConnectionState.ERROR, "set up")
        conn.enter_reconnecting(attempt=1)
        assert conn.state == ConnectionState.RECONNECTING

    def test_enter_reconnecting_returns_lifecycle_event(self) -> None:
        from slm_mcp_hub.resilience.lifecycle import LifecycleEvent

        conn = _bare_conn()
        event = conn.enter_reconnecting(attempt=3)
        assert isinstance(event, LifecycleEvent)
        assert event.to_state == ConnectionState.RECONNECTING
        assert event.attempt == 3

    def test_enter_reconnecting_encodes_attempt_in_event(self) -> None:
        conn = _bare_conn()
        event = conn.enter_reconnecting(attempt=7)
        assert event.attempt == 7

    def test_enter_circuit_open_sets_circuit_open_state(self) -> None:
        conn = _bare_conn()
        conn._transition(ConnectionState.RECONNECTING, "set up")
        conn.enter_circuit_open()
        assert conn.state == ConnectionState.CIRCUIT_OPEN

    def test_enter_circuit_open_returns_lifecycle_event(self) -> None:
        from slm_mcp_hub.resilience.lifecycle import LifecycleEvent

        conn = _bare_conn()
        event = conn.enter_circuit_open()
        assert isinstance(event, LifecycleEvent)
        assert event.to_state == ConnectionState.CIRCUIT_OPEN

    def test_mark_failed_sets_failed_state(self) -> None:
        conn = _bare_conn()
        conn._transition(ConnectionState.CONNECTING, "set up")
        conn.mark_failed(reason="bad config")
        assert conn.state == ConnectionState.FAILED

    def test_mark_failed_returns_terminal_class_event(self) -> None:
        from slm_mcp_hub.resilience.lifecycle import LifecycleEvent

        conn = _bare_conn()
        event = conn.mark_failed(reason="bad config")
        assert isinstance(event, LifecycleEvent)
        assert event.failure_class == "TERMINAL"
        assert event.to_state == ConnectionState.FAILED

    def test_mark_failed_reason_in_event(self) -> None:
        conn = _bare_conn()
        event = conn.mark_failed(reason="unknown transport")
        assert "unknown transport" in event.reason

    def test_enter_reconnecting_uses_private_transition(self) -> None:
        """enter_reconnecting must NOT bypass _transition (state setter)."""
        events: list[Any] = []
        conn = _bare_conn()
        conn.on_event = events.append
        conn.enter_reconnecting(attempt=2)
        assert len(events) == 1
        assert events[0].to_state == ConnectionState.RECONNECTING

    def test_enter_circuit_open_emits_event(self) -> None:
        events: list[Any] = []
        conn = _bare_conn()
        conn.on_event = events.append
        conn.enter_circuit_open()
        assert len(events) == 1
        assert events[0].to_state == ConnectionState.CIRCUIT_OPEN

    def test_mark_failed_emits_event(self) -> None:
        events: list[Any] = []
        conn = _bare_conn()
        conn.on_event = events.append
        conn.mark_failed(reason="terminal")
        assert len(events) == 1
        assert events[0].to_state == ConnectionState.FAILED


# ---------------------------------------------------------------------------
# 2. Backoff schedule — seeded RNG, full-jitter formula
# ---------------------------------------------------------------------------


class TestBackoffSchedule:
    """sleep = rng.uniform(0, min(max, base * factor ** attempt))."""

    def _sup_with_rng(self, rng: random.Random) -> ConnectionSupervisor:
        conn = _bare_conn()
        return _make_supervisor(conn, rng=rng, backoff_base=1.0, backoff_factor=2.0, backoff_max=60.0)

    def test_backoff_attempt_0_capped_at_1(self) -> None:
        """Attempt 0: cap = min(60, 1*2^0) = 1.0."""
        always_max_rng = MagicMock()
        always_max_rng.uniform = lambda lo, hi: hi  # returns cap
        sup = self._sup_with_rng(always_max_rng)
        assert sup._compute_backoff(0) == pytest.approx(1.0)

    def test_backoff_attempt_1_capped_at_2(self) -> None:
        always_max_rng = MagicMock()
        always_max_rng.uniform = lambda lo, hi: hi
        sup = self._sup_with_rng(always_max_rng)
        assert sup._compute_backoff(1) == pytest.approx(2.0)

    def test_backoff_attempt_5_capped_at_32(self) -> None:
        always_max_rng = MagicMock()
        always_max_rng.uniform = lambda lo, hi: hi
        sup = self._sup_with_rng(always_max_rng)
        assert sup._compute_backoff(5) == pytest.approx(32.0)

    def test_backoff_attempt_6_capped_at_max(self) -> None:
        """Attempt 6: 1*2^6 = 64 > 60 → capped at 60.0."""
        always_max_rng = MagicMock()
        always_max_rng.uniform = lambda lo, hi: hi
        sup = self._sup_with_rng(always_max_rng)
        assert sup._compute_backoff(6) == pytest.approx(60.0)

    def test_backoff_beyond_max_still_capped(self) -> None:
        always_max_rng = MagicMock()
        always_max_rng.uniform = lambda lo, hi: hi
        sup = self._sup_with_rng(always_max_rng)
        for n in (7, 8, 10, 20):
            assert sup._compute_backoff(n) == pytest.approx(60.0), f"attempt {n}"

    def test_backoff_always_nonnegative(self) -> None:
        """Jitter lower bound = 0: uniform(0, cap) >= 0."""
        zero_rng = MagicMock()
        zero_rng.uniform = lambda lo, hi: lo  # always returns 0.0
        sup = self._sup_with_rng(zero_rng)
        for n in range(8):
            assert sup._compute_backoff(n) == pytest.approx(0.0)

    def test_backoff_uses_seeded_rng_deterministically(self) -> None:
        """Seeded RNG produces identical schedule across two identically-seeded sups."""
        seed = 12345
        rng_a = random.Random(seed)
        rng_b = random.Random(seed)
        sup_a = self._sup_with_rng(rng_a)
        sup_b = self._sup_with_rng(rng_b)
        for attempt in range(8):
            assert sup_a._compute_backoff(attempt) == pytest.approx(
                sup_b._compute_backoff(attempt)
            )

    def test_backoff_max_overrides_base_factor(self) -> None:
        """With backoff_max=5, attempt 10 still returns <= 5."""
        always_max_rng = MagicMock()
        always_max_rng.uniform = lambda lo, hi: hi
        conn = _bare_conn()
        sup = ConnectionSupervisor(
            conn,
            backoff_base=1.0,
            backoff_factor=2.0,
            backoff_max=5.0,
            rng=always_max_rng,
            sleep_fn=_noop_sleep(),
        )
        assert sup._compute_backoff(10) == pytest.approx(5.0)

    def test_backoff_custom_base_and_factor(self) -> None:
        always_max_rng = MagicMock()
        always_max_rng.uniform = lambda lo, hi: hi
        conn = _bare_conn()
        sup = ConnectionSupervisor(
            conn,
            backoff_base=2.0,
            backoff_factor=3.0,
            backoff_max=200.0,
            rng=always_max_rng,
            sleep_fn=_noop_sleep(),
        )
        # attempt 2: min(200, 2*3^2) = min(200, 18) = 18
        assert sup._compute_backoff(2) == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# 3. Supervisor lifecycle — start / stop / task isolation
# ---------------------------------------------------------------------------


class TestSupervisorLifecycle:
    """start() / stop() manage the supervised loop in an isolated asyncio task."""

    async def test_start_creates_task(self) -> None:
        pc = _PatchedConn(always_fail=True)
        sup = _make_supervisor(pc.conn)
        await sup.start()
        assert sup._task is not None
        assert not sup._task.done()
        await sup.stop()

    async def test_start_is_idempotent(self) -> None:
        pc = _PatchedConn(always_fail=True)
        sup = _make_supervisor(pc.conn)
        await sup.start()
        first_task = sup._task
        await sup.start()  # second call — must NOT create a new task
        assert sup._task is first_task
        await sup.stop()

    async def test_stop_cancels_task(self) -> None:
        pc = _PatchedConn(always_fail=True)
        sup = _make_supervisor(pc.conn)
        await sup.start()
        assert not sup._task.done()  # type: ignore[union-attr]
        await sup.stop()
        assert sup._task is None or sup._task.done()

    async def test_stop_before_start_is_noop(self) -> None:
        pc = _PatchedConn(always_fail=True)
        sup = _make_supervisor(pc.conn)
        await sup.stop()  # Should not raise

    async def test_task_name_includes_server_name(self) -> None:
        pc = _PatchedConn(name="my-backend", always_fail=True)
        sup = _make_supervisor(pc.conn)
        await sup.start()
        assert "my-backend" in (sup._task.get_name() if sup._task else "")
        await sup.stop()


# ---------------------------------------------------------------------------
# 4. Supervisor loop — connect success / failure / backoff
# ---------------------------------------------------------------------------


class TestSupervisorConnectBehavior:
    """Supervisor calls connect(), handles success and transient failures."""

    async def test_successful_connect_resets_failures(self) -> None:
        """On first successful connect, consecutive_failures = 0 and restart_count = 1."""
        pc = _PatchedConn(fail_n=0)  # succeed immediately

        async def _fast_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=_fast_sleep)
        await sup.start()

        # Wait until connected
        await asyncio.wait_for(pc._connected_event.wait(), timeout=2.0)

        assert pc.conn.state == ConnectionState.CONNECTED
        assert sup.consecutive_failures == 0
        assert sup.restart_count == 1
        assert sup.needs_attention is False

        await sup.stop()

    async def test_failures_increment_consecutive_count(self) -> None:
        """Each transient failure increments consecutive_failures before success."""
        slept_counts: list[float] = []
        pc = _PatchedConn(fail_n=3)  # fail 3 times then succeed

        async def recording_sleep(s: float) -> None:
            slept_counts.append(s)
            await asyncio.sleep(0)

        sup = _make_supervisor(pc.conn, sleep_fn=recording_sleep)
        await sup.start()

        await asyncio.wait_for(pc._connected_event.wait(), timeout=2.0)
        # 3 sleeps (one per transient retry)
        assert len(slept_counts) >= 3
        # After success, failures reset
        assert sup.consecutive_failures == 0

        await sup.stop()

    async def test_health_surface_state_property(self) -> None:
        """sup.state proxies conn.state."""
        pc = _PatchedConn(always_fail=True)
        sup = _make_supervisor(pc.conn)
        # Not yet started — state is whatever conn.state is
        assert sup.state == pc.conn.state
        await sup.start()
        await asyncio.sleep(0)
        await sup.stop()

    async def test_health_snapshot_returns_dict(self) -> None:
        pc = _PatchedConn(always_fail=True)
        sup = _make_supervisor(pc.conn)
        snap = sup.health_snapshot()
        assert isinstance(snap, dict)
        assert "consecutive_failures" in snap
        assert "needs_attention" in snap
        assert "restart_count" in snap
        assert "breaker_open" in snap
        assert "breaker_open_cycles" in snap
        assert "last_error" in snap
        assert "state" in snap
