"""P05 — tests for auth policy config models.

TDD RED: tests written before implementation exists.
Gate: 100% line coverage, ≥95% branch coverage on auth/models.py.
"""
from __future__ import annotations

import pytest

from slm_mcp_hub.auth.models import (
    AUTH_CREDENTIAL_HEADERS,
    AuthMode,
    AuthNoneConfig,
    AuthOAuthConfig,
    AuthStaticHeadersConfig,
    parse_auth_config,
)
from slm_mcp_hub.core.config import (
    ConfigValidationError,
    MCPServerConfig,
    parse_mcp_server,
    validate_server_config,
)

# ---------------------------------------------------------------------------
# AuthNoneConfig
# ---------------------------------------------------------------------------


class TestAuthNoneConfig:
    def test_default_mode_is_none(self):
        cfg = AuthNoneConfig()
        assert cfg.mode == AuthMode.NONE

    def test_is_frozen(self):
        cfg = AuthNoneConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.mode = "static_headers"  # type: ignore[misc]

    def test_equality(self):
        assert AuthNoneConfig() == AuthNoneConfig()

    def test_repr_contains_no_credential(self):
        r = repr(AuthNoneConfig())
        # none mode config can never contain a credential
        for bad in ("password", "secret", "token", "authorization"):
            assert bad not in r.lower()


# ---------------------------------------------------------------------------
# AuthStaticHeadersConfig
# ---------------------------------------------------------------------------


class TestAuthStaticHeadersConfig:
    def test_mode_is_static_headers(self):
        cfg = AuthStaticHeadersConfig()
        assert cfg.mode == AuthMode.STATIC_HEADERS

    def test_is_frozen(self):
        cfg = AuthStaticHeadersConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.mode = "none"  # type: ignore[misc]

    def test_equality_different_instances(self):
        a = AuthStaticHeadersConfig()
        b = AuthStaticHeadersConfig()
        assert a == b

    def test_repr_does_not_expose_credential_values(self):
        cfg = AuthStaticHeadersConfig()
        r = repr(cfg)
        # mode name is fine; raw credential values must never appear
        assert "bearer" not in r.lower()


# ---------------------------------------------------------------------------
# AuthOAuthConfig
# ---------------------------------------------------------------------------


class TestAuthOAuthConfig:
    def test_mode_is_oauth(self):
        cfg = AuthOAuthConfig()
        assert cfg.mode == AuthMode.OAUTH

    def test_defaults(self):
        cfg = AuthOAuthConfig()
        assert cfg.scopes == ()
        assert cfg.client_metadata_url is None
        assert cfg.callback_host == "127.0.0.1"
        assert cfg.callback_port == 0

    def test_custom_scopes(self):
        cfg = AuthOAuthConfig(scopes=("read", "write"))
        assert cfg.scopes == ("read", "write")

    def test_custom_callback_port(self):
        cfg = AuthOAuthConfig(callback_port=8765)
        assert cfg.callback_port == 8765

    def test_custom_callback_host(self):
        cfg = AuthOAuthConfig(callback_host="127.0.0.1")
        assert cfg.callback_host == "127.0.0.1"

    def test_client_metadata_url_set(self):
        cfg = AuthOAuthConfig(client_metadata_url="https://example.com/.well-known/client")
        assert cfg.client_metadata_url == "https://example.com/.well-known/client"

    def test_is_frozen(self):
        cfg = AuthOAuthConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.scopes = ("rw",)  # type: ignore[misc]

    def test_repr_never_contains_token_or_secret(self):
        cfg = AuthOAuthConfig(scopes=("read",))
        r = repr(cfg)
        for bad in ("access_token", "refresh_token", "client_secret", "password", "bearer"):
            assert bad not in r.lower(), f"repr contains sensitive term '{bad}': {r!r}"

    def test_scopes_must_be_strings(self):
        with pytest.raises((TypeError, ValueError, ConfigValidationError)):
            AuthOAuthConfig(scopes=(1, 2))  # type: ignore[arg-type]

    def test_callback_port_must_be_int(self):
        with pytest.raises((TypeError, ValueError, ConfigValidationError)):
            AuthOAuthConfig(callback_port="abc")  # type: ignore[arg-type]

    def test_callback_host_must_be_string(self):
        with pytest.raises((TypeError, ValueError, ConfigValidationError)):
            AuthOAuthConfig(callback_host=127001)  # type: ignore[arg-type]

    def test_callback_host_must_be_loopback(self):
        """SEC-L-01: a non-loopback callback_host is rejected at config time."""
        for bad in ("0.0.0.0", "attacker.com", "192.168.1.10"):
            with pytest.raises((TypeError, ValueError, ConfigValidationError)):
                AuthOAuthConfig(callback_host=bad)
        # Loopback values are accepted.
        for ok in ("127.0.0.1", "localhost", "::1"):
            assert AuthOAuthConfig(callback_host=ok).callback_host == ok


# ---------------------------------------------------------------------------
# Credential-header blocklist
# ---------------------------------------------------------------------------


class TestCredentialHeaders:
    def test_authorization_in_blocklist(self):
        assert "authorization" in AUTH_CREDENTIAL_HEADERS

    def test_cookie_in_blocklist(self):
        assert "cookie" in AUTH_CREDENTIAL_HEADERS

    def test_proxy_authorization_in_blocklist(self):
        assert "proxy-authorization" in AUTH_CREDENTIAL_HEADERS

    def test_blocklist_is_lowercase(self):
        for h in AUTH_CREDENTIAL_HEADERS:
            assert h == h.lower(), f"Header {h!r} is not lowercase"


# ---------------------------------------------------------------------------
# parse_auth_config
# ---------------------------------------------------------------------------


class TestParseAuthConfig:
    def test_none_raw(self):
        cfg = parse_auth_config({"mode": "none"})
        assert isinstance(cfg, AuthNoneConfig)

    def test_static_headers_raw(self):
        cfg = parse_auth_config({"mode": "static_headers"})
        assert isinstance(cfg, AuthStaticHeadersConfig)

    def test_oauth_raw_minimal(self):
        cfg = parse_auth_config({"mode": "oauth"})
        assert isinstance(cfg, AuthOAuthConfig)
        assert cfg.scopes == ()

    def test_oauth_raw_full(self):
        cfg = parse_auth_config({
            "mode": "oauth",
            "scopes": ["mcp.read"],
            "client_metadata_url": "https://example.com/.well-known/client",
            "callback_host": "127.0.0.1",
            "callback_port": 9999,
        })
        assert isinstance(cfg, AuthOAuthConfig)
        assert cfg.scopes == ("mcp.read",)
        assert cfg.callback_port == 9999

    def test_missing_mode_defaults_to_none(self):
        cfg = parse_auth_config({})
        assert isinstance(cfg, AuthNoneConfig)

    def test_none_input_defaults_to_none(self):
        cfg = parse_auth_config(None)  # type: ignore[arg-type]
        assert isinstance(cfg, AuthNoneConfig)

    def test_invalid_mode_raises(self):
        with pytest.raises((ConfigValidationError, ValueError, KeyError)):
            parse_auth_config({"mode": "unknown_mode_xyz"})

    def test_oauth_scopes_list_becomes_tuple(self):
        cfg = parse_auth_config({"mode": "oauth", "scopes": ["a", "b", "c"]})
        assert isinstance(cfg, AuthOAuthConfig)
        assert cfg.scopes == ("a", "b", "c")


# ---------------------------------------------------------------------------
# MCPServerConfig integration — auth field
# ---------------------------------------------------------------------------


class TestMCPServerConfigAuth:
    def test_auth_defaults_to_none(self):
        cfg = MCPServerConfig(name="s", transport="stdio", command="uv")
        assert isinstance(cfg.auth, AuthNoneConfig)

    def test_auth_oauth_can_be_set(self):
        auth = AuthOAuthConfig(scopes=("read",))
        cfg = MCPServerConfig(name="s", transport="http", url="https://example.com", auth=auth)
        assert cfg.auth.mode == AuthMode.OAUTH

    def test_auth_static_can_be_set(self):
        auth = AuthStaticHeadersConfig()
        cfg = MCPServerConfig(name="s", transport="http", url="https://example.com", auth=auth)
        assert cfg.auth.mode == AuthMode.STATIC_HEADERS

    def test_config_is_frozen(self):
        cfg = MCPServerConfig(name="s", transport="stdio", command="uv")
        with pytest.raises((AttributeError, TypeError)):
            cfg.auth = AuthNoneConfig()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# validate_server_config — OAuth + credential-header conflict (negative tests)
# ---------------------------------------------------------------------------


class TestOAuthCredentialHeaderConflict:
    def _make_oauth_server(self, headers: dict) -> MCPServerConfig:
        return MCPServerConfig(
            name="test-server",
            transport="http",
            url="https://example.com/mcp",
            headers=headers,
            auth=AuthOAuthConfig(scopes=("read",)),
        )

    def test_oauth_plus_authorization_header_raises(self):
        cfg = self._make_oauth_server({"Authorization": "Bearer some-token"})
        with pytest.raises(ConfigValidationError, match="(?i)(credential|authorization|oauth)"):
            validate_server_config(cfg)

    def test_oauth_plus_cookie_header_raises(self):
        cfg = self._make_oauth_server({"Cookie": "session=abc"})
        with pytest.raises(ConfigValidationError, match="(?i)(credential|cookie|oauth)"):
            validate_server_config(cfg)

    def test_oauth_plus_proxy_authorization_raises(self):
        cfg = self._make_oauth_server({"Proxy-Authorization": "Basic xyz"})
        with pytest.raises(ConfigValidationError, match="(?i)(credential|proxy|oauth)"):
            validate_server_config(cfg)

    def test_oauth_plus_authorization_case_insensitive(self):
        # Header names are case-insensitive in HTTP
        cfg = self._make_oauth_server({"authorization": "Bearer tok"})
        with pytest.raises(ConfigValidationError):
            validate_server_config(cfg)

    def test_oauth_plus_whitespace_padded_authorization_raises(self):
        """Hardening: a whitespace-padded header name must not smuggle a
        credential past the reject list under oauth mode."""
        cfg = self._make_oauth_server({"  Authorization  ": "Bearer tok"})
        with pytest.raises(ConfigValidationError):
            validate_server_config(cfg)

    def test_oauth_plus_non_credential_header_is_ok(self):
        cfg = self._make_oauth_server({"X-Custom-Header": "safe-value"})
        # Should NOT raise — non-credential companion header is allowed
        validate_server_config(cfg)

    def test_oauth_with_no_headers_is_ok(self):
        cfg = self._make_oauth_server({})
        validate_server_config(cfg)  # must not raise

    def test_static_headers_with_authorization_is_ok(self):
        """static_headers mode is designed for credential headers — must remain valid."""
        cfg = MCPServerConfig(
            name="test-server",
            transport="http",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer token"},
            auth=AuthStaticHeadersConfig(),
        )
        validate_server_config(cfg)  # must not raise

    def test_none_auth_with_authorization_header_is_ok(self):
        """Existing behavior: none mode + headers is valid (legacy config)."""
        cfg = MCPServerConfig(
            name="test-server",
            transport="http",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer token"},
        )
        validate_server_config(cfg)  # must not raise (backward compat)


# ---------------------------------------------------------------------------
# parse_mcp_server — auth block integration
# ---------------------------------------------------------------------------


class TestParseMCPServerAuth:
    def test_no_auth_block_gives_none_mode(self):
        srv = parse_mcp_server("my-server", {"url": "https://example.com"})
        assert isinstance(srv.auth, AuthNoneConfig)

    def test_auth_none_block(self):
        srv = parse_mcp_server("my-server", {
            "url": "https://example.com",
            "auth": {"mode": "none"},
        })
        assert isinstance(srv.auth, AuthNoneConfig)

    def test_auth_oauth_block(self):
        srv = parse_mcp_server("oauth-server", {
            "url": "https://mcp.example.com",
            "auth": {
                "mode": "oauth",
                "scopes": ["mcp.tools"],
                "callback_port": 0,
            },
        })
        assert isinstance(srv.auth, AuthOAuthConfig)
        assert srv.auth.scopes == ("mcp.tools",)

    def test_auth_static_headers_block(self):
        srv = parse_mcp_server("static-server", {
            "url": "https://api.example.com",
            "headers": {"X-API-Key": "${MY_KEY}"},
            "auth": {"mode": "static_headers"},
        })
        assert isinstance(srv.auth, AuthStaticHeadersConfig)
        assert srv.headers == {"X-API-Key": "${MY_KEY}"}

    def test_existing_headers_without_auth_still_work(self):
        """Back-compat: headers in server config without auth block must parse."""
        srv = parse_mcp_server("legacy-server", {
            "url": "https://example.com",
            "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
        })
        # Default auth is none when no auth block
        assert isinstance(srv.auth, AuthNoneConfig)
        assert srv.headers["Authorization"] == "Bearer ${MY_TOKEN}"

    def test_oauth_plus_credential_header_raises_at_parse(self):
        with pytest.raises(ConfigValidationError):
            parse_mcp_server("bad-server", {
                "url": "https://example.com",
                "headers": {"Authorization": "Bearer token"},
                "auth": {"mode": "oauth"},
            })


# ---------------------------------------------------------------------------
# Additional coverage: __post_init__ validation branches
# ---------------------------------------------------------------------------


class TestPostInitValidation:
    """Cover the internal mode-guard branches in each frozen config class."""

    def test_auth_none_config_wrong_mode_raises(self):
        """AuthNoneConfig.__post_init__ rejects non-none mode."""
        with pytest.raises(ValueError):
            # noinspection PyArgumentList
            AuthNoneConfig(mode=AuthMode.OAUTH)  # type: ignore[call-arg]

    def test_auth_static_config_wrong_mode_raises(self):
        """AuthStaticHeadersConfig.__post_init__ rejects non-static mode."""
        with pytest.raises(ValueError):
            AuthStaticHeadersConfig(mode=AuthMode.OAUTH)  # type: ignore[call-arg]

    def test_auth_oauth_config_wrong_mode_raises(self):
        """AuthOAuthConfig.__post_init__ rejects non-oauth mode."""
        with pytest.raises(ValueError):
            AuthOAuthConfig(mode=AuthMode.NONE)  # type: ignore[call-arg]

    def test_auth_oauth_scopes_list_not_tuple_raises(self):
        """_validate_scopes raises TypeError when scopes is a list (not a tuple)."""
        with pytest.raises(TypeError, match="tuple"):
            AuthOAuthConfig(scopes=["a", "b"])  # type: ignore[arg-type]

    def test_parse_auth_config_scopes_not_a_list_raises(self):
        """parse_auth_config raises ConfigValidationError when scopes is a string."""
        with pytest.raises(ConfigValidationError, match="scopes"):
            parse_auth_config({"mode": "oauth", "scopes": "read"})

    def test_parse_auth_config_invalid_callback_port_raises(self):
        """ConfigValidationError when callback_port is a non-integer."""
        with pytest.raises(ConfigValidationError, match="(?i)(oauth|callback|invalid)"):
            parse_auth_config({"mode": "oauth", "callback_port": "not-a-number"})
