"""Config diff — compute the minimal set of add/remove/modify operations
needed to transition a ConnectionManager from one HubConfig to another.

The diff drives hot-reload: it ensures unchanged servers are NOT touched
(critical for kite SSE / OAuth session survival per Charter Feature A4)
while only changed servers go through the drain → replace cycle.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig


@dataclass(frozen=True)
class ConfigDiff:
    """The minimal set of operations to apply to transition between configs.

    Servers in `unchanged` MUST NOT be touched — they keep their live
    connection objects, sessions, and OAuth state.
    """

    added: tuple[MCPServerConfig, ...] = ()
    removed: tuple[str, ...] = ()  # server names
    modified: tuple[MCPServerConfig, ...] = ()  # new configs for modified servers
    unchanged: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    @property
    def change_count(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    def summary(self) -> str:
        return (
            f"+{len(self.added)} ~{len(self.modified)} -{len(self.removed)} "
            f"={len(self.unchanged)} unchanged"
        )


def _fingerprint(server: MCPServerConfig) -> str:
    """Stable hash of a server config — used to detect 'modified' servers.

    Two servers with the same name but different command/args/env/url/enabled
    state produce different fingerprints. Ordering inside dicts (env, headers)
    is normalized via sort_keys.
    """
    payload = {
        "transport": server.transport,
        "command": server.command,
        "args": list(server.args),
        "env": dict(sorted(server.env.items())) if server.env else {},
        "url": server.url,
        "headers": dict(sorted(server.headers.items())) if server.headers else {},
        "enabled": server.enabled,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def diff_configs(old: HubConfig, new: HubConfig) -> ConfigDiff:
    """Compute the diff between two HubConfigs.

    Algorithm:
    - Index both configs by server name.
    - A server in `new` but not `old` → added.
    - A server in `old` but not `new` → removed.
    - A server in both → unchanged if fingerprints match, else modified.

    Disabled servers in `new` are NOT included in `added` or `modified`.
    A previously-enabled server that becomes disabled is treated as `removed`.
    """
    old_by_name = {s.name: s for s in old.mcp_servers}
    new_by_name = {s.name: s for s in new.mcp_servers}

    added: list[MCPServerConfig] = []
    removed: list[str] = []
    modified: list[MCPServerConfig] = []
    unchanged: list[str] = []

    for name, new_srv in new_by_name.items():
        old_srv = old_by_name.get(name)

        # Treat disabled-in-new as if absent (will go to removed below)
        if not new_srv.enabled:
            if old_srv is not None and old_srv.enabled:
                removed.append(name)
            continue

        if old_srv is None or not old_srv.enabled:
            added.append(new_srv)
            continue

        if _fingerprint(old_srv) != _fingerprint(new_srv):
            modified.append(new_srv)
        else:
            unchanged.append(name)

    # Any old enabled server not present in new is removed
    for name, old_srv in old_by_name.items():
        if name not in new_by_name and old_srv.enabled:
            removed.append(name)

    return ConfigDiff(
        added=tuple(added),
        removed=tuple(removed),
        modified=tuple(modified),
        unchanged=tuple(unchanged),
    )
