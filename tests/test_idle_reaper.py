"""W3-P2 — Idle eviction reaper + last-activity tracking.

TDD approach — tests written BEFORE implementation to drive the RED→GREEN cycle.

Test strategy:
1. IdleReaper basics: mark_activity, seed_activity, forget
2. Disabled when idle_ttl_seconds == 0 (no-op start, no task created)
3. Eviction eligibility: ONLY lazy backends evicted; eager/pinned NEVER
4. Fake-clock TTL: idle lazy backend evicted; active lazy backend NOT evicted
5. Concurrent eviction: asyncio.gather, not serial
6. Start/stop: idempotent start; cancel+await on stop; no task leak
7. Manager.mark_activity delegates to reaper
8. Router calls activity_fn on each SUCCESSFUL route; NOT on failures
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.manager import ConnectionManager

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class FakeClock:
    """Monotonic-clock stub that advances on demand."""

    def __init__(self, initial: float = 0.0) -> None:
        self.now: float = initial

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class FakeSleep:
    """asyncio.sleep stub that completes instantly and records calls."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _lazy_cfg(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="echo", spawn="lazy")


def _eager_cfg(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="echo", spawn="eager")


def _pinned_cfg(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="echo", spawn="pinned")


def _always_on_cfg(name: str) -> MCPServerConfig:
    """always_on=True without explicit spawn → is_pinned=True via property."""
    return MCPServerConfig(
        name=name, transport="stdio", command="echo", always_on=True, spawn="eager"
    )


def _make_reaper(
    config: HubConfig,
    evict_fn: Any,
    backends: list[MCPServerConfig],
    live_names: set[str] | None = None,
    *,
    clock: FakeClock | None = None,
    sleep_fn: FakeSleep | None = None,
    interval: float = 30.0,
    has_inflight_fn: Any = None,
) -> Any:
    """Construct an IdleReaper with injected dependencies."""
    from slm_mcp_hub.federation.eviction import IdleReaper

    _live = live_names if live_names is not None else {b.name for b in backends}
    return IdleReaper(
        config=config,
        evict_fn=evict_fn,
        get_backends_fn=lambda: backends,
        is_live_fn=lambda n: n in _live,
        interval=interval,
        time_fn=clock if clock is not None else FakeClock(0.0),
        sleep_fn=sleep_fn if sleep_fn is not None else FakeSleep(),
        has_inflight_fn=has_inflight_fn,
    )


def _fake_conn(name: str, tools: list[str], *, connected: bool = True) -> MagicMock:
    from slm_mcp_hub.federation.connection import ConnectionState

    mock = MagicMock()
    mock.name = name
    mock.is_connected = connected
    mock.is_auth_required = False
    mock.in_flight_count = 0  # W3-P2: no in-flight calls by default
    mock.capabilities = {
        "tools": [{"name": t, "description": "test"} for t in tools],
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


# ---------------------------------------------------------------------------
# 1. IdleReaper basics — mark_activity / seed_activity / forget
# ---------------------------------------------------------------------------

class TestIdleReaperBasics:
    def test_mark_activity_stores_current_time(self) -> None:
        from slm_mcp_hub.federation.eviction import IdleReaper

        clock = FakeClock(42.0)
        config = HubConfig(idle_ttl_seconds=60)
        reaper = IdleReaper(
            config=config,
            evict_fn=AsyncMock(),
            get_backends_fn=lambda: [],
            is_live_fn=lambda n: True,
            time_fn=clock,
        )

        reaper.mark_activity("backend-a")
        assert reaper._last_activity["backend-a"] == 42.0

        clock.advance(5.0)
        reaper.mark_activity("backend-a")
        assert reaper._last_activity["backend-a"] == 47.0

    def test_seed_activity_initialises_without_overwriting(self) -> None:
        from slm_mcp_hub.federation.eviction import IdleReaper

        clock = FakeClock(10.0)
        config = HubConfig(idle_ttl_seconds=60)
        reaper = IdleReaper(
            config=config,
            evict_fn=AsyncMock(),
            get_backends_fn=lambda: [],
            is_live_fn=lambda n: True,
            time_fn=clock,
        )

        # First seed sets the value.
        reaper.seed_activity("svc")
        assert reaper._last_activity["svc"] == 10.0

        # Advancing and re-seeding must NOT overwrite.
        clock.advance(5.0)
        reaper.seed_activity("svc")
        assert reaper._last_activity["svc"] == 10.0, (
            "seed_activity must not overwrite an existing timestamp"
        )

    def test_forget_removes_tracking_entry(self) -> None:
        from slm_mcp_hub.federation.eviction import IdleReaper

        config = HubConfig(idle_ttl_seconds=60)
        reaper = IdleReaper(
            config=config,
            evict_fn=AsyncMock(),
            get_backends_fn=lambda: [],
            is_live_fn=lambda n: True,
        )

        reaper.mark_activity("svc-x")
        assert "svc-x" in reaper._last_activity

        reaper.forget("svc-x")
        assert "svc-x" not in reaper._last_activity

    def test_forget_missing_name_is_noop(self) -> None:
        from slm_mcp_hub.federation.eviction import IdleReaper

        config = HubConfig(idle_ttl_seconds=60)
        reaper = IdleReaper(
            config=config,
            evict_fn=AsyncMock(),
            get_backends_fn=lambda: [],
            is_live_fn=lambda n: True,
        )
        # Must not raise.
        reaper.forget("nonexistent")


# ---------------------------------------------------------------------------
# 2. Disabled when idle_ttl_seconds == 0
# ---------------------------------------------------------------------------

class TestIdleReaperDisabled:
    @pytest.mark.asyncio
    async def test_start_is_noop_when_ttl_zero(self) -> None:
        config = HubConfig(idle_ttl_seconds=0)
        evict_mock = AsyncMock()
        reaper = _make_reaper(config, evict_mock, [_lazy_cfg("svc")])

        await reaper.start()

        # No background task must be created.
        assert reaper._task is None
        assert reaper.is_running is False

    @pytest.mark.asyncio
    async def test_check_and_evict_skips_when_ttl_zero(self) -> None:
        """_check_and_evict is harmless even if called directly when ttl=0."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=0)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_lazy_cfg("svc")],
            clock=clock,
        )

        reaper.seed_activity("svc")
        clock.advance(10000.0)  # massively past any TTL

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Eviction eligibility — ONLY lazy backends; eager/pinned NEVER
# ---------------------------------------------------------------------------

class TestEvictionEligibility:
    @pytest.mark.asyncio
    async def test_lazy_idle_backend_is_evicted(self) -> None:
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_lazy_cfg("lazy-svc")],
            clock=clock,
        )

        reaper.seed_activity("lazy-svc")
        clock.advance(61.0)

        await reaper._check_and_evict()

        evict_mock.assert_awaited_once_with("lazy-svc")

    @pytest.mark.asyncio
    async def test_eager_backend_never_evicted_regardless_of_age(self) -> None:
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_eager_cfg("eager-svc")],
            clock=clock,
        )

        reaper.seed_activity("eager-svc")
        clock.advance(99999.0)  # way past TTL

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pinned_backend_never_evicted(self) -> None:
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_pinned_cfg("pinned-svc")],
            clock=clock,
        )

        reaper.seed_activity("pinned-svc")
        clock.advance(99999.0)

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_always_on_backend_never_evicted(self) -> None:
        """always_on=True → is_pinned=True → reaper must skip it even if spawn="eager"."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_always_on_cfg("always-hot")],
            clock=clock,
        )

        reaper.seed_activity("always-hot")
        clock.advance(99999.0)

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lazy_always_on_backend_never_evicted(self) -> None:
        """Edge case: spawn="lazy" AND always_on=True → is_pinned=True → never evicted.

        This is the belt-and-suspenders guard in _check_and_evict (line after
        the spawn!="lazy" check) that covers the unusual programmatic combination.
        """
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        # spawn="lazy" passes the first filter, but is_pinned=True (via always_on)
        # hits the second guard.
        unusual_cfg = MCPServerConfig(
            name="odd-svc", transport="stdio", command="echo",
            spawn="lazy", always_on=True,
        )
        reaper = _make_reaper(
            config, evict_mock, [unusual_cfg],
            clock=clock,
        )

        reaper.seed_activity("odd-svc")
        clock.advance(99999.0)

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_live_backend_skipped(self) -> None:
        """A lazy backend that is not live (already evicted) is not re-evicted."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_lazy_cfg("dead-svc")],
            live_names=set(),  # backend is NOT live
            clock=clock,
        )

        reaper.seed_activity("dead-svc")
        clock.advance(99999.0)

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unseeded_backend_not_evicted(self) -> None:
        """A just-connected lazy backend with no activity seeded yet is skipped."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_lazy_cfg("fresh-svc")],
            clock=clock,
        )
        # No seed_activity call — simulates backend connecting but reaper
        # checking before seed_activity is called (conservative skip).
        clock.advance(99999.0)

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Fake-clock TTL precision — active backend NOT evicted
# ---------------------------------------------------------------------------

class TestFakeClockEviction:
    @pytest.mark.asyncio
    async def test_active_lazy_backend_not_evicted_within_ttl(self) -> None:
        """Recent activity (within TTL) prevents eviction."""
        clock = FakeClock(100.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_lazy_cfg("active-lazy")],
            clock=clock,
        )

        # Mark activity at t=100
        reaper.mark_activity("active-lazy")

        # Advance only 30s (within TTL of 60s)
        clock.advance(30.0)

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backend_evicted_exactly_at_ttl_boundary(self) -> None:
        """Backend is evicted when idle time STRICTLY exceeds TTL."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config, evict_mock, [_lazy_cfg("boundary-svc")],
            clock=clock,
        )

        reaper.seed_activity("boundary-svc")
        # Exactly at TTL boundary: NOT evicted (idle == TTL, not > TTL)
        clock.advance(60.0)
        await reaper._check_and_evict()
        evict_mock.assert_not_awaited()

        # One second past TTL: evicted
        clock.advance(1.0)
        await reaper._check_and_evict()
        evict_mock.assert_awaited_once_with("boundary-svc")

    @pytest.mark.asyncio
    async def test_multiple_idle_backends_all_evicted(self) -> None:
        """All idle lazy backends in the config are evicted."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        backends = [_lazy_cfg("a"), _lazy_cfg("b"), _lazy_cfg("c")]
        reaper = _make_reaper(
            config, evict_mock, backends,
            clock=clock,
        )

        for b in backends:
            reaper.seed_activity(b.name)
        clock.advance(61.0)

        await reaper._check_and_evict()

        evicted_names = {call.args[0] for call in evict_mock.await_args_list}
        assert evicted_names == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_mixed_active_and_idle_backends(self) -> None:
        """Only the idle backend is evicted; the active one is spared."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        backends = [_lazy_cfg("idle-one"), _lazy_cfg("active-one")]
        reaper = _make_reaper(
            config, evict_mock, backends,
            clock=clock,
        )

        # Both seeded at t=0
        reaper.seed_activity("idle-one")
        reaper.seed_activity("active-one")

        # Advance 50s, then mark active-one (within TTL from now)
        clock.advance(50.0)
        reaper.mark_activity("active-one")

        # Advance another 15s: idle-one is 65s old, active-one is 15s old
        clock.advance(15.0)

        await reaper._check_and_evict()

        evicted_names = {call.args[0] for call in evict_mock.await_args_list}
        assert "idle-one" in evicted_names
        assert "active-one" not in evicted_names


# ---------------------------------------------------------------------------
# 5. Concurrent eviction (asyncio.gather, not serial)
# ---------------------------------------------------------------------------

class TestConcurrentEviction:
    @pytest.mark.asyncio
    async def test_evictions_launched_concurrently(self) -> None:
        """Multiple idle backends are evicted concurrently (via asyncio.gather)."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        eviction_order: list[str] = []

        async def slow_evict(name: str) -> None:
            eviction_order.append(name)
            # Yield to the event loop — concurrent means all names are appended
            # before any of the coroutines returns, in an arbitrary order.
            await asyncio.sleep(0)

        backends = [_lazy_cfg("x"), _lazy_cfg("y"), _lazy_cfg("z")]
        reaper = _make_reaper(
            config, slow_evict, backends, clock=clock
        )

        for b in backends:
            reaper.seed_activity(b.name)
        clock.advance(999.0)

        await reaper._check_and_evict()

        # All three must have been evicted (order is non-deterministic)
        assert sorted(eviction_order) == ["x", "y", "z"]

    @pytest.mark.asyncio
    async def test_evict_error_does_not_block_others(self) -> None:
        """If one eviction raises, the others still complete."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_calls: list[str] = []

        async def maybe_fail(name: str) -> None:
            evict_calls.append(name)
            if name == "flaky":
                raise RuntimeError("drain failed")

        backends = [_lazy_cfg("flaky"), _lazy_cfg("good")]
        reaper = _make_reaper(config, maybe_fail, backends, clock=clock)

        for b in backends:
            reaper.seed_activity(b.name)
        clock.advance(999.0)

        # Must not raise even if one eviction fails.
        await reaper._check_and_evict()

        assert "flaky" in evict_calls
        assert "good" in evict_calls


# ---------------------------------------------------------------------------
# 6. Start / stop lifecycle — idempotent, no task leak
# ---------------------------------------------------------------------------

class TestIdleReaperStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_background_task(self) -> None:
        config = HubConfig(idle_ttl_seconds=60)
        sleep_fn = FakeSleep()
        reaper = _make_reaper(
            config, AsyncMock(), [_lazy_cfg("svc")], sleep_fn=sleep_fn
        )

        await reaper.start()
        try:
            assert reaper.is_running is True
            assert reaper._task is not None
            assert not reaper._task.done()
        finally:
            await reaper.stop()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """Calling start twice does not create a second task."""
        config = HubConfig(idle_ttl_seconds=60)
        reaper = _make_reaper(config, AsyncMock(), [_lazy_cfg("svc")])

        await reaper.start()
        task_first = reaper._task
        await reaper.start()
        task_second = reaper._task

        try:
            assert task_first is task_second, "start() must not replace a running task"
        finally:
            await reaper.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_and_awaits_task(self) -> None:
        """stop() cancels the task and leaves is_running=False with no pending task."""
        config = HubConfig(idle_ttl_seconds=60)
        reaper = _make_reaper(config, AsyncMock(), [_lazy_cfg("svc")])

        await reaper.start()
        assert reaper.is_running is True

        await reaper.stop()

        assert reaper.is_running is False
        assert reaper._task is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started_is_noop(self) -> None:
        """stop() on a never-started reaper must not raise."""
        config = HubConfig(idle_ttl_seconds=60)
        reaper = _make_reaper(config, AsyncMock(), [_lazy_cfg("svc")])

        # Must not raise.
        await reaper.stop()
        assert reaper.is_running is False

    @pytest.mark.asyncio
    async def test_stop_idempotent(self) -> None:
        """Calling stop() twice must not raise."""
        config = HubConfig(idle_ttl_seconds=60)
        reaper = _make_reaper(config, AsyncMock(), [_lazy_cfg("svc")])

        await reaper.start()
        await reaper.stop()
        await reaper.stop()  # second call must not raise

    @pytest.mark.asyncio
    async def test_reaper_loop_calls_check_and_evict_after_sleep(self) -> None:
        """Reaper wakes up after interval and checks for idle backends."""
        clock = FakeClock(0.0)
        config = HubConfig(idle_ttl_seconds=60)
        evict_mock = AsyncMock()
        sleep_fn = FakeSleep()
        backends = [_lazy_cfg("lazy-b")]
        reaper = _make_reaper(
            config, evict_mock, backends,
            clock=clock, sleep_fn=sleep_fn, interval=30.0,
        )

        reaper.seed_activity("lazy-b")
        # After interval, clock is past TTL.
        clock.advance(999.0)

        await reaper.start()

        # Yield to the event loop so the task can run one iteration.
        # The FakeSleep completes instantly, so the loop runs immediately.
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # second yield ensures _check_and_evict runs

        await reaper.stop()

        # The evict_fn should have been called.
        evict_mock.assert_awaited_with("lazy-b")

    @pytest.mark.asyncio
    async def test_no_task_leak_after_stop(self) -> None:
        """After stop(), no asyncio tasks created by the reaper remain pending."""
        config = HubConfig(idle_ttl_seconds=60)
        reaper = _make_reaper(config, AsyncMock(), [_lazy_cfg("svc")])

        await reaper.start()
        task = reaper._task
        assert task is not None

        await reaper.stop()

        # Task must be done (cancelled).
        assert task.done()
        assert reaper._task is None


# ---------------------------------------------------------------------------
# 7. Manager.mark_activity delegates to reaper
# ---------------------------------------------------------------------------

class TestManagerMarkActivity:
    def test_manager_has_mark_activity_method(self, tmp_path) -> None:
        """ConnectionManager exposes mark_activity() for router to call."""
        config = HubConfig(config_dir=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        # Must not raise.
        mgr.mark_activity("some-backend")

    def test_manager_mark_activity_updates_reaper_timestamp(self, tmp_path) -> None:
        """mark_activity() must update the reaper's last_activity dict."""
        config = HubConfig(config_dir=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        mgr.mark_activity("svc-a")
        assert "svc-a" in mgr._reaper._last_activity

    @pytest.mark.asyncio
    async def test_manager_disconnect_all_stops_reaper(self, tmp_path) -> None:
        """disconnect_all() must stop the reaper (no task leak)."""
        lazy_srv = MCPServerConfig(
            name="lazy-s", transport="stdio", command="echo", spawn="lazy"
        )
        config = HubConfig(
            config_dir=tmp_path,
            idle_ttl_seconds=60,
            mcp_servers=(lazy_srv,),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        # Manually start the reaper (bypassing connect_all).
        await mgr._reaper.start()
        assert mgr._reaper.is_running is True

        await mgr.disconnect_all()

        assert mgr._reaper.is_running is False


# ---------------------------------------------------------------------------
# 8. Router calls activity_fn on successful routes; NOT on failures
# ---------------------------------------------------------------------------

class TestRouterActivityTracking:
    def _make_registry_with_tool(
        self, server_name: str, tool_name: str
    ) -> CapabilityRegistry:
        registry = CapabilityRegistry()
        registry.sync({
            server_name: {
                "tools": [{"name": tool_name, "description": "test"}],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            }
        })
        return registry

    def _make_registry_with_resource(
        self, server_name: str, raw_uri: str
    ) -> CapabilityRegistry:
        """Register a resource with the given raw URI (un-namespaced).

        The registry will namespace it as ``{server_name}__{raw_uri}``.
        Pass the namespaced form to ``route_resource_read``.
        """
        registry = CapabilityRegistry()
        registry.sync({
            server_name: {
                "tools": [],
                "resources": [{"uri": raw_uri, "name": "res", "description": "d"}],
                "resource_templates": [],
                "prompts": [],
            }
        })
        return registry

    def _make_registry_with_prompt(
        self, server_name: str, prompt_name: str
    ) -> CapabilityRegistry:
        registry = CapabilityRegistry()
        registry.sync({
            server_name: {
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "prompts": [{"name": prompt_name, "description": "d"}],
            }
        })
        return registry

    @pytest.mark.asyncio
    async def test_route_tool_call_success_marks_activity(self) -> None:
        """Successful tool route triggers activity_fn."""
        from slm_mcp_hub.federation.router import FederationRouter

        conn = _fake_conn("svc", ["svc__my_tool"])
        registry = self._make_registry_with_tool("svc", "my_tool")
        activity_calls: list[str] = []
        router = FederationRouter(
            registry,
            {"svc": conn},
            activity_fn=activity_calls.append,
        )

        await router.route_tool_call("svc__my_tool", {})

        assert "svc" in activity_calls

    @pytest.mark.asyncio
    async def test_route_tool_call_not_found_no_activity(self) -> None:
        """Tool not found → no activity tracked."""
        from slm_mcp_hub.federation.router import FederationRouter

        registry = CapabilityRegistry()
        activity_calls: list[str] = []
        router = FederationRouter(
            registry,
            {},
            activity_fn=activity_calls.append,
        )

        await router.route_tool_call("nonexistent__tool", {})

        assert activity_calls == []

    @pytest.mark.asyncio
    async def test_route_tool_call_not_connected_no_activity(self) -> None:
        """Not-connected server → no activity tracked."""
        from slm_mcp_hub.federation.router import FederationRouter

        conn = _fake_conn("svc", ["svc__tool"], connected=False)
        registry = self._make_registry_with_tool("svc", "tool")
        activity_calls: list[str] = []
        router = FederationRouter(
            registry,
            {"svc": conn},
            activity_fn=activity_calls.append,
        )

        await router.route_tool_call("svc__tool", {})

        assert activity_calls == []

    @pytest.mark.asyncio
    async def test_route_resource_read_success_marks_activity(self) -> None:
        """Successful resource read triggers activity_fn.

        Resources are namespaced as ``{server}__<raw-uri>`` by the registry,
        so we register raw URI ``"res/path"`` and look up ``"svc__res/path"``.
        """
        from slm_mcp_hub.federation.router import FederationRouter

        conn = _fake_conn("svc", [])
        registry = self._make_registry_with_resource("svc", "res/path")
        activity_calls: list[str] = []
        router = FederationRouter(
            registry,
            {"svc": conn},
            activity_fn=activity_calls.append,
        )

        await router.route_resource_read("svc__res/path")

        assert "svc" in activity_calls

    @pytest.mark.asyncio
    async def test_route_prompt_get_success_marks_activity(self) -> None:
        """Successful prompt get triggers activity_fn.

        Prompts are namespaced as ``{server}__<raw-name>`` by the registry,
        so we register raw name ``"my_prompt"`` and look up ``"svc__my_prompt"``.
        """
        from slm_mcp_hub.federation.router import FederationRouter

        conn = _fake_conn("svc", [])
        registry = self._make_registry_with_prompt("svc", "my_prompt")
        activity_calls: list[str] = []
        router = FederationRouter(
            registry,
            {"svc": conn},
            activity_fn=activity_calls.append,
        )

        await router.route_prompt_get("svc__my_prompt", {})

        assert "svc" in activity_calls

    @pytest.mark.asyncio
    async def test_router_without_activity_fn_works_normally(self) -> None:
        """Router without activity_fn does not raise (backward compatible)."""
        from slm_mcp_hub.federation.router import FederationRouter

        conn = _fake_conn("svc", ["svc__tool"])
        registry = self._make_registry_with_tool("svc", "tool")
        router = FederationRouter(registry, {"svc": conn})  # no activity_fn

        result = await router.route_tool_call("svc__tool", {})
        assert result.success is True


# ---------------------------------------------------------------------------
# 9. In-flight guard: in-flight backends are NEVER evicted
# ---------------------------------------------------------------------------

class TestInflightEvictionGuard:
    """A backend with an in-flight routed call must never be evicted, no matter
    how far past the idle TTL it is — evict()'s 5s drain grace would force-kill
    a long-running (e.g. 30-min) call otherwise."""

    @pytest.mark.asyncio
    async def test_inflight_lazy_backend_not_evicted_past_ttl(self) -> None:
        config = HubConfig(idle_ttl_seconds=60)
        clock = FakeClock(0.0)
        evict_mock = AsyncMock()
        reaper = _make_reaper(
            config,
            evict_mock,
            [_lazy_cfg("busy")],
            clock=clock,
            has_inflight_fn=lambda n: True,  # always in-flight
        )
        reaper.seed_activity("busy")
        clock.advance(10_000)  # far past the TTL

        await reaper._check_and_evict()

        evict_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backend_evicted_only_once_inflight_clears(self) -> None:
        config = HubConfig(idle_ttl_seconds=60)
        clock = FakeClock(0.0)
        evict_mock = AsyncMock()
        inflight = {"busy": True}
        reaper = _make_reaper(
            config,
            evict_mock,
            [_lazy_cfg("busy")],
            clock=clock,
            has_inflight_fn=lambda n: inflight.get(n, False),
        )
        reaper.seed_activity("busy")
        clock.advance(10_000)

        await reaper._check_and_evict()
        evict_mock.assert_not_awaited()  # in-flight → skipped

        inflight["busy"] = False  # the call finished
        await reaper._check_and_evict()
        evict_mock.assert_awaited_once_with("busy")  # now evictable


# ---------------------------------------------------------------------------
# 10. evict() forgets activity so a reconnect re-seeds fresh
# ---------------------------------------------------------------------------

class TestEvictForgetsActivity:
    @pytest.mark.asyncio
    async def test_evict_forgets_activity_timestamp(self, tmp_path) -> None:
        """After evict(), the stale timestamp is gone so a later reconnect
        (route OR manager.reconnect/admin warm) re-seeds a FRESH one instead of
        inheriting a stale value and being reaped on the very next sweep."""
        lazy_srv = MCPServerConfig(
            name="lz", transport="stdio", command="echo", spawn="lazy"
        )
        config = HubConfig(config_dir=tmp_path, idle_ttl_seconds=60, mcp_servers=(lazy_srv,))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)
        conn = _fake_conn("lz", ["lz__t"])
        mgr._connections["lz"] = conn
        mgr._reaper.seed_activity("lz")
        assert "lz" in mgr._reaper._last_activity

        await mgr.evict("lz")

        assert "lz" not in mgr._reaper._last_activity, (
            "evict() must forget activity so a reconnect re-seeds a fresh timestamp"
        )

    @pytest.mark.asyncio
    async def test_reseed_after_forget_sets_fresh_timestamp(self) -> None:
        """seed_activity re-seeds fresh once forget() has cleared the entry —
        proving the evict→forget→reconnect cycle yields a current timestamp."""
        config = HubConfig(idle_ttl_seconds=60)
        clock = FakeClock(1000.0)
        reaper = _make_reaper(config, AsyncMock(), [_lazy_cfg("lz")], clock=clock)
        reaper.seed_activity("lz")
        assert reaper._last_activity["lz"] == 1000.0
        clock.advance(5000)  # would be far past TTL if the stale value survived

        reaper.forget("lz")  # what evict() now does
        reaper.seed_activity("lz")  # what the reconnect does

        assert reaper._last_activity["lz"] == 6000.0  # fresh, not the stale 1000.0


# ---------------------------------------------------------------------------
# 11. W3-P2: router marks activity at COMPLETION (incl. the error path)
# ---------------------------------------------------------------------------

class TestRouterActivityAtCompletion:
    @staticmethod
    def _registry_with_tool(server: str, tool: str) -> CapabilityRegistry:
        registry = CapabilityRegistry()
        registry.sync({
            server: {
                "tools": [{"name": tool, "description": "d"}],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            }
        })
        return registry

    @pytest.mark.asyncio
    async def test_route_tool_call_error_still_marks_activity(self) -> None:
        """A live backend whose tool call RAISES is still in-use — activity is
        marked in the finally so it is not treated as idle."""
        from slm_mcp_hub.federation.router import FederationRouter

        conn = _fake_conn("svc", ["svc__t"])
        conn.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        registry = self._registry_with_tool("svc", "t")
        activity_calls: list[str] = []
        router = FederationRouter(
            registry, {"svc": conn}, activity_fn=activity_calls.append
        )

        result = await router.route_tool_call("svc__t", {})

        assert result.success is False
        assert "svc" in activity_calls  # marked in finally despite the error


# ---------------------------------------------------------------------------
# 12. W3-P2: reaper loop survives a sweep fault; re-raises cancellation
# ---------------------------------------------------------------------------

class _StopLoop(Exception):
    """Test sentinel to break the reaper's infinite loop deterministically."""


class TestReaperLoopRobustness:
    @pytest.mark.asyncio
    async def test_sweep_exception_does_not_kill_reaper(self) -> None:
        """A transient fault in a sweep is logged and swallowed; the reaper
        keeps sweeping (one bad sweep must not kill it for the hub's lifetime)."""
        from slm_mcp_hub.federation.eviction import IdleReaper

        config = HubConfig(idle_ttl_seconds=60)
        sweeps = {"n": 0}

        def flaky_backends() -> list[MCPServerConfig]:
            sweeps["n"] += 1
            if sweeps["n"] == 1:
                raise RuntimeError("transient sweep fault")
            return []

        class StoppingSleep:
            def __init__(self) -> None:
                self.count = 0

            async def __call__(self, seconds: float) -> None:
                self.count += 1
                if self.count > 3:
                    raise _StopLoop
                await asyncio.sleep(0)  # yield so cancellation/stop can interleave

        reaper = IdleReaper(
            config=config,
            evict_fn=AsyncMock(),
            get_backends_fn=flaky_backends,
            is_live_fn=lambda n: True,
            sleep_fn=StoppingSleep(),
            time_fn=FakeClock(0.0),
        )

        with pytest.raises(_StopLoop):
            await reaper._loop()

        assert sweeps["n"] >= 2, "reaper must survive a sweep fault and keep sweeping"

    @pytest.mark.asyncio
    async def test_sweep_cancellederror_is_reraised(self) -> None:
        """A CancelledError raised inside a sweep must propagate (so stop() can
        cancel cleanly) — it is NOT swallowed by the robustness guard."""
        from slm_mcp_hub.federation.eviction import IdleReaper

        config = HubConfig(idle_ttl_seconds=60)

        def cancel_backends() -> list[MCPServerConfig]:
            raise asyncio.CancelledError

        reaper = IdleReaper(
            config=config,
            evict_fn=AsyncMock(),
            get_backends_fn=cancel_backends,
            is_live_fn=lambda n: True,
            sleep_fn=FakeSleep(),
            time_fn=FakeClock(0.0),
        )

        with pytest.raises(asyncio.CancelledError):
            await reaper._loop()


# ---------------------------------------------------------------------------
# 13. W3-P2: manager wires has_inflight_fn to the connection's in_flight_count
# ---------------------------------------------------------------------------

class TestManagerInflightWiring:
    def test_reaper_has_inflight_reads_connection_count(self, tmp_path) -> None:
        config = HubConfig(config_dir=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)
        conn = _fake_conn("svc", ["svc__t"])
        mgr._connections["svc"] = conn

        conn.in_flight_count = 0
        assert mgr._reaper._has_inflight_fn("svc") is False

        conn.in_flight_count = 2
        assert mgr._reaper._has_inflight_fn("svc") is True

        # Unknown backend must not raise (KeyError-safe).
        assert mgr._reaper._has_inflight_fn("ghost") is False
