"""Config reloader — applies a new HubConfig to a live ConnectionManager
through the lifecycle methods, then fires the notifier.

This is the orchestrator for hot-reload. It is intentionally transport-
agnostic: callers (CLI `slm-hub server reload`, future file watcher,
admin API) all hit the same `apply_config` entry point.

Key correctness properties (per Master Plan risk matrix):
- Single asyncio.Lock around the whole reload → concurrent triggers serialize.
- Parse-then-validate-then-apply: if the new config is invalid we abort
  BEFORE any connection is touched, preserving current state.
- Removed servers drain BEFORE we connect new servers, so we don't double-
  spend resources during overlap.
- Notifier fires AFTER all changes are applied — clients see consistent state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from slm_mcp_hub.core.config import HubConfig
from slm_mcp_hub.lifecycle.config_diff import ConfigDiff, diff_configs

if TYPE_CHECKING:
    from slm_mcp_hub.federation.manager import ConnectionManager
    from slm_mcp_hub.lifecycle.notifier import ChangeNotifier

logger = logging.getLogger(__name__)


class ReloadError(Exception):
    """Raised when a reload attempt fails — current state is preserved."""


class ConfigReloader:
    """Apply a new HubConfig to a live ConnectionManager."""

    def __init__(
        self,
        conn_manager: ConnectionManager,
        notifier: ChangeNotifier,
        *,
        drain_timeout_s: float = 30.0,
    ) -> None:
        self._conn_manager = conn_manager
        self._notifier = notifier
        self._drain_timeout_s = drain_timeout_s
        # Serializes reload triggers (CLI vs admin API vs future file-watcher)
        # so two concurrent edits don't race.
        import asyncio
        self._reload_lock: asyncio.Lock = asyncio.Lock()

    async def apply_config(self, new_config: HubConfig) -> ConfigDiff:
        """Diff old vs new, apply the changes, fire notifier.

        Returns the ConfigDiff that was applied. If the diff is empty,
        no operations run and no notification is fired.

        Raises ReloadError if the new config fails basic validation.
        Individual server connection failures during apply are logged but
        do NOT abort the reload — they end up in the manager's _failed map
        for retry-loop handling, same as cold start.
        """
        if not isinstance(new_config, HubConfig):
            raise ReloadError(f"Expected HubConfig, got {type(new_config).__name__}")

        async with self._reload_lock:
            old_config = self._conn_manager.config
            diff = diff_configs(old_config, new_config)

            if diff.is_empty:
                logger.info("Reload: no changes (%s)", diff.summary())
                return diff

            logger.info("Reload starting: %s", diff.summary())

            # Order matters: remove first, then modify, then add.
            # Removing first frees subprocess resources before we spawn
            # potentially-conflicting new processes (e.g., a server that
            # bound a port is now free for its replacement).
            await self._apply_removes(diff)
            await self._apply_modifies(diff)
            await self._apply_adds(diff)

            logger.info("Reload complete: %s", diff.summary())

        # Notify AFTER lock release — notifier has its own debounce + lock.
        if diff.change_count > 0:
            await self._notifier.notify_tools_changed()

        return diff

    async def _apply_removes(self, diff: ConfigDiff) -> None:
        for name in diff.removed:
            try:
                ok, msg = await self._conn_manager.remove_server(
                    name, drain_timeout_s=self._drain_timeout_s,
                )
                if not ok:
                    logger.warning("Reload remove %s: %s", name, msg)
            except Exception as exc:
                logger.error("Reload remove %s crashed: %s", name, exc)

    async def _apply_modifies(self, diff: ConfigDiff) -> None:
        for server_cfg in diff.modified:
            try:
                ok, msg = await self._conn_manager.replace_server(
                    server_cfg, drain_timeout_s=self._drain_timeout_s,
                )
                if not ok:
                    logger.warning("Reload modify %s: %s", server_cfg.name, msg)
            except Exception as exc:
                logger.error("Reload modify %s crashed: %s", server_cfg.name, exc)

    async def _apply_adds(self, diff: ConfigDiff) -> None:
        for server_cfg in diff.added:
            try:
                ok, msg = await self._conn_manager.add_server(server_cfg)
                if not ok:
                    logger.warning("Reload add %s: %s", server_cfg.name, msg)
            except Exception as exc:
                logger.error("Reload add %s crashed: %s", server_cfg.name, exc)
