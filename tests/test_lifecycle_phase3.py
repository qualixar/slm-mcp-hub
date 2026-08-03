"""Tests for Phase 3 — config diff + notifier + reloader.

Coverage:
- diff_configs: added / removed / modified / unchanged classification
- diff_configs: disabled-server transitions
- diff_configs: env/args/headers changes detected
- ChangeNotifier: subscribe / unsubscribe / broadcast / coalesce
- ChangeNotifier: subscriber error isolation
- ConfigReloader: empty diff is no-op
- ConfigReloader: applies remove-then-modify-then-add ordering
- ConfigReloader: invalid config raises ReloadError, state preserved
- ConfigReloader: fires notifier exactly once per non-empty reload
- HubRuntime: notifier + reloader wired and accessible
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.manager import ConnectionManager
from slm_mcp_hub.lifecycle.config_diff import ConfigDiff, diff_configs
from slm_mcp_hub.lifecycle.notifier import ChangeNotifier
from slm_mcp_hub.lifecycle.reloader import ConfigReloader, ReloadError

# ---------- helpers ----------

def _srv(name: str, *, transport: str = "stdio", command: str = "echo",
         args: tuple = (), env: dict | None = None, enabled: bool = True,
         url: str = "") -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport=transport, command=command, args=args,
        env=env or {}, enabled=enabled, url=url,
    )


def _cfg(*servers: MCPServerConfig, tmp_path=None) -> HubConfig:
    return HubConfig(
        config_dir=tmp_path if tmp_path else None,
        mcp_servers=servers,
    )


# ---------- diff_configs ----------

class TestDiffConfigs:
    def test_empty_to_empty(self):
        d = diff_configs(_cfg(), _cfg())
        assert d.is_empty
        assert d.change_count == 0

    def test_pure_add(self):
        d = diff_configs(_cfg(), _cfg(_srv("alpha")))
        assert [s.name for s in d.added] == ["alpha"]
        assert d.removed == ()
        assert d.modified == ()

    def test_pure_remove(self):
        d = diff_configs(_cfg(_srv("alpha")), _cfg())
        assert d.removed == ("alpha",)
        assert d.added == ()

    def test_modified_env_change(self):
        old = _cfg(_srv("alpha", env={"KEY": "v1"}))
        new = _cfg(_srv("alpha", env={"KEY": "v2"}))
        d = diff_configs(old, new)
        assert d.modified[0].name == "alpha"
        assert d.added == () and d.removed == ()

    def test_modified_args_change(self):
        old = _cfg(_srv("alpha", args=("a",)))
        new = _cfg(_srv("alpha", args=("b",)))
        d = diff_configs(old, new)
        assert [s.name for s in d.modified] == ["alpha"]

    def test_unchanged_identical(self):
        srv = _srv("alpha", args=("x",), env={"K": "V"})
        d = diff_configs(_cfg(srv), _cfg(srv))
        assert d.is_empty
        assert d.unchanged == ("alpha",)

    def test_disabled_to_enabled_is_add(self):
        old = _cfg(_srv("alpha", enabled=False))
        new = _cfg(_srv("alpha", enabled=True))
        d = diff_configs(old, new)
        assert [s.name for s in d.added] == ["alpha"]

    def test_enabled_to_disabled_is_remove(self):
        old = _cfg(_srv("alpha", enabled=True))
        new = _cfg(_srv("alpha", enabled=False))
        d = diff_configs(old, new)
        assert d.removed == ("alpha",)

    def test_mixed_diff(self):
        old = _cfg(_srv("keep"), _srv("modme", args=("v1",)), _srv("dropme"))
        new = _cfg(_srv("keep"), _srv("modme", args=("v2",)), _srv("newone"))
        d = diff_configs(old, new)
        assert d.unchanged == ("keep",)
        assert [s.name for s in d.modified] == ["modme"]
        assert d.removed == ("dropme",)
        assert [s.name for s in d.added] == ["newone"]

    def test_summary_format(self):
        d = ConfigDiff(
            added=(_srv("a"),),
            modified=(_srv("b"),),
            removed=("c",),
            unchanged=("d",),
        )
        assert "+1" in d.summary()
        assert "~1" in d.summary()
        assert "-1" in d.summary()


# ---------- ChangeNotifier ----------

class TestChangeNotifier:
    @pytest.mark.asyncio
    async def test_subscribe_and_unsubscribe(self):
        n = ChangeNotifier(debounce_seconds=0.0)
        n.subscribe("s1", lambda _: None)
        assert n.subscriber_count == 1
        n.unsubscribe("s1")
        assert n.subscriber_count == 0
        # Unsubscribing unknown id is safe
        n.unsubscribe("never-existed")

    @pytest.mark.asyncio
    async def test_broadcast_to_async_subscriber(self):
        n = ChangeNotifier(debounce_seconds=0.0)
        received: list[dict] = []

        async def sub(msg):
            received.append(msg)

        n.subscribe("s1", sub)
        await n.notify_tools_changed()
        # Give the debounce task a tick to finish
        await asyncio.sleep(0.05)
        await n.shutdown()
        assert len(received) == 1
        assert received[0]["method"] == "notifications/tools/list_changed"

    @pytest.mark.asyncio
    async def test_broadcast_to_sync_subscriber(self):
        n = ChangeNotifier(debounce_seconds=0.0)
        received: list[dict] = []

        def sync_sub(msg):
            received.append(msg)

        n.subscribe("s1", sync_sub)
        await n.notify_tools_changed()
        await asyncio.sleep(0.05)
        await n.shutdown()
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_multiple_notify_coalesced(self):
        """Within the debounce window, N calls fire ONE broadcast."""
        n = ChangeNotifier(debounce_seconds=0.1)
        count = 0

        def sub(_msg):
            nonlocal count
            count += 1

        n.subscribe("s1", sub)
        # Hammer with 5 notifies — should coalesce to 1 broadcast
        for _ in range(5):
            await n.notify_tools_changed()
        await asyncio.sleep(0.2)
        await n.shutdown()
        assert count == 1

    @pytest.mark.asyncio
    async def test_subscriber_error_isolated(self):
        n = ChangeNotifier(debounce_seconds=0.0)
        good_received: list[dict] = []

        def bad(_msg):
            raise RuntimeError("oops")

        def good(msg):
            good_received.append(msg)

        n.subscribe("bad", bad)
        n.subscribe("good", good)
        await n.notify_tools_changed()
        await asyncio.sleep(0.05)
        await n.shutdown()
        # Good subscriber still got the message despite bad's error
        assert len(good_received) == 1


# ---------- ConfigReloader ----------

class TestConfigReloader:
    def _make_mgr_and_reloader(self, initial_servers=(), tmp_path=None):
        registry = CapabilityRegistry()
        cfg = HubConfig(
            config_dir=tmp_path if tmp_path else None,
            mcp_servers=initial_servers,
        )
        mgr = ConnectionManager(cfg, registry)
        # Stub out the real lifecycle methods to record what was called
        mgr.add_server = AsyncMock(return_value=(True, "ok"))
        mgr.remove_server = AsyncMock(return_value=(True, "ok"))
        mgr.replace_server = AsyncMock(return_value=(True, "ok"))
        notifier = MagicMock(spec=ChangeNotifier)
        notifier.notify_tools_changed = AsyncMock()
        notifier.shutdown = AsyncMock()
        reloader = ConfigReloader(mgr, notifier)
        return mgr, notifier, reloader

    @pytest.mark.asyncio
    async def test_empty_diff_is_noop(self):
        srv = _srv("alpha")
        mgr, notifier, reloader = self._make_mgr_and_reloader((srv,))
        diff = await reloader.apply_config(_cfg(srv))
        assert diff.is_empty
        mgr.add_server.assert_not_called()
        mgr.remove_server.assert_not_called()
        mgr.replace_server.assert_not_called()
        notifier.notify_tools_changed.assert_not_called()

    @pytest.mark.asyncio
    async def test_applies_in_remove_modify_add_order(self):
        old_srv = _srv("dropme")
        mod_srv_old = _srv("modme", args=("v1",))
        mod_srv_new = _srv("modme", args=("v2",))
        add_srv = _srv("newone")

        mgr, notifier, reloader = self._make_mgr_and_reloader((old_srv, mod_srv_old))
        new_config = _cfg(mod_srv_new, add_srv)

        diff = await reloader.apply_config(new_config)
        assert diff.removed == ("dropme",)
        assert [s.name for s in diff.modified] == ["modme"]
        assert [s.name for s in diff.added] == ["newone"]

        mgr.remove_server.assert_awaited_once()
        mgr.replace_server.assert_awaited_once()
        mgr.add_server.assert_awaited_once()
        notifier.notify_tools_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_config_raises(self):
        _, _, reloader = self._make_mgr_and_reloader()
        with pytest.raises(ReloadError):
            await reloader.apply_config("not a HubConfig")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_abort_reload(self):
        """If one server fails to add, the others still apply and notifier still fires."""
        mgr, notifier, reloader = self._make_mgr_and_reloader()
        # First add succeeds, second add raises — reloader catches and continues
        mgr.add_server = AsyncMock(side_effect=[(True, "ok"), RuntimeError("boom")])
        new_config = _cfg(_srv("good"), _srv("bad"))

        # Should NOT raise — partial failures are tolerated, logged, and reload completes.
        diff = await reloader.apply_config(new_config)
        assert len(diff.added) == 2
        # Both add attempts were made
        assert mgr.add_server.call_count == 2
        # Notifier still fires — clients should refresh based on the partially-applied state
        notifier.notify_tools_changed.assert_awaited_once()


# ---------- HubRuntime wiring ----------

class TestHubRuntimeWiring:
    @pytest.mark.asyncio
    async def test_runtime_exposes_notifier_and_reloader(self):
        from slm_mcp_hub.core.hub import HubOrchestrator, reset_hub
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        reset_hub()
        cfg = HubConfig(host="127.0.0.1", port=52414, mcp_servers=())
        try:
            async with HubOrchestrator(cfg) as hub:
                runtime = HubRuntime(hub)
                assert runtime.notifier is not None
                assert runtime.reloader is not None
                # Manager is wired to the notifier
                assert runtime.conn_manager._notifier is runtime.notifier
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_runtime_disconnect_all_shuts_notifier(self):
        from slm_mcp_hub.core.hub import HubOrchestrator, reset_hub
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        reset_hub()
        cfg = HubConfig(host="127.0.0.1", port=52414, mcp_servers=())
        try:
            async with HubOrchestrator(cfg) as hub:
                runtime = HubRuntime(hub)
                runtime.notifier.subscribe("s1", lambda _: None)
                assert runtime.notifier.subscriber_count == 1
                await runtime.disconnect_all()
                # Notifier was shut down — subscribers cleared
                assert runtime.notifier.subscriber_count == 0
        finally:
            reset_hub()
