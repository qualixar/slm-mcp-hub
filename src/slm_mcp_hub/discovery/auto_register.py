"""Auto-registration of hub with AI clients."""

from __future__ import annotations

import copy
import json
import logging
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from slm_mcp_hub.core.config import (
    MCPServerConfig,
    parse_mcp_server,
    load_config,
    save_config,
)
from slm_mcp_hub.core.constants import DEFAULT_PORT
from slm_mcp_hub.discovery.client_detector import DetectedClient

logger = logging.getLogger(__name__)

HUB_ENTRY_NAME = "hub"


@dataclass(frozen=True)
class RegistrationPlan:
    """What register() would do in dry-run mode."""

    client_name: str
    config_path: Path
    hub_entry: dict[str, Any]
    backup_path: Path
    already_registered: bool


@dataclass(frozen=True)
class RegistrationResult:
    """Result of a registration operation."""

    success: bool
    client_name: str
    config_path: Path
    backup_path: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class ImportResult:
    """Result of importing MCPs from a client."""

    source_name: str
    imported_count: int
    skipped_count: int
    total_in_source: int


def _build_hub_entry(hub_url: str) -> dict[str, Any]:
    """Build the hub MCP entry for any client config."""
    return {"type": "http", "url": hub_url}


def _read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON file."""
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write data as formatted JSON."""
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _inject_hub_entry(
    data: dict[str, Any],
    mcp_key: str,
    entry_name: str,
    entry_value: dict[str, Any],
) -> dict[str, Any]:
    """Return a NEW dict with the hub entry injected into the MCP section.

    Never mutates the input dict — creates a deep copy first.
    """
    result = copy.deepcopy(data)
    section = _ensure_section(result, mcp_key)
    section[entry_name] = entry_value
    return result


def _remove_from_section(
    data: dict[str, Any],
    mcp_key: str,
    entry_name: str,
) -> dict[str, Any]:
    """Return a NEW dict with the entry removed from the MCP section."""
    result = copy.deepcopy(data)
    section = _ensure_section(result, mcp_key)
    section.pop(entry_name, None)
    return result


def _ensure_section(data: dict[str, Any], mcp_key: str) -> dict[str, Any]:
    """Navigate nested keys, creating intermediates as needed. Mutates data in place."""
    if "." in mcp_key:
        parts = mcp_key.split(".")
        current = data
        for part in parts:
            if part not in current:
                current[part] = {}
            current = current[part]
        return current
    if mcp_key not in data:
        data[mcp_key] = {}
    return data[mcp_key]


def _get_section_readonly(data: dict[str, Any], mcp_key: str) -> dict[str, Any]:
    """Read-only navigation of nested keys. Returns empty dict if missing."""
    if "." in mcp_key:
        parts = mcp_key.split(".")
        current: Any = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part, {})
            else:
                return {}
        return current if isinstance(current, dict) else {}
    return data.get(mcp_key, {})


class AutoRegister:
    """Register and unregister hub in AI client configurations."""

    def __init__(self, hub_url: str | None = None) -> None:
        self._hub_url = hub_url or f"http://127.0.0.1:{DEFAULT_PORT}/mcp"

    @property
    def hub_url(self) -> str:
        return self._hub_url

    def plan(self, client: DetectedClient) -> RegistrationPlan:
        """Generate a registration plan without modifying anything."""
        backup_path = client.config_path.parent / (client.config_path.name + ".pre-hub-backup")
        hub_entry = _build_hub_entry(self._hub_url)

        return RegistrationPlan(
            client_name=client.name,
            config_path=client.config_path,
            hub_entry=hub_entry,
            backup_path=backup_path,
            already_registered=client.hub_registered,
        )

    def register(
        self,
        client: DetectedClient,
        mcp_key: str = "mcpServers",
        dry_run: bool = False,
    ) -> RegistrationResult | RegistrationPlan:
        """Register the hub with a client. Returns plan if dry_run=True."""
        if dry_run:
            return self.plan(client)

        if client.hub_registered:
            return RegistrationResult(
                success=True,
                client_name=client.name,
                config_path=client.config_path,
                error="already_registered",
            )

        backup_path = client.config_path.parent / (
            client.config_path.name + ".pre-hub-backup"
        )

        try:
            data = _read_json(client.config_path)
        except (json.JSONDecodeError, OSError) as exc:
            return RegistrationResult(
                success=False,
                client_name=client.name,
                config_path=client.config_path,
                error=f"Failed to read config: {exc}",
            )

        # Create backup BEFORE modifying
        try:
            shutil.copy2(client.config_path, backup_path)
        except OSError as exc:
            return RegistrationResult(
                success=False,
                client_name=client.name,
                config_path=client.config_path,
                error=f"Failed to create backup: {exc}",
            )

        # Inject hub entry (immutable — creates new dict)
        updated_data = _inject_hub_entry(
            data, mcp_key, HUB_ENTRY_NAME, _build_hub_entry(self._hub_url)
        )

        try:
            _write_json(client.config_path, updated_data)
        except OSError as exc:
            # Restore from backup on write failure
            try:
                shutil.copy2(backup_path, client.config_path)
            except OSError:
                pass
            return RegistrationResult(
                success=False,
                client_name=client.name,
                config_path=client.config_path,
                backup_path=backup_path,
                error=f"Failed to write config: {exc}",
            )

        logger.info(
            "Registered hub with %s at %s",
            client.display_name,
            client.config_path,
        )

        return RegistrationResult(
            success=True,
            client_name=client.name,
            config_path=client.config_path,
            backup_path=backup_path,
        )

    def unregister(
        self,
        client: DetectedClient,
        mcp_key: str = "mcpServers",
    ) -> RegistrationResult:
        """Remove hub entry from a client config."""
        try:
            data = _read_json(client.config_path)
        except (json.JSONDecodeError, OSError) as exc:
            return RegistrationResult(
                success=False,
                client_name=client.name,
                config_path=client.config_path,
                error=f"Failed to read config: {exc}",
            )

        servers = _get_section_readonly(data, mcp_key)
        if HUB_ENTRY_NAME not in servers:
            return RegistrationResult(
                success=True,
                client_name=client.name,
                config_path=client.config_path,
                error="not_registered",
            )

        updated_data = _remove_from_section(data, mcp_key, HUB_ENTRY_NAME)

        try:
            _write_json(client.config_path, updated_data)
        except OSError as exc:
            return RegistrationResult(
                success=False,
                client_name=client.name,
                config_path=client.config_path,
                error=f"Failed to write config: {exc}",
            )

        logger.info("Unregistered hub from %s", client.display_name)
        return RegistrationResult(
            success=True,
            client_name=client.name,
            config_path=client.config_path,
        )

    def register_transparent(
        self,
        client: DetectedClient,
        server_names: list[str],
        mcp_key: str = "mcpServers",
        dry_run: bool = False,
    ) -> RegistrationResult:
        """Register hub in transparent proxy mode.

        Replaces each MCP entry with an HTTP entry pointing to
        /mcp/{server_name}. Original tool names are preserved.
        Claude sees identical tool names as direct connection.
        """
        try:
            data = _read_json(client.config_path)
        except (json.JSONDecodeError, OSError) as exc:
            return RegistrationResult(
                success=False,
                client_name=client.name,
                config_path=client.config_path,
                error=f"Failed to read config: {exc}",
            )

        backup_path = client.config_path.parent / (
            client.config_path.name + ".pre-hub-backup"
        )

        # Build new MCP entries: each server → HTTP to hub's per-server endpoint
        base_url = self._hub_url.rstrip("/mcp").rstrip("/")
        new_servers: dict[str, Any] = {}
        for name in server_names:
            new_servers[name] = {
                "type": "http",
                "url": f"{base_url}/mcp/{name}",
            }

        if dry_run:
            return RegistrationResult(
                success=True,
                client_name=client.name,
                config_path=client.config_path,
                backup_path=backup_path,
                error=f"dry_run:would_replace_{len(new_servers)}_servers",
            )

        # Create backup
        try:
            shutil.copy2(client.config_path, backup_path)
        except OSError as exc:
            return RegistrationResult(
                success=False,
                client_name=client.name,
                config_path=client.config_path,
                error=f"Failed to create backup: {exc}",
            )

        # Replace the MCP section with transparent proxy entries
        updated_data = copy.deepcopy(data)
        _ensure_section(updated_data, mcp_key)
        if "." in mcp_key:
            parts = mcp_key.split(".")
            current = updated_data
            for part in parts[:-1]:
                current = current[part]
            current[parts[-1]] = new_servers
        else:
            updated_data[mcp_key] = new_servers

        try:
            _write_json(client.config_path, updated_data)
        except OSError as exc:
            try:
                shutil.copy2(backup_path, client.config_path)
            except OSError:
                pass
            return RegistrationResult(
                success=False,
                client_name=client.name,
                config_path=client.config_path,
                backup_path=backup_path,
                error=f"Failed to write config: {exc}",
            )

        logger.info(
            "Registered %d servers in transparent mode for %s",
            len(new_servers),
            client.display_name,
        )
        return RegistrationResult(
            success=True,
            client_name=client.name,
            config_path=client.config_path,
            backup_path=backup_path,
        )

    def import_mcps(
        self,
        source_path: Path,
        config_format: str = "claude",
        hub_config_path: Path | None = None,
    ) -> ImportResult:
        """Import MCP servers from a client config into hub config."""
        try:
            data = _read_json(source_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read source config %s: %s", source_path, exc)
            return ImportResult(
                source_name=source_path.name,
                imported_count=0,
                skipped_count=0,
                total_in_source=0,
            )

        if config_format == "claude":
            servers_raw = data.get("mcpServers", {})
        else:
            servers_raw = data.get("servers", data.get("mcp", {}).get("servers", {}))

        total = len(servers_raw)

        source_servers = [
            parse_mcp_server(name, cfg) for name, cfg in servers_raw.items()
        ]

        # Load existing hub config
        existing = load_config(hub_config_path)
        existing_names = {s.name for s in existing.mcp_servers}

        new_servers = [s for s in source_servers if s.name not in existing_names]
        skipped = len(source_servers) - len(new_servers)

        if new_servers:
            merged = tuple(list(existing.mcp_servers) + new_servers)
            updated = replace(existing, mcp_servers=merged)
            save_config(updated, hub_config_path)

        return ImportResult(
            source_name=source_path.name,
            imported_count=len(new_servers),
            skipped_count=skipped,
            total_in_source=total,
        )
