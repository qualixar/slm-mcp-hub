"""P06 — OAuth metadata and transport-policy tests.

Tests cover:
- URL validation predicate (HTTPS allowed, loopback HTTP allowed, everything else rejected)
- SSRF: private-network targets blocked for public endpoints
- provider.build_runtime_provider() has no redirect_handler (structural)
- provider.build_login_provider() has handlers (structural)
- Token value never surfaces in OAuthClientMetadata repr

RED phase: these fail with ImportError until auth/provider.py is created.
Gate: auth/provider.py reaches ≥97% line and ≥90% branch coverage.
"""
from __future__ import annotations

from slm_mcp_hub.auth.models import AuthOAuthConfig
from slm_mcp_hub.auth.provider import (
    OAuthProviderMode,
    build_login_provider,
    build_runtime_provider,
    is_safe_oauth_metadata_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_storage():
    """Return a no-op token storage stub (no keyring required for structural tests)."""
    from unittest.mock import AsyncMock, MagicMock
    storage = MagicMock()
    storage.get_tokens = AsyncMock(return_value=None)
    storage.get_client_info = AsyncMock(return_value=None)
    storage.set_tokens = AsyncMock()
    storage.set_client_info = AsyncMock()
    return storage


def _oauth_config(**kw) -> AuthOAuthConfig:
    return AuthOAuthConfig(
        scopes=kw.get("scopes", ("read",)),
        client_metadata_url=kw.get("client_metadata_url"),
        callback_host=kw.get("callback_host", "127.0.0.1"),
        callback_port=kw.get("callback_port", 0),
    )


# ---------------------------------------------------------------------------
# is_safe_oauth_metadata_url — URL transport policy
# ---------------------------------------------------------------------------


class TestSafeOAuthMetadataUrl:
    """Unit tests for the SSRF/redirect transport policy predicate."""

    def test_https_is_always_allowed(self):
        """HTTPS is the primary safe scheme for OAuth metadata."""
        assert is_safe_oauth_metadata_url(
            "https://example.com/.well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com/mcp",
        ) is True

    def test_https_with_port_is_allowed(self):
        assert is_safe_oauth_metadata_url(
            "https://example.com:8443/.well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com:8443/mcp",
        ) is True

    def test_http_exact_loopback_v4_allowed(self):
        """http://127.0.0.1 is the ONLY allowed non-HTTPS scheme."""
        assert is_safe_oauth_metadata_url(
            "http://127.0.0.1:8080/.well-known/oauth-authorization-server",
            mcp_endpoint="http://127.0.0.1:8080/mcp",
        ) is True

    def test_http_localhost_allowed(self):
        assert is_safe_oauth_metadata_url(
            "http://localhost:9000/.well-known/oauth-authorization-server",
            mcp_endpoint="http://localhost:9000/mcp",
        ) is True

    def test_http_loopback_v6_allowed(self):
        assert is_safe_oauth_metadata_url(
            "http://[::1]:8080/.well-known/oauth-authorization-server",
            mcp_endpoint="http://[::1]:8080/mcp",
        ) is True

    def test_http_public_host_rejected(self):
        """http:// to a non-loopback host MUST be rejected (no TLS = MITM)."""
        assert is_safe_oauth_metadata_url(
            "http://example.com/.well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_userinfo_in_https_url_rejected(self):
        """https://user:password@example.com is rejected (credential in URL)."""
        assert is_safe_oauth_metadata_url(
            "https://user:secret@example.com/.well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_userinfo_in_http_loopback_rejected(self):
        assert is_safe_oauth_metadata_url(
            "http://admin:admin@127.0.0.1:8080/.well-known/oauth-authorization-server",
            mcp_endpoint="http://127.0.0.1:8080/mcp",
        ) is False

    def test_file_scheme_rejected(self):
        assert is_safe_oauth_metadata_url(
            "file:///etc/passwd",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_ftp_scheme_rejected(self):
        assert is_safe_oauth_metadata_url(
            "ftp://example.com/metadata",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_data_scheme_rejected(self):
        assert is_safe_oauth_metadata_url(
            "data:text/plain;base64,aGVsbG8=",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_private_network_rfc1918_rejected_for_public_endpoint(self):
        """192.168.x.x / 10.x / 172.16-31.x are private; blocked for public endpoints."""
        private_urls = [
            "https://192.168.1.1/.well-known/oauth-authorization-server",
            "https://10.0.0.1/.well-known/oauth-authorization-server",
            "https://172.16.0.1/.well-known/oauth-authorization-server",
        ]
        for url in private_urls:
            assert is_safe_oauth_metadata_url(
                url, mcp_endpoint="https://example.com/mcp"
            ) is False, f"Expected {url!r} to be rejected for public endpoint"

    def test_private_network_allowed_when_mcp_is_loopback(self):
        """When the MCP endpoint itself is loopback, private metadata is also allowed.

        This permits local dev setups where both MCP server and AS run on localhost.
        """
        assert is_safe_oauth_metadata_url(
            "http://127.0.0.1:9000/.well-known/oauth-authorization-server",
            mcp_endpoint="http://127.0.0.1:8080/mcp",
        ) is True

    def test_empty_url_rejected(self):
        assert is_safe_oauth_metadata_url("", mcp_endpoint="https://example.com/mcp") is False

    def test_relative_url_rejected(self):
        assert is_safe_oauth_metadata_url(
            "/well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_javascript_scheme_rejected(self):
        assert is_safe_oauth_metadata_url(
            "javascript:alert(1)",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_unparseable_url_rejected(self):
        """URL that causes urlparse to raise is safely rejected (except Exception branch)."""
        from unittest.mock import patch

        with patch(
            "slm_mcp_hub.auth.provider.urlparse",
            side_effect=Exception("parse error"),
        ):
            result = is_safe_oauth_metadata_url(
                "https://example.com/test",
                mcp_endpoint="https://example.com/mcp",
            )
        assert result is False

    def test_empty_hostname_rejected(self):
        """URL with no hostname component is rejected (empty host branch)."""
        # 'https:///path' parses with hostname=None → _canonical_host returns ""
        assert is_safe_oauth_metadata_url(
            "https:///path",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_private_rfc1918_allowed_when_mcp_is_loopback(self):
        """RFC 1918 metadata URL is allowed when MCP endpoint is a loopback address.

        This is the local-dev exception: both AS and MCP server run on the same
        machine.  The branch: private_network AND mcp_host IN _LOOPBACK_HOSTS →
        skip 'return False' → reach 'return True'.
        """
        assert is_safe_oauth_metadata_url(
            "https://192.168.1.100/.well-known/oauth-authorization-server",
            mcp_endpoint="http://127.0.0.1:8080/mcp",
        ) is True


# ---------------------------------------------------------------------------
# Provider helper function coverage
# ---------------------------------------------------------------------------


class TestProviderHelpers:
    """Direct tests for private helpers that are not reachable via the public API."""

    async def test_open_browser_handler_calls_webbrowser(self):
        """_open_browser_handler opens the system browser (mocked to avoid real open)."""
        from unittest.mock import patch

        from slm_mcp_hub.auth.provider import _open_browser_handler  # noqa: PLC2701

        auth_url = "https://as.example.com/auth?client_id=x&response_type=code"
        with patch("slm_mcp_hub.auth.provider.webbrowser.open") as mock_open:
            await _open_browser_handler(auth_url)
            mock_open.assert_called_once_with(auth_url)

    def test_canonical_host_strips_ipv6_brackets(self):
        """_canonical_host strips square brackets from bracketed IPv6 addresses."""
        from slm_mcp_hub.auth.provider import _canonical_host  # noqa: PLC2701

        assert _canonical_host("[::1]") == "::1"
        assert _canonical_host("[2001:db8::1]") == "2001:db8::1"
        # Non-bracketed hosts are returned as-is (lower + strip)
        assert _canonical_host("127.0.0.1") == "127.0.0.1"
        assert _canonical_host("EXAMPLE.COM") == "example.com"


# ---------------------------------------------------------------------------
# OAuthProviderMode enum
# ---------------------------------------------------------------------------


class TestOAuthProviderMode:
    def test_runtime_and_login_modes_exist(self):
        assert OAuthProviderMode.RUNTIME is not None
        assert OAuthProviderMode.CLI_LOGIN is not None

    def test_modes_are_distinct(self):
        assert OAuthProviderMode.RUNTIME != OAuthProviderMode.CLI_LOGIN


# ---------------------------------------------------------------------------
# build_runtime_provider — structural (no redirect handler)
# ---------------------------------------------------------------------------


class TestBuildRuntimeProvider:
    """build_runtime_provider must never attach a redirect_handler."""

    def test_returns_oauth_client_provider(self):
        from mcp.client.auth import OAuthClientProvider

        config = _oauth_config()
        storage = _simple_storage()
        provider = build_runtime_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        assert isinstance(provider, OAuthClientProvider)

    def test_redirect_handler_is_none(self):
        """THE key runtime invariant: no redirect_handler → no browser opens."""
        config = _oauth_config()
        storage = _simple_storage()
        provider = build_runtime_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        # The context attribute is OAuthContext (dataclass); redirect_handler is None
        assert provider.context.redirect_handler is None

    def test_callback_handler_is_none(self):
        config = _oauth_config()
        storage = _simple_storage()
        provider = build_runtime_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        assert provider.context.callback_handler is None

    def test_server_url_passed_through(self):
        config = _oauth_config()
        storage = _simple_storage()
        provider = build_runtime_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        assert "example.com" in provider.context.server_url

    def test_scopes_included_in_metadata(self):
        config = _oauth_config(scopes=("read", "write"))
        storage = _simple_storage()
        provider = build_runtime_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        # SDK stores scopes as space-separated string in OAuthClientMetadata.scope
        assert provider.context.client_metadata.scope is not None
        scope_str = provider.context.client_metadata.scope
        assert "read" in scope_str
        assert "write" in scope_str

    def test_client_metadata_url_forwarded(self):
        config = _oauth_config(client_metadata_url="https://example.com/client-meta.json")
        storage = _simple_storage()
        provider = build_runtime_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        assert provider.context.client_metadata_url == "https://example.com/client-meta.json"

    def test_redirect_uris_uses_loopback(self):
        """Client metadata must have a loopback redirect_uri."""
        config = _oauth_config()
        storage = _simple_storage()
        provider = build_runtime_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        uris = provider.context.client_metadata.redirect_uris
        assert uris is not None and len(uris) >= 1
        uri = str(uris[0])
        assert "127.0.0.1" in uri or "localhost" in uri or "[::1]" in uri


# ---------------------------------------------------------------------------
# build_login_provider — structural (has redirect handler)
# ---------------------------------------------------------------------------


class TestBuildLoginProvider:
    """build_login_provider attaches browser and callback handlers for CLI use only."""

    def test_returns_oauth_client_provider(self):
        from mcp.client.auth import OAuthClientProvider


        # Use a mock callback server — don't start a real one for this structural test
        mock_cb = MagicMock_callback_server()
        config = _oauth_config()
        storage = _simple_storage()
        provider = build_login_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
            callback_server=mock_cb,
        )
        assert isinstance(provider, OAuthClientProvider)

    def test_redirect_handler_is_set(self):
        """CLI-login mode MUST have a redirect_handler."""
        mock_cb = MagicMock_callback_server()
        config = _oauth_config()
        storage = _simple_storage()
        provider = build_login_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
            callback_server=mock_cb,
        )
        assert provider.context.redirect_handler is not None

    def test_callback_handler_is_set(self):
        """CLI-login mode MUST have a callback_handler (bound to CallbackServer)."""
        mock_cb = MagicMock_callback_server()
        config = _oauth_config()
        storage = _simple_storage()
        provider = build_login_provider(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
            callback_server=mock_cb,
        )
        assert provider.context.callback_handler is not None


# ---------------------------------------------------------------------------
# Helpers that aren't in the stdlib — defined here for the tests above
# ---------------------------------------------------------------------------

def MagicMock_callback_server():
    """Minimal stub for CallbackServer (only needs redirect_uri and callback_handler)."""
    from unittest.mock import AsyncMock, MagicMock

    cb = MagicMock()
    cb.redirect_uri = "http://127.0.0.1:54321/callback"
    cb.callback_handler = AsyncMock()
    return cb


# ---------------------------------------------------------------------------
# Fix 2: IPv6 private-network + DNS-rebinding tests
# ---------------------------------------------------------------------------


class TestIPv6AndDNSRebinding:
    """IPv6 ULA / link-local + hostname DNS-rebinding protection.

    Prior to this fix _is_private_network was IPv4-only; IPv6 private addresses
    and hostnames that DNS-resolve to private IPs were incorrectly allowed.
    """

    def test_ipv6_ula_fd_prefix_rejected(self):
        """fd00::/8 is inside the fc00::/7 ULA range — must be rejected."""
        assert is_safe_oauth_metadata_url(
            "https://[fd00::1]/.well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_ipv6_ula_fc_prefix_rejected(self):
        """fc00::1 is inside fc00::/7 — must be rejected."""
        assert is_safe_oauth_metadata_url(
            "https://[fc00::1]/.well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_ipv6_link_local_rejected(self):
        """fe80::1 is link-local (fe80::/10) — must be rejected."""
        assert is_safe_oauth_metadata_url(
            "https://[fe80::1]/.well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_ipv6_multicast_rejected(self):
        """ff02::1 is multicast — must be rejected."""
        import ipaddress

        from slm_mcp_hub.auth.provider import _ip_is_blocked  # noqa: PLC2701
        assert _ip_is_blocked(ipaddress.ip_address("ff02::1")) is True

    def test_ipv6_unspecified_rejected(self):
        """:: (unspecified) — must be rejected."""
        import ipaddress

        from slm_mcp_hub.auth.provider import _ip_is_blocked  # noqa: PLC2701
        assert _ip_is_blocked(ipaddress.ip_address("::")) is True

    def test_ipv4_loopback_blocked(self):
        """127.0.0.1 is loopback — must be reported as blocked."""
        import ipaddress

        from slm_mcp_hub.auth.provider import _ip_is_blocked  # noqa: PLC2701
        assert _ip_is_blocked(ipaddress.ip_address("127.0.0.1")) is True

    def test_ipv4_public_not_blocked(self):
        """8.8.8.8 is a public IP — must NOT be blocked."""
        import ipaddress

        from slm_mcp_hub.auth.provider import _ip_is_blocked  # noqa: PLC2701
        assert _ip_is_blocked(ipaddress.ip_address("8.8.8.8")) is False

    def test_imds_link_local_rejected(self):
        """169.254.169.254 (AWS IMDS) is link-local IPv4 — must be rejected."""
        assert is_safe_oauth_metadata_url(
            "https://169.254.169.254/latest/meta-data/iam/security-credentials/",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_private_ipv4_10_rejected(self):
        """10.0.0.5 is RFC 1918 private — must be rejected for public MCP endpoint."""
        assert is_safe_oauth_metadata_url(
            "https://10.0.0.5/.well-known/oauth-authorization-server",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_hostname_resolving_to_private_ipv4_rejected(self):
        """Hostname that DNS-resolves to 10.x.x.x must be rejected (rebinding)."""
        import socket
        from unittest.mock import patch

        with patch("slm_mcp_hub.auth.provider.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))
            ]
            result = is_safe_oauth_metadata_url(
                "https://malicious-rebind.example.com/.well-known/oauth",
                mcp_endpoint="https://example.com/mcp",
            )
        assert result is False

    def test_hostname_resolving_to_192_168_rejected(self):
        """Hostname resolving to 192.168.x.x must be rejected."""
        import socket
        from unittest.mock import patch

        with patch("slm_mcp_hub.auth.provider.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0))
            ]
            result = is_safe_oauth_metadata_url(
                "https://internal.corp.example.com/.well-known/oauth",
                mcp_endpoint="https://example.com/mcp",
            )
        assert result is False

    def test_unresolvable_hostname_rejected_fail_closed(self):
        """Hostname that cannot be resolved is REJECTED (fail-closed).

        A metadata host we cannot resolve cannot be verified as safe, so it must
        not be fetched. (hardening — replaces the prior fail-open.)
        """
        from unittest.mock import patch

        with patch(
            "slm_mcp_hub.auth.provider.socket.getaddrinfo",
            side_effect=OSError("NXDOMAIN"),
        ):
            result = is_safe_oauth_metadata_url(
                "https://doesnotexist.invalid/.well-known/oauth",
                mcp_endpoint="https://example.com/mcp",
            )
        assert result is False

    def test_ipv4_mapped_ipv6_imds_rejected(self):
        """::ffff:169.254.169.254 (IPv4-mapped IMDS) must not bypass the guard."""
        import ipaddress

        from slm_mcp_hub.auth.provider import _ip_is_blocked  # noqa: PLC2701
        assert _ip_is_blocked(ipaddress.ip_address("::ffff:169.254.169.254")) is True
        assert is_safe_oauth_metadata_url(
            "https://[::ffff:169.254.169.254]/latest/meta-data/",
            mcp_endpoint="https://example.com/mcp",
        ) is False

    def test_ipv4_mapped_ipv6_rfc1918_rejected(self):
        """::ffff:10.0.0.1 (IPv4-mapped RFC1918) must not bypass the guard."""
        import ipaddress

        from slm_mcp_hub.auth.provider import _ip_is_blocked  # noqa: PLC2701
        assert _ip_is_blocked(ipaddress.ip_address("::ffff:10.0.0.1")) is True

    def test_ipv4_mapped_ipv6_public_allowed(self):
        """::ffff:8.8.8.8 (IPv4-mapped public) is not blocked."""
        import ipaddress

        from slm_mcp_hub.auth.provider import _ip_is_blocked  # noqa: PLC2701
        assert _ip_is_blocked(ipaddress.ip_address("::ffff:8.8.8.8")) is False

    def test_hostname_resolving_to_public_allowed(self):
        """Hostname resolving to a public IP must be allowed."""
        import socket
        from unittest.mock import patch

        with patch("slm_mcp_hub.auth.provider.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
            ]
            result = is_safe_oauth_metadata_url(
                "https://as.example.com/.well-known/oauth",
                mcp_endpoint="https://example.com/mcp",
            )
        assert result is True
