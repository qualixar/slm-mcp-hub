"""Auth CLI commands for SLM MCP Hub.

Three sub-commands under the ``auth`` group:

    slm-hub auth login SERVER [--callback-port PORT]
        Opens the system browser for OAuth2 PKCE authorization.
        This is the ONLY code path that calls ``webbrowser.open`` — all
        browser interaction is gated behind OAuthClientProvider construction
        inside ``_run_login``.  Tokens are stored in the OS keychain.

    slm-hub auth status [SERVER] [--json]
        Shows auth mode, current state, issuer, scopes, and expiry state.
        NEVER reveals token values, client secrets, auth codes, or any
        credential material.  Provides a ``next_action`` hint for servers
        in ``auth_required`` state.

    slm-hub auth logout SERVER [--yes]
        Removes stored OAuth tokens and client registration from the OS
        keychain.  Idempotent: succeeds cleanly even if already logged out.
        NEVER opens a browser.

Security invariants
-------------------
* ``login`` is the ONLY command path allowed to trigger ``webbrowser.open``.
  ``status`` and ``logout`` must never open a browser (enforced by tests).
* No command ever prints token values, client secrets, auth codes, PKCE
  verifiers, or any credential material in human or JSON output.
* ``logout`` is idempotent — two consecutive calls both succeed.
* ``_build_storage`` uses the configured ``callback_port`` (default 0) so the
  keyring account key is stable across login / status / logout invocations,
  even though each ``login`` run may use a different ephemeral port for the
  actual OAuth redirect callback.
* HTTP client library usage is confined to the auth/ adapter layer.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import click

from slm_mcp_hub.auth.models import (
    AuthNoneConfig,
    AuthOAuthConfig,
    AuthStaticHeadersConfig,
)
from slm_mcp_hub.auth.token_store import KeyringTokenStorage, KeyringUnavailableError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command group
# ---------------------------------------------------------------------------


@click.group("auth")
def auth() -> None:
    """OAuth authentication management for federated MCP servers."""


# ---------------------------------------------------------------------------
# Internal helpers (exported for testing)
# ---------------------------------------------------------------------------


def _get_config():  # type: ignore[return]  # returns HubConfig
    """Load hub config from disk."""
    from slm_mcp_hub.core.config import load_config

    return load_config()


def _find_server(server_name: str):  # type: ignore[return]  # returns MCPServerConfig
    """Find MCPServerConfig by name; raise ClickException if absent."""
    config = _get_config()
    for s in config.mcp_servers:
        if s.name == server_name:
            return s
    raise click.ClickException(
        f"Server {server_name!r} not found in config. "
        f"Use 'slm-hub config show' to list configured servers."
    )


def _build_storage(server_config: Any) -> KeyringTokenStorage:
    """Build ``KeyringTokenStorage`` for *server_config* with a stable account key.

    Uses the configured ``callback_port`` (default 0) so the keyring slot is
    identical across login / status / logout even though each login flow uses
    an ephemeral OS-assigned port for the actual OAuth redirect.

    Raises ``ValueError`` if the server is not in OAuth mode.
    """
    auth = server_config.auth
    if not isinstance(auth, AuthOAuthConfig):
        raise ValueError(
            f"Server {server_config.name!r} is not in OAuth mode "
            f"(mode={auth.mode.value!r}). Only OAuth servers use token storage."
        )
    # Stable redirect_uri: uses configured callback_port (default=0).
    # The actual OAuth callback happens on an ephemeral port chosen by
    # CallbackServer, but that port must NOT affect the storage key.
    redirect_uri = f"http://{auth.callback_host}:{auth.callback_port}/callback"
    return KeyringTokenStorage(endpoint=server_config.url, redirect_uri=redirect_uri)


async def _async_get_token_and_client_info(
    storage: KeyringTokenStorage,
) -> tuple[Any, Any]:
    """Fetch stored token then client-info within one async block (sequential)."""
    token = await storage.get_tokens()
    client_info = await storage.get_client_info()
    return token, client_info


def _safe_status_entry(server_config: Any) -> dict[str, Any]:
    """Return a secret-free status dict for *server_config*.

    This function NEVER includes token values, client secrets, authorization
    codes, PKCE verifiers, or any other credential material.  It is safe to
    print or serialise in any format.

    Possible ``status`` values:
    * ``"not_required"`` — auth mode is ``none`` or ``static_headers``.
    * ``"auth_required"`` — OAuth mode but no valid token stored.
    * ``"authorized"``   — OAuth mode with a stored token present.
    * ``"error"``        — keyring backend unavailable or corrupt.
    """
    auth_cfg = server_config.auth
    mode: str = auth_cfg.mode.value

    if isinstance(auth_cfg, (AuthNoneConfig, AuthStaticHeadersConfig)):
        return {"server": server_config.name, "mode": mode, "status": "not_required"}

    # OAuth mode — inspect keyring
    assert isinstance(auth_cfg, AuthOAuthConfig)
    try:
        storage = _build_storage(server_config)
        token, client_info = asyncio.run(_async_get_token_and_client_info(storage))
    except KeyringUnavailableError as exc:
        return {
            "server": server_config.name,
            "mode": mode,
            "status": "error",
            "error": str(exc),
            "next_action": f"slm-hub auth login {server_config.name}",
        }

    if token is None:
        entry: dict[str, Any] = {
            "server": server_config.name,
            "mode": mode,
            "status": "auth_required",
            "next_action": f"slm-hub auth login {server_config.name}",
        }
        if auth_cfg.scopes:
            entry["scopes"] = list(auth_cfg.scopes)
        if client_info is not None and client_info.issuer:
            entry["issuer"] = str(client_info.issuer)
        return entry

    # Token is present — build a safe summary.  Access token value is NEVER included.
    entry = {"server": server_config.name, "mode": mode, "status": "authorized"}

    # Issuer from stored client registration (not a credential)
    if client_info is not None and client_info.issuer:
        entry["issuer"] = str(client_info.issuer)

    # Scope: what was actually granted (not a secret)
    granted_scope: str | None = getattr(token, "scope", None)
    if granted_scope:
        entry["scopes"] = granted_scope.split()
    elif auth_cfg.scopes:
        entry["scopes"] = list(auth_cfg.scopes)

    # Token type: e.g. "Bearer" (not a secret)
    token_type: str | None = getattr(token, "token_type", None)
    if token_type:
        entry["token_type"] = token_type

    # Expiry: expires_in is a delta from issuance; no absolute timestamp stored.
    expires_in: int | None = getattr(token, "expires_in", None)
    if expires_in is not None:
        entry["expires_in_seconds_from_issuance"] = expires_in
    entry["expiry_note"] = (
        f"No absolute timestamp stored. "
        f"Run 'slm-hub auth login {server_config.name}' to renew."
    )

    # Refresh token: boolean presence only — value is NEVER shown
    entry["has_refresh_token"] = getattr(token, "refresh_token", None) is not None

    return entry


def _print_login_success(server_config: Any) -> None:
    """Verify token was stored and print safe success summary.

    Called from ``login`` after ``_run_login`` completes.
    NEVER prints token values, client secrets, or any credential material.
    """
    auth_cfg = server_config.auth
    assert isinstance(auth_cfg, AuthOAuthConfig)

    storage = _build_storage(server_config)
    try:
        token, client_info = asyncio.run(_async_get_token_and_client_info(storage))
    except KeyringUnavailableError as exc:
        raise click.ClickException(
            f"Keyring unavailable after login for {server_config.name!r}: {exc}"
        ) from exc

    if token is None:
        raise click.ClickException(
            "Login completed but no token was stored. "
            "The authorization server may have rejected the request. "
            f"Run 'slm-hub auth status {server_config.name}' to check."
        )

    click.echo(f"\nLogin successful for {server_config.name!r}.")
    if client_info is not None and client_info.issuer:
        click.echo(f"  Issuer: {client_info.issuer}")
    granted_scope: str | None = getattr(token, "scope", None)
    if granted_scope:
        click.echo(f"  Granted scopes: {granted_scope}")
    elif auth_cfg.scopes:
        click.echo(f"  Requested scopes: {' '.join(auth_cfg.scopes)}")
    click.echo("  Token stored securely in OS keychain.")
    click.echo(f"  Run 'slm-hub auth status {server_config.name}' to verify.")


def _print_status_entry(entry: dict[str, Any]) -> None:
    """Human-readable single-server status.  NEVER prints token values."""
    server = entry.get("server", "?")
    mode = entry.get("mode", "?")
    state = entry.get("status", "?")

    click.echo(f"Server: {server}")
    click.echo(f"  Mode:   {mode}")
    click.echo(f"  Status: {state}")

    if state == "not_required":
        return

    if state == "error":
        click.echo(f"  Error: {entry.get('error', 'unknown')}")

    issuer = entry.get("issuer")
    if issuer:
        click.echo(f"  Issuer: {issuer}")

    scopes = entry.get("scopes")
    if scopes:
        click.echo(f"  Scopes: {' '.join(scopes)}")

    token_type = entry.get("token_type")
    if token_type:
        click.echo(f"  Token type: {token_type}")

    expires_in = entry.get("expires_in_seconds_from_issuance")
    if expires_in is not None:
        click.echo(f"  Expires-in (from issuance): {expires_in}s")

    expiry_note = entry.get("expiry_note")
    if expiry_note:
        click.echo(f"  Expiry note: {expiry_note}")

    has_refresh = entry.get("has_refresh_token")
    if has_refresh is not None:
        click.echo(f"  Refresh token: {'yes' if has_refresh else 'no'}")

    next_action = entry.get("next_action")
    if next_action:
        click.echo(f"  Next action: {next_action}")


# ---------------------------------------------------------------------------
# auth login
# ---------------------------------------------------------------------------


@auth.command("login")
@click.argument("server_name")
@click.option(
    "--callback-port",
    type=int,
    default=0,
    metavar="PORT",
    help="Local callback port (default: OS-assigned ephemeral port).",
)
def login(server_name: str, callback_port: int) -> None:
    """Authenticate with an OAuth-protected MCP server.

    Opens the system browser for OAuth2 PKCE authorization.  Tokens are
    stored securely in the OS keychain via KeyringTokenStorage.

    This is the ONLY slm-hub command that opens the system browser.
    Run 'slm-hub auth status SERVER' to verify the result.
    """
    server_config = _find_server(server_name)

    if not isinstance(server_config.auth, AuthOAuthConfig):
        raise click.ClickException(
            f"Server {server_name!r} uses auth mode "
            f"{server_config.auth.mode.value!r}; "
            f"only servers with auth.mode='oauth' require login."
        )

    click.echo(f"Starting OAuth login for {server_name!r}...")

    try:
        asyncio.run(_run_login(server_config, callback_port))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(
            f"Login failed ({type(exc).__name__}). "
            f"Run 'slm-hub auth status {server_name}' to check state."
        ) from exc

    # Verify token was stored and print safe success (no token value ever printed)
    _print_login_success(server_config)


async def _run_login(server_config: Any, callback_port: int) -> None:
    """Execute the OAuth PKCE flow for CLI login.

    Starts a loopback callback server, then delegates the full provider
    and HTTP client flow to ``run_login_flow`` in the auth adapter layer.
    ``webbrowser.open`` is called ONLY inside ``_open_browser_handler``
    (auth/provider.py) which is wired as the provider's redirect_handler.

    This function never calls ``webbrowser.open`` directly.
    The HTTP client library is confined to the auth/ adapter layer.
    """
    from slm_mcp_hub.auth.broker import run_login_flow
    from slm_mcp_hub.auth.callback import CallbackServer

    auth_cfg = server_config.auth
    assert isinstance(auth_cfg, AuthOAuthConfig)

    storage = _build_storage(server_config)
    host = auth_cfg.callback_host
    port = callback_port or auth_cfg.callback_port

    async with CallbackServer(host=host, port=port) as cb:
        click.echo(f"  Callback listening on {cb.redirect_uri}")
        click.echo("  Opening system browser for authorization...")
        click.echo("  Waiting for authorization callback (120 second timeout)...")

        # run_login_flow builds the provider, triggers browser + callback,
        # and stores the token — all inside auth/broker.py.
        await run_login_flow(
            server_url=server_config.url,
            auth_config=auth_cfg,
            storage=storage,
            callback_server=cb,
        )


# ---------------------------------------------------------------------------
# auth status
# ---------------------------------------------------------------------------


@auth.command("status")
@click.argument("server_name", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def status(server_name: str | None, as_json: bool) -> None:
    """Show auth status for one or all configured MCP servers.

    Displays auth mode, current state (not_required / auth_required /
    authorized / error), issuer, granted scopes, token type, expiry notes,
    and whether a refresh token is stored.

    NEVER reveals token values, client secrets, or auth codes.
    For servers in auth_required state, shows the login command to run.
    """
    if server_name is not None:
        server_config = _find_server(server_name)
        entry = _safe_status_entry(server_config)
        if as_json:
            click.echo(json.dumps(entry, indent=2))
        else:
            _print_status_entry(entry)
        return

    # All servers
    config = _get_config()
    entries = [_safe_status_entry(s) for s in config.mcp_servers]
    if as_json:
        click.echo(json.dumps({"servers": entries}, indent=2))
    else:
        for entry in entries:
            _print_status_entry(entry)
            click.echo("")  # blank line between servers


# ---------------------------------------------------------------------------
# auth logout
# ---------------------------------------------------------------------------


@auth.command("logout")
@click.argument("server_name")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def logout(server_name: str, yes: bool) -> None:
    """Remove stored OAuth tokens for a server from the OS keychain.

    Removes both the access/refresh token and the client registration.
    Idempotent: safe to run multiple times; succeeds even if already logged out.
    NEVER opens a browser.

    To re-authenticate: slm-hub auth login SERVER
    """
    server_config = _find_server(server_name)

    if not isinstance(server_config.auth, AuthOAuthConfig):
        raise click.ClickException(
            f"Server {server_name!r} uses auth mode "
            f"{server_config.auth.mode.value!r}; "
            f"only OAuth servers have stored tokens to remove."
        )

    if not yes:
        click.confirm(
            f"Remove stored OAuth tokens for {server_name!r}?",
            abort=True,
        )

    try:
        storage = _build_storage(server_config)
        storage.logout()  # idempotent — silently succeeds if already absent
    except KeyringUnavailableError as exc:
        raise click.ClickException(
            f"Keyring unavailable for {server_name!r}: {exc}. "
            "Tokens cannot be removed without a secure keychain backend."
        ) from exc
    except Exception as exc:
        raise click.ClickException(
            f"Failed to remove tokens for {server_name!r} "
            f"({type(exc).__name__})."
        ) from exc

    click.echo(f"Logged out of {server_name!r}. Tokens removed from OS keychain.")
    click.echo(f"To re-authenticate: slm-hub auth login {server_name}")
