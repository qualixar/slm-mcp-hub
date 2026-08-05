"""P02 — Transport-neutral product operations.

Tests for protocol/models.py, protocol/product_operations.py, and
protocol/conversion.py.  Written FIRST (RED) before implementation.

Gate requirements (03-EXECUTION-PACKETS.md §P02):
- All legacy tests remain green (wire behavior unchanged in mcp_endpoint.py).
- Conversion tests cover every supported SDK content/result variant.
- Unknown/unsafe variants are rejected explicitly (no silent pass-through).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import mcp.types as t
import pytest

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.protocol.conversion import (
    call_tool_outcome_to_sdk,
    call_tool_outcome_to_wire,
    dict_to_sdk_content_block,
    discover_to_wire,
    initialize_to_wire,
    prompt_get_to_sdk,
    prompt_get_to_wire,
    prompts_list_to_wire,
    resource_read_to_sdk,
    resource_read_to_wire,
    resource_templates_list_to_wire,
    resources_list_to_wire,
    sdk_call_tool_result_to_outcome,
    sdk_content_block_to_dict,
    tools_list_to_sdk,
    tools_list_to_wire,
)

# --- Imports that WILL FAIL until implementation (RED phase) ---------------
from slm_mcp_hub.protocol.models import (
    AuthorizationState,
    CachePolicy,
    CallToolOutcome,
    DiscoverOutcome,
    InitializeOutcome,
    NegotiatedPeer,
    PromptGetOutcome,
    PromptsListOutcome,
    ProtocolEra,
    ResourceReadOutcome,
    ResourcesListOutcome,
    ResourceTemplatesListOutcome,
    ToolsListOutcome,
)
from slm_mcp_hub.protocol.product_operations import (
    HubProductOperations,
)

# ---------------------------------------------------------------------------

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _make_ops(
    *,
    tools: list | None = None,
    resources: list | None = None,
    templates: list | None = None,
    prompts: list | None = None,
) -> tuple[HubProductOperations, AsyncMock]:
    """Build HubProductOperations backed by mocked router."""
    registry = CapabilityRegistry()
    registry.sync({
        "github": {
            "tools": tools if tools is not None else [
                {"name": "search", "description": "Search GitHub", "inputSchema": {}},
            ],
            "resources": resources or [],
            "resource_templates": templates or [],
            "prompts": prompts or [],
        },
    })
    router = AsyncMock(spec=FederationRouter)
    router.route_tool_call = AsyncMock(return_value=RouteResult(
        result={"content": [{"type": "text", "text": "ok"}]},
        server_name="github", tool_name="search", duration_ms=1, success=True,
    ))
    router.route_resource_read = AsyncMock(return_value=RouteResult(
        result={"contents": [{"uri": "ns:r", "text": "hello", "mimeType": "text/plain"}]},
        server_name="github", tool_name="ns:r", duration_ms=1, success=True,
    ))
    router.route_prompt_get = AsyncMock(return_value=RouteResult(
        result={"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]},
        server_name="github", tool_name="p", duration_ms=1, success=True,
    ))
    ops = HubProductOperations(registry=registry, router=router)
    return ops, router


# ===========================================================================
# Protocol model tests
# ===========================================================================

class TestProtocolModels:
    def test_protocol_era_modern_value(self) -> None:
        assert ProtocolEra.MODERN_2026 == "2026-07-28"

    def test_protocol_era_legacy_value(self) -> None:
        assert ProtocolEra.LEGACY == "legacy"

    def test_negotiated_peer_immutable(self) -> None:
        peer = NegotiatedPeer(
            era=ProtocolEra.MODERN_2026,
            protocol_version="2026-07-28",
            capabilities={"tools": {}},
        )
        with pytest.raises((AttributeError, TypeError)):
            peer.era = ProtocolEra.LEGACY  # type: ignore[misc]

    def test_cache_policy_fields(self) -> None:
        policy = CachePolicy(ttl_ms=5000, cache_scope="private")
        assert policy.ttl_ms == 5000
        assert policy.cache_scope == "private"

    def test_cache_policy_immutable(self) -> None:
        policy = CachePolicy(ttl_ms=1, cache_scope="public")
        with pytest.raises((AttributeError, TypeError)):
            policy.ttl_ms = 0  # type: ignore[misc]

    def test_authorization_state_no_token_fields(self) -> None:
        state = AuthorizationState(
            mode="none",
            status="not_required",
            issuer=None,
            resource=None,
            scopes=(),
        )
        fields = vars(state).keys()
        for name in fields:
            assert "token" not in name.lower()
            assert "secret" not in name.lower()
            assert "credential" not in name.lower()

    def test_call_tool_outcome_immutable(self) -> None:
        outcome = CallToolOutcome(
            content=({"type": "text", "text": "hi"},),
            is_error=False,
            server_name="srv",
        )
        with pytest.raises((AttributeError, TypeError)):
            outcome.is_error = True  # type: ignore[misc]

    def test_tools_list_outcome_holds_tuple(self) -> None:
        outcome = ToolsListOutcome(
            tools=({"name": "t", "description": "d", "inputSchema": {}},)
        )
        assert isinstance(outcome.tools, tuple)


# ===========================================================================
# HubProductOperations — list_tools
# ===========================================================================

class TestListTools:
    async def test_returns_exactly_three_meta_tools(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.list_tools()
        assert isinstance(outcome, ToolsListOutcome)
        assert len(outcome.tools) == 3

    async def test_meta_tool_names(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.list_tools()
        names = {tool["name"] for tool in outcome.tools}
        assert names == {"search_tools", "call_tool", "list_servers"}

    async def test_each_meta_tool_has_object_schema(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.list_tools()
        for tool in outcome.tools:
            assert tool.get("inputSchema", {}).get("type") == "object"

    async def test_search_tools_description_references_registry_count(self) -> None:
        ops, _ = _make_ops()  # 1 tool (github__search)
        outcome = await ops.list_tools()
        search = next(t for t in outcome.tools if t["name"] == "search_tools")
        assert "1" in search["description"]


# ===========================================================================
# HubProductOperations — discover + negotiate
# ===========================================================================

class TestDiscover:
    async def test_returns_discover_outcome(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.discover()
        assert isinstance(outcome, DiscoverOutcome)

    async def test_modern_version_present(self) -> None:
        ops, _ = _make_ops()
        assert MODERN_VERSION in (await ops.discover()).supported_versions

    async def test_all_legacy_versions_present(self) -> None:
        ops, _ = _make_ops()
        sv = (await ops.discover()).supported_versions
        for v in LEGACY_VERSIONS:
            assert v in sv, f"{v!r} missing from {sv!r}"

    async def test_capabilities_structure(self) -> None:
        ops, _ = _make_ops()
        caps = (await ops.discover()).capabilities
        assert "tools" in caps
        assert "resources" in caps
        assert "prompts" in caps

    async def test_server_name_is_hub(self) -> None:
        ops, _ = _make_ops()
        assert (await ops.discover()).server_name == "slm-mcp-hub"

    async def test_instructions_mention_meta_tools(self) -> None:
        ops, _ = _make_ops()
        instr = (await ops.discover()).instructions
        assert "search_tools" in instr
        assert "call_tool" in instr


class TestNegotiate:
    async def test_known_legacy_preserved(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.negotiate("2025-11-25", client_info={})
        assert isinstance(outcome, InitializeOutcome)
        assert outcome.protocol_version == "2025-11-25"

    async def test_unknown_version_falls_back(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.negotiate("9999-99-99", client_info={})
        assert outcome.protocol_version in LEGACY_VERSIONS

    async def test_capabilities_present(self) -> None:
        ops, _ = _make_ops()
        assert "tools" in (await ops.negotiate("2025-11-25", client_info={})).capabilities

    async def test_modern_version_not_accepted_as_negotiate(self) -> None:
        # MODERN_VERSION goes through server/discover, not initialize
        ops, _ = _make_ops()
        outcome = await ops.negotiate(MODERN_VERSION, client_info={})
        # Must still return a valid version (falls back or accepts)
        assert outcome.protocol_version  # non-empty


# ===========================================================================
# HubProductOperations — call_tool (meta handler)
# ===========================================================================

class TestCallTool:
    async def test_self_reference_call_tool_is_error(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.call_tool(
            {"tool": "call_tool"}, session_id="sid", client_name="c"
        )
        assert isinstance(outcome, CallToolOutcome)
        assert outcome.is_error is True

    async def test_hub_alias_self_reference_is_error(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.call_tool(
            {"tool": "hub__call_tool"}, session_id="sid", client_name="c"
        )
        assert outcome.is_error is True

    async def test_missing_tool_key_is_error(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.call_tool(
            {}, session_id="sid", client_name="c"
        )
        assert outcome.is_error is True

    async def test_empty_tool_name_is_error(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.call_tool(
            {"tool": "   "}, session_id="sid", client_name="c"
        )
        assert outcome.is_error is True

    async def test_routes_to_router(self) -> None:
        ops, router = _make_ops()
        outcome = await ops.call_tool(
            {"tool": "github__search", "arguments": {"q": "x"}},
            session_id="sid", client_name="c",
        )
        assert isinstance(outcome, CallToolOutcome)
        router.route_tool_call.assert_awaited_once_with("github__search", {"q": "x"})

    async def test_nested_search_tools_meta(self) -> None:
        ops, router = _make_ops()
        outcome = await ops.call_tool(
            {"tool": "search_tools", "arguments": {"query": "search"}},
            session_id="sid", client_name="c",
        )
        assert not outcome.is_error
        router.route_tool_call.assert_not_awaited()

    async def test_nested_list_servers_meta(self) -> None:
        ops, router = _make_ops()
        outcome = await ops.call_tool(
            {"tool": "list_servers"},
            session_id="sid", client_name="c",
        )
        assert not outcome.is_error
        router.route_tool_call.assert_not_awaited()

    async def test_dotted_arguments_repaired(self) -> None:
        ops, router = _make_ops()
        await ops.call_tool(
            {"tool": "github__search", "arguments.q": "test"},
            session_id="sid", client_name="c",
        )
        router.route_tool_call.assert_awaited_once_with("github__search", {"q": "test"})

    async def test_plugin_notified_on_routed_call(self) -> None:
        ops, _ = _make_ops()
        hub = MagicMock()
        hub.notify_plugins_tool_call_after = AsyncMock()
        ops._hub = hub
        await ops.call_tool(
            {"tool": "github__search", "arguments": {}},
            session_id="sid", client_name="c",
        )
        hub.notify_plugins_tool_call_after.assert_awaited_once()

    async def test_plugin_error_does_not_propagate(self) -> None:
        ops, _ = _make_ops()
        hub = MagicMock()
        hub.notify_plugins_tool_call_after = AsyncMock(side_effect=RuntimeError("boom"))
        ops._hub = hub
        outcome = await ops.call_tool(
            {"tool": "github__search", "arguments": {}},
            session_id="sid", client_name="c",
        )
        assert isinstance(outcome, CallToolOutcome)

    async def test_plugin_args_are_normalized(self) -> None:
        ops, _ = _make_ops()
        hub = MagicMock()
        hub.notify_plugins_tool_call_after = AsyncMock()
        ops._hub = hub
        await ops.call_tool(
            {"tool": "github__search", "arguments.q": "A-1"},
            session_id="sid", client_name="c",
        )
        kw = hub.notify_plugins_tool_call_after.await_args.kwargs
        assert kw["args"] == {"q": "A-1"}
        assert kw["tool"] == "search"
        assert kw["server"] == "github"


# ===========================================================================
# HubProductOperations — search_tools / list_servers
# ===========================================================================

class TestSearchTools:
    async def test_finds_match(self) -> None:
        ops, _ = _make_ops(
            tools=[{"name": "list_files", "description": "List files", "inputSchema": {}}]
        )
        outcome = await ops.search_tools({"query": "list"})
        data = json.loads(outcome.content[0]["text"])
        assert data["found"] >= 1

    async def test_no_match(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.search_tools({"query": "zzznomatch"})
        data = json.loads(outcome.content[0]["text"])
        assert data["found"] == 0

    async def test_non_string_query_is_error(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.search_tools({"query": 42})
        assert outcome.is_error is True

    async def test_empty_query_returns_all(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.search_tools({"query": ""})
        data = json.loads(outcome.content[0]["text"])
        assert data["found"] >= 1  # github__search is in registry


class TestListServers:
    async def test_includes_known_server(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.list_servers()
        data = json.loads(outcome.content[0]["text"])
        names = [s["server"] for s in data["servers"]]
        assert "github" in names


# ===========================================================================
# HubProductOperations — resources + prompts
# ===========================================================================

class TestResources:
    async def test_list_resources(self) -> None:
        ops, _ = _make_ops(
            resources=[{"uri": "file://x", "name": "x", "mimeType": "text/plain"}]
        )
        outcome = await ops.list_resources()
        assert isinstance(outcome, ResourcesListOutcome)
        assert len(outcome.resources) >= 1

    async def test_list_resource_templates(self) -> None:
        ops, _ = _make_ops()
        outcome = await ops.list_resource_templates()
        assert isinstance(outcome, ResourceTemplatesListOutcome)

    async def test_read_resource_delegates(self) -> None:
        ops, router = _make_ops()
        outcome = await ops.read_resource("ns:r")
        assert isinstance(outcome, ResourceReadOutcome)
        router.route_resource_read.assert_awaited_once_with("ns:r")


class TestPrompts:
    async def test_list_prompts(self) -> None:
        ops, _ = _make_ops(
            prompts=[{"name": "my_prompt", "description": "A prompt"}]
        )
        outcome = await ops.list_prompts()
        assert isinstance(outcome, PromptsListOutcome)
        assert len(outcome.prompts) >= 1

    async def test_get_prompt_delegates(self) -> None:
        ops, router = _make_ops()
        outcome = await ops.get_prompt("github__p", {})
        assert isinstance(outcome, PromptGetOutcome)
        router.route_prompt_get.assert_awaited_once_with("github__p", {})


# ===========================================================================
# Conversion — neutral → wire dicts
# ===========================================================================

class TestToWire:
    def test_tools_list_to_wire(self) -> None:
        outcome = ToolsListOutcome(tools=(
            {"name": "t", "description": "d", "inputSchema": {"type": "object"}},
        ))
        wire = tools_list_to_wire(outcome)
        assert wire == {"tools": [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]}

    def test_call_tool_success_wire(self) -> None:
        outcome = CallToolOutcome(
            content=({"type": "text", "text": "hi"},),
            is_error=False, server_name="s",
        )
        wire = call_tool_outcome_to_wire(outcome)
        assert wire["content"] == [{"type": "text", "text": "hi"}]
        assert not wire.get("isError")

    def test_call_tool_error_wire(self) -> None:
        outcome = CallToolOutcome(
            content=({"type": "text", "text": "err"},),
            is_error=True, server_name="s",
        )
        wire = call_tool_outcome_to_wire(outcome)
        assert wire["isError"] is True

    def test_resources_list_to_wire(self) -> None:
        outcome = ResourcesListOutcome(resources=({"uri": "f://x", "name": "x"},))
        assert resources_list_to_wire(outcome) == {"resources": [{"uri": "f://x", "name": "x"}]}

    def test_resource_templates_to_wire(self) -> None:
        outcome = ResourceTemplatesListOutcome(resource_templates=({"uriTemplate": "f://{x}"},))
        assert resource_templates_list_to_wire(outcome) == {"resourceTemplates": [{"uriTemplate": "f://{x}"}]}

    def test_prompts_list_to_wire(self) -> None:
        outcome = PromptsListOutcome(prompts=({"name": "p", "description": "d"},))
        assert prompts_list_to_wire(outcome) == {"prompts": [{"name": "p", "description": "d"}]}

    def test_resource_read_to_wire(self) -> None:
        raw = {"contents": [{"uri": "x", "text": "y"}]}
        assert resource_read_to_wire(ResourceReadOutcome(raw=raw)) == raw

    def test_prompt_get_to_wire(self) -> None:
        raw = {"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]}
        assert prompt_get_to_wire(PromptGetOutcome(raw=raw)) == raw

    def test_initialize_to_wire(self) -> None:
        from slm_mcp_hub.core.constants import VERSION
        outcome = InitializeOutcome(
            protocol_version="2025-11-25",
            capabilities={"tools": {"listChanged": True}},
            server_name="slm-mcp-hub",
            server_version=VERSION,
        )
        wire = initialize_to_wire(outcome)
        assert wire["protocolVersion"] == "2025-11-25"
        assert wire["serverInfo"]["name"] == "slm-mcp-hub"
        assert wire["capabilities"]["tools"]["listChanged"] is True

    def test_discover_to_wire(self) -> None:
        from slm_mcp_hub.core.constants import VERSION
        outcome = DiscoverOutcome(
            supported_versions=("2026-07-28", "2025-11-25"),
            capabilities={"tools": {"listChanged": True}},
            server_name="slm-mcp-hub",
            server_version=VERSION,
            instructions="Use search_tools.",
        )
        wire = discover_to_wire(outcome)
        assert "2026-07-28" in wire["supportedVersions"]
        assert wire["serverInfo"]["name"] == "slm-mcp-hub"
        assert wire["instructions"] == "Use search_tools."


# ===========================================================================
# Conversion — SDK types (content blocks)
# ===========================================================================

class TestSDKContentBlockToDict:
    def test_text(self) -> None:
        d = sdk_content_block_to_dict(t.TextContent(type="text", text="hello"))
        assert d == {"type": "text", "text": "hello"}

    def test_image(self) -> None:
        d = sdk_content_block_to_dict(t.ImageContent(type="image", data="b64", mimeType="image/png"))
        assert d["type"] == "image"
        assert d["mimeType"] == "image/png"
        assert d["data"] == "b64"

    def test_audio(self) -> None:
        d = sdk_content_block_to_dict(t.AudioContent(type="audio", data="b64", mimeType="audio/mp3"))
        assert d["type"] == "audio"
        assert d["data"] == "b64"

    def test_embedded_resource_text(self) -> None:
        res = t.TextResourceContents(uri="f://x", text="content", mimeType="text/plain")
        d = sdk_content_block_to_dict(t.EmbeddedResource(type="resource", resource=res))
        assert d["type"] == "resource"
        assert d["resource"]["text"] == "content"

    def test_embedded_resource_blob(self) -> None:
        res = t.BlobResourceContents(uri="f://x", blob="b64d", mimeType="application/octet-stream")
        d = sdk_content_block_to_dict(t.EmbeddedResource(type="resource", resource=res))
        assert d["resource"]["blob"] == "b64d"

    def test_resource_link(self) -> None:
        d = sdk_content_block_to_dict(t.ResourceLink(type="resource_link", uri="f://x", name="n"))
        assert d["type"] == "resource_link"
        assert d["uri"] == "f://x"
        assert "description" not in d
        assert "mimeType" not in d

    def test_resource_link_with_description(self) -> None:
        """Covers line 125: ResourceLink.description is not None branch."""
        d = sdk_content_block_to_dict(
            t.ResourceLink(type="resource_link", uri="f://x", name="n", description="A resource")
        )
        assert d["description"] == "A resource"

    def test_resource_link_with_mime_type(self) -> None:
        """Covers line 127: ResourceLink.mime_type is not None branch."""
        d = sdk_content_block_to_dict(
            t.ResourceLink(type="resource_link", uri="f://x", name="n", mimeType="text/html")
        )
        assert d["mimeType"] == "text/html"

    def test_embedded_resource_unknown_content_type_raises(self) -> None:
        """Covers line 144: unknown resource content type inside EmbeddedResource."""
        block = MagicMock(spec=t.EmbeddedResource)
        block.__class__ = t.EmbeddedResource
        unknown_resource = MagicMock()
        unknown_resource.__class__ = type("FutureResourceContent", (), {})
        block.resource = unknown_resource
        with pytest.raises(ValueError, match="Unknown resource content type"):
            sdk_content_block_to_dict(block)

    def test_unknown_block_type_raises(self) -> None:
        """Covers line 147: fallthrough for unknown ContentBlock subtype."""
        block = MagicMock()
        block.__class__ = type("FutureBlock", (), {})
        with pytest.raises(ValueError, match="Unknown content block type"):
            sdk_content_block_to_dict(block)


class TestSDKCallToolResultToOutcome:
    def test_basic(self) -> None:
        r = t.CallToolResult(content=[t.TextContent(type="text", text="ok")])
        outcome = sdk_call_tool_result_to_outcome(r, server_name="srv")
        assert isinstance(outcome, CallToolOutcome)
        assert outcome.server_name == "srv"
        assert not outcome.is_error
        assert outcome.content[0]["text"] == "ok"

    def test_is_error_true(self) -> None:
        r = t.CallToolResult(content=[t.TextContent(type="text", text="e")], isError=True)
        outcome = sdk_call_tool_result_to_outcome(r, server_name="s")
        assert outcome.is_error is True


class TestDictToSDKContentBlock:
    def test_text(self) -> None:
        assert isinstance(dict_to_sdk_content_block({"type": "text", "text": "hi"}), t.TextContent)

    def test_image(self) -> None:
        b = dict_to_sdk_content_block({"type": "image", "data": "b", "mimeType": "image/jpeg"})
        assert isinstance(b, t.ImageContent)

    def test_audio(self) -> None:
        b = dict_to_sdk_content_block({"type": "audio", "data": "b", "mimeType": "audio/mp3"})
        assert isinstance(b, t.AudioContent)

    def test_embedded_text_resource(self) -> None:
        b = dict_to_sdk_content_block({
            "type": "resource",
            "resource": {"uri": "f://x", "text": "hi", "mimeType": "text/plain"},
        })
        assert isinstance(b, t.EmbeddedResource)
        assert isinstance(b.resource, t.TextResourceContents)

    def test_embedded_blob_resource(self) -> None:
        b = dict_to_sdk_content_block({
            "type": "resource",
            "resource": {"uri": "f://x", "blob": "b64", "mimeType": "application/octet-stream"},
        })
        assert isinstance(b.resource, t.BlobResourceContents)

    def test_embedded_unknown_resource_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown resource content"):
            dict_to_sdk_content_block({"type": "resource", "resource": {"uri": "f://x"}})

    def test_resource_link(self) -> None:
        """Covers line 196: dict_to_sdk_content_block for resource_link type."""
        b = dict_to_sdk_content_block({"type": "resource_link", "uri": "f://x", "name": "n"})
        assert isinstance(b, t.ResourceLink)
        assert b.uri == "f://x"
        assert b.name == "n"

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown content block type"):
            dict_to_sdk_content_block({"type": "future_type_xyz", "data": "x"})


# ===========================================================================
# Conversion — neutral → SDK types
# ===========================================================================

class TestOutcomeToSDK:
    def test_tools_list_to_sdk(self) -> None:
        outcome = ToolsListOutcome(tools=(
            {"name": "my_tool", "description": "d", "inputSchema": {"type": "object", "properties": {}}},
        ))
        sdk_r = tools_list_to_sdk(outcome)
        assert isinstance(sdk_r, t.ListToolsResult)
        assert len(sdk_r.tools) == 1
        assert sdk_r.tools[0].name == "my_tool"

    def test_call_tool_to_sdk_success(self) -> None:
        outcome = CallToolOutcome(
            content=({"type": "text", "text": "hello"},),
            is_error=False, server_name="s",
        )
        sdk_r = call_tool_outcome_to_sdk(outcome)
        assert isinstance(sdk_r, t.CallToolResult)
        assert isinstance(sdk_r.content[0], t.TextContent)
        assert not sdk_r.is_error

    def test_call_tool_to_sdk_error(self) -> None:
        outcome = CallToolOutcome(
            content=({"type": "text", "text": "err"},),
            is_error=True, server_name="s",
        )
        assert call_tool_outcome_to_sdk(outcome).is_error is True

    def test_call_tool_unknown_content_raises(self) -> None:
        outcome = CallToolOutcome(
            content=({"type": "future_xyz", "data": "x"},),
            is_error=False, server_name="s",
        )
        with pytest.raises(ValueError, match="Unknown content block type"):
            call_tool_outcome_to_sdk(outcome)

    def test_resource_read_text_to_sdk(self) -> None:
        outcome = ResourceReadOutcome(raw={
            "contents": [{"uri": "f://x", "text": "hello", "mimeType": "text/plain"}]
        })
        sdk_r = resource_read_to_sdk(outcome)
        assert isinstance(sdk_r, t.ReadResourceResult)
        assert isinstance(sdk_r.contents[0], t.TextResourceContents)

    def test_resource_read_blob_to_sdk(self) -> None:
        outcome = ResourceReadOutcome(raw={
            "contents": [{"uri": "f://x", "blob": "b64", "mimeType": "application/octet-stream"}]
        })
        assert isinstance(resource_read_to_sdk(outcome).contents[0], t.BlobResourceContents)

    def test_resource_read_unknown_raises(self) -> None:
        outcome = ResourceReadOutcome(raw={"contents": [{"uri": "f://x"}]})
        with pytest.raises(ValueError, match="Unknown resource content"):
            resource_read_to_sdk(outcome)

    def test_prompt_get_to_sdk(self) -> None:
        outcome = PromptGetOutcome(raw={
            "messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]
        })
        sdk_r = prompt_get_to_sdk(outcome)
        assert isinstance(sdk_r, t.GetPromptResult)
        assert sdk_r.messages[0].role == "user"

    def test_prompt_get_unknown_content_raises(self) -> None:
        outcome = PromptGetOutcome(raw={
            "messages": [{"role": "user", "content": {"type": "zap_future", "data": "x"}}]
        })
        with pytest.raises(ValueError, match="Unknown content block type"):
            prompt_get_to_sdk(outcome)


# ===========================================================================
# Regression guards — P02 harsh-audit findings (2026-08-04)
# ===========================================================================

class TestGrokAuditRegressions:
    """Locks the three wire-fidelity defects caught in the first P02 draft."""

    async def test_routed_call_preserves_unmodelled_result_keys(self) -> None:
        """Finding 2: a federated tool result must survive VERBATIM — the original
        endpoint returned router.result by identity, so structuredContent, _meta,
        and an explicit ``isError: false`` must not be dropped."""
        ops, router = _make_ops()
        upstream = {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"rows": [1, 2, 3]},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "up"}},
            "isError": False,
        }
        router.route_tool_call = AsyncMock(return_value=RouteResult(
            result=upstream, server_name="github", tool_name="search",
            duration_ms=1, success=True,
        ))
        outcome = await ops.route_tool("github__search", {"q": "x"}, "sess")
        assert outcome.raw == upstream
        wire = call_tool_outcome_to_wire(outcome)
        assert wire == upstream  # byte-for-byte; unmodelled keys intact
        assert wire["structuredContent"] == {"rows": [1, 2, 3]}
        assert "_meta" in wire
        assert wire["isError"] is False  # explicit false preserved, not stripped

    async def test_meta_tool_success_has_no_raw_and_omits_iserror(self) -> None:
        """Hub-generated results (raw is None) still build the minimal success
        shape: content only, no isError key."""
        ops, _ = _make_ops()
        outcome = await ops.search_tools({"query": "search"})
        assert outcome.raw is None
        wire = call_tool_outcome_to_wire(outcome)
        assert set(wire) == {"content"}

    async def test_list_tools_schemas_isolated_across_calls(self) -> None:
        """Finding 1: tools/list must not share mutable schema objects — mutating
        one response cannot corrupt the next."""
        ops, _ = _make_ops()
        first = await ops.list_tools()
        call_tool_schema = first.tools[1]["inputSchema"]
        call_tool_schema["properties"]["tool"]["description"] = "CORRUPTED"
        call_tool_schema["properties"]["injected"] = {"type": "string"}
        second = await ops.list_tools()
        second_schema = second.tools[1]["inputSchema"]
        assert second_schema["properties"]["tool"]["description"] == (
            "Full tool name from search_tools results (e.g., 'context7__query-docs')"
        )
        assert "injected" not in second_schema["properties"]

    async def test_call_tool_schema_descriptions_are_extraction_identical(self) -> None:
        """Finding 3: extracted schema text must match the pre-refactor endpoint."""
        ops, _ = _make_ops()
        props = (await ops.list_tools()).tools[1]["inputSchema"]["properties"]
        assert props["tool"]["description"] == (
            "Full tool name from search_tools results (e.g., 'context7__query-docs')"
        )
        assert props["arguments"]["description"] == (
            "Arguments to pass to the tool — see inputSchema from search_tools"
        )
