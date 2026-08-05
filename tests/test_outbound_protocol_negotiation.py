"""P04 — OutboundClient stdio transport integration tests.

Proves the stdio↔stdio and stdio↔HTTP transport cells with REAL processes.
All tests use a real subprocess MCP server (fixture server via Python -c).
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.protocol.models import AuthorizationState, NegotiatedPeer, ProtocolEra
from slm_mcp_hub.protocol.outbound import OutboundClient

# ---------------------------------------------------------------------------
# Fixture server code — run as subprocess via sys.executable -c "..."
# ---------------------------------------------------------------------------

_FIXTURE_SERVER_CODE = """\
import asyncio, sys
sys.path.insert(0, {site_packages!r})
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

server = MCPServer('fixture-server')

@server.tool(description='Echo text back')
async def echo(text: str = '') -> list[TextContent]:
    return [TextContent(type='text', text=f'echo: {{text}}')]

@server.resource('test://fixture', description='Test resource')
async def fixture_resource() -> str:
    return 'fixture content'

@server.prompt(description='Test prompt')
async def test_prompt() -> str:
    return 'prompt content'

asyncio.run(server.run_stdio_async())
"""


def _server_code() -> str:
    """Build the fixture server code with the correct site-packages path."""
    # Find the venv site-packages so the server subprocess can import mcp
    venv_site = None
    for p in sys.path:
        if "site-packages" in p and ".venv" in p:
            venv_site = p
            break
    if venv_site is None:
        # Fallback: use the same sys.path entries
        venv_site = str([p for p in sys.path if p][-1])
    return _FIXTURE_SERVER_CODE.format(site_packages=venv_site)


def _stdio_config(**kw: Any) -> MCPServerConfig:
    """Config pointing at the fixture stdio server."""
    defaults = dict(
        name="fixture",
        transport="stdio",
        command=sys.executable,
        args=("-c", _server_code()),
    )
    defaults.update(kw)
    return MCPServerConfig(**defaults)


# A fixture server that writes a secret sentinel to its OWN stderr at startup
# before serving MCP. Used to prove the Hub never captures child stderr into
# structured logs / status / error surfaces (SDK routes it to the process
# stderr stream, not into Python logging).
_STDERR_LEAK_SERVER_CODE = """\
import sys
sys.stderr.write({secret!r} + "\\n")
sys.stderr.flush()
""" + _FIXTURE_SERVER_CODE


def _leaky_server_code(secret: str) -> str:
    """Fixture server code that emits *secret* on stderr before serving."""
    venv_site = None
    for p in sys.path:
        if "site-packages" in p and ".venv" in p:
            venv_site = p
            break
    if venv_site is None:
        venv_site = str([p for p in sys.path if p][-1])
    return _STDERR_LEAK_SERVER_CODE.format(site_packages=venv_site, secret=secret)


# ---------------------------------------------------------------------------
# Tests: OutboundClient – stdio transport cell (stdio↔stdio)
# ---------------------------------------------------------------------------

class TestOutboundClientStdioConnect:
    """OutboundClient connects to a real stdio subprocess MCP server."""

    @pytest.mark.asyncio
    async def test_stdio_connect_discovers_tools(self):
        """connect() discovers tools from real stdio server."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            assert len(client.capabilities["tools"]) == 1
            tool = client.capabilities["tools"][0]
            assert tool["name"] == "echo"
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_stdio_connect_discovers_resources(self):
        """connect() discovers resources from real stdio server."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            assert len(client.capabilities["resources"]) == 1
            assert client.capabilities["resources"][0]["uri"] == "test://fixture"
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_stdio_connect_discovers_prompts(self):
        """connect() discovers prompts from real stdio server."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            assert len(client.capabilities["prompts"]) == 1
            assert client.capabilities["prompts"][0]["name"] == "test_prompt"
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_stdio_call_tool_echo(self):
        """call_tool() calls the real echo tool and returns a dict result."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            result = await client.call_tool("echo", {"text": "hello-p04"})
            assert isinstance(result, dict)
            assert "content" in result
            texts = [c["text"] for c in result["content"] if c.get("type") == "text"]
            assert any("hello-p04" in t for t in texts)
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_stdio_read_resource(self):
        """read_resource() reads a real resource and returns dict."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            result = await client.read_resource("test://fixture")
            assert isinstance(result, dict)
            assert "contents" in result
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_stdio_get_prompt(self):
        """get_prompt() fetches a real prompt and returns dict."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            result = await client.get_prompt("test_prompt", {})
            assert isinstance(result, dict)
            assert "messages" in result
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_stdio_negotiated_peer_has_version(self):
        """connect() captures the negotiated protocol version."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            peer = client.negotiated_peer
            assert peer is not None
            assert isinstance(peer, NegotiatedPeer)
            assert peer.protocol_version  # non-empty string
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_stdio_modern_era_negotiated(self):
        """Fixture server negotiates MODERN_2026 era (protocol 2026-07-28)."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            peer = client.negotiated_peer
            assert peer is not None
            # Modern servers should produce MODERN_2026 era
            assert peer.era in (ProtocolEra.MODERN_2026, ProtocolEra.LEGACY)
        finally:
            await client.disconnect()


class TestOutboundClientAuthorizationState:
    """authorization_state is always 'none/not_required' in P04 (OAuth is P06)."""

    def test_authorization_state_before_connect(self):
        """authorization_state returns not_required even before connect."""
        client = OutboundClient(_stdio_config())
        state = client.authorization_state
        assert isinstance(state, AuthorizationState)
        assert state.mode == "none"
        assert state.status == "not_required"
        assert state.issuer is None
        assert state.resource is None
        assert state.scopes == ()

    @pytest.mark.asyncio
    async def test_authorization_state_after_connect(self):
        """authorization_state remains not_required after connect."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            state = client.authorization_state
            assert state.mode == "none"
            assert state.status == "not_required"
        finally:
            await client.disconnect()


class TestOutboundClientStdioEnvSecurity:
    """Child stdio processes receive only the restricted environment."""

    @pytest.mark.asyncio
    async def test_restricted_env_excludes_unrelated_secrets(self, monkeypatch):
        """Hub creds/unrelated env vars are NOT passed to child stdio process.

        OutboundClient builds StdioServerParameters with get_default_environment()
        as the base (restricted set), then adds only the server's explicit env.
        An env var set on the Hub process must NOT appear in the child's env.
        """
        monkeypatch.setenv("UNRELATED_HUB_CRED", "should-not-leak-sentinel")

        captured: dict[str, Any] = {}

        def fake_stdio_client(params, **kw):
            captured["env"] = dict(params.env) if params.env is not None else {}
            raise RuntimeError("test-stop")  # prevent actual subprocess

        with patch("slm_mcp_hub.protocol.outbound.stdio_client", fake_stdio_client):
            cfg = MCPServerConfig(name="test", transport="stdio", command="python")
            client = OutboundClient(cfg)
            with pytest.raises((RuntimeError, ConnectionError)):
                await client.connect()

        assert "UNRELATED_HUB_CRED" not in captured.get("env", {})

    @pytest.mark.asyncio
    async def test_explicit_server_env_is_passed(self, monkeypatch):
        """Server-specific env vars from MCPServerConfig.env reach the child."""
        monkeypatch.setenv("MY_API_KEY", "explicit-key-value")

        captured: dict[str, Any] = {}

        def fake_stdio_client(params, **kw):
            captured["env"] = dict(params.env) if params.env is not None else {}
            raise RuntimeError("test-stop")

        with patch("slm_mcp_hub.protocol.outbound.stdio_client", fake_stdio_client):
            cfg = MCPServerConfig(
                name="test",
                transport="stdio",
                command="python",
                env={"EXPLICIT_TOKEN": "${MY_API_KEY}"},
            )
            # materialize_server_config resolves env placeholders
            client = OutboundClient(cfg)
            with pytest.raises((RuntimeError, ConnectionError)):
                await client.connect()

        assert captured.get("env", {}).get("EXPLICIT_TOKEN") == "explicit-key-value"

    @pytest.mark.asyncio
    async def test_placeholder_expanded_in_command(self, monkeypatch):
        """${VAR} placeholders in command/args are expanded before StdioServerParameters."""
        monkeypatch.setenv("FIXTURE_CMD", "python")

        captured: dict[str, Any] = {}

        def fake_stdio_client(params, **kw):
            captured["command"] = params.command
            raise RuntimeError("test-stop")

        with patch("slm_mcp_hub.protocol.outbound.stdio_client", fake_stdio_client):
            cfg = MCPServerConfig(
                name="test",
                transport="stdio",
                command="${FIXTURE_CMD}",
            )
            client = OutboundClient(cfg)
            with pytest.raises((RuntimeError, ConnectionError)):
                await client.connect()

        assert captured.get("command") == "python"


class TestOutboundClientStdioErrors:
    """Error handling for stdlib transport failures."""

    @pytest.mark.asyncio
    async def test_command_not_found_raises_connection_error(self):
        """FileNotFoundError from subprocess → ConnectionError('Command not found')."""
        cfg = MCPServerConfig(
            name="missing",
            transport="stdio",
            command="/no/such/binary_xyz_p04",
        )
        client = OutboundClient(cfg)
        with pytest.raises(ConnectionError, match="Command not found"):
            await client.connect()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected_is_safe(self):
        """disconnect() before connect() does not raise."""
        client = OutboundClient(_stdio_config())
        await client.disconnect()  # should not raise

    @pytest.mark.asyncio
    async def test_call_tool_not_connected_raises(self):
        """call_tool() before connect() raises ConnectionError."""
        client = OutboundClient(_stdio_config())
        with pytest.raises(ConnectionError, match="[Nn]ot connected"):
            await client.call_tool("echo", {"text": "x"})

    @pytest.mark.asyncio
    async def test_read_resource_not_connected_raises(self):
        """read_resource() before connect() raises ConnectionError."""
        client = OutboundClient(_stdio_config())
        with pytest.raises(ConnectionError, match="[Nn]ot connected"):
            await client.read_resource("test://fixture")

    @pytest.mark.asyncio
    async def test_get_prompt_not_connected_raises(self):
        """get_prompt() before connect() raises ConnectionError."""
        client = OutboundClient(_stdio_config())
        with pytest.raises(ConnectionError, match="[Nn]ot connected"):
            await client.get_prompt("test_prompt", {})


class TestOutboundClientCapabilitiesGating:
    """Discovery only probes capabilities the server advertises."""

    @pytest.mark.asyncio
    async def test_capabilities_are_populated_after_connect(self):
        """capabilities dict has all four expected keys after connect."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            caps = client.capabilities
            assert set(caps.keys()) >= {"tools", "resources", "resource_templates", "prompts"}
        finally:
            await client.disconnect()


# ---------------------------------------------------------------------------
# Coverage gap tests — exercise error/edge branches not hit by happy-path tests
# ---------------------------------------------------------------------------

class TestOutboundClientInternals:
    """Unit tests for internal methods and error branches."""

    @pytest.mark.asyncio
    async def test_connect_already_connected_is_noop(self):
        """Second connect() call is a no-op (already connected guard, line 106)."""
        client = OutboundClient(_stdio_config())
        try:
            await client.connect()
            assert client._connected
            # Second call — should return immediately without spawning a new subprocess
            await client.connect()
            assert client._connected
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_build_client_exception_becomes_connection_error(self):
        """Any exception from _build_client → ConnectionError('initialization failed')."""
        client = OutboundClient(_stdio_config())
        with patch.object(
            client, "_build_client",
            side_effect=RuntimeError("internal build error"),
        ):
            with pytest.raises(ConnectionError, match="initialization failed"):
                await client.connect()

    @pytest.mark.asyncio
    async def test_build_client_file_not_found_becomes_command_not_found_error(self):
        """FileNotFoundError from _build_client → ConnectionError('Command not found')."""
        client = OutboundClient(_stdio_config())
        with patch.object(
            client, "_build_client",
            side_effect=FileNotFoundError("no such file"),
        ):
            with pytest.raises(ConnectionError, match="Command not found"):
                await client.connect()

    @pytest.mark.asyncio
    async def test_enter_async_context_os_error_becomes_initialization_failed(self):
        """Non-FileNotFoundError OSError during transport entry → 'initialization failed'."""
        from unittest.mock import AsyncMock, MagicMock

        client = OutboundClient(_stdio_config())
        # Provide a fake client object so _build_client succeeds
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(side_effect=OSError("permission denied"))
        fake_client.__aexit__ = AsyncMock(return_value=False)
        with patch.object(client, "_build_client", return_value=fake_client):
            with pytest.raises(ConnectionError, match="initialization failed"):
                await client.connect()

    @pytest.mark.asyncio
    async def test_discover_capabilities_exception_swallowed(self):
        """Discovery failure does not abort a successfully opened connection."""
        client = OutboundClient(_stdio_config())
        with patch.object(
            OutboundClient,
            "_discover_capabilities",
            AsyncMock(side_effect=RuntimeError("discovery boom")),
        ):
            try:
                await client.connect()
                # Connected despite discovery failure
                assert client._connected
            finally:
                await client.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_stack_close_error_is_swallowed(self):
        """Exception during _stack.aclose() in disconnect() is logged, not raised."""
        client = OutboundClient(_stdio_config())
        await client.connect()
        # Inject a stack whose aclose() raises
        original_stack = client._stack
        mock_stack = AsyncMock()
        mock_stack.aclose = AsyncMock(side_effect=RuntimeError("aclose failed"))
        client._stack = mock_stack
        # disconnect() should not raise
        await client.disconnect()
        # Clean up the real stack manually
        if original_stack:
            await original_stack.aclose()

    @pytest.mark.asyncio
    async def test_list_tools_failure_swallowed_returns_empty(self):
        """list_tools() failure is logged; tools stays empty list."""
        from unittest.mock import AsyncMock

        client = OutboundClient(_stdio_config())
        await client.connect()
        try:
            # Patch the already-connected client's list_tools
            client._client.list_tools = AsyncMock(  # type: ignore[union-attr]
                side_effect=RuntimeError("tools boom")
            )
            client._capabilities["tools"] = []
            await client._discover_capabilities()
            assert client._capabilities["tools"] == []
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_list_resources_failure_swallowed(self):
        """list_resources() failure is logged; resources stays empty."""
        from unittest.mock import AsyncMock

        client = OutboundClient(_stdio_config())
        await client.connect()
        try:
            client._client.list_resources = AsyncMock(  # type: ignore[union-attr]
                side_effect=RuntimeError("resources boom")
            )
            client._capabilities["resources"] = []
            await client._discover_capabilities()
            # resources should stay empty on failure (no raise)
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_list_resource_templates_failure_swallowed(self):
        """list_resource_templates() failure is logged; resource_templates stays empty."""
        from unittest.mock import AsyncMock

        client = OutboundClient(_stdio_config())
        await client.connect()
        try:
            client._client.list_resource_templates = AsyncMock(  # type: ignore[union-attr]
                side_effect=RuntimeError("templates boom")
            )
            client._capabilities["resource_templates"] = []
            await client._discover_capabilities()
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_list_prompts_failure_swallowed(self):
        """list_prompts() failure is logged; prompts stays empty."""
        from unittest.mock import AsyncMock

        client = OutboundClient(_stdio_config())
        await client.connect()
        try:
            client._client.list_prompts = AsyncMock(  # type: ignore[union-attr]
                side_effect=RuntimeError("prompts boom")
            )
            client._capabilities["prompts"] = []
            await client._discover_capabilities()
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_capture_negotiated_peer_preserves_version_when_caps_dump_fails(self):
        """A transient capabilities-serialization failure must NOT fabricate an
        "unknown"/legacy version. The authoritative negotiated protocol version
        is preserved; only the (non-critical) capabilities degrade to {}. This
        guards against advertising the wrong protocol era to downstream clients
        for a perfectly good modern peer."""
        from unittest.mock import MagicMock, PropertyMock

        client = OutboundClient(_stdio_config())
        await client.connect()
        try:
            real_version = client._client.protocol_version
            bad_caps = MagicMock()
            bad_caps.model_dump.side_effect = RuntimeError("caps dump failed")
            with patch.object(
                type(client._client),
                "server_capabilities",
                new_callable=PropertyMock,
                return_value=bad_caps,
            ):
                peer = client._capture_negotiated_peer()
            assert peer.protocol_version == real_version  # preserved, never "unknown"
            assert peer.capabilities == {}
            expected_era = (
                ProtocolEra.MODERN_2026
                if real_version == "2026-07-28"
                else ProtocolEra.LEGACY
            )
            assert peer.era == expected_era
        finally:
            await client.disconnect()

    def test_serialize_call_tool_result_is_error_true(self):
        """_serialize_call_tool_result includes isError key when result.is_error is True."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.protocol.outbound import _serialize_call_tool_result

        block = MagicMock()
        block.model_dump.return_value = {"type": "text", "text": "oops"}

        mock_result = MagicMock()
        mock_result.content = [block]
        mock_result.is_error = True

        out = _serialize_call_tool_result(mock_result)
        assert out["isError"] is True
        assert out["content"][0]["text"] == "oops"

    def test_serialize_call_tool_result_no_is_error_key_when_false(self):
        """_serialize_call_tool_result omits isError key when result.is_error is False."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.protocol.outbound import _serialize_call_tool_result

        block = MagicMock()
        block.model_dump.return_value = {"type": "text", "text": "ok"}

        mock_result = MagicMock()
        mock_result.content = [block]
        mock_result.is_error = False

        out = _serialize_call_tool_result(mock_result)
        assert "isError" not in out

    @pytest.mark.asyncio
    async def test_discover_skips_resources_when_not_advertised(self):
        """_discover_capabilities skips resource discovery when server omits 'resources' cap."""
        from unittest.mock import MagicMock, patch

        client = OutboundClient(_stdio_config())
        await client.connect()
        try:
            # Patch server_capabilities to omit "resources" and "prompts"
            mock_caps = MagicMock()
            mock_caps.model_dump.return_value = {"tools": {}}
            with patch.object(
                type(client._client), "server_capabilities",
                new_callable=lambda: property(lambda s: mock_caps),
            ):
                await client._discover_capabilities()
            # Resources and prompts should not have been populated
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_discover_skips_prompts_when_not_advertised(self):
        """_discover_capabilities skips prompt discovery when server omits 'prompts' cap."""
        from unittest.mock import MagicMock, patch

        client = OutboundClient(_stdio_config())
        await client.connect()
        try:
            # Patch server_capabilities to advertise resources but NOT prompts
            mock_caps = MagicMock()
            mock_caps.model_dump.return_value = {"tools": {}, "resources": {}}
            with patch.object(
                type(client._client), "server_capabilities",
                new_callable=lambda: property(lambda s: mock_caps),
            ):
                await client._discover_capabilities()
            # Prompts branch (line 354) should have been skipped
        finally:
            await client.disconnect()


class TestStdioStderrSecurity:
    """Regression: a child stdio server's stderr may carry
    secrets. It must NEVER surface in the Hub's structured logs, status, or
    error messages. This replaces the deleted hand-rolled
    ``_exit_diagnostic``/``_drain_stderr`` sanitization tests: under the SDK
    transport, child stderr is routed to the process stderr stream, never into
    Python logging or Hub status/error surfaces."""

    @pytest.mark.asyncio
    async def test_child_stderr_secret_never_reaches_hub_logs_or_status(
        self, caplog: Any
    ) -> None:
        import logging

        secret = "SEKRET-STDERR-a1b2c3d4-DO-NOT-LOG"
        cfg = MCPServerConfig(
            name="leaky",
            transport="stdio",
            command=sys.executable,
            args=("-c", _leaky_server_code(secret)),
        )
        client = OutboundClient(cfg)
        with caplog.at_level(logging.DEBUG):
            await client.connect()
            try:
                peer = client.negotiated_peer
                caps = client.capabilities
                # A tool call also exercises the SDK path end-to-end.
                await client.call_tool("echo", {"text": "hi"})
            finally:
                await client.disconnect()

        # The secret the child wrote to its own stderr must not be captured by
        # the Hub into Python logging or any structured surface it exposes.
        assert secret not in caplog.text
        assert secret not in repr(peer)
        assert secret not in repr(caps)
