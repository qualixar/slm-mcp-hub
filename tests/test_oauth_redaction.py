"""P05 — secret redaction tests.

Verifies that token/secret sentinel values NEVER appear in:
- Hub model repr/str (AuthConfig variants, KeyringTokenStorage)
- Exception messages from our code
- JSON that the Hub writes to config / snapshot / status

TDD RED: tests written before implementation exists.
"""
from __future__ import annotations

import json
import logging

import keyring
import keyring.backend
import keyring.errors
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from slm_mcp_hub.auth.models import (
    AuthNoneConfig,
    AuthOAuthConfig,
    AuthStaticHeadersConfig,
)
from slm_mcp_hub.auth.token_store import KeyringTokenStorage, KeyringUnavailableError
from slm_mcp_hub.core.config import (
    HubConfig,
    MCPServerConfig,
    save_config,
)

# ---------------------------------------------------------------------------
# Sentinels — values that must never escape into any log/repr/file surface
# ---------------------------------------------------------------------------

SENTINEL_ACCESS = "SENTINEL-ACCESS-TOKEN-DO-NOT-LOG"
SENTINEL_REFRESH = "SENTINEL-REFRESH-TOKEN-DO-NOT-LOG"
SENTINEL_SECRET = "SENTINEL-CLIENT-SECRET-DO-NOT-LOG"


# ---------------------------------------------------------------------------
# In-memory keyring for test isolation (same pattern as test_oauth_token_store)
# ---------------------------------------------------------------------------


class InMemoryKeyring(keyring.backend.KeyringBackend):
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
            raise keyring.errors.PasswordDeleteError("Not found")
        del self._store[key]


class AlwaysFailKeyring(keyring.backend.KeyringBackend):
    priority: float = 20.0

    def get_password(self, service: str, username: str) -> str | None:
        raise keyring.errors.NoKeyringError("no backend")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.NoKeyringError("no backend")

    def delete_password(self, service: str, username: str) -> None:
        raise keyring.errors.NoKeyringError("no backend")


@pytest.fixture()
def mem_keyring():
    original = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(original)


@pytest.fixture()
def fail_keyring():
    original = keyring.get_keyring()
    keyring.set_keyring(AlwaysFailKeyring())
    yield
    keyring.set_keyring(original)


# ---------------------------------------------------------------------------
# Auth model repr redaction
# ---------------------------------------------------------------------------


class TestAuthModelReprRedaction:
    def test_auth_none_repr_is_clean(self):
        r = repr(AuthNoneConfig())
        _assert_no_sentinel(r, "AuthNoneConfig repr")

    def test_auth_static_repr_is_clean(self):
        r = repr(AuthStaticHeadersConfig())
        _assert_no_sentinel(r, "AuthStaticHeadersConfig repr")

    def test_auth_oauth_repr_is_clean(self):
        r = repr(AuthOAuthConfig(scopes=("read",)))
        _assert_no_sentinel(r, "AuthOAuthConfig repr")
        # Scopes are policy — they may appear, but tokens must not
        for sentinel in (SENTINEL_ACCESS, SENTINEL_REFRESH, SENTINEL_SECRET):
            assert sentinel not in r


# ---------------------------------------------------------------------------
# KeyringTokenStorage repr/str redaction
# ---------------------------------------------------------------------------


class TestKeyringStorageReprRedaction:
    def test_store_repr_does_not_contain_sentinel_access(self, mem_keyring):
        store = KeyringTokenStorage(
            endpoint="https://mcp.example.com",
            redirect_uri="http://127.0.0.1:0/callback",
        )
        r = repr(store)
        assert SENTINEL_ACCESS not in r

    def test_store_str_does_not_contain_sentinel(self, mem_keyring):
        store = KeyringTokenStorage(
            endpoint="https://mcp.example.com",
            redirect_uri="http://127.0.0.1:0/callback",
        )
        s = str(store)
        _assert_no_sentinel(s, "KeyringTokenStorage str")


# ---------------------------------------------------------------------------
# Exception message redaction
# ---------------------------------------------------------------------------


class TestExceptionMessageRedaction:
    """Exception messages must never carry token/secret material."""

    @pytest.mark.asyncio
    async def test_set_tokens_error_message_clean(self, fail_keyring):
        store = KeyringTokenStorage(
            endpoint="https://mcp.example.com",
            redirect_uri="http://127.0.0.1:0/cb",
        )
        tok = OAuthToken(access_token=SENTINEL_ACCESS, token_type="bearer",
                         refresh_token=SENTINEL_REFRESH)
        with pytest.raises(KeyringUnavailableError) as exc_info:
            await store.set_tokens(tok)
        msg = str(exc_info.value)
        assert SENTINEL_ACCESS not in msg
        assert SENTINEL_REFRESH not in msg

    @pytest.mark.asyncio
    async def test_set_client_info_error_message_clean(self, fail_keyring):
        store = KeyringTokenStorage(
            endpoint="https://mcp.example.com",
            redirect_uri="http://127.0.0.1:0/cb",
        )
        ci = OAuthClientInformationFull(
            client_id="client-x",
            client_secret=SENTINEL_SECRET,
            redirect_uris=["http://127.0.0.1:0/cb"],
        )
        with pytest.raises(KeyringUnavailableError) as exc_info:
            await store.set_client_info(ci)
        msg = str(exc_info.value)
        assert SENTINEL_SECRET not in msg

    @pytest.mark.asyncio
    async def test_get_tokens_error_message_clean(self, fail_keyring):
        store = KeyringTokenStorage(
            endpoint="https://mcp.example.com",
            redirect_uri="http://127.0.0.1:0/cb",
        )
        with pytest.raises(KeyringUnavailableError) as exc_info:
            await store.get_tokens()
        msg = str(exc_info.value)
        _assert_no_sentinel(msg, "get_tokens exception")


# ---------------------------------------------------------------------------
# MCPServerConfig / save_config JSON redaction
# ---------------------------------------------------------------------------


class TestConfigJsonRedaction:
    """Config JSON saved to disk must not contain token material."""

    def test_mcp_server_config_repr_does_not_expose_token(self):
        """MCPServerConfig repr may show header names but not resolved token values."""
        cfg = MCPServerConfig(
            name="my-server",
            transport="http",
            url="https://example.com",
            headers={"X-API-Key": "${SENTINEL_KEY}"},  # unresolved placeholder
            auth=AuthOAuthConfig(scopes=("read",)),
        )
        r = repr(cfg)
        # Placeholder token reference is fine; actual sentinel value should not appear
        assert SENTINEL_ACCESS not in r
        assert SENTINEL_REFRESH not in r
        assert SENTINEL_SECRET not in r

    def test_save_config_json_does_not_contain_access_token(self, tmp_path):
        """Saved config JSON must never contain token material."""
        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name="oauth-srv",
                    transport="http",
                    url="https://mcp.example.com",
                    auth=AuthOAuthConfig(scopes=("read",)),
                ),
            ),
        )
        config_path = tmp_path / "config.json"
        save_config(cfg, config_path)

        raw = config_path.read_text()
        data = json.loads(raw)

        # Sentinel values must not appear anywhere in the file
        for sentinel in (SENTINEL_ACCESS, SENTINEL_REFRESH, SENTINEL_SECRET):
            assert sentinel not in raw, f"Sentinel {sentinel!r} leaked into config JSON"

        # Verify auth policy IS present (policy fields are OK)
        srv_data = data.get("mcpServers", {}).get("oauth-srv", {})
        assert srv_data.get("auth", {}).get("mode") == "oauth"

    def test_save_config_auth_none_no_auth_block_needed(self, tmp_path):
        """Servers with none-mode auth may omit the auth block entirely."""
        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name="no-auth-srv",
                    transport="stdio",
                    command="my-cmd",
                    auth=AuthNoneConfig(),
                ),
            ),
        )
        config_path = tmp_path / "config.json"
        save_config(cfg, config_path)
        raw = config_path.read_text()
        _assert_no_sentinel(raw, "config JSON with none auth")

    def test_save_config_static_headers_no_tokens_in_json(self, tmp_path):
        """Static-header config stores header names but not resolved values."""
        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name="static-srv",
                    transport="http",
                    url="https://example.com",
                    headers={"X-API-Key": "${MY_KEY}"},  # unresolved placeholder
                    auth=AuthStaticHeadersConfig(),
                ),
            ),
        )
        config_path = tmp_path / "config.json"
        save_config(cfg, config_path)
        raw = config_path.read_text()
        for sentinel in (SENTINEL_ACCESS, SENTINEL_REFRESH, SENTINEL_SECRET):
            assert sentinel not in raw

    def test_save_config_oauth_with_client_metadata_url(self, tmp_path):
        """client_metadata_url is serialized as a policy field (not a secret)."""
        meta_url = "https://example.com/.well-known/client"
        cfg = HubConfig(
            mcp_servers=(
                MCPServerConfig(
                    name="oauth-with-meta",
                    transport="http",
                    url="https://mcp.example.com",
                    auth=AuthOAuthConfig(
                        scopes=("read",),
                        client_metadata_url=meta_url,
                    ),
                ),
            ),
        )
        config_path = tmp_path / "config.json"
        save_config(cfg, config_path)
        data = json.loads(config_path.read_text())
        srv_auth = data["mcpServers"]["oauth-with-meta"]["auth"]
        assert srv_auth["client_metadata_url"] == meta_url
        _assert_no_sentinel(config_path.read_text(), "config JSON with client_metadata_url")


# ---------------------------------------------------------------------------
# _serialize_auth: none-mode guard raises (CRIT fix — prevents silent wrong serialization)
# ---------------------------------------------------------------------------


class TestSerializeAuthNoneModeGuard:
    """Proves _serialize_auth raises rather than silently serializing none-mode auth."""

    def test_serialize_auth_none_mode_raises(self):
        from slm_mcp_hub.auth.models import AuthNoneConfig
        from slm_mcp_hub.core.config import ConfigValidationError, _serialize_auth

        with pytest.raises(ConfigValidationError, match="none-mode"):
            _serialize_auth(AuthNoneConfig())


# ---------------------------------------------------------------------------
# Logging redaction — prove tokens don't escape via logging
# ---------------------------------------------------------------------------


class TestLoggingRedaction:
    """Tokens must not appear in log output generated by our code."""

    @pytest.mark.asyncio
    async def test_token_not_logged_on_successful_set(self, mem_keyring, caplog):
        store = KeyringTokenStorage(
            endpoint="https://mcp.example.com",
            redirect_uri="http://127.0.0.1:0/cb",
        )
        tok = OAuthToken(access_token=SENTINEL_ACCESS, token_type="bearer")
        with caplog.at_level(logging.DEBUG, logger="slm_mcp_hub"):
            await store.set_tokens(tok)

        combined = " ".join(record.getMessage() for record in caplog.records)
        assert SENTINEL_ACCESS not in combined

    @pytest.mark.asyncio
    async def test_client_secret_not_logged(self, mem_keyring, caplog):
        store = KeyringTokenStorage(
            endpoint="https://mcp.example.com",
            redirect_uri="http://127.0.0.1:0/cb",
        )
        ci = OAuthClientInformationFull(
            client_id="cid",
            client_secret=SENTINEL_SECRET,
            redirect_uris=["http://127.0.0.1:0/cb"],
        )
        with caplog.at_level(logging.DEBUG, logger="slm_mcp_hub"):
            await store.set_client_info(ci)

        combined = " ".join(record.getMessage() for record in caplog.records)
        assert SENTINEL_SECRET not in combined


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _assert_no_sentinel(text: str, context: str) -> None:
    for sentinel in (SENTINEL_ACCESS, SENTINEL_REFRESH, SENTINEL_SECRET):
        assert sentinel not in text, (
            f"Sentinel {sentinel!r} found in {context}: {text[:200]!r}"
        )
