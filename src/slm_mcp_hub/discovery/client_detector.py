"""AI client detection for SLM MCP Hub."""

from __future__ import annotations

import json
import logging
import platform
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClientConfig:
    """Known AI client configuration location."""

    name: str
    display_name: str
    config_paths: tuple[Path, ...]
    mcp_key: str
    config_format: str  # "claude" | "vscode"


@dataclass(frozen=True)
class DetectedClient:
    """A detected AI client installation."""

    name: str
    display_name: str
    config_path: Path
    mcp_count: int
    hub_registered: bool
    config_format: str


def _macos_app_support() -> Path:
    return Path.home() / "Library" / "Application Support"


def _linux_config() -> Path:
    return Path.home() / ".config"


def _build_known_clients() -> tuple[ClientConfig, ...]:
    """Build the known clients registry with platform-appropriate paths."""
    is_mac = platform.system() == "Darwin"

    if is_mac:
        vscode_path = _macos_app_support() / "Code" / "User" / "settings.json"
        cursor_path = _macos_app_support() / "Cursor" / "User" / "settings.json"
    else:
        vscode_path = _linux_config() / "Code" / "User" / "settings.json"
        cursor_path = _linux_config() / "Cursor" / "User" / "settings.json"

    return (
        ClientConfig(
            name="claude_code",
            display_name="Claude Code",
            config_paths=(Path.home() / ".claude.json",),
            mcp_key="mcpServers",
            config_format="claude",
        ),
        ClientConfig(
            name="vscode_copilot",
            display_name="VS Code Copilot",
            config_paths=(vscode_path,),
            mcp_key="mcp.servers",
            config_format="vscode",
        ),
        ClientConfig(
            name="cursor",
            display_name="Cursor",
            config_paths=(cursor_path,),
            mcp_key="mcp.servers",
            config_format="vscode",
        ),
        ClientConfig(
            name="windsurf",
            display_name="Windsurf",
            config_paths=(Path.home() / ".codeium" / "windsurf" / "settings.json",),
            mcp_key="mcpServers",
            config_format="claude",
        ),
        ClientConfig(
            name="codex_cli",
            display_name="Codex CLI",
            config_paths=(Path.home() / ".codex" / "config.json",),
            mcp_key="mcpServers",
            config_format="claude",
        ),
    )


def _extract_mcp_count(data: dict, mcp_key: str) -> int:
    """Extract MCP server count from parsed config JSON."""
    if "." in mcp_key:
        parts = mcp_key.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part, {})
            else:
                return 0
        return len(current) if isinstance(current, dict) else 0
    return len(data.get(mcp_key, {}))


def _check_hub_registered(data: dict, mcp_key: str, hub_name: str = "hub") -> bool:
    """Check if hub is already registered in the client config."""
    if "." in mcp_key:
        parts = mcp_key.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part, {})
            else:
                return False
        servers = current if isinstance(current, dict) else {}
    else:
        servers = data.get(mcp_key, {})

    return hub_name in servers


class ClientDetector:
    """Detects installed AI clients on this machine."""

    def __init__(
        self,
        known_clients: tuple[ClientConfig, ...] | None = None,
    ) -> None:
        self._known_clients = known_clients or _build_known_clients()

    @property
    def known_clients(self) -> tuple[ClientConfig, ...]:
        return self._known_clients

    def detect_all(self) -> tuple[DetectedClient, ...]:
        """Detect all installed AI clients. Returns immutable tuple."""
        results: list[DetectedClient] = []

        for client in self._known_clients:
            detected = self._detect_one(client)
            if detected is not None:
                results.append(detected)

        return tuple(results)

    def _detect_one(self, client: ClientConfig) -> DetectedClient | None:
        """Try to detect a single client. Returns None if not found."""
        for config_path in client.config_paths:
            if not config_path.exists():
                continue

            try:
                text = config_path.read_text(encoding="utf-8")
                data = json.loads(text)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Skipping %s: could not read %s: %s",
                    client.display_name,
                    config_path,
                    exc,
                )
                continue

            mcp_count = _extract_mcp_count(data, client.mcp_key)
            hub_registered = _check_hub_registered(data, client.mcp_key)

            return DetectedClient(
                name=client.name,
                display_name=client.display_name,
                config_path=config_path,
                mcp_count=mcp_count,
                hub_registered=hub_registered,
                config_format=client.config_format,
            )

        return None
