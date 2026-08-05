"""Regression tests for W1-P2 audit findings.

Each test is explicitly designed to FAIL before the fix and PASS after:

  BLOCKING-1 (case b): test_connected_at_boot_drop_then_reconnect
    Backend CONNECTED at boot → _ensure_supervisors creates a supervisor for it →
    supervisor detects the drop and reconnects. No manager restart required.

  BLOCKING-2 (case a): test_late_failure_admitted_on_tick
    Backend that is NOT in _failed at coordinator start-time fails later →
    _ensure_supervisors is called again on the next tick → supervisor is created
    and retries start.

  MAJOR: test_wait_returns_immediately_when_not_connected
    _wait_for_drop_or_stop is level-triggered: if the connection is already not
    CONNECTED when the method is entered, it returns immediately without awaiting
    the drop event.  Without this guard the method hangs until stop() is called.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.manager import ConnectionManager
from slm_mcp_hub.resilience.supervisor import ConnectionSupervisor

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _srv_cfg(name: str = "test-srv", **kw: Any) -> MCPServerConfig:
    defaults: dict[str, Any] = dict(transport="stdio", command="echo", args=("hi",))
    defaults.update(kw)
    return MCPServerConfig(name=name, **defaults)


def _noop_sleep() -> Any:
    async def _sleep(_s: float) -> None:
        await asyncio.sleep(0)

    return _sleep


def _make_mgr(server_names: list[str]) -> ConnectionManager:
    cfg = HubConfig(
        mcp_servers=[_srv_cfg(name=n) for n in server_names],
        host="localhost",
        port=8000,
    )
    return ConnectionManager(cfg, CapabilityRegistry())


# ---------------------------------------------------------------------------
# BLOCKING — dynamic failure admission
# ---------------------------------------------------------------------------


class TestBlockingDynamicAdmission:
    """_ensure_supervisors() must cover ALL configured backends, not just _failed."""

    async def test_connected_at_boot_drop_then_reconnect(self) -> None:
        """BLOCKING case (b): CONNECTED at boot → supervisor watches drop → reconnects.

        The old _supervisor_fleet_coordinator only started supervisors for backends
        already in self._failed.  A backend that was connected at boot time never
        received a supervisor, so unexpected drops went undetected forever.
        """
        mgr = _make_mgr(["srv1"])
        reconnect_count = 0

        # Build a MCPConnection already in CONNECTED state (simulates boot-time success)
        conn = MCPConnection(_srv_cfg(name="srv1"))
        conn._state = ConnectionState.CONNECTED
        conn._connected_at = time.time()

        async def patched_connect() -> None:
            nonlocal reconnect_count
            # Mimic the real MCPConnection.connect() CONNECTED guard
            if conn._state == ConnectionState.CONNECTED:
                return
            reconnect_count += 1
            conn._transition(ConnectionState.CONNECTING, "reconnect")
            conn._transition(ConnectionState.CONNECTED, "reconnect success")
            conn._connected_at = time.time()

        conn.connect = patched_connect  # type: ignore[method-assign]
        mgr._connections["srv1"] = conn
        # srv1 is NOT in _failed — it connected fine at boot
        assert "srv1" not in mgr._failed

        # _ensure_supervisors must create a supervisor even though srv1 is not failed
        await mgr._ensure_supervisors()

        assert "srv1" in mgr._supervisors, (
            "BLOCKING: _ensure_supervisors must create supervisor for CONNECTED backend"
        )
        sup = mgr._supervisors["srv1"]
        assert sup._task is not None
        assert not sup._task.done()

        # Let the supervisor task run and settle into _wait_for_drop_or_stop
        for _ in range(20):
            await asyncio.sleep(0)

        assert conn.state == ConnectionState.CONNECTED
        assert reconnect_count == 0  # no reconnects yet — all is well

        # Simulate an unexpected network drop
        conn._transition(ConnectionState.DISCONNECTED, "network drop")

        # Give the supervisor time to detect the drop and reconnect
        for _ in range(20):
            await asyncio.sleep(0)

        assert reconnect_count >= 1, (
            "BLOCKING: supervisor must reconnect after the drop"
        )
        await sup.stop()

    async def test_late_failure_admitted_on_tick(self) -> None:
        """BLOCKING case (a): backend fails AFTER coordinator started → admitted on tick.

        The old coordinator snapshotted self._failed at launch and never revisited
        it on each tick.  Backends that failed after the coordinator started were
        never supervised and never retried.
        """
        mgr = _make_mgr(["early-srv", "late-srv"])

        # "early-srv" is in _failed at coordinator first-tick time
        mgr._failed["early-srv"] = "initial failure"

        # First call to _ensure_supervisors (simulates coordinator start)
        await mgr._ensure_supervisors()
        assert "early-srv" in mgr._supervisors, "early-srv must be supervised"
        # late-srv is not yet in _failed — it may or may not have a supervisor yet
        # (acceptable: _ensure_supervisors covers all configured backends)

        # Now simulate late-srv failing AFTER the coordinator first tick
        mgr._failed["late-srv"] = "late failure"

        # Second call to _ensure_supervisors (simulates the 5 s tick)
        await mgr._ensure_supervisors()

        assert "late-srv" in mgr._supervisors, (
            "BLOCKING: backend that fails after coordinator start must be admitted "
            "on the next _ensure_supervisors tick"
        )

        # Clean up
        for sup in mgr._supervisors.values():
            await sup.stop()


# ---------------------------------------------------------------------------
# MAJOR — drop-detection arming race (level-triggered fix)
# ---------------------------------------------------------------------------


class TestMajorDropArmingRace:
    """_wait_for_drop_or_stop must be level-triggered, not edge-triggered."""

    async def test_wait_returns_immediately_when_not_connected(self) -> None:
        """MAJOR: if conn is already not CONNECTED, _wait_for_drop_or_stop must return
        immediately rather than blocking until the drop event is set.

        Without the fix:
          The method creates two tasks (stop.wait + drop_event.wait) and awaits
          asyncio.wait().  Neither event is set → blocks until timeout → TimeoutError.

        With the fix:
          The method checks `if not self._conn.is_connected: return` before creating
          any tasks → returns immediately → test passes with no exception.
        """
        conn = MCPConnection(_srv_cfg(name="race-srv"))
        # conn is DISCONNECTED (default) — simulates the race where the drop fired
        # before _drop_event was initialized (when _drop_event was still None).
        assert not conn.is_connected

        sup = ConnectionSupervisor(conn, sleep_fn=_noop_sleep())
        # Manually arm the internal events that _wait_for_drop_or_stop expects
        sup._stop_event = asyncio.Event()   # not set
        sup._drop_event = asyncio.Event()   # armed but NOT set

        # Without the fix this times out; with the fix it returns immediately.
        await asyncio.wait_for(
            sup._wait_for_drop_or_stop(sup._stop_event),
            timeout=0.2,
        )
        # Reaching here means the fix is in place.

    async def test_supervisor_handles_pre_loop_drop_without_hanging(self) -> None:
        """MAJOR: drop fires before _drop_event is armed (while None) → no hang.

        Race timeline:
          1. supervisor __init__: _drop_event = None, on_event installed
          2. start() schedules _supervised_loop (not yet running)
          3. External code transitions conn CONNECTED → DISCONNECTED
             → _capture_event fires; _drop_event is None → drop NOT edge-captured
          4. _supervised_loop finally runs:
             - _drop_event = asyncio.Event() (new)
             - connect() called: conn is DISCONNECTED → patched_connect runs
               → conn transitions to CONNECTED → reconnect_done.set()
        With the level-triggered fix in _wait_for_drop_or_stop, even if the
        drop event is missed, the is_connected check prevents a subsequent hang
        if the same race happens after a successful reconnect.
        """
        reconnect_done: asyncio.Event = asyncio.Event()

        conn = MCPConnection(_srv_cfg(name="race-srv2"))
        conn._state = ConnectionState.CONNECTED
        conn._connected_at = time.time()

        async def patched_connect() -> None:
            if conn._state == ConnectionState.CONNECTED:
                return  # no-op guard
            conn._transition(ConnectionState.CONNECTING, "reconnect")
            conn._transition(ConnectionState.CONNECTED, "reconnect success")
            conn._connected_at = time.time()
            reconnect_done.set()

        conn.connect = patched_connect  # type: ignore[method-assign]

        sup = ConnectionSupervisor(conn, sleep_fn=_noop_sleep())
        # _drop_event is None at this point — the race window

        # Schedule start() but drop fires BEFORE the task actually runs
        start_task = asyncio.create_task(sup.start())
        # Trigger drop while _drop_event is still None
        conn._transition(ConnectionState.DISCONNECTED, "pre-loop drop")

        # Let everything run — supervisor must reconnect without hanging
        await asyncio.wait_for(reconnect_done.wait(), timeout=2.0)
        assert conn.state == ConnectionState.CONNECTED

        await sup.stop()
        await start_task


# ---------------------------------------------------------------------------
# W1-P3 REGRESSION — terminal-churn fix (_ensure_supervisors terminal skip)
# ---------------------------------------------------------------------------


class TestTerminalChurnFix:
    """W1-P3: a FAILED backend must NOT be re-supervised on every coordinator tick.

    W1-P2 review (Round 2) deferred this fix to W1-P3 as "terminal-failure churn":
    _ensure_supervisors() was called on every 5 s tick and would re-create a supervisor
    for a backend whose task had ended — including one that terminated because the
    failure was TERMINAL (bad config, missing binary, unknown transport).

    The fix: skip any backend whose MCPConnection state is FAILED.

    An explicit ``reconnect(server)`` call (which creates a fresh MCPConnection in
    DISCONNECTED state) must still be able to resume supervision.
    """

    async def test_failed_backend_not_re_supervised_on_tick(self) -> None:
        """TERMINAL backend: _ensure_supervisors must NOT create a new supervisor
        when the connection state is FAILED.

        Without the fix: task.done() == True → supervisor recreated → retries resume
        → another terminal failure → supervisor stops → loop on every tick (churn).
        With the fix: conn.state == FAILED → skip → no new supervisor → no churn.
        """
        mgr = _make_mgr(["bad-binary"])

        # Set up a connection already in FAILED state (simulates terminal failure)
        conn = MCPConnection(_srv_cfg(name="bad-binary"))
        conn._state = ConnectionState.FAILED  # terminal — no retry
        mgr._connections["bad-binary"] = conn

        # Place a stopped supervisor (task is None / done) to simulate the post-terminal state
        sup = ConnectionSupervisor(conn, sleep_fn=_noop_sleep())
        mgr._supervisors["bad-binary"] = sup
        # The supervisor task is None (never started, or already ended)
        assert sup._task is None or (sup._task is not None and sup._task.done())

        # First tick: should NOT create a new supervisor for the FAILED backend
        await mgr._ensure_supervisors()

        assert mgr._supervisors["bad-binary"] is sup, (
            "W1-P3 terminal-churn fix: FAILED backend must NOT get a new supervisor on tick; "
            "original supervisor object must be unchanged"
        )
        # Connection must remain in FAILED — no reconnect attempts
        assert conn.state == ConnectionState.FAILED

    async def test_reconnecting_backend_gets_supervisor(self) -> None:
        """A backend in RECONNECTING (not FAILED) state must get a supervisor on tick.

        This verifies the terminal-skip is ONLY for FAILED state — other states
        (ERROR, RECONNECTING, DISCONNECTED) must still be supervised.
        """
        mgr = _make_mgr(["flaky-srv"])

        conn = MCPConnection(_srv_cfg(name="flaky-srv"))
        conn._state = ConnectionState.RECONNECTING  # transient — should be supervised
        mgr._connections["flaky-srv"] = conn

        reconnect_attempts = [0]

        async def patched_connect() -> None:
            reconnect_attempts[0] += 1
            if conn._state != ConnectionState.CONNECTED:
                conn._transition(ConnectionState.CONNECTING, "attempt")
                conn._transition(ConnectionState.CONNECTED, "success")
                conn._connected_at = time.time()

        conn.connect = patched_connect  # type: ignore[method-assign]

        await mgr._ensure_supervisors()

        assert "flaky-srv" in mgr._supervisors, (
            "RECONNECTING backend must receive a supervisor on tick"
        )
        sup = mgr._supervisors["flaky-srv"]
        assert sup._task is not None
        assert not sup._task.done()

        await sup.stop()

    async def test_failed_backend_can_reconnect_after_explicit_reconnect(self) -> None:
        """After explicit reconnect(), a previously-FAILED backend gets a new supervisor.

        reconnect() creates a fresh MCPConnection (DISCONNECTED), which is not in
        FAILED state.  The next _ensure_supervisors() tick must then admit it.
        """
        mgr = _make_mgr(["recoverable-srv"])

        # Simulate a previously-failed state
        bad_conn = MCPConnection(_srv_cfg(name="recoverable-srv"))
        bad_conn._state = ConnectionState.FAILED
        mgr._connections["recoverable-srv"] = bad_conn

        old_sup = ConnectionSupervisor(bad_conn, sleep_fn=_noop_sleep())
        mgr._supervisors["recoverable-srv"] = old_sup

        # Tick 1: FAILED → skip
        await mgr._ensure_supervisors()
        assert mgr._supervisors["recoverable-srv"] is old_sup  # unchanged

        # Simulate explicit reconnect(): manager replaces the connection object
        new_conn = MCPConnection(_srv_cfg(name="recoverable-srv"))
        new_conn._state = ConnectionState.DISCONNECTED  # fresh — not FAILED

        async def patched_connect() -> None:
            if new_conn._state != ConnectionState.CONNECTED:
                new_conn._transition(ConnectionState.CONNECTING, "reconnect")
                new_conn._transition(ConnectionState.CONNECTED, "success")
                new_conn._connected_at = time.time()

        new_conn.connect = patched_connect  # type: ignore[method-assign]
        mgr._connections["recoverable-srv"] = new_conn  # replaces FAILED conn

        # Tick 2: new conn is DISCONNECTED → NOT FAILED → new supervisor admitted
        await mgr._ensure_supervisors()

        new_sup = mgr._supervisors["recoverable-srv"]
        assert new_sup is not old_sup, (
            "After explicit reconnect (new DISCONNECTED conn), "
            "_ensure_supervisors must create a new supervisor"
        )
        assert new_conn.state in (
            ConnectionState.CONNECTED,
            ConnectionState.CONNECTING,
            ConnectionState.DISCONNECTED,
        )
        await new_sup.stop()
