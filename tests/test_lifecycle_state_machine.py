"""W1-P1 — Lifecycle state machine core tests.

Covers:
- ConnectionState extension with new lifecycle states (additive)
- LifecycleEvent frozen dataclass
- LIFECYCLE_TRANSITIONS table + is_valid_transition() helper
- MCPConnection._transition() single mutation point + event emission

Backward-compatibility and integration tests live in test_lifecycle_compat.py.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.resilience.lifecycle import (
    LIFECYCLE_TRANSITIONS,
    LifecycleEvent,
    is_valid_transition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**kw: Any) -> MCPServerConfig:
    defaults: dict[str, Any] = dict(
        name="test-srv", transport="stdio", command="echo", args=("hi",)
    )
    defaults.update(kw)
    return MCPServerConfig(**defaults)


# ---------------------------------------------------------------------------
# 1. ConnectionState extension — additive, legacy preserved
# ---------------------------------------------------------------------------


class TestConnectionStateExtension:
    """New states are additive; all legacy values must remain untouched."""

    # Legacy states (must stay exactly as-is)
    def test_legacy_disconnected_exists(self) -> None:
        assert ConnectionState.DISCONNECTED.value == "disconnected"

    def test_legacy_connecting_exists(self) -> None:
        assert ConnectionState.CONNECTING.value == "connecting"

    def test_legacy_connected_exists(self) -> None:
        assert ConnectionState.CONNECTED.value == "connected"

    def test_legacy_draining_exists(self) -> None:
        assert ConnectionState.DRAINING.value == "draining"

    def test_legacy_error_exists(self) -> None:
        assert ConnectionState.ERROR.value == "error"

    def test_legacy_auth_required_exists(self) -> None:
        assert ConnectionState.AUTH_REQUIRED.value == "auth_required"

    # New lifecycle states (from LLD §2)
    def test_new_state_starting(self) -> None:
        assert ConnectionState.STARTING.value == "starting"

    def test_new_state_initializing(self) -> None:
        assert ConnectionState.INITIALIZING.value == "initializing"

    def test_new_state_ready(self) -> None:
        assert ConnectionState.READY.value == "ready"

    def test_new_state_degraded(self) -> None:
        assert ConnectionState.DEGRADED.value == "degraded"

    def test_new_state_reconnecting(self) -> None:
        assert ConnectionState.RECONNECTING.value == "reconnecting"

    def test_new_state_circuit_open(self) -> None:
        assert ConnectionState.CIRCUIT_OPEN.value == "circuit_open"

    def test_new_state_stopped(self) -> None:
        assert ConnectionState.STOPPED.value == "stopped"

    def test_new_state_failed(self) -> None:
        assert ConnectionState.FAILED.value == "failed"

    def test_all_states_are_str(self) -> None:
        for state in ConnectionState:
            assert isinstance(state.value, str), f"{state} should have str value"

    def test_total_state_count(self) -> None:
        """14 states total: 6 legacy + 8 new."""
        assert len(ConnectionState) == 14

    def test_str_enum_coercion(self) -> None:
        """ConnectionState extends str — enum members compare equal to their values."""
        assert ConnectionState.CONNECTED == "connected"
        assert ConnectionState.STARTING == "starting"


# ---------------------------------------------------------------------------
# 2. LifecycleEvent — frozen + correct fields
# ---------------------------------------------------------------------------


class TestLifecycleEvent:
    """LifecycleEvent must be an immutable frozen dataclass."""

    def _make_event(self, **overrides: Any) -> LifecycleEvent:
        defaults: dict[str, Any] = dict(
            server="srv1",
            from_state=ConnectionState.DISCONNECTED,
            to_state=ConnectionState.CONNECTING,
            reason="connect() called",
            ts=time.time(),
        )
        defaults.update(overrides)
        return LifecycleEvent(**defaults)

    def test_event_is_frozen(self) -> None:
        evt = self._make_event()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            evt.server = "other"  # type: ignore[misc]

    def test_event_to_state_immutable(self) -> None:
        evt = self._make_event()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            evt.to_state = ConnectionState.CONNECTED  # type: ignore[misc]

    def test_event_server_field(self) -> None:
        assert self._make_event(server="alpha").server == "alpha"

    def test_event_from_to_state(self) -> None:
        evt = self._make_event(
            from_state=ConnectionState.CONNECTING,
            to_state=ConnectionState.CONNECTED,
        )
        assert evt.from_state == ConnectionState.CONNECTING
        assert evt.to_state == ConnectionState.CONNECTED

    def test_event_reason(self) -> None:
        assert self._make_event(reason="explicit reconnect").reason == "explicit reconnect"

    def test_event_ts_is_float(self) -> None:
        evt = self._make_event(ts=1_700_000_000.5)
        assert isinstance(evt.ts, float)
        assert evt.ts == pytest.approx(1_700_000_000.5)

    def test_event_failure_class_defaults_none(self) -> None:
        assert self._make_event().failure_class is None

    def test_event_attempt_defaults_none(self) -> None:
        assert self._make_event().attempt is None

    def test_event_failure_class_set(self) -> None:
        assert self._make_event(failure_class="TRANSIENT").failure_class == "TRANSIENT"

    def test_event_attempt_set(self) -> None:
        assert self._make_event(attempt=3).attempt == 3

    def test_event_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(self._make_event())

    def test_event_equality(self) -> None:
        ts = time.time()
        a = LifecycleEvent(
            server="s",
            from_state=ConnectionState.DISCONNECTED,
            to_state=ConnectionState.CONNECTING,
            reason="r",
            ts=ts,
        )
        b = LifecycleEvent(
            server="s",
            from_state=ConnectionState.DISCONNECTED,
            to_state=ConnectionState.CONNECTING,
            reason="r",
            ts=ts,
        )
        assert a == b


# ---------------------------------------------------------------------------
# 3. LIFECYCLE_TRANSITIONS table + is_valid_transition()
# ---------------------------------------------------------------------------


class TestLifecycleTransitionsTable:
    """LIFECYCLE_TRANSITIONS documents valid edges; is_valid_transition() queries it."""

    def test_transitions_is_collection(self) -> None:
        assert isinstance(LIFECYCLE_TRANSITIONS, (set, frozenset, dict, list, tuple))

    def test_is_valid_transition_returns_bool(self) -> None:
        result = is_valid_transition(
            ConnectionState.DISCONNECTED, ConnectionState.CONNECTING
        )
        assert isinstance(result, bool)

    # --- Legacy designed edges ---

    def test_valid_disconnected_to_connecting(self) -> None:
        assert is_valid_transition(ConnectionState.DISCONNECTED, ConnectionState.CONNECTING)

    def test_valid_connecting_to_connected(self) -> None:
        assert is_valid_transition(ConnectionState.CONNECTING, ConnectionState.CONNECTED)

    def test_valid_connecting_to_error(self) -> None:
        assert is_valid_transition(ConnectionState.CONNECTING, ConnectionState.ERROR)

    def test_valid_connecting_to_auth_required(self) -> None:
        assert is_valid_transition(ConnectionState.CONNECTING, ConnectionState.AUTH_REQUIRED)

    def test_valid_connected_to_draining(self) -> None:
        assert is_valid_transition(ConnectionState.CONNECTED, ConnectionState.DRAINING)

    def test_valid_connected_to_disconnected(self) -> None:
        assert is_valid_transition(ConnectionState.CONNECTED, ConnectionState.DISCONNECTED)

    def test_valid_draining_to_disconnected(self) -> None:
        assert is_valid_transition(ConnectionState.DRAINING, ConnectionState.DISCONNECTED)

    def test_valid_error_to_disconnected(self) -> None:
        assert is_valid_transition(ConnectionState.ERROR, ConnectionState.DISCONNECTED)

    def test_valid_auth_required_to_disconnected(self) -> None:
        assert is_valid_transition(ConnectionState.AUTH_REQUIRED, ConnectionState.DISCONNECTED)

    # --- New lifecycle designed edges (LLD §2) ---

    def test_valid_disconnected_to_starting(self) -> None:
        assert is_valid_transition(ConnectionState.DISCONNECTED, ConnectionState.STARTING)

    def test_valid_starting_to_initializing(self) -> None:
        assert is_valid_transition(ConnectionState.STARTING, ConnectionState.INITIALIZING)

    def test_valid_initializing_to_ready(self) -> None:
        assert is_valid_transition(ConnectionState.INITIALIZING, ConnectionState.READY)

    def test_valid_ready_to_degraded(self) -> None:
        assert is_valid_transition(ConnectionState.READY, ConnectionState.DEGRADED)

    def test_valid_degraded_to_ready(self) -> None:
        assert is_valid_transition(ConnectionState.DEGRADED, ConnectionState.READY)

    def test_valid_ready_to_reconnecting(self) -> None:
        assert is_valid_transition(ConnectionState.READY, ConnectionState.RECONNECTING)

    def test_valid_reconnecting_to_initializing(self) -> None:
        assert is_valid_transition(ConnectionState.RECONNECTING, ConnectionState.INITIALIZING)

    def test_valid_reconnecting_to_circuit_open(self) -> None:
        assert is_valid_transition(ConnectionState.RECONNECTING, ConnectionState.CIRCUIT_OPEN)

    def test_valid_circuit_open_to_reconnecting(self) -> None:
        assert is_valid_transition(ConnectionState.CIRCUIT_OPEN, ConnectionState.RECONNECTING)

    def test_valid_ready_to_draining(self) -> None:
        assert is_valid_transition(ConnectionState.READY, ConnectionState.DRAINING)

    def test_valid_draining_to_stopped(self) -> None:
        assert is_valid_transition(ConnectionState.DRAINING, ConnectionState.STOPPED)

    def test_valid_any_to_failed(self) -> None:
        """Any live state can transition to FAILED (terminal)."""
        for state in (
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.READY,
            ConnectionState.DEGRADED,
            ConnectionState.RECONNECTING,
            ConnectionState.CIRCUIT_OPEN,
        ):
            assert is_valid_transition(state, ConnectionState.FAILED), (
                f"{state} -> FAILED should be valid"
            )

    def test_valid_any_to_auth_required(self) -> None:
        """Auth required can be signalled from connecting or connected states."""
        for state in (
            ConnectionState.CONNECTING,
            ConnectionState.STARTING,
            ConnectionState.INITIALIZING,
            ConnectionState.READY,
        ):
            assert is_valid_transition(state, ConnectionState.AUTH_REQUIRED), (
                f"{state} -> AUTH_REQUIRED should be valid"
            )

    # --- Invalid transitions ---

    def test_invalid_disconnected_to_connected_direct(self) -> None:
        """Must go through CONNECTING/STARTING first."""
        assert not is_valid_transition(
            ConnectionState.DISCONNECTED, ConnectionState.CONNECTED
        )

    def test_invalid_connected_to_error(self) -> None:
        """CONNECTED -> ERROR is not in the designed set (use RECONNECTING)."""
        assert not is_valid_transition(ConnectionState.CONNECTED, ConnectionState.ERROR)

    def test_invalid_failed_to_anything(self) -> None:
        """FAILED is terminal — no outgoing transitions."""
        assert not is_valid_transition(
            ConnectionState.FAILED, ConnectionState.RECONNECTING
        )

    def test_invalid_stopped_to_connecting(self) -> None:
        """STOPPED requires admin action — no automatic reconnect."""
        assert not is_valid_transition(
            ConnectionState.STOPPED, ConnectionState.CONNECTING
        )


# ---------------------------------------------------------------------------
# 4. MCPConnection._transition() — single state mutation point
# ---------------------------------------------------------------------------


class TestTransitionMethod:
    """_transition() must be the single place that mutates _state."""

    def _conn(self, **kw: Any) -> MCPConnection:
        return MCPConnection(_cfg(**kw))

    def test_transition_changes_state(self) -> None:
        c = self._conn()
        assert c.state == ConnectionState.DISCONNECTED
        c._transition(ConnectionState.CONNECTING, reason="test")
        assert c.state == ConnectionState.CONNECTING

    def test_transition_updates_to_any_valid_state(self) -> None:
        c = self._conn()
        c._transition(ConnectionState.STARTING, reason="supervisor start")
        assert c.state == ConnectionState.STARTING

    def test_transition_emits_event_to_callback(self) -> None:
        received: list[LifecycleEvent] = []
        c = self._conn()
        c.on_event = received.append
        c._transition(ConnectionState.CONNECTING, reason="connect called")
        assert len(received) == 1
        assert isinstance(received[0], LifecycleEvent)

    def test_transition_event_server_name(self) -> None:
        received: list[LifecycleEvent] = []
        c = MCPConnection(_cfg(name="my-server"))
        c.on_event = received.append
        c._transition(ConnectionState.CONNECTING, reason="r")
        assert received[0].server == "my-server"

    def test_transition_event_from_state(self) -> None:
        received: list[LifecycleEvent] = []
        c = self._conn()
        c.on_event = received.append
        c._transition(ConnectionState.CONNECTING, reason="r")
        assert received[0].from_state == ConnectionState.DISCONNECTED

    def test_transition_event_to_state(self) -> None:
        received: list[LifecycleEvent] = []
        c = self._conn()
        c.on_event = received.append
        c._transition(ConnectionState.CONNECTING, reason="r")
        assert received[0].to_state == ConnectionState.CONNECTING

    def test_transition_event_reason(self) -> None:
        received: list[LifecycleEvent] = []
        c = self._conn()
        c.on_event = received.append
        c._transition(ConnectionState.CONNECTING, reason="explicit reason")
        assert received[0].reason == "explicit reason"

    def test_transition_event_ts_is_recent_float(self) -> None:
        received: list[LifecycleEvent] = []
        c = self._conn()
        c.on_event = received.append
        before = time.time()
        c._transition(ConnectionState.CONNECTING, reason="r")
        after = time.time()
        ts = received[0].ts
        assert isinstance(ts, float)
        assert before <= ts <= after

    def test_transition_event_failure_class_forwarded(self) -> None:
        received: list[LifecycleEvent] = []
        c = self._conn()
        c.on_event = received.append
        c._transition(ConnectionState.ERROR, reason="auth error", failure_class="AUTH")
        assert received[0].failure_class == "AUTH"

    def test_transition_event_attempt_forwarded(self) -> None:
        received: list[LifecycleEvent] = []
        c = self._conn()
        c.on_event = received.append
        c._transition(ConnectionState.RECONNECTING, reason="retry 2", attempt=2)
        assert received[0].attempt == 2

    def test_transition_invalid_logs_warning_not_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unexpected edges log a warning; state still changes (fail-open)."""
        c = self._conn()
        # DISCONNECTED -> CONNECTED directly is not a designed edge.
        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.federation.connection"):
            c._transition(ConnectionState.CONNECTED, reason="forced by test")

        assert c.state == ConnectionState.CONNECTED
        assert any(
            "unexpected" in r.message.lower() or "invalid" in r.message.lower()
            for r in caplog.records
        ), "Expected a warning about an unexpected/invalid transition"

    def test_transition_no_callback_is_noop(self) -> None:
        """If on_event is not set, _transition must not raise."""
        c = self._conn()
        c._transition(ConnectionState.CONNECTING, reason="no callback")
        assert c.state == ConnectionState.CONNECTING

    def test_transition_default_callback_is_noop(self) -> None:
        """MCPConnection.on_event defaults to None; _transition works fine."""
        c = self._conn()
        c._transition(ConnectionState.CONNECTING, reason="default callback test")
        assert c.state == ConnectionState.CONNECTING

    def test_transition_isolates_raising_callback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising on_event must NEVER break the lifecycle path.

        Regression: a flaky observer (a future event-bus /
        webhook dispatcher) must not turn a successful connect/disconnect into a
        raise or corrupt the state we just set. The exception is swallowed +
        logged; the state still changes and the event is still returned.
        """

        def _boom(_event: LifecycleEvent) -> None:
            raise RuntimeError("observer blew up")

        c = self._conn()
        c.on_event = _boom
        with caplog.at_level(logging.ERROR, logger="slm_mcp_hub.federation.connection"):
            event = c._transition(ConnectionState.CONNECTING, reason="observer isolation")

        assert c.state == ConnectionState.CONNECTING
        assert event.to_state == ConnectionState.CONNECTING
        assert any("on_event callback raised" in r.message for r in caplog.records), (
            "expected the raising callback to be logged, not propagated"
        )

    def test_transition_sequence_emits_multiple_events(self) -> None:
        received: list[LifecycleEvent] = []
        c = self._conn()
        c.on_event = received.append

        c._transition(ConnectionState.CONNECTING, reason="step1")
        c._transition(ConnectionState.CONNECTED, reason="step2")
        c._transition(ConnectionState.DRAINING, reason="step3")

        assert len(received) == 3
        assert received[0].from_state == ConnectionState.DISCONNECTED
        assert received[0].to_state == ConnectionState.CONNECTING
        assert received[1].from_state == ConnectionState.CONNECTING
        assert received[1].to_state == ConnectionState.CONNECTED
        assert received[2].from_state == ConnectionState.CONNECTED
        assert received[2].to_state == ConnectionState.DRAINING
