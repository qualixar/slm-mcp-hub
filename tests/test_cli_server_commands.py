"""Tests for Phase 4 — CLI `slm-hub server *` subcommands.

Coverage:
- `server add` happy path: edits config + calls reload
- `server add` validation: duplicate name, missing command for stdio, missing url for http
- `server remove` happy path: edits config + calls reload
- `server remove` not found
- `server modify` env/args/enable/disable
- `server modify` rejects empty change
- `server reload` calls API and reports summary
- `server list` shows disk config when hub down, full table when up
- `server status` per-server vs all
- `_parse_env_args` validation
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from slm_mcp_hub.cli.server_commands import _parse_env_args, server


@pytest.fixture()
def temp_config(tmp_path, monkeypatch):
    """Redirect dynamic config lookup to tmp_path so tests never touch user data."""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("SLM_HUB_CONFIG_DIR", str(tmp_path))
    return cfg_path


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _seed_config(servers: list[dict]) -> None:
    """Write a hub config with given servers to disk."""
    from slm_mcp_hub.core.config import HubConfig, MCPServerConfig, save_config
    cfg = HubConfig(
        mcp_servers=tuple(
            MCPServerConfig(
                name=s["name"],
                transport=s.get("transport", "stdio"),
                command=s.get("command", "echo"),
                args=tuple(s.get("args", ())),
                env=s.get("env", {}),
                enabled=s.get("enabled", True),
                url=s.get("url", ""),
            )
            for s in servers
        ),
    )
    save_config(cfg)


# ---------- _parse_env_args ----------

class TestParseEnvArgs:
    def test_valid_pairs(self):
        result = _parse_env_args(("KEY=VAL", "FOO=BAR"))
        assert result == {"KEY": "VAL", "FOO": "BAR"}

    def test_empty(self):
        assert _parse_env_args(()) == {}

    def test_invalid_no_equals(self):
        import click
        with pytest.raises(click.BadParameter):
            _parse_env_args(("INVALID",))

    def test_value_with_equals(self):
        # KEY=foo=bar should keep "foo=bar" as value
        result = _parse_env_args(("KEY=foo=bar",))
        assert result == {"KEY": "foo=bar"}


# ---------- server list ----------

class TestServerList:
    def test_list_when_hub_down_shows_disk_config(self, temp_config, runner):
        _seed_config([{"name": "alpha"}, {"name": "beta", "enabled": False}])

        with patch("slm_mcp_hub.cli.server_commands._get_status_detail") as m:
            m.return_value = {"servers": None, "error": "Hub is not running"}
            result = runner.invoke(server, ["list"])

        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "Hub not running" in result.output

    def test_list_when_hub_running(self, temp_config, runner):
        with patch("slm_mcp_hub.cli.server_commands._get_status_detail") as m:
            m.return_value = {
                "servers": [
                    {"name": "alpha", "transport": "stdio", "enabled": True, "connected": True, "tools": 3, "connect_time_ms": 250},
                    {"name": "beta", "transport": "http", "enabled": True, "connected": False, "tools": 0, "connect_time_ms": 5000, "error": "timeout"},
                ]
            }
            result = runner.invoke(server, ["list", "--show-tools"])

        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "connected" in result.output
        assert "beta" in result.output
        assert "failed" in result.output
        assert "timeout" in result.output


# ---------- server add ----------

class TestServerAdd:
    def test_add_stdio_happy_path(self, temp_config, runner):
        _seed_config([])

        with patch("slm_mcp_hub.cli.server_commands._post_reload") as m:
            m.return_value = {"success": True, "summary": "+1 ~0 -0 =0 unchanged"}
            result = runner.invoke(server, [
                "add", "alpha", "--command", "echo", "--arg", "hello", "--env", "KEY=val",
            ])

        assert result.exit_code == 0
        assert "Added 'alpha'" in result.output
        assert "Hot-reload applied" in result.output

        # Verify persisted to disk
        from slm_mcp_hub.core.config import load_config
        cfg = load_config()
        assert any(s.name == "alpha" for s in cfg.mcp_servers)
        alpha = next(s for s in cfg.mcp_servers if s.name == "alpha")
        assert alpha.command == "echo"
        assert alpha.args == ("hello",)
        assert alpha.env == {"KEY": "val"}

    def test_add_duplicate_rejected(self, temp_config, runner):
        _seed_config([{"name": "alpha"}])
        result = runner.invoke(server, ["add", "alpha", "--command", "echo"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_add_stdio_without_command_rejected(self, temp_config, runner):
        _seed_config([])
        result = runner.invoke(server, ["add", "alpha"])
        assert result.exit_code == 1
        assert "requires --command" in result.output

    def test_add_http_without_url_rejected(self, temp_config, runner):
        _seed_config([])
        result = runner.invoke(server, ["add", "alpha", "--type", "http"])
        assert result.exit_code == 1
        assert "requires --url" in result.output

    def test_add_no_reload_skips_api_call(self, temp_config, runner):
        _seed_config([])
        with patch("slm_mcp_hub.cli.server_commands._post_reload") as m:
            result = runner.invoke(server, [
                "add", "alpha", "--command", "echo", "--no-reload",
            ])
        assert result.exit_code == 0
        m.assert_not_called()
        assert "Skipped hot-reload" in result.output

    def test_add_http_with_headers(self, temp_config, runner):
        _seed_config([])
        with patch("slm_mcp_hub.cli.server_commands._post_reload") as m:
            m.return_value = {"success": True, "summary": "ok"}
            result = runner.invoke(server, [
                "add", "remote", "--type", "http", "--url", "https://x.example/mcp",
                "--header", "Authorization=Bearer xyz",
            ])
        assert result.exit_code == 0
        from slm_mcp_hub.core.config import load_config
        cfg = load_config()
        remote = next(s for s in cfg.mcp_servers if s.name == "remote")
        assert remote.url == "https://x.example/mcp"
        assert remote.headers == {"Authorization": "Bearer xyz"}


# ---------- server remove ----------

class TestServerRemove:
    def test_remove_happy_path(self, temp_config, runner):
        _seed_config([{"name": "alpha"}, {"name": "beta"}])
        with patch("slm_mcp_hub.cli.server_commands._post_reload") as m:
            m.return_value = {"success": True, "summary": "+0 ~0 -1 =1 unchanged"}
            result = runner.invoke(server, ["remove", "alpha"])
        assert result.exit_code == 0
        assert "Removed 'alpha'" in result.output
        from slm_mcp_hub.core.config import load_config
        cfg = load_config()
        names = {s.name for s in cfg.mcp_servers}
        assert "alpha" not in names
        assert "beta" in names

    def test_remove_not_found(self, temp_config, runner):
        _seed_config([{"name": "alpha"}])
        result = runner.invoke(server, ["remove", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output


# ---------- server modify ----------

class TestServerModify:
    def test_modify_env(self, temp_config, runner):
        _seed_config([{"name": "alpha", "env": {"K": "v1"}}])
        with patch("slm_mcp_hub.cli.server_commands._post_reload") as m:
            m.return_value = {"success": True, "summary": "ok"}
            result = runner.invoke(server, ["modify", "alpha", "--env", "K=v2"])
        assert result.exit_code == 0
        from slm_mcp_hub.core.config import load_config
        alpha = next(s for s in load_config().mcp_servers if s.name == "alpha")
        assert alpha.env == {"K": "v2"}

    def test_modify_disable(self, temp_config, runner):
        _seed_config([{"name": "alpha"}])
        with patch("slm_mcp_hub.cli.server_commands._post_reload") as m:
            m.return_value = {"success": True, "summary": "ok"}
            result = runner.invoke(server, ["modify", "alpha", "--disabled"])
        assert result.exit_code == 0
        from slm_mcp_hub.core.config import load_config
        alpha = next(s for s in load_config().mcp_servers if s.name == "alpha")
        assert alpha.enabled is False

    def test_modify_empty_change_rejected(self, temp_config, runner):
        _seed_config([{"name": "alpha"}])
        result = runner.invoke(server, ["modify", "alpha"])
        assert result.exit_code == 1
        assert "No changes specified" in result.output

    def test_modify_not_found(self, temp_config, runner):
        _seed_config([{"name": "alpha"}])
        result = runner.invoke(server, ["modify", "nope", "--enabled"])
        assert result.exit_code == 1
        assert "not found" in result.output


# ---------- server reload ----------

class TestServerReload:
    def test_reload_success(self, temp_config, runner):
        with patch("slm_mcp_hub.cli.server_commands._post_reload") as m:
            m.return_value = {
                "success": True,
                "summary": "+1 ~1 -1 =3 unchanged",
                "added": ["new"],
                "removed": ["old"],
                "modified": ["changed"],
            }
            result = runner.invoke(server, ["reload"])
        assert result.exit_code == 0
        assert "+1 ~1 -1" in result.output
        assert "new" in result.output
        assert "old" in result.output
        assert "changed" in result.output

    def test_reload_failure_exits_nonzero(self, temp_config, runner):
        with patch("slm_mcp_hub.cli.server_commands._post_reload") as m:
            m.return_value = {"success": False, "error": "bad config"}
            result = runner.invoke(server, ["reload"])
        assert result.exit_code == 1
        assert "Reload failed" in result.output


# ---------- server status ----------

class TestServerStatus:
    def test_status_specific(self, temp_config, runner):
        with patch("slm_mcp_hub.cli.server_commands._get_status_detail") as m:
            m.return_value = {
                "servers": [
                    {"name": "alpha", "transport": "stdio", "enabled": True, "connected": True, "tools": 5, "connect_time_ms": 100},
                ]
            }
            result = runner.invoke(server, ["status", "alpha"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "tools" in result.output

    def test_status_not_found(self, temp_config, runner):
        with patch("slm_mcp_hub.cli.server_commands._get_status_detail") as m:
            m.return_value = {"servers": [{"name": "alpha", "transport": "stdio", "enabled": True, "connected": True, "tools": 0, "connect_time_ms": 0}]}
            result = runner.invoke(server, ["status", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_status_hub_down(self, temp_config, runner):
        with patch("slm_mcp_hub.cli.server_commands._get_status_detail") as m:
            m.return_value = {"servers": None, "error": "Hub is not running"}
            result = runner.invoke(server, ["status"])
        assert result.exit_code == 1
        assert "not running" in result.output
