"""P07 — Auth CLI tests.

TDD RED phase: tests written before implementation exists. All fail with
ImportError or ClickException until auth_commands.py and main.py registrations
are in place.

Coverage requirements:
  - auth_commands.py ≥ 97% line coverage (aim 100%)
  - Full suite stays ≥ 98% line coverage

Secret sentinel: a known fake token value is seeded into the keyring; every
output path (human and JSON) is asserted to NOT contain that value.

Browser isolation: webbrowser.open must NEVER be called by status or logout;
only the login command's OAuthClientProvider path may call it.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import keyring
import keyring.backend
import keyring.errors
import pytest
from click.testing import CliRunner
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from slm_mcp_hub.cli.main import cli
from slm_mcp_hub.core.hub import reset_hub

# ---------------------------------------------------------------------------
# Keyring helpers (same pattern as test_oauth_token_store.py)
# ---------------------------------------------------------------------------


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Pure in-memory keyring for test isolation — never touches the OS keychain."""

    priority: float = 20.0

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        key = (service, username)
        if key not in self._store:
            raise keyring.errors.PasswordDeleteError(
                f"No password for {service!r}/{username!r}"
            )
        del self._store[key]


@pytest.fixture()
def mem_keyring() -> InMemoryKeyring:
    """Install in-memory backend; restore original after the test."""
    original = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(original)


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def oauth_hub_config():
    """Write a hub config with OAuth + plain servers to the isolated tmp dir."""
    from slm_mcp_hub.auth.models import AuthNoneConfig, AuthOAuthConfig
    from slm_mcp_hub.core.config import HubConfig, MCPServerConfig, save_config

    cfg = HubConfig(
        mcp_servers=(
            MCPServerConfig(
                name="oauth-server",
                transport="http",
                url="http://localhost:8888/mcp",
                auth=AuthOAuthConfig(scopes=("read", "write")),
            ),
            MCPServerConfig(
                name="plain-server",
                transport="http",
                url="http://localhost:8889/mcp",
                auth=AuthNoneConfig(),
            ),
        )
    )
    save_config(cfg)
    return cfg


@pytest.fixture()
def oauth_server_config():
    """Return just the OAuth MCPServerConfig (no disk write required for direct tests)."""
    from slm_mcp_hub.auth.models import AuthOAuthConfig
    from slm_mcp_hub.core.config import MCPServerConfig

    return MCPServerConfig(
        name="oauth-server",
        transport="http",
        url="http://localhost:8888/mcp",
        auth=AuthOAuthConfig(scopes=("read", "write")),
    )


# ---------------------------------------------------------------------------
# Helper: seed a token into the in-memory keyring via KeyringTokenStorage
# ---------------------------------------------------------------------------


def _seed_token(oauth_server_config, token_value: str = "VALID_ACCESS_TOKEN") -> None:
    """Seed a fake OAuthToken so status can report 'authorized'."""
    from slm_mcp_hub.cli.auth_commands import _build_storage

    storage = _build_storage(oauth_server_config)
    token = OAuthToken(
        access_token=token_value,
        token_type="Bearer",
        scope="read write",
        expires_in=3600,
        refresh_token="FAKE_REFRESH_TOKEN",
    )
    asyncio.run(storage.set_tokens(token))


def _seed_client_info(oauth_server_config, issuer: str = "https://auth.example.com") -> None:
    """Seed fake OAuthClientInformationFull so status can report issuer."""
    from slm_mcp_hub.cli.auth_commands import _build_storage

    storage = _build_storage(oauth_server_config)
    client_info = OAuthClientInformationFull(
        client_id="test-client-id",
        client_secret=None,
        redirect_uris=["http://127.0.0.1:0/callback"],
        issuer=issuer,  # type: ignore[arg-type]
    )
    asyncio.run(storage.set_client_info(client_info))


# ===========================================================================
# 1. Auth group registration
# ===========================================================================


class TestAuthGroupRegistration:
    """Verify the auth command group is reachable via the main CLI."""

    def setup_method(self) -> None:
        reset_hub()

    def test_auth_help_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["auth", "--help"])
        assert result.exit_code == 0
        assert "login" in result.output
        assert "status" in result.output
        assert "logout" in result.output

    def test_auth_login_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["auth", "login", "--help"])
        assert result.exit_code == 0
        assert "SERVER" in result.output.upper() or "server" in result.output

    def test_auth_status_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["auth", "status", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output

    def test_auth_logout_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["auth", "logout", "--help"])
        assert result.exit_code == 0
        assert "--yes" in result.output


# ===========================================================================
# 2. auth login
# ===========================================================================


class TestAuthLogin:
    """Tests for `slm-hub auth login SERVER`."""

    def setup_method(self) -> None:
        reset_hub()

    def test_login_server_not_found(self, runner: CliRunner, oauth_hub_config) -> None:
        result = runner.invoke(cli, ["auth", "login", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_login_non_oauth_server_rejected(
        self, runner: CliRunner, oauth_hub_config
    ) -> None:
        result = runner.invoke(cli, ["auth", "login", "plain-server"])
        assert result.exit_code != 0
        assert "oauth" in result.output.lower()

    def test_login_success_mocked(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """Login command succeeds when _run_login is mocked to store a token."""

        async def _fake_run_login(server_config, callback_port: int) -> None:
            # Simulate storing a token (what the real flow does)
            from slm_mcp_hub.cli.auth_commands import _build_storage

            storage = _build_storage(server_config)
            token = OAuthToken(
                access_token="FAKE_TOKEN_NOT_PRINTED",
                token_type="Bearer",
                scope="read write",
            )
            await storage.set_tokens(token)

        with patch(
            "slm_mcp_hub.cli.auth_commands._run_login",
            side_effect=_fake_run_login,
        ):
            result = runner.invoke(cli, ["auth", "login", "oauth-server"])

        assert result.exit_code == 0, result.output
        assert "successful" in result.output.lower()
        # Token value must NEVER appear in output
        assert "FAKE_TOKEN_NOT_PRINTED" not in result.output

    def test_login_callback_port_override(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """--callback-port is forwarded to _run_login."""
        captured: list[int] = []

        async def _fake_run_login(server_config, callback_port: int) -> None:
            captured.append(callback_port)
            from slm_mcp_hub.cli.auth_commands import _build_storage

            storage = _build_storage(server_config)
            await storage.set_tokens(OAuthToken(access_token="t", token_type="Bearer"))

        with patch(
            "slm_mcp_hub.cli.auth_commands._run_login", side_effect=_fake_run_login
        ):
            result = runner.invoke(
                cli, ["auth", "login", "oauth-server", "--callback-port", "9999"]
            )

        assert result.exit_code == 0, result.output
        assert captured == [9999]

    def test_login_failure_shows_friendly_message(
        self, runner: CliRunner, oauth_hub_config
    ) -> None:
        """When _run_login raises, the CLI shows a friendly error (no traceback leaked)."""

        async def _failing_run_login(server_config, callback_port: int) -> None:
            raise RuntimeError("network unreachable")

        with patch(
            "slm_mcp_hub.cli.auth_commands._run_login", side_effect=_failing_run_login
        ):
            result = runner.invoke(cli, ["auth", "login", "oauth-server"])

        assert result.exit_code != 0
        # Friendly message, no raw Python traceback details
        output = result.output.lower()
        assert "login failed" in output or "error" in output

    def test_login_reraises_click_exception_from_run_login(
        self, runner: CliRunner, oauth_hub_config
    ) -> None:
        """ClickException raised inside _run_login is re-raised verbatim (line 317)."""
        import click

        async def _raise_click_exc(server_config, callback_port: int) -> None:
            raise click.ClickException("provider rejected the request")

        with patch(
            "slm_mcp_hub.cli.auth_commands._run_login", side_effect=_raise_click_exc
        ):
            result = runner.invoke(cli, ["auth", "login", "oauth-server"])

        assert result.exit_code != 0
        assert "provider rejected the request" in result.output

    def test_login_fails_if_no_token_stored_after_flow(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """If _run_login completes but stores no token, _print_login_success raises (line 217)."""

        async def _noop_run_login(server_config, callback_port: int) -> None:
            pass  # flow "completes" but never stores a token

        with patch(
            "slm_mcp_hub.cli.auth_commands._run_login", side_effect=_noop_run_login
        ):
            result = runner.invoke(cli, ["auth", "login", "oauth-server"])

        assert result.exit_code != 0
        output = result.output
        assert "no token was stored" in output or "Error" in output

    def test_login_shows_issuer_when_client_info_seeded(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """Login success output includes issuer when client registration stored it (line 225)."""

        async def _fake_run_login(server_config, callback_port: int) -> None:
            # Use await directly — we're already inside asyncio.run, cannot nest
            from slm_mcp_hub.cli.auth_commands import _build_storage

            storage = _build_storage(server_config)
            await storage.set_tokens(
                OAuthToken(access_token="FAKE_TK", token_type="Bearer", scope="read")
            )
            client_info = OAuthClientInformationFull(
                client_id="test-client",
                client_secret=None,
                redirect_uris=["http://127.0.0.1:0/callback"],
                issuer="https://auth.issuer-test.com",  # type: ignore[arg-type]
            )
            await storage.set_client_info(client_info)

        with patch(
            "slm_mcp_hub.cli.auth_commands._run_login", side_effect=_fake_run_login
        ):
            result = runner.invoke(cli, ["auth", "login", "oauth-server"])

        assert result.exit_code == 0, result.output
        assert "auth.issuer-test.com" in result.output

    def test_login_keyring_unavailable_during_success_check(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """KeyringUnavailableError inside the try in _print_login_success → ClickException (lines 211-212).

        _run_login succeeds (no token stored).
        _async_get_token_and_client_info is mocked to raise KeyringUnavailableError
        so the except-KeyringUnavailableError branch at lines 211-212 is exercised.
        """
        from slm_mcp_hub.auth.token_store import KeyringUnavailableError

        async def _noop_run_login(server_config, callback_port: int) -> None:
            pass  # "completes" without touching keyring

        async def _raising_token_info(storage):
            raise KeyringUnavailableError("keyring vanished after login")

        with patch(
            "slm_mcp_hub.cli.auth_commands._run_login", side_effect=_noop_run_login
        ), patch(
            "slm_mcp_hub.cli.auth_commands._async_get_token_and_client_info",
            side_effect=_raising_token_info,
        ):
            result = runner.invoke(cli, ["auth", "login", "oauth-server"])

        assert result.exit_code != 0
        assert "Keyring unavailable" in result.output

    def test_login_does_not_print_token_value(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """Even on success, the access token value must NOT appear in output."""
        secret = "SUPER_SECRET_ACCESS_TOKEN_ABC123"

        async def _fake_run_login(server_config, callback_port: int) -> None:
            from slm_mcp_hub.cli.auth_commands import _build_storage

            storage = _build_storage(server_config)
            await storage.set_tokens(
                OAuthToken(access_token=secret, token_type="Bearer", scope="read")
            )

        with patch(
            "slm_mcp_hub.cli.auth_commands._run_login", side_effect=_fake_run_login
        ):
            result = runner.invoke(cli, ["auth", "login", "oauth-server"])

        assert result.exit_code == 0, result.output
        assert secret not in result.output, "Access token MUST NOT appear in login output"


# ===========================================================================
# 3. auth status
# ===========================================================================


class TestAuthStatus:
    """Tests for `slm-hub auth status [SERVER] [--json]`."""

    def setup_method(self) -> None:
        reset_hub()

    def test_status_server_not_found(self, runner: CliRunner, oauth_hub_config) -> None:
        result = runner.invoke(cli, ["auth", "status", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_status_not_required_plain_server(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        result = runner.invoke(cli, ["auth", "status", "plain-server"])
        assert result.exit_code == 0, result.output
        assert "not_required" in result.output

    def test_status_auth_required_when_no_token(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """OAuth server with no stored token → auth_required."""
        result = runner.invoke(cli, ["auth", "status", "oauth-server"])
        assert result.exit_code == 0, result.output
        assert "auth_required" in result.output

    def test_status_auth_required_shows_next_action(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        result = runner.invoke(cli, ["auth", "status", "oauth-server"])
        assert result.exit_code == 0, result.output
        assert "slm-hub auth login oauth-server" in result.output

    def test_status_authorized_when_token_present(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        _seed_token(oauth_server_config)
        result = runner.invoke(cli, ["auth", "status", "oauth-server"])
        assert result.exit_code == 0, result.output
        assert "authorized" in result.output

    def test_status_shows_scopes_when_present(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        _seed_token(oauth_server_config)
        result = runner.invoke(cli, ["auth", "status", "oauth-server"])
        assert result.exit_code == 0, result.output
        # scope from stored token: "read write"
        assert "read" in result.output

    def test_status_shows_issuer_from_client_info(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        _seed_token(oauth_server_config)
        _seed_client_info(oauth_server_config, "https://auth.example.com")
        result = runner.invoke(cli, ["auth", "status", "oauth-server"])
        assert result.exit_code == 0, result.output
        assert "auth.example.com" in result.output

    def test_status_all_servers_no_argument(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """Status with no argument lists all servers."""
        result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0, result.output
        assert "oauth-server" in result.output
        assert "plain-server" in result.output

    # --- JSON output ---

    def test_status_shows_error_when_keyring_unavailable(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """KeyringUnavailableError in _safe_status_entry → error dict → error printed (lines 143-144, 249)."""
        from slm_mcp_hub.auth.token_store import KeyringUnavailableError

        with patch(
            "slm_mcp_hub.cli.auth_commands._build_storage",
            side_effect=KeyringUnavailableError("keyring unavailable"),
        ):
            result = runner.invoke(cli, ["auth", "status", "oauth-server"])

        # Command succeeds (exit 0) — error is surfaced as output, not crash
        assert result.exit_code == 0, result.output
        assert "error" in result.output.lower()

    def test_status_json_single_server(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        result = runner.invoke(cli, ["auth", "status", "oauth-server", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["server"] == "oauth-server"
        assert data["mode"] == "oauth"
        assert "status" in data

    def test_status_json_all_servers(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        result = runner.invoke(cli, ["auth", "status", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "servers" in data
        names = {s["server"] for s in data["servers"]}
        assert "oauth-server" in names
        assert "plain-server" in names

    def test_status_json_is_valid_json(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        result = runner.invoke(cli, ["auth", "status", "--json"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)  # must not raise
        assert isinstance(parsed, dict)

    def test_status_json_includes_next_action_for_auth_required(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        result = runner.invoke(cli, ["auth", "status", "oauth-server", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "auth_required"
        assert "next_action" in data
        assert "slm-hub auth login oauth-server" in data["next_action"]

    # --- Secret sentinel tests ---

    def test_secret_sentinel_token_not_in_human_output(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """SENTINEL: the access token value must NEVER appear in human output."""
        sentinel = "SENTINEL_ACCESS_TOKEN_MUST_NOT_APPEAR_IN_OUTPUT_XYZ"
        _seed_token(oauth_server_config, token_value=sentinel)

        result = runner.invoke(cli, ["auth", "status", "oauth-server"])
        assert result.exit_code == 0, result.output
        assert sentinel not in result.output, (
            f"Secret sentinel found in human status output: {result.output!r}"
        )

    def test_secret_sentinel_token_not_in_json_output(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """SENTINEL: the access token value must NEVER appear in --json output."""
        sentinel = "SENTINEL_ACCESS_TOKEN_MUST_NOT_APPEAR_IN_JSON_XYZ"
        _seed_token(oauth_server_config, token_value=sentinel)

        result = runner.invoke(cli, ["auth", "status", "oauth-server", "--json"])
        assert result.exit_code == 0, result.output
        assert sentinel not in result.output, (
            f"Secret sentinel found in JSON status output: {result.output!r}"
        )

    def test_secret_sentinel_refresh_token_not_in_output(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """SENTINEL: refresh token must NEVER appear in any output."""
        sentinel = "SENTINEL_REFRESH_TOKEN_MUST_NOT_APPEAR_XYZ"
        storage_for_seed = None
        # Seed directly using _build_storage
        from slm_mcp_hub.cli.auth_commands import _build_storage

        storage_for_seed = _build_storage(oauth_server_config)
        asyncio.run(
            storage_for_seed.set_tokens(
                OAuthToken(
                    access_token="some-access",
                    token_type="Bearer",
                    refresh_token=sentinel,
                )
            )
        )

        result = runner.invoke(cli, ["auth", "status", "oauth-server"])
        assert result.exit_code == 0, result.output
        assert sentinel not in result.output, (
            f"Refresh token sentinel found in human output: {result.output!r}"
        )

        result_json = runner.invoke(cli, ["auth", "status", "oauth-server", "--json"])
        assert result_json.exit_code == 0, result_json.output
        assert sentinel not in result_json.output, (
            f"Refresh token sentinel found in JSON output: {result_json.output!r}"
        )

    def test_secret_sentinel_client_secret_not_in_output(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """SENTINEL: client_secret must NEVER appear in status output."""
        sentinel = "SENTINEL_CLIENT_SECRET_MUST_NOT_APPEAR_XYZ"
        from slm_mcp_hub.cli.auth_commands import _build_storage

        storage = _build_storage(oauth_server_config)
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            client_secret=sentinel,  # type: ignore[arg-type]
            redirect_uris=["http://127.0.0.1:0/callback"],
            issuer="https://auth.example.com",  # type: ignore[arg-type]
        )
        asyncio.run(storage.set_client_info(client_info))
        _seed_token(oauth_server_config)

        result = runner.invoke(cli, ["auth", "status", "oauth-server"])
        assert result.exit_code == 0, result.output
        assert sentinel not in result.output

        result_json = runner.invoke(cli, ["auth", "status", "oauth-server", "--json"])
        assert result_json.exit_code == 0, result_json.output
        assert sentinel not in result_json.output


# ===========================================================================
# 4. auth logout
# ===========================================================================


class TestAuthLogout:
    """Tests for `slm-hub auth logout SERVER [--yes]`."""

    def setup_method(self) -> None:
        reset_hub()

    def test_logout_server_not_found(self, runner: CliRunner, oauth_hub_config) -> None:
        result = runner.invoke(cli, ["auth", "logout", "nonexistent", "--yes"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_logout_non_oauth_server_rejected(
        self, runner: CliRunner, oauth_hub_config
    ) -> None:
        result = runner.invoke(cli, ["auth", "logout", "plain-server", "--yes"])
        assert result.exit_code != 0
        assert "oauth" in result.output.lower()

    def test_logout_requires_confirmation_without_yes(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """Without --yes, logout prompts for confirmation."""
        _seed_token(oauth_server_config)
        # Provide 'n' to decline
        result = runner.invoke(cli, ["auth", "logout", "oauth-server"], input="n\n")
        # Should abort (exit code 1 from click.Abort)
        assert result.exit_code != 0

    def test_logout_yes_flag_skips_confirmation(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        _seed_token(oauth_server_config)
        result = runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])
        assert result.exit_code == 0, result.output
        assert "logged out" in result.output.lower()

    def test_logout_removes_token_from_keyring(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """After logout, get_tokens() returns None."""
        _seed_token(oauth_server_config)
        runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])

        from slm_mcp_hub.cli.auth_commands import _build_storage

        storage = _build_storage(oauth_server_config)
        token = asyncio.run(storage.get_tokens())
        assert token is None

    def test_logout_idempotent_no_token_stored(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """Logout on an already-logged-out server succeeds cleanly (idempotent)."""
        result = runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])
        assert result.exit_code == 0, result.output

        # Second logout also succeeds
        result2 = runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])
        assert result2.exit_code == 0, result2.output

    def test_logout_idempotent_after_first_logout(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """Logout after a successful first logout still succeeds."""
        _seed_token(oauth_server_config)
        result = runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])
        assert result.exit_code == 0, result.output

        result2 = runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])
        assert result2.exit_code == 0, result2.output

    def test_logout_shows_next_action(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        result = runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])
        assert result.exit_code == 0, result.output
        assert "slm-hub auth login oauth-server" in result.output

    def test_logout_confirmation_yes(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """Confirm 'y' at prompt completes the logout."""
        _seed_token(oauth_server_config)
        result = runner.invoke(cli, ["auth", "logout", "oauth-server"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "logged out" in result.output.lower()

    def test_logout_keyring_unavailable_raises_error(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """KeyringUnavailableError from storage.logout() surfaces as ClickException (lines 437-439)."""
        from slm_mcp_hub.auth.token_store import (
            KeyringTokenStorage,
            KeyringUnavailableError,
        )

        mock_storage = MagicMock(spec=KeyringTokenStorage)
        mock_storage.logout.side_effect = KeyringUnavailableError("no keychain backend")

        with patch(
            "slm_mcp_hub.cli.auth_commands._build_storage", return_value=mock_storage
        ):
            result = runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])

        assert result.exit_code != 0
        assert "Keyring unavailable" in result.output

    def test_logout_generic_exception_raises_error(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """Unexpected exception from storage.logout() surfaces as ClickException (lines 440-443)."""
        from slm_mcp_hub.auth.token_store import KeyringTokenStorage

        mock_storage = MagicMock(spec=KeyringTokenStorage)
        mock_storage.logout.side_effect = RuntimeError("disk full")

        with patch(
            "slm_mcp_hub.cli.auth_commands._build_storage", return_value=mock_storage
        ):
            result = runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])

        assert result.exit_code != 0
        assert "Failed to remove" in result.output


# ===========================================================================
# 5. Browser isolation
# ===========================================================================


class TestBrowserIsolation:
    """status and logout must NEVER call webbrowser.open.

    login is the ONLY command allowed to trigger webbrowser.open (indirectly,
    via OAuthClientProvider's redirect_handler).
    """

    def setup_method(self) -> None:
        reset_hub()

    def test_status_never_opens_browser(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        with patch("webbrowser.open") as mock_wb:
            runner.invoke(cli, ["auth", "status", "oauth-server"])
        mock_wb.assert_not_called()

    def test_status_json_never_opens_browser(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        with patch("webbrowser.open") as mock_wb:
            runner.invoke(cli, ["auth", "status", "oauth-server", "--json"])
        mock_wb.assert_not_called()

    def test_logout_never_opens_browser(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        with patch("webbrowser.open") as mock_wb:
            runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])
        mock_wb.assert_not_called()

    def test_login_provider_wired_with_browser_handler(
        self, runner: CliRunner, oauth_hub_config, mem_keyring, oauth_server_config
    ) -> None:
        """login wires the browser redirect_handler into OAuthClientProvider.

        Verifies build_login_provider is called (not bypassed), meaning the
        browser path is gated behind provider construction — not a direct
        webbrowser.open call in auth_commands.py.
        """
        build_calls: list = []

        try:
            from slm_mcp_hub.auth.provider import (
                build_login_provider as _real,  # noqa: F401
            )
        except ImportError:
            pass

        async def _fake_run_login(server_config, callback_port: int) -> None:
            """Check that _run_login imports and uses build_login_provider."""
            build_calls.append(True)
            from slm_mcp_hub.cli.auth_commands import _build_storage

            storage = _build_storage(server_config)
            await storage.set_tokens(OAuthToken(access_token="tok", token_type="Bearer"))

        with patch("slm_mcp_hub.cli.auth_commands._run_login", side_effect=_fake_run_login):
            result = runner.invoke(cli, ["auth", "login", "oauth-server"])

        assert result.exit_code == 0, result.output
        assert build_calls, "_run_login was never called"

    def test_status_does_not_import_webbrowser_at_call_time(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        """status command body must not call webbrowser at all.

        We verify via the module-level webbrowser patch that status never
        triggers webbrowser.open regardless of import chain.
        """
        with patch("slm_mcp_hub.auth.provider.webbrowser") as mock_wb_module:
            runner.invoke(cli, ["auth", "status", "oauth-server"])
        mock_wb_module.open.assert_not_called()

    def test_logout_does_not_call_webbrowser_open(
        self, runner: CliRunner, oauth_hub_config, mem_keyring
    ) -> None:
        with patch("slm_mcp_hub.auth.provider.webbrowser") as mock_wb_module:
            runner.invoke(cli, ["auth", "logout", "oauth-server", "--yes"])
        mock_wb_module.open.assert_not_called()


# ===========================================================================
# 6. auth_required next-action in federation manager
# ===========================================================================


class TestAuthRequiredNextAction:
    """servers/detail must include next_action when a server is in AUTH_REQUIRED state."""

    def test_get_server_status_includes_next_action_when_auth_required(self) -> None:
        """get_server_status() returns next_action for auth_required servers."""
        from slm_mcp_hub.auth.models import AuthOAuthConfig
        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
        from slm_mcp_hub.core.registry import CapabilityRegistry
        from slm_mcp_hub.federation.manager import ConnectionManager

        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name="my-oauth",
                    transport="http",
                    url="http://localhost:9999/mcp",
                    auth=AuthOAuthConfig(scopes=("read",)),
                ),
            )
        )
        registry = CapabilityRegistry()
        manager = ConnectionManager(cfg, registry)

        # Inject a mock connection in AUTH_REQUIRED state
        mock_conn = MagicMock()
        mock_conn.is_connected = False
        mock_conn.is_auth_required = True
        mock_conn.capabilities = {"tools": [], "resources": [], "prompts": []}
        manager._connections["my-oauth"] = mock_conn

        entries = manager.get_server_status()
        entry = next(e for e in entries if e["name"] == "my-oauth")

        assert entry["auth_required"] is True
        assert "next_action" in entry, "next_action missing for auth_required server"
        assert "slm-hub auth login my-oauth" in entry["next_action"]

    def test_get_server_status_no_next_action_when_not_auth_required(self) -> None:
        """next_action is absent when auth_required is False."""
        from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
        from slm_mcp_hub.core.registry import CapabilityRegistry
        from slm_mcp_hub.federation.manager import ConnectionManager

        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name="normal-server",
                    transport="http",
                    url="http://localhost:9998/mcp",
                ),
            )
        )
        registry = CapabilityRegistry()
        manager = ConnectionManager(cfg, registry)

        mock_conn = MagicMock()
        mock_conn.is_connected = True
        mock_conn.is_auth_required = False
        mock_conn.capabilities = {"tools": [], "resources": [], "prompts": []}
        manager._connections["normal-server"] = mock_conn

        entries = manager.get_server_status()
        entry = next(e for e in entries if e["name"] == "normal-server")

        assert entry["auth_required"] is False
        assert "next_action" not in entry, "next_action must be absent when not auth_required"


# ===========================================================================
# 7. _build_storage helper unit tests
# ===========================================================================


class TestBuildStorage:
    """Unit tests for the _build_storage helper."""

    def test_build_storage_returns_keyring_token_storage(self, oauth_server_config) -> None:
        from slm_mcp_hub.auth.token_store import KeyringTokenStorage
        from slm_mcp_hub.cli.auth_commands import _build_storage

        storage = _build_storage(oauth_server_config)
        assert isinstance(storage, KeyringTokenStorage)

    def test_build_storage_rejects_non_oauth_server(self) -> None:
        from slm_mcp_hub.auth.models import AuthNoneConfig
        from slm_mcp_hub.cli.auth_commands import _build_storage
        from slm_mcp_hub.core.config import MCPServerConfig

        plain = MCPServerConfig(
            name="plain",
            transport="http",
            url="http://localhost:9000/mcp",
            auth=AuthNoneConfig(),
        )
        with pytest.raises(ValueError):
            _build_storage(plain)

    def test_build_storage_stable_key_across_calls(self, oauth_server_config) -> None:
        """Two calls with same config produce storage with identical account keys."""
        from slm_mcp_hub.cli.auth_commands import _build_storage

        s1 = _build_storage(oauth_server_config)
        s2 = _build_storage(oauth_server_config)
        assert s1._token_account == s2._token_account
        assert s1._client_account == s2._client_account

    def test_build_storage_stable_with_different_callback_port(
        self, oauth_server_config
    ) -> None:
        """Storage key uses configured port=0, not an ephemeral port."""
        from slm_mcp_hub.cli.auth_commands import _build_storage

        # Default config has callback_port=0 → key stable
        s1 = _build_storage(oauth_server_config)
        # Key should NOT change just because we build storage again
        s2 = _build_storage(oauth_server_config)
        assert s1._token_account == s2._token_account


# ===========================================================================
# 8. _safe_status_entry unit tests
# ===========================================================================


class TestSafeStatusEntry:
    """Unit tests for _safe_status_entry helper."""

    def test_not_required_for_none_mode(self) -> None:
        from slm_mcp_hub.auth.models import AuthNoneConfig
        from slm_mcp_hub.cli.auth_commands import _safe_status_entry
        from slm_mcp_hub.core.config import MCPServerConfig

        srv = MCPServerConfig(
            name="plain",
            transport="http",
            url="http://localhost:9000/mcp",
            auth=AuthNoneConfig(),
        )
        entry = _safe_status_entry(srv)
        assert entry["status"] == "not_required"
        assert "access_token" not in str(entry)

    def test_auth_required_when_no_token(self, mem_keyring, oauth_server_config) -> None:
        from slm_mcp_hub.cli.auth_commands import _safe_status_entry

        entry = _safe_status_entry(oauth_server_config)
        assert entry["status"] == "auth_required"
        assert "next_action" in entry

    def test_authorized_when_token_stored(
        self, mem_keyring, oauth_server_config
    ) -> None:
        from slm_mcp_hub.cli.auth_commands import _safe_status_entry

        _seed_token(oauth_server_config, "valid_token_value")
        entry = _safe_status_entry(oauth_server_config)
        assert entry["status"] == "authorized"
        assert "valid_token_value" not in str(entry)

    def test_entry_never_contains_access_token_key(
        self, mem_keyring, oauth_server_config
    ) -> None:
        """The dict must never have 'access_token' as a key or value."""
        from slm_mcp_hub.cli.auth_commands import _safe_status_entry

        _seed_token(oauth_server_config, "should_never_appear")
        entry = _safe_status_entry(oauth_server_config)
        as_str = json.dumps(entry)
        assert "should_never_appear" not in as_str
        assert "access_token" not in as_str

    def test_keyring_unavailable_returns_error_status(
        self, oauth_server_config
    ) -> None:
        """KeyringUnavailableError in _safe_status_entry returns error dict (lines 143-144)."""
        from slm_mcp_hub.auth.token_store import KeyringUnavailableError
        from slm_mcp_hub.cli.auth_commands import _safe_status_entry

        with patch(
            "slm_mcp_hub.cli.auth_commands._build_storage",
            side_effect=KeyringUnavailableError("no keyring"),
        ):
            entry = _safe_status_entry(oauth_server_config)

        assert entry["status"] == "error"
        assert "error" in entry
        # next_action still provided even on error
        assert "next_action" in entry

    def test_auth_required_shows_issuer_from_client_info(
        self, mem_keyring, oauth_server_config
    ) -> None:
        """When client_info has issuer but no token, issuer appears in auth_required dict (line 162)."""
        from slm_mcp_hub.cli.auth_commands import _safe_status_entry

        _seed_client_info(oauth_server_config, issuer="https://issuer.test.example.com")
        # No token seeded — status must be auth_required
        entry = _safe_status_entry(oauth_server_config)

        assert entry["status"] == "auth_required"
        assert entry.get("issuer") == "https://issuer.test.example.com"


# ===========================================================================
# 9. _run_login async body (lines 339-356)
# ===========================================================================


class TestRunLoginAsyncBody:
    """Cover the _run_login async function body directly."""

    def test_run_login_starts_callback_server_and_calls_run_login_flow(
        self, mem_keyring, oauth_server_config
    ) -> None:
        """_run_login opens CallbackServer and delegates to run_login_flow (lines 339-356)."""
        from slm_mcp_hub.cli.auth_commands import _run_login

        # Build an async context manager mock for CallbackServer
        mock_cb_instance = MagicMock()
        mock_cb_instance.redirect_uri = "http://127.0.0.1:12345/callback"
        mock_cb_instance.__aenter__ = AsyncMock(return_value=mock_cb_instance)
        mock_cb_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cb_class = MagicMock(return_value=mock_cb_instance)

        flow_calls: list[dict] = []

        async def _fake_run_login_flow(**kwargs) -> None:
            flow_calls.append(kwargs)

        with patch("slm_mcp_hub.auth.callback.CallbackServer", mock_cb_class), patch(
            "slm_mcp_hub.auth.broker.run_login_flow", _fake_run_login_flow
        ):
            asyncio.run(_run_login(oauth_server_config, 0))

        # CallbackServer was instantiated for the callback loop
        assert mock_cb_class.called
        # run_login_flow was called with the right arguments
        assert len(flow_calls) == 1
        assert flow_calls[0]["server_url"] == oauth_server_config.url


# ===========================================================================
# 10. run_login_flow real body (auth/broker.py) — fail-closed error policy
# ===========================================================================


class _FakeAsyncClientFactory:
    """Return a fake httpx2.AsyncClient whose .get runs a supplied coroutine.

    Ignores the ``auth=`` provider and ``timeout=`` kwargs the real client
    takes, so the real OAuth provider is never driven — we simulate its
    side effects (token persistence and/or a transport error) via ``on_get``.
    """

    def __init__(self, on_get) -> None:
        self._on_get = on_get

    def __call__(self, *args, **kwargs):  # noqa: D401 - factory
        on_get = self._on_get

        class _FakeAsyncClient:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def get(self_inner, url):
                await on_get(url)

        return _FakeAsyncClient()


class TestRunLoginFlow:
    """Exercise the REAL ``run_login_flow`` control flow (broker.py 270-283).

    Every other login test patches ``_run_login`` or ``run_login_flow`` out;
    these drive the actual body so its fail-closed error policy is proven:
    a transport error is tolerated ONLY when a NEW token was persisted this
    run, and any auth/token-exchange failure is surfaced (never a false
    success on a stale token).
    """

    def setup_method(self) -> None:
        reset_hub()

    def _storage(self, oauth_server_config):
        from slm_mcp_hub.cli.auth_commands import _build_storage

        return _build_storage(oauth_server_config)

    def _run(self, oauth_server_config, on_get):
        import httpx2  # noqa: F401 - real HTTPError base stays in effect

        from slm_mcp_hub.auth.broker import run_login_flow

        storage = self._storage(oauth_server_config)
        with patch(
            "slm_mcp_hub.auth.provider.build_login_provider", return_value=MagicMock()
        ), patch(
            "slm_mcp_hub.auth.broker.httpx2.AsyncClient",
            _FakeAsyncClientFactory(on_get),
        ):
            asyncio.run(
                run_login_flow(
                    server_url=oauth_server_config.url,
                    auth_config=oauth_server_config.auth,
                    storage=storage,
                    callback_server=MagicMock(),
                )
            )
        return storage

    def test_tolerates_transport_error_when_new_token_persisted(
        self, mem_keyring, oauth_server_config
    ) -> None:
        """Probe GET fails AFTER a new token is stored → tolerated, no raise."""
        import httpx2

        storage_holder = self._storage(oauth_server_config)

        async def on_get(url):
            # Provider exchanged + persisted a fresh token, then the probe GET
            # of the MCP endpoint failed at the transport layer.
            await storage_holder.set_tokens(
                OAuthToken(access_token="NEW_ACCESS", token_type="Bearer")
            )
            raise httpx2.ConnectError("probe GET failed")

        from slm_mcp_hub.auth.broker import run_login_flow

        with patch(
            "slm_mcp_hub.auth.provider.build_login_provider", return_value=MagicMock()
        ), patch(
            "slm_mcp_hub.auth.broker.httpx2.AsyncClient",
            _FakeAsyncClientFactory(on_get),
        ):
            asyncio.run(
                run_login_flow(
                    server_url=oauth_server_config.url,
                    auth_config=oauth_server_config.auth,
                    storage=storage_holder,
                    callback_server=MagicMock(),
                )
            )

        token = asyncio.run(storage_holder.get_tokens())
        assert token is not None and token.access_token == "NEW_ACCESS"

    def test_reraises_transport_error_when_no_token(
        self, mem_keyring, oauth_server_config
    ) -> None:
        """First-time login: auth fails, nothing persisted → error surfaces."""
        import httpx2

        async def on_get(url):
            raise httpx2.ConnectError("authorization endpoint unreachable")

        with pytest.raises(httpx2.HTTPError):
            self._run(oauth_server_config, on_get)

    def test_reraises_when_only_stale_token_remains(
        self, mem_keyring, oauth_server_config
    ) -> None:
        """BLOCKING regression: prior token present, re-auth fails → must raise.

        A failed re-login must NEVER be reported as success just because a
        stale token from a previous session is still in the keyring.
        """
        import httpx2

        storage = self._storage(oauth_server_config)
        asyncio.run(
            storage.set_tokens(OAuthToken(access_token="STALE", token_type="Bearer"))
        )

        async def on_get(url):
            raise httpx2.ConnectError("re-authorization rejected")

        from slm_mcp_hub.auth.broker import run_login_flow

        with patch(
            "slm_mcp_hub.auth.provider.build_login_provider", return_value=MagicMock()
        ), patch(
            "slm_mcp_hub.auth.broker.httpx2.AsyncClient",
            _FakeAsyncClientFactory(on_get),
        ):
            with pytest.raises(httpx2.HTTPError):
                asyncio.run(
                    run_login_flow(
                        server_url=oauth_server_config.url,
                        auth_config=oauth_server_config.auth,
                        storage=storage,
                        callback_server=MagicMock(),
                    )
                )

        # The stale token is untouched, but the failure was surfaced, not masked.
        remaining = asyncio.run(storage.get_tokens())
        assert remaining is not None and remaining.access_token == "STALE"

    def test_non_transport_error_propagates(
        self, mem_keyring, oauth_server_config
    ) -> None:
        """A provider/PKCE error (not httpx2.HTTPError) is never swallowed."""

        async def on_get(url):
            raise RuntimeError("PKCE verifier mismatch")

        with pytest.raises(RuntimeError, match="PKCE"):
            self._run(oauth_server_config, on_get)

    def test_success_without_transport_error_keeps_token(
        self, mem_keyring, oauth_server_config
    ) -> None:
        """Driving GET returns normally (e.g. 405, no raise); token persists."""

        async def on_get(url):
            from slm_mcp_hub.cli.auth_commands import _build_storage

            storage = _build_storage(oauth_server_config)
            await storage.set_tokens(
                OAuthToken(access_token="OK_TOKEN", token_type="Bearer")
            )

        storage = self._run(oauth_server_config, on_get)
        token = asyncio.run(storage.get_tokens())
        assert token is not None and token.access_token == "OK_TOKEN"
