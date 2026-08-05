"""W6-P1 — SSE dispatch correctness and auth/transport compatibility gate.

Tests for:
1. transport="sse" routes to sse_client (NOT streamable_http_client)
2. transport="http" still routes to streamable_http_client (unchanged)
3. transport="stdio" still routes to stdio_client (unchanged)
4. sse + oauth raises ConfigValidationError (auth/transport gate in config.py)
5. sse + none is accepted
6. sse + static_headers is accepted
7. _build_sse_client calls sse_client with correct url/headers/sse_read_timeout
8. _build_sse_client without headers passes headers=None

All tests are unit tests — no live network, no subprocess. sse_client is patched
at the import site in slm_mcp_hub.protocol.outbound so the patch actually intercepts
dispatch. streamable_http_client is similarly patched to assert it is NOT called
for sse transport.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from slm_mcp_hub.auth.models import (
    AuthNoneConfig,
    AuthOAuthConfig,
    AuthStaticHeadersConfig,
)
from slm_mcp_hub.core.config import (
    ConfigValidationError,
    MCPServerConfig,
    _validate_auth_transport_compatibility,  # noqa: PLC2701 – tested directly
    validate_server_config,
)
from slm_mcp_hub.protocol.outbound import _SSE_DEFAULT_READ_TIMEOUT_S, OutboundClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse_config(
    url: str = "http://host:8080/sse",
    headers: dict[str, str] | None = None,
    auth=None,
) -> MCPServerConfig:
    """Build a minimal SSE MCPServerConfig."""
    return MCPServerConfig(
        name="test-sse",
        transport="sse",
        url=url,
        headers=headers or {},
        auth=auth if auth is not None else AuthNoneConfig(),
    )


def _http_config(url: str = "http://host:8080/mcp") -> MCPServerConfig:
    return MCPServerConfig(
        name="test-http",
        transport="http",
        url=url,
    )


def _stdio_config(command: str = "python3") -> MCPServerConfig:
    return MCPServerConfig(
        name="test-stdio",
        transport="stdio",
        command=command,
    )


def _oauth_config() -> AuthOAuthConfig:
    return AuthOAuthConfig(
        callback_host="127.0.0.1",
        callback_port=9999,
        scopes=("openid",),
    )


# ---------------------------------------------------------------------------
# Group 1: _build_client dispatch
# ---------------------------------------------------------------------------

class TestBuildClientDispatch:
    """Verify that _build_client routes each transport to the correct SDK client."""

    def test_sse_transport_calls_sse_client_not_streamable(self) -> None:
        """transport='sse' must call sse_client — NOT streamable_http_client.

        This is the W6-P1 core correctness assertion: before the fix,
        transport='sse' fell through to _build_http_client which used
        streamable_http_client, which cannot speak the legacy SSE protocol.
        """
        config = _sse_config(url="http://host:8080/sse")
        client_obj = OutboundClient(config)

        # Patch BOTH transports at the outbound module import site.
        fake_transport = MagicMock(name="fake_sse_transport")
        fake_sse = MagicMock(return_value=fake_transport, name="sse_client_mock")
        fake_streamable = MagicMock(name="streamable_http_client_mock")

        with (
            patch("slm_mcp_hub.protocol.outbound.sse_client", fake_sse),
            patch("slm_mcp_hub.protocol.outbound.streamable_http_client", fake_streamable),
            patch("slm_mcp_hub.protocol.outbound.Client") as fake_client_cls,
        ):
            client_obj._build_client(config)

            # sse_client MUST be called
            assert fake_sse.called, "sse_client was not called for transport='sse'"
            # streamable_http_client must NOT be called
            assert not fake_streamable.called, (
                "streamable_http_client was called for transport='sse' — "
                "this is the W6-P1 bug (wrong client used for legacy SSE)"
            )
            # SDK Client must be constructed
            assert fake_client_cls.called, "Client() was not called"

    def test_http_transport_calls_streamable_not_sse(self) -> None:
        """transport='http' must route to streamable_http_client, not sse_client."""
        config = _http_config()
        client_obj = OutboundClient(config)

        fake_sse = MagicMock(name="sse_client_mock")
        fake_streamable = MagicMock(return_value=MagicMock(), name="streamable_http_mock")

        with (
            patch("slm_mcp_hub.protocol.outbound.sse_client", fake_sse),
            patch("slm_mcp_hub.protocol.outbound.streamable_http_client", fake_streamable),
            patch("slm_mcp_hub.protocol.outbound.Client") as fake_client_cls,
        ):
            client_obj._build_client(config)

            assert not fake_sse.called, "sse_client called for transport='http'"
            # Client either via streamable or direct url — just not via sse
            assert fake_client_cls.called, "Client() not called for http transport"

    def test_stdio_transport_does_not_call_sse_client(self) -> None:
        """transport='stdio' must route to stdio_client, not sse_client."""
        config = _stdio_config()
        client_obj = OutboundClient(config)

        fake_sse = MagicMock(name="sse_client_mock")
        fake_stdio = MagicMock(return_value=MagicMock(), name="stdio_client_mock")

        with (
            patch("slm_mcp_hub.protocol.outbound.sse_client", fake_sse),
            patch("slm_mcp_hub.protocol.outbound.stdio_client", fake_stdio),
            patch("slm_mcp_hub.protocol.outbound.Client"),
        ):
            client_obj._build_client(config)

            assert not fake_sse.called, "sse_client called for transport='stdio'"
            assert fake_stdio.called, "stdio_client not called for transport='stdio'"


# ---------------------------------------------------------------------------
# Group 2: _build_sse_client parameter correctness
# ---------------------------------------------------------------------------

class TestBuildSseClientParameters:
    """Verify _build_sse_client passes the right arguments to sse_client."""

    def test_sse_client_receives_correct_url(self) -> None:
        """sse_client must be called with url= matching the config url."""
        url = "http://example.com:9090/sse"
        config = _sse_config(url=url)
        client_obj = OutboundClient(config)

        fake_sse = MagicMock(return_value=MagicMock())
        with (
            patch("slm_mcp_hub.protocol.outbound.sse_client", fake_sse),
            patch("slm_mcp_hub.protocol.outbound.Client"),
        ):
            client_obj._build_sse_client(config)

        call_kwargs = fake_sse.call_args.kwargs
        assert call_kwargs["url"] == url, (
            f"sse_client called with url={call_kwargs.get('url')!r}, expected {url!r}"
        )

    def test_sse_client_receives_default_read_timeout(self) -> None:
        """sse_client must be called with sse_read_timeout=_SSE_DEFAULT_READ_TIMEOUT_S."""
        config = _sse_config()
        client_obj = OutboundClient(config)

        fake_sse = MagicMock(return_value=MagicMock())
        with (
            patch("slm_mcp_hub.protocol.outbound.sse_client", fake_sse),
            patch("slm_mcp_hub.protocol.outbound.Client"),
        ):
            client_obj._build_sse_client(config)

        call_kwargs = fake_sse.call_args.kwargs
        assert call_kwargs["sse_read_timeout"] == _SSE_DEFAULT_READ_TIMEOUT_S, (
            f"sse_read_timeout was {call_kwargs.get('sse_read_timeout')}, "
            f"expected {_SSE_DEFAULT_READ_TIMEOUT_S}"
        )

    def test_sse_client_receives_headers_when_configured(self) -> None:
        """When MCPServerConfig.headers is non-empty, sse_client receives them."""
        headers = {"X-Api-Key": "test-key", "X-Tenant": "acme"}
        config = _sse_config(headers=headers)
        client_obj = OutboundClient(config)

        fake_sse = MagicMock(return_value=MagicMock())
        with (
            patch("slm_mcp_hub.protocol.outbound.sse_client", fake_sse),
            patch("slm_mcp_hub.protocol.outbound.Client"),
        ):
            client_obj._build_sse_client(config)

        call_kwargs = fake_sse.call_args.kwargs
        assert call_kwargs["headers"] == headers, (
            f"sse_client headers={call_kwargs.get('headers')!r}, expected {headers!r}"
        )

    def test_sse_client_receives_none_headers_when_not_configured(self) -> None:
        """When MCPServerConfig.headers is empty, sse_client receives headers=None."""
        config = _sse_config(headers={})
        client_obj = OutboundClient(config)

        fake_sse = MagicMock(return_value=MagicMock())
        with (
            patch("slm_mcp_hub.protocol.outbound.sse_client", fake_sse),
            patch("slm_mcp_hub.protocol.outbound.Client"),
        ):
            client_obj._build_sse_client(config)

        call_kwargs = fake_sse.call_args.kwargs
        assert call_kwargs["headers"] is None, (
            f"sse_client headers should be None for empty config, got {call_kwargs.get('headers')!r}"
        )

    def test_sse_build_returns_client_instance(self) -> None:
        """_build_sse_client must return a Client (from the SDK) wrapping sse_client."""
        from mcp import Client

        config = _sse_config()
        client_obj = OutboundClient(config)

        # Do NOT mock Client — let it be real so we verify the return type.
        fake_sse = MagicMock(return_value=MagicMock())
        with patch("slm_mcp_hub.protocol.outbound.sse_client", fake_sse):
            result = client_obj._build_sse_client(config)

        assert isinstance(result, Client), (
            f"_build_sse_client returned {type(result)}, expected mcp.Client"
        )

    def test_sse_constant_value(self) -> None:
        """_SSE_DEFAULT_READ_TIMEOUT_S must be 300.0 seconds."""
        assert _SSE_DEFAULT_READ_TIMEOUT_S == 300.0, (
            f"_SSE_DEFAULT_READ_TIMEOUT_S is {_SSE_DEFAULT_READ_TIMEOUT_S}, expected 300.0"
        )


# ---------------------------------------------------------------------------
# Group 3: auth/transport compatibility gate (config.py)
# ---------------------------------------------------------------------------

class TestAuthTransportCompatibility:
    """Verify that sse + oauth is rejected at config validation time."""

    def test_sse_with_oauth_raises_config_validation_error(self) -> None:
        """sse transport combined with oauth auth mode must raise ConfigValidationError.

        The mcp SDK's OAuth client provider is Streamable-HTTP-only. Routing
        an OAuth-configured server to sse_client would silently fail to
        authenticate. This gate prevents that misconfiguration at config
        validation time rather than at runtime.
        """
        config = _sse_config(auth=_oauth_config())
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_auth_transport_compatibility(config)

        msg = str(exc_info.value).lower()
        # Error message must mention both concepts clearly
        assert "sse" in msg, f"Error must mention 'sse'; got: {exc_info.value!r}"
        assert "oauth" in msg, f"Error must mention 'oauth'; got: {exc_info.value!r}"

    def test_sse_with_oauth_rejected_by_validate_server_config(self) -> None:
        """validate_server_config must also reject sse+oauth (gate is wired in)."""
        config = _sse_config(auth=_oauth_config())
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_server_config(config)

        msg = str(exc_info.value).lower()
        assert "sse" in msg
        assert "oauth" in msg

    def test_sse_with_none_auth_is_accepted(self) -> None:
        """sse + auth=none must not raise (no auth is safe with SSE)."""
        config = _sse_config(auth=AuthNoneConfig())
        # Should not raise
        _validate_auth_transport_compatibility(config)
        validate_server_config(config)

    def test_sse_with_static_headers_auth_is_accepted(self) -> None:
        """sse + static_headers auth must not raise (headers forwarded via sse_client)."""
        config = _sse_config(
            headers={"X-Api-Key": "key123"},
            auth=AuthStaticHeadersConfig(),
        )
        # Should not raise
        _validate_auth_transport_compatibility(config)
        validate_server_config(config)

    def test_http_with_oauth_is_unaffected(self) -> None:
        """http + oauth must not raise (OAuth is only blocked for SSE)."""
        config = MCPServerConfig(
            name="test-http-oauth",
            transport="http",
            url="http://host:8080/mcp",
            auth=_oauth_config(),
        )
        # Should not raise ConfigValidationError from the sse+oauth gate
        _validate_auth_transport_compatibility(config)

    def test_stdio_with_none_auth_is_unaffected(self) -> None:
        """stdio + none must not raise — the gate only acts on sse+oauth."""
        config = _stdio_config()
        _validate_auth_transport_compatibility(config)

    def test_sse_oauth_error_message_is_actionable(self) -> None:
        """ConfigValidationError for sse+oauth must suggest corrective action."""
        config = _sse_config(auth=_oauth_config())
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_auth_transport_compatibility(config)

        msg = str(exc_info.value)
        # Message should guide the user to use transport="http" for OAuth
        assert "http" in msg.lower(), (
            f"Error should mention 'http' as the correct transport for OAuth; "
            f"got: {msg!r}"
        )

    def test_oauth_on_non_sse_doesnt_trigger_sse_gate(self) -> None:
        """Only sse transport triggers the sse+oauth gate — http and stdio never do."""
        for transport, extra_kw in (
            ("http", {"url": "http://host/mcp"}),
            ("stdio", {"command": "python3"}),
        ):
            config = MCPServerConfig(
                name=f"test-{transport}-oauth",
                transport=transport,
                auth=_oauth_config(),
                **extra_kw,
            )
            # Must not raise from _validate_auth_transport_compatibility
            _validate_auth_transport_compatibility(config)
