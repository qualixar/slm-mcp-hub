"""Supervisor-fleet and retry-loop machinery extracted from ConnectionManager.

Extracted from federation/manager.py (W8-P6) to keep that module under the
800-line cap. ConnectionManager retains thin wrapper methods that delegate here,
so all existing call sites (including tests) are unaffected.

All four functions accept a ``ConnectionManager`` instance as first argument and
access instance state directly (``manager._connections``, etc.). This pattern
avoids a circular import while preserving full fidelity to the original behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slm_mcp_hub.federation.manager import ConnectionManager

logger = logging.getLogger(__name__)

# Retry constants are NOT defined here — they live in manager.py so that
# test patches on ``slm_mcp_hub.federation.manager._INITIAL_RETRY_DELAY_S``
# etc. take effect in retry_failed_servers() below (which reads them lazily).


# ---------------------------------------------------------------------------
# Retry-loop bootstrap
# ---------------------------------------------------------------------------


def start_retry_loop(manager: ConnectionManager) -> None:
    """Start per-backend supervisor loops for all currently failed servers.

    W1-P2 replacement for the global ``_retry_failed_servers`` loop.
    Each failed backend gets its own :class:`ConnectionSupervisor` running
    in an isolated asyncio task with exponential backoff + circuit breaker.
    A slow or broken backend cannot stall any other backend.

    The ``_retry_task`` attribute is preserved as a lightweight fleet-
    coordinator task so existing guards that check ``_retry_task.done()``
    remain backward-compatible (e.g. ``test_start_retry_loop_is_idempotent``).
    """
    if manager._retry_task and not manager._retry_task.done():
        return
    manager._retry_task = asyncio.create_task(
        supervisor_fleet_coordinator(manager),
        name="supervisor-fleet-coordinator",
    )


# ---------------------------------------------------------------------------
# Supervisor fleet coordination
# ---------------------------------------------------------------------------


async def ensure_supervisors(manager: ConnectionManager) -> None:
    """Idempotent: start a supervisor for every enabled backend without one.

    Covers all configured backends (not just failed ones) so CONNECTED
    backends that drop unexpectedly also get a supervisor. W1-P3: skips
    FAILED (terminal) backends to prevent churn — an explicit reconnect()
    is required to re-admit them. Idempotency guard: skips if the
    supervisor task is still alive.
    """
    from slm_mcp_hub.federation.connection import (  # noqa: PLC0415
        ConnectionState,
        MCPConnection,
    )
    from slm_mcp_hub.resilience.supervisor import ConnectionSupervisor  # noqa: PLC0415

    for srv_cfg in manager._config.mcp_servers:
        if manager._shutdown:
            break
        if not srv_cfg.enabled:
            continue
        name = srv_cfg.name

        conn = manager._connections.get(name)
        if conn is not None and conn.state == ConnectionState.FAILED:
            continue  # W1-P3: terminal backends not re-supervised (churn guard)

        if name in manager._supervisors:
            existing_task = manager._supervisors[name]._task
            if existing_task is not None and not existing_task.done():
                continue

        if conn is None:
            conn = MCPConnection(srv_cfg)
            manager._connections[name] = conn
            manager._subscribe_bus_to_conn(name, conn)

        sup = ConnectionSupervisor(conn)
        manager._supervisors[name] = sup
        await sup.start()
        logger.debug("Supervisor started/restarted for backend: %s", name)


async def supervisor_fleet_coordinator(manager: ConnectionManager) -> None:
    """Fleet coordinator: start supervisors, tick every 5 s to admit new ones.

    Preserved as ``_retry_task`` for backward-compat idempotency guards.
    """
    await ensure_supervisors(manager)

    while not manager._shutdown:
        await asyncio.sleep(5.0)
        for sup_name, sup in list(manager._supervisors.items()):
            if sup._conn.is_connected and sup_name in manager._failed:
                manager._failed.pop(sup_name, None)
                manager._sync_registry()
        await ensure_supervisors(manager)

    for _name, _sup in list(manager._supervisors.items()):
        try:
            await _sup.stop()
        except Exception:
            logger.exception("Error stopping supervisor for backend: %s", _name)


# ---------------------------------------------------------------------------
# Backward-compat naive retry loop (kept for existing tests)
# ---------------------------------------------------------------------------


async def retry_failed_servers(manager: ConnectionManager) -> None:
    """Backward-compat shim: naive retry loop (kept for existing tests).

    .. deprecated::
        W1-P2 replaced this global loop with per-backend
        :class:`~slm_mcp_hub.resilience.supervisor.ConnectionSupervisor`
        tasks started by :meth:`_start_retry_loop`.  This method is
        preserved because ``test_coverage_gaps2.py`` calls it directly to
        verify shutdown-guard and per-server-iteration behavior.

    Callers that invoke this directly (e.g. tests) get the original
    exponential-backoff behavior.  Production code uses ``_start_retry_loop``
    (which now delegates to the supervisor fleet).

    Retry constants are read lazily from manager.py so that test patches on
    ``slm_mcp_hub.federation.manager._INITIAL_RETRY_DELAY_S`` etc. take effect.
    """
    import slm_mcp_hub.federation.manager as _mgr_mod  # noqa: PLC0415
    delay = _mgr_mod._INITIAL_RETRY_DELAY_S
    max_attempts = _mgr_mod._MAX_RETRY_ATTEMPTS
    max_delay = _mgr_mod._MAX_RETRY_DELAY_S
    attempt = 0

    while not manager._shutdown and manager._failed and attempt < max_attempts:
        attempt += 1
        failed_names = list(manager._failed.keys())
        logger.info(
            "Retry attempt %d/%d for %d failed servers (delay %.0fs): %s",
            attempt, max_attempts, len(failed_names), delay, failed_names,
        )

        await asyncio.sleep(delay)

        if manager._shutdown:
            break

        for name in failed_names:
            if manager._shutdown:
                break
            server_config = next(
                (s for s in manager._config.mcp_servers if s.name == name),
                None,
            )
            if server_config:
                await manager._connect_timed(server_config)

        # Exponential backoff capped at max
        delay = min(delay * 2, max_delay)

    if manager._failed and not manager._shutdown:
        logger.warning(
            "Gave up retrying %d servers after %d attempts: %s",
            len(manager._failed), max_attempts, list(manager._failed.keys()),
        )
