"""Auth package — OAuth policy, secure storage, and provider factory."""

from slm_mcp_hub.auth.broker import (
    OAuthAuthRequiredError,
    build_oauth_http_client,
    get_refresh_lock_path,
    refresh_lock_context,
)
from slm_mcp_hub.auth.callback import CallbackError, CallbackServer
from slm_mcp_hub.auth.models import (
    AUTH_CREDENTIAL_HEADERS,
    AuthMode,
    AuthNoneConfig,
    AuthOAuthConfig,
    AuthStaticHeadersConfig,
    parse_auth_config,
)
from slm_mcp_hub.auth.provider import (
    OAuthProviderMode,
    build_login_provider,
    build_runtime_provider,
    is_safe_oauth_metadata_url,
)
from slm_mcp_hub.auth.token_store import KeyringTokenStorage, KeyringUnavailableError

__all__ = [
    "AUTH_CREDENTIAL_HEADERS",
    "AuthMode",
    "AuthNoneConfig",
    "AuthOAuthConfig",
    "AuthStaticHeadersConfig",
    "CallbackError",
    "CallbackServer",
    "KeyringTokenStorage",
    "KeyringUnavailableError",
    "OAuthAuthRequiredError",
    "OAuthProviderMode",
    "build_login_provider",
    "build_oauth_http_client",
    "build_runtime_provider",
    "get_refresh_lock_path",
    "is_safe_oauth_metadata_url",
    "parse_auth_config",
    "refresh_lock_context",
]
