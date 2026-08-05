"""P01 — MCP SDK v2 contract spike.

Empirically pins the ``mcp==2.0.0`` public API surface that the v0.3.0 protocol
and OAuth migration builds on. These are regression guards: if the SDK API
drifts under us, the migration's assumptions break here first, loudly, instead
of silently at runtime.

Every symbol asserted below was confirmed present by direct wheel introspection
on 2026-08-03 (see docs/release-evidence/v0.3.0/00-sol-plan-validation.md).
"""

from __future__ import annotations

import importlib
import inspect
from importlib.metadata import version

import pytest


# --------------------------------------------------------------------------- #
# Installed dependency versions
# --------------------------------------------------------------------------- #
def test_mcp_sdk_pinned_at_2_0_0() -> None:
    assert version("mcp") == "2.0.0"
    assert version("mcp-types") == "2.0.0"


def test_keyring_and_filelock_available_at_required_floors() -> None:
    keyring_major, keyring_minor = (int(p) for p in version("keyring").split(".")[:2])
    filelock_major, filelock_minor = (int(p) for p in version("filelock").split(".")[:2])
    assert (keyring_major, keyring_minor) >= (25, 7)
    assert keyring_major < 26
    assert (filelock_major, filelock_minor) >= (3, 32)
    assert filelock_major < 4


def test_httpx_and_httpx2_are_distinct_modules() -> None:
    """The Hub keeps httpx (its own code); the SDK brings httpx2. This proves the
    two libraries are distinct importable modules. Source-level isolation (no v2
    object reaching v1 code) is enforced separately below."""
    import httpx
    import httpx2

    assert httpx.__name__ == "httpx"
    assert httpx2.__name__ == "httpx2"
    assert httpx is not httpx2


def test_httpx2_is_confined_to_the_sdk_adapter_layer() -> None:
    """P01 isolation guard implementing the plan's 'repository search proves
    httpx and httpx2 objects are isolated' requirement, as an enduring
    Anti-Corruption-Layer invariant: the SDK's HTTP library (httpx2) may appear
    ONLY inside the protocol/ and auth/ adapter packages, never in routing,
    intelligence, storage, plugins, or any other Hub layer. At P01 the adapter
    packages do not exist yet, so the allowed set is empty."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "slm_mcp_hub"
    allowed_prefixes = ("protocol/", "auth/")
    offenders = sorted(
        rel
        for path in src.rglob("*.py")
        if "httpx2" in path.read_text(encoding="utf-8")
        for rel in (str(path.relative_to(src)).replace("\\", "/"),)
        if not rel.startswith(allowed_prefixes)
    )
    assert offenders == [], f"httpx2 leaked outside the SDK adapter layer: {offenders}"


# --------------------------------------------------------------------------- #
# Outbound client contract
# --------------------------------------------------------------------------- #
def test_client_supports_auto_negotiation_mode() -> None:
    from mcp.client import Client

    params = inspect.signature(Client.__init__).parameters
    assert "mode" in params
    assert params["mode"].default == "auto"


def test_stdio_transport_symbols_and_restricted_env() -> None:
    from mcp.client.stdio import (
        StdioServerParameters,
        get_default_environment,
        stdio_client,
    )

    assert callable(stdio_client)
    stdio_params = inspect.signature(StdioServerParameters).parameters
    assert "command" in stdio_params
    assert "env" in stdio_params
    # The SDK-provided restricted inherited environment is a plain dict; the Hub
    # relies on it so child stdio servers do not inherit Hub credentials.
    assert isinstance(get_default_environment(), dict)


def test_streamable_http_client_symbol_present() -> None:
    from mcp.client.streamable_http import (
        StreamableHTTPTransport,
        streamable_http_client,
    )

    assert callable(streamable_http_client)
    assert inspect.isclass(StreamableHTTPTransport)


# --------------------------------------------------------------------------- #
# OAuth client contract
# --------------------------------------------------------------------------- #
def test_oauth_client_provider_contract() -> None:
    from mcp.client.auth import OAuthClientProvider, PKCEParameters, TokenStorage

    params = inspect.signature(OAuthClientProvider).parameters
    for required in (
        "server_url",
        "client_metadata",
        "storage",
        "redirect_handler",
        "callback_handler",
        "client_metadata_url",  # Client ID Metadata Document support (CIMD)
        "validate_resource_url",
    ):
        assert required in params, f"OAuthClientProvider missing {required!r}"
    assert inspect.isclass(TokenStorage)
    assert inspect.isclass(PKCEParameters)


# --------------------------------------------------------------------------- #
# Inbound low-level server contract
# --------------------------------------------------------------------------- #
def test_lowlevel_server_uses_dynamic_constructor_handlers() -> None:
    from mcp.server.lowlevel import Server

    params = inspect.signature(Server).parameters
    for handler in (
        "on_list_tools",
        "on_call_tool",
        "on_list_resources",
        "on_read_resource",
        "on_list_prompts",
        "on_get_prompt",
        "on_subscriptions_listen",
        "cache_hints",
    ):
        assert handler in params, f"lowlevel.Server missing {handler!r}"
    # Dynamic mounting into the existing ASGI app.
    assert hasattr(Server, "streamable_http_app")


def test_transport_security_module_present() -> None:
    module = importlib.import_module("mcp.server.transport_security")
    assert module.__name__ == "mcp.server.transport_security"


# --------------------------------------------------------------------------- #
# Modern result / negotiation types
# --------------------------------------------------------------------------- #
def test_modern_2026_result_types_present() -> None:
    import mcp.types as sdk_types

    for name in (
        "CacheableResult",
        "InputRequiredResult",
        "DiscoverRequest",
        "DiscoverResult",
        "SubscriptionsListenRequest",
        "SubscriptionsListenResult",
        "ResultType",
        "ListToolsResult",
        "Tool",
    ):
        assert hasattr(sdk_types, name), f"mcp.types missing {name!r}"


# --------------------------------------------------------------------------- #
# Integration proof: in-memory auto-negotiation reaches the modern era
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_inmemory_client_autonegotiates_modern_2026() -> None:
    """A low-level dynamic Server connected to an auto-mode Client negotiates
    the modern 2026-07-28 protocol without a hard-coded handshake. This is the
    load-bearing proof for the whole inbound/outbound SDK adapter design."""
    import mcp.types as sdk_types
    from mcp.client import Client
    from mcp.server.lowlevel import Server

    async def on_list_tools(_ctx, _params):
        return sdk_types.ListToolsResult(
            tools=[sdk_types.Tool(name="ping", inputSchema={"type": "object"})]
        )

    server = Server("contract-hub", version="0.3.0", on_list_tools=on_list_tools)
    async with Client(server, mode="auto") as client:
        assert client.protocol_version == "2026-07-28"
        result = await client.list_tools()
        assert [tool.name for tool in result.tools] == ["ping"]
