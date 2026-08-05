"""P05 — tests for KeyringTokenStorage.

TDD RED: tests written before implementation exists.
Gate: 100% line coverage, ≥95% branch coverage on auth/token_store.py.
"""
from __future__ import annotations

import keyring
import keyring.backend
import keyring.errors
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from slm_mcp_hub.auth.token_store import KeyringTokenStorage, KeyringUnavailableError

# ---------------------------------------------------------------------------
# In-memory keyring for test isolation — NEVER touches the OS keychain
# ---------------------------------------------------------------------------


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Pure-in-memory keyring backend. Thread-unsafe; single-test scope only."""

    priority: float = 20.0  # Outrank real backends when set explicitly

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
                f"No password found for {service!r}/{username!r}"
            )
        del self._store[key]


class AlwaysFailKeyring(keyring.backend.KeyringBackend):
    """Raises NoKeyringError on every operation — simulates absent secure backend."""

    priority: float = 20.0

    def get_password(self, service: str, username: str) -> str | None:
        raise keyring.errors.NoKeyringError("No secure backend available (test stub)")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.NoKeyringError("No secure backend available (test stub)")

    def delete_password(self, service: str, username: str) -> None:
        raise keyring.errors.NoKeyringError("No secure backend available (test stub)")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_keyring() -> InMemoryKeyring:
    """Install in-memory backend and restore the original after the test."""
    original = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(original)


@pytest.fixture()
def fail_keyring() -> AlwaysFailKeyring:
    """Install always-fail backend and restore after the test."""
    original = keyring.get_keyring()
    backend = AlwaysFailKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(original)


def _make_store(
    endpoint: str = "https://mcp.example.com",
    redirect_uri: str = "http://127.0.0.1:0/callback",
    profile_id: str = "default",
) -> KeyringTokenStorage:
    return KeyringTokenStorage(
        endpoint=endpoint,
        redirect_uri=redirect_uri,
        profile_id=profile_id,
    )


def _make_token(access_token: str = "test-access-token") -> OAuthToken:
    return OAuthToken(access_token=access_token, token_type="bearer")


def _make_client_info(
    client_id: str = "client-123",
    issuer: str = "https://auth.example.com",
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=["http://127.0.0.1:0/callback"],
        issuer=issuer,
    )


# ---------------------------------------------------------------------------
# Basic get/set round-trips
# ---------------------------------------------------------------------------


class TestTokenRoundTrip:
    @pytest.mark.asyncio
    async def test_get_tokens_empty_returns_none(self, mem_keyring):
        store = _make_store()
        result = await store.get_tokens()
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_tokens(self, mem_keyring):
        store = _make_store()
        token = _make_token("my-access-token")
        await store.set_tokens(token)
        retrieved = await store.get_tokens()
        assert retrieved is not None
        assert retrieved.access_token == "my-access-token"

    @pytest.mark.asyncio
    async def test_get_client_info_empty_returns_none(self, mem_keyring):
        store = _make_store()
        result = await store.get_client_info()
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_client_info(self, mem_keyring):
        store = _make_store()
        info = _make_client_info("cid-001", "https://auth.example.com")
        await store.set_client_info(info)
        retrieved = await store.get_client_info()
        assert retrieved is not None
        assert retrieved.client_id == "cid-001"
        assert retrieved.issuer == "https://auth.example.com"

    @pytest.mark.asyncio
    async def test_tokens_preserve_optional_fields(self, mem_keyring):
        store = _make_store()
        token = OAuthToken(
            access_token="tok",
            token_type="bearer",
            expires_in=3600,
            scope="read write",
            refresh_token="ref-tok",
        )
        await store.set_tokens(token)
        result = await store.get_tokens()
        assert result is not None
        assert result.expires_in == 3600
        assert result.scope == "read write"
        assert result.refresh_token == "ref-tok"

    @pytest.mark.asyncio
    async def test_overwrite_tokens(self, mem_keyring):
        store = _make_store()
        await store.set_tokens(_make_token("old-token"))
        await store.set_tokens(_make_token("new-token"))
        result = await store.get_tokens()
        assert result is not None
        assert result.access_token == "new-token"


# ---------------------------------------------------------------------------
# Account key isolation
# ---------------------------------------------------------------------------


class TestAccountKeyIsolation:
    @pytest.mark.asyncio
    async def test_different_endpoints_are_isolated(self, mem_keyring):
        store_a = _make_store(endpoint="https://mcp-a.example.com")
        store_b = _make_store(endpoint="https://mcp-b.example.com")
        await store_a.set_tokens(_make_token("token-for-a"))
        result = await store_b.get_tokens()
        assert result is None

    @pytest.mark.asyncio
    async def test_different_redirect_uris_are_isolated(self, mem_keyring):
        store_a = _make_store(redirect_uri="http://127.0.0.1:8080/callback")
        store_b = _make_store(redirect_uri="http://127.0.0.1:9090/callback")
        await store_a.set_tokens(_make_token("tok-a"))
        assert await store_b.get_tokens() is None

    @pytest.mark.asyncio
    async def test_different_profile_ids_are_isolated(self, mem_keyring):
        store_a = _make_store(profile_id="profile-1")
        store_b = _make_store(profile_id="profile-2")
        await store_a.set_tokens(_make_token("tok-profile1"))
        assert await store_b.get_tokens() is None

    @pytest.mark.asyncio
    async def test_same_params_share_storage(self, mem_keyring):
        """Two stores with identical params point to the same keychain slot."""
        store_a = _make_store()
        store_b = _make_store()  # identical params
        await store_a.set_tokens(_make_token("shared-token"))
        result = await store_b.get_tokens()
        assert result is not None
        assert result.access_token == "shared-token"


# ---------------------------------------------------------------------------
# Account key does NOT depend on client_id
# ---------------------------------------------------------------------------


class TestAccountKeyIndependenceFromClientId:
    """The account key must be derivable before DCR; client_id unknown then."""

    @pytest.mark.asyncio
    async def test_lookup_succeeds_before_client_info_is_set(self, mem_keyring):
        """get_client_info returns None even without knowing client_id."""
        store = _make_store()
        # client_id is not needed to construct the store or call get_client_info
        result = await store.get_client_info()
        assert result is None

    @pytest.mark.asyncio
    async def test_client_info_retrievable_after_dcr(self, mem_keyring):
        """After DCR sets client_id, subsequent restarts can retrieve it."""
        store1 = _make_store()
        info = _make_client_info("dcr-generated-id")
        await store1.set_client_info(info)

        # Simulate restart: create a new store with same params (client_id not needed)
        store2 = _make_store()  # same endpoint, redirect_uri, profile_id
        retrieved = await store2.get_client_info()
        assert retrieved is not None
        assert retrieved.client_id == "dcr-generated-id"

    @pytest.mark.asyncio
    async def test_account_key_is_deterministic(self, mem_keyring):
        """Same inputs always produce the same account key."""
        s1 = _make_store(endpoint="https://example.com", redirect_uri="http://127.0.0.1:0/cb")
        s2 = _make_store(endpoint="https://example.com", redirect_uri="http://127.0.0.1:0/cb")
        # Internal key must match — proven by shared storage
        await s1.set_tokens(_make_token("det-tok"))
        assert (await s2.get_tokens()) is not None


# ---------------------------------------------------------------------------
# Issuer / resource / redirect binding-change invalidation
# ---------------------------------------------------------------------------


class TestBindingChangeInvalidation:
    @pytest.mark.asyncio
    async def test_issuer_change_clears_stored_tokens(self, mem_keyring):
        """When client_info is updated with a new issuer, stored tokens are cleared."""
        store = _make_store()
        token = _make_token("old-tok")
        info_a = _make_client_info("cid", "https://issuer-A.example.com")

        await store.set_tokens(token)
        await store.set_client_info(info_a)
        assert await store.get_tokens() is not None  # tokens present

        # SDK discovers issuer changed — sets new client_info
        info_b = _make_client_info("cid-new", "https://issuer-B.example.com")
        await store.set_client_info(info_b)

        # Tokens must be cleared; new client_info stored
        assert await store.get_tokens() is None
        new_info = await store.get_client_info()
        assert new_info is not None
        assert new_info.issuer == "https://issuer-B.example.com"

    @pytest.mark.asyncio
    async def test_same_issuer_preserves_tokens(self, mem_keyring):
        """Updating client_info with same issuer must NOT clear tokens."""
        store = _make_store()
        await store.set_tokens(_make_token("keep-tok"))
        info = _make_client_info("cid", "https://auth.example.com")
        await store.set_client_info(info)
        # Update with same issuer
        info_v2 = _make_client_info("cid", "https://auth.example.com")
        await store.set_client_info(info_v2)

        result = await store.get_tokens()
        assert result is not None
        assert result.access_token == "keep-tok"

    @pytest.mark.asyncio
    async def test_redirect_change_gives_different_storage(self, mem_keyring):
        """Different redirect_uri → different account key → naturally isolated."""
        store_a = _make_store(redirect_uri="http://127.0.0.1:8080/callback")
        store_b = _make_store(redirect_uri="http://127.0.0.1:9090/callback")
        await store_a.set_tokens(_make_token("tok-a"))
        # store_b uses different account key; old tokens are invisible
        assert await store_b.get_tokens() is None

    @pytest.mark.asyncio
    async def test_no_existing_client_info_before_issuer_update(self, mem_keyring):
        """set_client_info when no prior entry must succeed without clearing tokens."""
        store = _make_store()
        await store.set_tokens(_make_token("my-tok"))
        # No prior client_info — setting it should NOT clear tokens
        info = _make_client_info("cid", "https://auth.example.com")
        await store.set_client_info(info)
        result = await store.get_tokens()
        assert result is not None
        assert result.access_token == "my-tok"


# ---------------------------------------------------------------------------
# Fail closed — no secure backend
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_set_tokens_fails_closed_on_no_backend(self, fail_keyring):
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            await store.set_tokens(_make_token())

    @pytest.mark.asyncio
    async def test_get_tokens_fails_closed_on_no_backend(self, fail_keyring):
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            await store.get_tokens()

    @pytest.mark.asyncio
    async def test_set_client_info_fails_closed_on_no_backend(self, fail_keyring):
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            await store.set_client_info(_make_client_info())

    @pytest.mark.asyncio
    async def test_get_client_info_fails_closed_on_no_backend(self, fail_keyring):
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            await store.get_client_info()

    @pytest.mark.asyncio
    async def test_error_message_does_not_contain_token_value(self, fail_keyring):
        """KeyringUnavailableError must not include the actual token in its message."""
        store = _make_store()
        sentinel = "SENTINEL-SECRET-TOKEN-XYZ"
        tok = OAuthToken(access_token=sentinel, token_type="bearer")
        with pytest.raises(KeyringUnavailableError) as exc_info:
            await store.set_tokens(tok)
        assert sentinel not in str(exc_info.value)
        assert sentinel not in repr(exc_info.value)


# ---------------------------------------------------------------------------
# Logout — deletion and idempotency
# ---------------------------------------------------------------------------


class TestLogout:
    @pytest.mark.asyncio
    async def test_logout_removes_tokens(self, mem_keyring):
        store = _make_store()
        await store.set_tokens(_make_token())
        await store.set_client_info(_make_client_info())
        store.logout()
        assert await store.get_tokens() is None
        assert await store.get_client_info() is None

    @pytest.mark.asyncio
    async def test_logout_is_idempotent(self, mem_keyring):
        store = _make_store()
        await store.set_tokens(_make_token())
        store.logout()
        store.logout()  # second call must not raise

    @pytest.mark.asyncio
    async def test_logout_when_nothing_stored_is_safe(self, mem_keyring):
        store = _make_store()
        # Nothing stored — logout should succeed silently
        store.logout()

    @pytest.mark.asyncio
    async def test_logout_only_affects_own_account(self, mem_keyring):
        store_a = _make_store(endpoint="https://a.example.com")
        store_b = _make_store(endpoint="https://b.example.com")
        await store_a.set_tokens(_make_token("tok-a"))
        await store_b.set_tokens(_make_token("tok-b"))
        store_a.logout()
        assert await store_a.get_tokens() is None
        # store_b's tokens must remain
        tok_b = await store_b.get_tokens()
        assert tok_b is not None
        assert tok_b.access_token == "tok-b"


# ---------------------------------------------------------------------------
# Keyring service name constant
# ---------------------------------------------------------------------------


class TestKeychainConstants:
    def test_keyring_service_name(self):
        assert KeyringTokenStorage.KEYRING_SERVICE == "slm-mcp-hub"

    def test_schema_version_is_string(self):
        assert isinstance(KeyringTokenStorage.SCHEMA_VERSION, str)
        assert KeyringTokenStorage.SCHEMA_VERSION  # non-empty


# ---------------------------------------------------------------------------
# RuntimeError backend — covers the RuntimeError catch branches
# ---------------------------------------------------------------------------


class GetFailKeyring(keyring.backend.KeyringBackend):
    """Raises RuntimeError on get_password — covers _keyring_get RuntimeError."""

    priority: float = 20.0

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("simulated get error")

    def set_password(self, service: str, username: str, password: str) -> None:
        pass  # no-op

    def delete_password(self, service: str, username: str) -> None:
        pass  # no-op


class SetDeleteFailKeyring(keyring.backend.KeyringBackend):
    """Succeeds on get; raises RuntimeError on set/delete — covers those branches."""

    priority: float = 20.0

    def get_password(self, service: str, username: str) -> str | None:
        return None  # no stored data

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("simulated set error")

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("simulated delete error")


@pytest.fixture()
def get_fail_keyring():
    original = keyring.get_keyring()
    keyring.set_keyring(GetFailKeyring())
    yield
    keyring.set_keyring(original)


@pytest.fixture()
def set_delete_fail_keyring():
    original = keyring.get_keyring()
    keyring.set_keyring(SetDeleteFailKeyring())
    yield
    keyring.set_keyring(original)


class TestRuntimeErrorBranches:
    """Cover RuntimeError catch branches in all keyring helpers."""

    @pytest.mark.asyncio
    async def test_get_tokens_runtime_error_fails_closed(self, get_fail_keyring):
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            await store.get_tokens()

    @pytest.mark.asyncio
    async def test_get_client_info_runtime_error_fails_closed(self, get_fail_keyring):
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            await store.get_client_info()

    @pytest.mark.asyncio
    async def test_set_tokens_runtime_error_fails_closed(self, set_delete_fail_keyring):
        """_keyring_set RuntimeError branch via set_tokens (which skips get_client_info)."""
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            await store.set_tokens(_make_token())

    def test_logout_no_keyring_backend_fails_closed(self, fail_keyring):
        """_keyring_delete_idempotent NoKeyringError branch."""
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            store.logout()

    def test_logout_runtime_error_fails_closed(self, set_delete_fail_keyring):
        """_keyring_delete_idempotent RuntimeError branch."""
        store = _make_store()
        with pytest.raises(KeyringUnavailableError):
            store.logout()


# ---------------------------------------------------------------------------
# Payload codec — corrupt / wrong-version / invalid-model branches
# ---------------------------------------------------------------------------


class TestPayloadCodecCorruption:
    """Direct tests of static _decode_* helpers for payload-corruption paths."""

    def test_decode_token_corrupt_json_returns_none(self):
        result = KeyringTokenStorage._decode_token("not-valid-json{{{")
        assert result is None

    def test_decode_token_wrong_version_returns_none(self):
        import json
        raw = json.dumps({"v": 999, "data": {"access_token": "tok", "token_type": "bearer"}})
        result = KeyringTokenStorage._decode_token(raw)
        assert result is None

    def test_decode_token_invalid_model_returns_none(self):
        """Payload with correct version but schema-invalid data returns None."""
        import json
        # OAuthToken requires 'access_token'; missing it causes ValidationError
        raw = json.dumps({"v": 1, "data": {"no_access_token_here": True}})
        result = KeyringTokenStorage._decode_token(raw)
        assert result is None

    def test_decode_client_info_corrupt_json_returns_none(self):
        result = KeyringTokenStorage._decode_client_info("{{invalid-json")
        assert result is None

    def test_decode_client_info_wrong_version_returns_none(self):
        import json
        raw = json.dumps({"v": 42, "data": {"client_id": "cid"}})
        result = KeyringTokenStorage._decode_client_info(raw)
        assert result is None

    def test_decode_client_info_invalid_model_returns_none(self):
        """Payload with correct version but schema-invalid client data returns None."""
        import json
        # OAuthClientInformationFull requires 'client_id'; omit it
        raw = json.dumps({"v": 1, "data": {"no_client_id": True}})
        result = KeyringTokenStorage._decode_client_info(raw)
        assert result is None
