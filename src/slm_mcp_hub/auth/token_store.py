"""Secure keyring-backed token storage for OAuth2.

``KeyringTokenStorage`` implements the four async methods of
``mcp.client.auth.TokenStorage`` and stores all credential material exclusively
in the operating-system keychain via the ``keyring`` library.

Design invariants
-----------------
* **Fail closed.** Any attempt to read or write when no secure backend is
  available raises ``KeyringUnavailableError``.  There is no plaintext
  fallback.
* **No secret in repr/str/exceptions.** This class never keeps token or secret
  values as instance attributes.  ``__repr__`` exposes only the endpoint and
  schema version — never any credential.
* **Account key independence from client_id.** The keychain slot is derived
  from (schema_version, profile_id, endpoint, redirect_uri) via SHA-256.
  ``client_id`` is unknown before ``get_client_info()`` returns, so it must
  not appear in the key.
* **Issuer-binding change detection.** When ``set_client_info`` is called with
  a new issuer that differs from the stored one, the previously stored tokens
  are cleared.  The SDK ``OAuthClientProvider`` is then responsible for
  discovering and re-validating server metadata.
* **Idempotent logout.** ``logout()`` removes both entries and silently
  succeeds if they were already absent.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

import keyring
import keyring.errors

if TYPE_CHECKING:
    pass  # kept for future typing imports

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

_PAYLOAD_VERSION = 1


class KeyringUnavailableError(RuntimeError):
    """Raised when no secure keyring backend is available.

    The message intentionally contains no secret or token material.
    """


class KeyringTokenStorage:
    """OS-keychain-backed token storage for ``OAuthClientProvider``.

    Parameters
    ----------
    endpoint:
        Canonical MCP endpoint URL (used in account-key derivation).
    redirect_uri:
        OAuth redirect URI (used in account-key derivation).
    profile_id:
        Local installation / profile identity.  Defaults to ``"default"``.
        Change this to isolate multiple Hub instances on the same machine.
    """

    KEYRING_SERVICE: str = "slm-mcp-hub"
    SCHEMA_VERSION: str = "1"

    def __init__(
        self,
        endpoint: str,
        redirect_uri: str,
        profile_id: str = "default",
    ) -> None:
        self._endpoint = endpoint
        self._redirect_uri = redirect_uri
        self._profile_id = profile_id
        base = self._make_account_key()
        self._token_account: str = base + ":t"
        self._client_account: str = base + ":c"

    # ------------------------------------------------------------------
    # Account-key derivation
    # ------------------------------------------------------------------

    def _make_account_key(self) -> str:
        """Return a hex SHA-256 derived from non-secret, stable inputs.

        Specifically excludes ``client_id`` — that value is unknown before
        Dynamic Client Registration and must not block pre-DCR lookups.
        """
        raw = "\x00".join(
            [self.SCHEMA_VERSION, self._profile_id, self._endpoint, self._redirect_uri]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    # ------------------------------------------------------------------
    # TokenStorage interface (all four methods are async per SDK contract)
    # ------------------------------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        """Retrieve stored access tokens from the keychain.

        Returns ``None`` if nothing is stored.
        Raises ``KeyringUnavailableError`` if the backend is unavailable.
        """
        raw = self._keyring_get(self._token_account)
        if raw is None:
            return None
        return self._decode_token(raw)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist access tokens to the keychain.

        Raises ``KeyringUnavailableError`` if the backend is unavailable.
        The token value is never written to logs or exception messages.
        """
        payload = {"v": _PAYLOAD_VERSION, "data": tokens.model_dump(mode="json")}
        self._keyring_set(self._token_account, json.dumps(payload))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Retrieve stored client registration from the keychain.

        Returns ``None`` if nothing is stored.
        Raises ``KeyringUnavailableError`` if the backend is unavailable.
        """
        raw = self._keyring_get(self._client_account)
        if raw is None:
            return None
        return self._decode_client_info(raw)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Persist client registration to the keychain.

        If the incoming ``client_info.issuer`` differs from the previously
        stored issuer, the stored tokens are cleared first (binding-change
        invalidation).  This lets the SDK rediscover and re-validate metadata
        without the Hub duplicating any discovery logic.

        Raises ``KeyringUnavailableError`` if the backend is unavailable.
        """
        existing = await self.get_client_info()
        if existing is not None and existing.issuer != client_info.issuer:
            self._keyring_delete_idempotent(self._token_account)

        payload = {"v": _PAYLOAD_VERSION, "data": client_info.model_dump(mode="json")}
        self._keyring_set(self._client_account, json.dumps(payload))

    # ------------------------------------------------------------------
    # Logout
    # ------------------------------------------------------------------

    def logout(self) -> None:
        """Remove both token and client-info entries.  Idempotent."""
        self._keyring_delete_idempotent(self._token_account)
        self._keyring_delete_idempotent(self._client_account)

    # ------------------------------------------------------------------
    # Internal keyring helpers — all raise KeyringUnavailableError on failure
    # ------------------------------------------------------------------

    def _keyring_get(self, account: str) -> str | None:
        try:
            return keyring.get_password(self.KEYRING_SERVICE, account)
        except keyring.errors.NoKeyringError as exc:
            raise KeyringUnavailableError(
                "No secure keychain backend available; "
                "install a supported keyring backend"
            ) from exc
        except RuntimeError as exc:
            raise KeyringUnavailableError(
                f"Keyring backend error: {type(exc).__name__}"
            ) from exc

    def _keyring_set(self, account: str, value: str) -> None:
        try:
            keyring.set_password(self.KEYRING_SERVICE, account, value)
        except keyring.errors.NoKeyringError as exc:
            raise KeyringUnavailableError(
                "No secure keychain backend available; "
                "install a supported keyring backend"
            ) from exc
        except RuntimeError as exc:
            raise KeyringUnavailableError(
                f"Keyring backend error: {type(exc).__name__}"
            ) from exc

    def _keyring_delete_idempotent(self, account: str) -> None:
        """Delete a keychain entry; silently ignore missing-password errors."""
        try:
            keyring.delete_password(self.KEYRING_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent — idempotent
        except keyring.errors.NoKeyringError as exc:
            raise KeyringUnavailableError(
                "No secure keychain backend available; "
                "install a supported keyring backend"
            ) from exc
        except RuntimeError as exc:
            raise KeyringUnavailableError(
                f"Keyring backend error: {type(exc).__name__}"
            ) from exc

    # ------------------------------------------------------------------
    # Payload codec
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_token(raw: str) -> OAuthToken | None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("slm_mcp_hub.auth: corrupt token payload; discarding")
            return None
        if payload.get("v") != _PAYLOAD_VERSION:
            logger.warning(
                "slm_mcp_hub.auth: unrecognised token payload version %s; discarding",
                payload.get("v"),
            )
            return None
        try:
            return OAuthToken.model_validate(payload["data"])
        except Exception:  # noqa: BLE001
            logger.warning("slm_mcp_hub.auth: invalid OAuthToken payload; discarding")
            return None

    @staticmethod
    def _decode_client_info(raw: str) -> OAuthClientInformationFull | None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("slm_mcp_hub.auth: corrupt client-info payload; discarding")
            return None
        if payload.get("v") != _PAYLOAD_VERSION:
            logger.warning(
                "slm_mcp_hub.auth: unrecognised client-info payload version %s; discarding",
                payload.get("v"),
            )
            return None
        try:
            return OAuthClientInformationFull.model_validate(payload["data"])
        except Exception:  # noqa: BLE001
            logger.warning("slm_mcp_hub.auth: invalid OAuthClientInformationFull payload; discarding")
            return None

    # ------------------------------------------------------------------
    # Safe repr — must never expose token material
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"KeyringTokenStorage("
            f"endpoint={self._endpoint!r}, "
            f"profile_id={self._profile_id!r}, "
            f"schema_version={self.SCHEMA_VERSION!r}"
            f")"
        )
