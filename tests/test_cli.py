"""CLI tests for Phase 0."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from slm_mcp_hub.cli.main import cli, main
from slm_mcp_hub.core.hub import reset_hub


runner = CliRunner()


class TestCLI:
    def setup_method(self):
        reset_hub()

    def test_cli_version(self):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_cli_status_not_running(self):
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "not running" in result.output

    def test_cli_config_show_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SLM_HUB_CONFIG_DIR", str(tmp_path))
        result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        assert "Port:" in result.output
        assert "52414" in result.output

    def test_cli_config_init(self, tmp_path):
        path = tmp_path / "config.json"
        # We can't easily override CONFIG_FILE in the CLI, so test the function directly
        from slm_mcp_hub.core.config import generate_default_config
        config = generate_default_config(path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert "mcpServers" in data
        assert data["port"] == 52414

    def test_cli_config_import_claude(self, tmp_path):
        # Create a sample claude.json
        claude_data = {
            "mcpServers": {
                "test-mcp": {
                    "command": "echo",
                    "args": ["test"],
                }
            }
        }
        claude_path = tmp_path / "claude.json"
        claude_path.write_text(json.dumps(claude_data))

        # Create a hub config to merge into
        from slm_mcp_hub.core.config import generate_default_config
        hub_config_path = tmp_path / "config.json"
        generate_default_config(hub_config_path)

        result = runner.invoke(cli, ["config", "import", str(claude_path), "--format", "claude"])
        assert result.exit_code == 0
        assert "1" in result.output  # 1 server imported

    def test_cli_config_import_auto_detect(self, tmp_path):
        claude_data = {"mcpServers": {"auto-test": {"command": "echo", "args": []}}}
        path = tmp_path / "auto.json"
        path.write_text(json.dumps(claude_data))

        from slm_mcp_hub.core.config import generate_default_config
        generate_default_config(tmp_path / "config.json")

        result = runner.invoke(cli, ["config", "import", str(path)])
        assert result.exit_code == 0

    def test_cli_config_import_auto_detect_vscode(self, tmp_path):
        """Auto-detect vscode format when 'servers' key present (line 133)."""
        vscode_data = {"servers": {"vs-srv": {"command": "node", "args": ["server.js"]}}}
        path = tmp_path / "vscode.json"
        path.write_text(json.dumps(vscode_data))

        from slm_mcp_hub.core.config import generate_default_config
        generate_default_config(tmp_path / "config.json")

        result = runner.invoke(cli, ["config", "import", str(path)])
        assert result.exit_code == 0
        assert "1" in result.output

    def test_cli_start_command(self, tmp_path, monkeypatch):
        """Test start command (lines 46-77): mock asyncio.run to avoid blocking."""
        config_data = {"host": "127.0.0.1", "port": 55555, "mcpServers": {}}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data))

        pid_file = tmp_path / "hub.pid"
        monkeypatch.setattr("slm_mcp_hub.cli.main.PID_FILE", pid_file)

        # Mock asyncio.run to simulate the start command completing
        def mock_asyncio_run(coro):
            # Close the coroutine to prevent "was never awaited" warning
            coro.close()

        monkeypatch.setattr("slm_mcp_hub.cli.main.asyncio.run", mock_asyncio_run)

        result = runner.invoke(cli, ["start", "--config", str(config_path)])
        # The command should have run
        assert result.exit_code == 0

    def test_cli_start_keyboard_interrupt(self, tmp_path, monkeypatch):
        """Test start command handles KeyboardInterrupt (line 76-77)."""
        config_data = {"host": "127.0.0.1", "port": 55555, "mcpServers": {}}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data))

        pid_file = tmp_path / "hub.pid"
        monkeypatch.setattr("slm_mcp_hub.cli.main.PID_FILE", pid_file)

        def mock_asyncio_run(coro):
            coro.close()
            raise KeyboardInterrupt()

        monkeypatch.setattr("slm_mcp_hub.cli.main.asyncio.run", mock_asyncio_run)

        result = runner.invoke(cli, ["start", "--config", str(config_path)])
        assert "stopped" in result.output.lower() or result.exit_code == 0

    def test_cli_status_running(self, tmp_path, monkeypatch):
        """Test status when hub IS running (lines 84-88): PID file exists."""
        pid_file = tmp_path / "hub.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr("slm_mcp_hub.cli.main.PID_FILE", pid_file)
        # Patch CONFIG_FILE in both modules
        monkeypatch.setattr("slm_mcp_hub.cli.main.CONFIG_FILE", tmp_path / "nonexistent.json")
        monkeypatch.setattr("slm_mcp_hub.core.config.CONFIG_FILE", tmp_path / "nonexistent.json")

        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "running" in result.output.lower()
        assert "52414" in result.output  # Default port

    def test_cli_config_show_http_server(self, tmp_path, monkeypatch):
        """Test config show with HTTP transport server (line 118)."""
        config_data = {
            "host": "127.0.0.1",
            "port": 52414,
            "mcpServers": {
                "http-srv": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "enabled": True,
                }
            },
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data))

        # Patch CONFIG_FILE in the config module (where load_config reads from)
        monkeypatch.setattr("slm_mcp_hub.core.config.CONFIG_FILE", config_path)

        result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        assert "http-srv" in result.output
        assert "https://example.com/mcp" in result.output

    def test_cli_config_init_decline_overwrite(self, tmp_path, monkeypatch):
        """Test config init when config exists and user declines (line 168)."""
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        monkeypatch.setattr("slm_mcp_hub.cli.main.CONFIG_FILE", config_path)

        # Simulate user saying 'n' to overwrite
        result = runner.invoke(cli, ["config", "init"], input="n\n")
        assert result.exit_code == 0
        assert "Overwrite?" in result.output or "already exists" in result.output

    def test_main_function(self):
        """Test main() entry point (line 179)."""
        # main() just calls cli(), so invoke with --help to verify it works
        with patch("slm_mcp_hub.cli.main.cli") as mock_cli:
            main()
            mock_cli.assert_called_once()

    def test_cli_start_runs_coroutine(self, tmp_path, monkeypatch):
        """Test start command runs the async coroutine with uvicorn wiring."""
        config_data = {"host": "127.0.0.1", "port": 55555, "mcpServers": {}}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data))

        pid_file = tmp_path / "hub.pid"
        monkeypatch.setattr("slm_mcp_hub.cli.main.PID_FILE", pid_file)

        hub_mock = MagicMock()
        hub_mock.plugins = []
        hub_mock.get_status = MagicMock(return_value={"state": "ready"})

        mock_hub_cls = MagicMock()
        mock_instance = AsyncMock()
        mock_instance.__aenter__ = AsyncMock(return_value=hub_mock)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_hub_cls.return_value = mock_instance

        monkeypatch.setattr("slm_mcp_hub.cli.main.HubOrchestrator", mock_hub_cls)

        # Mock uvicorn.Server.serve to return immediately
        mock_server = MagicMock()
        mock_server.serve = AsyncMock()
        mock_server_cls = MagicMock(return_value=mock_server)

        import uvicorn
        monkeypatch.setattr(uvicorn, "Server", mock_server_cls)

        result = runner.invoke(cli, ["start", "--config", str(config_path)])
        assert result.exit_code == 0
        assert not pid_file.exists()  # Cleaned up in finally block

    def test_cli_start_with_port_override(self, tmp_path, monkeypatch):
        """Test start --port override (line 51)."""
        config_data = {"host": "127.0.0.1", "port": 55555, "mcpServers": {}}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data))

        pid_file = tmp_path / "hub.pid"
        monkeypatch.setattr("slm_mcp_hub.cli.main.PID_FILE", pid_file)

        hub_mock = MagicMock()
        hub_mock.plugins = []
        hub_mock.get_status = MagicMock(return_value={"state": "ready"})

        mock_hub_cls = MagicMock()
        mock_instance = AsyncMock()
        mock_instance.__aenter__ = AsyncMock(return_value=hub_mock)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_hub_cls.return_value = mock_instance

        monkeypatch.setattr("slm_mcp_hub.cli.main.HubOrchestrator", mock_hub_cls)

        import uvicorn
        mock_server = MagicMock()
        mock_server.serve = AsyncMock()
        monkeypatch.setattr(uvicorn, "Server", MagicMock(return_value=mock_server))

        result = runner.invoke(cli, ["start", "--config", str(config_path), "--port", "9999"])
        assert result.exit_code == 0
        call_args = mock_hub_cls.call_args[0][0]
        assert call_args.port == 9999

