"""Phase 1 Federation Tests — 100% coverage target."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry, RegisteredCapability
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.namespace import (
    make_unique_id,
    namespace_name,
    namespace_prompt,
    namespace_resource,
    namespace_resource_template,
    namespace_tool,
    parse_namespaced,
    safe_server_id,
)
from slm_mcp_hub.federation.router import FederationRouter, RouteResult


# ===========================================================================
# Namespace Engine Tests
# ===========================================================================

class TestSafeServerId:
    def test_simple_name(self):
        assert safe_server_id("github") == "github"

    def test_hyphens_replaced(self):
        assert safe_server_id("my-server") == "my_server"

    def test_dots_replaced(self):
        assert safe_server_id("server.v2") == "server_v2"

    def test_special_chars(self):
        assert safe_server_id("@org/pkg") == "_org_pkg"

    def test_empty_string(self):
        assert safe_server_id("") == ""

    def test_already_safe(self):
        assert safe_server_id("context7") == "context7"


class TestMakeUniqueId:
    def test_no_collision(self):
        assert make_unique_id("github", set()) == "github"

    def test_collision_appends_2(self):
        assert make_unique_id("github", {"github"}) == "github_2"

    def test_multiple_collisions(self):
        assert make_unique_id("github", {"github", "github_2"}) == "github_3"

    def test_many_collisions(self):
        existing = {"a", "a_2", "a_3", "a_4"}
        assert make_unique_id("a", existing) == "a_5"


class TestNamespaceName:
    def test_basic(self):
        assert namespace_name("github", "create_issue") == "github__create_issue"

    def test_with_delimiter_in_original(self):
        assert namespace_name("remote", "github__search") == "remote__github__search"


class TestParseNamespaced:
    def test_basic(self):
        assert parse_namespaced("github__create_issue") == ("github", "create_issue")

    def test_nested_delimiter(self):
        assert parse_namespaced("remote__github__search") == ("remote", "github__search")

    def test_no_delimiter_raises(self):
        with pytest.raises(ValueError, match="no '__' delimiter"):
            parse_namespaced("nope")


class TestNamespaceHelpers:
    def test_namespace_tool(self):
        tool = {"name": "search", "description": "Search"}
        result = namespace_tool("github", tool)
        assert result["name"] == "github__search"
        assert result["description"] == "Search"
        assert tool["name"] == "search"  # Original not mutated

    def test_namespace_resource(self):
        res = {"uri": "file:///tmp", "name": "tmp"}
        result = namespace_resource("fs", res)
        assert result["uri"] == "fs__file:///tmp"
        assert result["name"] == "tmp"
        assert res["uri"] == "file:///tmp"  # Original not mutated

    def test_namespace_resource_template(self):
        tmpl = {"uriTemplate": "file:///{path}", "name": "files"}
        result = namespace_resource_template("fs", tmpl)
        assert result["uriTemplate"] == "fs__file:///{path}"
        assert tmpl["uriTemplate"] == "file:///{path}"  # Original not mutated

    def test_namespace_prompt(self):
        prompt = {"name": "explain", "description": "Explain code"}
        result = namespace_prompt("helper", prompt)
        assert result["name"] == "helper__explain"
        assert prompt["name"] == "explain"  # Original not mutated


# ===========================================================================
# Capability Registry Tests
# ===========================================================================

class TestCapabilityRegistry:
    def _sample_servers(self):
        return {
            "github": {
                "tools": [
                    {"name": "create_issue", "description": "Create issue"},
                    {"name": "search_code", "description": "Search code"},
                ],
                "resources": [{"uri": "repo://main", "name": "Main repo"}],
                "resource_templates": [],
                "prompts": [{"name": "pr_review", "description": "Review a PR"}],
            },
            "context7": {
                "tools": [{"name": "query-docs", "description": "Query docs"}],
                "resources": [],
                "resource_templates": [{"uriTemplate": "doc:///{lib}", "name": "Lib docs"}],
                "prompts": [],
            },
        }

    def test_sync_populates(self):
        reg = CapabilityRegistry()
        changed = reg.sync(self._sample_servers())
        assert changed is True
        assert reg.tool_count == 3
        assert reg.resource_count == 1
        assert reg.prompt_count == 1

    def test_sync_no_change(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        changed = reg.sync(self._sample_servers())
        assert changed is False

    def test_list_tools(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        tools = reg.list_tools()
        names = {t["name"] for t in tools}
        assert "github__create_issue" in names
        assert "context7__query_docs" in names or "context7__query-docs" in names

    def test_list_resources(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        resources = reg.list_resources()
        assert len(resources) == 1
        assert "github__repo://main" == resources[0]["uri"]

    def test_list_resource_templates(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        templates = reg.list_resource_templates()
        assert len(templates) == 1

    def test_list_prompts(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        prompts = reg.list_prompts()
        assert len(prompts) == 1
        assert prompts[0]["name"] == "github__pr_review"

    def test_lookup_tool_found(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        cap = reg.lookup_tool("github__create_issue")
        assert cap is not None
        assert cap.server_name == "github"
        assert cap.original_name == "create_issue"

    def test_lookup_tool_not_found(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        assert reg.lookup_tool("nonexistent__tool") is None

    def test_lookup_resource(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        cap = reg.lookup_resource("github__repo://main")
        assert cap is not None
        assert cap.server_name == "github"

    def test_lookup_resource_not_found(self):
        reg = CapabilityRegistry()
        assert reg.lookup_resource("nope") is None

    def test_lookup_prompt(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        cap = reg.lookup_prompt("github__pr_review")
        assert cap is not None

    def test_lookup_prompt_not_found(self):
        reg = CapabilityRegistry()
        assert reg.lookup_prompt("nope") is None

    def test_get_server_id(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        assert reg.get_server_id("github") == "github"
        assert reg.get_server_id("context7") == "context7"
        assert reg.get_server_id("nonexistent") is None

    def test_server_name_collision(self):
        servers = {
            "my-server": {"tools": [{"name": "a", "description": ""}]},
            "my.server": {"tools": [{"name": "b", "description": ""}]},
        }
        reg = CapabilityRegistry()
        reg.sync(servers)
        # Both become "my_server" — one gets _2
        assert reg.tool_count == 2

    def test_clear(self):
        reg = CapabilityRegistry()
        reg.sync(self._sample_servers())
        assert reg.tool_count > 0
        reg.clear()
        assert reg.tool_count == 0
        assert reg.resource_count == 0
        assert reg.prompt_count == 0

    def test_empty_sync(self):
        reg = CapabilityRegistry()
        changed = reg.sync({})
        assert changed is False
        assert reg.tool_count == 0


# ===========================================================================
# MCPConnection Tests (unit, mocked process)
# ===========================================================================

class TestMCPConnection:
    def _make_config(self, **overrides):
        defaults = dict(
            name="test-mcp",
            transport="stdio",
            command="echo",
            args=("hello",),
        )
        defaults.update(overrides)
        return MCPServerConfig(**defaults)

    def test_initial_state(self):
        conn = MCPConnection(self._make_config())
        assert conn.name == "test-mcp"
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.is_connected is False
        assert conn.uptime_seconds == 0.0
        assert conn.capabilities["tools"] == []

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self):
        conn = MCPConnection(self._make_config())
        await conn.disconnect()  # Should not raise
        assert conn.state == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_connect_http_connection_error(self):
        """HTTP transport raises ConnectionError on unreachable server."""
        conn = MCPConnection(self._make_config(transport="http", url="http://127.0.0.1:1/mcp"))
        with pytest.raises(ConnectionError, match="initialization failed"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_command_not_found(self):
        conn = MCPConnection(self._make_config(command="/nonexistent/binary"))
        with pytest.raises(ConnectionError, match="Command not found"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_call_tool_not_connected(self):
        conn = MCPConnection(self._make_config())
        with pytest.raises(ConnectionError, match="not connected"):
            await conn.call_tool("test", {})

    @pytest.mark.asyncio
    async def test_send_notification_not_connected(self):
        conn = MCPConnection(self._make_config())
        with pytest.raises(ConnectionError, match="not connected"):
            await conn._send_notification("test", {})

    @pytest.mark.asyncio
    async def test_disconnect_fails_pending(self):
        conn = MCPConnection(self._make_config())
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        conn._pending[1] = future
        await conn.disconnect()
        assert future.done()
        with pytest.raises(ConnectionError):
            future.result()


# ===========================================================================
# Federation Router Tests
# ===========================================================================

class TestFederationRouter:
    def _setup_router(self):
        reg = CapabilityRegistry()
        reg.sync({
            "github": {
                "tools": [{"name": "search", "description": "Search"}],
                "resources": [{"uri": "repo://main", "name": "Main"}],
                "prompts": [{"name": "review", "description": "Review"}],
                "resource_templates": [],
            },
        })

        mock_conn = AsyncMock(spec=MCPConnection)
        mock_conn.is_connected = True
        mock_conn.call_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        mock_conn.read_resource = AsyncMock(return_value={"contents": [{"text": "data"}]})
        mock_conn.get_prompt = AsyncMock(return_value={"messages": [{"role": "user", "content": "hi"}]})

        connections = {"github": mock_conn}
        router = FederationRouter(reg, connections)
        return router, mock_conn

    @pytest.mark.asyncio
    async def test_route_tool_call_success(self):
        router, mock = self._setup_router()
        result = await router.route_tool_call("github__search", {"q": "test"})
        assert result.success is True
        assert result.server_name == "github"
        assert result.tool_name == "search"
        assert result.duration_ms >= 0
        mock.call_tool.assert_called_once_with("search", {"q": "test"})

    @pytest.mark.asyncio
    async def test_route_tool_not_found(self):
        router, _ = self._setup_router()
        result = await router.route_tool_call("fake__tool", {})
        assert result.success is False
        assert "not found" in result.result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_route_tool_server_disconnected(self):
        router, mock = self._setup_router()
        mock.is_connected = False
        result = await router.route_tool_call("github__search", {})
        assert result.success is False
        assert "not connected" in result.result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_route_tool_call_exception(self):
        router, mock = self._setup_router()
        mock.call_tool.side_effect = RuntimeError("MCP crashed")
        result = await router.route_tool_call("github__search", {})
        assert result.success is False
        assert "MCP crashed" in result.result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_route_tool_call_is_error_flag(self):
        router, mock = self._setup_router()
        mock.call_tool.return_value = {"content": [{"type": "text", "text": "err"}], "isError": True}
        result = await router.route_tool_call("github__search", {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_route_resource_read_success(self):
        router, mock = self._setup_router()
        result = await router.route_resource_read("github__repo://main")
        assert result.success is True
        mock.read_resource.assert_called_once_with("repo://main")

    @pytest.mark.asyncio
    async def test_route_resource_not_found(self):
        router, _ = self._setup_router()
        result = await router.route_resource_read("fake__uri")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_route_resource_server_disconnected(self):
        router, mock = self._setup_router()
        mock.is_connected = False
        result = await router.route_resource_read("github__repo://main")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_route_resource_exception(self):
        router, mock = self._setup_router()
        mock.read_resource.side_effect = RuntimeError("fail")
        result = await router.route_resource_read("github__repo://main")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_route_prompt_success(self):
        router, mock = self._setup_router()
        result = await router.route_prompt_get("github__review", {"pr": "123"})
        assert result.success is True
        mock.get_prompt.assert_called_once_with("review", {"pr": "123"})

    @pytest.mark.asyncio
    async def test_route_prompt_not_found(self):
        router, _ = self._setup_router()
        result = await router.route_prompt_get("fake__prompt", {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_route_prompt_server_disconnected(self):
        router, mock = self._setup_router()
        mock.is_connected = False
        result = await router.route_prompt_get("github__review", {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_route_prompt_exception(self):
        router, mock = self._setup_router()
        mock.get_prompt.side_effect = RuntimeError("fail")
        result = await router.route_prompt_get("github__review", {})
        assert result.success is False


class TestRouteResult:
    def test_immutable_fields(self):
        r = RouteResult(
            result={"ok": True},
            server_name="github",
            tool_name="search",
            duration_ms=42,
            success=True,
            cached=False,
        )
        assert r.result == {"ok": True}
        assert r.server_name == "github"
        assert r.duration_ms == 42
        assert r.cached is False
