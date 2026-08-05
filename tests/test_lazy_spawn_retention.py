"""W3-P1 — Spawn policy + capability retention across eviction.

Test strategy (TDD — RED first):
1. Config: `spawn` field (eager/lazy/pinned), `is_pinned` property,
   `idle_ttl_seconds` and `max_live_backends` on HubConfig.
2. Retention: evicted backend's tools stay in registry/search_tools;
   failed/removed backend's tools drop.
3. Reconnect: live caps take over, evicted cache cleared.
4. Connect failure: _evicted_caps NOT populated.
5. Status: evicted backend reported as stopped/not-live with tool count.
6. Pinned guard: evicting a pinned backend is a no-op (safe, logged).

All tests use fake MCPConnection-like objects; no real subprocesses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import (
    ConfigValidationError,
    HubConfig,
    MCPServerConfig,
    load_config,
    parse_mcp_server,
    save_config,
    validate_server_config,
)
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.manager import ConnectionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_conn(name: str, tools: list[str], *, connected: bool = True) -> MagicMock:
    """Return a fake MCPConnection-like object with stable capabilities."""
    from slm_mcp_hub.federation.connection import ConnectionState

    mock = MagicMock()
    mock.name = name
    mock.is_connected = connected
    mock.is_auth_required = False
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
    return mock


def _server_cfg(name: str, spawn: str = "eager") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command="echo",
        spawn=spawn,
    )


# ---------------------------------------------------------------------------
# 1. CONFIG — spawn field
# ---------------------------------------------------------------------------

class TestSpawnField:
    def test_default_spawn_is_eager(self) -> None:
        cfg = MCPServerConfig(name="s", transport="stdio")
        assert cfg.spawn == "eager"

    def test_spawn_lazy_accepted(self) -> None:
        cfg = MCPServerConfig(name="s", transport="stdio", spawn="lazy")
        assert cfg.spawn == "lazy"

    def test_spawn_pinned_accepted(self) -> None:
        cfg = MCPServerConfig(name="s", transport="stdio", spawn="pinned")
        assert cfg.spawn == "pinned"

    def test_spawn_invalid_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="spawn"):
            validate_server_config(
                MCPServerConfig(name="s", transport="stdio", spawn="invalid")
            )

    def test_spawn_is_pinned_via_spawn_field(self) -> None:
        cfg = MCPServerConfig(name="s", transport="stdio", spawn="pinned")
        assert cfg.is_pinned is True

    def test_spawn_is_pinned_via_always_on(self) -> None:
        cfg = MCPServerConfig(name="s", transport="stdio", always_on=True, spawn="eager")
        assert cfg.is_pinned is True

    def test_spawn_is_not_pinned(self) -> None:
        cfg = MCPServerConfig(name="s", transport="stdio", spawn="lazy")
        assert cfg.is_pinned is False

    def test_parse_mcp_server_spawn_field(self) -> None:
        raw = {"command": "echo", "args": [], "spawn": "lazy"}
        cfg = parse_mcp_server("my-server", raw)
        assert cfg.spawn == "lazy"

    def test_parse_mcp_server_default_spawn(self) -> None:
        raw = {"command": "echo", "args": []}
        cfg = parse_mcp_server("my-server", raw)
        assert cfg.spawn == "eager"

    def test_save_and_load_roundtrip_spawn(self, tmp_path) -> None:
        """spawn=lazy must survive a save→load round-trip."""
        cfg = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(
                MCPServerConfig(name="s", transport="stdio", command="echo", spawn="lazy"),
            ),
        )
        config_path = tmp_path / "config.json"
        save_config(cfg, config_path)
        loaded = load_config(config_path)
        assert loaded.mcp_servers[0].spawn == "lazy"


# ---------------------------------------------------------------------------
# 2. CONFIG — HubConfig: idle_ttl_seconds and max_live_backends
# ---------------------------------------------------------------------------

class TestHubConfigW3Fields:
    def test_default_idle_ttl_is_300(self) -> None:
        cfg = HubConfig()
        assert cfg.idle_ttl_seconds == 300

    def test_default_max_live_backends_is_zero(self) -> None:
        cfg = HubConfig()
        assert cfg.max_live_backends == 0

    def test_zero_idle_ttl_means_never_evict(self) -> None:
        cfg = HubConfig(idle_ttl_seconds=0)
        assert cfg.idle_ttl_seconds == 0

    def test_max_live_backends_custom(self) -> None:
        cfg = HubConfig(max_live_backends=5)
        assert cfg.max_live_backends == 5

    def test_idle_ttl_negative_rejected(self) -> None:
        with pytest.raises((ValueError, ConfigValidationError)):
            HubConfig(idle_ttl_seconds=-1)

    def test_max_live_backends_negative_rejected(self) -> None:
        with pytest.raises((ValueError, ConfigValidationError)):
            HubConfig(max_live_backends=-1)

    def test_hub_config_roundtrip_new_fields(self, tmp_path) -> None:
        """idle_ttl_seconds and max_live_backends survive save→load."""
        cfg = HubConfig(
            config_dir=tmp_path,
            idle_ttl_seconds=120,
            max_live_backends=10,
        )
        config_path = tmp_path / "config.json"
        save_config(cfg, config_path)
        loaded = load_config(config_path)
        assert loaded.idle_ttl_seconds == 120
        assert loaded.max_live_backends == 10

    def test_env_override_idle_ttl(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_IDLE_TTL_SECONDS", "600")
        cfg = load_config(tmp_path / "nonexistent.json")
        assert cfg.idle_ttl_seconds == 600

    def test_env_override_max_live_backends(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_MAX_LIVE_BACKENDS", "15")
        cfg = load_config(tmp_path / "nonexistent.json")
        assert cfg.max_live_backends == 15


# ---------------------------------------------------------------------------
# 3. MANAGER — evict() method and _evicted_caps cache
# ---------------------------------------------------------------------------

class TestEvictMethod:
    @pytest.mark.asyncio
    async def test_evict_stores_caps_before_disconnect(self, tmp_path) -> None:
        """evict() caches the connection's capabilities before teardown."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("alpha", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("alpha", ["alpha__tool_one"])
        mgr._connections["alpha"] = conn

        await mgr.evict("alpha")

        # Capabilities must be cached
        assert "alpha" in mgr._evicted_caps
        assert mgr._evicted_caps["alpha"]["tools"][0]["name"] == "alpha__tool_one"

    @pytest.mark.asyncio
    async def test_evict_disconnects_connection(self, tmp_path) -> None:
        """evict() tears down the connection (freeing RAM)."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("beta", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("beta", ["beta__tool_x"])
        mgr._connections["beta"] = conn

        await mgr.evict("beta")

        # disconnect must have been called
        conn.drain_and_disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_evict_leaves_connection_in_stopped_state(self, tmp_path) -> None:
        """After eviction, the connection object is still in _connections but not live."""

        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("gamma", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("gamma", ["gamma__tool"])
        mgr._connections["gamma"] = conn

        await mgr.evict("gamma")

        # After eviction: connection exists but is not live
        stored = mgr._connections.get("gamma")
        assert stored is not None
        # The fake conn's is_connected should be False after eviction OR
        # the manager should track eviction independently
        assert "gamma" in mgr._evicted_caps

    @pytest.mark.asyncio
    async def test_evict_idempotent_already_absent(self, tmp_path) -> None:
        """Evicting a backend not in _connections is a safe no-op."""
        config = HubConfig(config_dir=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        # Must not raise
        await mgr.evict("nonexistent")
        assert "nonexistent" not in mgr._evicted_caps

    @pytest.mark.asyncio
    async def test_evict_pinned_backend_is_noop(self, tmp_path) -> None:
        """Evicting a pinned backend (spawn=pinned OR always_on) is a no-op."""
        pinned_cfg = MCPServerConfig(
            name="pinned-svc", transport="stdio", command="echo", spawn="pinned"
        )
        config = HubConfig(config_dir=tmp_path, mcp_servers=(pinned_cfg,))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("pinned-svc", ["pinned__tool"])
        mgr._connections["pinned-svc"] = conn

        await mgr.evict("pinned-svc")

        # Pinned: should not evict, drain not called
        conn.drain_and_disconnect.assert_not_awaited()
        assert "pinned-svc" not in mgr._evicted_caps

    @pytest.mark.asyncio
    async def test_evict_always_on_backend_is_noop(self, tmp_path) -> None:
        """always_on=True maps to pinned — eviction must be refused."""
        always_on_cfg = MCPServerConfig(
            name="always-hot", transport="stdio", command="echo", always_on=True
        )
        config = HubConfig(config_dir=tmp_path, mcp_servers=(always_on_cfg,))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("always-hot", ["hot__tool"])
        mgr._connections["always-hot"] = conn

        await mgr.evict("always-hot")

        conn.drain_and_disconnect.assert_not_awaited()
        assert "always-hot" not in mgr._evicted_caps


# ---------------------------------------------------------------------------
# 4. REGISTRY RETENTION — evicted tools remain discoverable
# ---------------------------------------------------------------------------

class TestRegistryRetention:
    @pytest.mark.asyncio
    async def test_evicted_tool_stays_in_registry(self, tmp_path) -> None:
        """After eviction, the tool must still appear in registry.list_tools()."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("svc", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        # Seed the connection as live
        conn = _fake_conn("svc", ["my_tool"])
        conn.is_connected = True
        mgr._connections["svc"] = conn
        mgr._sync_registry()

        # Confirm the tool is present while live
        tool_names_before = [t["name"] for t in registry.list_tools()]
        assert any("my_tool" in n for n in tool_names_before), (
            f"Expected my_tool in registry before eviction, got {tool_names_before}"
        )

        # Evict
        conn.is_connected = False  # simulate teardown
        await mgr.evict("svc")

        # Tool must remain discoverable after eviction
        tool_names_after = [t["name"] for t in registry.list_tools()]
        assert any("my_tool" in n for n in tool_names_after), (
            f"Expected my_tool STILL in registry after eviction, got {tool_names_after}"
        )

    @pytest.mark.asyncio
    async def test_evicted_tool_discovered_by_search(self, tmp_path) -> None:
        """search_tools must return an evicted backend's tool (not-live, but cached)."""
        from slm_mcp_hub.federation.router import FederationRouter
        from slm_mcp_hub.protocol.product_operations import HubProductOperations

        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("svc2", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("svc2", ["magic_tool"])
        conn.is_connected = True
        mgr._connections["svc2"] = conn
        mgr._sync_registry()

        conn.is_connected = False
        await mgr.evict("svc2")

        # search_tools must find the cached tool
        router = FederationRouter(registry, mgr)
        ops = HubProductOperations(registry, router)
        outcome = await ops.search_tools({"query": "magic_tool"})
        import json as _json
        result = _json.loads(outcome.content[0]["text"])
        found_names = [t["tool"] for t in result["tools"]]
        assert any("magic_tool" in n for n in found_names), (
            f"Expected magic_tool in search_tools result; got {found_names}"
        )

    @pytest.mark.asyncio
    async def test_failed_backend_tools_drop_from_registry(self, tmp_path) -> None:
        """A failed (not evicted) backend's tools must be GONE from the registry."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("failing", "eager"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("failing", ["failure_tool"])
        conn.is_connected = True
        mgr._connections["failing"] = conn
        mgr._sync_registry()

        # Simulate a failure (not an eviction) — just mark disconnected
        conn.is_connected = False
        mgr._sync_registry()  # re-sync without eviction

        # Tools must be gone — failure, not eviction
        tool_names = [t["name"] for t in registry.list_tools()]
        assert not any("failure_tool" in n for n in tool_names), (
            f"Expected failure_tool GONE after failure; got {tool_names}"
        )

    @pytest.mark.asyncio
    async def test_removed_backend_tools_drop_from_registry(self, tmp_path) -> None:
        """remove_server() must clear evicted caps AND drop tools from registry."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("removable", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("removable", ["removable_tool"])
        conn.is_connected = True
        mgr._connections["removable"] = conn
        mgr._sync_registry()

        # First evict so _evicted_caps is populated
        conn.is_connected = False
        await mgr.evict("removable")

        # Verify tool is still cached after eviction
        tool_names_evicted = [t["name"] for t in registry.list_tools()]
        assert any("removable_tool" in n for n in tool_names_evicted), (
            f"Expected removable_tool in registry (evicted cache); got {tool_names_evicted}"
        )

        # Now remove the server permanently
        await mgr.remove_server("removable")

        # Tools must now be gone
        tool_names_removed = [t["name"] for t in registry.list_tools()]
        assert not any("removable_tool" in n for n in tool_names_removed), (
            f"Expected removable_tool GONE after remove_server; got {tool_names_removed}"
        )
        assert "removable" not in mgr._evicted_caps


# ---------------------------------------------------------------------------
# 5. RECONNECT — evicted cache cleared on successful reconnect
# ---------------------------------------------------------------------------

class TestReconnectClearsCache:
    @pytest.mark.asyncio
    async def test_reconnect_clears_evicted_caps(self, tmp_path) -> None:
        """A successful reconnect must remove the backend from _evicted_caps."""
        config = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(_server_cfg("revived", "lazy"),),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        # Populate _evicted_caps manually (simulating a prior eviction)
        mgr._evicted_caps["revived"] = {
            "tools": [{"name": "old_tool", "description": "stale"}],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }

        # Mock MCPConnection so the reconnect succeeds
        live_conn = _fake_conn("revived", ["new_tool"])
        live_conn.is_connected = True

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=live_conn):
            success, _ = await mgr.reconnect("revived")

        assert success is True
        assert "revived" not in mgr._evicted_caps

    @pytest.mark.asyncio
    async def test_live_caps_override_evicted_caps_in_registry(self, tmp_path) -> None:
        """After reconnect, registry shows live tools, NOT stale evicted ones."""
        config = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(_server_cfg("freshened", "lazy"),),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        # Seed evicted caps with OLD tool
        mgr._evicted_caps["freshened"] = {
            "tools": [{"name": "old_stale_tool", "description": "stale"}],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }

        live_conn = _fake_conn("freshened", ["brand_new_tool"])
        live_conn.is_connected = True

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=live_conn):
            await mgr.reconnect("freshened")

        tool_names = [t["name"] for t in registry.list_tools()]
        # Old stale tool must be GONE
        assert not any("old_stale_tool" in n for n in tool_names), (
            f"Expected old_stale_tool gone; got {tool_names}"
        )
        # New live tool must be PRESENT
        assert any("brand_new_tool" in n for n in tool_names), (
            f"Expected brand_new_tool present; got {tool_names}"
        )

    @pytest.mark.asyncio
    async def test_connect_failure_does_not_populate_evicted_caps(self, tmp_path) -> None:
        """A failed connect attempt must NOT write to _evicted_caps."""
        config = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(_server_cfg("broken", "lazy"),),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        failing_conn = _fake_conn("broken", [])
        failing_conn.is_connected = False
        failing_conn.connect = AsyncMock(side_effect=ConnectionError("backend down"))

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=failing_conn):
            success, _ = await mgr.reconnect("broken")

        assert success is False
        # Critical: failure must NOT populate _evicted_caps
        assert "broken" not in mgr._evicted_caps, (
            "_evicted_caps must NOT be populated on connection failure"
        )


# ---------------------------------------------------------------------------
# 6. STATUS REPORTING — evicted backend appears stopped/not-live with tool count
# ---------------------------------------------------------------------------

class TestStatusReporting:
    @pytest.mark.asyncio
    async def test_get_server_status_evicted_shows_tool_count(self, tmp_path) -> None:
        """get_server_status must show an evicted backend's cached tool count."""
        config = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(_server_cfg("evicted-svc", "lazy"),),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("evicted-svc", ["tool_one", "tool_two"])
        conn.is_connected = True
        mgr._connections["evicted-svc"] = conn

        conn.is_connected = False
        await mgr.evict("evicted-svc")

        statuses = mgr.get_server_status()
        svc = next(s for s in statuses if s["name"] == "evicted-svc")

        # Not live
        assert svc["connected"] is False
        # Tool count from evicted cache
        assert svc["tools"] == 2

    @pytest.mark.asyncio
    async def test_get_server_status_evicted_lifecycle_is_stopped(self, tmp_path) -> None:
        """Evicted backend's lifecycle should be 'stopped' in status."""
        config = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(_server_cfg("evicted2", "lazy"),),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("evicted2", ["some_tool"])
        conn.is_connected = True
        mgr._connections["evicted2"] = conn

        conn.is_connected = False
        await mgr.evict("evicted2")

        statuses = mgr.get_server_status()
        svc = next(s for s in statuses if s["name"] == "evicted2")

        # lifecycle must indicate stopped (not connected, not failed)
        assert svc["lifecycle"] == "stopped"

    @pytest.mark.asyncio
    async def test_get_server_status_failed_shows_zero_tools(self, tmp_path) -> None:
        """A failed (not evicted) backend shows zero tools in status."""
        config = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(_server_cfg("failed-svc", "eager"),),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("failed-svc", ["wont_survive"])
        conn.is_connected = False  # failure state
        mgr._connections["failed-svc"] = conn
        mgr._failed["failed-svc"] = "connection error"

        statuses = mgr.get_server_status()
        svc = next(s for s in statuses if s["name"] == "failed-svc")

        assert svc["connected"] is False
        # No eviction = no cached tools count (uses live conn which reports 0)
        assert svc["tools"] == 0


# ---------------------------------------------------------------------------
# 7. IMMUTABILITY — evicted caps stored as independent copy (not reference)
# ---------------------------------------------------------------------------

class TestEvictedCapsMutationSafety:
    @pytest.mark.asyncio
    async def test_evicted_caps_are_deep_copy(self, tmp_path) -> None:
        """_evicted_caps must store a copy; mutating the original must not affect cache."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("copy-svc", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        tools_list: list[dict] = [{"name": "original_tool", "description": "d"}]
        conn = _fake_conn("copy-svc", [])
        conn.capabilities = {
            "tools": tools_list,
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }
        conn.is_connected = True
        mgr._connections["copy-svc"] = conn

        conn.is_connected = False
        await mgr.evict("copy-svc")

        # Mutate the original tools list
        tools_list.append({"name": "injected_tool", "description": "bad"})

        # Cache must NOT see the mutation
        cached_tools = mgr._evicted_caps["copy-svc"]["tools"]
        assert len(cached_tools) == 1
        assert cached_tools[0]["name"] == "original_tool"


# ---------------------------------------------------------------------------
# 8. ERROR HANDLING — drain_and_disconnect failure during evict
# ---------------------------------------------------------------------------

class TestEvictErrorHandling:
    @pytest.mark.asyncio
    async def test_evict_continues_on_drain_disconnect_error(self, tmp_path) -> None:
        """If drain_and_disconnect raises, evict() must still cache caps and not propagate."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("flaky", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("flaky", ["flaky_tool"])
        conn.drain_and_disconnect = AsyncMock(side_effect=RuntimeError("drain failed"))
        conn.is_connected = True
        mgr._connections["flaky"] = conn

        conn.is_connected = False
        # Must not raise — error is logged but suppressed
        await mgr.evict("flaky")

        # Caps must still be cached despite the drain error
        assert "flaky" in mgr._evicted_caps
        assert mgr._evicted_caps["flaky"]["tools"][0]["name"] == "flaky_tool"

    @pytest.mark.asyncio
    async def test_disconnect_all_clears_evicted_caps(self, tmp_path) -> None:
        """disconnect_all() must clear _evicted_caps so a reused manager starts clean."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("svc", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        # Seed evicted caps
        mgr._evicted_caps["svc"] = {
            "tools": [{"name": "stale_tool", "description": "old"}],
            "resources": [], "resource_templates": [], "prompts": [],
        }

        await mgr.disconnect_all()

        assert len(mgr._evicted_caps) == 0, (
            "_evicted_caps must be empty after disconnect_all"
        )

    @pytest.mark.asyncio
    async def test_disconnect_one_clears_evicted_caps(self, tmp_path) -> None:
        """disconnect_one() must also drop the evicted cap cache for that server."""
        config = HubConfig(config_dir=tmp_path, mcp_servers=(_server_cfg("svc2", "lazy"),))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(config, registry)

        conn = _fake_conn("svc2", ["gone_tool"])
        mgr._connections["svc2"] = conn
        mgr._evicted_caps["svc2"] = {
            "tools": [{"name": "gone_tool", "description": "old"}],
            "resources": [], "resource_templates": [], "prompts": [],
        }

        await mgr.disconnect_one("svc2")

        assert "svc2" not in mgr._evicted_caps, (
            "disconnect_one must clear evicted caps for the removed server"
        )
