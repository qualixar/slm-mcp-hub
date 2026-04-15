"""Tests for ConnectionManager — the real MCP federation orchestrator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.manager import ConnectionManager


@pytest.fixture()
def registry() -> CapabilityRegistry:
    return CapabilityRegistry()


@pytest.fixture()
def config_with_servers(tmp_path) -> HubConfig:
    return HubConfig(
        config_dir=tmp_path,
        mcp_servers=(
            MCPServerConfig(name="server_a", transport="stdio", command="echo", args=("hello",)),
            MCPServerConfig(name="server_b", transport="stdio", command="echo", args=("world",)),
            MCPServerConfig(name="disabled_srv", transport="stdio", command="echo", enabled=False),
        ),
    )


@pytest.fixture()
def empty_config(tmp_path) -> HubConfig:
    return HubConfig(config_dir=tmp_path)


class TestConnectionManager:
    @pytest.mark.asyncio
    async def test_connect_all_empty_config(self, empty_config, registry) -> None:
        mgr = ConnectionManager(empty_config, registry)
        failed = await mgr.connect_all()
        assert failed == {}
        assert mgr.connected_count == 0

    @pytest.mark.asyncio
    async def test_connect_all_skips_disabled(self, config_with_servers, registry) -> None:
        """Disabled servers are not connected."""
        mock_conn = MagicMock()
        mock_conn.is_connected = False
        mock_conn.capabilities = {"tools": [], "resources": [], "resource_templates": [], "prompts": []}
        mock_conn.connect = AsyncMock(side_effect=ConnectionError("test"))

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=mock_conn):
            mgr = ConnectionManager(config_with_servers, registry)
            failed = await mgr.connect_all()

        # Only 2 enabled servers attempted (disabled_srv skipped)
        assert len(mgr.connections) == 2
        assert "disabled_srv" not in mgr.connections

    @pytest.mark.asyncio
    async def test_connect_all_success(self, config_with_servers, registry) -> None:
        """Successful connections register tools in registry."""
        def make_mock_conn(config):
            mock = MagicMock()
            mock.is_connected = True
            mock.capabilities = {
                "tools": [{"name": f"tool_{config.name}", "description": "test"}],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            }
            mock.connect = AsyncMock()
            return mock

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=make_mock_conn):
            mgr = ConnectionManager(config_with_servers, registry)
            failed = await mgr.connect_all()

        assert failed == {}
        assert mgr.connected_count == 2
        assert registry.tool_count == 2  # 2 tools from 2 servers

    @pytest.mark.asyncio
    async def test_connect_one_failure_isolated(self, config_with_servers, registry) -> None:
        """One server failing doesn't block others."""
        call_count = 0

        def make_mock_conn(config):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            if config.name == "server_a":
                mock.connect = AsyncMock(side_effect=ConnectionError("server_a down"))
                mock.is_connected = False
            else:
                mock.connect = AsyncMock()
                mock.is_connected = True
            mock.capabilities = {
                "tools": [{"name": "tool_b", "description": "test"}] if config.name == "server_b" else [],
                "resources": [], "resource_templates": [], "prompts": [],
            }
            return mock

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=make_mock_conn):
            mgr = ConnectionManager(config_with_servers, registry)
            failed = await mgr.connect_all()

        assert "server_a" in failed
        assert mgr.connected_count == 1
        assert registry.tool_count == 1

    @pytest.mark.asyncio
    async def test_disconnect_all(self, config_with_servers, registry) -> None:
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.disconnect = AsyncMock()
        mock_conn.connect = AsyncMock()
        mock_conn.capabilities = {"tools": [], "resources": [], "resource_templates": [], "prompts": []}

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=mock_conn):
            mgr = ConnectionManager(config_with_servers, registry)
            await mgr.connect_all()
            assert len(mgr.connections) == 2

            await mgr.disconnect_all()
            assert len(mgr.connections) == 0

    @pytest.mark.asyncio
    async def test_connect_one_by_name(self, config_with_servers, registry) -> None:
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.connect = AsyncMock()
        mock_conn.capabilities = {
            "tools": [{"name": "test_tool", "description": "test"}],
            "resources": [], "resource_templates": [], "prompts": [],
        }

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=mock_conn):
            mgr = ConnectionManager(config_with_servers, registry)
            result = await mgr.connect_one("server_a")

        assert result is True
        assert registry.tool_count == 1

    @pytest.mark.asyncio
    async def test_connect_one_unknown_name(self, config_with_servers, registry) -> None:
        mgr = ConnectionManager(config_with_servers, registry)
        result = await mgr.connect_one("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_disconnect_one(self, config_with_servers, registry) -> None:
        def make_mock(cfg):
            m = MagicMock()
            m.is_connected = True
            m.disconnect = AsyncMock()
            m.connect = AsyncMock()
            m.capabilities = {
                "tools": [{"name": f"tool_{cfg.name}", "description": ""}],
                "resources": [], "resource_templates": [], "prompts": [],
            }
            return m

        with patch("slm_mcp_hub.federation.manager.MCPConnection", side_effect=make_mock):
            mgr = ConnectionManager(config_with_servers, registry)
            await mgr.connect_all()
            assert registry.tool_count == 2

            # After disconnect, mark it as not connected so sync removes it
            conn_a = mgr.connections["server_a"]
            conn_a.is_connected = False
            await mgr.disconnect_one("server_a")
            # Only server_b remains in registry
            assert registry.tool_count == 1

    @pytest.mark.asyncio
    async def test_failed_servers_property(self, config_with_servers, registry) -> None:
        mock_conn = MagicMock()
        mock_conn.is_connected = False
        mock_conn.connect = AsyncMock(side_effect=ConnectionError("down"))
        mock_conn.capabilities = {"tools": [], "resources": [], "resource_templates": [], "prompts": []}

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=mock_conn):
            mgr = ConnectionManager(config_with_servers, registry)
            await mgr.connect_all()

        failed = mgr.failed_servers
        assert len(failed) == 2
        assert "server_a" in failed
        assert "server_b" in failed

    @pytest.mark.asyncio
    async def test_disconnect_one_nonexistent(self, empty_config, registry) -> None:
        """Disconnecting a server that doesn't exist is a no-op."""
        mgr = ConnectionManager(empty_config, registry)
        await mgr._disconnect_one("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_disconnect_error_isolated(self, config_with_servers, registry) -> None:
        """Disconnect error on one server doesn't block others."""
        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock(side_effect=RuntimeError("cleanup error"))
        mock_conn.capabilities = {"tools": [], "resources": [], "resource_templates": [], "prompts": []}

        with patch("slm_mcp_hub.federation.manager.MCPConnection", return_value=mock_conn):
            mgr = ConnectionManager(config_with_servers, registry)
            await mgr.connect_all()
            await mgr.disconnect_all()  # Should not raise

        assert len(mgr.connections) == 0
