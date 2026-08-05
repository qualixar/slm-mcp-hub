"""Tests for Phase 2 — ConnectionManager lifecycle methods + MCPConnection drain + race fixes.

Coverage:
- add_server / remove_server / replace_server happy + sad paths
- disconnect_one removes entry from _connections (bug fix)
- MCPConnection drain_and_disconnect (no in-flight, with in-flight, timeout)
- ConnectionManager _lock serializes concurrent mutations
- FederationRouter routes correctly when conn.is_draining is True

Deleted (P04 dead-code removal):
- TestExitDiagnostic — tested _exit_diagnostic(), _stderr_tail, _process
- TestDrainingRejectsRequests — tested _send_request_stdio() (deleted)
- TestEOFFailsPending — tested _read_stdout() and _pending dict (deleted)
- test_disconnect_tolerates_already_dead_process — tested _process teardown (deleted)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.manager import ConnectionManager
from slm_mcp_hub.federation.router import FederationRouter

# ---------- Fixtures ----------

@pytest.fixture()
def registry() -> CapabilityRegistry:
    return CapabilityRegistry()


@pytest.fixture()
def empty_config(tmp_path) -> HubConfig:
    return HubConfig(config_dir=tmp_path)


@pytest.fixture()
def config_with_servers(tmp_path) -> HubConfig:
    return HubConfig(
        config_dir=tmp_path,
        mcp_servers=(
            MCPServerConfig(name="alpha", transport="stdio", command="echo", args=("a",)),
            MCPServerConfig(name="beta", transport="stdio", command="echo", args=("b",)),
        ),
    )


def _make_conn_mock(name: str, *, connected: bool = True, tools: int = 1):
    """Build a MagicMock that quacks like an MCPConnection."""
    mock = MagicMock()
    mock.name = name
    mock.is_connected = connected
    mock.is_draining = False
    mock.in_flight_count = 0
    mock.capabilities = {
        "tools": [{"name": f"t{i}", "description": "x"} for i in range(tools)],
        "resources": [],
        "resource_templates": [],
        "prompts": [],
    }
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock.drain_and_disconnect = AsyncMock()
    return mock


# ---------- ConnectionManager: add_server ----------

class TestAddServer:
    @pytest.mark.asyncio
    async def test_add_server_happy_path(self, empty_config, registry):
        mock_conn = _make_conn_mock("gamma", connected=True, tools=2)
        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=mock_conn):
            mgr = ConnectionManager(empty_config, registry)
            cfg = MCPServerConfig(name="gamma", transport="stdio", command="echo")
            ok, msg = await mgr.add_server(cfg)

        assert ok is True
        assert "Connected" in msg
        assert "gamma" in mgr.connections
        # Config was extended
        assert any(s.name == "gamma" for s in mgr._config.mcp_servers)

    @pytest.mark.asyncio
    async def test_add_server_duplicate_rejected(self, empty_config, registry):
        mock_conn = _make_conn_mock("gamma", connected=True)
        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=mock_conn):
            mgr = ConnectionManager(empty_config, registry)
            cfg = MCPServerConfig(name="gamma", transport="stdio", command="echo")
            ok1, _ = await mgr.add_server(cfg)
            ok2, msg2 = await mgr.add_server(cfg)

        assert ok1 is True
        assert ok2 is False
        assert "already connected" in msg2

    @pytest.mark.asyncio
    async def test_add_server_connect_failure(self, empty_config, registry):
        mock_conn = _make_conn_mock("bad", connected=False)
        mock_conn.connect = AsyncMock(side_effect=ConnectionError("nope"))
        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=mock_conn):
            mgr = ConnectionManager(empty_config, registry)
            cfg = MCPServerConfig(name="bad", transport="stdio", command="echo")
            ok, msg = await mgr.add_server(cfg)

        assert ok is False
        assert "Failed to connect" in msg
        # Server is still in config (so retry/status sees it) and in _failed
        assert "bad" in mgr._failed


# ---------- ConnectionManager: remove_server ----------

class TestRemoveServer:
    @pytest.mark.asyncio
    async def test_remove_server_happy_path(self, config_with_servers, registry):
        mocks = {
            "alpha": _make_conn_mock("alpha", connected=True),
            "beta": _make_conn_mock("beta", connected=True),
        }
        def factory(cfg):
            return mocks[cfg.name]

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=factory):
            mgr = ConnectionManager(config_with_servers, registry)
            await mgr.connect_all()
            assert "alpha" in mgr.connections

            ok, msg = await mgr.remove_server("alpha", drain_timeout_s=1.0)

        assert ok is True
        assert "Removed" in msg
        assert "alpha" not in mgr.connections
        # Other server untouched
        assert "beta" in mgr.connections
        # Config no longer carries removed server
        assert not any(s.name == "alpha" for s in mgr._config.mcp_servers)
        # drain_and_disconnect was called on alpha
        mocks["alpha"].drain_and_disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_server_not_found(self, empty_config, registry):
        mgr = ConnectionManager(empty_config, registry)
        ok, msg = await mgr.remove_server("nope")
        assert ok is False
        assert "not found" in msg


# ---------- ConnectionManager: replace_server ----------

class TestReplaceServer:
    @pytest.mark.asyncio
    async def test_replace_server_in_place(self, config_with_servers, registry):
        # First connection: returns this mock
        old_mock = _make_conn_mock("alpha", connected=True, tools=1)
        new_mock = _make_conn_mock("alpha", connected=True, tools=3)
        beta_mock = _make_conn_mock("beta", connected=True)

        call_count = {"alpha": 0}
        def factory(cfg):
            if cfg.name == "alpha":
                call_count["alpha"] += 1
                return old_mock if call_count["alpha"] == 1 else new_mock
            return beta_mock

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=factory):
            mgr = ConnectionManager(config_with_servers, registry)
            await mgr.connect_all()
            new_cfg = MCPServerConfig(name="alpha", transport="stdio", command="echo", args=("new",))
            ok, msg = await mgr.replace_server(new_cfg, drain_timeout_s=1.0)

        assert ok is True
        old_mock.drain_and_disconnect.assert_awaited_once()
        # The new connection object is what's now in the map
        assert mgr.connections["alpha"] is new_mock
        # Beta is untouched
        beta_mock.drain_and_disconnect.assert_not_called()


# ---------- disconnect_one bug fix: removes from _connections ----------

class TestDisconnectOneRemoval:
    @pytest.mark.asyncio
    async def test_disconnect_one_removes_entry(self, config_with_servers, registry):
        mocks = {
            "alpha": _make_conn_mock("alpha", connected=True),
            "beta": _make_conn_mock("beta", connected=True),
        }
        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=lambda c: mocks[c.name]):
            mgr = ConnectionManager(config_with_servers, registry)
            await mgr.connect_all()
            assert "alpha" in mgr.connections

            await mgr.disconnect_one("alpha")

        # Bug fix: alpha is gone from the live map, not just .disconnect()'d
        assert "alpha" not in mgr.connections
        assert "beta" in mgr.connections


# ---------- ConnectionManager: lock serializes mutations ----------

class TestLockSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_add_calls_serialized(self, empty_config, registry):
        """Two concurrent add_server calls must not interleave inside the lock."""
        observed_lock_state: list[bool] = []
        connect_started = asyncio.Event()
        release_connect = asyncio.Event()

        async def slow_connect():
            observed_lock_state.append(True)  # connect is happening
            connect_started.set()
            await release_connect.wait()

        def factory(cfg):
            m = _make_conn_mock(cfg.name, connected=True)
            if cfg.name == "first":
                m.connect = AsyncMock(side_effect=slow_connect)
            return m

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=factory):
            mgr = ConnectionManager(empty_config, registry)
            cfg1 = MCPServerConfig(name="first", transport="stdio", command="echo")
            cfg2 = MCPServerConfig(name="second", transport="stdio", command="echo")

            # Launch first add, wait for it to enter the slow connect
            task1 = asyncio.create_task(mgr.add_server(cfg1))
            await connect_started.wait()

            # Launch second add — it must wait on the lock
            task2 = asyncio.create_task(mgr.add_server(cfg2))
            await asyncio.sleep(0.05)
            # task2 still blocked because lock is held by task1
            assert not task2.done()

            # Release first and let both finish
            release_connect.set()
            ok1, _ = await task1
            ok2, _ = await task2

        assert ok1 is True
        assert ok2 is True
        # Both servers landed in connections
        assert set(mgr.connections.keys()) == {"first", "second"}


# ---------- MCPConnection drain semantics ----------

class TestDrainSemantics:
    @pytest.mark.asyncio
    async def test_drain_with_no_in_flight_disconnects_immediately(self):
        cfg = MCPServerConfig(name="z", transport="stdio", command="echo")
        conn = MCPConnection(cfg)
        # Pretend it's connected
        conn._state = ConnectionState.CONNECTED

        with patch.object(conn, "disconnect", new=AsyncMock()) as dc:
            await conn.drain_and_disconnect(timeout_s=5.0)

        dc.assert_awaited_once()
        assert conn._state == ConnectionState.DRAINING

    @pytest.mark.asyncio
    async def test_drain_waits_for_in_flight(self):
        cfg = MCPServerConfig(name="z", transport="stdio", command="echo")
        conn = MCPConnection(cfg)
        conn._state = ConnectionState.CONNECTED
        conn._in_flight = 1  # Simulate one call in flight

        async def finish_call_after_delay():
            await asyncio.sleep(0.05)
            # Simulate request finishing — clear in-flight + signal drain event
            conn._in_flight = 0
            if conn._drain_event is not None:
                conn._drain_event.set()

        with patch.object(conn, "disconnect", new=AsyncMock()) as dc:
            await asyncio.gather(
                conn.drain_and_disconnect(timeout_s=2.0),
                finish_call_after_delay(),
            )

        dc.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drain_timeout_forces_disconnect(self):
        cfg = MCPServerConfig(name="z", transport="stdio", command="echo")
        conn = MCPConnection(cfg)
        conn._state = ConnectionState.CONNECTED
        conn._in_flight = 1  # never resolves

        with patch.object(conn, "disconnect", new=AsyncMock()) as dc:
            await conn.drain_and_disconnect(timeout_s=0.1)

        dc.assert_awaited_once()
        # Even on timeout we still disconnect — drain is best-effort

    @pytest.mark.asyncio
    async def test_drain_when_already_disconnected_is_safe(self):
        cfg = MCPServerConfig(name="z", transport="stdio", command="echo")
        conn = MCPConnection(cfg)
        # State stays DISCONNECTED
        with patch.object(conn, "disconnect", new=AsyncMock()) as dc:
            await conn.drain_and_disconnect()
        dc.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_drains_serialize_via_lock(self):
        """Regression: two concurrent drain_and_disconnect on the SAME
        connection used to overwrite each other's _drain_event and hang
        the first caller until timeout. v0.2.0 final: per-connection lock
        serializes drains so the second caller sees DISCONNECTED state and
        returns immediately after the first completes."""
        cfg = MCPServerConfig(name="z", transport="stdio", command="echo")
        conn = MCPConnection(cfg)
        conn._state = ConnectionState.CONNECTED
        conn._in_flight = 1  # has in-flight call

        disconnect_calls = 0
        async def fake_disconnect():
            nonlocal disconnect_calls
            disconnect_calls += 1
            conn._state = ConnectionState.DISCONNECTED

        with patch.object(conn, "disconnect", new=fake_disconnect):
            async def resolve_call():
                await asyncio.sleep(0.05)
                conn._in_flight = 0
                if conn._drain_event is not None:
                    conn._drain_event.set()

            # Two concurrent drains — both should finish promptly without timeout
            await asyncio.wait_for(
                asyncio.gather(
                    conn.drain_and_disconnect(timeout_s=2.0),
                    conn.drain_and_disconnect(timeout_s=2.0),
                    resolve_call(),
                ),
                timeout=1.0,  # if lock was broken, would take >2s
            )
        # Both calls succeed; disconnect called at least once
        assert disconnect_calls >= 1


# ---------- FederationRouter: draining state surfaced ----------

class TestRouterDrainingState:
    def _setup(self):
        registry = CapabilityRegistry()
        registry.sync({
            "github": {
                "tools": [{"name": "search", "description": "search"}],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            }
        })
        conn = MagicMock()
        conn.is_connected = False
        conn.is_draining = True
        return FederationRouter(registry, {"github": conn}), conn

    @pytest.mark.asyncio
    async def test_route_returns_shutting_down_when_draining(self):
        router, _ = self._setup()
        result = await router.route_tool_call("github__search", {})
        assert result.success is False
        assert "shutting down" in result.result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_route_returns_not_connected_when_disconnected(self):
        # Defensive: explicitly set is_draining=False (not the truthy default MagicMock)
        registry = CapabilityRegistry()
        registry.sync({
            "github": {
                "tools": [{"name": "search", "description": "x"}],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            }
        })
        conn = MagicMock()
        conn.is_connected = False
        conn.is_draining = False
        router = FederationRouter(registry, {"github": conn})
        result = await router.route_tool_call("github__search", {})
        assert result.success is False
        assert "not connected" in result.result["content"][0]["text"]
