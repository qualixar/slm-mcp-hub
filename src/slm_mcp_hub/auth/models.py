"""Immutable auth policy models for SLM MCP Hub.

These models store POLICY ONLY.  They never hold access tokens, refresh tokens,
client secrets, authorization codes, PKCE verifiers, or raw Authorization header
values.  Any repr/str of these models is safe to log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AuthMode(str, Enum):
    """Allowed auth modes for an MCP server entry."""

    NONE = "none"
    STATIC_HEADERS = "static_headers"
    OAUTH = "oauth"


# Headers that are NEVER allowed alongside oauth mode.  Case-folded for
# comparison; incoming header names are lower-cased before checking.
AUTH_CREDENTIAL_HEADERS: frozenset[str] = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)


# ---------------------------------------------------------------------------
# Frozen auth config variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthNoneConfig:
    """No authentication.  Default for every server config."""

    mode: AuthMode = field(default=AuthMode.NONE)

    def __post_init__(self) -> None:
        if self.mode is not AuthMode.NONE:
            raise ValueError(f"AuthNoneConfig.mode must be {AuthMode.NONE!r}")


@dataclass(frozen=True)
class AuthStaticHeadersConfig:
    """Static-header authentication mode.

    The actual header key/value pairs live in MCPServerConfig.headers so that
    existing config shapes remain valid without migration.  This model is a
    policy marker: it signals that the configured headers act as auth material.
    """

    mode: AuthMode = field(default=AuthMode.STATIC_HEADERS)

    def __post_init__(self) -> None:
        if self.mode is not AuthMode.STATIC_HEADERS:
            raise ValueError(
                f"AuthStaticHeadersConfig.mode must be {AuthMode.STATIC_HEADERS!r}"
            )


@dataclass(frozen=True)
class AuthOAuthConfig:
    """OAuth2 authentication policy.

    Stores server-side policy that drives the OAuth flow.  Never stores tokens,
    secrets, codes, or any credential material.
    """

    mode: AuthMode = field(default=AuthMode.OAUTH)
    scopes: tuple[str, ...] = field(default=())
    client_metadata_url: str | None = field(default=None)
    callback_host: str = field(default="127.0.0.1")
    callback_port: int = field(default=0)

    def __post_init__(self) -> None:
        if self.mode is not AuthMode.OAUTH:
            raise ValueError(f"AuthOAuthConfig.mode must be {AuthMode.OAUTH!r}")
        _validate_scopes(self.scopes)
        _validate_callback_host(self.callback_host)
        _validate_callback_port(self.callback_port)


def _validate_scopes(scopes: object) -> None:
    if not isinstance(scopes, tuple):
        raise TypeError(f"scopes must be a tuple, got {type(scopes).__name__}")
    for item in scopes:
        if not isinstance(item, str):
            raise TypeError(f"Each scope must be a str, got {type(item).__name__}")


def _validate_callback_host(host: object) -> None:
    if not isinstance(host, str):
        raise TypeError(f"callback_host must be a str, got {type(host).__name__}")
    # SEC-L-01: the OAuth callback server binds loopback only; reject a non-loopback
    # callback_host at config time (fail-fast) instead of at login time.
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise TypeError(
            f"callback_host must be loopback (127.0.0.1 / localhost / ::1), got {host!r}"
        )


def _validate_callback_port(port: object) -> None:
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError(f"callback_port must be an int, got {type(port).__name__}")


# ---------------------------------------------------------------------------
# Union type alias
# ---------------------------------------------------------------------------

AuthConfig = AuthNoneConfig | AuthStaticHeadersConfig | AuthOAuthConfig


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_MODE_MAP: dict[str, type[AuthConfig]] = {
    AuthMode.NONE.value: AuthNoneConfig,
    AuthMode.STATIC_HEADERS.value: AuthStaticHeadersConfig,
    AuthMode.OAUTH.value: AuthOAuthConfig,
}


def parse_auth_config(raw: dict[str, Any] | None) -> AuthConfig:
    """Parse a raw auth config dict into the appropriate frozen model.

    Accepts ``None`` or an empty dict — both return ``AuthNoneConfig()``.
    Raises ``ConfigValidationError`` for unknown modes.
    """
    from slm_mcp_hub.core.config import (
        ConfigValidationError,  # local import avoids cycle
    )

    if not raw:
        return AuthNoneConfig()

    mode_str = raw.get("mode", AuthMode.NONE.value)
    cls = _MODE_MAP.get(mode_str)
    if cls is None:
        raise ConfigValidationError(
            f"Unknown auth mode {mode_str!r}. "
            f"Allowed: {sorted(_MODE_MAP)}"
        )

    if cls is AuthNoneConfig:
        return AuthNoneConfig()
    if cls is AuthStaticHeadersConfig:
        return AuthStaticHeadersConfig()

    # AuthOAuthConfig
    scopes_raw = raw.get("scopes", [])
    if not isinstance(scopes_raw, list):
        raise ConfigValidationError("auth.scopes must be a list of strings")
    scopes = tuple(scopes_raw)

    client_metadata_url = raw.get("client_metadata_url")
    callback_host = raw.get("callback_host", "127.0.0.1")
    callback_port_raw = raw.get("callback_port", 0)

    try:
        return AuthOAuthConfig(
            scopes=scopes,
            client_metadata_url=client_metadata_url,
            callback_host=callback_host,
            callback_port=callback_port_raw,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"Invalid oauth auth config: {exc}") from exc
