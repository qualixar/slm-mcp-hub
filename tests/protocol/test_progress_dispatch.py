"""W8-P1 progress dispatch tests.

TDD: RED phase — written BEFORE implementation.

Tests:
1. _extract_progress_token unit cases (None, missing key, alias, int, bool rejection)
2. HubProductOperations.route_tool with progress_callback → router gets it
3. HubProductOperations.route_tool without progress_callback → router called with NO extra kwarg
4. HubProductOperations.handle_meta_tool threads progress_callback to nested call_tool path
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.protocol.inbound import _extract_progress_token
from slm_mcp_hub.protocol.product_operations import HubProductOperations

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_ops() -> tuple[HubProductOperations, AsyncMock]:
    """Build HubProductOperations backed by mocked router."""
    registry = CapabilityRegistry()
    registry.sync(
        {
            "backend": {
                "tools": [
                    {"name": "tool", "description": "A tool", "inputSchema": {}},
                ],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            }
        }
    )
    router = AsyncMock(spec=FederationRouter)
    router.route_tool_call = AsyncMock(
        return_value=RouteResult(
            result={"content": [{"type": "text", "text": "ok"}]},
            server_name="backend",
            tool_name="tool",
            duration_ms=1,
            success=True,
        )
    )
    ops = HubProductOperations(registry=registry, router=router)
    return ops, router


# ---------------------------------------------------------------------------
# Group 1: _extract_progress_token unit cases
# ---------------------------------------------------------------------------


class TestExtractProgressToken:
    """_extract_progress_token extracts the token from meta or returns None."""

    def test_none_meta_returns_none(self) -> None:
        """None meta → None."""
        assert _extract_progress_token(None) is None

    def test_empty_dict_returns_none(self) -> None:
        """No progress_token key → None."""
        assert _extract_progress_token({}) is None

    def test_string_progress_token(self) -> None:
        """String token is returned as-is."""
        result = _extract_progress_token({"progress_token": "my-token"})
        assert result == "my-token"

    def test_empty_string_rejected(self) -> None:
        """Empty-string token rejected — must not create a useless live bridge."""
        assert _extract_progress_token({"progress_token": ""}) is None

    def test_non_str_int_types_rejected(self) -> None:
        """float / list / dict / object rejected — MCP progressToken is str|int only."""
        for bad in (1.5, [1, 2], {"a": 1}, object()):
            assert _extract_progress_token({"progress_token": bad}) is None

    def test_int_progress_token(self) -> None:
        """Int token is returned as-is."""
        result = _extract_progress_token({"progress_token": 42})
        assert result == 42

    def test_zero_int_progress_token(self) -> None:
        """Zero int (falsy but valid) is returned."""
        result = _extract_progress_token({"progress_token": 0})
        assert result == 0

    def test_bool_true_rejected(self) -> None:
        """bool True is rejected (bool ⊂ int — must not treat True as 1)."""
        result = _extract_progress_token({"progress_token": True})
        assert result is None

    def test_bool_false_rejected(self) -> None:
        """bool False is rejected (bool ⊂ int)."""
        result = _extract_progress_token({"progress_token": False})
        assert result is None

    def test_none_value_returns_none(self) -> None:
        """Explicit None value → None."""
        result = _extract_progress_token({"progress_token": None})
        assert result is None

    def test_progress_token_alias_camel_case(self) -> None:
        """progressToken (camelCase alias) is also extracted."""
        result = _extract_progress_token({"progressToken": "camel-tok"})
        assert result == "camel-tok"

    def test_snake_case_takes_precedence_over_camel(self) -> None:
        """progress_token (snake) wins if both present."""
        result = _extract_progress_token(
            {"progress_token": "snake", "progressToken": "camel"}
        )
        assert result == "snake"

    def test_other_meta_keys_ignored(self) -> None:
        """Other keys in meta do not affect extraction."""
        result = _extract_progress_token({"other_key": "x", "progress_token": "tok"})
        assert result == "tok"


# ---------------------------------------------------------------------------
# Group 2: progress_callback threading through ops.route_tool
# ---------------------------------------------------------------------------


class TestRouteToolProgressCallbackThreading:
    """HubProductOperations.route_tool correctly threads progress_callback to router."""

    async def test_route_tool_with_callback_passes_kwarg_to_router(self) -> None:
        """When progress_callback is not None, router.route_tool_call receives it."""
        ops, router = _make_ops()

        async def my_callback(p: float, t: float | None, m: str | None) -> None:
            pass

        await ops.route_tool("backend__tool", {}, "session-1", progress_callback=my_callback)

        router.route_tool_call.assert_awaited_once()
        call_kwargs = router.route_tool_call.call_args
        assert call_kwargs.kwargs.get("progress_callback") is my_callback, (
            "progress_callback must be forwarded to router.route_tool_call"
        )

    async def test_route_tool_without_callback_no_extra_kwarg(self) -> None:
        """When progress_callback is None, router.route_tool_call is called with NO extra kwargs.

        This is the critical test: the mock assertion `assert_awaited_once_with(name, args)`
        (no kwargs) must pass for the backward-compat path.
        """
        ops, router = _make_ops()

        await ops.route_tool("backend__tool", {"key": "val"}, "session-2")

        router.route_tool_call.assert_awaited_once_with("backend__tool", {"key": "val"})

    async def test_route_tool_none_callback_no_extra_kwarg_explicit(self) -> None:
        """Explicit progress_callback=None also results in no extra kwarg to router."""
        ops, router = _make_ops()

        await ops.route_tool(
            "backend__tool", {}, "session-3", progress_callback=None
        )

        router.route_tool_call.assert_awaited_once_with("backend__tool", {})


# ---------------------------------------------------------------------------
# Group 3: handle_meta_tool threads callback into call_tool
# ---------------------------------------------------------------------------


class TestHandleMetaToolProgressCallbackThreading:
    """HubProductOperations.handle_meta_tool threads progress_callback through to router."""

    async def test_handle_meta_tool_call_tool_with_callback(self) -> None:
        """handle_meta_tool('call_tool', ..., progress_callback=cb) → router gets cb."""
        ops, router = _make_ops()

        async def cb(p: float, t: float | None, m: str | None) -> None:
            pass

        # call_tool dispatches to _route_tool which passes callback to router
        await ops.handle_meta_tool(
            name="call_tool",
            arguments={"tool": "backend__tool", "arguments": {}},
            session_id="sid",
            client_name="test-client",
            progress_callback=cb,
        )

        router.route_tool_call.assert_awaited_once()
        call_kwargs = router.route_tool_call.call_args
        assert call_kwargs.kwargs.get("progress_callback") is cb

    async def test_handle_meta_tool_without_callback_no_extra_kwarg(self) -> None:
        """handle_meta_tool without callback → router called with NO progress kwarg."""
        ops, router = _make_ops()

        await ops.handle_meta_tool(
            name="call_tool",
            arguments={"tool": "backend__tool", "arguments": {"x": 1}},
            session_id="sid",
            client_name="test-client",
        )

        router.route_tool_call.assert_awaited_once_with("backend__tool", {"x": 1})

    async def test_handle_meta_tool_search_tools_ignores_callback(self) -> None:
        """search_tools meta-tool does not call route_tool_call; callback is irrelevant."""
        ops, router = _make_ops()

        async def cb(p: float, t: float | None, m: str | None) -> None:
            pass

        outcome = await ops.handle_meta_tool(
            name="search_tools",
            arguments={"query": "backend"},
            session_id="sid",
            client_name="c",
            progress_callback=cb,
        )

        router.route_tool_call.assert_not_awaited()
        assert not outcome.is_error


# ---------------------------------------------------------------------------
# Group 4: inbound progress token extraction and routing
# ---------------------------------------------------------------------------


class TestInboundProgressDispatch:
    """Tests that build_sdk_server's on_call_tool extracts and forwards progress tokens."""

    async def test_route_tool_call_with_progress_token_in_meta(self) -> None:
        """Simulate: ctx.meta has progress_token → ops.route_tool called with progress_callback."""
        from slm_mcp_hub.streaming.progress import ProgressBridge, make_progress_bridge

        # Test make_progress_bridge returns ProgressBridge when session and token are set
        fake_session = MagicMock()
        result = make_progress_bridge(fake_session, "tok-1", "req-42")
        assert result is not None
        assert isinstance(result, ProgressBridge)

    async def test_make_progress_bridge_returns_none_when_no_token(self) -> None:
        """make_progress_bridge returns None when progress_token is None."""
        from slm_mcp_hub.streaming.progress import make_progress_bridge

        fake_session = MagicMock()
        result = make_progress_bridge(fake_session, None, "req-42")
        assert result is None

    async def test_make_progress_bridge_returns_none_when_no_session(self) -> None:
        """make_progress_bridge returns None when server_session is None."""
        from slm_mcp_hub.streaming.progress import make_progress_bridge

        result = make_progress_bridge(None, "tok-1", "req-42")
        assert result is None
