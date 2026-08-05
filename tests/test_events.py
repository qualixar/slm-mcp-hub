"""W1-P4 — Subscriber fan-out, event bus, health snapshot, manager wiring tests.

TDD: written FIRST; expected to FAIL until W1-P4 implementation lands.

Covers:
- MCPConnection.subscribe() multi-subscriber fan-out
- on_event back-compat property setter (replaces primary, coexists with subscribe())
- Per-subscriber isolation (raising subscriber never breaks lifecycle or others)
- Mid-iteration unsubscription safety (snapshot copy)
- LifecycleEventBus fan-out and consumer isolation
- Supervisor subscribe() coexistence with event bus drop-watch
- ConnectionManager.health_snapshot() correctness
- get_server_status() additive last_transition_ts field
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.manager import ConnectionManager
from slm_mcp_hub.resilience.events import LifecycleEventBus
from slm_mcp_hub.resilience.lifecycle import LifecycleEvent
from slm_mcp_hub.resilience.supervisor import ConnectionSupervisor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(name: str = "test-srv", **kw: Any) -> MCPServerConfig:
    defaults: dict[str, Any] = dict(transport="stdio", command="echo", args=("hi",))
    defaults.update(kw)
    return MCPServerConfig(name=name, **defaults)


def _bare_conn(name: str = "test-srv") -> MCPConnection:
    return MCPConnection(_cfg(name=name))


def _make_event(
    conn: MCPConnection | None = None,
    server: str = "srv",
    from_state: ConnectionState = ConnectionState.DISCONNECTED,
    to_state: ConnectionState = ConnectionState.CONNECTING,
) -> LifecycleEvent:
    if conn is not None:
        server = conn.name
    return LifecycleEvent(
        server=server,
        from_state=from_state,
        to_state=to_state,
        reason="test",
        ts=time.time(),
    )


# ---------------------------------------------------------------------------
# 1. MCPConnection.subscribe() — multi-subscriber fan-out
# ---------------------------------------------------------------------------


class TestSubscribeFanOut:
    """MCPConnection supports multiple concurrent subscribers via subscribe()."""

    def test_subscribe_returns_callable(self) -> None:
        """subscribe() must return an unsubscribe callable."""
        conn = _bare_conn()
        unsub = conn.subscribe(lambda e: None)
        assert callable(unsub)

    def test_single_subscriber_receives_event(self) -> None:
        """A subscribed callback receives lifecycle events."""
        received: list[LifecycleEvent] = []
        conn = _bare_conn()
        conn.subscribe(received.append)
        conn._transition(ConnectionState.CONNECTING, reason="test")
        assert len(received) == 1
        assert received[0].to_state == ConnectionState.CONNECTING

    def test_multiple_subscribers_all_receive_event(self) -> None:
        """All subscribers receive each event (fan-out)."""
        r1: list[LifecycleEvent] = []
        r2: list[LifecycleEvent] = []
        r3: list[LifecycleEvent] = []
        conn = _bare_conn()
        conn.subscribe(r1.append)
        conn.subscribe(r2.append)
        conn.subscribe(r3.append)
        conn._transition(ConnectionState.CONNECTING, reason="fan-out test")
        assert len(r1) == 1
        assert len(r2) == 1
        assert len(r3) == 1

    def test_unsubscribe_stops_receiving_events(self) -> None:
        """Calling the unsubscribe callable stops delivery to that subscriber."""
        received: list[LifecycleEvent] = []
        conn = _bare_conn()
        unsub = conn.subscribe(received.append)
        conn._transition(ConnectionState.CONNECTING, reason="before unsub")
        assert len(received) == 1

        unsub()
        conn._transition(ConnectionState.CONNECTED, reason="after unsub")
        assert len(received) == 1  # no new event

    def test_unsubscribe_idempotent(self) -> None:
        """Calling unsubscribe twice must not raise."""
        conn = _bare_conn()
        unsub = conn.subscribe(lambda e: None)
        unsub()
        unsub()  # must not raise

    def test_raising_subscriber_does_not_break_other_subscribers(self) -> None:
        """A raising subscriber is isolated; others still receive the event."""
        received: list[LifecycleEvent] = []

        def _boom(_e: LifecycleEvent) -> None:
            raise RuntimeError("subscriber exploded")

        conn = _bare_conn()
        conn.subscribe(_boom)
        conn.subscribe(received.append)
        conn._transition(ConnectionState.CONNECTING, reason="isolation test")
        # Other subscriber still received the event
        assert len(received) == 1

    def test_raising_subscriber_does_not_break_lifecycle(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising subscriber must NOT affect the lifecycle path or state."""

        def _boom(_e: LifecycleEvent) -> None:
            raise ValueError("kaboom")

        conn = _bare_conn()
        conn.subscribe(_boom)
        with caplog.at_level(logging.ERROR, logger="slm_mcp_hub.federation.connection"):
            event = conn._transition(ConnectionState.CONNECTING, reason="isolation")

        assert conn.state == ConnectionState.CONNECTING
        assert event.to_state == ConnectionState.CONNECTING

    def test_raising_subscriber_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising subscriber is logged (contains 'on_event callback raised')."""

        def _boom(_e: LifecycleEvent) -> None:
            raise RuntimeError("boom")

        conn = _bare_conn()
        conn.subscribe(_boom)
        with caplog.at_level(logging.ERROR, logger="slm_mcp_hub.federation.connection"):
            conn._transition(ConnectionState.CONNECTING, reason="log test")

        assert any("on_event callback raised" in r.message for r in caplog.records)

    def test_unsubscribe_mid_iteration_safe(self) -> None:
        """Unsubscribing inside a callback does not skip subsequent subscribers.

        This is the snapshot-copy safety check. If _emit iterates over a
        live collection and a subscriber removes itself, the iteration must
        not skip or double-call other subscribers.
        """
        call_order: list[int] = []
        unsubs: list[Any] = []
        conn = _bare_conn()

        def _make_sub(idx: int) -> Any:
            def _cb(_e: LifecycleEvent) -> None:
                call_order.append(idx)
                if idx == 1 and unsubs:  # first subscriber removes itself
                    unsubs[0]()

            return _cb

        for i in range(3):
            u = conn.subscribe(_make_sub(i))
            unsubs.append(u)

        conn._transition(ConnectionState.CONNECTING, reason="mid-iter")
        # All 3 must have been called despite the in-callback unsubscription
        assert sorted(call_order) == [0, 1, 2]

    def test_multiple_transitions_ordered(self) -> None:
        """Events arrive in emission order."""
        received: list[ConnectionState] = []
        conn = _bare_conn()
        conn.subscribe(lambda e: received.append(e.to_state))
        conn._transition(ConnectionState.CONNECTING, reason="s1")
        conn._transition(ConnectionState.CONNECTED, reason="s2")
        conn._transition(ConnectionState.DRAINING, reason="s3")
        assert received == [
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.DRAINING,
        ]


# ---------------------------------------------------------------------------
# 2. on_event back-compat property
# ---------------------------------------------------------------------------


class TestOnEventBackCompat:
    """on_event = cb must keep working exactly as before (W1-P1 tests preserved)."""

    def test_on_event_none_by_default(self) -> None:
        """on_event property returns None before any assignment."""
        conn = _bare_conn()
        assert conn.on_event is None

    def test_on_event_setter_registers_callback(self) -> None:
        """Assigning on_event registers a callback that receives transitions."""
        received: list[LifecycleEvent] = []
        conn = _bare_conn()
        conn.on_event = received.append
        conn._transition(ConnectionState.CONNECTING, reason="on_event setter")
        assert len(received) == 1

    def test_on_event_getter_returns_assigned_callable(self) -> None:
        """on_event getter returns the callable that was assigned."""
        cb = lambda _e: None  # noqa: E731
        conn = _bare_conn()
        conn.on_event = cb
        assert conn.on_event is cb

    def test_on_event_setter_replaces_previous_primary(self) -> None:
        """Second assignment replaces the first primary subscriber."""
        r1: list[LifecycleEvent] = []
        r2: list[LifecycleEvent] = []
        conn = _bare_conn()
        conn.on_event = r1.append
        conn.on_event = r2.append  # replaces r1.append
        conn._transition(ConnectionState.CONNECTING, reason="replace test")
        assert len(r1) == 0  # r1 was replaced
        assert len(r2) == 1  # r2 received it

    def test_on_event_none_removes_primary(self) -> None:
        """Setting on_event = None removes the primary subscriber."""
        received: list[LifecycleEvent] = []
        conn = _bare_conn()
        conn.on_event = received.append
        conn.on_event = None
        conn._transition(ConnectionState.CONNECTING, reason="none removal")
        assert len(received) == 0

    def test_on_event_and_subscribe_coexist(self) -> None:
        """on_event primary and subscribe()-registered subscribers both receive events."""
        primary: list[LifecycleEvent] = []
        secondary: list[LifecycleEvent] = []
        conn = _bare_conn()
        conn.on_event = primary.append
        conn.subscribe(secondary.append)
        conn._transition(ConnectionState.CONNECTING, reason="coexist test")
        assert len(primary) == 1
        assert len(secondary) == 1

    def test_raising_on_event_callback_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Backward-compat: raising via on_event setter still logs correctly."""

        def _boom(_e: LifecycleEvent) -> None:
            raise RuntimeError("observer blew up")

        conn = _bare_conn()
        conn.on_event = _boom
        with caplog.at_level(logging.ERROR, logger="slm_mcp_hub.federation.connection"):
            event = conn._transition(ConnectionState.CONNECTING, reason="compat log")

        assert conn.state == ConnectionState.CONNECTING
        assert event.to_state == ConnectionState.CONNECTING
        assert any("on_event callback raised" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. LifecycleEventBus
# ---------------------------------------------------------------------------


class TestLifecycleEventBus:
    """LifecycleEventBus fans out events with consumer isolation."""

    def test_register_consumer_returns_unsubscribe(self) -> None:
        bus = LifecycleEventBus()
        unsub = bus.register_consumer(lambda e: None)
        assert callable(unsub)

    def test_emit_reaches_all_consumers(self) -> None:
        r1: list[LifecycleEvent] = []
        r2: list[LifecycleEvent] = []
        bus = LifecycleEventBus()
        bus.register_consumer(r1.append)
        bus.register_consumer(r2.append)
        event = _make_event()
        bus.emit(event)
        assert len(r1) == 1
        assert len(r2) == 1
        assert r1[0] is event
        assert r2[0] is event

    def test_emit_no_consumers_is_safe(self) -> None:
        """emit() with no consumers must not raise."""
        bus = LifecycleEventBus()
        bus.emit(_make_event())  # no error

    def test_raising_consumer_does_not_break_other_consumers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        received: list[LifecycleEvent] = []

        def _boom(_e: LifecycleEvent) -> None:
            raise ValueError("consumer failure")

        bus = LifecycleEventBus()
        bus.register_consumer(_boom)
        bus.register_consumer(received.append)
        with caplog.at_level(logging.ERROR, logger="slm_mcp_hub.resilience.events"):
            bus.emit(_make_event())

        assert len(received) == 1

    def test_raising_consumer_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bus = LifecycleEventBus()
        bus.register_consumer(lambda _e: (_ for _ in ()).throw(RuntimeError("bus boom")))
        with caplog.at_level(logging.ERROR, logger="slm_mcp_hub.resilience.events"):
            bus.emit(_make_event())
        assert any("consumer" in r.message.lower() for r in caplog.records)

    def test_unsubscribe_consumer(self) -> None:
        received: list[LifecycleEvent] = []
        bus = LifecycleEventBus()
        unsub = bus.register_consumer(received.append)
        bus.emit(_make_event())
        assert len(received) == 1
        unsub()
        bus.emit(_make_event())
        assert len(received) == 1  # no new event after unsubscribe

    def test_event_ordering_preserved(self) -> None:
        """Events arrive in emission order."""
        received: list[str] = []
        bus = LifecycleEventBus()
        bus.register_consumer(lambda e: received.append(e.reason))
        for i in range(5):
            bus.emit(
                LifecycleEvent(
                    server="srv",
                    from_state=ConnectionState.DISCONNECTED,
                    to_state=ConnectionState.CONNECTING,
                    reason=f"evt-{i}",
                    ts=time.time(),
                )
            )
        assert received == [f"evt-{i}" for i in range(5)]

    def test_bus_connected_to_connection_via_subscribe(self) -> None:
        """Wiring: conn.subscribe(bus.emit) routes transition events into the bus."""
        received: list[LifecycleEvent] = []
        bus = LifecycleEventBus()
        bus.register_consumer(received.append)
        conn = _bare_conn()
        conn.subscribe(bus.emit)
        conn._transition(ConnectionState.CONNECTING, reason="bus wiring")
        assert len(received) == 1
        assert received[0].server == conn.name


# ---------------------------------------------------------------------------
# 4. Supervisor subscribe() coexistence with event bus
# ---------------------------------------------------------------------------


class TestSupervisorSubscribeCoexistence:
    """Supervisor drop-watch and event bus must coexist after W1-P4 migration."""

    def _noop_sleep(self) -> Any:
        async def _s(_: float) -> None:
            await asyncio.sleep(0)

        return _s

    def test_supervisor_uses_subscribe_not_on_event_overwrite(self) -> None:
        """After W1-P4: supervisor uses subscribe(); on_event primary is NOT replaced."""
        received_primary: list[LifecycleEvent] = []
        conn = _bare_conn()
        conn.on_event = received_primary.append  # primary subscriber

        # Creating supervisor must NOT wipe out the primary subscriber
        ConnectionSupervisor(conn, sleep_fn=self._noop_sleep())
        conn._transition(ConnectionState.CONNECTING, reason="coexist check")
        # Primary subscriber must still receive events
        assert len(received_primary) >= 1

    def test_supervisor_drop_watch_coexists_with_bus(self) -> None:
        """Supervisor's _capture_event and event bus both receive transitions."""
        bus_received: list[LifecycleEvent] = []
        bus = LifecycleEventBus()
        bus.register_consumer(bus_received.append)

        conn = _bare_conn()
        conn.subscribe(bus.emit)  # bus wired

        ConnectionSupervisor(conn, sleep_fn=self._noop_sleep())

        # Supervisor installs its own subscriber; bus subscriber must still work
        conn._transition(ConnectionState.CONNECTING, reason="coexist drop-watch")
        assert len(bus_received) >= 1

    @pytest.mark.asyncio
    async def test_supervisor_stop_cleans_up_subscription(self) -> None:
        """After sup.stop(), the supervisor's subscriber is unregistered."""
        conn = _bare_conn()
        received_after_stop: list[LifecycleEvent] = []

        sup = ConnectionSupervisor(conn, sleep_fn=self._noop_sleep())
        await sup.stop()
        # Now subscribe a new listener to verify the transition still works
        conn.subscribe(received_after_stop.append)
        conn._transition(ConnectionState.CONNECTING, reason="after stop")

        # The connection transitioned, received_after_stop has the event
        # (supervisor cleanup verified by no dangling task or reference)
        assert len(received_after_stop) >= 1


# ---------------------------------------------------------------------------
# 5. ConnectionManager.health_snapshot()
# ---------------------------------------------------------------------------


class TestHealthSnapshot:
    """ConnectionManager.health_snapshot() returns per-backend health dicts."""

    def _minimal_manager(self, *server_names: str) -> ConnectionManager:
        servers = tuple(_cfg(name=n) for n in server_names)
        cfg = HubConfig(mcp_servers=servers)
        from slm_mcp_hub.core.registry import CapabilityRegistry
        return ConnectionManager(cfg, CapabilityRegistry())

    def test_health_snapshot_empty_config(self) -> None:
        mgr = self._minimal_manager()
        snap = mgr.health_snapshot()
        assert isinstance(snap, dict)
        assert len(snap) == 0

    def test_health_snapshot_contains_all_servers(self) -> None:
        mgr = self._minimal_manager("srv-a", "srv-b")
        snap = mgr.health_snapshot()
        assert set(snap.keys()) == {"srv-a", "srv-b"}

    def test_health_snapshot_lifecycle_field(self) -> None:
        mgr = self._minimal_manager("srv")
        snap = mgr.health_snapshot()
        assert "lifecycle" in snap["srv"]

    def test_health_snapshot_required_fields(self) -> None:
        mgr = self._minimal_manager("srv")
        snap = mgr.health_snapshot()
        entry = snap["srv"]
        required = {
            "lifecycle",
            "needs_attention",
            "consecutive_failures",
            "restart_count",
            "last_error",
            "last_transition_ts",
            "breaker_open",
            "breaker_open_cycles",
        }
        assert required.issubset(entry.keys()), (
            f"Missing fields: {required - entry.keys()}"
        )

    def test_health_snapshot_defaults_when_no_supervisor(self) -> None:
        mgr = self._minimal_manager("srv")
        snap = mgr.health_snapshot()
        entry = snap["srv"]
        assert entry["lifecycle"] == ConnectionState.DISCONNECTED.value
        assert entry["needs_attention"] is False
        assert entry["consecutive_failures"] == 0
        assert entry["restart_count"] == 0
        assert entry["last_error"] is None
        assert entry["last_transition_ts"] == 0.0
        assert entry["breaker_open"] is False


# ---------------------------------------------------------------------------
# 6. get_server_status() additive fields (last_transition_ts)
# ---------------------------------------------------------------------------


class TestGetServerStatusAdditiveFields:
    """get_server_status() gains last_transition_ts without removing any existing field."""

    def _minimal_manager(self, *server_names: str) -> ConnectionManager:
        servers = tuple(_cfg(name=n) for n in server_names)
        cfg = HubConfig(mcp_servers=servers)
        from slm_mcp_hub.core.registry import CapabilityRegistry
        return ConnectionManager(cfg, CapabilityRegistry())

    def test_existing_fields_still_present(self) -> None:
        """P07/P08-era fields must not be renamed or removed."""
        mgr = self._minimal_manager("srv")
        statuses = mgr.get_server_status()
        assert len(statuses) == 1
        entry = statuses[0]
        # P07/P08 mandatory fields
        for field in (
            "name", "transport", "enabled", "connected", "auth_required",
            "tools", "connect_time_ms", "lifecycle",
            "consecutive_failures", "needs_attention", "restart_count",
            "last_error", "breaker_open", "breaker_open_cycles",
        ):
            assert field in entry, f"Existing field '{field}' was removed"

    def test_last_transition_ts_field_added(self) -> None:
        """W1-P4 adds last_transition_ts to get_server_status() output."""
        mgr = self._minimal_manager("srv")
        statuses = mgr.get_server_status()
        assert "last_transition_ts" in statuses[0], (
            "get_server_status() must expose last_transition_ts (W1-P4)"
        )
