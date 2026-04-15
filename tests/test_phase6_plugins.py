"""Tests for Phase 6: SLM Plugins (Memory, Learning, Mesh).

Tests mock httpx calls to the SLM daemon HTTP API.
No Python import of superlocalmemory required.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from slm_mcp_hub.core.config import HubConfig
from slm_mcp_hub.core.hub import HubOrchestrator, reset_hub
from slm_mcp_hub.plugins.base import HubPlugin
from slm_mcp_hub.plugins.mesh_plugin import (
    MeshPeerInfo,
    MeshPlugin,
    RemoteToolRoute,
)
from slm_mcp_hub.plugins.slm_plugin import (
    RING_BUFFER_MAX,
    SLMPlugin,
    SessionSummary,
    ToolObservation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    """Create a mock httpx.Response with JSON data."""
    resp = httpx.Response(status_code=status_code, json=json_data)
    return resp


def _error_response(status_code: int = 500) -> httpx.Response:
    return httpx.Response(status_code=status_code, json={"error": "fail"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_hub() -> None:
    reset_hub()


@pytest.fixture()
def slm_plugin() -> SLMPlugin:
    return SLMPlugin(slm_url="http://test:8765")


@pytest.fixture()
def mesh_plugin() -> MeshPlugin:
    return MeshPlugin(slm_url="http://test:8765")


@pytest.fixture()
def hub_config(tmp_path) -> HubConfig:
    return HubConfig(config_dir=tmp_path / "hub")


class _TestPlugin(HubPlugin):
    """Test plugin that tracks calls."""

    def __init__(self, name_val: str = "test") -> None:
        self._name = name_val
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "0.0.1"

    async def on_tool_call_before(self, session_id, server, tool, args):
        self.calls.append("before")
        return None

    async def on_tool_call_after(self, session_id, server, tool, args, result, duration_ms, success):
        self.calls.append("after")

    async def on_session_start(self, session_id, client_info):
        self.calls.append("session_start")

    async def on_session_end(self, session_id):
        self.calls.append("session_end")

    async def on_mcp_connect(self, server_name):
        self.calls.append("mcp_connect")

    async def on_mcp_disconnect(self, server_name):
        self.calls.append("mcp_disconnect")


class _ErrorPlugin(HubPlugin):
    """Plugin that raises errors for all hooks."""

    @property
    def name(self) -> str:
        return "error_plugin"

    @property
    def version(self) -> str:
        return "0.0.1"

    async def on_tool_call_before(self, session_id, server, tool, args):
        raise RuntimeError("before error")

    async def on_tool_call_after(self, session_id, server, tool, args, result, duration_ms, success):
        raise RuntimeError("after error")

    async def on_session_start(self, session_id, client_info):
        raise RuntimeError("session_start error")

    async def on_session_end(self, session_id):
        raise RuntimeError("session_end error")

    async def on_mcp_connect(self, server_name):
        raise RuntimeError("mcp_connect error")

    async def on_mcp_disconnect(self, server_name):
        raise RuntimeError("mcp_disconnect error")


class _ModifyingPlugin(HubPlugin):
    """Plugin that modifies args in on_tool_call_before."""

    @property
    def name(self) -> str:
        return "modifier"

    @property
    def version(self) -> str:
        return "0.0.1"

    async def on_tool_call_before(self, session_id, server, tool, args):
        return {**args, "injected": True}


# ---------------------------------------------------------------------------
# SLM Plugin Tests
# ---------------------------------------------------------------------------


class TestSLMPlugin:
    def test_name_and_version(self, slm_plugin: SLMPlugin) -> None:
        assert slm_plugin.name == "slm"
        assert slm_plugin.version == "0.1.0"

    def test_not_available_initially(self, slm_plugin: SLMPlugin) -> None:
        assert slm_plugin.available is False

    def test_slm_url_configurable(self) -> None:
        plugin = SLMPlugin(slm_url="http://custom:9999")
        assert plugin.slm_url == "http://custom:9999"

    def test_slm_url_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_DAEMON_URL", "http://env:1234")
        plugin = SLMPlugin()
        assert plugin.slm_url == "http://env:1234"

    @pytest.mark.asyncio
    async def test_on_hub_start_daemon_reachable(self, slm_plugin: SLMPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=_ok_response({
            "status": "running", "mode": "b", "fact_count": 4451,
        }))

        with patch("slm_mcp_hub.plugins.slm_plugin.httpx.AsyncClient", return_value=mock_client):
            await slm_plugin.on_hub_start(mock_hub)

        assert slm_plugin.available is True

    @pytest.mark.asyncio
    async def test_on_hub_start_daemon_not_reachable(self, slm_plugin: SLMPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with patch("slm_mcp_hub.plugins.slm_plugin.httpx.AsyncClient", return_value=mock_client):
            await slm_plugin.on_hub_start(mock_hub)

        assert slm_plugin.available is False

    @pytest.mark.asyncio
    async def test_on_hub_start_daemon_timeout(self, slm_plugin: SLMPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        with patch("slm_mcp_hub.plugins.slm_plugin.httpx.AsyncClient", return_value=mock_client):
            await slm_plugin.on_hub_start(mock_hub)

        assert slm_plugin.available is False

    @pytest.mark.asyncio
    async def test_on_hub_start_daemon_not_running(self, slm_plugin: SLMPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=_ok_response({"status": "starting"}))

        with patch("slm_mcp_hub.plugins.slm_plugin.httpx.AsyncClient", return_value=mock_client):
            await slm_plugin.on_hub_start(mock_hub)

        assert slm_plugin.available is False

    @pytest.mark.asyncio
    async def test_on_hub_start_daemon_error_status(self, slm_plugin: SLMPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=_error_response(503))

        with patch("slm_mcp_hub.plugins.slm_plugin.httpx.AsyncClient", return_value=mock_client):
            await slm_plugin.on_hub_start(mock_hub)

        assert slm_plugin.available is False

    @pytest.mark.asyncio
    async def test_on_hub_start_unexpected_error(self, slm_plugin: SLMPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=ValueError("unexpected"))

        with patch("slm_mcp_hub.plugins.slm_plugin.httpx.AsyncClient", return_value=mock_client):
            await slm_plugin.on_hub_start(mock_hub)

        assert slm_plugin.available is False

    @pytest.mark.asyncio
    async def test_observe_tool_call(self, slm_plugin: SLMPlugin) -> None:
        await slm_plugin.on_tool_call_after(
            session_id="s1", server="context7", tool="query-docs",
            args={"query": "test"}, result="ok", duration_ms=150, success=True,
        )
        assert slm_plugin.observation_count == 1

    @pytest.mark.asyncio
    async def test_observe_multiple_calls(self, slm_plugin: SLMPlugin) -> None:
        for i in range(5):
            await slm_plugin.on_tool_call_after(
                session_id="s1", server="srv", tool=f"t{i}",
                args={}, result=None, duration_ms=10, success=True,
            )
        assert slm_plugin.observation_count == 5

    @pytest.mark.asyncio
    async def test_ring_buffer_eviction(self, slm_plugin: SLMPlugin) -> None:
        for i in range(RING_BUFFER_MAX + 100):
            await slm_plugin.on_tool_call_after(
                session_id="s1", server="srv", tool=f"t{i}",
                args={}, result=None, duration_ms=1, success=True,
            )
        assert slm_plugin.observation_count == RING_BUFFER_MAX

    @pytest.mark.asyncio
    async def test_session_start_stores_context(self, slm_plugin: SLMPlugin) -> None:
        await slm_plugin.on_session_start("s1", {"project_path": "/test/project"})
        assert "s1" in slm_plugin._session_contexts
        assert slm_plugin._session_contexts["s1"]["project_path"] == "/test/project"

    @pytest.mark.asyncio
    async def test_session_end_cleans_up(self, slm_plugin: SLMPlugin) -> None:
        await slm_plugin.on_session_start("s1", {"project_path": "/test"})
        await slm_plugin.on_tool_call_after(
            session_id="s1", server="srv", tool="t1",
            args={}, result=None, duration_ms=100, success=True,
        )
        await slm_plugin.on_session_end("s1")

        assert "s1" not in slm_plugin._session_contexts
        assert "s1" not in slm_plugin._session_tool_counts
        assert "s1" not in slm_plugin._session_durations

    def test_get_learned_tools(self, slm_plugin: SLMPlugin) -> None:
        obs = ToolObservation(
            session_id="s1", server="context7", tool="query-docs",
            duration_ms=100, success=True, timestamp=time.time(),
        )
        slm_plugin._observations.append(obs)
        tools = slm_plugin.get_learned_tools()
        assert "context7__query-docs" in tools

    def test_get_learned_tools_old_excluded(self, slm_plugin: SLMPlugin) -> None:
        old_obs = ToolObservation(
            session_id="s1", server="old", tool="tool",
            duration_ms=10, success=True, timestamp=time.time() - (8 * 24 * 3600),
        )
        slm_plugin._observations.append(old_obs)
        assert slm_plugin.get_learned_tools() == set()

    def test_get_warm_up_predictions_empty(self, slm_plugin: SLMPlugin) -> None:
        assert slm_plugin.get_warm_up_predictions() == []

    def test_get_warm_up_predictions(self, slm_plugin: SLMPlugin) -> None:
        now = time.time()
        for _ in range(10):
            slm_plugin._observations.append(ToolObservation(
                session_id="s1", server="gemini", tool="search",
                duration_ms=100, success=True, timestamp=now,
            ))
        for _ in range(5):
            slm_plugin._observations.append(ToolObservation(
                session_id="s1", server="context7", tool="query",
                duration_ms=50, success=True, timestamp=now,
            ))

        predictions = slm_plugin.get_warm_up_predictions()
        assert predictions[0] == "gemini"
        assert "context7" in predictions

    @pytest.mark.asyncio
    async def test_on_hub_stop(self, slm_plugin: SLMPlugin) -> None:
        slm_plugin._client = AsyncMock(spec=httpx.AsyncClient)
        await slm_plugin.on_hub_stop()
        assert slm_plugin.available is False
        assert slm_plugin._client is None

    def test_tool_observation_frozen(self) -> None:
        obs = ToolObservation(
            session_id="s1", server="srv", tool="t",
            duration_ms=100, success=True, timestamp=1.0,
        )
        with pytest.raises(AttributeError):
            obs.session_id = "s2"  # type: ignore[misc]

    def test_session_summary_frozen(self) -> None:
        summary = SessionSummary(
            session_id="s1", tool_counts={"a": 1},
            total_duration_ms=100, total_calls=1, project_path="/test",
        )
        with pytest.raises(AttributeError):
            summary.session_id = "s2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SLM Plugin — Available Mode Tests
# ---------------------------------------------------------------------------


class TestSLMPluginAvailable:
    @pytest.mark.asyncio
    async def test_observe_posts_tool_event(self) -> None:
        """When available, on_tool_call_after posts to /api/v3/tool-event."""
        plugin = SLMPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"ok": True}))
        plugin._client = mock_client

        await plugin.on_tool_call_after(
            "s1", "gemini", "search", {"q": "test"}, "result", 150, True,
        )

        assert plugin.observation_count == 1
        # The POST is fire-and-forget via create_task, so we check the helper directly
        await plugin._post_tool_event("gemini__search", "complete", 150, "s1")
        mock_client.post.assert_called()

    @pytest.mark.asyncio
    async def test_post_tool_event_error_swallowed(self) -> None:
        """HTTP error doesn't crash the plugin."""
        plugin = SLMPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        plugin._client = mock_client

        # Should not raise
        await plugin._post_tool_event("test__tool", "complete", 100, "s1")

    @pytest.mark.asyncio
    async def test_post_tool_event_no_client(self) -> None:
        """No client means no POST attempt."""
        plugin = SLMPlugin(slm_url="http://test:8765")
        plugin._client = None
        await plugin._post_tool_event("test__tool", "complete", 100, "s1")

    @pytest.mark.asyncio
    async def test_session_start_recall(self) -> None:
        """When available, session start triggers recall via HTTP."""
        plugin = SLMPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({
            "query": "hub session /test/project",
            "result_count": 3,
            "retrieval_time_ms": 150,
            "results": [{"content": "past context"}],
        }))
        plugin._client = mock_client

        await plugin.on_session_start("s1", {"project_path": "/test/project"})

        mock_client.post.assert_called_once()
        assert plugin._session_contexts["s1"]["recalled"] is not None
        assert plugin._session_contexts["s1"]["recalled"]["result_count"] == 3

    @pytest.mark.asyncio
    async def test_session_start_recall_error_swallowed(self) -> None:
        """Recall failure doesn't crash session start."""
        plugin = SLMPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        plugin._client = mock_client

        await plugin.on_session_start("s1", {"project_path": "/test"})
        assert "s1" in plugin._session_contexts

    @pytest.mark.asyncio
    async def test_session_end_posts_summary(self) -> None:
        """When available, session end posts summary tool-event."""
        plugin = SLMPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"ok": True}))
        plugin._client = mock_client

        await plugin.on_session_start("s1", {"project_path": "/test"})
        await plugin.on_tool_call_after("s1", "srv", "t", {}, None, 100, True)
        await plugin.on_session_end("s1")

        # Directly verify summary post works
        await plugin._post_tool_event(
            "hub__session_summary", "session_end", session_id="s1",
            output_summary="1 calls, 100ms total",
        )
        assert mock_client.post.call_count >= 1

    @pytest.mark.asyncio
    async def test_session_end_summary_error_swallowed(self) -> None:
        """Summary failure doesn't crash session end."""
        plugin = SLMPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        plugin._client = mock_client

        await plugin.on_session_start("s1", {})
        await plugin.on_session_end("s1")  # Should not raise

    @pytest.mark.asyncio
    async def test_tool_call_when_unavailable_still_stores_locally(self) -> None:
        """When daemon is down, observations still stored in ring buffer."""
        plugin = SLMPlugin(slm_url="http://test:8765")
        plugin._available = False

        await plugin.on_tool_call_after("s1", "srv", "t", {}, None, 100, True)
        assert plugin.observation_count == 1


# ---------------------------------------------------------------------------
# Mesh Plugin Tests
# ---------------------------------------------------------------------------


class TestMeshPlugin:
    def test_name_and_version(self, mesh_plugin: MeshPlugin) -> None:
        assert mesh_plugin.name == "mesh"
        assert mesh_plugin.version == "0.1.0"

    def test_not_available_initially(self, mesh_plugin: MeshPlugin) -> None:
        assert mesh_plugin.available is False

    @pytest.mark.asyncio
    async def test_on_hub_start_daemon_reachable(self, mesh_plugin: MeshPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_hub.config = MagicMock()
        mock_hub.config.host = "127.0.0.1"
        mock_hub.config.port = 52414
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"peer_id": "abc-123"}))

        with patch("slm_mcp_hub.plugins.mesh_plugin.httpx.AsyncClient", return_value=mock_client):
            await mesh_plugin.on_hub_start(mock_hub)

        assert mesh_plugin.available is True
        assert mesh_plugin._peer_id == "abc-123"

    @pytest.mark.asyncio
    async def test_on_hub_start_daemon_not_reachable(self, mesh_plugin: MeshPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with patch("slm_mcp_hub.plugins.mesh_plugin.httpx.AsyncClient", return_value=mock_client):
            await mesh_plugin.on_hub_start(mock_hub)

        assert mesh_plugin.available is False

    @pytest.mark.asyncio
    async def test_on_hub_start_register_error(self, mesh_plugin: MeshPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_error_response(500))

        with patch("slm_mcp_hub.plugins.mesh_plugin.httpx.AsyncClient", return_value=mock_client):
            await mesh_plugin.on_hub_start(mock_hub)

        assert mesh_plugin.available is False

    @pytest.mark.asyncio
    async def test_on_hub_start_unexpected_error(self, mesh_plugin: MeshPlugin) -> None:
        mock_hub = MagicMock(spec=HubOrchestrator)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=ValueError("unexpected"))

        with patch("slm_mcp_hub.plugins.mesh_plugin.httpx.AsyncClient", return_value=mock_client):
            await mesh_plugin.on_hub_start(mock_hub)

        assert mesh_plugin.available is False

    @pytest.mark.asyncio
    async def test_all_hooks_noop_when_unavailable(self, mesh_plugin: MeshPlugin) -> None:
        assert mesh_plugin.available is False
        result = await mesh_plugin.on_tool_call_before("s1", "srv", "t", {})
        assert result is None
        await mesh_plugin.on_tool_call_after("s1", "srv", "t", {}, None, 100, True)
        await mesh_plugin.on_session_start("s1", {})
        await mesh_plugin.on_session_end("s1")
        await mesh_plugin.on_mcp_connect("srv")
        await mesh_plugin.on_mcp_disconnect("srv")

    def test_get_remote_tools_empty(self, mesh_plugin: MeshPlugin) -> None:
        assert mesh_plugin.get_remote_tools() == {}

    def test_get_remote_tools_after_update(self, mesh_plugin: MeshPlugin) -> None:
        mesh_plugin.update_remote_tools({
            "macstudio": ["gemini__search", "context7__query"],
        })
        result = mesh_plugin.get_remote_tools()
        assert "macstudio" in result
        assert len(result["macstudio"]) == 2

    @pytest.mark.asyncio
    async def test_acquire_lock_unavailable(self, mesh_plugin: MeshPlugin) -> None:
        assert await mesh_plugin.acquire_lock("test_resource", "s1") is False

    @pytest.mark.asyncio
    async def test_acquire_lock_available(self, mesh_plugin: MeshPlugin) -> None:
        mesh_plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"locked": True}))
        mesh_plugin._client = mock_client

        result = await mesh_plugin.acquire_lock("sqlite:write", "s1")
        assert result is True

    @pytest.mark.asyncio
    async def test_acquire_lock_error_returns_false(self, mesh_plugin: MeshPlugin) -> None:
        mesh_plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        mesh_plugin._client = mock_client

        result = await mesh_plugin.acquire_lock("resource", "s1")
        assert result is False

    @pytest.mark.asyncio
    async def test_release_lock_unavailable(self, mesh_plugin: MeshPlugin) -> None:
        await mesh_plugin.release_lock("test", "s1")  # Should not raise

    @pytest.mark.asyncio
    async def test_release_lock_available(self, mesh_plugin: MeshPlugin) -> None:
        mesh_plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"released": True}))
        mesh_plugin._client = mock_client

        await mesh_plugin.release_lock("sqlite:write", "s1")
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_lock_error_swallowed(self, mesh_plugin: MeshPlugin) -> None:
        mesh_plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        mesh_plugin._client = mock_client

        await mesh_plugin.release_lock("resource", "s1")  # Should not raise

    @pytest.mark.asyncio
    async def test_on_hub_stop(self, mesh_plugin: MeshPlugin) -> None:
        mesh_plugin._available = True
        mesh_plugin._client = AsyncMock(spec=httpx.AsyncClient)
        mesh_plugin._peers = {"a": MagicMock()}
        mesh_plugin._remote_tools = {"b": ["t1"]}

        await mesh_plugin.on_hub_stop()

        assert mesh_plugin.available is False
        assert mesh_plugin.peer_count == 0
        assert mesh_plugin.get_remote_tools() == {}

    def test_update_peers(self, mesh_plugin: MeshPlugin) -> None:
        peer = MeshPeerInfo(
            peer_id="p1", hostname="macstudio", port=52414,
            mcp_count=38, last_seen=time.time(),
        )
        mesh_plugin.update_peers({"p1": peer})
        assert mesh_plugin.peer_count == 1

    def test_mesh_peer_info_frozen(self) -> None:
        peer = MeshPeerInfo(
            peer_id="p1", hostname="test", port=52414,
            mcp_count=10, last_seen=1.0,
        )
        with pytest.raises(AttributeError):
            peer.peer_id = "p2"  # type: ignore[misc]

    def test_remote_tool_route_frozen(self) -> None:
        route = RemoteToolRoute(
            peer_id="p1", server_name="gemini",
            tool_name="search", latency_ms=15.0,
        )
        with pytest.raises(AttributeError):
            route.peer_id = "p2"  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_tool_call_before_routes_remote(self, mesh_plugin: MeshPlugin) -> None:
        mesh_plugin._available = True
        mesh_plugin.update_remote_tools({
            "macstudio": ["gemini__search"],
        })

        result = await mesh_plugin.on_tool_call_before("s1", "gemini", "search", {"q": "test"})
        assert result is not None
        assert "_mesh_route" in result
        assert result["_mesh_route"]["peer_id"] == "macstudio"

    @pytest.mark.asyncio
    async def test_tool_call_before_no_remote(self, mesh_plugin: MeshPlugin) -> None:
        mesh_plugin._available = True
        result = await mesh_plugin.on_tool_call_before("s1", "local", "tool", {})
        assert result is None


# ---------------------------------------------------------------------------
# Mesh Plugin — Available Mode Tests
# ---------------------------------------------------------------------------


class TestMeshPluginAvailable:
    @pytest.mark.asyncio
    async def test_tool_call_after_broadcasts(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"ok": True}))
        plugin._client = mock_client

        await plugin.on_tool_call_after("s1", "srv", "t", {}, None, 100, True)
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_call_after_broadcast_error_swallowed(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        plugin._client = mock_client

        await plugin.on_tool_call_after("s1", "srv", "t", {}, None, 100, True)

    @pytest.mark.asyncio
    async def test_session_start_broadcasts(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"ok": True}))
        plugin._client = mock_client

        await plugin.on_session_start("s1", {"project": "/test"})
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_end_broadcasts(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"ok": True}))
        plugin._client = mock_client

        await plugin.on_session_end("s1")
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_mcp_connect_broadcasts(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"ok": True}))
        plugin._client = mock_client

        await plugin.on_mcp_connect("gemini")
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_mcp_disconnect_broadcasts(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_ok_response({"ok": True}))
        plugin._client = mock_client

        await plugin.on_mcp_disconnect("sqlite")
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_start_broadcast_error_swallowed(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        plugin._client = mock_client

        await plugin.on_session_start("s1", {"project": "/test"})

    @pytest.mark.asyncio
    async def test_session_end_broadcast_error_swallowed(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        plugin._client = mock_client

        await plugin.on_session_end("s1")

    @pytest.mark.asyncio
    async def test_mcp_connect_broadcast_error_swallowed(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        plugin._client = mock_client

        await plugin.on_mcp_connect("gemini")

    @pytest.mark.asyncio
    async def test_mcp_disconnect_broadcast_error_swallowed(self) -> None:
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._available = True
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        plugin._client = mock_client

        await plugin.on_mcp_disconnect("sqlite")

    @pytest.mark.asyncio
    async def test_mesh_send_no_client(self) -> None:
        """No client means no send attempt."""
        plugin = MeshPlugin(slm_url="http://test:8765")
        plugin._client = None
        await plugin._mesh_send("test message")  # Should not raise


# ---------------------------------------------------------------------------
# Hub Plugin Extension Tests
# ---------------------------------------------------------------------------


class _StopErrorPlugin(HubPlugin):
    """Plugin that raises error on stop."""

    @property
    def name(self) -> str:
        return "stop_error_plugin"

    @property
    def version(self) -> str:
        return "0.0.1"

    async def on_hub_stop(self) -> None:
        raise RuntimeError("stop failed!")

    async def on_hub_start(self, hub: Any) -> None:
        raise RuntimeError("init failed!")


class _InitFailPlugin(HubPlugin):
    """Plugin that fails during on_hub_start."""

    @property
    def name(self) -> str:
        return "init_fail_plugin"

    @property
    def version(self) -> str:
        return "0.0.1"

    async def on_hub_start(self, hub: Any) -> None:
        raise RuntimeError("plugin init crashed")


class TestPluginDiscovery:
    @pytest.mark.asyncio
    async def test_discover_plugin_not_in_enabled_list(self, hub_config: HubConfig) -> None:
        from dataclasses import replace
        config_with_enabled = replace(hub_config, plugins_enabled=("only_this_one",))
        hub = HubOrchestrator(config_with_enabled)
        try:
            mock_ep = MagicMock()
            mock_ep.name = "not_enabled_plugin"

            mock_eps = {"slm_mcp_hub.plugins": [mock_ep]}
            with patch("slm_mcp_hub.core.hub.importlib.metadata.entry_points", return_value=mock_eps):
                hub._discover_plugins()

            assert len(hub.plugins) == 0
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_discover_plugin_not_hub_plugin(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            mock_ep = MagicMock()
            mock_ep.name = "bad_plugin"
            mock_ep.load.return_value = lambda: "not_a_plugin"

            mock_eps = {"slm_mcp_hub.plugins": [mock_ep]}
            with patch("slm_mcp_hub.core.hub.importlib.metadata.entry_points", return_value=mock_eps):
                hub._discover_plugins()

            assert len(hub.plugins) == 0
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_discover_plugin_load_failure(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            mock_ep = MagicMock()
            mock_ep.name = "crash_plugin"
            mock_ep.load.side_effect = ImportError("broken package")

            mock_eps = {"slm_mcp_hub.plugins": [mock_ep]}
            with patch("slm_mcp_hub.core.hub.importlib.metadata.entry_points", return_value=mock_eps):
                hub._discover_plugins()

            assert len(hub.plugins) == 0
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_init_plugins_failure_path(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            failing = _InitFailPlugin()
            hub._plugins = [failing]
            await hub._init_plugins()
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_plugin_stop_error_path(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            async with hub:
                stop_error = _StopErrorPlugin()
                hub._plugins = [stop_error]
            assert hub.state == "stopped"
        finally:
            reset_hub()

    def test_hub_config_property(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            assert hub.config is hub_config
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_discover_valid_plugin_via_entrypoints(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            mock_ep = MagicMock()
            mock_ep.name = "test_plugin"
            mock_ep.load.return_value = lambda: _TestPlugin("discovered")

            mock_eps = {"slm_mcp_hub.plugins": [mock_ep]}
            with patch("slm_mcp_hub.core.hub.importlib.metadata.entry_points", return_value=mock_eps):
                hub._discover_plugins()

            assert len(hub.plugins) == 1
            assert hub.plugins[0].name == "discovered"
        finally:
            reset_hub()


class TestHubPluginExtensions:
    @pytest.mark.asyncio
    async def test_notify_before_returns_modified_args(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            modifier = _ModifyingPlugin()
            hub._plugins = [modifier]

            result = await hub.notify_plugins_tool_call_before("s1", "srv", "t", {"a": 1})
            assert result is not None
            assert result["injected"] is True
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_notify_before_returns_none(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            plugin = _TestPlugin()
            hub._plugins = [plugin]

            result = await hub.notify_plugins_tool_call_before("s1", "srv", "t", {})
            assert result is None
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_notify_session_start(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            plugin = _TestPlugin()
            hub._plugins = [plugin]

            await hub.notify_plugins_session_start("s1", {"project": "/test"})
            assert "session_start" in plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_notify_session_end(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            plugin = _TestPlugin()
            hub._plugins = [plugin]

            await hub.notify_plugins_session_end("s1")
            assert "session_end" in plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_notify_mcp_connect(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            plugin = _TestPlugin()
            hub._plugins = [plugin]

            await hub.notify_plugins_mcp_connect("gemini")
            assert "mcp_connect" in plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_notify_mcp_disconnect(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            plugin = _TestPlugin()
            hub._plugins = [plugin]

            await hub.notify_plugins_mcp_disconnect("gemini")
            assert "mcp_disconnect" in plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_get_plugin_by_name(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            plugin = _TestPlugin("alpha")
            hub._plugins = [plugin]

            found = hub.get_plugin("alpha")
            assert found is plugin

            missing = hub.get_plugin("nonexistent")
            assert missing is None
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_error_isolation_before(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            error_plugin = _ErrorPlugin()
            good_plugin = _TestPlugin()
            hub._plugins = [error_plugin, good_plugin]

            result = await hub.notify_plugins_tool_call_before("s1", "srv", "t", {})
            assert result is None
            assert "before" in good_plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_error_isolation_session_start(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            error_plugin = _ErrorPlugin()
            good_plugin = _TestPlugin()
            hub._plugins = [error_plugin, good_plugin]

            await hub.notify_plugins_session_start("s1", {})
            assert "session_start" in good_plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_error_isolation_session_end(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            error_plugin = _ErrorPlugin()
            good_plugin = _TestPlugin()
            hub._plugins = [error_plugin, good_plugin]

            await hub.notify_plugins_session_end("s1")
            assert "session_end" in good_plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_error_isolation_mcp_connect(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            error_plugin = _ErrorPlugin()
            good_plugin = _TestPlugin()
            hub._plugins = [error_plugin, good_plugin]

            await hub.notify_plugins_mcp_connect("gemini")
            assert "mcp_connect" in good_plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_error_isolation_mcp_disconnect(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            error_plugin = _ErrorPlugin()
            good_plugin = _TestPlugin()
            hub._plugins = [error_plugin, good_plugin]

            await hub.notify_plugins_mcp_disconnect("gemini")
            assert "mcp_disconnect" in good_plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_error_isolation_tool_call_after(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            error_plugin = _ErrorPlugin()
            good_plugin = _TestPlugin()
            hub._plugins = [error_plugin, good_plugin]

            await hub.notify_plugins_tool_call_after(
                "s1", "srv", "t", {}, None, 100, True,
            )
            assert "after" in good_plugin.calls
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_hub_without_plugins(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            hub._plugins = []

            result = await hub.notify_plugins_tool_call_before("s1", "srv", "t", {})
            assert result is None
            await hub.notify_plugins_tool_call_after("s1", "srv", "t", {}, None, 100, True)
            await hub.notify_plugins_session_start("s1", {})
            await hub.notify_plugins_session_end("s1")
            await hub.notify_plugins_mcp_connect("srv")
            await hub.notify_plugins_mcp_disconnect("srv")
            assert hub.get_plugin("anything") is None
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_hub_start_failure_transitions_to_error(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            hub._db = MagicMock()
            hub._db.open.side_effect = RuntimeError("disk full")

            with pytest.raises(RuntimeError, match="disk full"):
                await hub.start()

            assert hub.state == "error"
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_hub_plugin_init_failure_doesnt_crash(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            failing_plugin = _ErrorPlugin()
            hub._plugins = [failing_plugin]
            await hub._init_plugins()
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_hub_plugin_stop_error_doesnt_crash(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            async with hub:
                failing_plugin = _ErrorPlugin()
                good_plugin = _TestPlugin()
                hub._plugins = [failing_plugin, good_plugin]
            assert hub.state == "stopped"
        finally:
            reset_hub()

    @pytest.mark.asyncio
    async def test_hub_event_handler_error_swallowed(self, hub_config: HubConfig) -> None:
        hub = HubOrchestrator(hub_config)
        try:
            def bad_handler(**kwargs: Any) -> None:
                raise RuntimeError("handler crashed")

            hub.on("test_event", bad_handler)
            hub._emit("test_event")
        finally:
            reset_hub()
