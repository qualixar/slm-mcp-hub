"""Configuration management for SLM MCP Hub."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from slm_mcp_hub.core.constants import (
    CACHE_DEFAULT_TTL_SECONDS,
    CACHE_MAX_ENTRIES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    IDLE_SHUTDOWN_SECONDS,
    MAX_SESSIONS,
    SESSION_TIMEOUT_SECONDS,
    get_config_dir,
    get_config_file,
    get_snapshots_dir,
)

logger = logging.getLogger(__name__)
SUPPORTED_TRANSPORTS = frozenset({"stdio", "http", "sse"})


class ConfigValidationError(ValueError):
    """Raised when persisted or programmatic server config is malformed."""


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for a single MCP server."""

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
    if not isinstance(raw, dict):
        raise ConfigValidationError("Server configuration must be an object")
    if "url" in raw:
        transport = raw.get("type", "http")
        config = MCPServerConfig(
            name=name,
            transport=transport,
            url=raw["url"],
            headers=raw.get("headers", {}),
            enabled=raw.get("enabled", True),
            always_on=raw.get("always_on", False),
            no_cache=raw.get("no_cache", False),
            cost_per_call_cents=raw.get("cost_per_call_cents", 0.0),
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
        always_on=raw.get("always_on", False),
        no_cache=raw.get("no_cache", False),
        cost_per_call_cents=raw.get("cost_per_call_cents", 0.0),
    )
    validate_server_config(config)
    return config


def load_config(config_path: Path | None = None) -> HubConfig:
    """Load hub configuration from file, with env var overrides."""
    path = config_path or get_config_file()

    if not path.exists():
        logger.info("No config file found at %s, using defaults", path)
        return _apply_env_overrides(HubConfig())

    _secure_config_permissions(path)

    with open(path) as f:
        raw = json.load(f)

    servers_raw = raw.get("mcpServers", raw.get("servers", {}))
    servers = tuple(
        parse_mcp_server(name, cfg) for name, cfg in servers_raw.items()
    )

    config = HubConfig(
        host=raw.get("host", DEFAULT_HOST),
        port=raw.get("port", DEFAULT_PORT),
        config_dir=Path(raw.get("config_dir", str(get_config_dir()))),
        mcp_servers=servers,
        session_timeout_seconds=raw.get("session_timeout_seconds", SESSION_TIMEOUT_SECONDS),
        max_sessions=raw.get("max_sessions", MAX_SESSIONS),
        cache_ttl_seconds=raw.get("cache_ttl_seconds", CACHE_DEFAULT_TTL_SECONDS),
        cache_max_entries=raw.get("cache_max_entries", CACHE_MAX_ENTRIES),
        idle_shutdown_seconds=raw.get("idle_shutdown_seconds", IDLE_SHUTDOWN_SECONDS),
        log_level=raw.get("log_level", "INFO"),
        cors_origins=tuple(
            raw.get("cors_origins", ["http://127.0.0.1", "http://localhost"])
        ),
        plugins_enabled=tuple(raw.get("plugins_enabled", [])),
    )

    return _apply_env_overrides(config)


def _apply_env_overrides(config: HubConfig) -> HubConfig:
    """Apply environment variable overrides to config. Returns new config."""
    port = int(os.environ.get("SLM_HUB_PORT", config.port))
    host = os.environ.get("SLM_HUB_HOST", config.host)
    log_level = os.environ.get("SLM_HUB_LOG_LEVEL", config.log_level)
    config_dir = Path(os.environ.get("SLM_HUB_CONFIG_DIR", str(config.config_dir)))

    if (
        port == config.port
        and host == config.host
        and log_level == config.log_level
        and config_dir == config.config_dir
    ):
        return config

    return HubConfig(
        host=host,
        port=port,
        config_dir=config_dir,
        mcp_servers=config.mcp_servers,
        session_timeout_seconds=config.session_timeout_seconds,
        max_sessions=config.max_sessions,
        cache_ttl_seconds=config.cache_ttl_seconds,
        cache_max_entries=config.cache_max_entries,
        idle_shutdown_seconds=config.idle_shutdown_seconds,
        log_level=log_level,
        cors_origins=config.cors_origins,
        plugins_enabled=config.plugins_enabled,
    )


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


MAX_SNAPSHOTS = 50
DROP_GUARD_THRESHOLD = 0.7  # refuse save if MCP count drops below 70% of current


def _secure_config_permissions(path: Path) -> None:
    """Restrict live config and retained snapshots without following symlinks."""
    if path.exists() and not path.is_symlink():
        path.chmod(0o600)

    snapshots_dir = get_snapshots_dir(path.parent)
    if not snapshots_dir.exists() or snapshots_dir.is_symlink():
        return
    snapshots_dir.chmod(0o700)
    # Snapshot retention is capped at MAX_SNAPSHOTS. Limit migration work to
    # that same bounded set during config load.
    for snapshot in sorted(
        snapshots_dir.glob("config-*.json"), reverse=True
    )[:MAX_SNAPSHOTS]:
        if snapshot.is_file() and not snapshot.is_symlink():
            snapshot.chmod(0o600)


def _snapshot_existing(path: Path) -> Path | None:
    """Snapshot existing config to versioned file before overwriting.

    Skips snapshot if existing config is empty/trivial (< 3 MCPs) — no point
    in keeping useless snapshots that bloat the snapshot dir.

    Returns snapshot path if created, None otherwise.
    """
    if not path.exists():
        return None

    # Don't snapshot trivial configs
    try:
        with open(path) as f:
            existing = json.load(f)
        existing_count = len(existing.get("mcpServers", existing.get("servers", {})))
        if existing_count < 3:
            return None  # not worth snapshotting
    except (json.JSONDecodeError, OSError):
        return None  # corrupt file, can't snapshot meaningfully

    snapshots_dir = get_snapshots_dir(path.parent)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.chmod(0o700)
    import time
    ts = time.strftime("%Y%m%d-%H%M%S")
    snap_path = snapshots_dir / f"config-{ts}-{existing_count}mcps.json"

    import shutil
    shutil.copy2(path, snap_path)
    snap_path.chmod(0o600)

    # Prune old snapshots — keep MAX_SNAPSHOTS newest
    snaps = sorted(snapshots_dir.glob("config-*.json"))
    while len(snaps) > MAX_SNAPSHOTS:
        snaps[0].unlink(missing_ok=True)
        snaps = snaps[1:]

    return snap_path


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + rename.

    Validates JSON parses before rename. If anything fails, original file
    is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)

        # Verify the file we just wrote parses back identically
        with open(tmp_path) as f:
            verify = json.load(f)
        if verify.get("mcpServers", {}) != data.get("mcpServers", {}):
            raise RuntimeError("Atomic write verification failed: mcpServers mismatch")

        os.replace(tmp_path, path)
        path.chmod(0o600)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def save_config(config: HubConfig, config_path: Path | None = None, force: bool = False) -> None:
    """Save hub configuration to JSON file.

    Defenses (in order):
    1. PYTEST guard — refuses to write real user config during pytest.
    2. COUNT-DROP guard — refuses if new MCP count < 70% of existing (unless force=True).
    3. SNAPSHOT — versioned backup written to ~/.slm-mcp-hub/snapshots/ before overwrite.
    4. ATOMIC WRITE — write to .tmp, validate, rename.
    """
    import os
    path = config_path or get_config_file(config.config_dir)
    _secure_config_permissions(path)

    if "PYTEST_CURRENT_TEST" in os.environ:
        real_user_config = (Path.home() / ".slm-mcp-hub" / "config.json").resolve()
        if path.resolve() == real_user_config:
            raise RuntimeError(
                f"REFUSING to overwrite real user config {path} during pytest. "
                "Tests must pass an explicit config_path. "
                "This guard prevents the April 26 incident where tests "
                "nuked 39 MCP server configurations."
            )

    # COUNT-DROP GUARD — refuse catastrophic shrinkage unless forced
    new_count = len(config.mcp_servers)
    if path.exists() and not force:
        try:
            with open(path) as f:
                existing = json.load(f)
            existing_count = len(existing.get("mcpServers", existing.get("servers", {})))
            if existing_count > 5 and new_count < int(existing_count * DROP_GUARD_THRESHOLD):
                raise RuntimeError(
                    f"REFUSING to save: MCP count would drop from {existing_count} to {new_count} "
                    f"(>{int((1-DROP_GUARD_THRESHOLD)*100)}% loss). "
                    f"Pass force=True or use 'slm-hub config restore' if this is unintended. "
                    f"Snapshots: {get_snapshots_dir(path.parent)}"
                )
        except (json.JSONDecodeError, OSError):
            pass  # corrupt existing file — let save proceed

    # SNAPSHOT existing before overwriting
    snap = _snapshot_existing(path)
    if snap:
        logger.info("Snapshot saved: %s", snap)

    path.parent.mkdir(parents=True, exist_ok=True)

    servers_dict = {}
    for srv in config.mcp_servers:
        validate_server_config(srv)
        entry: dict[str, Any] = {"enabled": srv.enabled}
        if srv.transport == "stdio":
            entry["command"] = srv.command
            entry["args"] = list(srv.args)
            if srv.env:
                entry["env"] = srv.env
        else:
            entry["type"] = srv.transport
            entry["url"] = srv.url
            if srv.headers:
                entry["headers"] = srv.headers
        if srv.always_on:
            entry["always_on"] = True
        if srv.no_cache:
            entry["no_cache"] = True
        if srv.cost_per_call_cents > 0:
            entry["cost_per_call_cents"] = srv.cost_per_call_cents
        servers_dict[srv.name] = entry

    data = {
        "host": config.host,
        "port": config.port,
        "mcpServers": servers_dict,
        "session_timeout_seconds": config.session_timeout_seconds,
        "max_sessions": config.max_sessions,
        "cache_ttl_seconds": config.cache_ttl_seconds,
        "cache_max_entries": config.cache_max_entries,
        "idle_shutdown_seconds": config.idle_shutdown_seconds,
        "log_level": config.log_level,
        "cors_origins": list(config.cors_origins),
        "plugins_enabled": list(config.plugins_enabled),
    }

    _atomic_write(path, data)
    _secure_config_permissions(path)
    logger.info("Config saved to %s (%d MCP servers)", path, len(config.mcp_servers))


def list_snapshots() -> list[dict[str, Any]]:
    """List all config snapshots, newest first."""
    snapshots_dir = get_snapshots_dir()
    if not snapshots_dir.exists():
        return []
    out = []
    for snap in sorted(snapshots_dir.glob("config-*.json"), reverse=True):
        try:
            with open(snap) as f:
                d = json.load(f)
            mcp_count = len(d.get("mcpServers", d.get("servers", {})))
        except (json.JSONDecodeError, OSError):
            mcp_count = -1
        out.append({
            "path": snap,
            "name": snap.name,
            "mcp_count": mcp_count,
            "size": snap.stat().st_size,
        })
    return out


def restore_snapshot(snapshot_name: str, target: Path | None = None) -> Path:
    """Restore a snapshot to the live config path. Returns the restored path."""
    snap = get_snapshots_dir() / snapshot_name
    if not snap.exists():
        raise FileNotFoundError(f"Snapshot not found: {snap}")

    target = target or get_config_file()

    # Snapshot the current state before restoring (so restore is reversible)
    _snapshot_existing(target)

    import shutil
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snap, target)
    target.chmod(0o600)
    return target


def generate_default_config(config_path: Path | None = None) -> HubConfig:
    """Generate and save a default configuration file."""
    config = HubConfig()
    save_config(config, config_path)
    return config
