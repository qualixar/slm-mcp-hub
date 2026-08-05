"""W3-P3 — On-demand reconnect on routing + global LRU cap.

TDD: tests written BEFORE implementation (RED→GREEN).

Test groups
-----------
1.  select_lru_victim — pure unit tests
2.  Router: tool/resource/prompt route to evicted backend triggers reconnect + succeeds
3.  Router: reconnect failure → clean RouteResult (no hang, no raise)
4.  Manager.ensure_connected: concurrent calls → single MCPConnection.connect() (idempotent)
5.  LRU cap: (N+1)th non-pinned backend evicts exactly the LRU non-pinned one
6.  LRU cap: pinned backend is NEVER victim
7.  max_live_backends == 0 → no cap (no eviction)
8.  End-to-end loop: LRU-evicted backend stays discoverable + auto-reconnects on next use
9.  HubRuntime wires reconnect_fn (integration smoke-test)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.manager import ConnectionManager

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _lazy_cfg(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="echo", spawn="lazy")


def _pinned_cfg(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="echo", spawn="pinned")


def _eager_cfg(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="echo", spawn="eager")


def _fake_conn(name: str, tools: list[str], *, connected: bool = True) -> MagicMock:
    """Minimal fake MCPConnection for routing tests."""
    from slm_mcp_hub.federation.connection import ConnectionState

    mock = MagicMock()
    mock.name = name
    mock.is_connected = connected
    mock.is_auth_required = False
    mock.in_flight_count = 0
    mock.capabilities = {
        "tools": [{"name": t, "description": "d"} for t in tools],
        "resources": [],
        "resource_templates": [],
        "prompts": [],
    }
    mock.state = ConnectionState.CONNECTED if connected else ConnectionState.STOPPED
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock.drain_and_disconnect = AsyncMock()
    mock.subscribe = MagicMock(return_value=lambda: None)
    mock.call_tool = AsyncMock(return_value={"result": "ok", "isError": False})
    mock.read_resource = AsyncMock(return_value={"content": "data"})
    mock.get_prompt = AsyncMock(return_value={"prompt": "text"})
    return mock


def _registry_with_tool(server: str, tool: str) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.sync({
        server: {
            "tools": [{"name": tool, "description": "d"}],
            "resources": [], "resource_templates": [], "prompts": [],
        }
    })
    return reg


def _registry_with_resource(server: str, raw_uri: str) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.sync({
        server: {
            "tools": [],
            "resources": [{"uri": raw_uri, "name": "r", "description": "d"}],
            "resource_templates": [], "prompts": [],
        }
    })
    return reg


def _registry_with_prompt(server: str, prompt: str) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.sync({
        server: {
            "tools": [], "resources": [], "resource_templates": [],
            "prompts": [{"name": prompt, "description": "d"}],
        }
    })
    return reg


# ---------------------------------------------------------------------------
# 1. select_lru_victim — pure unit tests
# ---------------------------------------------------------------------------

class TestSelectLruVictim:
    def test_empty_candidates_returns_none(self) -> None:
        from slm_mcp_hub.federation.lru import select_lru_victim

        assert select_lru_victim([], {}) is None

    def test_single_candidate_returned(self) -> None:
        from slm_mcp_hub.federation.lru import select_lru_victim

        result = select_lru_victim(["only"], {"only": 100.0})
        assert result == "only"

    def test_picks_minimum_last_activity(self) -> None:
        from slm_mcp_hub.federation.lru import select_lru_victim

        activity = {"a": 50.0, "b": 10.0, "c": 90.0}
        result = select_lru_victim(["a", "b", "c"], activity)
        assert result == "b"  # oldest (smallest ts)

    def test_unseeded_candidate_treated_as_oldest(self) -> None:
        """A backend with no last_activity entry is older than any seeded backend."""
        from slm_mcp_hub.federation.lru import select_lru_victim

        # "ghost" has no entry — should be chosen as LRU victim
        activity = {"seeded": 1.0}
        result = select_lru_victim(["seeded", "ghost"], activity)
        assert result == "ghost"

    def test_all_unseeded_picks_first_alphabetically_or_deterministically(self) -> None:
        """When all candidates have identical priority (all missing), still returns one."""
        from slm_mcp_hub.federation.lru import select_lru_victim

        result = select_lru_victim(["x", "y"], {})
        assert result in {"x", "y"}  # one of the two must be returned

    def test_two_candidates_picks_older(self) -> None:
        from slm_mcp_hub.federation.lru import select_lru_victim

        activity = {"newer": 999.0, "older": 1.0}
        assert select_lru_victim(["newer", "older"], activity) == "older"


# ---------------------------------------------------------------------------
# 2. Router: evicted backend → reconnect succeeds for tool/resource/prompt
# ---------------------------------------------------------------------------

class TestRouterReconnectSuccess:
    """When cap is found in registry but conn is disconnected,
    reconnect_fn must be called and route must succeed."""

    @pytest.mark.asyncio
    async def test_tool_route_triggers_reconnect_then_succeeds(self) -> None:
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", ["my_tool"], connected=False)
        live = _fake_conn("svc", ["my_tool"], connected=True)
        registry = _registry_with_tool("svc", "my_tool")
        connections: dict[str, Any] = {"svc": disconnected}
        reconnect_calls: list[str] = []

        async def reconnect_fn(name: str) -> bool:
            reconnect_calls.append(name)
            connections[name] = live  # simulate reconnect updating the shared dict
            return True

        router = FederationRouter(
            registry, connections, reconnect_fn=reconnect_fn
        )
        result = await router.route_tool_call("svc__my_tool", {})

        assert "svc" in reconnect_calls, "reconnect_fn must be called with server name"
        assert result.success is True
        assert result.server_name == "svc"

    @pytest.mark.asyncio
    async def test_resource_route_triggers_reconnect_then_succeeds(self) -> None:
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", [], connected=False)
        live = _fake_conn("svc", [], connected=True)
        registry = _registry_with_resource("svc", "res/path")
        connections: dict[str, Any] = {"svc": disconnected}
        reconnect_calls: list[str] = []

        async def reconnect_fn(name: str) -> bool:
            reconnect_calls.append(name)
            connections[name] = live
            return True

        router = FederationRouter(
            registry, connections, reconnect_fn=reconnect_fn
        )
        result = await router.route_resource_read("svc__res/path")

        assert "svc" in reconnect_calls
        assert result.success is True

    @pytest.mark.asyncio
    async def test_prompt_route_triggers_reconnect_then_succeeds(self) -> None:
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", [], connected=False)
        live = _fake_conn("svc", [], connected=True)
        registry = _registry_with_prompt("svc", "greet")
        connections: dict[str, Any] = {"svc": disconnected}
        reconnect_calls: list[str] = []

        async def reconnect_fn(name: str) -> bool:
            reconnect_calls.append(name)
            connections[name] = live
            return True

        router = FederationRouter(
            registry, connections, reconnect_fn=reconnect_fn
        )
        result = await router.route_prompt_get("svc__greet", {})

        assert "svc" in reconnect_calls
        assert result.success is True

    @pytest.mark.asyncio
    async def test_conn_none_also_triggers_reconnect(self) -> None:
        """When conn is None (backend not yet in pool), reconnect_fn is called."""
        from slm_mcp_hub.federation.router import FederationRouter

        live = _fake_conn("svc", ["my_tool"], connected=True)
        registry = _registry_with_tool("svc", "my_tool")
        connections: dict[str, Any] = {}  # no conn yet

        async def reconnect_fn(name: str) -> bool:
            connections[name] = live
            return True

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_tool_call("svc__my_tool", {})

        assert result.success is True

    @pytest.mark.asyncio
    async def test_reconnect_fn_not_called_when_conn_is_live(self) -> None:
        """An already-live backend never calls reconnect_fn."""
        from slm_mcp_hub.federation.router import FederationRouter

        live = _fake_conn("svc", ["my_tool"], connected=True)
        registry = _registry_with_tool("svc", "my_tool")
        connections: dict[str, Any] = {"svc": live}
        reconnect_calls: list[str] = []

        async def reconnect_fn(name: str) -> bool:
            reconnect_calls.append(name)
            return True

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_tool_call("svc__my_tool", {})

        assert result.success is True
        assert reconnect_calls == [], "reconnect_fn must NOT be called for live backends"

    @pytest.mark.asyncio
    async def test_draining_backend_not_reconnected(self) -> None:
        """A draining (is_draining=True) backend must NOT trigger reconnect_fn."""
        from slm_mcp_hub.federation.router import FederationRouter

        draining_conn = _fake_conn("svc", ["my_tool"], connected=False)
        draining_conn.is_draining = True
        registry = _registry_with_tool("svc", "my_tool")
        connections: dict[str, Any] = {"svc": draining_conn}
        reconnect_calls: list[str] = []

        async def reconnect_fn(name: str) -> bool:
            reconnect_calls.append(name)
            return True

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_tool_call("svc__my_tool", {})

        assert reconnect_calls == [], "draining backend must not trigger reconnect_fn"
        assert result.success is False
        assert "shutting down" in result.result.get("content", [{}])[0].get("text", "")

    @pytest.mark.asyncio
    async def test_activity_marked_after_successful_reconnect_and_route(self) -> None:
        """After reconnect + successful route, activity_fn must be called."""
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", ["t"], connected=False)
        live = _fake_conn("svc", ["t"], connected=True)
        registry = _registry_with_tool("svc", "t")
        connections: dict[str, Any] = {"svc": disconnected}
        activity_calls: list[str] = []

        async def reconnect_fn(name: str) -> bool:
            connections[name] = live
            return True

        router = FederationRouter(
            registry, connections,
            reconnect_fn=reconnect_fn,
            activity_fn=activity_calls.append,
        )
        result = await router.route_tool_call("svc__t", {})

        assert result.success is True
        assert "svc" in activity_calls


# ---------------------------------------------------------------------------
# 3. Router: reconnect failure → clean RouteResult (no hang, no raise)
# ---------------------------------------------------------------------------

class TestRouterReconnectFailure:
    """When reconnect_fn returns False (or raises), the router must return
    a clean RouteResult with success=False and never raise or hang."""

    @pytest.mark.asyncio
    async def test_tool_reconnect_failure_returns_clean_error(self) -> None:
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", ["t"], connected=False)
        registry = _registry_with_tool("svc", "t")
        connections: dict[str, Any] = {"svc": disconnected}

        async def reconnect_fn(_: str) -> bool:
            return False

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_tool_call("svc__t", {})

        assert result.success is False
        assert result.result.get("isError") is True

    @pytest.mark.asyncio
    async def test_resource_reconnect_failure_returns_clean_error(self) -> None:
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", [], connected=False)
        registry = _registry_with_resource("svc", "res/path")
        connections: dict[str, Any] = {"svc": disconnected}

        async def reconnect_fn(_: str) -> bool:
            return False

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_resource_read("svc__res/path")

        assert result.success is False

    @pytest.mark.asyncio
    async def test_prompt_reconnect_failure_returns_clean_error(self) -> None:
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", [], connected=False)
        registry = _registry_with_prompt("svc", "greet")
        connections: dict[str, Any] = {"svc": disconnected}

        async def reconnect_fn(_: str) -> bool:
            return False

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_prompt_get("svc__greet", {})

        assert result.success is False

    @pytest.mark.asyncio
    async def test_reconnect_fn_raises_returns_clean_error(self) -> None:
        """If reconnect_fn itself raises an exception, router never propagates it."""
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", ["t"], connected=False)
        registry = _registry_with_tool("svc", "t")
        connections: dict[str, Any] = {"svc": disconnected}

        async def reconnect_fn(_: str) -> bool:
            raise RuntimeError("network error")

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        # Must not raise
        result = await router.route_tool_call("svc__t", {})

        assert result.success is False

    @pytest.mark.asyncio
    async def test_reconnect_fn_returns_true_but_conn_still_dead_is_error(self) -> None:
        """reconnect_fn claims success but conn is still disconnected → error."""
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", ["t"], connected=False)
        registry = _registry_with_tool("svc", "t")
        connections: dict[str, Any] = {"svc": disconnected}

        async def reconnect_fn(_: str) -> bool:
            return True  # lies — doesn't update connections

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_tool_call("svc__t", {})

        # conn is still disconnected → should be error despite True return
        assert result.success is False

    @pytest.mark.asyncio
    async def test_resource_reconnect_fn_raises_returns_clean_error(self) -> None:
        """If reconnect_fn raises inside route_resource_read, router never propagates it."""
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", [], connected=False)
        registry = _registry_with_resource("svc", "res/path")
        connections: dict[str, Any] = {"svc": disconnected}

        async def reconnect_fn(_: str) -> bool:
            raise RuntimeError("network error")

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_resource_read("svc__res/path")

        assert result.success is False

    @pytest.mark.asyncio
    async def test_prompt_reconnect_fn_raises_returns_clean_error(self) -> None:
        """If reconnect_fn raises inside route_prompt_get, router never propagates it."""
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", [], connected=False)
        registry = _registry_with_prompt("svc", "greet")
        connections: dict[str, Any] = {"svc": disconnected}

        async def reconnect_fn(_: str) -> bool:
            raise RuntimeError("network error")

        router = FederationRouter(registry, connections, reconnect_fn=reconnect_fn)
        result = await router.route_prompt_get("svc__greet", {})

        assert result.success is False

    @pytest.mark.asyncio
    async def test_no_reconnect_fn_preserves_original_error_messages(self) -> None:
        """Without reconnect_fn, existing error message shapes are unchanged."""
        from slm_mcp_hub.federation.router import FederationRouter

        # conn is None → "Server not configured"
        registry = _registry_with_tool("svc", "t")
        router = FederationRouter(registry, {})  # no reconnect_fn

        result = await router.route_tool_call("svc__t", {})
        assert result.success is False
        text = result.result.get("content", [{}])[0].get("text", "")
        assert "not configured" in text

    @pytest.mark.asyncio
    async def test_no_reconnect_fn_disconnected_conn_error_message(self) -> None:
        """Without reconnect_fn + disconnected conn → 'Server not connected'."""
        from slm_mcp_hub.federation.router import FederationRouter

        disconnected = _fake_conn("svc", ["t"], connected=False)
        registry = _registry_with_tool("svc", "t")
        router = FederationRouter(registry, {"svc": disconnected})  # no reconnect_fn

        result = await router.route_tool_call("svc__t", {})
        assert result.success is False
        text = result.result.get("content", [{}])[0].get("text", "")
        assert "not connected" in text


# ---------------------------------------------------------------------------
# 4. Concurrent ensure_connected → single MCPConnection.connect() (idempotent)
# ---------------------------------------------------------------------------

class TestEnsureConnectedIdempotent:
    @pytest.mark.asyncio
    async def test_concurrent_ensure_connected_calls_connect_once(
        self, tmp_path
    ) -> None:
        """Two concurrent ensure_connected calls for the same evicted backend
        must trigger exactly ONE MCPConnection.connect() call (W2-P1 gate)."""
        lazy_srv = _lazy_cfg("svc")
        config = HubConfig(config_dir=tmp_path, mcp_servers=(lazy_srv,))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        # Seed evicted caps so registry and _connect_timed can proceed
        mgr._evicted_caps["svc"] = {
            "tools": [{"name": "my_tool", "description": "d"}],
            "resources": [], "resource_templates": [], "prompts": [],
        }
        mgr._sync_registry()

        connect_calls: list[str] = []

        async def delayed_connect() -> None:
            connect_calls.append("connect")
            await asyncio.sleep(0.02)  # allow second coroutine to reach the gate

        live_conn = _fake_conn("svc", ["my_tool"])
        live_conn.connect = AsyncMock(side_effect=delayed_connect)

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=live_conn):
            results = await asyncio.gather(
                mgr.ensure_connected("svc"),
                mgr.ensure_connected("svc"),
            )

        assert all(results), f"Both calls must return True; got {results}"
        assert len(connect_calls) == 1, (
            f"MCPConnection.connect() must be called exactly once; got {len(connect_calls)}"
        )

    @pytest.mark.asyncio
    async def test_ensure_connected_already_live_returns_true_immediately(
        self, tmp_path
    ) -> None:
        """ensure_connected on an already-live backend returns True without reconnecting."""
        lazy_srv = _lazy_cfg("svc")
        config = HubConfig(config_dir=tmp_path, mcp_servers=(lazy_srv,))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        live = _fake_conn("svc", ["t"])
        mgr._connections["svc"] = live  # already connected

        result = await mgr.ensure_connected("svc")
        assert result is True
        live.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ensure_connected_unknown_backend_returns_false(
        self, tmp_path
    ) -> None:
        """ensure_connected for an unknown backend name returns False."""
        config = HubConfig(config_dir=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        result = await mgr.ensure_connected("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# 5. LRU cap: (N+1)th non-pinned backend evicts exactly the LRU one
# ---------------------------------------------------------------------------

class TestLruCapEviction:
    @pytest.mark.asyncio
    async def test_nth_plus_one_backend_evicts_lru_non_pinned(
        self, tmp_path
    ) -> None:
        """When max_live_backends == 2 and 2 non-pinned backends are live,
        connecting a third must evict the LRU one."""
        svc_a = _lazy_cfg("a")
        svc_b = _lazy_cfg("b")
        svc_c = _lazy_cfg("c")
        config = HubConfig(
            config_dir=tmp_path,
            max_live_backends=2,
            mcp_servers=(svc_a, svc_b, svc_c),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn_a = _fake_conn("a", ["a_tool"])
        conn_b = _fake_conn("b", ["b_tool"])
        mgr._connections["a"] = conn_a
        mgr._connections["b"] = conn_b

        # "a" was active later → "b" is LRU (activity at earlier time)
        mgr._reaper._last_activity["a"] = 100.0
        mgr._reaper._last_activity["b"] = 10.0

        conn_c = _fake_conn("c", ["c_tool"])

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection", return_value=conn_c
        ):
            await mgr.ensure_connected("c")

        # "b" is LRU — its drain must have been called
        conn_b.drain_and_disconnect.assert_awaited()
        # "a" must be untouched
        conn_a.drain_and_disconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cap_not_exceeded_no_eviction(self, tmp_path) -> None:
        """When live count < max_live_backends, no eviction occurs."""
        svc_a = _lazy_cfg("a")
        svc_b = _lazy_cfg("b")
        config = HubConfig(
            config_dir=tmp_path,
            max_live_backends=3,  # cap is 3, only 1 live
            mcp_servers=(svc_a, svc_b),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn_a = _fake_conn("a", ["a_tool"])
        mgr._connections["a"] = conn_a
        mgr._reaper._last_activity["a"] = 1.0

        conn_b = _fake_conn("b", ["b_tool"])

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection", return_value=conn_b
        ):
            await mgr.ensure_connected("b")

        conn_a.drain_and_disconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lru_victim_unseeded_treated_as_oldest(self, tmp_path) -> None:
        """A live backend with no activity timestamp is the LRU victim."""
        svc_a = _lazy_cfg("a")
        svc_b = _lazy_cfg("b")
        svc_c = _lazy_cfg("c")
        config = HubConfig(
            config_dir=tmp_path,
            max_live_backends=2,
            mcp_servers=(svc_a, svc_b, svc_c),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn_a = _fake_conn("a", ["a_tool"])
        conn_b = _fake_conn("b", ["b_tool"])
        mgr._connections["a"] = conn_a
        mgr._connections["b"] = conn_b

        # "a" has activity; "b" has NO activity → "b" is oldest (treated as -inf)
        mgr._reaper._last_activity["a"] = 50.0
        # "b" intentionally left unseeded

        conn_c = _fake_conn("c", ["c_tool"])

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection", return_value=conn_c
        ):
            await mgr.ensure_connected("c")

        # "b" (unseeded → -inf) must be evicted
        conn_b.drain_and_disconnect.assert_awaited()
        conn_a.drain_and_disconnect.assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. LRU cap: pinned backend is NEVER victim
# ---------------------------------------------------------------------------

class TestLruCapPinnedExempt:
    @pytest.mark.asyncio
    async def test_pinned_backend_never_lru_victim(self, tmp_path) -> None:
        """max_live_backends=1; 1 non-pinned live + 1 pinned live.
        Connecting another non-pinned must evict the non-pinned one, not pinned."""
        svc_np = _lazy_cfg("non_pinned")
        svc_pin = _pinned_cfg("pinned")
        svc_new = _lazy_cfg("new")
        config = HubConfig(
            config_dir=tmp_path,
            max_live_backends=1,
            mcp_servers=(svc_np, svc_pin, svc_new),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn_np = _fake_conn("non_pinned", ["np_tool"])
        conn_pin = _fake_conn("pinned", ["pin_tool"])
        mgr._connections["non_pinned"] = conn_np
        mgr._connections["pinned"] = conn_pin

        # Pinned is oldest (lower activity) — must still never be victim
        mgr._reaper._last_activity["non_pinned"] = 100.0
        mgr._reaper._last_activity["pinned"] = 1.0

        conn_new = _fake_conn("new", ["new_tool"])

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection", return_value=conn_new
        ):
            await mgr.ensure_connected("new")

        # Pinned must survive; non_pinned is the only eligible victim
        conn_pin.drain_and_disconnect.assert_not_awaited()
        conn_np.drain_and_disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_pinned_backend_not_counted_toward_cap(self, tmp_path) -> None:
        """Pinned backends do not count toward max_live_backends.
        max_live_backends=1; only pinned backends live → connecting a non-pinned
        does NOT trigger eviction (count of non-pinned = 0 < cap)."""
        svc_pin = _pinned_cfg("pinned")
        svc_new = _lazy_cfg("new")
        config = HubConfig(
            config_dir=tmp_path,
            max_live_backends=1,
            mcp_servers=(svc_pin, svc_new),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn_pin = _fake_conn("pinned", ["pin_tool"])
        mgr._connections["pinned"] = conn_pin

        conn_new = _fake_conn("new", ["new_tool"])

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection", return_value=conn_new
        ):
            await mgr.ensure_connected("new")

        # No non-pinned backends were live before → no eviction needed
        conn_pin.drain_and_disconnect.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. max_live_backends == 0 → no cap (no eviction for capacity)
# ---------------------------------------------------------------------------

class TestLruCapDisabled:
    @pytest.mark.asyncio
    async def test_zero_max_live_backends_never_evicts(self, tmp_path) -> None:
        """max_live_backends=0 means unlimited — no LRU evictions for capacity."""
        backends = [_lazy_cfg(f"svc{i}") for i in range(5)]
        config = HubConfig(
            config_dir=tmp_path,
            max_live_backends=0,  # unlimited
            mcp_servers=tuple(backends),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        live_conns: dict[str, MagicMock] = {}
        for b in backends[:4]:
            c = _fake_conn(b.name, [b.name + "_tool"])
            mgr._connections[b.name] = c
            mgr._reaper._last_activity[b.name] = float(len(live_conns))
            live_conns[b.name] = c

        new_conn = _fake_conn("svc4", ["svc4_tool"])
        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection", return_value=new_conn
        ):
            await mgr.ensure_connected("svc4")

        for c in live_conns.values():
            c.drain_and_disconnect.assert_not_awaited()


# ---------------------------------------------------------------------------
# 8. End-to-end loop: LRU-evicted → stays discoverable → auto-reconnects
# ---------------------------------------------------------------------------

class TestLruEvictedAutoReconnectLoop:
    @pytest.mark.asyncio
    async def test_lru_evicted_backend_stays_discoverable(self, tmp_path) -> None:
        """An LRU-evicted backend's tools remain in the registry (W3-P1)
        so a later route call can discover and reconnect it."""
        svc_a = _lazy_cfg("a")
        svc_b = _lazy_cfg("b")
        config = HubConfig(
            config_dir=tmp_path,
            max_live_backends=1,
            mcp_servers=(svc_a, svc_b),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn_a = _fake_conn("a", ["a_tool"])
        mgr._connections["a"] = conn_a
        mgr._reaper._last_activity["a"] = 1.0
        # Register "a"'s tools in registry
        mgr._sync_registry()

        # Connect "b" — LRU cap evicts "a" first
        conn_b = _fake_conn("b", ["b_tool"])
        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=conn_b):
            await mgr.ensure_connected("b")

        # "a" must have been evicted
        conn_a.drain_and_disconnect.assert_awaited()

        # "a"'s tool must still be discoverable (W3-P1 retention)
        all_tools = [t["name"] for t in registry.list_tools()]
        assert any("a_tool" in n for n in all_tools), (
            f"a_tool must stay in registry after LRU eviction; got {all_tools}"
        )

    @pytest.mark.asyncio
    async def test_lru_evicted_backend_reconnects_on_next_route(
        self, tmp_path
    ) -> None:
        """After LRU eviction + reconnect, routing to the evicted backend succeeds."""
        from slm_mcp_hub.federation.router import FederationRouter

        svc_a = _lazy_cfg("a")
        svc_b = _lazy_cfg("b")
        config = HubConfig(
            config_dir=tmp_path,
            max_live_backends=1,
            mcp_servers=(svc_a, svc_b),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn_a = _fake_conn("a", ["a_tool"])
        mgr._connections["a"] = conn_a
        mgr._reaper._last_activity["a"] = 1.0
        mgr._sync_registry()

        # Step 1: connect "b" → evicts "a"
        conn_b = _fake_conn("b", ["b_tool"])
        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=conn_b):
            await mgr.ensure_connected("b")

        # "a" is now evicted but discoverable
        assert any("a_tool" in t["name"] for t in registry.list_tools())

        # Step 2: route to "a__a_tool" — must trigger reconnect
        conn_a_new = _fake_conn("a", ["a_tool"])

        router = FederationRouter(
            registry,
            mgr.connections,
            reconnect_fn=mgr.ensure_connected,
        )

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=conn_a_new):
            # Reconnecting "a" will evict "b" (LRU cap still = 1)
            result = await router.route_tool_call("a__a_tool", {})

        assert result.success is True, f"Route to reconnected backend must succeed; got {result}"


# ---------------------------------------------------------------------------
# 9. HubRuntime wires reconnect_fn (integration smoke-test)
# ---------------------------------------------------------------------------

class TestHubRuntimeWiring:
    def test_runtime_router_has_reconnect_fn(self, tmp_path) -> None:
        """HubRuntime must wire reconnect_fn=manager.ensure_connected to the router."""
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        mock_hub = MagicMock()
        mock_hub.config = HubConfig(config_dir=tmp_path)

        runtime = HubRuntime(mock_hub)

        assert runtime.router._reconnect_fn is not None, (
            "router._reconnect_fn must be wired by HubRuntime"
        )
        # Should point to manager.ensure_connected
        assert runtime.router._reconnect_fn == runtime.conn_manager.ensure_connected
