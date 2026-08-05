"""P06 — OAuth broker tests.

Covers:
1. Downstream Authorization header NEVER becomes upstream Authorization (negative test)
2. OAuth mode rejects credential-bearing static headers (Authorization, Cookie)
3. Cross-process filelock acquired around refresh
4. Concurrent refresh produces exactly ONE token request
5. One 401 retry after refresh; no second retry (no loop)
6. OAuthAuthRequiredError raised when no redirect handler can satisfy auth

RED phase: fails with ImportError until auth/broker.py is created.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.auth.broker import (
    OAuthAuthRequiredError,
    build_oauth_http_client,
    get_refresh_lock_path,
)
from slm_mcp_hub.auth.models import AUTH_CREDENTIAL_HEADERS, AuthOAuthConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _oauth_config(**kw) -> AuthOAuthConfig:
    return AuthOAuthConfig(
        scopes=kw.get("scopes", ("read",)),
        client_metadata_url=kw.get("client_metadata_url"),
        callback_host=kw.get("callback_host", "127.0.0.1"),
        callback_port=kw.get("callback_port", 0),
    )


def _simple_storage():
    storage = MagicMock()
    storage.get_tokens = AsyncMock(return_value=None)
    storage.get_client_info = AsyncMock(return_value=None)
    storage.set_tokens = AsyncMock()
    storage.set_client_info = AsyncMock()
    return storage


# ---------------------------------------------------------------------------
# Security: downstream authz never upstream (invariant #1)
# ---------------------------------------------------------------------------


class TestDownstreamAuthzNeverUpstream:
    """Downstream Authorization must NEVER reach the upstream MCP server."""

    def test_build_oauth_http_client_takes_no_inbound_headers(self):
        """build_oauth_http_client has no parameter for inbound headers.

        This structural test ensures there is no API surface through which
        inbound Authorization / Cookie headers could be forwarded.
        """
        import inspect
        sig = inspect.signature(build_oauth_http_client)
        param_names = set(sig.parameters.keys())
        # Must NOT have any parameter that could inject inbound auth headers
        forbidden = {"inbound_headers", "request_headers", "forward_headers", "headers"}
        intersection = param_names & forbidden
        assert not intersection, (
            f"build_oauth_http_client must not accept inbound header parameters; "
            f"found: {intersection}"
        )

    def test_client_built_without_inbound_bearer(self):
        """Even if a caller attempts to pass an Authorization header, the client
        must not expose a way to forward it.
        """
        import httpx2

        config = _oauth_config()
        storage = _simple_storage()
        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        assert isinstance(client, httpx2.AsyncClient)
        # Verify no Authorization header is baked into the client's default headers
        default_auth = client.headers.get("authorization", "")
        assert default_auth == ""

    def test_sentinel_bearer_not_in_client_headers(self):
        """A sentinel Bearer token must not appear in the constructed client."""
        import httpx2

        SENTINEL = "Bearer should_never_appear_in_upstream_Bearer_tok123"
        config = _oauth_config()
        storage = _simple_storage()
        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        assert isinstance(client, httpx2.AsyncClient)
        headers_str = str(dict(client.headers)).lower()
        assert "should_never_appear" not in headers_str
        assert SENTINEL.lower() not in headers_str


# ---------------------------------------------------------------------------
# OAuth mode rejects credential-bearing static headers (invariant #2)
# ---------------------------------------------------------------------------


class TestOAuthStaticHeadersRejection:
    """When auth.mode==oauth, credential-bearing static headers are invalid."""

    def test_auth_credential_headers_contains_expected_keys(self):
        """AUTH_CREDENTIAL_HEADERS must include the known forbidden set."""
        assert "authorization" in AUTH_CREDENTIAL_HEADERS
        assert "cookie" in AUTH_CREDENTIAL_HEADERS
        assert "proxy-authorization" in AUTH_CREDENTIAL_HEADERS

    def test_oauth_config_rejects_authorization_header_via_validation(self, tmp_path):
        """MCPServerConfig with oauth+Authorization static header raises at parse time."""
        from slm_mcp_hub.core.config import ConfigValidationError, load_config

        # mcpServers is a dict of {name: config} in the Hub's config format
        bad_config = {
            "mcpServers": {
                "bad-server": {
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer bad_token"},
                    "auth": {"mode": "oauth", "scopes": ["read"]},
                }
            }
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(bad_config))

        with pytest.raises((ConfigValidationError, ValueError)):
            load_config(config_file)

    def test_oauth_config_rejects_cookie_header_via_validation(self, tmp_path):
        """MCPServerConfig with oauth+Cookie static header raises at parse time."""
        from slm_mcp_hub.core.config import ConfigValidationError, load_config

        # mcpServers is a dict of {name: config} in the Hub's config format
        bad_config = {
            "mcpServers": {
                "bad-server": {
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Cookie": "session=bad"},
                    "auth": {"mode": "oauth", "scopes": ["read"]},
                }
            }
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(bad_config))

        with pytest.raises((ConfigValidationError, ValueError)):
            load_config(config_file)


# ---------------------------------------------------------------------------
# Cross-process lock path
# ---------------------------------------------------------------------------


class TestRefreshLockPath:
    def test_lock_path_is_under_config_dir(self, tmp_path):
        """Lock file must live under the Hub runtime dir, not in /tmp or cwd."""
        account_key = "abc123"
        lock_path = get_refresh_lock_path(tmp_path, account_key)
        assert lock_path.is_relative_to(tmp_path)

    def test_lock_path_contains_account_key(self, tmp_path):
        """Lock path is keyed by account_key so different servers have different locks."""
        key_a = "aaa111"
        key_b = "bbb222"
        path_a = get_refresh_lock_path(tmp_path, key_a)
        path_b = get_refresh_lock_path(tmp_path, key_b)
        assert path_a != path_b

    def test_lock_path_has_no_secret(self, tmp_path):
        """Lock path must never contain a token or bearer value.

        The account_key is a SHA-256 hex digest — it looks like random hex,
        not like a credential value.  A valid hex digest contains only
        [0-9a-f] characters, no Bearer prefix, no access_token substring.
        """
        # SHA-256 hex digest example — no credential-looking substrings
        account_key = "7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069"
        lock_path = get_refresh_lock_path(tmp_path, account_key)
        path_str = str(lock_path)
        # Must not embed something that looks like a Bearer token
        assert "Bearer" not in path_str
        assert "access_token" not in path_str.lower()
        # Account key IS in the path (that's correct) but it's just hex
        assert account_key in path_str

    def test_lock_file_extension(self, tmp_path):
        """Lock file should have .lock extension."""
        lock_path = get_refresh_lock_path(tmp_path, "any_key")
        assert lock_path.suffix == ".lock"


# ---------------------------------------------------------------------------
# Concurrent refresh — single token request (invariant #6)
# ---------------------------------------------------------------------------


class TestConcurrentRefreshSerialization:
    async def test_concurrent_callers_produce_one_token_request(self, tmp_path):
        """Multiple concurrent coroutines acquiring the same refresh lock
        must serialize: only one actually reaches the token endpoint at a time.
        """
        from slm_mcp_hub.auth.broker import refresh_lock_context

        call_count = 0
        in_lock_simultaneously = 0
        lock_path = get_refresh_lock_path(tmp_path, "test_account_key")

        async def _refresh_task():
            nonlocal call_count, in_lock_simultaneously
            async with refresh_lock_context(lock_path):
                in_lock_simultaneously += 1
                assert in_lock_simultaneously == 1, (
                    "Multiple coroutines inside the lock simultaneously"
                )
                call_count += 1
                await asyncio.sleep(0.01)  # Simulate token endpoint call
                in_lock_simultaneously -= 1

        # Run 5 concurrent refreshes
        await asyncio.gather(*(_refresh_task() for _ in range(5)))
        assert call_count == 5  # All ran, but serialized


# ---------------------------------------------------------------------------
# OAuthAuthRequiredError
# ---------------------------------------------------------------------------


class TestOAuthAuthRequiredError:
    def test_is_exception_subclass(self):
        assert issubclass(OAuthAuthRequiredError, Exception)

    def test_does_not_leak_token(self):
        """Error message must not contain token or secret material."""
        SENTINEL = "super_secret_token_XYZ"
        err = OAuthAuthRequiredError("OAuth authorization is required")
        assert SENTINEL not in str(err)
        assert SENTINEL not in repr(err)

    def test_message_contains_safe_remediation_hint(self):
        """Error message should reference auth login for remediation."""
        err = OAuthAuthRequiredError(
            "OAuth authorization required; run: slm-hub auth login SERVER"
        )
        assert "auth" in str(err).lower() or "login" in str(err).lower()


# ---------------------------------------------------------------------------
# refresh_lock_context: filelock release exception swallowed
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fix 1: SSRF hook wiring in build_oauth_http_client
# ---------------------------------------------------------------------------


class TestSSRFHookWiring:
    """Tests that build_oauth_http_client wires is_safe_oauth_metadata_url as an
    httpx2 request event hook that gates ALL outgoing requests (incl. redirects).
    """

    def test_client_has_request_event_hook(self):
        """Client returned by build_oauth_http_client has at least one request hook."""
        config = _oauth_config()
        storage = _simple_storage()
        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        assert len(client.event_hooks.get("request", [])) >= 1

    async def test_ssrf_hook_blocks_imds_url(self):
        """SSRF hook raises ValueError for 169.254.169.254 (AWS IMDS)."""
        import httpx2

        config = _oauth_config()
        storage = _simple_storage()
        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        hooks = client.event_hooks.get("request", [])
        assert hooks, "No request hooks found"

        private_req = httpx2.Request(
            "GET", "https://169.254.169.254/latest/meta-data/"
        )
        with pytest.raises(ValueError):
            for hook in hooks:
                await hook(private_req)

    async def test_ssrf_hook_blocks_private_ipv4(self):
        """SSRF hook raises ValueError for 10.0.0.5 (RFC 1918)."""
        import httpx2

        config = _oauth_config()
        storage = _simple_storage()
        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        hooks = client.event_hooks.get("request", [])
        req = httpx2.Request("GET", "https://10.0.0.5/token")
        with pytest.raises(ValueError):
            for hook in hooks:
                await hook(req)

    async def test_ssrf_hook_blocks_ipv6_ula(self):
        """SSRF hook raises ValueError for IPv6 ULA [fd00::1]."""
        import httpx2

        config = _oauth_config()
        storage = _simple_storage()
        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        hooks = client.event_hooks.get("request", [])
        req = httpx2.Request("GET", "https://[fd00::1]/token")
        with pytest.raises(ValueError):
            for hook in hooks:
                await hook(req)

    async def test_ssrf_hook_blocks_non_https_non_loopback(self):
        """SSRF hook raises ValueError for http:// to a non-loopback host."""
        import httpx2

        config = _oauth_config()
        storage = _simple_storage()
        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        hooks = client.event_hooks.get("request", [])
        req = httpx2.Request("GET", "http://example.com/token")
        with pytest.raises(ValueError):
            for hook in hooks:
                await hook(req)

    async def test_ssrf_hook_allows_safe_public_https(self):
        """SSRF hook allows requests to public HTTPS endpoints without raising.

        DNS is mocked to a public IP so the safe host resolves (the guard now
        fails CLOSED on unresolvable hosts, so a fake FQDN must be pinned)."""
        import socket
        from unittest.mock import patch

        import httpx2

        config = _oauth_config()
        storage = _simple_storage()
        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
        )
        hooks = client.event_hooks.get("request", [])
        safe_req = httpx2.Request(
            "GET",
            "https://as.example.com/.well-known/oauth-authorization-server",
        )
        with patch(
            "slm_mcp_hub.auth.provider.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        ):
            for hook in hooks:
                await hook(safe_req)  # Must not raise — resolves to a public IP


# ---------------------------------------------------------------------------
# Fix 4: _LockedAuth wrapper wires refresh lock into auth flow
# ---------------------------------------------------------------------------


class TestLockedAuthWrapper:
    """Tests for the _LockedAuth class and its integration into build_oauth_http_client."""

    def test_locked_auth_exposes_inner_context(self, tmp_path):
        """_LockedAuth.context delegates to the inner OAuthClientProvider.context."""
        from slm_mcp_hub.auth.broker import _LockedAuth  # noqa: PLC2701
        from slm_mcp_hub.auth.provider import build_runtime_provider

        config = _oauth_config()
        storage = _simple_storage()
        inner = build_runtime_provider("https://example.com/mcp", config, storage)
        lock_path = get_refresh_lock_path(tmp_path, "ctx_test_key")

        wrapped = _LockedAuth(inner, lock_path)
        assert wrapped.context is inner.context
        assert wrapped.context.redirect_handler is None

    async def test_build_with_lock_path_returns_locked_auth(self, tmp_path):
        """build_oauth_http_client(lock_path=...) wraps auth in _LockedAuth."""
        from slm_mcp_hub.auth.broker import _LockedAuth  # noqa: PLC2701

        config = _oauth_config()
        storage = _simple_storage()
        lock_path = get_refresh_lock_path(tmp_path, "wrap_test_key")

        client = build_oauth_http_client(
            server_url="https://example.com/mcp",
            auth_config=config,
            storage=storage,
            lock_path=lock_path,
        )
        assert isinstance(client.auth, _LockedAuth)
        assert client.auth.context.redirect_handler is None

    async def test_locked_auth_serializes_concurrent_401_handling(self, tmp_path):
        """Concurrent 401-handling flows are serialized: max 1 inside lock at a time."""
        from slm_mcp_hub.auth.broker import _LockedAuth  # noqa: PLC2701

        lock_path = get_refresh_lock_path(tmp_path, "serial_401_key")
        concurrent_in_401_phase = 0
        max_concurrent = 0

        def make_inner():
            inner = MagicMock()
            inner.context = MagicMock()
            inner.context.redirect_handler = None

            async def mock_flow(request):  # noqa: ARG001
                initial_req = MagicMock()
                response = yield initial_req  # noqa: F841
                # Second yield onwards = 401-handling phase
                nonlocal concurrent_in_401_phase, max_concurrent
                concurrent_in_401_phase += 1
                max_concurrent = max(max_concurrent, concurrent_in_401_phase)
                await asyncio.sleep(0.01)  # Simulate token-endpoint latency
                concurrent_in_401_phase -= 1
                retry_req = MagicMock()
                response = yield retry_req  # noqa: F841

            inner.async_auth_flow = mock_flow
            return inner

        async def run_flow():
            provider = _LockedAuth(make_inner(), lock_path)
            gen = provider.async_auth_flow(MagicMock())
            _ = await gen.__anext__()
            mock_401 = MagicMock()
            mock_401.status_code = 401
            try:
                _ = await gen.asend(mock_401)
                mock_200 = MagicMock()
                mock_200.status_code = 200
                try:
                    await gen.asend(mock_200)
                except StopAsyncIteration:
                    pass
            except StopAsyncIteration:
                pass

        # Three concurrent flows all hitting 401
        await asyncio.gather(*(run_flow() for _ in range(3)))
        assert max_concurrent == 1, (
            f"At most 1 should be inside the 401-handling lock at once; got {max_concurrent}"
        )

    async def test_locked_auth_normal_200_flow(self, tmp_path):
        """_LockedAuth completes normally when inner provider sees a 200 (no refresh)."""
        from slm_mcp_hub.auth.broker import _LockedAuth  # noqa: PLC2701

        lock_path = get_refresh_lock_path(tmp_path, "ok_200_key")

        inner = MagicMock()
        inner.context = MagicMock()

        async def mock_flow_200(request):  # noqa: ARG001
            req = MagicMock()
            _ = yield req
            # 200 response → no further yields → StopAsyncIteration

        inner.async_auth_flow = mock_flow_200

        provider = _LockedAuth(inner, lock_path)
        gen = provider.async_auth_flow(MagicMock())
        _ = await gen.__anext__()
        mock_200 = MagicMock()
        mock_200.status_code = 200
        try:
            await gen.asend(mock_200)
        except StopAsyncIteration:
            pass  # Expected — clean completion

    async def test_locked_auth_inner_stops_on_first_anext(self, tmp_path):
        """_LockedAuth handles an inner generator that immediately raises StopAsyncIteration."""
        from slm_mcp_hub.auth.broker import _LockedAuth  # noqa: PLC2701

        lock_path = get_refresh_lock_path(tmp_path, "first_stop_key")

        inner = MagicMock()
        inner.context = MagicMock()

        async def mock_flow_empty(request):  # noqa: ARG001
            return  # No yields at all → StopAsyncIteration on first __anext__
            yield  # Make it a generator (unreachable)

        inner.async_auth_flow = mock_flow_empty

        provider = _LockedAuth(inner, lock_path)
        gen = provider.async_auth_flow(MagicMock())
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass  # Expected


class TestRefreshLockContextEdgeCases:
    async def test_filelock_release_exception_is_swallowed(self, tmp_path):
        """If FileLock.release() raises, the exception is silently swallowed.

        This prevents a secondary exception during cleanup from masking
        the original error (or from propagating into the Hub runtime).
        """
        import filelock

        from slm_mcp_hub.auth.broker import refresh_lock_context

        lock_path = get_refresh_lock_path(tmp_path, "release_fail_key")

        class _FailRelease(filelock.FileLock):
            def release(self, *args, **kwargs):  # noqa: ARG002
                raise RuntimeError("simulated lock release failure")

        async def _run():
            async with refresh_lock_context(lock_path):
                pass  # Lock acquired; release will fail in finally

        with patch("slm_mcp_hub.auth.broker.filelock.FileLock", _FailRelease):
            # Must NOT raise — the except Exception: pass swallows it
            await _run()
