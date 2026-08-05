"""MCPConnection public-interface tests.

Covers only public behavior after the hand-rolled upstream stack was removed
in P04.  All tests that set conn._pending / conn._process / conn._http_client
directly or called _connect_stdio / _connect_http / _send_request /
_send_notification / _read_stdout have been deleted — they were testing
deleted implementation internals.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection


def _cfg(**kw):
    defaults = dict(name="test", transport="stdio", command="echo", args=("hi",))
    defaults.update(kw)
    return MCPServerConfig(**defaults)


class TestMCPConnectionProperties:
    def test_initial_state(self):
        c = MCPConnection(_cfg())
        assert c.name == "test"
        assert c.state == ConnectionState.DISCONNECTED
        assert c.is_connected is False
        assert c.uptime_seconds == 0.0
        assert c.capabilities == {
            "tools": [],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }


class TestMCPConnectionDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_noop_when_not_connected(self):
        c = MCPConnection(_cfg())
        await c.disconnect()
        assert c.state == ConnectionState.DISCONNECTED


class TestMCPConnectionErrors:
    @pytest.mark.asyncio
    async def test_connect_command_not_found(self):
        c = MCPConnection(_cfg(command="/no/such/binary_xyz_999"))
        with pytest.raises(ConnectionError, match="Command not found"):
            await c.connect()

    @pytest.mark.asyncio
    async def test_connect_os_error(self):
        """connect() raises ConnectionError and marks ERROR state on spawn failure.

        The SDK path (OutboundClient) wraps all exceptions from the handshake
        as ConnectionError.  We patch OutboundClient.connect() directly.
        """
        from slm_mcp_hub.protocol.outbound import OutboundClient

        with patch.object(
            OutboundClient, "connect", AsyncMock(side_effect=OSError("Permission denied"))
        ):
            c = MCPConnection(_cfg())
            with pytest.raises(ConnectionError):
                await c.connect()
            assert c.state == ConnectionState.ERROR

    @pytest.mark.asyncio
    async def test_connect_http_connection_error(self):
        """HTTP transport raises ConnectionError on unreachable server."""
        c = MCPConnection(_cfg(transport="http", url="http://127.0.0.1:1/mcp"))
        with pytest.raises(ConnectionError, match="initialization failed"):
            await c.connect()

    @pytest.mark.asyncio
    async def test_call_tool_not_connected(self):
        c = MCPConnection(_cfg())
        with pytest.raises(ConnectionError, match="not connected"):
            await c.call_tool("x", {})

    @pytest.mark.asyncio
    async def test_read_resource_not_connected(self):
        c = MCPConnection(_cfg())
        with pytest.raises(ConnectionError, match="not connected"):
            await c.read_resource("test://foo")

    @pytest.mark.asyncio
    async def test_get_prompt_not_connected(self):
        c = MCPConnection(_cfg())
        with pytest.raises(ConnectionError, match="not connected"):
            await c.get_prompt("p", {})

    @pytest.mark.asyncio
    async def test_call_tool_raises_when_draining(self):
        """Draining state prevents new requests (public behavior migrated from
        removed _send_request_stdio draining check)."""
        c = MCPConnection(_cfg())
        c._state = ConnectionState.DRAINING
        with pytest.raises(ConnectionError, match="draining"):
            await c.call_tool("x", {})

    @pytest.mark.asyncio
    async def test_read_resource_raises_when_draining(self):
        c = MCPConnection(_cfg())
        c._state = ConnectionState.DRAINING
        with pytest.raises(ConnectionError, match="draining"):
            await c.read_resource("test://foo")

    @pytest.mark.asyncio
    async def test_get_prompt_raises_when_draining(self):
        c = MCPConnection(_cfg())
        c._state = ConnectionState.DRAINING
        with pytest.raises(ConnectionError, match="draining"):
            await c.get_prompt("p", {})


class TestMCPConnectionUptime:
    """Test uptime and connect edge cases."""

    def test_uptime_zero_when_not_connected(self):
        """uptime_seconds returns 0.0 when connected_at is 0."""
        c = MCPConnection(_cfg())
        assert c._connected_at == 0
        assert c.uptime_seconds == 0.0

    def test_uptime_positive_when_connected(self):
        """uptime_seconds returns positive value when connected_at is set."""
        c = MCPConnection(_cfg())
        c._connected_at = 1000.0
        with patch("slm_mcp_hub.federation.connection.time.time", return_value=1005.0):
            assert c.uptime_seconds == 5.0

    @pytest.mark.asyncio
    async def test_connect_already_connected_returns_early(self):
        """connect() returns immediately when already connected."""
        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        await c.connect()  # Should not raise, should be a no-op
        assert c.state == ConnectionState.CONNECTED


class TestMCPConnectionProperties2:
    """Coverage for properties and SDK-path lifecycle not exercised elsewhere."""

    def test_is_draining_true_when_state_draining(self):
        c = MCPConnection(_cfg())
        c._state = ConnectionState.DRAINING
        assert c.is_draining is True

    def test_is_draining_false_when_connected(self):
        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        assert c.is_draining is False

    def test_in_flight_count_property(self):
        c = MCPConnection(_cfg())
        c._in_flight = 3
        assert c.in_flight_count == 3

    def test_is_auth_required_true(self):
        c = MCPConnection(_cfg())
        c._state = ConnectionState.AUTH_REQUIRED
        assert c.is_auth_required is True

    def test_is_auth_required_false_when_connected(self):
        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        assert c.is_auth_required is False

    def test_negotiated_peer_none_when_no_outbound(self):
        c = MCPConnection(_cfg())
        assert c.negotiated_peer is None

    def test_negotiated_peer_delegates_to_outbound(self):
        from unittest.mock import MagicMock

        from slm_mcp_hub.protocol.models import NegotiatedPeer, ProtocolEra

        c = MCPConnection(_cfg())
        mock_peer = NegotiatedPeer(
            era=ProtocolEra.MODERN_2026,
            protocol_version="2026-07-28",
            capabilities={},
        )
        mock_outbound = MagicMock()
        mock_outbound.negotiated_peer = mock_peer
        c._outbound = mock_outbound
        assert c.negotiated_peer is mock_peer

    @pytest.mark.asyncio
    async def test_connect_success_sets_state_and_capabilities(self):
        """connect() success path: state→CONNECTED, capabilities set, _outbound assigned."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.protocol.outbound import OutboundClient

        fake_caps = {"tools": [{"name": "t"}], "resources": [], "resource_templates": [], "prompts": []}
        mock_outbound = MagicMock(spec=OutboundClient)
        mock_outbound.connect = AsyncMock()
        mock_outbound.capabilities = fake_caps
        mock_outbound.negotiated_peer = None

        c = MCPConnection(_cfg())
        with patch("slm_mcp_hub.federation.connection.OutboundClient", return_value=mock_outbound):
            await c.connect()

        assert c.state == ConnectionState.CONNECTED
        assert c.is_connected is True
        assert c._outbound is mock_outbound
        assert c.capabilities["tools"][0]["name"] == "t"
        assert c._connected_at > 0

    @pytest.mark.asyncio
    async def test_disconnect_closes_outbound_and_resets_state(self):
        """disconnect() when outbound is set: closes it and resets state."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.protocol.outbound import OutboundClient

        mock_outbound = MagicMock(spec=OutboundClient)
        mock_outbound.disconnect = AsyncMock()

        c = MCPConnection(_cfg())
        c._outbound = mock_outbound
        c._state = ConnectionState.CONNECTED
        c._connected_at = 999.0

        await c.disconnect()

        mock_outbound.disconnect.assert_awaited_once()
        assert c._outbound is None
        assert c.state == ConnectionState.DISCONNECTED
        assert c._connected_at == 0.0

    @pytest.mark.asyncio
    async def test_disconnect_outbound_error_is_swallowed(self):
        """disconnect() swallows exceptions from outbound.disconnect()."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.protocol.outbound import OutboundClient

        mock_outbound = MagicMock(spec=OutboundClient)
        mock_outbound.disconnect = AsyncMock(side_effect=RuntimeError("close failed"))

        c = MCPConnection(_cfg())
        c._outbound = mock_outbound
        c._state = ConnectionState.CONNECTED

        # Must not raise
        await c.disconnect()
        assert c.state == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_read_resource_delegates_to_outbound(self):
        """read_resource() goes through _dispatch to the outbound client."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.protocol.outbound import OutboundClient

        mock_outbound = MagicMock(spec=OutboundClient)
        mock_outbound.read_resource = AsyncMock(return_value={"contents": [{"text": "data"}]})

        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        c._outbound = mock_outbound

        result = await c.read_resource("test://foo")
        assert result == {"contents": [{"text": "data"}]}
        mock_outbound.read_resource.assert_awaited_once_with("test://foo")

    @pytest.mark.asyncio
    async def test_get_prompt_delegates_to_outbound(self):
        """get_prompt() goes through _dispatch to the outbound client."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.protocol.outbound import OutboundClient

        mock_outbound = MagicMock(spec=OutboundClient)
        mock_outbound.get_prompt = AsyncMock(return_value={"messages": []})

        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        c._outbound = mock_outbound

        result = await c.get_prompt("my-prompt", {"key": "val"})
        assert result == {"messages": []}
        mock_outbound.get_prompt.assert_awaited_once_with("my-prompt", {"key": "val"})


class TestMCPConnectionInFlight:
    """_in_flight tracking for drain semantics via call_tool/read_resource/get_prompt."""

    @pytest.mark.asyncio
    async def test_in_flight_incremented_during_call_tool(self):
        """_in_flight is >0 while a call_tool coroutine is executing."""
        from slm_mcp_hub.protocol.outbound import OutboundClient

        observed: list[int] = []

        async def fake_call_tool(tool_name, arguments):
            observed.append(c._in_flight)
            return {"content": []}

        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        mock_outbound = AsyncMock(spec=OutboundClient)
        mock_outbound.call_tool = fake_call_tool
        c._outbound = mock_outbound

        await c.call_tool("x", {})
        assert observed == [1]
        assert c._in_flight == 0

    @pytest.mark.asyncio
    async def test_in_flight_decremented_on_exception(self):
        """_in_flight is decremented even when call_tool raises."""
        from slm_mcp_hub.protocol.outbound import OutboundClient

        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        mock_outbound = AsyncMock(spec=OutboundClient)
        mock_outbound.call_tool = AsyncMock(side_effect=RuntimeError("upstream error"))
        c._outbound = mock_outbound

        with pytest.raises(RuntimeError):
            await c.call_tool("x", {})

        assert c._in_flight == 0

    @pytest.mark.asyncio
    async def test_drain_event_set_when_in_flight_reaches_zero(self):
        """_drain_event is set when the last in-flight call completes."""
        from slm_mcp_hub.protocol.outbound import OutboundClient

        drain_event = asyncio.Event()

        c = MCPConnection(_cfg())
        c._state = ConnectionState.CONNECTED
        c._in_flight = 1  # simulate one already in flight
        c._drain_event = drain_event

        mock_outbound = AsyncMock(spec=OutboundClient)
        mock_outbound.call_tool = AsyncMock(return_value={"content": []})
        c._outbound = mock_outbound

        # call_tool will go through _dispatch which increments then decrements
        # Since we start at 1 and the new call increments to 2 then back to 1,
        # the event fires only when it reaches 0.  Test the decrement path:
        c._in_flight = 0
        await c.call_tool("x", {})
        # After returning to 0, drain_event should be set
        assert drain_event.is_set()
