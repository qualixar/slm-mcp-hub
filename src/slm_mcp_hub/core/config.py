"""Configuration management for SLM MCP Hub."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from slm_mcp_hub.auth.models import AuthConfig

from slm_mcp_hub.core.constants import (
    CACHE_DEFAULT_TTL_SECONDS,
    CACHE_MAX_ENTRIES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    IDLE_SHUTDOWN_SECONDS,
    MAX_SESSIONS,
    SESSION_TIMEOUT_SECONDS,
    TIMEOUT_CLASS_DEFAULT,
    get_config_dir,
    get_snapshots_dir,  # noqa: F401 — imported for test monkeypatching (setattr on this module)
)

logger = logging.getLogger(__name__)
SUPPORTED_TRANSPORTS = frozenset({"stdio", "http", "sse"})
# W3-P1: valid spawn policy values.
SUPPORTED_SPAWN_POLICIES = frozenset({"eager", "lazy", "pinned"})
# W4-P2: valid timeout class values (mirrors federation/timeouts.VALID_TIMEOUT_CLASSES).
SUPPORTED_TIMEOUT_CLASSES = frozenset({"fast", "default", "extended", "unbounded"})
# W4-P2: default per-backend concurrency limit.
DEFAULT_MAX_CONCURRENCY = 10

# W8-P6: snapshot retention constants — defined here so tests can patch them on
# ``slm_mcp_hub.core.config``.  config_io.py functions read these lazily from
# this module so that ``monkeypatch.setattr(config_module, "MAX_SNAPSHOTS", …)``
# takes effect inside _snapshot_existing / list_snapshots / restore_snapshot.
MAX_SNAPSHOTS = 50
DROP_GUARD_THRESHOLD = 0.7  # refuse save if MCP count drops below 70% of current


class ConfigValidationError(ValueError):
    """Raised when persisted or programmatic server config is malformed."""


def _default_auth() -> "AuthConfig":
    """Return a fresh AuthNoneConfig as the default auth policy."""
    from slm_mcp_hub.auth.models import AuthNoneConfig  # noqa: PLC0415
    return AuthNoneConfig()


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for a single MCP server.

    W3-P1 additions:
    - ``spawn``: spawn policy — "eager" (default), "lazy", or "pinned".
      "eager" preserves pre-W3 behavior: connects at startup and is NEVER
      evicted by the idle reaper (it stays connected regardless of age).
      "lazy" = harvested at boot to populate the capability cache, then
      eligible for idle eviction by the W3-P2 reaper when idle > idle_ttl_seconds.
      "pinned" = always hot, NEVER evicted (same as always_on=True).
    - ``is_pinned``: derived property — True when spawn=="pinned" OR always_on is True.

    W3-P2 eviction eligibility reconciliation (review finding):
    The idle reaper evicts ONLY backends with ``spawn == "lazy"``.
    ``spawn == "eager"`` backends stay connected and are NEVER idle-evicted.
    ``spawn == "pinned"`` / ``always_on=True`` (``is_pinned=True``) are also
    NEVER evicted.  When ``idle_ttl_seconds == 0``, the reaper is fully
    disabled and no evictions occur.
    """

    name: str
    transport: str  # "stdio" | "http" | "sse"
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    always_on: bool = False
    no_cache: bool = False
    cost_per_call_cents: float = 0.0
    auth: "AuthConfig" = field(default_factory=_default_auth)
    # W3-P1: spawn policy (eager | lazy | pinned). Default eager preserves
    # all pre-W3 behavior and keeps every existing config/test green.
    spawn: str = "eager"

    # W4-P2: timeout class — governs read_timeout_seconds for streaming calls.
    # "fast" (30s) | "default" (120s) | "extended" (600s) | "unbounded" (None).
    # Default "default" preserves existing behavior (120s = DEFAULT_TOOL_TIMEOUT_S).
    timeout_class: str = TIMEOUT_CLASS_DEFAULT

    # W4-P2: per-backend concurrent-slot cap for BackendConcurrencyGate.
    # Default 10 (= DEFAULT_MAX_CONCURRENCY in concurrency.py). Set to 1 for
    # backends that cannot handle concurrent calls safely.
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY

    @property
    def is_pinned(self) -> bool:
        """True if this backend is pinned (never to be evicted).

        A backend is effectively pinned if ``spawn == "pinned"``
        OR ``always_on is True`` (the legacy spelling of the same concept).
        """
        return self.spawn == "pinned" or self.always_on


@dataclass(frozen=True)
class HubConfig:
    """Complete hub configuration — immutable after creation."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    config_dir: Path = field(default_factory=get_config_dir)
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    session_timeout_seconds: int = SESSION_TIMEOUT_SECONDS
    max_sessions: int = MAX_SESSIONS
    cache_ttl_seconds: int = CACHE_DEFAULT_TTL_SECONDS
    cache_max_entries: int = CACHE_MAX_ENTRIES
    idle_shutdown_seconds: int = IDLE_SHUTDOWN_SECONDS
    log_level: str = "INFO"
    cors_origins: tuple[str, ...] = ("http://127.0.0.1", "http://localhost")
    plugins_enabled: tuple[str, ...] = ()
    # W1-P4: optional outbound webhook URLs for lifecycle event alerting.
    # Default is empty (disabled). Each URL is validated (http/https only) by
    # WebhookDispatcher at construction time. No secret material is ever
    # included in the webhook payload — see _event_to_dict() in events.py.
    webhooks: tuple[str, ...] = ()
    # W2-P1: cap on concurrent _connect_timed calls during connect_all().
    # Prevents the startup thundering-herd (N subprocesses spawned at once).
    # Minimum 1. Env override: SLM_HUB_STARTUP_MAX_CONCURRENCY.
    startup_max_concurrency: int = 8
    # W3-P1: seconds of idle time before a NON-pinned backend is evicted.
    # 0 means never evict (W3-P2 reaper will respect this).
    # Env override: SLM_HUB_IDLE_TTL_SECONDS.
    idle_ttl_seconds: int = 300
    # W3-P1: maximum number of concurrently live (connected) backends.
    # 0 means unlimited. When a new on-demand reconnect (W3-P3) would exceed
    # this cap, the least-recently-used non-pinned backend is evicted first.
    # Env override: SLM_HUB_MAX_LIVE_BACKENDS.
    max_live_backends: int = 0

    # W4-P3: event store (InMemoryEventStore). Wired when event_store_enabled=True.
    # Default True. Set False to disable. Honored in stateful mode (transport_stateful=True).
    event_store_enabled: bool = True
    event_store_max_events_per_stream: int = 500
    event_store_max_streams: int = 200
    event_store_stream_ttl_s: float = 7200.0  # 2 hours; covers UNBOUNDED-class calls
    # W8-P3: transport mode. False = stateless (default, modern MCP 2026-07-28); True = stateful.
    transport_stateful: bool = False

    # W5-P1: admin dashboard + SSE event queue config.
    # dashboard_enabled: set to False to disable /dashboard HTML route (default on).
    # dashboard_bind: SECURITY DEFAULT is "127.0.0.1" (localhost only).
    #   Setting to "0.0.0.0" exposes admin controls over the network — always
    #   pair with SLM_HUB_API_KEY. Logged at startup so the security default is visible.
    # event_queue_maxsize: per-client SSE event queue depth before drop-oldest.
    dashboard_enabled: bool = True
    dashboard_bind: str = "127.0.0.1"
    event_queue_maxsize: int = 256

    def __post_init__(self) -> None:
        if self.startup_max_concurrency < 1:
            raise ConfigValidationError(
                f"startup_max_concurrency must be >= 1, got {self.startup_max_concurrency}"
            )
        if self.idle_ttl_seconds < 0:
            raise ConfigValidationError(
                f"idle_ttl_seconds must be >= 0 (0 = never evict), got {self.idle_ttl_seconds}"
            )
        if self.max_live_backends < 0:
            raise ConfigValidationError(
                f"max_live_backends must be >= 0 (0 = unlimited), got {self.max_live_backends}"
            )
        # W5-P1: dashboard config validation.
        if not isinstance(self.dashboard_bind, str) or not self.dashboard_bind.strip():
            raise ConfigValidationError(
                f"dashboard_bind must be a non-empty string, got {self.dashboard_bind!r}"
            )
        if not isinstance(self.event_queue_maxsize, int) or self.event_queue_maxsize < 1:
            raise ConfigValidationError(
                f"event_queue_maxsize must be a positive integer, got {self.event_queue_maxsize!r}"
            )


def _resolve_env_vars(value: str) -> str:
    """Resolve ${VAR} and ${env:VAR} placeholders in config values."""
    def _replacer(match: re.Match) -> str:
        var_name = match.group(1) or match.group(2)
        return os.environ.get(var_name, match.group(0))

    value = re.sub(r"\$\{env:([^}]+)\}", _replacer, value)
    value = re.sub(r"\$\{([^}:]+)\}", _replacer, value)
    return value


def _validate_string_map(field_name: str, value: object) -> None:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ConfigValidationError(
            f"Server {field_name} must be an object containing string values"
        )


def validate_server_config(config: MCPServerConfig) -> None:
    """Validate a server config without including sensitive values in errors."""
    if not isinstance(config.name, str) or not config.name.strip():
        raise ConfigValidationError("Server name must be a non-empty string")
    if config.transport not in SUPPORTED_TRANSPORTS:
        raise ConfigValidationError(
            "Server transport must be one of: http, sse, stdio"
        )
    if not isinstance(config.command, str):
        raise ConfigValidationError("Server command must be a string")
    if not isinstance(config.args, tuple) or any(
        not isinstance(item, str) for item in config.args
    ):
        raise ConfigValidationError("Server args must contain only strings")
    _validate_string_map("env", config.env)
    if not isinstance(config.url, str):
        raise ConfigValidationError("Server url must be a string")
    _validate_string_map("headers", config.headers)
    for field_name in ("enabled", "always_on", "no_cache"):
        if not isinstance(getattr(config, field_name), bool):
            raise ConfigValidationError(f"Server {field_name} must be a boolean")
    cost = config.cost_per_call_cents
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        raise ConfigValidationError(
            "Server cost_per_call_cents must be a non-negative number"
        )
    # W3-P1: spawn policy validation.
    if config.spawn not in SUPPORTED_SPAWN_POLICIES:
        raise ConfigValidationError(
            f"Server spawn must be one of: {', '.join(sorted(SUPPORTED_SPAWN_POLICIES))}; "
            f"got {config.spawn!r}"
        )
    # W4-P2: timeout class validation.
    if config.timeout_class not in SUPPORTED_TIMEOUT_CLASSES:
        raise ConfigValidationError(
            f"Server timeout_class must be one of: "
            f"{', '.join(sorted(SUPPORTED_TIMEOUT_CLASSES))}; "
            f"got {config.timeout_class!r}"
        )
    # W4-P2: max_concurrency validation.
    if not isinstance(config.max_concurrency, int) or config.max_concurrency < 1:
        raise ConfigValidationError(
            f"Server max_concurrency must be a positive integer, got {config.max_concurrency!r}"
        )
    _validate_auth_header_compatibility(config)
    _validate_auth_transport_compatibility(config)


def _validate_auth_transport_compatibility(config: MCPServerConfig) -> None:
    """Raise if sse+oauth is configured — OAuth requires Streamable HTTP (W6-P1)."""
    from slm_mcp_hub.auth.models import AuthMode  # noqa: PLC0415
    if config.transport != "sse" or config.auth.mode is not AuthMode.OAUTH:
        return
    raise ConfigValidationError(
        "transport='sse' is incompatible with auth.mode='oauth': "
        "use transport='http' for OAuth-authenticated backends."
    )


def _validate_auth_header_compatibility(config: MCPServerConfig) -> None:
    """Raise if oauth mode is combined with credential-bearing static headers.

    oauth mode is incompatible with Authorization / Cookie /
    Proxy-Authorization (or any other credential-bearing header) in the
    static headers map.  Only non-credential companion headers are allowed
    alongside OAuth.
    """
    from slm_mcp_hub.auth.models import (  # noqa: PLC0415
        AUTH_CREDENTIAL_HEADERS,
        AuthMode,
    )

    if config.auth.mode is not AuthMode.OAUTH:
        return  # only validate for oauth mode

    # Normalise the header name (strip surrounding whitespace + case-fold) so a
    # padded key like " Authorization" cannot smuggle a credential past the
    # reject list under oauth mode.
    bad_headers = [
        k for k in config.headers if k.strip().lower() in AUTH_CREDENTIAL_HEADERS
    ]
    if bad_headers:
        # Do NOT include the header *value* — only the name is safe to log.
        names = ", ".join(sorted(bad_headers))
        raise ConfigValidationError(
            f"oauth auth mode is incompatible with credential-bearing static "
            f"headers: {names}. "
            f"Remove the header(s) or switch to static_headers auth mode."
        )


def _serialize_auth(auth: "AuthConfig") -> dict[str, Any]:
    """Serialize an auth policy to a JSON-safe dict.

    The output contains ONLY policy fields — no tokens, secrets, or credentials.
    Must be called only for non-none auth modes; save_config guards this.
    """
    from slm_mcp_hub.auth.models import AuthMode, AuthOAuthConfig  # noqa: PLC0415

    if auth.mode is AuthMode.STATIC_HEADERS:
        return {"mode": auth.mode.value}
    if auth.mode is AuthMode.OAUTH:
        # No assert here — mode guard already guarantees type; assert is stripped by -O.
        oauth_auth: AuthOAuthConfig = auth  # type: ignore[assignment]
        out: dict[str, Any] = {
            "mode": oauth_auth.mode.value,
            "scopes": list(oauth_auth.scopes),
            "callback_host": oauth_auth.callback_host,
            "callback_port": oauth_auth.callback_port,
        }
        if oauth_auth.client_metadata_url is not None:
            out["client_metadata_url"] = oauth_auth.client_metadata_url
        return out
    # Callers guard with `not isinstance(srv.auth, AuthNoneConfig)` so none-mode
    # should never reach here.  Raise explicitly rather than silently serializing.
    raise ConfigValidationError(
        "_serialize_auth called with none-mode auth — callers must guard against this"
    )


def materialize_server_config(config: MCPServerConfig) -> MCPServerConfig:
    """Create a runtime config with environment placeholders resolved.

    The input remains the canonical persisted representation. Keeping expansion at
    the connection boundary prevents config saves and snapshots from receiving
    resolved credentials.
    """
    validate_server_config(config)
    return replace(
        config,
        command=_resolve_env_vars(config.command),
        args=tuple(_resolve_env_vars(value) for value in config.args),
        env={key: _resolve_env_vars(value) for key, value in config.env.items()},
        url=_resolve_env_vars(config.url),
        headers={
            key: _resolve_env_vars(value) for key, value in config.headers.items()
        },
    )


def parse_mcp_server(name: str, raw: dict[str, Any]) -> MCPServerConfig:
    """Parse a server while preserving unresolved persisted values."""
    from slm_mcp_hub.auth.models import parse_auth_config  # noqa: PLC0415

    if not isinstance(raw, dict):
        raise ConfigValidationError("Server configuration must be an object")

    auth = parse_auth_config(raw.get("auth"))

    # W3-P1: always_on=True implies pinned. Explicit spawn="pinned" is
    # the canonical form; always_on is the legacy alias. If always_on is
    # set and spawn is not given, default to "pinned" so is_pinned() is
    # consistent even before the property is evaluated.
    always_on = raw.get("always_on", False)
    spawn_raw = raw.get("spawn", "pinned" if always_on else "eager")

    # W4-P2: timeout class and per-backend concurrency limit.
    timeout_class = raw.get("timeout_class", TIMEOUT_CLASS_DEFAULT)
    max_concurrency = int(raw.get("max_concurrency", DEFAULT_MAX_CONCURRENCY))

    if "url" in raw:
        transport = raw.get("type", "http")
        config = MCPServerConfig(
            name=name,
            transport=transport,
            url=raw["url"],
            headers=raw.get("headers", {}),
            enabled=raw.get("enabled", True),
            always_on=always_on,
            no_cache=raw.get("no_cache", False),
            cost_per_call_cents=raw.get("cost_per_call_cents", 0.0),
            auth=auth,
            spawn=spawn_raw,
            timeout_class=timeout_class,
            max_concurrency=max_concurrency,
        )
        validate_server_config(config)
        return config

    command = raw.get("command", "")
    args = tuple(raw.get("args", []))
    env = raw.get("env", {})
    config = MCPServerConfig(
        name=name,
        transport="stdio",
        command=command,
        args=args,
        env=env,
        enabled=raw.get("enabled", True),
        always_on=always_on,
        no_cache=raw.get("no_cache", False),
        cost_per_call_cents=raw.get("cost_per_call_cents", 0.0),
        auth=auth,
        spawn=spawn_raw,
        timeout_class=timeout_class,
        max_concurrency=max_concurrency,
    )
    validate_server_config(config)
    return config


def import_claude_config(claude_json_path: Path) -> list[MCPServerConfig]:
    """Import MCP server definitions from Claude Code's ~/.claude.json."""
    with open(claude_json_path) as f:
        raw = json.load(f)

    servers_raw = raw.get("mcpServers", {})
    return [parse_mcp_server(name, cfg) for name, cfg in servers_raw.items()]


def import_vscode_config(vscode_json_path: Path) -> list[MCPServerConfig]:
    """Import MCP server definitions from VS Code settings.json or mcp.json."""
    with open(vscode_json_path) as f:
        raw = json.load(f)

    servers_raw = raw.get("servers", raw.get("mcp.servers", raw.get("mcpServers", {})))
    return [parse_mcp_server(name, cfg) for name, cfg in servers_raw.items()]


# ---------------------------------------------------------------------------
# Re-export I/O layer — moved to core/config_io.py (W8-P6).
# All symbols below remain importable from this module for backward compat.
# ---------------------------------------------------------------------------
from slm_mcp_hub.core.config_io import (  # noqa: E402, F401
    _apply_env_overrides,
    _atomic_write,
    _snapshot_existing,
    as_bool,
    generate_default_config,
    list_snapshots,
    load_config,
    restore_snapshot,
    save_config,
)
