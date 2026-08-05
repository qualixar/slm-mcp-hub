"""MCP 2026-07-28 contract tests for the inbound SDK server adapter.

RED phase: these tests will fail until protocol/inbound.py exists and
build_sdk_server() is implemented.

Tests cover:
- build_sdk_server() returns a properly configured Server
- on_list_tools handler delegates to HubProductOperations
- on_call_tool routes meta-tools and federated tools correctly
- on_list_resources delegates and converts correctly
- on_list_resource_templates delegates and converts correctly
- on_read_resource delegates and converts correctly
- on_list_prompts delegates and converts correctly
- on_get_prompt delegates and converts correctly
- Alias resolution: hub__search_tools -> search_tools
- Client name and session ID extraction from context
- Error propagation: call_tool converts errors to SDK is_error flag
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import mcp.types as t
import pytest

from slm_mcp_hub.protocol.models import (
    CallToolOutcome,
    PromptGetOutcome,
    PromptsListOutcome,
    ResourceReadOutcome,
    ResourcesListOutcome,
    ResourceTemplatesListOutcome,
    ToolsListOutcome,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_ops() -> Any:
    """Create a HubProductOperations mock returning minimal neutral outcomes."""
    ops = MagicMock()
    ops.list_tools = AsyncMock(
        return_value=ToolsListOutcome(
            tools=(
                {
                    "name": "search_tools",
                    "description": "Search tools",
                    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                },
                {
                    "name": "call_tool",
                    "description": "Call a tool",
                    "inputSchema": {"type": "object", "properties": {"tool": {"type": "string"}}},
                },
                {
                    "name": "list_servers",
                    "description": "List servers",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            )
        )
    )
    ops.handle_meta_tool = AsyncMock(
        return_value=CallToolOutcome(
            content=({"type": "text", "text": '{"found": 0, "tools": []}'},),
            is_error=False,
            server_name="hub",
        )
    )
    ops.route_tool = AsyncMock(
        return_value=CallToolOutcome(
            content=({"type": "text", "text": "federated-result"},),
            is_error=False,
            server_name="myserver",
        )
    )
    ops.list_resources = AsyncMock(
        return_value=ResourcesListOutcome(
            resources=(
                {"uri": "file:///test", "name": "test-resource", "mimeType": "text/plain"},
            )
        )
    )
    ops.list_resource_templates = AsyncMock(
        return_value=ResourceTemplatesListOutcome(
            resource_templates=(
                {"uriTemplate": "file:///{path}", "name": "file-template"},
            )
        )
    )
    ops.read_resource = AsyncMock(
        return_value=ResourceReadOutcome(
            raw={"contents": [{"uri": "file:///test", "text": "hello", "mimeType": "text/plain"}]}
        )
    )
    ops.list_prompts = AsyncMock(
        return_value=PromptsListOutcome(
            prompts=({"name": "test-prompt", "description": "A test prompt"},)
        )
    )
    ops.get_prompt = AsyncMock(
        return_value=PromptGetOutcome(
            raw={
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": "Hello"}}
                ],
                "description": "Test",
            }
        )
    )
    return ops


def _make_ctx(client_name: str = "test-client") -> Any:
    """Create a minimal ServerRequestContext mock."""
    ctx = MagicMock()
    client_params = MagicMock()
    client_params.client_info = MagicMock()
    client_params.client_info.name = client_name
    ctx.session.client_params = client_params
    ctx.session._connection.session_id = "ctx-session-abc"
    ctx.session.protocol_version = "2026-07-28"
    return ctx


def _make_ctx_no_client() -> Any:
    """Create a context with no client info (modern stateless)."""
    ctx = MagicMock()
    ctx.session.client_params = None
    ctx.session._connection.session_id = None
    return ctx


# ---------------------------------------------------------------------------
# TestBuildSdkServer — construction
# ---------------------------------------------------------------------------

class TestBuildSdkServer:
    def test_returns_server_instance(self) -> None:
        from mcp.server.lowlevel import Server

        from slm_mcp_hub.protocol.inbound import build_sdk_server  # RED

        server = build_sdk_server(_make_ops())
        assert isinstance(server, Server)

    def test_server_name_is_hub(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        server = build_sdk_server(_make_ops())
        assert server.name == "slm-mcp-hub"

    def test_tools_list_handler_registered(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        server = build_sdk_server(_make_ops())
        assert server.get_request_handler("tools/list") is not None

    def test_tools_call_handler_registered(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        server = build_sdk_server(_make_ops())
        assert server.get_request_handler("tools/call") is not None

    def test_resources_list_handler_registered(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        server = build_sdk_server(_make_ops())
        assert server.get_request_handler("resources/list") is not None

    def test_resources_read_handler_registered(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        server = build_sdk_server(_make_ops())
        assert server.get_request_handler("resources/read") is not None

    def test_prompts_list_handler_registered(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        server = build_sdk_server(_make_ops())
        assert server.get_request_handler("prompts/list") is not None

    def test_prompts_get_handler_registered(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        server = build_sdk_server(_make_ops())
        assert server.get_request_handler("prompts/get") is not None


# ---------------------------------------------------------------------------
# TestOnListTools — list_tools handler
# ---------------------------------------------------------------------------

class TestOnListTools:
    @pytest.mark.asyncio
    async def test_returns_three_hub_meta_tools(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert isinstance(result, t.ListToolsResult)
        names = [tool.name for tool in result.tools]
        assert "search_tools" in names
        assert "call_tool" in names
        assert "list_servers" in names

    @pytest.mark.asyncio
    async def test_delegates_to_ops_list_tools(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/list")
        await handler_entry.handler(_make_ctx(), None)

        ops.list_tools.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tool_input_schema_preserved(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/list")
        result = await handler_entry.handler(_make_ctx(), None)

        search_tool = next(t for t in result.tools if t.name == "search_tools")
        assert "query" in search_tool.input_schema.get("properties", {})


# ---------------------------------------------------------------------------
# TestOnCallTool — call_tool handler
# ---------------------------------------------------------------------------

class TestOnCallTool:
    @pytest.mark.asyncio
    async def test_meta_tool_dispatched_to_handle_meta_tool(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        params = t.CallToolRequestParams(name="search_tools", arguments={"query": "github"})
        result = await handler_entry.handler(_make_ctx(), params)

        assert isinstance(result, t.CallToolResult)
        ops.handle_meta_tool.assert_awaited_once()
        call_kwargs = ops.handle_meta_tool.call_args
        assert call_kwargs.kwargs["name"] == "search_tools"
        assert call_kwargs.kwargs["arguments"] == {"query": "github"}

    @pytest.mark.asyncio
    async def test_alias_hub__search_tools_resolved(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        params = t.CallToolRequestParams(name="hub__search_tools", arguments={"query": "x"})
        await handler_entry.handler(_make_ctx(), params)

        ops.handle_meta_tool.assert_awaited_once()
        call_kwargs = ops.handle_meta_tool.call_args
        assert call_kwargs.kwargs["name"] == "search_tools"

    @pytest.mark.asyncio
    async def test_federated_tool_dispatched_to_route_tool(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        params = t.CallToolRequestParams(name="myserver__my_tool", arguments={"key": "val"})
        result = await handler_entry.handler(_make_ctx(), params)

        assert isinstance(result, t.CallToolResult)
        ops.route_tool.assert_awaited_once_with("myserver__my_tool", {"key": "val"}, ANY)

    @pytest.mark.asyncio
    async def test_error_outcome_sets_is_error_true(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        ops.handle_meta_tool.return_value = CallToolOutcome(
            content=({"type": "text", "text": "Error: something went wrong"},),
            is_error=True,
            server_name="hub",
        )
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        params = t.CallToolRequestParams(name="search_tools", arguments={})
        result = await handler_entry.handler(_make_ctx(), params)

        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_no_arguments_defaults_to_empty_dict(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        params = t.CallToolRequestParams(name="list_servers")
        await handler_entry.handler(_make_ctx(), params)

        call_kwargs = ops.handle_meta_tool.call_args
        assert call_kwargs.kwargs["arguments"] == {}

    @pytest.mark.asyncio
    async def test_client_name_extracted_from_context(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        ctx = _make_ctx(client_name="my-special-client")
        params = t.CallToolRequestParams(name="search_tools", arguments={})
        await handler_entry.handler(ctx, params)

        call_kwargs = ops.handle_meta_tool.call_args
        assert call_kwargs.kwargs["client_name"] == "my-special-client"

    @pytest.mark.asyncio
    async def test_missing_client_defaults_to_sdk_client(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        ctx = _make_ctx_no_client()
        params = t.CallToolRequestParams(name="search_tools", arguments={})
        await handler_entry.handler(ctx, params)

        call_kwargs = ops.handle_meta_tool.call_args
        assert call_kwargs.kwargs["client_name"] == "sdk-client"


# ---------------------------------------------------------------------------
# TestOnListResources — list_resources handler
# ---------------------------------------------------------------------------

class TestOnListResources:
    @pytest.mark.asyncio
    async def test_returns_sdk_list_resources_result(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert isinstance(result, t.ListResourcesResult)
        assert len(result.resources) == 1
        assert result.resources[0].uri == "file:///test"

    @pytest.mark.asyncio
    async def test_delegates_to_ops_list_resources(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/list")
        await handler_entry.handler(_make_ctx(), None)

        ops.list_resources.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_resources_returns_empty_list(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        ops.list_resources.return_value = ResourcesListOutcome(resources=())
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert result.resources == []


# ---------------------------------------------------------------------------
# TestOnReadResource — read_resource handler
# ---------------------------------------------------------------------------

class TestOnReadResource:
    @pytest.mark.asyncio
    async def test_returns_sdk_read_resource_result(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/read")

        params = t.ReadResourceRequestParams(uri="file:///test")
        result = await handler_entry.handler(_make_ctx(), params)

        assert isinstance(result, t.ReadResourceResult)

    @pytest.mark.asyncio
    async def test_uri_passed_to_ops(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/read")

        params = t.ReadResourceRequestParams(uri="file:///specific/path")
        await handler_entry.handler(_make_ctx(), params)

        ops.read_resource.assert_awaited_once()
        assert "specific/path" in str(ops.read_resource.call_args[0][0])


# ---------------------------------------------------------------------------
# TestOnListResourceTemplates
# ---------------------------------------------------------------------------

class TestOnListResourceTemplates:
    @pytest.mark.asyncio
    async def test_returns_sdk_list_resource_templates_result(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/templates/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert isinstance(result, t.ListResourceTemplatesResult)
        assert len(result.resource_templates) == 1

    @pytest.mark.asyncio
    async def test_uri_template_field_preserved(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/templates/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert result.resource_templates[0].uri_template == "file:///{path}"


# ---------------------------------------------------------------------------
# TestOnListPrompts
# ---------------------------------------------------------------------------

class TestOnListPrompts:
    @pytest.mark.asyncio
    async def test_returns_sdk_list_prompts_result(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("prompts/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert isinstance(result, t.ListPromptsResult)
        assert len(result.prompts) == 1
        assert result.prompts[0].name == "test-prompt"


# ---------------------------------------------------------------------------
# TestOnGetPrompt
# ---------------------------------------------------------------------------

class TestOnGetPrompt:
    @pytest.mark.asyncio
    async def test_returns_sdk_get_prompt_result(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("prompts/get")

        params = t.GetPromptRequestParams(name="test-prompt")
        result = await handler_entry.handler(_make_ctx(), params)

        assert isinstance(result, t.GetPromptResult)
        assert len(result.messages) == 1

    @pytest.mark.asyncio
    async def test_prompt_name_and_args_passed_to_ops(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("prompts/get")

        params = t.GetPromptRequestParams(name="my-prompt", arguments={"lang": "en"})
        await handler_entry.handler(_make_ctx(), params)

        ops.get_prompt.assert_awaited_once_with("my-prompt", {"lang": "en"})


# ---------------------------------------------------------------------------
# TestSessionIdExtraction
# ---------------------------------------------------------------------------

class TestSessionIdExtraction:
    @pytest.mark.asyncio
    async def test_connection_session_id_used_when_available(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        ctx = _make_ctx()
        ctx.session._connection.session_id = "my-specific-session"
        params = t.CallToolRequestParams(name="search_tools", arguments={})
        await handler_entry.handler(ctx, params)

        call_kwargs = ops.handle_meta_tool.call_args
        assert call_kwargs.kwargs["session_id"] == "my-specific-session"

    @pytest.mark.asyncio
    async def test_fallback_uuid_when_no_session_id(self) -> None:
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/call")

        ctx = _make_ctx_no_client()
        params = t.CallToolRequestParams(name="search_tools", arguments={})
        await handler_entry.handler(ctx, params)

        call_kwargs = ops.handle_meta_tool.call_args
        session_id = call_kwargs.kwargs["session_id"]
        assert isinstance(session_id, str)
        assert len(session_id) > 0

    @pytest.mark.asyncio
    async def test_attribute_error_on_connection_falls_back_to_uuid(self) -> None:
        """When _connection.session_id raises AttributeError, a UUID is used."""
        from slm_mcp_hub.protocol.inbound import _extract_session_id

        # Simulate a ctx where ._connection has NO session_id attribute
        ctx = MagicMock()
        del ctx.session._connection.session_id  # ensure AttributeError
        ctx.session._connection = MagicMock(spec=[])  # spec=[] means no attrs

        result = _extract_session_id(ctx)
        assert result.startswith("sdk-"), f"Expected sdk-prefix fallback, got {result!r}"
        assert len(result) > 4

    @pytest.mark.asyncio
    async def test_client_name_extraction_handles_exception(self) -> None:
        """When client_params access raises an exception, 'sdk-client' is returned."""
        from slm_mcp_hub.protocol.inbound import _extract_client_name

        # Simulate a ctx where .client_params property raises
        ctx = MagicMock()
        type(ctx.session).client_params = property(lambda self: (_ for _ in ()).throw(RuntimeError("broken")))

        result = _extract_client_name(ctx)
        assert result == "sdk-client"


# ---------------------------------------------------------------------------
# TestStdioAndHttpEquivalence — same inventory via both transports
# ---------------------------------------------------------------------------

class TestInventoryEquivalence:
    """stdio and HTTP must return the same normalized inventory."""

    @pytest.mark.asyncio
    async def test_list_tools_same_result_regardless_of_context(self) -> None:
        """Calling the handler with different contexts returns identical results."""
        from slm_mcp_hub.protocol.inbound import build_sdk_server

        ops = _make_ops()
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("tools/list")

        result_with_client = await handler_entry.handler(_make_ctx("client-A"), None)
        result_no_client = await handler_entry.handler(_make_ctx_no_client(), None)

        names_a = {t.name for t in result_with_client.tools}
        names_b = {t.name for t in result_no_client.tools}
        assert names_a == names_b


# ---------------------------------------------------------------------------
# TestPromptFidelity — regression: prompt arguments + title survive
# ---------------------------------------------------------------------------

class TestPromptFidelity:
    """Regression tests for the controller's inbound.py fidelity fix.

    Before the fix: _prompts_list_to_sdk dropped prompt arguments entirely
    (passed None) and did not map title.  A federated prompt with required
    arguments was silently stripped, making it unusable to downstream clients.
    """

    @pytest.mark.asyncio
    async def test_prompt_arguments_survive_list_prompts(self) -> None:
        """Prompt arguments from the federated registry MUST appear in the SDK result."""
        from slm_mcp_hub.protocol.inbound import build_sdk_server
        from slm_mcp_hub.protocol.models import PromptsListOutcome

        prompt_dict = {
            "name": "code-review",
            "title": "Code Review Prompt",
            "description": "Review code for quality",
            "arguments": [
                {"name": "language", "description": "Programming language", "required": True},
                {"name": "style_guide", "description": "Style guide to apply", "required": False},
            ],
        }

        ops = _make_ops()
        ops.list_prompts = AsyncMock(
            return_value=PromptsListOutcome(prompts=(prompt_dict,))
        )
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("prompts/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert len(result.prompts) == 1
        sdk_prompt = result.prompts[0]
        assert sdk_prompt.name == "code-review"
        assert sdk_prompt.title == "Code Review Prompt"
        assert sdk_prompt.description == "Review code for quality"

        # Arguments MUST be present and correctly typed
        assert sdk_prompt.arguments is not None, "Arguments must not be None"
        assert len(sdk_prompt.arguments) == 2

        arg_map = {a.name: a for a in sdk_prompt.arguments}
        assert "language" in arg_map
        assert arg_map["language"].description == "Programming language"
        assert arg_map["language"].required is True

        assert "style_guide" in arg_map
        assert arg_map["style_guide"].required is False

    @pytest.mark.asyncio
    async def test_prompt_with_no_arguments_returns_none(self) -> None:
        """A prompt without arguments yields None (not an empty list)."""
        from slm_mcp_hub.protocol.inbound import build_sdk_server
        from slm_mcp_hub.protocol.models import PromptsListOutcome

        prompt_dict = {"name": "simple-prompt", "description": "No args"}
        ops = _make_ops()
        ops.list_prompts = AsyncMock(
            return_value=PromptsListOutcome(prompts=(prompt_dict,))
        )
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("prompts/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert len(result.prompts) == 1
        assert result.prompts[0].arguments is None

    @pytest.mark.asyncio
    async def test_resource_title_survives_list_resources(self) -> None:
        """Resource title from the federated registry MUST appear in the SDK result."""
        from slm_mcp_hub.protocol.inbound import build_sdk_server
        from slm_mcp_hub.protocol.models import ResourcesListOutcome

        resource_dict = {
            "name": "config-file",
            "title": "Hub Configuration",
            "uri": "file:///config.json",
            "description": "The hub configuration file",
            "mimeType": "application/json",
        }

        ops = _make_ops()
        ops.list_resources = AsyncMock(
            return_value=ResourcesListOutcome(resources=(resource_dict,))
        )
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert len(result.resources) == 1
        sdk_resource = result.resources[0]
        assert sdk_resource.name == "config-file"
        assert sdk_resource.title == "Hub Configuration"
        assert sdk_resource.uri == "file:///config.json"

    @pytest.mark.asyncio
    async def test_resource_template_title_survives(self) -> None:
        """Resource template title MUST appear in the SDK result."""
        from slm_mcp_hub.protocol.inbound import build_sdk_server
        from slm_mcp_hub.protocol.models import ResourceTemplatesListOutcome

        template_dict = {
            "name": "server-log",
            "title": "Server Log Template",
            "uriTemplate": "logs://{server_name}/latest",
            "description": "Fetch the latest log for a server",
        }

        ops = _make_ops()
        ops.list_resource_templates = AsyncMock(
            return_value=ResourceTemplatesListOutcome(resource_templates=(template_dict,))
        )
        server = build_sdk_server(ops)
        handler_entry = server.get_request_handler("resources/templates/list")
        result = await handler_entry.handler(_make_ctx(), None)

        assert len(result.resource_templates) == 1
        sdk_tmpl = result.resource_templates[0]
        assert sdk_tmpl.name == "server-log"
        assert sdk_tmpl.title == "Server Log Template"


# ---------------------------------------------------------------------------
# Sentinel for ANY positional argument
# ---------------------------------------------------------------------------

class _ANY:
    def __eq__(self, other: object) -> bool:
        return True

    def __repr__(self) -> str:
        return "ANY"


ANY = _ANY()
