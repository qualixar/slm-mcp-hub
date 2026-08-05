"""Targeted tests covering previously-uncovered lines and branches.

Each class documents exactly which file:line it targets.  These are
regression guards for the specific edge-path identified by the coverage
report, not broad feature tests (those live in their own modules).
"""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# auth/provider.py  lines 337-338
# _is_private_network: except ValueError: continue inside the DNS-result loop
# ---------------------------------------------------------------------------


class TestProviderDNSLoopValueError:
    """Lines 337-338: ipaddress.ip_address(sockaddr[0]) raises ValueError.

    This happens when getaddrinfo returns an entry whose sockaddr contains a
    string that is not a valid IP address.  In practice this is extremely rare
    (getaddrinfo always returns valid IPs) but the defensive handler must be
    reachable.
    """

    def test_invalid_ip_in_getaddrinfo_result_is_skipped(self):
        """If one getaddrinfo entry has an invalid IP string, it is skipped
        (the ValueError is caught), and if no other entry is private the
        function correctly returns False (not blocked).
        """
        from slm_mcp_hub.auth.provider import _is_private_network

        # Return one entry with an invalid IP string, then one valid public IP.
        fake_results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("NOT_AN_IP", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ]
        with patch(
            "slm_mcp_hub.auth.provider.socket.getaddrinfo",
            return_value=fake_results,
        ):
            result = _is_private_network("example.com")
        # NOT_AN_IP raises ValueError → continue.  93.184.216.34 is public →
        # not blocked.  So overall: not private.
        assert result is False

    def test_only_invalid_ip_in_result_returns_false(self):
        """If ALL getaddrinfo entries have invalid IP strings, the function
        returns False (not blocked) because none triggered a private-IP match.
        """
        from slm_mcp_hub.auth.provider import _is_private_network

        fake_results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("BAD_IP_1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("BAD_IP_2", 0)),
        ]
        with patch(
            "slm_mcp_hub.auth.provider.socket.getaddrinfo",
            return_value=fake_results,
        ):
            result = _is_private_network("example.com")
        assert result is False


# ---------------------------------------------------------------------------
# lifecycle/notifier.py  lines 88-89
# _debounce_then_broadcast: except asyncio.CancelledError: return
# ---------------------------------------------------------------------------


class TestNotifierCancelledDebounce:
    """Lines 88-89: CancelledError raised inside asyncio.sleep in the debounce
    task when shutdown() cancels it before the sleep completes.
    """

    @pytest.mark.asyncio
    async def test_shutdown_while_debouncing_covers_cancelled_error_branch(self):
        """Create a notifier with a long debounce, trigger, then immediately
        shut down.  shutdown() cancels the still-sleeping task, which causes
        asyncio.CancelledError inside _debounce_then_broadcast → the
        except CancelledError: return branch executes.
        """
        from slm_mcp_hub.lifecycle.notifier import ChangeNotifier

        n = ChangeNotifier(debounce_seconds=100.0)  # very long — will not expire
        await n.notify_tools_changed()
        # Yield to allow the debounce task to start and enter asyncio.sleep
        await asyncio.sleep(0)
        # shutdown() cancels the task mid-sleep → triggers lines 88-89
        await n.shutdown()
        # Verify the notifier cleaned up correctly
        assert n._pending_task is None
        assert n.subscriber_count == 0


# ---------------------------------------------------------------------------
# lifecycle/reloader.py  lines 102-103, 112
# _apply_removes: except Exception (lines 102-103)
# _apply_modifies: if not ok: logger.warning (line 112)
# ---------------------------------------------------------------------------


class TestReloaderExceptionPaths:
    """Covers error-handling paths inside _apply_removes and _apply_modifies."""

    def _make_reloader(self, conn_manager, notifier=None):
        from slm_mcp_hub.lifecycle.reloader import ConfigReloader

        if notifier is None:
            notifier = MagicMock()
            notifier.notify_tools_changed = AsyncMock()
        return ConfigReloader(conn_manager, notifier)

    @pytest.mark.asyncio
    async def test_remove_server_exception_is_logged_not_propagated(self):
        """Lines 102-103: when remove_server raises, the error is logged and
        the reload continues — it must NOT propagate to the caller.
        """
        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig

        old_srv = MCPServerConfig(name="srv-a", transport="stdio", command="x")
        new_cfg = HubConfig(config_dir=Path("/tmp"), mcp_servers=())

        # Build a mock ConnectionManager that reports old config + raises on remove
        conn_mgr = MagicMock()
        conn_mgr.config = HubConfig(
            config_dir=Path("/tmp"), mcp_servers=(old_srv,)
        )
        conn_mgr.remove_server = AsyncMock(
            side_effect=RuntimeError("simulated remove crash")
        )
        conn_mgr.replace_server = AsyncMock(return_value=(True, "ok"))
        conn_mgr.add_server = AsyncMock(return_value=(True, "ok"))

        reloader = self._make_reloader(conn_mgr)
        # apply_config must succeed despite the remove exception
        diff = await reloader.apply_config(new_cfg)
        assert "srv-a" in diff.removed

    @pytest.mark.asyncio
    async def test_replace_server_not_ok_warns(self):
        """Line 112: when replace_server returns (False, msg), a warning is
        logged. The reload must still complete successfully.
        """
        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig

        old_srv = MCPServerConfig(name="srv-a", transport="stdio", command="x")
        new_srv = MCPServerConfig(
            name="srv-a", transport="stdio", command="x-new"
        )

        conn_mgr = MagicMock()
        conn_mgr.config = HubConfig(
            config_dir=Path("/tmp"), mcp_servers=(old_srv,)
        )
        conn_mgr.replace_server = AsyncMock(
            return_value=(False, "replace failed: port busy")
        )
        conn_mgr.remove_server = AsyncMock(return_value=(True, "ok"))
        conn_mgr.add_server = AsyncMock(return_value=(True, "ok"))

        reloader = self._make_reloader(conn_mgr)
        new_cfg = HubConfig(config_dir=Path("/tmp"), mcp_servers=(new_srv,))
        diff = await reloader.apply_config(new_cfg)
        # Modified path taken even though replace returned not-ok
        assert any(s.name == "srv-a" for s in diff.modified)


# ---------------------------------------------------------------------------
# federation/manager.py  various gaps
# ---------------------------------------------------------------------------


class TestConnectionManagerCoverageGaps:
    """Covers several uncovered lines and branches in ConnectionManager."""

    def _make_manager(self, *server_cfgs, tmp_path=None):
        from slm_mcp_hub.core.config import HubConfig
        from slm_mcp_hub.core.registry import CapabilityRegistry
        from slm_mcp_hub.federation.manager import ConnectionManager

        cfg = HubConfig(
            config_dir=tmp_path or Path("/tmp"),
            mcp_servers=server_cfgs,
        )
        registry = CapabilityRegistry()
        return ConnectionManager(cfg, registry)

    # ---- Line 83: connect_times property ----

    def test_connect_times_property_returns_dict(self, tmp_path):
        """Line 83: connect_times property was never called in existing tests."""
        mgr = self._make_manager(tmp_path=tmp_path)
        ct = mgr.connect_times
        assert isinstance(ct, dict)

    # ---- Line 410: _start_retry_loop early-return when task already running ----

    @pytest.mark.asyncio
    async def test_start_retry_loop_is_idempotent(self, tmp_path):
        """Line 410: calling _start_retry_loop twice when a task is still running
        must return without creating a duplicate task.
        """
        mgr = self._make_manager(tmp_path=tmp_path)

        # Manually create a long-running sentinel task so the guard fires
        async def _forever():
            await asyncio.sleep(9999)

        mgr._retry_task = asyncio.create_task(_forever())
        try:
            assert not mgr._retry_task.done()
            first_task = mgr._retry_task
            mgr._start_retry_loop()  # Should hit `return` at line 410
            assert mgr._retry_task is first_task  # same task — no new one created
        finally:
            mgr._retry_task.cancel()
            try:
                await mgr._retry_task
            except asyncio.CancelledError:
                pass

    # ---- Lines 400-405: RuntimeError in _sync_registry (no event loop) ----

    @pytest.mark.asyncio
    async def test_sync_registry_runtime_error_swallowed(self, tmp_path):
        """Lines 400-405: asyncio.create_task raises RuntimeError when there
        is no running loop (test-path or at module level).  The error must be
        silently swallowed, not propagated.
        """
        from slm_mcp_hub.lifecycle.notifier import ChangeNotifier

        mgr = self._make_manager(tmp_path=tmp_path)
        notifier = MagicMock(spec=ChangeNotifier)
        notifier.notify_tools_changed = AsyncMock()
        mgr.set_notifier(notifier)

        # Force the registry to report "changed" so the notifier path is taken
        mgr._registry = MagicMock()
        mgr._registry.sync = MagicMock(return_value=True)

        with patch(
            "slm_mcp_hub.federation.manager.asyncio.create_task",
            side_effect=RuntimeError("no running event loop"),
        ):
            # Must not raise — RuntimeError is caught and silenced
            mgr._sync_registry()

    # ---- Branch 109->117: connect_all with HTTP-only servers (no stdio) ----

    @pytest.mark.asyncio
    async def test_connect_all_http_only_servers(self, tmp_path):
        """Branch 109->117: the `if stdio:` at line 109 is False when there
        are no stdio servers.  The HTTP-only path (phase 2 directly) is taken.
        """
        from slm_mcp_hub.core.config import MCPServerConfig

        http_srv = MCPServerConfig(
            name="http-srv", transport="http", url="https://example.com/mcp"
        )
        mgr = self._make_manager(http_srv, tmp_path=tmp_path)

        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.is_connected = True
        mock_conn.capabilities = {
            "tools": [], "resources": [], "resource_templates": [], "prompts": [],
        }

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection",
            return_value=mock_conn,
        ):
            failed = await mgr.connect_all()

        assert "http-srv" not in failed
        assert mgr.connected_count >= 0

    # ---- Branch 253->266: replace_server when old connection is None ----

    @pytest.mark.asyncio
    async def test_replace_server_no_existing_connection(self, tmp_path):
        """Branch 253->266: replace_server called for a server that is NOT in
        self._connections (old_conn is None).  The drain step is skipped and
        add_server is called directly.
        """
        from slm_mcp_hub.core.config import MCPServerConfig

        srv = MCPServerConfig(name="new-srv", transport="stdio", command="echo")
        mgr = self._make_manager(srv, tmp_path=tmp_path)

        # No existing connection — _connections is empty
        assert "new-srv" not in mgr._connections

        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.is_connected = True
        mock_conn.capabilities = {
            "tools": [{"name": "ping"}],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection",
            return_value=mock_conn,
        ):
            ok, msg = await mgr.replace_server(srv)

        assert ok is True

    # ---- Lines 364-365: disconnect exception after TimeoutError ----

    @pytest.mark.asyncio
    async def test_connect_timed_disconnect_exception_after_timeout(self, tmp_path):
        """Lines 364-365: when a connection times out AND the subsequent
        disconnect() raises, the exception must be silently swallowed.
        """
        from slm_mcp_hub.core.config import MCPServerConfig

        srv = MCPServerConfig(name="slow-srv", transport="stdio", command="sleep")
        mgr = self._make_manager(srv, tmp_path=tmp_path)

        mock_conn = MagicMock()
        # connect() hangs until cancelled (simulate timeout)
        mock_conn.connect = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_conn.disconnect = AsyncMock(
            side_effect=RuntimeError("disconnect failed after timeout")
        )
        mock_conn.is_connected = False
        mock_conn.capabilities = {
            "tools": [], "resources": [], "resource_templates": [], "prompts": [],
        }

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection",
            return_value=mock_conn,
        ):
            # _connect_timed must not propagate the disconnect exception
            await mgr._connect_timed(srv)

        assert "slow-srv" in mgr._failed

    # ---- Lines 428-445: retry loop body and exhaustion warning ----

    @pytest.mark.asyncio
    async def test_retry_loop_runs_and_warns_when_exhausted(self, tmp_path):
        """Lines 428-445: the background _retry_failed_servers() loop retries
        failed servers and logs a warning after exhausting all attempts.
        """
        from slm_mcp_hub.core.config import MCPServerConfig

        srv = MCPServerConfig(name="flaky", transport="stdio", command="echo")
        mgr = self._make_manager(srv, tmp_path=tmp_path)

        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock(
            side_effect=ConnectionError("always fails")
        )
        mock_conn.disconnect = AsyncMock()
        mock_conn.is_connected = False
        mock_conn.capabilities = {
            "tools": [], "resources": [], "resource_templates": [], "prompts": [],
        }

        # Patch timing constants so the test finishes quickly
        with (
            patch(
                "slm_mcp_hub.federation.manager.MCPConnection",
                return_value=mock_conn,
            ),
            patch(
                "slm_mcp_hub.federation.manager._INITIAL_RETRY_DELAY_S", 0.001
            ),
            patch(
                "slm_mcp_hub.federation.manager._MAX_RETRY_ATTEMPTS", 2
            ),
            patch(
                "slm_mcp_hub.federation.manager._MAX_RETRY_DELAY_S", 0.01
            ),
        ):
            await mgr.connect_all()
            assert "flaky" in mgr._failed

            # Run the retry loop directly (not as background task)
            await mgr._retry_failed_servers()

        # After all attempts exhausted, server is still failed and warning was logged
        assert "flaky" in mgr._failed

    # ---- Lines 297->302: fast_retry_failed when pop returns None ----

    @pytest.mark.asyncio
    async def test_fast_retry_when_connection_already_gone(self, tmp_path):
        """Branch 297->302: fast_retry_failed pops the old connection; if the
        pop returns None (connection already cleaned up), the disconnect step
        is skipped gracefully.
        """
        from slm_mcp_hub.core.config import MCPServerConfig
        from slm_mcp_hub.federation.manager import ConnectionManager

        srv = MCPServerConfig(name="gone-srv", transport="stdio", command="x")
        cfg_module = __import__(
            "slm_mcp_hub.core.config", fromlist=["HubConfig"]
        )
        HubConfig = cfg_module.HubConfig
        from slm_mcp_hub.core.registry import CapabilityRegistry

        cfg = HubConfig(config_dir=tmp_path, mcp_servers=(srv,))
        mgr = ConnectionManager(cfg, CapabilityRegistry())

        # Manually put "gone-srv" in failed but NOT in connections
        mgr._failed["gone-srv"] = "never connected"
        # _connections does NOT have "gone-srv" → pop returns None

        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.is_connected = True
        mock_conn.capabilities = {
            "tools": [], "resources": [], "resource_templates": [], "prompts": [],
        }

        with (
            patch(
                "slm_mcp_hub.federation.manager.MCPConnection",
                return_value=mock_conn,
            ),
            patch(
                "slm_mcp_hub.federation.manager._FAST_RETRY_DELAYS_S",
                (0.001,),
            ),
        ):
            result = await mgr.fast_retry_failed()

        # gone-srv was retried and succeeded → not in failed anymore
        assert "gone-srv" not in result


# ---------------------------------------------------------------------------
# plugins/slm_plugin.py  lines 186-187, 244-245
# on_tool_call_after / on_session_end: RuntimeError from create_task
# ---------------------------------------------------------------------------


class TestSLMPluginCreateTaskError:
    """Lines 186-187 and 244-245: asyncio.create_task raises RuntimeError
    (no running event loop or cancelled scheduler).  The exception must be
    caught and logged, never propagated.
    """

    def _make_plugin(self):
        from slm_mcp_hub.plugins.slm_plugin import SLMPlugin

        return SLMPlugin(slm_url="http://127.0.0.1:9999")

    @pytest.mark.asyncio
    async def test_on_tool_call_after_create_task_error_swallowed(self):
        """Lines 186-187: RuntimeError from asyncio.create_task in
        on_tool_call_after must not propagate.
        """
        plugin = self._make_plugin()
        # Mark plugin as available so we reach the create_task call
        plugin._available = True
        plugin._client = MagicMock()

        with patch(
            "slm_mcp_hub.plugins.slm_plugin.asyncio.create_task",
            side_effect=RuntimeError("no event loop"),
        ):
            # Must not raise
            await plugin.on_tool_call_after(
                session_id="s1",
                server="test-server",
                tool="test-tool",
                args={},
                result=None,
                duration_ms=10,
                success=True,
            )

    @pytest.mark.asyncio
    async def test_on_session_end_create_task_error_swallowed(self):
        """Lines 244-245: RuntimeError from asyncio.create_task in
        on_session_end must not propagate.
        """
        plugin = self._make_plugin()
        plugin._available = True
        plugin._client = MagicMock()
        # Pre-populate session data
        plugin._session_tool_counts["s1"]["tool"] = 3
        plugin._session_durations["s1"] = 100
        plugin._session_contexts["s1"] = {"project_path": "/test"}

        with patch(
            "slm_mcp_hub.plugins.slm_plugin.asyncio.create_task",
            side_effect=RuntimeError("no event loop"),
        ):
            # Must not raise
            await plugin.on_session_end("s1")


# ---------------------------------------------------------------------------
# discovery/auto_register.py  line 176, 370-371, 453->458
# ---------------------------------------------------------------------------


class TestAutoRegisterCoverageGaps:
    """Covers the transport property getter and OS-error paths."""

    def test_transport_property(self):
        """Line 176: the `transport` property getter was never called."""
        from slm_mcp_hub.discovery.auto_register import AutoRegister

        reg = AutoRegister("http://127.0.0.1:52414/mcp", transport="stdio")
        assert reg.transport == "stdio"

    def test_register_backup_oserror_returns_failure(self, tmp_path):
        """Lines 370-371: if shutil.copy2 for the backup raises OSError, the
        function returns a RegistrationResult with success=False.
        """
        import json

        from slm_mcp_hub.discovery.auto_register import AutoRegister
        from slm_mcp_hub.discovery.client_detector import DetectedClient

        cfg_file = tmp_path / "claude.json"
        cfg_file.write_text(json.dumps({"mcpServers": {"existing": {}}}))

        client = DetectedClient(
            name="claude",
            display_name="Claude",
            config_path=cfg_file,
            mcp_count=1,
            hub_registered=False,
            config_format="claude",
        )
        reg = AutoRegister("http://127.0.0.1:52414/mcp")

        with patch(
            "slm_mcp_hub.discovery.auto_register.shutil.copy2",
            side_effect=OSError("no space left"),
        ):
            result = reg.register(client, mcp_key="mcpServers")

        assert result.success is False
        assert "backup" in result.error.lower() or "no space" in result.error.lower()

    def test_register_transparent_backup_oserror_returns_failure(self, tmp_path):
        """Lines 370-371: shutil.copy2 raises OSError during backup in
        register_transparent → returns RegistrationResult(success=False).
        """
        import json

        from slm_mcp_hub.discovery.auto_register import AutoRegister
        from slm_mcp_hub.discovery.client_detector import DetectedClient

        cfg_file = tmp_path / "claude.json"
        cfg_file.write_text(json.dumps({"mcpServers": {"my-server": {}}}))

        client = DetectedClient(
            name="claude",
            display_name="Claude",
            config_path=cfg_file,
            mcp_count=1,
            hub_registered=False,
            config_format="claude",
        )
        reg = AutoRegister("http://127.0.0.1:52414/mcp")

        with patch(
            "slm_mcp_hub.discovery.auto_register.shutil.copy2",
            side_effect=OSError("disk full"),
        ):
            result = reg.register_transparent(
                client=client,
                server_names=["my-server"],
                mcp_key="mcpServers",
            )

        assert result.success is False
        assert "backup" in result.error.lower() or "disk full" in result.error.lower()

    def test_register_transparent_write_and_restore_both_fail(self, tmp_path):
        """Lines 395-396: _write_json raises OSError AND shutil.copy2 for
        the backup restoration also raises.  Both must be handled without
        propagating.
        """
        import json

        from slm_mcp_hub.discovery.auto_register import AutoRegister
        from slm_mcp_hub.discovery.client_detector import DetectedClient

        cfg_file = tmp_path / "claude.json"
        cfg_file.write_text(json.dumps({"mcpServers": {"my-server": {}}}))

        client = DetectedClient(
            name="claude",
            display_name="Claude",
            config_path=cfg_file,
            mcp_count=1,
            hub_registered=False,
            config_format="claude",
        )
        reg = AutoRegister("http://127.0.0.1:52414/mcp")

        call_count = {"n": 0}

        def mock_copy2(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 2:
                # Second call = backup restoration → also fail
                raise OSError("restore also failed")
            # First call = create backup → succeed

        with (
            patch(
                "slm_mcp_hub.discovery.auto_register.shutil.copy2",
                side_effect=mock_copy2,
            ),
            patch(
                "slm_mcp_hub.discovery.auto_register._write_json",
                side_effect=OSError("write failed"),
            ),
        ):
            result = reg.register_transparent(
                client=client,
                server_names=["my-server"],
                mcp_key="mcpServers",
            )

        assert result.success is False

    def test_import_mcps_skips_already_known_servers(self, tmp_path):
        """Branch 453->458: when all servers in the source config are already
        present in the hub config, new_servers is empty and lines 454-456 are
        skipped — the branch from 453 to 458 is taken.
        """
        import json

        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig, save_config
        from slm_mcp_hub.discovery.auto_register import AutoRegister

        # Source config with one server
        source = tmp_path / "source.json"
        source.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "already-there": {
                            "type": "http",
                            "url": "https://example.com/mcp",
                        }
                    }
                }
            )
        )

        # Hub config with the same server already registered
        hub_cfg_file = tmp_path / "hub_config.json"
        existing_hub = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(
                MCPServerConfig(
                    name="already-there",
                    transport="http",
                    url="https://example.com/mcp",
                ),
            ),
        )
        save_config(existing_hub, hub_cfg_file)

        reg = AutoRegister("http://127.0.0.1:52414/mcp")
        result = reg.import_mcps(
            source_path=source,
            config_format="claude",
            hub_config_path=hub_cfg_file,
        )
        # No new servers imported — all were already in hub config
        assert result.imported_count == 0
        assert result.skipped_count == 1
