"""Tests for HubRuntime — Phase 1 extraction verification.

Validates that HubRuntime correctly owns the object graph that
previously lived inside cli/main.py:start()._run().
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.hub import reset_hub


@pytest.fixture(autouse=True)
def _reset_hub_singleton():
    """Ensure hub singleton is clean before and after each test."""
    reset_hub()
    yield
    reset_hub()


def _make_config(servers: list[MCPServerConfig] | None = None) -> HubConfig:
    return HubConfig(
        host="127.0.0.1",
        port=52414,
        mcp_servers=tuple(servers or []),
    )


def _make_server_config(name: str = "test-srv") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command="/usr/bin/echo",
        args=["hello"],
        enabled=True,
    )


class TestHubRuntimeConstruction:
    """Verify HubRuntime constructs all runtime objects correctly."""

    @pytest.mark.asyncio
    async def test_runtime_creates_all_components(self):
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        config = _make_config([_make_server_config()])
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)

            assert runtime.hub is hub
            assert runtime.config is hub.config
            assert runtime.registry is not None
            assert runtime.conn_manager is not None
            assert runtime.router is not None
            assert runtime.session_manager is not None
            assert runtime.mcp_endpoint is not None
            assert runtime.proxy is not None

    @pytest.mark.asyncio
    async def test_runtime_registry_starts_empty(self):
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            assert runtime.registry.tool_count == 0

    @pytest.mark.asyncio
    async def test_runtime_session_manager_respects_config(self):
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        config = HubConfig(
            host="127.0.0.1",
            port=52414,
            max_sessions=42,
            session_timeout_seconds=999,
            mcp_servers=(),
        )
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            assert runtime.session_manager.max_sessions == 42


class TestHubRuntimeLifecycle:
    """Verify connect_all / disconnect_all delegate correctly."""

    @pytest.mark.asyncio
    async def test_connect_all_delegates_to_conn_manager(self):
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            result = await runtime.connect_all()
            assert result == {}

    @pytest.mark.asyncio
    async def test_disconnect_all_delegates_to_conn_manager(self):
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            await runtime.disconnect_all()


class TestHubRuntimeStatus:
    """Verify get_status combines hub + runtime info."""

    @pytest.mark.asyncio
    async def test_get_status_includes_runtime_fields(self):
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            status = runtime.get_status()

            assert "state" in status
            assert "version" in status
            assert "servers_connected" in status
            assert "tools_registered" in status
            assert "sessions_active" in status
            assert status["servers_connected"] == 0
            assert status["tools_registered"] == 0
            assert status["sessions_active"] == 0
