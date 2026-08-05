"""OAuth provider factory for SLM MCP Hub.

This module constructs ``mcp.client.auth.OAuthClientProvider`` instances for
two explicit modes:

RUNTIME mode (``build_runtime_provider``)
    No ``redirect_handler``, no ``callback_handler``.  The Hub cannot open a
    browser during startup, hot-reload, or a tool call.  When the SDK needs
    interactive authorization (after a 401 from the upstream), it raises
    ``OAuthFlowError`` which the Hub converts to ``AUTH_REQUIRED`` state.

CLI-LOGIN mode (``build_login_provider``)
    Attaches a system-browser redirect handler and a bounded loopback
    ``CallbackServer``.  Only ``slm-hub auth login SERVER`` uses this path.

Transport policy (``is_safe_oauth_metadata_url``)
    All OAuth metadata URLs (PRM, OASM) must pass ``is_safe_oauth_metadata_url``
    before the SDK is allowed to fetch them.  The predicate enforces:
    - Scheme: HTTPS or exact loopback HTTP only.
    - No userinfo component.
    - No private-network target unless the configured MCP endpoint is itself
      loopback.

Design invariants
-----------------
* ``build_runtime_provider`` NEVER passes ``redirect_handler`` to the SDK.
  Tests verify this by inspecting ``provider.context.redirect_handler is None``.
* Token values NEVER appear in this module (OAuthClientMetadata stores only
  policy — scopes, redirect URIs, grant types).
* The SDK owns PKCE, state, iss, resource, and token-exchange.  This module
  owns only metadata construction and transport policy.
"""
from __future__ import annotations

import asyncio
import enum
import ipaddress
import logging
import socket
import webbrowser
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata
from pydantic import AnyUrl

from slm_mcp_hub.auth.models import AuthOAuthConfig

if TYPE_CHECKING:
    from slm_mcp_hub.auth.callback import CallbackServer
    from slm_mcp_hub.auth.token_store import KeyringTokenStorage

logger = logging.getLogger(__name__)

# Loopback host strings accepted as "exact loopback"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# IPv4 private-network prefixes (RFC 1918 + loopback + link-local + CGNAT)
_PRIVATE_NETWORKS_V4: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),   # link-local / IMDS
    ipaddress.IPv4Network("100.64.0.0/10"),    # CGNAT (RFC 6598)
    ipaddress.IPv4Network("127.0.0.0/8"),      # loopback
    ipaddress.IPv4Network("0.0.0.0/8"),        # unspecified / this-network
)

# Keep the old name as an alias so existing internal callers are not broken.
_PRIVATE_NETWORKS = _PRIVATE_NETWORKS_V4

# IPv6 unique-local (ULA): fc00::/7 covers fc00:: and fd00:: (RFC 4193).
# Checked explicitly because Python < 3.11 may not include ULA in is_private.
_IPV6_ULA = ipaddress.IPv6Network("fc00::/7")


class OAuthProviderMode(enum.Enum):
    """Explicit mode for provider construction."""

    RUNTIME = "runtime"
    CLI_LOGIN = "cli_login"


# ---------------------------------------------------------------------------
# Public: URL transport-policy predicate
# ---------------------------------------------------------------------------


def is_safe_oauth_metadata_url(url: str, *, mcp_endpoint: str) -> bool:
    """Return True iff *url* passes the OAuth metadata transport policy.

    Rules (all must pass):
    1. URL is non-empty and parseable.
    2. Scheme is ``https`` — OR — scheme is ``http`` with exact loopback host.
    3. No userinfo (``user:password@host``).
    4. If the host is a private-network address, the *mcp_endpoint* must
       itself be a loopback host (local dev exception).

    Private-network addresses include RFC 1918 (10/8, 172.16/12, 192.168/16),
    link-local (169.254/16), CGNAT (100.64/10), and IPv4-mapped loopback.
    Loopback (127/8) is allowed when the MCP endpoint is also loopback.
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    scheme = parsed.scheme.lower()
    if not scheme:
        return False

    # Block all non-HTTP(S) schemes
    if scheme not in ("http", "https"):
        return False

    # Block userinfo (credentials in URL)
    if parsed.username or parsed.password:
        return False

    host = _canonical_host(parsed.hostname or "")
    if not host:
        return False

    is_loopback_host = host in _LOOPBACK_HOSTS

    # http:// is only allowed for exact loopback
    if scheme == "http" and not is_loopback_host:
        return False

    # Check if the host is a private-network address
    if not is_loopback_host and _is_private_network(host):
        # Allow only when MCP endpoint is itself loopback
        mcp_host = _canonical_host(urlparse(mcp_endpoint).hostname or "")
        if mcp_host not in _LOOPBACK_HOSTS:
            return False

    return True


# ---------------------------------------------------------------------------
# Public: factory functions
# ---------------------------------------------------------------------------


def build_runtime_provider(
    server_url: str,
    auth_config: AuthOAuthConfig,
    storage: "KeyringTokenStorage",
) -> OAuthClientProvider:
    """Construct an ``OAuthClientProvider`` for RUNTIME mode.

    In runtime mode:
    - ``redirect_handler`` is ``None`` — the Hub never opens a browser.
    - ``callback_handler`` is ``None`` — no loopback server is started.
    - When the upstream returns 401 and the SDK needs interactive auth, it
      raises ``OAuthFlowError``.  The Hub catches this and maps it to
      ``AUTH_REQUIRED`` state (not a crash).

    Args:
        server_url:   Canonical MCP endpoint URL.
        auth_config:  OAuth policy (scopes, callback host/port, metadata URL).
        storage:      Keyring-backed ``TokenStorage`` implementation.

    Returns:
        ``OAuthClientProvider`` with ``redirect_handler=None`` and
        ``callback_handler=None``.
    """
    redirect_uri = _default_redirect_uri(auth_config)
    metadata = _build_client_metadata(auth_config, redirect_uri)
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=None,    # NEVER — runtime must not open browser
        callback_handler=None,    # NEVER — no loopback server in runtime
        client_metadata_url=auth_config.client_metadata_url,
    )


def build_login_provider(
    server_url: str,
    auth_config: AuthOAuthConfig,
    storage: "KeyringTokenStorage",
    callback_server: "CallbackServer",
) -> OAuthClientProvider:
    """Construct an ``OAuthClientProvider`` for CLI-LOGIN mode.

    In login mode:
    - ``redirect_handler`` opens the system browser at the authorization URL.
    - ``callback_handler`` is bound to the given ``CallbackServer`` instance.

    This mode is used exclusively by ``slm-hub auth login SERVER``.

    Args:
        server_url:      Canonical MCP endpoint URL.
        auth_config:     OAuth policy.
        storage:         Keyring-backed ``TokenStorage``.
        callback_server: Started ``CallbackServer`` instance (must already be
                         inside its ``async with`` block).

    Returns:
        ``OAuthClientProvider`` with both handlers wired.
    """
    redirect_uri = callback_server.redirect_uri
    metadata = _build_client_metadata(auth_config, redirect_uri)
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=_open_browser_handler,
        callback_handler=callback_server.callback_handler,
        client_metadata_url=auth_config.client_metadata_url,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _default_redirect_uri(auth_config: AuthOAuthConfig) -> str:
    """Build the default redirect URI from the auth config callback settings.

    In runtime mode we have no live port (port=0 stays 0 until a CallbackServer
    starts).  We use port=0 as a placeholder — this URI is stored in client
    registration but is only exercised during CLI login where a real
    CallbackServer provides the actual port.
    """
    host = auth_config.callback_host
    port = auth_config.callback_port
    return f"http://{host}:{port}/callback"


def _build_client_metadata(
    auth_config: AuthOAuthConfig,
    redirect_uri: str,
) -> OAuthClientMetadata:
    """Construct ``OAuthClientMetadata`` from the auth policy.

    This object holds no tokens, secrets, or credentials — only:
    - redirect_uris (list of one)
    - scope (space-separated from config scopes)
    - grant_types
    - application_type (native — required by 2026 DCR spec)
    """
    scope_str = " ".join(auth_config.scopes) if auth_config.scopes else None
    return OAuthClientMetadata(
        redirect_uris=[AnyUrl(redirect_uri)],
        scope=scope_str,
        grant_types=["authorization_code", "refresh_token"],
        application_type="native",
    )


async def _open_browser_handler(authorization_url: str) -> None:
    """Open the system browser at the authorization URL (CLI-login only).

    Runs ``webbrowser.open`` in a thread-pool executor to avoid blocking the
    event loop — browser discovery and subprocess spawning can take > 100 ms.
    """
    # SEC-L-03: log only scheme://host/path — never the query string, which carries
    # the CSRF ``state`` nonce (and other flow params). The full URL still opens.
    from urllib.parse import urlparse  # noqa: PLC0415

    _safe_url = urlparse(authorization_url)._replace(query="", fragment="").geturl()
    logger.info("Opening browser for OAuth authorization: %s", _safe_url)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, webbrowser.open, authorization_url)


def _canonical_host(hostname: str) -> str:
    """Normalize a hostname for comparison."""
    h = hostname.lower().strip()
    # Strip IPv6 brackets if present
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


def _ip_is_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* is a private, reserved, link-local, or otherwise blocked IP.

    IPv4: checked against the explicit ``_PRIVATE_NETWORKS_V4`` list (includes
    RFC 1918, loopback, link-local/IMDS 169.254/16, CGNAT 100.64/10).

    IPv6: checks loopback, link-local (fe80::/10), multicast (ff00::/8),
    reserved, unspecified (::/128), and explicit ULA (fc00::/7) — the ULA
    check uses the literal network object so it works on Python < 3.11 where
    ``is_private`` may not include fc00::/7.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        # IPv4-mapped IPv6 (::ffff:a.b.c.d): re-check the embedded IPv4 against
        # the IPv4 block list so an attacker cannot smuggle 169.254.169.254 or
        # an RFC1918 address past the guard by wrapping it as IPv6.
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return _ip_is_blocked(mapped)
        return bool(
            addr.is_loopback
            or addr.is_private         # ULA + other private ranges (stdlib)
            or addr.is_link_local      # fe80::/10
            or addr.is_multicast       # ff00::/8
            or addr.is_reserved
            or addr.is_unspecified     # ::/128
            or addr in _IPV6_ULA       # fc00::/7  (explicit for Py < 3.11)
        )
    # IPv4 — use the explicit prefix list for maximum portability
    return any(addr in net for net in _PRIVATE_NETWORKS_V4)


def _is_private_network(host: str) -> bool:
    """Return True if *host* is a private/reserved address or resolves to one.

    Covers IPv4 (RFC 1918, loopback 127/8, link-local 169.254/16, CGNAT 100.64/10)
    and IPv6 (ULA fc00::/7, link-local fe80::/10, loopback ::1, multicast, reserved,
    unspecified ::/128).

    For hostnames, performs DNS resolution via ``socket.getaddrinfo`` and rejects if
    ANY resolved IP is private/reserved — this defeats DNS-rebinding attacks where
    an attacker's public hostname briefly resolves to a private IP.

    Fails CLOSED on resolution failure (NXDOMAIN / OSError): a metadata host that
    cannot be resolved cannot be verified as safe, so it is treated as blocked. A
    real, reachable OAuth metadata endpoint must resolve; the loopback-dev
    exception in ``is_safe_oauth_metadata_url`` still applies when the MCP endpoint
    is itself loopback.

    Residual (documented): the guard resolves the host here and httpx resolves it
    again at connect time, so a sub-second DNS-rebinding window remains. Fully
    closing it requires a connect-to-pinned-IP transport (follow-up hardening);
    the all-resolved-IPs check + IPv4-mapped handling + fail-closed substantially
    narrow it for this single-user / local-first release.
    """
    # Fast path: try as a literal IP address first (no DNS involved)
    try:
        addr = ipaddress.ip_address(host)
        return _ip_is_blocked(addr)
    except ValueError:
        pass

    # Hostname: resolve all returned addresses and check each one.
    # Unresolvable (NXDOMAIN, timeout, restricted DNS) → fail CLOSED (treat as
    # blocked): a metadata host we cannot verify must not be fetched.
    try:
        results = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return True

    for _family, _type, _proto, _canonname, sockaddr in results:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_blocked(addr):
            return True

    return False
