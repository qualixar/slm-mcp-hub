"""Regression tests for call_tool argument normalisation (AIDEV-277).

The hub used to read the target tool's arguments with a bare
``arguments.get("arguments", {})``. That silently produced an empty
argument map when a client flattened the arguments to the top level, and
forwarded a raw ``str`` unparsed when a client JSON-encoded them.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.server.mcp_endpoint import (
    MCPEndpoint,
    _coerce_object,
    _normalise_tool_arguments,
    _reconstruct_dotted_arguments,
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


class TestDotNotationReconstruction:
    """AIDEV-281: some models flatten nested tool params to dotted top-level
    keys (confirmed on Gemini 3.6 Flash), so ``arguments.issue_key`` arrives
    as a sibling of ``tool`` and the real arguments never reach the backend."""

    def test_single_flattened_key_is_nested(self):
        payload, repaired, skipped = _reconstruct_dotted_arguments({
            "tool": "jira__get_issue", "arguments.issue_key": "A-1",
        })
        assert payload["arguments"] == {"issue_key": "A-1"}
        assert repaired == ["arguments.issue_key"]
        assert skipped == []

    def test_multiple_flattened_keys_merge_into_one_object(self):
        payload, repaired, _ = _reconstruct_dotted_arguments({
            "tool": "jira__search", "arguments.jql": "project = A", "arguments.limit": 10,
        })
        assert payload["arguments"] == {"jql": "project = A", "limit": 10}
        assert repaired == ["arguments.jql", "arguments.limit"]

    def test_multi_level_path_is_reconstructed_to_full_depth(self):
        payload, repaired, _ = _reconstruct_dotted_arguments({
            "tool": "t", "arguments.filter.status": "open", "arguments.filter.owner": "me",
        })
        assert payload["arguments"] == {"filter": {"status": "open", "owner": "me"}}
        assert repaired == ["arguments.filter.owner", "arguments.filter.status"]

    def test_numeric_segments_are_dict_keys_not_list_indices(self):
        payload, _, _ = _reconstruct_dotted_arguments({
            "tool": "t", "arguments.items.0": "first", "arguments.items.1": "second",
        })
        assert payload["arguments"] == {"items": {"0": "first", "1": "second"}}

    def test_no_dotted_keys_is_a_strict_no_op(self):
        original = {"tool": "jira__get_issue", "arguments": {"issue_key": "A-1"}}
        payload, repaired, skipped = _reconstruct_dotted_arguments(original)
        assert payload is original
        assert (repaired, skipped) == ([], [])

    def test_empty_payload_is_a_strict_no_op(self):
        original: dict = {}
        payload, repaired, skipped = _reconstruct_dotted_arguments(original)
        assert payload is original
        assert (repaired, skipped) == ([], [])

    def test_explicit_nested_value_wins_over_conflicting_flattened_key(self):
        payload, repaired, skipped = _reconstruct_dotted_arguments({
            "tool": "t", "arguments": {"issue_key": "EXPLICIT"}, "arguments.issue_key": "FLAT",
        })
        assert payload["arguments"] == {"issue_key": "EXPLICIT"}
        assert repaired == []
        assert skipped == ["arguments.issue_key"]

    def test_flattened_keys_extend_a_partial_explicit_object(self):
        payload, repaired, _ = _reconstruct_dotted_arguments({
            "tool": "t", "arguments": {"jql": "x"}, "arguments.limit": 5,
        })
        assert payload["arguments"] == {"jql": "x", "limit": 5}
        assert repaired == ["arguments.limit"]

    def test_repair_applies_when_arguments_is_present_but_empty(self):
        payload, repaired, _ = _reconstruct_dotted_arguments({
            "tool": "t", "arguments": {}, "arguments.issue_key": "A-1",
        })
        assert payload["arguments"] == {"issue_key": "A-1"}
        assert repaired == ["arguments.issue_key"]

    def test_repair_applies_when_arguments_is_null(self):
        payload, repaired, _ = _reconstruct_dotted_arguments({
            "tool": "t", "arguments": None, "arguments.issue_key": "A-1",
        })
        assert payload["arguments"] == {"issue_key": "A-1"}
        assert repaired == ["arguments.issue_key"]

    def test_explicit_non_object_arguments_is_left_for_the_normaliser(self):
        payload, repaired, skipped = _reconstruct_dotted_arguments({
            "tool": "t", "arguments": '{"issue_key": "A-1"}', "arguments.limit": 5,
        })
        assert payload["arguments"] == '{"issue_key": "A-1"}'
        assert repaired == []
        assert skipped == ["arguments.limit"]

    def test_path_through_an_existing_non_dict_is_discarded_not_raised(self):
        payload, repaired, skipped = _reconstruct_dotted_arguments({
            "tool": "t", "arguments": {"filter": "not-an-object"},
            "arguments.filter.status": "open",
        })
        assert payload["arguments"] == {"filter": "not-an-object"}
        assert repaired == []
        assert skipped == ["arguments.filter.status"]

    def test_path_through_a_flattened_scalar_is_discarded_not_raised(self):
        payload, repaired, skipped = _reconstruct_dotted_arguments({
            "tool": "t", "arguments.filter": "scalar", "arguments.filter.status": "open",
        })
        assert payload["arguments"] == {"filter": "scalar"}
        assert repaired == ["arguments.filter"]
        assert skipped == ["arguments.filter.status"]

    @pytest.mark.parametrize("bad_key", ["arguments.", "arguments..status", "arguments.a."])
    def test_empty_path_segments_are_ignored_without_error(self, bad_key):
        payload, repaired, skipped = _reconstruct_dotted_arguments({"tool": "t", bad_key: "x"})
        assert repaired == []
        assert skipped == [bad_key]
        assert bad_key not in payload

    def test_a_fully_skipped_repair_does_not_invent_an_arguments_key(self):
        """Otherwise a stray dotted key would suppress 277's sibling hoist."""
        payload, repaired, skipped = _reconstruct_dotted_arguments({
            "tool": "t", "arguments.": "x", "issue_key": "A-1",
        })
        assert "arguments" not in payload
        assert _normalise_tool_arguments(payload) == ({"issue_key": "A-1"}, None)
        assert (repaired, skipped) == ([], ["arguments."])

    def test_all_prefixed_keys_are_stripped_from_the_envelope(self):
        payload, _, _ = _reconstruct_dotted_arguments({
            "tool": "t", "arguments.a": 1, "arguments.b.c": 2, "arguments.": 3,
        })
        assert not [k for k in payload if k.startswith("arguments.")]

    def test_tool_and_unrelated_keys_are_preserved_verbatim(self):
        payload, _, _ = _reconstruct_dotted_arguments({
            "tool": " jira__get_issue ", "arguments": {"issue_key": "A-1"},
            "reasoning": "chatter", "arguments.ignored": "x",
        })
        assert payload["tool"] == " jira__get_issue "
        assert payload["reasoning"] == "chatter"

    def test_bare_siblings_survive_a_partial_repair(self):
        """277 hoists bare siblings only while 'arguments' is absent, so a
        synthesised 'arguments' key would silently drop them."""
        payload, repaired, _ = _reconstruct_dotted_arguments({
            "tool": "jira__get_issue", "issue_key": "A-1", "arguments.fields": "summary",
        })
        assert payload["arguments"] == {"issue_key": "A-1", "fields": "summary"}
        assert repaired == ["arguments.fields"]
        assert _normalise_tool_arguments(payload) == (
            {"issue_key": "A-1", "fields": "summary"}, None,
        )

    def test_a_dotted_key_wins_over_a_conflicting_bare_sibling(self):
        payload, _, _ = _reconstruct_dotted_arguments({
            "tool": "t", "issue_key": "SIBLING", "arguments.issue_key": "DOTTED",
        })
        assert payload["arguments"] == {"issue_key": "DOTTED"}

    def test_bare_siblings_are_not_absorbed_when_arguments_is_explicit(self):
        payload, _, _ = _reconstruct_dotted_arguments({
            "tool": "t", "arguments": {}, "reasoning": "chatter", "arguments.a": 1,
        })
        assert payload["arguments"] == {"a": 1}
        assert payload["reasoning"] == "chatter"

    def test_the_original_payload_is_not_mutated(self):
        original = {"tool": "t", "arguments": {"jql": "x"}, "arguments.limit": 5}
        _reconstruct_dotted_arguments(original)
        assert original == {"tool": "t", "arguments": {"jql": "x"}, "arguments.limit": 5}

    def test_nested_explicit_objects_are_not_mutated(self):
        nested = {"status": "open"}
        original = {"tool": "t", "arguments": {"filter": nested}, "arguments.filter.owner": "me"}
        payload, _, _ = _reconstruct_dotted_arguments(original)
        assert payload["arguments"]["filter"] == {"status": "open", "owner": "me"}
        assert nested == {"status": "open"}


class TestDotNotationEndToEnd:
    """The repair runs on the raw envelope before AIDEV-277's normalisation,
    so a dot-notation-only payload never reaches a backend prefixed."""

    async def test_gemini_shaped_payload_reaches_the_backend_nested(self):
        endpoint, sid, router = _make_endpoint()
        result = await _call(endpoint, sid, {
            "tool": "jira__get_issue", "arguments.issue_key": "A-1",
        })
        assert not result.get("isError")
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {"issue_key": "A-1"})

    async def test_legacy_hub_alias_is_repaired_identically(self):
        endpoint, sid, router = _make_endpoint()
        await endpoint.handle_tools_call(sid, {
            "name": "hub__call_tool",
            "arguments": {"tool": "jira__get_issue", "arguments.issue_key": "A-1"},
        })
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {"issue_key": "A-1"})

    async def test_meta_tool_dispatch_through_call_tool_is_repaired(self):
        endpoint, sid, _ = _make_endpoint()
        result = await _call(endpoint, sid, {
            "tool": "search_tools", "arguments.query": "jira issue",
        })
        assert "isError" not in result
        assert '"found": 1' in result["content"][0]["text"]

    async def test_search_tools_query_containing_a_dot_is_unaffected(self):
        endpoint, sid, _ = _make_endpoint()
        result = await endpoint.handle_tools_call(sid, {
            "name": "search_tools", "arguments": {"query": "jira.get_issue"},
        })
        assert "isError" not in result
        assert '"found": 0' in result["content"][0]["text"]

    async def test_list_servers_is_unaffected(self):
        endpoint, sid, _ = _make_endpoint()
        result = await endpoint.handle_tools_call(sid, {"name": "list_servers"})
        assert "isError" not in result

    async def test_explicit_arguments_still_win_end_to_end(self):
        endpoint, sid, router = _make_endpoint()
        await _call(endpoint, sid, {
            "tool": "jira__get_issue",
            "arguments": {"issue_key": "EXPLICIT"},
            "arguments.issue_key": "FLAT",
        })
        router.route_tool_call.assert_awaited_once_with(
            "jira__get_issue", {"issue_key": "EXPLICIT"},
        )

    async def test_argumentless_call_forwards_an_empty_map(self):
        endpoint, sid, router = _make_endpoint()
        await _call(endpoint, sid, {"tool": "jira__get_issue"})
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {})


class TestDotNotationTelemetry:
    """Misbehaving clients must be visible to operators — without leaking
    argument values into the log."""

    async def test_repair_logs_a_warning_with_client_and_paths(self, caplog):
        endpoint, sid, _ = _make_endpoint()
        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.server.mcp_endpoint"):
            await _call(endpoint, sid, {
                "tool": "jira__get_issue", "arguments.issue_key": "SECRET-VALUE",
            })
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "test" in message
        assert "jira__get_issue" in message
        assert "arguments.issue_key" in message

    async def test_argument_values_never_appear_in_the_log(self, caplog):
        endpoint, sid, _ = _make_endpoint()
        with caplog.at_level(logging.DEBUG, logger="slm_mcp_hub.server.mcp_endpoint"):
            await _call(endpoint, sid, {
                "tool": "jira__get_issue", "arguments.issue_key": "SECRET-VALUE",
            })
        assert "SECRET-VALUE" not in caplog.text

    async def test_a_well_formed_call_logs_no_warning(self, caplog):
        endpoint, sid, _ = _make_endpoint()
        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.server.mcp_endpoint"):
            await _call(endpoint, sid, {
                "tool": "jira__get_issue", "arguments": {"issue_key": "A-1"},
            })
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    async def test_skipped_paths_are_reported(self, caplog):
        endpoint, sid, _ = _make_endpoint()
        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.server.mcp_endpoint"):
            await _call(endpoint, sid, {
                "tool": "jira__get_issue",
                "arguments": {"issue_key": "A-1"},
                "arguments.issue_key": "FLAT",
            })
        message = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert "arguments.issue_key" in message
        assert "skipped" in message.lower()

    async def test_an_unresolvable_client_is_reported_as_unknown(self, caplog):
        endpoint, _, _ = _make_endpoint()
        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.server.mcp_endpoint"):
            await endpoint.handle_tools_call("no-such-session", {
                "name": "call_tool",
                "arguments": {"tool": "jira__get_issue", "arguments.issue_key": "A-1"},
            })
        message = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert "unknown" in message

    async def test_meta_tool_handlers_default_the_session_id(self):
        """Existing call sites pass no session id; they must keep working."""
        endpoint, _, router = _make_endpoint()
        result = await endpoint._handle_meta_tool(
            "call_tool", {"tool": "jira__get_issue", "arguments.issue_key": "A-1"},
        )
        assert not result.get("isError")
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {"issue_key": "A-1"})


class TestDotNotationReviewFindings:
    """Regressions for defects found by adversarial review of the repair."""

    async def test_mixed_bare_and_dotted_arguments_all_reach_the_backend(self):
        """A single hallucinated dotted key used to discard 277's bare siblings,
        producing a silent wrong call rather than an error."""
        endpoint, sid, router = _make_endpoint()
        await _call(endpoint, sid, {
            "tool": "jira__get_issue", "issue_key": "A-1", "arguments.fields": "summary",
        })
        router.route_tool_call.assert_awaited_once_with(
            "jira__get_issue", {"issue_key": "A-1", "fields": "summary"},
        )

    @pytest.mark.parametrize("bad_name", [None, 1234, ["a"]])
    async def test_a_non_string_client_name_does_not_fail_the_call(self, bad_name):
        """clientInfo.name is unvalidated, so a session can carry a non-string
        name. The repair path must not turn that into a -32603."""
        endpoint, _, router = _make_endpoint()
        sid = endpoint._session_manager.create_session(client_name=bad_name)
        result = await _call(endpoint, sid, {
            "tool": "jira__get_issue", "arguments.issue_key": "A-1",
        })
        assert not result.get("isError")
        router.route_tool_call.assert_awaited_once_with("jira__get_issue", {"issue_key": "A-1"})

    async def test_a_non_string_client_name_is_logged_as_unknown(self, caplog):
        endpoint, _, _ = _make_endpoint()
        unvalidated: Any = None
        sid = endpoint._session_manager.create_session(client_name=unvalidated)
        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.server.mcp_endpoint"):
            await _call(endpoint, sid, {"tool": "jira__get_issue", "arguments.issue_key": "A-1"})
        message = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert "unknown" in message

    async def test_a_blank_client_name_is_logged_as_unknown(self, caplog):
        endpoint, _, _ = _make_endpoint()
        sid = endpoint._session_manager.create_session(client_name="   ")
        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.server.mcp_endpoint"):
            await _call(endpoint, sid, {"tool": "jira__get_issue", "arguments.issue_key": "A-1"})
        message = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert "unknown" in message

    async def test_self_invocation_guard_still_fires_on_a_dotted_payload(self):
        endpoint, sid, router = _make_endpoint()
        result = await _call(endpoint, sid, {
            "tool": "hub__call_tool", "arguments.tool": "jira__get_issue",
        })
        assert result["isError"] is True
        assert "cannot invoke itself" in result["content"][0]["text"]
        router.route_tool_call.assert_not_awaited()
