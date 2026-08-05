"""W1-P1 — Lifecycle backward-compatibility and integration tests.

Covers:
- Legacy property compatibility: is_connected, is_auth_required, is_draining
- connect/disconnect/drain use _transition internally
- ConnectionManager.get_server_status() lifecycle field (additive)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.manager import ConnectionManager


def _cfg(**kw: Any) -> MCPServerConfig:
    defaults: dict[str, Any] = dict(
        name="test-srv", transport="stdio", command="echo", args=("hi",)
    )
    defaults.update(kw)
    return MCPServerConfig(**defaults)


# ---------------------------------------------------------------------------
# Legacy compatibility — is_connected, is_auth_required, state enum
# ---------------------------------------------------------------------------


class TestLegacyCompatibility:
    """All existing observable properties must behave exactly as before."""

    def _conn(self) -> MCPConnection:
        return MCPConnection(_cfg())

    def test_is_connected_true_after_transition_to_connected(self) -> None:
        c = self._conn()
        c._transition(ConnectionState.CONNECTING, reason="r")
        c._transition(ConnectionState.CONNECTED, reason="r")
        assert c.is_connected is True

    def test_is_connected_false_in_draining(self) -> None:
        c = self._conn()
        c._transition(ConnectionState.CONNECTING, reason="r")
        c._transition(ConnectionState.CONNECTED, reason="r")
        c._transition(ConnectionState.DRAINING, reason="drain")
        assert c.is_connected is False

    def test_is_connected_false_in_starting(self) -> None:
        c = self._conn()
        c._transition(ConnectionState.STARTING, reason="r")
        assert c.is_connected is False

    def test_is_connected_false_in_ready(self) -> None:
        """READY is a new state; is_connected still checks CONNECTED only."""
        c = self._conn()
        c._transition(ConnectionState.STARTING, reason="r")
        c._transition(ConnectionState.INITIALIZING, reason="r")
        c._transition(ConnectionState.READY, reason="r")
        assert c.is_connected is False  # READY != CONNECTED

    def test_is_auth_required_true_after_transition(self) -> None:
        c = self._conn()
        c._transition(ConnectionState.CONNECTING, reason="r")
        c._transition(ConnectionState.AUTH_REQUIRED, reason="oauth")
        assert c.is_auth_required is True

    def test_is_auth_required_false_when_connected(self) -> None:
        c = self._conn()
        c._transition(ConnectionState.CONNECTING, reason="r")
        c._transition(ConnectionState.CONNECTED, reason="r")
        assert c.is_auth_required is False

    def test_is_draining_true_after_draining_transition(self) -> None:
        c = self._conn()
        c._transition(ConnectionState.CONNECTING, reason="r")
        c._transition(ConnectionState.CONNECTED, reason="r")
        c._transition(ConnectionState.DRAINING, reason="drain")
        assert c.is_draining is True

    def test_state_enum_value_returned_by_state_property(self) -> None:
        c = self._conn()
        c._transition(ConnectionState.CONNECTING, reason="r")
        assert c.state == ConnectionState.CONNECTING
        assert c.state == "connecting"  # str-mixin equality

    @pytest.mark.asyncio
    async def test_connect_uses_transition_internally(self) -> None:
        """connect() must route through _transition — state changes observed via property."""
        transitions: list[tuple[str, str]] = []
        c = MCPConnection(_cfg())
        original_transition = c._transition

        def record_transition(to_state: ConnectionState, reason: str, **kw: Any) -> None:
            transitions.append((c.state.value, to_state.value))
            original_transition(to_state, reason, **kw)

        c._transition = record_transition  # type: ignore[method-assign]

        mock_outbound = MagicMock()
        mock_outbound.connect = AsyncMock()
        mock_outbound.capabilities = {
            "tools": [], "resources": [], "resource_templates": [], "prompts": []
        }
        mock_outbound.negotiated_peer = None

        with patch("slm_mcp_hub.federation.connection.OutboundClient", return_value=mock_outbound):
            await c.connect()

        to_states = [t[1] for t in transitions]
        assert "connecting" in to_states, f"Expected 'connecting' in {to_states}"
        assert "connected" in to_states, f"Expected 'connected' in {to_states}"

    @pytest.mark.asyncio
    async def test_disconnect_uses_transition_internally(self) -> None:
        transitions: list[str] = []
        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        original_transition = c._transition

        def record_transition(to_state: ConnectionState, reason: str, **kw: Any) -> None:
            transitions.append(to_state.value)
            original_transition(to_state, reason, **kw)

        c._transition = record_transition  # type: ignore[method-assign]
        await c.disconnect()
        assert "disconnected" in transitions

    @pytest.mark.asyncio
    async def test_drain_and_disconnect_uses_transition_internally(self) -> None:
        transitions: list[str] = []
        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        original_transition = c._transition

        def record_transition(to_state: ConnectionState, reason: str, **kw: Any) -> None:
            transitions.append(to_state.value)
            original_transition(to_state, reason, **kw)

        c._transition = record_transition  # type: ignore[method-assign]
        await c.drain_and_disconnect()
        assert "draining" in transitions

    @pytest.mark.asyncio
    async def test_connect_error_sets_error_state_via_transition(self) -> None:
        """connect() still sets ERROR state on failure."""
        c = MCPConnection(_cfg())
        mock_outbound = MagicMock()
        mock_outbound.connect = AsyncMock(side_effect=ConnectionError("boom"))

        with patch("slm_mcp_hub.federation.connection.OutboundClient", return_value=mock_outbound):
            with pytest.raises(ConnectionError):
                await c.connect()

        assert c.state == ConnectionState.ERROR

    @pytest.mark.asyncio
    async def test_connect_auth_required_sets_auth_state_via_transition(self) -> None:
        """connect() still sets AUTH_REQUIRED on OAuthAuthRequiredError."""
        from slm_mcp_hub.auth.broker import OAuthAuthRequiredError

        c = MCPConnection(_cfg())
        mock_outbound = MagicMock()
        mock_outbound.connect = AsyncMock(
            side_effect=OAuthAuthRequiredError("auth needed")
        )

        with patch("slm_mcp_hub.federation.connection.OutboundClient", return_value=mock_outbound):
            await c.connect()  # Does NOT raise — auth_required is expected

        assert c.state == ConnectionState.AUTH_REQUIRED
        assert c.is_auth_required is True

    def test_direct_state_write_still_works_for_test_setup(self) -> None:
        """Existing tests that set _state directly for setup must still work."""
        c = self._conn()
        c._state = ConnectionState.DRAINING
        assert c.is_draining is True
        c._state = ConnectionState.CONNECTED
        assert c.is_connected is True


# ---------------------------------------------------------------------------
# get_server_status() — lifecycle field additive
# ---------------------------------------------------------------------------


class TestGetServerStatusLifecycleField:
    """lifecycle field is added to each entry; existing fields are unchanged."""

    def _manager_with_conn(
        self, server_name: str, state: ConnectionState
    ) -> ConnectionManager:
        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name=server_name,
                    transport="stdio",
                    command="echo",
                    args=(server_name,),
                ),
            )
        )
        mgr = ConnectionManager(cfg, CapabilityRegistry())
        mock_conn = MagicMock()
        mock_conn.is_connected = (state == ConnectionState.CONNECTED)
        mock_conn.is_auth_required = (state == ConnectionState.AUTH_REQUIRED)
        mock_conn.state = state
        mock_conn.capabilities = {
            "tools": [{"name": "t1"}],
            "resources": [],
            "prompts": [],
        }
        mgr._connections[server_name] = mock_conn
        return mgr

    def test_lifecycle_field_present(self) -> None:
        mgr = self._manager_with_conn("srv", ConnectionState.CONNECTED)
        status = mgr.get_server_status()
        assert len(status) == 1
        assert "lifecycle" in status[0], "lifecycle field missing from status entry"

    def test_lifecycle_field_value_matches_state(self) -> None:
        for state in (
            ConnectionState.CONNECTED,
            ConnectionState.DISCONNECTED,
            ConnectionState.ERROR,
            ConnectionState.DRAINING,
            ConnectionState.AUTH_REQUIRED,
            ConnectionState.STARTING,
            ConnectionState.INITIALIZING,
            ConnectionState.READY,
            ConnectionState.RECONNECTING,
            ConnectionState.CIRCUIT_OPEN,
            ConnectionState.FAILED,
        ):
            mgr = self._manager_with_conn("srv", state)
            status = mgr.get_server_status()
            assert status[0]["lifecycle"] == state.value, (
                f"lifecycle field should be '{state.value}' for state {state}"
            )

    def test_lifecycle_field_when_no_connection(self) -> None:
        """Server not yet connected: lifecycle should be 'disconnected'."""
        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name="unconnected",
                    transport="stdio",
                    command="echo",
                ),
            )
        )
        mgr = ConnectionManager(cfg, CapabilityRegistry())
        status = mgr.get_server_status()
        assert status[0]["lifecycle"] == ConnectionState.DISCONNECTED.value

    def test_existing_fields_still_present(self) -> None:
        """No existing field is renamed or removed."""
        mgr = self._manager_with_conn("srv", ConnectionState.CONNECTED)
        entry = mgr.get_server_status()[0]
        required = {"name", "transport", "enabled", "connected",
                    "auth_required", "tools", "connect_time_ms"}
        for field in required:
            assert field in entry, f"Legacy field '{field}' missing from status"

    def test_connected_field_correct_when_connected(self) -> None:
        mgr = self._manager_with_conn("srv", ConnectionState.CONNECTED)
        assert mgr.get_server_status()[0]["connected"] is True

    def test_connected_field_false_for_new_states(self) -> None:
        """New states like READY/STARTING do not set connected=True."""
        for state in (
            ConnectionState.STARTING,
            ConnectionState.INITIALIZING,
            ConnectionState.READY,
        ):
            mgr = self._manager_with_conn("srv", state)
            assert mgr.get_server_status()[0]["connected"] is False, (
                f"connected should be False for state {state}"
            )

    def test_auth_required_field_unchanged(self) -> None:
        mgr = self._manager_with_conn("srv", ConnectionState.AUTH_REQUIRED)
        assert mgr.get_server_status()[0]["auth_required"] is True

    def test_next_action_still_present_for_auth_required(self) -> None:
        """next_action field still appears for auth_required servers (P07 compat)."""
        mgr = self._manager_with_conn("srv", ConnectionState.AUTH_REQUIRED)
        status = mgr.get_server_status()[0]
        assert "next_action" in status
        assert "slm-hub auth login srv" in status["next_action"]

    def test_error_field_present_when_failed(self) -> None:
        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name="bad-srv", transport="stdio", command="echo",
                ),
            )
        )
        mgr = ConnectionManager(cfg, CapabilityRegistry())
        mgr._failed["bad-srv"] = "connection refused"
        assert mgr.get_server_status()[0]["error"] == "connection refused"
