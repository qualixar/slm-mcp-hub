"""Configuration management for SLM MCP Hub."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slm_mcp_hub.core.constants import (
    CACHE_DEFAULT_TTL_SECONDS,
    CACHE_MAX_ENTRIES,
    CONFIG_DIR,
    CONFIG_FILE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    IDLE_SHUTDOWN_SECONDS,
    MAX_SESSIONS,
    SESSION_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


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
    config_dir: Path = CONFIG_DIR
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


def _resolve_env_in_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively resolve environment variables in a dict."""
    result = {}
    for key, val in d.items():
        if isinstance(val, str):
            result[key] = _resolve_env_vars(val)
        elif isinstance(val, dict):
            result[key] = _resolve_env_in_dict(val)
        elif isinstance(val, list):
            result[key] = [
                _resolve_env_vars(v) if isinstance(v, str) else v for v in val
            ]
        else:
            result[key] = val
    return result


def parse_mcp_server(name: str, raw: dict[str, Any]) -> MCPServerConfig:
    """Parse a single MCP server entry from config JSON."""
    resolved = _resolve_env_in_dict(raw)

    if "url" in resolved:
        transport = resolved.get("type", "http")
        return MCPServerConfig(
            name=name,
            transport=transport,
            url=resolved["url"],
            headers=resolved.get("headers", {}),
            enabled=resolved.get("enabled", True),
            always_on=resolved.get("always_on", False),
            no_cache=resolved.get("no_cache", False),
            cost_per_call_cents=resolved.get("cost_per_call_cents", 0.0),
        )

    command = resolved.get("command", "")
    args = tuple(resolved.get("args", []))
    env = resolved.get("env", {})
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=command,
        args=args,
        env=env,
        enabled=resolved.get("enabled", True),
        always_on=resolved.get("always_on", False),
        no_cache=resolved.get("no_cache", False),
        cost_per_call_cents=resolved.get("cost_per_call_cents", 0.0),
    )


def load_config(config_path: Path | None = None) -> HubConfig:
    """Load hub configuration from file, with env var overrides."""
    path = config_path or CONFIG_FILE

    if not path.exists():
        logger.info("No config file found at %s, using defaults", path)
        return _apply_env_overrides(HubConfig())

    with open(path) as f:
        raw = json.load(f)

    servers_raw = raw.get("mcpServers", raw.get("servers", {}))
    servers = tuple(
        parse_mcp_server(name, cfg) for name, cfg in servers_raw.items()
    )

    config = HubConfig(
        host=raw.get("host", DEFAULT_HOST),
        port=raw.get("port", DEFAULT_PORT),
        config_dir=Path(raw.get("config_dir", str(CONFIG_DIR))),
        mcp_servers=servers,
        session_timeout_seconds=raw.get("session_timeout_seconds", SESSION_TIMEOUT_SECONDS),
        max_sessions=raw.get("max_sessions", MAX_SESSIONS),
        cache_ttl_seconds=raw.get("cache_ttl_seconds", CACHE_DEFAULT_TTL_SECONDS),
        cache_max_entries=raw.get("cache_max_entries", CACHE_MAX_ENTRIES),
        idle_shutdown_seconds=raw.get("idle_shutdown_seconds", IDLE_SHUTDOWN_SECONDS),
        log_level=raw.get("log_level", "INFO"),
        cors_origins=tuple(raw.get("cors_origins", ["*"])),
        plugins_enabled=tuple(raw.get("plugins_enabled", [])),
    )

    return _apply_env_overrides(config)


def _apply_env_overrides(config: HubConfig) -> HubConfig:
    """Apply environment variable overrides to config. Returns new config."""
    port = int(os.environ.get("SLM_HUB_PORT", config.port))
    host = os.environ.get("SLM_HUB_HOST", config.host)
    log_level = os.environ.get("SLM_HUB_LOG_LEVEL", config.log_level)

    if port == config.port and host == config.host and log_level == config.log_level:
        return config

    return HubConfig(
        host=host,
        port=port,
        config_dir=config.config_dir,
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


def save_config(config: HubConfig, config_path: Path | None = None) -> None:
    """Save hub configuration to JSON file."""
    path = config_path or CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    servers_dict = {}
    for srv in config.mcp_servers:
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

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    logger.info("Config saved to %s (%d MCP servers)", path, len(config.mcp_servers))


def generate_default_config(config_path: Path | None = None) -> HubConfig:
    """Generate and save a default configuration file."""
    config = HubConfig()
    save_config(config, config_path)
    return config
