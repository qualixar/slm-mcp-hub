"""Release-gate tests for operational safety and client integration paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from slm_mcp_hub.cli import main as cli_main
from slm_mcp_hub.cli.main import cli
from slm_mcp_hub.core import config as config_module
from slm_mcp_hub.core.config import (
    HubConfig,
    MCPServerConfig,
    _atomic_write,
    _snapshot_existing,
    list_snapshots,
    restore_snapshot,
    save_config,
)
from slm_mcp_hub.discovery.auto_register import AutoRegister
from slm_mcp_hub.discovery.client_detector import DetectedClient


def _client(path: Path, *, registered: bool = False) -> DetectedClient:
    return DetectedClient(
        name="test-client",
        display_name="Test Client",
        config_path=path,
        mcp_count=1,
        hub_registered=registered,
        config_format="claude",
    )


class TestSecretLoading:
    def test_load_secrets_preserves_existing_values_and_ignores_comments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared = tmp_path / "shared.env"
        hub = tmp_path / "hub.env"
        shared.write_text("# ignored\nTOKEN=from-shared\nMALFORMED\n")
        hub.write_text("TOKEN=from-hub\nSECOND=available\n")
        monkeypatch.setattr(cli_main, "SECRETS_PATHS", (shared, hub))
        monkeypatch.setenv("TOKEN", "pre-existing")
        monkeypatch.delenv("SECOND", raising=False)

        cli_main._load_secrets()

        assert os.environ["TOKEN"] == "pre-existing"
        assert os.environ["SECOND"] == "available"

    def test_load_secrets_ignores_unreadable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unreadable = tmp_path / "secrets.env"
        unreadable.write_text("TOKEN=should-not-load")
        monkeypatch.setattr(cli_main, "SECRETS_PATHS", (unreadable,))
        monkeypatch.setattr("builtins.open", MagicMock(side_effect=OSError("denied")))
        monkeypatch.delenv("TOKEN", raising=False)

        cli_main._load_secrets()

        assert "TOKEN" not in os.environ


class TestConfigSafety:
    def test_snapshot_prunes_oldest_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"mcpServers": {"a": {}, "b": {}, "c": {}}}))
        snapshots = tmp_path / "snapshots"
        snapshots.mkdir()
        for index in range(2):
            (snapshots / f"config-00000000-00000{index}-{index}mcps.json").write_text("{}")
        monkeypatch.setattr(config_module, "MAX_SNAPSHOTS", 2)

        created = _snapshot_existing(config_path)

        assert created is not None
        assert len(list(snapshots.glob("config-*.json"))) == 2

    def test_atomic_write_removes_partial_file_when_verification_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "config.json"
        target.write_text('{"preserve": true}')
        replacement = tmp_path / "config.json.tmp"
        monkeypatch.setattr(config_module.json, "load", lambda _: {"mcpServers": {"wrong": {}}})

        with pytest.raises(RuntimeError, match="verification failed"):
            _atomic_write(target, {"mcpServers": {"expected": {}}})

        assert target.read_text() == '{"preserve": true}'
        assert not replacement.exists()

    def test_save_rejects_large_server_drop_without_force(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"mcpServers": {f"server-{i}": {} for i in range(10)}}))

        with pytest.raises(RuntimeError, match="REFUSING to save"):
            save_config(HubConfig(), path)

        assert len(json.loads(path.read_text())["mcpServers"]) == 10

    def test_save_permits_corrupt_existing_config_and_writes_valid_replacement(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("not-json")

        save_config(HubConfig(mcp_servers=(MCPServerConfig("safe", "stdio", "echo"),)), path)

        assert json.loads(path.read_text())["mcpServers"]["safe"]["command"] == "echo"

    def test_list_snapshots_marks_corrupt_snapshot_without_hiding_valid_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snapshots = tmp_path / "snapshots"
        snapshots.mkdir()
        (snapshots / "config-20260101-3mcps.json").write_text('{"mcpServers": {"a": {}, "b": {}}}')
        (snapshots / "config-20260102-unknown.json").write_text("broken")
        monkeypatch.setattr(config_module, "get_snapshots_dir", lambda *_: snapshots)

        records = list_snapshots()

        assert {record["mcp_count"] for record in records} == {2, -1}

    def test_restore_snapshot_is_reversible(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshots = tmp_path / "snapshots"
        snapshots.mkdir()
        source = snapshots / "config-good.json"
        source.write_text('{"mcpServers": {"restored": {}}}')
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"mcpServers": {"a": {}, "b": {}, "c": {}}}))
        monkeypatch.setattr(config_module, "get_snapshots_dir", lambda *_: snapshots)

        restored = restore_snapshot(source.name, target)

        assert restored == target
        assert json.loads(target.read_text())["mcpServers"] == {"restored": {}}
        assert any(item.name != source.name for item in snapshots.glob("config-*.json"))

    def test_config_directory_override_is_resolved_after_modules_are_imported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dynamic_dir = tmp_path / "dynamic-after-import"
        monkeypatch.setenv("SLM_HUB_CONFIG_DIR", str(dynamic_dir))

        config = config_module.load_config()
        config_module.save_config(config)

        assert config.config_dir == dynamic_dir
        assert (dynamic_dir / "config.json").exists()


class TestTransparentRegistration:
    def test_transparent_register_replaces_only_target_section_and_keeps_backup(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "settings.json"
        original = {"editor": {"fontSize": 15}, "mcp": {"servers": {"old": {"command": "old"}}}}
        config_path.write_text(json.dumps(original))
        registrar = AutoRegister("http://127.0.0.1:52414/mcp")

        result = registrar.register_transparent(
            _client(config_path), ["calendar", "github"], mcp_key="mcp.servers"
        )

        assert result.success is True
        written = json.loads(config_path.read_text())
        assert written["editor"] == {"fontSize": 15}
        assert written["mcp"]["servers"] == {
            "calendar": {"type": "http", "url": "http://127.0.0.1:52414/mcp/calendar"},
            "github": {"type": "http", "url": "http://127.0.0.1:52414/mcp/github"},
        }
        assert json.loads(result.backup_path.read_text()) == original  # type: ignore[union-attr]

    def test_transparent_register_dry_run_has_no_side_effect(self, tmp_path: Path) -> None:
        config_path = tmp_path / "client.json"
        original = '{"mcpServers": {"old": {}}}'
        config_path.write_text(original)

        result = AutoRegister().register_transparent(_client(config_path), ["old"], dry_run=True)

        assert result.success is True
        assert result.error == "dry_run:would_replace_1_servers"
        assert config_path.read_text() == original
        assert not (tmp_path / "client.json.pre-hub-backup").exists()

    def test_transparent_register_restores_backup_when_write_fails(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "client.json"
        original = '{"mcpServers": {"old": {"command": "old"}}}'
        config_path.write_text(original)

        with patch("slm_mcp_hub.discovery.auto_register._write_json", side_effect=OSError("disk full")):
            result = AutoRegister().register_transparent(_client(config_path), ["new"])

        assert result.success is False
        assert "Failed to write" in result.error
        assert config_path.read_text() == original

    def test_transparent_register_reports_unreadable_source(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.json"

        result = AutoRegister().register_transparent(_client(missing), ["new"])

        assert result.success is False
        assert "Failed to read config" in result.error


class _Response:
    def __init__(self, payload: object, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error


class TestOperationalCli:
    def test_root_help_uses_factual_product_description(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "Local-first MCP gateway" in result.output
        assert "World's First" not in result.output

    def test_start_rejects_remote_bind_without_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLM_HUB_API_KEY", raising=False)
        monkeypatch.setattr(cli_main, "_load_secrets", lambda: None)
        monkeypatch.setattr(cli_main, "load_config", lambda _: HubConfig(host="0.0.0.0", port=5000))
        kill_existing = MagicMock()
        monkeypatch.setattr(cli_main, "_kill_existing_hub", kill_existing)

        result = CliRunner().invoke(cli, ["start"])

        assert result.exit_code != 0
        assert "Remote binding requires SLM_HUB_API_KEY" in result.output
        kill_existing.assert_not_called()

    def test_start_allows_remote_bind_with_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "test-key-not-a-real-secret")
        monkeypatch.setattr(cli_main, "_load_secrets", lambda: None)
        monkeypatch.setattr(cli_main, "load_config", lambda _: HubConfig(host="0.0.0.0", port=5001))
        kill_existing = MagicMock()
        monkeypatch.setattr(cli_main, "_kill_existing_hub", kill_existing)

        def stop_before_server(coro: object) -> None:
            coro.close()  # type: ignore[attr-defined]

        monkeypatch.setattr(cli_main.asyncio, "run", stop_before_server)
        result = CliRunner().invoke(cli, ["start"])

        assert result.exit_code == 0
        kill_existing.assert_called_once_with("0.0.0.0", 5001)

    def test_start_rejects_named_remote_host_without_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLM_HUB_API_KEY", raising=False)
        monkeypatch.setattr(cli_main, "_load_secrets", lambda: None)
        monkeypatch.setattr(cli_main, "load_config", lambda _: HubConfig(host="hub.example.test"))

        result = CliRunner().invoke(cli, ["start"])

        assert result.exit_code != 0
        assert "Remote binding requires" in result.output

    def test_kill_existing_hub_cleans_stale_pid_and_orphan_listener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeSocket:
            closed = False

            def connect_ex(self, _: object) -> int:
                return 0

            def close(self) -> None:
                self.closed = True

        socket = FakeSocket()
        removed = MagicMock()
        monkeypatch.setattr("socket.socket", lambda *_: socket)
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.is_running", lambda: True)
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.read_pid_file", lambda: 123)
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.remove_pid_file", removed)
        monkeypatch.setattr(cli_main.os, "getpid", lambda: 1)
        monkeypatch.setattr("subprocess.run", lambda *_, **__: SimpleNamespace(stdout="999\n"))

        def controlled_kill(pid: int, signal_number: int) -> None:
            if signal_number == 0 or (pid == 999 and signal_number != 0):
                raise ProcessLookupError()

        monkeypatch.setattr(cli_main.os, "kill", controlled_kill)
        cli_main._kill_existing_hub("127.0.0.1", 5000)

        assert socket.closed is True
        removed.assert_called_once()

    @pytest.mark.parametrize(
        ("retry_result", "expected"),
        [({"calendar": "timeout"}, "WARNING: calendar: timeout"), ({}, "Connected after retries")],
    )
    def test_start_reports_cold_start_retry_outcome(
        self,
        retry_result: dict[str, str],
        expected: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = HubConfig(host="127.0.0.1", port=5011, mcp_servers=(MCPServerConfig("calendar", "stdio", "echo"),))
        hub = MagicMock(plugins=[])
        hub.get_status = MagicMock(return_value={})
        runtime = MagicMock(
            mcp_endpoint=object(), session_manager=object(), proxy=object(), registry=SimpleNamespace(tool_count=2),
            reloader=object(), conn_manager=SimpleNamespace(connected_count=0),
        )
        runtime.connect_all = AsyncMock(return_value={"calendar": "initial failure"})
        runtime.conn_manager.fast_retry_failed = AsyncMock(return_value=retry_result)
        runtime.disconnect_all = AsyncMock()
        hub_context = MagicMock()
        hub_context.__aenter__ = AsyncMock(return_value=hub)
        hub_context.__aexit__ = AsyncMock(return_value=False)
        pid_file = tmp_path / "hub.pid"

        async def yield_once() -> None:
            await cli_main.asyncio.sleep(0)

        server = MagicMock()
        server.serve = AsyncMock(side_effect=yield_once)
        monkeypatch.setattr(cli_main, "_load_secrets", lambda: None)
        monkeypatch.setattr(cli_main, "load_config", lambda _: config)
        monkeypatch.setattr(cli_main, "_kill_existing_hub", lambda *_: None)
        monkeypatch.setattr(cli_main, "get_pid_file", lambda: pid_file)
        monkeypatch.setattr(cli_main, "HubOrchestrator", MagicMock(return_value=hub_context))
        with (
            patch("slm_mcp_hub.lifecycle.runtime.HubRuntime", return_value=runtime),
            patch("slm_mcp_hub.server.http_server.create_app", return_value=object()),
            patch("uvicorn.Server", return_value=server),
        ):
            result = CliRunner().invoke(cli, ["start"])

        assert result.exit_code == 0
        assert expected in result.output
        runtime.disconnect_all.assert_awaited_once()

    def test_mcp_stdio_connects_and_disconnects_without_writing_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = MagicMock()
        runtime = MagicMock(
            mcp_endpoint=object(), session_manager=object(), notifier=object(),
        )
        runtime.connect_all = AsyncMock(return_value={})
        runtime.disconnect_all = AsyncMock()
        stdio_server = MagicMock()
        stdio_server.serve = AsyncMock()
        hub_context = MagicMock()
        hub_context.__aenter__ = AsyncMock(return_value=hub)
        hub_context.__aexit__ = AsyncMock(return_value=False)

        monkeypatch.setattr(cli_main, "_load_secrets", lambda: None)
        monkeypatch.setattr(cli_main, "load_config", lambda: HubConfig())
        monkeypatch.setattr(cli_main, "HubOrchestrator", MagicMock(return_value=hub_context))
        with (
            patch("slm_mcp_hub.lifecycle.runtime.HubRuntime", return_value=runtime),
            patch("slm_mcp_hub.server.stdio_server.StdioServer", return_value=stdio_server),
        ):
            result = CliRunner().invoke(cli, ["mcp"])

        assert result.exit_code == 0
        assert result.output == ""
        runtime.disconnect_all.assert_awaited_once()
        stdio_server.serve.assert_awaited_once()

    def test_config_snapshot_and_restore_commands_show_actionable_outcomes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snapshot = {
            "name": "config-20260803-3mcps.json", "mcp_count": 3, "size": 42,
            "path": tmp_path / "config-20260803-3mcps.json",
        }
        restored = tmp_path / "config.json"
        monkeypatch.setattr(config_module, "list_snapshots", lambda: [snapshot])
        monkeypatch.setattr(config_module, "get_snapshots_dir", lambda *_: tmp_path)
        monkeypatch.setattr(config_module, "restore_snapshot", lambda _: restored)

        listed = CliRunner().invoke(cli, ["config", "snapshots"])
        restored_result = CliRunner().invoke(cli, ["config", "restore", snapshot["name"]])

        assert listed.exit_code == 0
        assert "3 MCPs" in listed.output
        assert restored_result.exit_code == 0
        assert "Restored" in restored_result.output

    def test_config_restore_explains_missing_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            config_module,
            "restore_snapshot",
            MagicMock(side_effect=FileNotFoundError("missing snapshot")),
        )

        result = CliRunner().invoke(cli, ["config", "restore", "missing.json"])

        assert result.exit_code == 0
        assert "List available" in result.output

    def test_daemon_install_macos_reports_success_and_load_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plist = tmp_path / "agent.plist"
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.install_launchd", lambda _: plist)
        monkeypatch.setattr("slm_mcp_hub.cli.main.load_config", lambda: HubConfig(port=5010))

        completed = SimpleNamespace(returncode=0, stderr="")
        with patch("subprocess.run", return_value=completed):
            success = CliRunner().invoke(cli, ["daemon", "install"])
        failed_process = SimpleNamespace(returncode=1, stderr="permission denied")
        with patch("subprocess.run", side_effect=[completed, failed_process]):
            failure = CliRunner().invoke(cli, ["daemon", "install"])

        assert "installed and loaded" in success.output
        assert "permission denied" in failure.output

    def test_daemon_status_and_uninstall_cover_install_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "home"
        plist = fake_home / "Library" / "LaunchAgents" / "com.qualixar.slm-mcp-hub.plist"
        plist.parent.mkdir(parents=True)
        monkeypatch.setattr(cli_main.Path, "home", classmethod(lambda _: fake_home))
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.is_running", lambda: True)
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.read_pid_file", lambda: 92)
        monkeypatch.setattr("subprocess.run", lambda *_, **__: SimpleNamespace(returncode=0))

        not_installed = CliRunner().invoke(cli, ["daemon", "status"])
        assert "Install with" in not_installed.output

        plist.write_text("plist")
        installed = CliRunner().invoke(cli, ["daemon", "status"])
        removed = CliRunner().invoke(cli, ["daemon", "uninstall"])
        assert "Plist installed: yes" in installed.output
        assert "PID 92" in installed.output
        assert "uninstalled" in removed.output
        assert not plist.exists()

    def test_status_verbose_renders_all_connection_states(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = HubConfig(host="127.0.0.1", port=5001)
        health = _Response({"version": "0.2.6", "state": "ready", "uptime_seconds": 3.2, "mcp_servers_configured": 4})
        details = _Response({"servers": [
            {"name": "disabled", "transport": "stdio", "enabled": False, "connected": False, "tools": 0},
            {"name": "connected", "transport": "http", "enabled": True, "connected": True, "tools": 2},
            {"name": "failed", "transport": "http", "enabled": True, "connected": False, "tools": 0, "error": "timeout"},
            {"name": "pending", "transport": "sse", "enabled": True, "connected": False, "tools": 0},
        ]})
        monkeypatch.setattr("slm_mcp_hub.cli.main.load_config", lambda: config)
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.is_running", lambda: True)
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.read_pid_file", lambda: 77)
        with patch("httpx.get", side_effect=[health, details]):
            result = CliRunner().invoke(cli, ["status", "--verbose"])

        assert result.exit_code == 0
        for state in ("disabled", "connected", "failed", "pending", "timeout"):
            assert state in result.output

    def test_status_handles_unreachable_and_verbose_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("slm_mcp_hub.cli.main.load_config", lambda: HubConfig(port=5002))
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.is_running", lambda: True)
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.read_pid_file", lambda: 78)
        with patch("httpx.get", side_effect=httpx.ConnectError("offline")):
            unreachable = CliRunner().invoke(cli, ["status"])
        assert "unreachable" in unreachable.output

        with patch("httpx.get", side_effect=[_Response({}), RuntimeError("detail broken")]):
            verbose = CliRunner().invoke(cli, ["status", "--verbose"])
        assert "verbose fetch failed" in verbose.output

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [({"success": True, "message": "connected"}, "Reconnected: demo (connected)"), ({"success": False}, "Failed: unknown error")],
    )
    def test_reconnect_reports_server_result(
        self, payload: dict[str, object], expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("slm_mcp_hub.cli.main.load_config", lambda: HubConfig(port=5003))
        with patch("httpx.post", return_value=_Response(payload)) as post:
            result = CliRunner().invoke(cli, ["reconnect", "demo"])

        assert result.exit_code == 0
        assert expected in result.output
        assert post.call_args.args[0].endswith("/demo/reconnect")

    def test_reconnect_handles_network_and_unexpected_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("slm_mcp_hub.cli.main.load_config", lambda: HubConfig())
        with patch("httpx.post", side_effect=httpx.ConnectError("offline")):
            offline = CliRunner().invoke(cli, ["reconnect", "demo"])
        with patch("httpx.post", side_effect=RuntimeError("bad response")):
            error = CliRunner().invoke(cli, ["reconnect", "demo"])
        assert "Hub is not running" in offline.output
        assert "Error: bad response" in error.output

    def test_tools_lists_and_filters_server_inventory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("slm_mcp_hub.cli.main.load_config", lambda: HubConfig(port=5004))
        payload = {"servers": [
            {"name": "calendar", "transport": "http", "status": "connected", "tools": ["create", "delete", "find", "list", "move", "update"]},
            {"name": "counted", "transport": "stdio", "status": "ready", "tools": 4},
        ]}
        with patch("httpx.get", return_value=_Response(payload)):
            result = CliRunner().invoke(cli, ["tools", "--query", "calendar"])

        assert result.exit_code == 0
        assert "calendar" in result.output
        assert "and 1 more" in result.output
        assert "counted" not in result.output

    def test_tools_handles_network_and_http_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("slm_mcp_hub.cli.main.load_config", lambda: HubConfig())
        with patch("httpx.get", side_effect=httpx.ConnectError("offline")):
            offline = CliRunner().invoke(cli, ["tools"])
        with patch("httpx.get", return_value=_Response([], error=RuntimeError("bad status"))):
            failed = CliRunner().invoke(cli, ["tools"])
        assert "Hub is not running" in offline.output
        assert "Error: bad status" in failed.output

    def test_daemon_install_linux_renders_systemd_unit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("slm_mcp_hub.cli.main.load_config", lambda: HubConfig(port=5555))
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.generate_systemd_unit", lambda port: f"unit port={port}")

        result = CliRunner().invoke(cli, ["daemon", "install", "--port", "6000"])

        assert result.exit_code == 0
        assert "macOS-only" in result.output
        assert "unit port=6000" in result.output
