"""Tests for Phase 6 — Desktop detection, stdio auto-register, fast retry, status --verbose.

Coverage:
- Claude Desktop in _build_known_clients (correct path per platform)
- _build_hub_entry stdio mode emits {command, args, env}
- _build_hub_entry http mode unchanged (backward compat)
- AutoRegister(transport="stdio") writes correct entry to disk
- AutoRegister(transport="http") still writes HTTP entry (existing tests should still pass)
- ConnectionManager.fast_retry_failed retries with 0.5/1.5/4.5s schedule
- ConnectionManager.fast_retry_failed succeeds on second attempt
- ConnectionManager.fast_retry_failed leaves still-failed in _failed
"""

from __future__ import annotations

import json
import platform
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.discovery.auto_register import AutoRegister, _build_hub_entry
from slm_mcp_hub.discovery.client_detector import (
    ClientDetector,
    DetectedClient,
    _build_known_clients,
)
from slm_mcp_hub.federation.manager import ConnectionManager

# ---------- client_detector: Claude Desktop ----------

class TestClaudeDesktopDetection:
    def test_claude_desktop_is_in_known_clients(self):
        clients = _build_known_clients()
        names = [c.name for c in clients]
        assert "claude_desktop" in names

    def test_claude_desktop_config_path_is_platform_correct(self):
        clients = _build_known_clients()
        desktop = next(c for c in clients if c.name == "claude_desktop")
        path_str = str(desktop.config_paths[0])
        if platform.system() == "Darwin":
            assert "Library/Application Support/Claude" in path_str
            assert "claude_desktop_config.json" in path_str
        else:
            assert ".config/Claude" in path_str

    def test_claude_desktop_uses_mcpServers_key(self):
        clients = _build_known_clients()
        desktop = next(c for c in clients if c.name == "claude_desktop")
        assert desktop.mcp_key == "mcpServers"
        assert desktop.config_format == "claude"

    def test_detect_finds_claude_desktop_when_config_exists(self, tmp_path):
        # Write a fake Claude Desktop config
        cfg_file = tmp_path / "claude_desktop_config.json"
        cfg_file.write_text(json.dumps({"mcpServers": {"existing": {}}}))

        # Build a custom detector pointing at our tmp file
        from slm_mcp_hub.discovery.client_detector import ClientConfig
        custom = (
            ClientConfig(
                name="claude_desktop",
                display_name="Claude Desktop",
                config_paths=(cfg_file,),
                mcp_key="mcpServers",
                config_format="claude",
            ),
        )
        detector = ClientDetector(known_clients=custom)
        detected = detector.detect_all()
        assert len(detected) == 1
        assert detected[0].name == "claude_desktop"
        assert detected[0].mcp_count == 1


# ---------- auto_register: stdio entry mode ----------

class TestStdioHubEntry:
    def test_build_hub_entry_http_default(self):
        entry = _build_hub_entry("http://127.0.0.1:52414/mcp")
        assert entry == {"type": "http", "url": "http://127.0.0.1:52414/mcp"}

    def test_build_hub_entry_stdio_basic(self):
        entry = _build_hub_entry("ignored", transport="stdio")
        assert entry == {"command": "slm-hub", "args": ["mcp"]}

    def test_build_hub_entry_stdio_with_env(self):
        entry = _build_hub_entry(
            "ignored", transport="stdio",
            env={"SLM_HUB_AGENT_ID": "claude-desktop"},
        )
        assert entry["command"] == "slm-hub"
        assert entry["args"] == ["mcp"]
        assert entry["env"] == {"SLM_HUB_AGENT_ID": "claude-desktop"}

    def test_build_hub_entry_stdio_custom_command(self):
        entry = _build_hub_entry(
            "ignored", transport="stdio",
            command="/usr/local/bin/slm-hub", args=("mcp", "--log-level", "ERROR"),
        )
        assert entry["command"] == "/usr/local/bin/slm-hub"
        assert entry["args"] == ["mcp", "--log-level", "ERROR"]


class TestAutoRegisterStdio:
    def test_register_stdio_writes_command_entry_to_disk(self, tmp_path):
        cfg_file = tmp_path / "claude_desktop_config.json"
        cfg_file.write_text(json.dumps({"mcpServers": {}}))

        client = DetectedClient(
            name="claude_desktop",
            display_name="Claude Desktop",
            config_path=cfg_file,
            mcp_count=0,
            hub_registered=False,
            config_format="claude",
        )

        registrar = AutoRegister(transport="stdio")
        result = registrar.register(client, mcp_key="mcpServers")
        assert result.success is True

        written = json.loads(cfg_file.read_text())
        assert "hub" in written["mcpServers"]
        hub_entry = written["mcpServers"]["hub"]
        assert hub_entry["command"] == "slm-hub"
        assert hub_entry["args"] == ["mcp"]
        # No "type" or "url" — stdio entries are clean
        assert "type" not in hub_entry
        assert "url" not in hub_entry

    def test_register_http_default_unchanged(self, tmp_path):
        """Backward compat: default transport='http' still writes HTTP entries."""
        cfg_file = tmp_path / "claude_code_config.json"
        cfg_file.write_text(json.dumps({"mcpServers": {}}))

        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=cfg_file,
            mcp_count=0,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister("http://127.0.0.1:52414/mcp")  # default transport
        result = registrar.register(client, mcp_key="mcpServers")
        assert result.success is True

        written = json.loads(cfg_file.read_text())
        hub_entry = written["mcpServers"]["hub"]
        assert hub_entry["type"] == "http"
        assert "command" not in hub_entry


# ---------- ConnectionManager.fast_retry_failed ----------

class TestFastRetry:
    @pytest.fixture(autouse=True)
    def speed_up_delays(self, monkeypatch):
        """Replace 0.5/1.5/4.5 with near-zero delays so tests stay fast.
        Production code still uses the real schedule — these tests verify
        the LOOP STRUCTURE (3 attempts, retries failed servers), not the
        wall-clock timing."""
        monkeypatch.setattr(
            "slm_mcp_hub.federation.manager._FAST_RETRY_DELAYS_S",
            (0.001, 0.001, 0.001),
        )

    @pytest.mark.asyncio
    async def test_fast_retry_succeeds_on_second_attempt(self, tmp_path):
        cfg = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(MCPServerConfig(name="flaky", transport="stdio", command="echo"),),
        )
        registry = CapabilityRegistry()

        # Mock connect: fail first 2 times, succeed third
        attempt_counter = {"n": 0}

        def conn_factory(srv_cfg):
            mock = MagicMock()
            attempt_counter["n"] += 1
            if attempt_counter["n"] < 3:
                mock.connect = AsyncMock(side_effect=ConnectionError("transient"))
                mock.is_connected = False
            else:
                mock.connect = AsyncMock()
                mock.is_connected = True
            mock.disconnect = AsyncMock()
            mock.capabilities = {"tools": [{"name": "ping"}], "resources": [], "resource_templates": [], "prompts": []}
            return mock

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=conn_factory):
            mgr = ConnectionManager(cfg, registry)
            await mgr.connect_all()
            # After initial: 1 attempt, failed
            assert "flaky" in mgr._failed

            still_failed = await mgr.fast_retry_failed()
            # After fast retry attempts 2 and 3 — succeeded on attempt 3
            assert still_failed == {}
            assert "flaky" not in mgr._failed
            assert mgr._connections["flaky"].is_connected

    @pytest.mark.asyncio
    async def test_fast_retry_gives_up_after_3_attempts(self, tmp_path):
        cfg = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(MCPServerConfig(name="dead", transport="stdio", command="echo"),),
        )
        registry = CapabilityRegistry()

        def conn_factory(srv_cfg):
            mock = MagicMock()
            mock.connect = AsyncMock(side_effect=ConnectionError("permanently broken"))
            mock.is_connected = False
            mock.disconnect = AsyncMock()
            mock.capabilities = {"tools": [], "resources": [], "resource_templates": [], "prompts": []}
            return mock

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=conn_factory):
            mgr = ConnectionManager(cfg, registry)
            # Don't run the slow background retry loop in this test
            mgr._start_retry_loop = MagicMock()
            await mgr.connect_all()
            still_failed = await mgr.fast_retry_failed()

        assert "dead" in still_failed
        assert "permanently broken" in still_failed["dead"]

    @pytest.mark.asyncio
    async def test_fast_retry_no_op_when_no_failures(self, tmp_path):
        cfg = HubConfig(config_dir=tmp_path, mcp_servers=())
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)
        # No failures → returns immediately, no sleep
        result = await mgr.fast_retry_failed()
        assert result == {}

    @pytest.mark.asyncio
    async def test_fast_retry_respects_shutdown(self, tmp_path):
        cfg = HubConfig(
            config_dir=tmp_path,
            mcp_servers=(MCPServerConfig(name="x", transport="stdio", command="echo"),),
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)
        mgr._failed = {"x": "initial fail"}
        mgr._shutdown = True

        # Shutdown set → fast retry should bail out immediately
        result = await mgr.fast_retry_failed()
        # State unchanged — no retry attempts made
        assert result == {"x": "initial fail"}


# ---------- status --verbose CLI ----------

class TestStatusVerbose:
    def test_status_verbose_help_lists_flag(self):
        """The --verbose flag is documented in status --help."""
        from click.testing import CliRunner

        from slm_mcp_hub.cli.main import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--help"])
        assert "verbose" in result.output.lower()
        assert "per-server" in result.output.lower()
