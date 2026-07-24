"""Regression tests for call_tool argument normalisation (AIDEV-277).

The hub used to read the target tool's arguments with a bare
``arguments.get("arguments", {})``. That silently produced an empty
argument map when a client flattened the arguments to the top level, and
forwarded a raw ``str`` unparsed when a client JSON-encoded them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.server.mcp_endpoint import (
    MCPEndpoint,
    _coerce_object,
    _normalise_tool_arguments,
)
from slm_mcp_hub.session.manager import SessionManager


def _make_endpoint():
    registry = CapabilityRegistry()
    registry.sync({
        "jira": {
            "tools": [{"name": "get_issue", "description": "Get an issue", "inputSchema": {}}],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        },
    })
    router = AsyncMock(spec=FederationRouter)
    router.route_tool_call = AsyncMock(return_value=RouteResult(
        result={"content": [{"type": "text", "text": "ok"}]},
        server_name="jira", tool_name="get_issue", duration_ms=1, success=True,
    ))
    sessions = SessionManager()
    session_id = sessions.create_session(client_name="test")
    return MCPEndpoint(registry, router, sessions), session_id, router


async def _call(endpoint, session_id, meta_arguments):
    return await endpoint.handle_tools_call(
        session_id, {"name": "call_tool", "arguments": meta_arguments},
    )


class TestCoerceObject:
    def test_none_becomes_empty_dict(self):
        assert _coerce_object(None) == ({}, None)

    def test_dict_passes_through(self):
        assert _coerce_object({"a": 1}) == ({"a": 1}, None)

    def test_json_string_is_parsed(self):
        assert _coerce_object('{"a": 1}') == ({"a": 1}, None)

    def test_blank_string_becomes_empty_dict(self):
        assert _coerce_object("   ") == ({}, None)

    def test_invalid_json_string_reports_error(self):
        value, error = _coerce_object("{not json")
        assert value is None
        assert "not valid JSON" in error

    @pytest.mark.parametrize("bad", [[1, 2], 42, True])
    def test_non_object_reports_error(self, bad):
        value, error = _coerce_object(bad)
        assert value is None
        assert "expected an object" in error

    def test_json_string_encoding_non_object_reports_error(self):
        value, error = _coerce_object("[1, 2]")
        assert value is None
        assert "expected an object" in error


class TestNormaliseToolArguments:
    def test_nested_form(self):
        payload = {"tool": "jira__get_issue", "arguments": {"issue_key": "A-1"}}
        assert _normalise_tool_arguments(payload) == ({"issue_key": "A-1"}, None)

    def test_flattened_form(self):
        payload = {"tool": "jira__get_issue", "issue_key": "A-1"}
        assert _normalise_tool_arguments(payload) == ({"issue_key": "A-1"}, None)

    def test_stringified_form(self):
        payload = {"tool": "jira__get_issue", "arguments": '{"issue_key": "A-1"}'}
        assert _normalise_tool_arguments(payload) == ({"issue_key": "A-1"}, None)

    def test_nested_wins_over_siblings(self):
        payload = {
            "tool": "jira__get_issue",
            "arguments": {"issue_key": "NESTED"},
            "issue_key": "SIBLING",
        }
        args, error = _normalise_tool_arguments(payload)
        assert error is None
        assert args == {"issue_key": "NESTED"}

    def test_no_arguments_at_all_is_empty(self):
        assert _normalise_tool_arguments({"tool": "jira__get_issue"}) == ({}, None)

    def test_explicit_empty_arguments_beats_siblings(self):
        """An explicit 'arguments' key wins even when empty.

        Selecting the flattened form on emptiness rather than key presence
        let unrelated sibling keys silently redefine a deliberately
        argument-less call — the same silent-wrong-call class this module
        exists to prevent.
        """
        payload = {"tool": "jira__get_issue", "arguments": {}, "reasoning": "because"}
        assert _normalise_tool_arguments(payload) == ({}, None)

    @pytest.mark.parametrize("empty", [{}, None, "null", "", "{}"])
    def test_present_but_empty_arguments_never_flattens(self, empty):
        payload = {"tool": "jira__get_issue", "arguments": empty, "stray": "leak"}
        assert _normalise_tool_arguments(payload) == ({}, None)

    def test_invalid_arguments_is_not_rescued_by_siblings(self):
        payload = {"tool": "jira__get_issue", "arguments": "{bad", "stray": "leak"}
        args, error = _normalise_tool_arguments(payload)
        assert args is None
        assert "'arguments' is invalid" in error

    def test_invalid_arguments_reports_error(self):
        args, error = _normalise_tool_arguments({"tool": "x", "arguments": "{bad"})
        assert args is None
        assert "'arguments' is invalid" in error


class TestCallToolArgumentShapes:
    """End-to-end through handle_tools_call, asserting what reaches the router."""

    @pytest.mark.asyncio
    async def test_nested_arguments_are_forwarded(self):
        endpoint, sid, router = _make_endpoint()
        await _call(endpoint, sid, {
            "tool": "jira__get_issue", "arguments": {"issue_key": "A-1"},
        })
        router.route_tool_call.assert_awaited_once_with(
            "jira__get_issue", {"issue_key": "A-1"},
        )

    @pytest.mark.asyncio
    async def test_flattened_arguments_are_forwarded(self):
        """The core AIDEV-277 bug: these used to be silently dropped."""
        endpoint, sid, router = _make_endpoint()
        result = await _call(endpoint, sid, {
            "tool": "jira__get_issue", "issue_key": "A-1",
        })
        router.route_tool_call.assert_awaited_once_with(
            "jira__get_issue", {"issue_key": "A-1"},
        )
        assert not result.get("isError")

    @pytest.mark.asyncio
    async def test_stringified_arguments_are_parsed(self):
        endpoint, sid, router = _make_endpoint()
        await _call(endpoint, sid, {
            "tool": "jira__get_issue", "arguments": '{"issue_key": "A-1"}',
        })
        router.route_tool_call.assert_awaited_once_with(
            "jira__get_issue", {"issue_key": "A-1"},
        )

    @pytest.mark.asyncio
    async def test_argumentless_call_still_works(self):
        endpoint, sid, router = _make_endpoint()
        await _call(endpoint, sid, {"tool": "jira__get_issue"})
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {})

    @pytest.mark.asyncio
    async def test_malformed_arguments_never_reach_the_router(self):
        endpoint, sid, router = _make_endpoint()
        result = await _call(endpoint, sid, {
            "tool": "jira__get_issue", "arguments": "{not json",
        })
        assert result["isError"] is True
        assert "not valid JSON" in result["content"][0]["text"]
        router.route_tool_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_object_arguments_never_reach_the_router(self):
        endpoint, sid, router = _make_endpoint()
        result = await _call(endpoint, sid, {
            "tool": "jira__get_issue", "arguments": [1, 2, 3],
        })
        assert result["isError"] is True
        assert "expected an object" in result["content"][0]["text"]
        router.route_tool_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_tool_name_is_rejected(self):
        endpoint, sid, router = _make_endpoint()
        result = await _call(endpoint, sid, {"arguments": {"issue_key": "A-1"}})
        assert result["isError"] is True
        assert "'tool' parameter is required" in result["content"][0]["text"]
        router.route_tool_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_meta_tool_dispatch_still_works_through_call_tool(self):
        endpoint, sid, router = _make_endpoint()
        result = await _call(endpoint, sid, {
            "tool": "list_servers", "arguments": {},
        })
        assert not result.get("isError")
        router.route_tool_call.assert_not_awaited()


class TestOuterArgumentsCoercion:
    """params['arguments'] itself arriving as a string used to raise
    'str' object has no attribute 'get' and surface as a JSON-RPC -32603."""

    @pytest.mark.asyncio
    async def test_stringified_outer_arguments_are_parsed(self):
        endpoint, sid, router = _make_endpoint()
        result = await endpoint.handle_tools_call(sid, {
            "name": "call_tool",
            "arguments": '{"tool": "jira__get_issue", "arguments": {"issue_key": "A-1"}}',
        })
        assert not result.get("isError")
        router.route_tool_call.assert_awaited_once_with(
            "jira__get_issue", {"issue_key": "A-1"},
        )

    @pytest.mark.asyncio
    async def test_malformed_outer_arguments_return_clean_error(self):
        endpoint, sid, _ = _make_endpoint()
        result = await endpoint.handle_tools_call(sid, {
            "name": "call_tool", "arguments": "{not json",
        })
        assert result["isError"] is True
        assert "not valid JSON" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_outer_arguments_defaults_to_empty(self):
        endpoint, sid, router = _make_endpoint()
        result = await endpoint.handle_tools_call(sid, {"name": "list_servers"})
        assert not result.get("isError")
        router.route_tool_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_namespaced_call_still_routes(self):
        endpoint, sid, router = _make_endpoint()
        await endpoint.handle_tools_call(sid, {
            "name": "jira__get_issue", "arguments": {"issue_key": "A-1"},
        })
        router.route_tool_call.assert_awaited_once_with(
            "jira__get_issue", {"issue_key": "A-1"},
        )


class TestPluginNotification:
    """The plugin hook is best-effort: it observes normalised arguments and
    must never fail the client's request."""

    @pytest.mark.asyncio
    async def test_plugins_receive_normalised_arguments(self):
        endpoint, sid, _ = _make_endpoint()
        hub = AsyncMock()
        endpoint._hub = hub

        await _call(endpoint, sid, {"tool": "jira__get_issue", "issue_key": "A-1"})

        hub.notify_plugins_tool_call_after.assert_awaited_once()
        kwargs = hub.notify_plugins_tool_call_after.await_args.kwargs
        assert kwargs["args"] == {"issue_key": "A-1"}
        assert kwargs["tool"] == "get_issue"
        assert kwargs["server"] == "jira"

    @pytest.mark.asyncio
    async def test_plugin_failure_does_not_fail_the_call(self):
        endpoint, sid, _ = _make_endpoint()
        hub = AsyncMock()
        hub.notify_plugins_tool_call_after.side_effect = RuntimeError("plugin exploded")
        endpoint._hub = hub

        result = await _call(endpoint, sid, {
            "tool": "jira__get_issue", "arguments": {"issue_key": "A-1"},
        })

        assert not result.get("isError")
        assert result["content"][0]["text"] == "ok"

    @pytest.mark.asyncio
    async def test_unnamespaced_tool_name_is_passed_through(self):
        endpoint, sid, _ = _make_endpoint()
        hub = AsyncMock()
        endpoint._hub = hub

        await _call(endpoint, sid, {"tool": "bare_tool", "arguments": {}})

        kwargs = hub.notify_plugins_tool_call_after.await_args.kwargs
        assert kwargs["tool"] == "bare_tool"


class TestMalformedInputHardening:
    """Client input errors must be reported as -32602, never as -32603.

    Before this hardening, a non-string tool name, a non-dict ``params``,
    or a non-string ``query`` raised ``AttributeError``/``TypeError`` deep
    inside the handler and surfaced as an opaque "Internal server error".
    """

    @pytest.mark.parametrize("bad_name", [123, ["a"], {"a": 1}, None, True])
    async def test_non_string_outer_name_is_invalid_params(self, bad_name):
        endpoint, session_id, _ = _make_endpoint()
        response = await endpoint.handle_jsonrpc(session_id, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": bad_name},
        })
        assert response["error"]["code"] == -32602
        assert "must be a string" in response["error"]["message"]

    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_blank_outer_name_is_invalid_params(self, blank):
        endpoint, session_id, _ = _make_endpoint()
        response = await endpoint.handle_jsonrpc(session_id, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": blank},
        })
        assert response["error"]["code"] == -32602
        assert "required" in response["error"]["message"]

    async def test_outer_name_is_stripped(self):
        endpoint, session_id, router = _make_endpoint()
        await endpoint.handle_tools_call(session_id, {
            "name": "  jira__get_issue  ", "arguments": {"key": "X-1"},
        })
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {"key": "X-1"})

    @pytest.mark.parametrize("bad_params", ["a string", ["a"], 42])
    async def test_non_object_params_is_invalid_params(self, bad_params):
        endpoint, session_id, _ = _make_endpoint()
        response = await endpoint.handle_jsonrpc(session_id, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": bad_params,
        })
        assert response["error"]["code"] == -32602
        assert "must be an object" in response["error"]["message"]

    async def test_null_params_is_treated_as_empty_object(self):
        endpoint, session_id, _ = _make_endpoint()
        response = await endpoint.handle_jsonrpc(session_id, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": None,
        })
        assert "result" in response

    @pytest.mark.parametrize("bad_tool", [123, ["a"], {"a": 1}, True, None, "", "   "])
    async def test_non_string_tool_is_rejected_as_tool_error(self, bad_tool):
        endpoint, session_id, router = _make_endpoint()
        result = await _call(endpoint, session_id, {"tool": bad_tool})
        assert result["isError"] is True
        assert "non-empty string" in result["content"][0]["text"]
        router.route_tool_call.assert_not_awaited()

    async def test_tool_name_is_stripped(self):
        endpoint, session_id, router = _make_endpoint()
        await _call(endpoint, session_id, {"tool": " jira__get_issue ", "arguments": {"key": "X-1"}})
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {"key": "X-1"})

    @pytest.mark.parametrize("bad_query", [123, ["a"], {"a": 1}])
    async def test_non_string_search_query_is_rejected(self, bad_query):
        endpoint, session_id, _ = _make_endpoint()
        result = await endpoint.handle_tools_call(
            session_id, {"name": "search_tools", "arguments": {"query": bad_query}},
        )
        assert result["isError"] is True
        assert "'query' must be a string" in result["content"][0]["text"]

    @pytest.mark.parametrize("query", [None, ""])
    async def test_empty_search_query_matches_everything(self, query):
        endpoint, session_id, _ = _make_endpoint()
        result = await endpoint.handle_tools_call(
            session_id, {"name": "search_tools", "arguments": {"query": query}},
        )
        assert "isError" not in result
        assert '"found": 1' in result["content"][0]["text"]

    async def test_search_tolerates_missing_description_and_schema(self):
        endpoint, session_id, _ = _make_endpoint()
        endpoint._registry.sync({
            "jira": {
                "tools": [{"name": "get_issue", "description": None, "inputSchema": None}],
                "resources": [], "resource_templates": [], "prompts": [],
            },
        })
        result = await endpoint.handle_tools_call(
            session_id, {"name": "search_tools", "arguments": {"query": "issue"}},
        )
        assert "isError" not in result

    async def test_json_encoded_null_arguments_becomes_empty(self):
        endpoint, session_id, router = _make_endpoint()
        result = await _call(endpoint, session_id, {"tool": "jira__get_issue", "arguments": "null"})
        assert "isError" not in result
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {})


class TestAdversarialReviewFindings:
    """Regressions for defects found by adversarial review of the first two commits."""

    async def test_explicit_empty_arguments_does_not_leak_siblings_to_router(self):
        endpoint, session_id, router = _make_endpoint()
        result = await _call(endpoint, session_id, {
            "tool": "jira__get_issue", "arguments": {}, "reasoning": "model chatter",
        })
        assert "isError" not in result
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {})

    async def test_nested_arguments_win_over_siblings(self):
        endpoint, session_id, router = _make_endpoint()
        await _call(endpoint, session_id, {
            "tool": "jira__get_issue", "arguments": {"key": "REAL"}, "key": "STRAY",
        })
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {"key": "REAL"})

    def test_oversized_integer_string_is_an_argument_error_not_a_crash(self):
        """Python's integer digit limit raises a bare ValueError, not JSONDecodeError."""
        payload = '{"x": ' + "1" * 5000 + "}"
        args, error = _coerce_object(payload)
        assert args is None
        assert "not valid JSON" in error

    def test_deeply_nested_json_string_is_an_argument_error_not_a_crash(self):
        args, error = _coerce_object("[" * 200_000 + "]" * 200_000)
        assert args is None
        assert "not valid JSON" in error

    async def test_call_tool_cannot_invoke_itself(self):
        endpoint, session_id, router = _make_endpoint()
        result = await _call(endpoint, session_id, {
            "tool": "call_tool",
            "arguments": {"tool": "jira__get_issue", "arguments": {"key": "X-1"}},
        })
        assert result["isError"] is True
        assert "cannot invoke itself" in result["content"][0]["text"]
        router.route_tool_call.assert_not_awaited()

    async def test_call_tool_cannot_invoke_itself_via_alias(self):
        endpoint, session_id, _ = _make_endpoint()
        result = await _call(endpoint, session_id, {"tool": "hub__call_tool", "arguments": {}})
        assert result["isError"] is True
        assert "cannot invoke itself" in result["content"][0]["text"]

    async def test_call_tool_may_still_invoke_other_meta_tools(self):
        endpoint, session_id, _ = _make_endpoint()
        result = await _call(endpoint, session_id, {"tool": "search_tools", "query": "jira"})
        assert "isError" not in result
        assert '"found": 1' in result["content"][0]["text"]
