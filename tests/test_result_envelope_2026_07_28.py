"""MCP 2026-07-28 result envelope: ``resultType`` / ``ttlMs`` / ``cacheScope``.

Regression for: Claude Code 2.1.292 reported the hub as
"Connected · tools fetch failed — Invalid result for tools/list: missing
required resultType". The 2026-07-28 schema makes ``resultType`` required on
every result, and ``ttlMs`` + ``cacheScope`` required on the cacheable ones
(``server/discover``, the list methods, ``resources/read``). Legacy clients
(``initialize`` handshake, revisions <= 2025-11-25) must keep getting the old
shape, where an absent ``resultType`` means "complete".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import mcp.types as t
import pytest
from fastapi.testclient import TestClient

from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.router import FederationRouter, RouteResult
from slm_mcp_hub.protocol.conversion import is_modern_request, to_modern_result
from slm_mcp_hub.server.http_server import create_app
from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
from slm_mcp_hub.session.manager import SessionManager

MODERN = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
MODERN_META = {
    META_VERSION: MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "probe", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}

CACHEABLE = {
    "server/discover",
    "tools/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "prompts/list",
}
# method -> params (without _meta)
REQUESTS: dict[str, dict[str, Any]] = {
    "server/discover": {},
    "tools/list": {},
    "tools/call": {"name": "list_servers", "arguments": {}},
    "resources/list": {},
    "resources/templates/list": {},
    "resources/read": {"uri": "jira__file:///a"},
    "prompts/list": {},
    "prompts/get": {"name": "jira__p", "arguments": {}},
}
LEGACY_REQUESTS = {m: p for m, p in REQUESTS.items() if m != "server/discover"}


def _rr(result: dict[str, Any]) -> RouteResult:
    return RouteResult(
        result=result, server_name="jira", tool_name="x", duration_ms=1, success=True
    )


def _endpoint() -> MCPEndpoint:
    registry = CapabilityRegistry()
    registry.sync({
        "jira": {
            "tools": [{"name": "get_issue", "description": "d", "inputSchema": {}}],
            "resources": [{"uri": "file:///a", "name": "a"}],
            "resource_templates": [{"uriTemplate": "file:///{p}", "name": "t"}],
            "prompts": [{"name": "p", "description": "d"}],
        },
    })
    router = AsyncMock(spec=FederationRouter)
    router.route_tool_call = AsyncMock(return_value=_rr({"content": [{"type": "text", "text": "ok"}]}))
    router.route_resource_read = AsyncMock(return_value=_rr(
        {"contents": [{"uri": "file:///a", "text": "hi", "mimeType": "text/plain"}]}
    ))
    router.route_prompt_get = AsyncMock(return_value=_rr(
        {"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]}
    ))
    return MCPEndpoint(registry, router, SessionManager())


async def _call(ep: MCPEndpoint, method: str, params: dict[str, Any], *, meta: dict[str, Any] | None) -> dict[str, Any]:
    body_params = dict(params)
    if meta is not None:
        body_params["_meta"] = meta
    reply = await ep.handle_jsonrpc("s1", {"jsonrpc": "2.0", "id": 1, "method": method, "params": body_params})
    assert reply is not None and "result" in reply, reply
    return reply["result"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestIsModernRequest:
    def test_modern_meta_is_modern(self):
        assert is_modern_request({"method": "tools/list", "params": {"_meta": MODERN_META}})

    def test_discover_is_always_modern(self):
        assert is_modern_request({"method": "server/discover"})
        assert is_modern_request({"method": "server/discover", "params": {}})

    @pytest.mark.parametrize("message", [
        {"method": "tools/list"},
        {"method": "tools/list", "params": {}},
        {"method": "tools/list", "params": None},
        {"method": "tools/list", "params": "bad"},
        {"method": "tools/list", "params": {"_meta": "bad"}},
        {"method": "tools/list", "params": {"_meta": {}}},
        {"method": "tools/list", "params": {"_meta": {META_VERSION: "2025-11-25"}}},
        {"method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
    ])
    def test_everything_else_is_legacy(self, message):
        assert not is_modern_request(message)


class TestToModernResult:
    def test_adds_result_type_only_for_non_cacheable(self):
        out = to_modern_result("tools/call", {"content": []})
        assert out == {"content": [], "resultType": "complete"}

    @pytest.mark.parametrize("method", sorted(CACHEABLE))
    def test_cacheable_methods_get_cache_fields(self, method):
        out = to_modern_result(method, {})
        assert out["resultType"] == "complete"
        assert out["ttlMs"] == 0
        assert out["cacheScope"] == "private"

    def test_does_not_mutate_input(self):
        src = {"tools": []}
        to_modern_result("tools/list", src)
        assert src == {"tools": []}

    def test_keeps_values_a_modern_backend_already_set(self):
        src = {"resultType": "input_required", "ttlMs": 5000, "cacheScope": "public"}
        assert to_modern_result("tools/list", src) == src


# ---------------------------------------------------------------------------
# Endpoint dispatch (shared by HTTP and stdio transports)
# ---------------------------------------------------------------------------

class TestModernRequests:
    @pytest.mark.parametrize("method", sorted(REQUESTS))
    async def test_every_result_carries_result_type(self, method):
        result = await _call(_endpoint(), method, REQUESTS[method], meta=MODERN_META)
        assert result["resultType"] == "complete"

    @pytest.mark.parametrize("method", sorted(REQUESTS))
    async def test_cache_fields_exactly_on_cacheable_results(self, method):
        result = await _call(_endpoint(), method, REQUESTS[method], meta=MODERN_META)
        if method in CACHEABLE:
            assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
            assert result["cacheScope"] in {"public", "private"}
        else:
            assert "ttlMs" not in result and "cacheScope" not in result

    async def test_payload_survives_the_envelope(self):
        ep = _endpoint()
        tools = (await _call(ep, "tools/list", {}, meta=MODERN_META))["tools"]
        assert {tool["name"] for tool in tools} == {"search_tools", "call_tool", "list_servers"}
        read = await _call(ep, "resources/read", REQUESTS["resources/read"], meta=MODERN_META)
        assert read["contents"][0]["text"] == "hi"

    async def test_discover_without_meta_is_still_modern(self):
        result = await _call(_endpoint(), "server/discover", {}, meta=None)
        assert result["resultType"] == "complete"
        assert MODERN in result["supportedVersions"]


class TestLegacyRequestsAreUnchanged:
    @pytest.mark.parametrize("method", sorted(LEGACY_REQUESTS))
    @pytest.mark.parametrize("meta", [None, {META_VERSION: "2025-11-25"}], ids=["no-meta", "old-meta"])
    async def test_no_modern_fields(self, method, meta):
        result = await _call(_endpoint(), method, LEGACY_REQUESTS[method], meta=meta)
        assert not {"resultType", "ttlMs", "cacheScope"} & result.keys()

    async def test_initialize_keeps_legacy_shape(self):
        result = await _call(
            _endpoint(), "initialize",
            {"protocolVersion": "2025-11-25", "clientInfo": {"name": "old"}}, meta=None,
        )
        assert "resultType" not in result and "protocolVersion" in result

    async def test_modern_error_responses_are_untouched(self):
        reply = await _endpoint().handle_jsonrpc("s1", {
            "jsonrpc": "2.0", "id": 9, "method": "nope/nope", "params": {"_meta": MODERN_META},
        })
        assert reply is not None and reply["error"]["code"] == -32601 and "result" not in reply


# ---------------------------------------------------------------------------
# SDK parity: the wire shape must be a superset of what the official SDK emits
# ---------------------------------------------------------------------------

class TestParityWithOfficialSdk:
    async def test_list_tools_keys_match_sdk_model(self):
        sdk_keys = set(t.ListToolsResult(tools=[]).model_dump(by_alias=True, exclude_none=True))
        ours = set(await _call(_endpoint(), "tools/list", {}, meta=MODERN_META))
        assert {"resultType", "ttlMs", "cacheScope"} <= sdk_keys, "SDK stopped emitting the envelope?"
        assert sdk_keys <= ours

    async def test_modern_results_round_trip_through_sdk_models(self):
        ep = _endpoint()
        t.ListToolsResult.model_validate(await _call(ep, "tools/list", {}, meta=MODERN_META))
        t.ListPromptsResult.model_validate(await _call(ep, "prompts/list", {}, meta=MODERN_META))
        t.ListResourcesResult.model_validate(await _call(ep, "resources/list", {}, meta=MODERN_META))


# ---------------------------------------------------------------------------
# HTTP transport (the exact path Claude Code's probe takes)
# ---------------------------------------------------------------------------

def _http_client() -> TestClient:
    ep = _endpoint()
    return TestClient(create_app(mcp_endpoint=ep, session_manager=ep._session_manager))


def _modern_post(client: TestClient, method: str, params: dict[str, Any]):
    return client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": MODERN, "Mcp-Method": method},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": MODERN_META}},
    )


class TestHttpTransport:
    def test_modern_tools_list_has_result_type(self):
        response = _modern_post(_http_client(), "tools/list", {})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == 0 and result["cacheScope"] == "private"
        assert len(result["tools"]) == 3

    def test_modern_discover_has_result_type(self):
        result = _modern_post(_http_client(), "server/discover", {}).json()["result"]
        assert result["resultType"] == "complete"

    def test_legacy_session_flow_is_unchanged(self):
        client = _http_client()
        init = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "old"}},
        })
        assert "resultType" not in init.json()["result"]
        sid = init.headers["Mcp-Session-Id"]
        listed = client.post(
            "/mcp", headers={"Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ).json()["result"]
        assert not {"resultType", "ttlMs", "cacheScope"} & listed.keys()
        assert len(listed["tools"]) == 3
