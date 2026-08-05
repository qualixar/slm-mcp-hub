"""Second batch of targeted coverage tests.

Each class covers specific uncovered lines/branches identified by the
coverage report after the first batch in test_coverage_gaps.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# core/config.py  lines 534, 556
# ---------------------------------------------------------------------------


class TestConfigSnapshotGaps:
    """Lines 534 and 556 in config.py."""

    def test_list_snapshots_returns_empty_when_dir_absent(self, tmp_path, monkeypatch):
        """Line 534: list_snapshots() returns [] when the snapshots dir does
        not exist — the `if not snapshots_dir.exists(): return []` branch.
        """
        from slm_mcp_hub.core import config as cfg_module
        from slm_mcp_hub.core.config import list_snapshots

        # Point get_snapshots_dir at a non-existent subdir
        nonexistent = tmp_path / "no-such-dir"
        monkeypatch.setattr(cfg_module, "get_snapshots_dir", lambda: nonexistent)

        result = list_snapshots()
        assert result == []

    def test_restore_snapshot_raises_when_not_found(self, tmp_path, monkeypatch):
        """Line 556: restore_snapshot() raises FileNotFoundError when the named
        snapshot does not exist in the snapshots dir.
        """
        from slm_mcp_hub.core import config as cfg_module
        from slm_mcp_hub.core.config import restore_snapshot

        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        monkeypatch.setattr(cfg_module, "get_snapshots_dir", lambda: snapshots_dir)

        with pytest.raises(FileNotFoundError, match="Snapshot not found"):
            restore_snapshot("config-20260101T000000.json")


# ---------------------------------------------------------------------------
# core/hub.py  lines 150-151
# ---------------------------------------------------------------------------


class TestHubPluginEntryPointError:
    """Lines 150-151: entry_points() raises → hub_plugins = [] fallback."""

    def test_entry_points_exception_handled(self, tmp_path):
        """When importlib.metadata.entry_points raises, HubOrchestrator catches
        the exception and falls back to an empty plugin list.
        """
        from slm_mcp_hub.core.config import HubConfig
        from slm_mcp_hub.core.hub import HubOrchestrator, reset_hub

        # Reset singleton so construction succeeds
        reset_hub()

        cfg = HubConfig(config_dir=tmp_path, mcp_servers=())

        with patch(
            "slm_mcp_hub.core.hub.importlib.metadata.entry_points",
            side_effect=RuntimeError("metadata unavailable"),
        ):
            orchestrator = HubOrchestrator(config=cfg)
            # _discover_plugins is sync; call it directly
            orchestrator._discover_plugins()

        # Plugin list should be empty (no crash)
        assert len(orchestrator.plugins) == 0

        # Always reset singleton so other tests aren't affected
        reset_hub()


# ---------------------------------------------------------------------------
# federation/router.py  line 69 + branch 68->69
# ---------------------------------------------------------------------------


class TestRouterConnectionNotFound:
    """Line 69: route_tool_call returns error RouteResult when the
    ConnectionManager has no MCPConnection for the named server.
    """

    @pytest.mark.asyncio
    async def test_route_returns_error_when_conn_is_none(self, tmp_path):
        """When the requested server is not in _connections, line 69 is
        reached and a RouteResult with success=False is returned.
        """
        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
        from slm_mcp_hub.core.registry import CapabilityRegistry
        from slm_mcp_hub.federation.manager import ConnectionManager
        from slm_mcp_hub.federation.router import FederationRouter

        srv = MCPServerConfig(name="ghost", transport="stdio", command="x")
        cfg = HubConfig(config_dir=tmp_path, mcp_servers=(srv,))
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        # FederationRouter signature: (registry, connections) — registry first
        router = FederationRouter(registry, mgr._connections)

        # Register "ghost" in the registry so capability lookup succeeds
        registry.sync(
            {
                "ghost": {
                    "tools": [{"name": "ping"}],
                    "resources": [],
                    "resource_templates": [],
                    "prompts": [],
                }
            }
        )
        # But do NOT add any MCPConnection to mgr._connections → conn is None

        result = await router.route_tool_call("ghost__ping", {}, timeout_s=5.0)
        assert result.success is False


# ---------------------------------------------------------------------------
# lifecycle/config_diff.py  branch 91->93
# ---------------------------------------------------------------------------


class TestConfigDiffDisabledInBothBranch:
    """Branch 91->93: a server that is disabled in new AND was already disabled
    (or absent) in old.  The `if old_srv is not None and old_srv.enabled:` is
    False → we skip `removed.append(name)` and go directly to `continue`.
    """

    def test_disabled_in_new_also_disabled_in_old_is_not_removed(self):
        """When a server is disabled in both old and new configs, it should
        appear in neither added, removed, nor modified.
        """
        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
        from slm_mcp_hub.lifecycle.config_diff import diff_configs

        disabled_in_old = MCPServerConfig(
            name="off-srv", transport="stdio", command="x", enabled=False
        )
        disabled_in_new = MCPServerConfig(
            name="off-srv", transport="stdio", command="x", enabled=False
        )

        old_cfg = HubConfig(config_dir=Path("/tmp"), mcp_servers=(disabled_in_old,))
        new_cfg = HubConfig(config_dir=Path("/tmp"), mcp_servers=(disabled_in_new,))

        diff = diff_configs(old_cfg, new_cfg)
        assert "off-srv" not in diff.removed
        assert not any(s.name == "off-srv" for s in diff.added)
        assert not any(s.name == "off-srv" for s in diff.modified)


# ---------------------------------------------------------------------------
# observability/tracer.py  branch 92->91 (get_trace returns None)
# ---------------------------------------------------------------------------


class TestTracerGetTraceNotFound:
    """Branch 92->91: the for-loop in get_trace completes without a match,
    returning None (the `return None` at line 94 is reached).
    """

    def test_get_trace_returns_none_when_not_found(self):
        """When no span with the given trace_id exists, get_trace returns None."""
        from slm_mcp_hub.observability.tracer import RequestTracer

        tracer = RequestTracer()
        # Buffer is empty → loop completes without match → returns None
        result = tracer.get_trace("nonexistent-trace-id")
        assert result is None


# ---------------------------------------------------------------------------
# security/audit.py  branch 83->85 (cleanup with zero deleted rows)
# ---------------------------------------------------------------------------


class TestAuditCleanupZeroRows:
    """Branch 83->85: cleanup_old_entries returns 0 when no rows are old
    enough to delete — the `if deleted > 0:` branch is False.
    """

    def test_cleanup_returns_zero_when_nothing_deleted(self, tmp_path):
        """When no audit rows match the retention cutoff, deleted == 0 and
        the info log at line 84 is skipped.
        """
        from slm_mcp_hub.security.audit import AuditLogger
        from slm_mcp_hub.storage.database import HubDatabase

        db_path = tmp_path / "audit.db"
        db = HubDatabase(db_path=db_path)
        db.open()
        try:
            auditor = AuditLogger(db=db)
            # No entries in DB → cleanup deletes 0 rows
            deleted = auditor.cleanup(retention_days=7)
        finally:
            db.close()
        assert deleted == 0


# ---------------------------------------------------------------------------
# protocol/inbound.py  branch 96->100 (empty client name)
# ---------------------------------------------------------------------------


class TestInboundEmptyClientName:
    """Branch 96->100: `if name:` is False because client_info.name is an
    empty string — the function falls through to `return 'sdk-client'`.
    """

    def test_extract_client_name_with_empty_name(self):
        """When client_info.name == '', the empty-string branch is taken and
        the fallback 'sdk-client' is returned.
        """
        from types import SimpleNamespace

        from slm_mcp_hub.protocol.inbound import _extract_client_name  # noqa: PLC2701

        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    client_info=SimpleNamespace(name="")
                )
            )
        )
        result = _extract_client_name(ctx)
        assert result == "sdk-client"


# ---------------------------------------------------------------------------
# plugins/mesh_plugin.py  line 225 (auth failure in acquire_lock)
# ---------------------------------------------------------------------------


class TestMeshPluginAcquireLockAuthFailure:
    """Line 225: acquire_lock returns False when _reject_auth_failure returns
    True (the server rejected the lock attempt with a 401/403).
    """

    @pytest.mark.asyncio
    async def test_acquire_lock_returns_false_on_auth_rejection(self):
        """_reject_auth_failure returns True → acquire_lock immediately returns
        False at line 225 without checking the status code.
        """
        from slm_mcp_hub.plugins.mesh_plugin import MeshPlugin

        plugin = MeshPlugin(slm_url="http://127.0.0.1:9999")
        plugin._available = True

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        plugin._client = mock_client

        # _reject_auth_failure must return True for 401 to trigger line 225
        with patch.object(plugin, "_reject_auth_failure", return_value=True):
            result = await plugin.acquire_lock(
                resource="/some/file", session_id="s1"
            )

        assert result is False


# ---------------------------------------------------------------------------
# federation/manager.py  lines 429, 433 — shutdown inside retry loop
# ---------------------------------------------------------------------------


class TestRetryLoopShutdown:
    """Lines 429, 433: shutdown flag set while retry loop is sleeping or
    iterating over servers.
    """

    @pytest.mark.asyncio
    async def test_retry_loop_exits_when_shutdown_set_before_inner_loop(
        self, tmp_path
    ):
        """Line 429: `if self._shutdown: break` (outer check after sleep).

        Set _shutdown BEFORE the retry_failed_servers loop runs its first
        sleep, so the outer `if self._shutdown: break` fires immediately.
        """
        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
        from slm_mcp_hub.core.registry import CapabilityRegistry
        from slm_mcp_hub.federation.manager import ConnectionManager

        srv = MCPServerConfig(name="s1", transport="stdio", command="x")
        cfg = HubConfig(config_dir=tmp_path, mcp_servers=(srv,))
        mgr = ConnectionManager(cfg, CapabilityRegistry())
        mgr._failed["s1"] = "connection error"
        mgr._shutdown = True  # Already shut down before loop starts

        # Loop should exit without iterating (shutdown at first check)
        with patch("slm_mcp_hub.federation.manager._INITIAL_RETRY_DELAY_S", 0.001):
            await mgr._retry_failed_servers()

        # s1 still failed (no retry ran)
        assert "s1" in mgr._failed

    @pytest.mark.asyncio
    async def test_retry_loop_exits_when_shutdown_set_inside_server_loop(
        self, tmp_path
    ):
        """Line 433: `if self._shutdown: break` (inner check per server).

        Set _shutdown = True WHILE iterating failed servers so the inner
        guard fires.
        """
        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
        from slm_mcp_hub.core.registry import CapabilityRegistry
        from slm_mcp_hub.federation.manager import ConnectionManager

        srv1 = MCPServerConfig(name="s1", transport="stdio", command="x")
        srv2 = MCPServerConfig(name="s2", transport="stdio", command="x")
        cfg = HubConfig(config_dir=tmp_path, mcp_servers=(srv1, srv2))
        mgr = ConnectionManager(cfg, CapabilityRegistry())
        mgr._failed["s1"] = "err"
        mgr._failed["s2"] = "err"

        connect_calls: list[str] = []

        async def connect_and_shutdown(srv_cfg):
            connect_calls.append(srv_cfg.name)
            mgr._shutdown = True  # Signal shutdown after first connect attempt

        with (
            patch("slm_mcp_hub.federation.manager._INITIAL_RETRY_DELAY_S", 0.001),
            patch("slm_mcp_hub.federation.manager._MAX_RETRY_ATTEMPTS", 3),
            patch.object(mgr, "_connect_timed", side_effect=connect_and_shutdown),
        ):
            await mgr._retry_failed_servers()

        # At most one server was attempted before shutdown fired
        assert len(connect_calls) <= 1
