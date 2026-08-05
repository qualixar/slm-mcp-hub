"""OAuth token broker — cross-process refresh serialization for SLM MCP Hub.

Responsibilities
----------------
1. Build ``httpx2.AsyncClient`` instances pre-loaded with an
   ``OAuthClientProvider`` (runtime mode) for the outbound client layer.
2. Serialize token refreshes across async tasks (in-process ``asyncio.Lock``)
   and across Hub processes (``filelock.FileLock``) using a secret-free lock
   file under the Hub runtime directory.
3. Expose the ``OAuthAuthRequiredError`` sentinel that the outbound layer
   raises (and ``MCPConnection`` catches) when an OAuth challenge cannot be
   satisfied without interactive authorization.

Design invariants
-----------------
* **No credential parameters.** ``build_oauth_http_client`` and
  ``refresh_lock_context`` accept only configuration, paths, and account keys
  — never tokens, secrets, or raw authorization header values.
* **No downstream authz forwarded.** The client is built from
  ``MCPServerConfig`` policy only; there is no API surface through which an
  inbound ``Authorization`` header could reach the upstream MCP server.
* **One refresh attempt per 401.** The SDK ``OAuthClientProvider`` already
  implements the single-refresh-then-full-reauth cycle.  The Hub adds the
  cross-process filelock to prevent duplicate token requests when multiple
  Hub processes race.
* **Lock file contains no secrets.** The lock path is derived from the
  account key (itself a SHA-256 hash of non-secret inputs) — never from a
  token value.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import filelock
import httpx2

from slm_mcp_hub.auth.models import AuthOAuthConfig
from slm_mcp_hub.auth.provider import build_runtime_provider, is_safe_oauth_metadata_url

if TYPE_CHECKING:
    from mcp.client.auth import OAuthClientProvider

    from slm_mcp_hub.auth.token_store import KeyringTokenStorage

logger = logging.getLogger(__name__)

# Sub-directory under the Hub config/runtime dir that holds lock files.
_LOCK_SUBDIR = "oauth-locks"

# In-process per-account-key locks: prevents two async tasks in the same
# process from both attempting a token refresh simultaneously.
# This dict is module-level (singleton per process).
_in_process_locks: dict[str, asyncio.Lock] = {}
_in_process_locks_mu: asyncio.Lock | None = None  # lazy-init (needs event loop)


class OAuthAuthRequiredError(Exception):
    """Raised when OAuth authorization is required but cannot proceed.

    This is a CLEAN, EXPECTED state (equivalent to HTTP 401) — NOT an
    unexpected crash.  ``MCPConnection`` converts it to
    ``ConnectionState.AUTH_REQUIRED``.

    The message MUST NOT contain any token, secret, or credential value.
    """

    def __init__(self, message: str = "OAuth authorization required") -> None:
        super().__init__(message)

    def __repr__(self) -> str:
        return f"OAuthAuthRequiredError({self.args[0]!r})"


# ---------------------------------------------------------------------------
# Internal: SSRF-safe auth wrapper
# ---------------------------------------------------------------------------


class _LockedAuth(httpx2.Auth):
    """Wraps ``OAuthClientProvider`` with cross-process refresh serialization.

    Integration point — how the lock wires into the httpx2 auth protocol
    -----------------------------------------------------------------
    httpx2 drives an auth flow as an async generator: the generator yields
    ``httpx2.Request`` objects (including the token-endpoint request that the
    SDK fires when it needs to refresh), and httpx2 sends each yielded request
    through the *same* ``AsyncClient`` that created the flow.  This means ALL
    requests — the initial resource request, the token-endpoint call, and the
    resource retry — pass through the client's event hooks and through this
    wrapper.

    The lock is acquired *before* the second generator iteration (the 401-
    handling phase) and held until the inner generator is exhausted.  This
    serializes concurrent token-endpoint calls across async tasks (via the
    in-process ``asyncio.Lock``) and across Hub processes (via ``filelock``).

    Context-manager suspension inside the lock
    ------------------------------------------
    Python allows ``yield`` inside ``async with``: the context manager stays
    active while the generator is suspended at the yield, so the lock is held
    throughout the 401-handling exchange (token request + resource retry).
    The lock is released when the inner generator raises ``StopAsyncIteration``
    or when the caller closes the generator (``GeneratorExit``).

    Invariants preserved
    --------------------
    * ``context`` attribute delegates to the inner provider — tests that
      inspect ``provider.context.redirect_handler`` continue to work.
    * ``requires_response_body = True`` tells httpx2 to buffer responses so the
      auth flow can inspect status codes.
    """

    requires_request_body: bool = False
    requires_response_body: bool = True

    def __init__(self, inner: "OAuthClientProvider", lock_path: Path) -> None:
        self._inner = inner
        self._lock_path = lock_path
        self.context = inner.context  # Delegate context for test introspection

    async def async_auth_flow(
        self, request: httpx2.Request
    ):  # → AsyncGenerator[Request, Response]
        """Delegate to inner provider; hold the refresh lock during 401 handling."""
        inner_gen = self._inner.async_auth_flow(request)

        # First iteration: obtain the (possibly token-bearing) outgoing request.
        try:
            outgoing = await inner_gen.__anext__()
        except StopAsyncIteration:
            return

        response = yield outgoing

        # All subsequent iterations (401 handling: token request + retry) run
        # inside the refresh lock to prevent concurrent token-endpoint calls.
        async with refresh_lock_context(self._lock_path):
            while True:
                try:
                    outgoing = await inner_gen.asend(response)
                except StopAsyncIteration:
                    return
                # Lock is held while httpx2 sends this request and we await
                # the response — prevents a second task from starting its own
                # token request before this one completes.
                response = yield outgoing


# ---------------------------------------------------------------------------
# Public: build an httpx2 client with OAuth auth
# ---------------------------------------------------------------------------


def build_oauth_http_client(
    server_url: str,
    auth_config: AuthOAuthConfig,
    storage: "KeyringTokenStorage",
    lock_path: Path | None = None,
) -> httpx2.AsyncClient:
    """Build an ``httpx2.AsyncClient`` with ``OAuthClientProvider`` as auth.

    The returned client is for RUNTIME mode — it has no ``redirect_handler``
    and no ``callback_handler``.  When the upstream returns 401 and the SDK
    cannot satisfy the challenge interactively, it raises
    ``mcp.client.auth.OAuthFlowError``.  The caller (``OutboundClient``) must
    catch this and raise ``OAuthAuthRequiredError``.

    No inbound headers are accepted by this function — there is no API surface
    for forwarding downstream ``Authorization`` or ``Cookie`` headers upstream.

    SSRF guard
    ----------
    Every outgoing request (initial resource request, OAuth metadata discovery,
    token-endpoint call, redirects) is validated by ``is_safe_oauth_metadata_url``
    before the transport layer makes a connection.  Requests to private-network
    addresses (RFC 1918, IMDS 169.254/16, IPv6 ULA fc00::/7, link-local) are
    rejected with ``ValueError``.  httpx2 fires the ``request`` hook on each
    redirect, so this gate covers redirect chains as well.

    Refresh lock
    ------------
    When ``lock_path`` is provided the auth provider is wrapped in
    ``_LockedAuth``, which serializes 401-triggered token refreshes via both an
    in-process ``asyncio.Lock`` and a cross-process ``filelock.FileLock``.  This
    prevents multiple concurrent Hub processes from each racing to exchange a
    stale token for a fresh one.

    Args:
        server_url:   Canonical MCP endpoint URL (no credentials).
        auth_config:  OAuth policy (scopes, callback address, metadata URL).
        storage:      Keyring-backed token storage.
        lock_path:    Optional path to the ``.lock`` file for cross-process
                      refresh serialization (from ``get_refresh_lock_path``).
                      When *None*, no cross-process lock is used.

    Returns:
        ``httpx2.AsyncClient`` configured for OAuth; caller must
        ``await client.aclose()`` or use it as an async context manager.
    """
    provider = build_runtime_provider(
        server_url=server_url,
        auth_config=auth_config,
        storage=storage,
    )

    # Wrap with refresh lock when a lock_path is supplied
    auth: _LockedAuth | "OAuthClientProvider"
    if lock_path is not None:
        auth = _LockedAuth(provider, lock_path)
    else:
        auth = provider

    # SSRF guard: validate every outgoing URL (including redirects) against
    # is_safe_oauth_metadata_url before any network connection is made.
    # The closure captures server_url so the guard can apply the loopback
    # exception correctly (private AS allowed only when MCP is loopback).
    async def _ssrf_guard(request: httpx2.Request) -> None:
        url = str(request.url)
        if not is_safe_oauth_metadata_url(url, mcp_endpoint=server_url):
            raise ValueError(
                f"OAuth request blocked by SSRF policy: host "
                f"{request.url.host!r} is private/reserved or uses an unsafe "
                f"scheme.  server_url={server_url!r}"
            )

    return httpx2.AsyncClient(
        auth=auth,
        event_hooks={"request": [_ssrf_guard]},
    )


# ---------------------------------------------------------------------------
# Public: CLI-login flow runner (keeps httpx2 inside the auth/ layer)
# ---------------------------------------------------------------------------


async def run_login_flow(
    server_url: str,
    auth_config: AuthOAuthConfig,
    storage: "KeyringTokenStorage",
    callback_server: Any,
) -> None:
    """Execute the OAuth PKCE login flow for ``slm-hub auth login``.

    Builds a CLI-login provider (with redirect_handler + callback_handler) and
    fires a GET to the server URL to trigger the full PKCE flow:
        1. Provider calls ``redirect_handler`` → opens system browser.
        2. User authorizes; browser redirects to ``callback_server``.
        3. Provider calls ``callback_handler`` → captures the auth code.
        4. SDK exchanges code + PKCE verifier for tokens.
        5. Tokens are persisted via ``storage.set_tokens()``.

    ``webbrowser.open`` is called inside ``_open_browser_handler``
    (auth/provider.py) — never directly here.

    Error policy (fail-closed). The OAuth exchange runs *during* the driving
    GET, so an ``httpx2`` transport error can mean EITHER that authorization
    failed, OR that the token was already exchanged and persisted and only the
    trailing probe of an MCP endpoint (which need not answer a plain GET)
    failed. We therefore tolerate an ``httpx2.HTTPError`` ONLY when this run
    actually persisted a NEW token; otherwise we re-raise so a genuine auth
    failure is never silently reported as success (even when a stale token from
    a previous login is still in the keyring). Any non-transport exception
    (provider / PKCE / SSRF / authorization errors) always propagates.

    Args:
        server_url:      Canonical MCP endpoint URL.
        auth_config:     OAuth policy.
        storage:         Keyring-backed token storage.
        callback_server: Already-started ``CallbackServer`` instance.
    """
    from slm_mcp_hub.auth.provider import build_login_provider

    provider = build_login_provider(
        server_url=server_url,
        auth_config=auth_config,
        storage=storage,
        callback_server=callback_server,
    )

    prior = await storage.get_tokens()
    prior_access = getattr(prior, "access_token", None)

    try:
        async with httpx2.AsyncClient(auth=provider, timeout=130.0) as client:
            await client.get(server_url)
    except httpx2.HTTPError:
        # Tolerate a transport error on the driving GET only if THIS run
        # exchanged and stored a new token; otherwise surface the failure so a
        # failed (re-)login can never look like success on a stale token.
        current = await storage.get_tokens()
        current_access = getattr(current, "access_token", None)
        if current_access is None or current_access == prior_access:
            raise


# ---------------------------------------------------------------------------
# Public: cross-process refresh lock
# ---------------------------------------------------------------------------


def get_refresh_lock_path(config_dir: Path, account_key: str) -> Path:
    """Return the filelock path for *account_key* inside *config_dir*.

    The path is:  ``<config_dir>/<_LOCK_SUBDIR>/<account_key>.lock``

    The lock file contains no secrets — ``account_key`` is a SHA-256 hex
    digest of non-secret inputs (schema version, profile ID, endpoint,
    redirect URI).

    Args:
        config_dir:   Hub runtime/config directory (e.g. ``~/.slm-mcp-hub``).
        account_key:  Hex SHA-256 account key (safe to use as filename).

    Returns:
        ``Path`` to the ``.lock`` file (parent directory may not exist yet).
    """
    return config_dir / _LOCK_SUBDIR / f"{account_key}.lock"


@asynccontextmanager
async def refresh_lock_context(lock_path: Path) -> AsyncIterator[None]:
    """Async context manager that serializes token refreshes.

    Provides two levels of serialization:
    1. **In-process**: an ``asyncio.Lock`` keyed by *lock_path* ensures that
       concurrent coroutines in the same Hub process take turns.
    2. **Cross-process**: a ``filelock.FileLock`` ensures that concurrent Hub
       processes (e.g., one HTTP Hub and one stdio Hub) don't both refresh the
       same token simultaneously.

    The lock file is created automatically (including parent directories).
    The lock is always released, even if the body raises.

    Args:
        lock_path: Path to the ``.lock`` file (from ``get_refresh_lock_path``).

    Yields:
        Nothing — caller executes inside the lock.
    """
    # Ensure parent directory exists
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # In-process lock (asyncio-safe)
    in_process_lock = await _get_in_process_lock(str(lock_path))

    async with in_process_lock:
        # Cross-process lock (blocking acquire, but wrapped in executor to avoid
        # blocking the event loop during the brief file-lock acquisition).
        # thread_local=False: lock state is shared across threads so that
        # acquire() from an executor thread and release() from the main
        # event-loop thread operate on the same lock object.
        fl = filelock.FileLock(str(lock_path), timeout=30, thread_local=False)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, fl.acquire)
        try:
            yield
        finally:
            try:
                fl.release()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _get_in_process_lock(key: str) -> asyncio.Lock:
    """Return (or create) the per-process asyncio.Lock for *key*.

    Uses a module-level dict guarded by a bootstrap lock to ensure each key
    gets exactly one ``asyncio.Lock`` object per process lifetime.
    """
    global _in_process_locks_mu
    # Lazy-init the mutex on first call (requires a running event loop)
    if _in_process_locks_mu is None:
        _in_process_locks_mu = asyncio.Lock()

    async with _in_process_locks_mu:
        if key not in _in_process_locks:
            _in_process_locks[key] = asyncio.Lock()
        return _in_process_locks[key]
