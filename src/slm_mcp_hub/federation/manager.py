"""Connection manager — spawns, monitors, and syncs all MCP server connections."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import TYPE_CHECKING, Any

from slm_mcp_hub.core.config import HubConfig, MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.eviction import IdleReaper
from slm_mcp_hub.federation.status import build_server_status
from slm_mcp_hub.resilience.events import LifecycleEventBus, WebhookDispatcher

if TYPE_CHECKING:
    from slm_mcp_hub.resilience.supervisor import ConnectionSupervisor

logger = logging.getLogger(__name__)

# Retry config — constants kept here so tests can patch them via
# `patch("slm_mcp_hub.federation.manager._INITIAL_RETRY_DELAY_S", ...)`.
# manager_retry.py reads them lazily through this module's namespace.
_INITIAL_RETRY_DELAY_S = 5.0
_MAX_RETRY_DELAY_S = 120.0
_MAX_RETRY_ATTEMPTS = 5

# Fast cold-start retry schedule (Phase 6, Charter Feature C1).
# Called explicitly by `slm-hub start` after the initial connect_all()
# parallel attempt. Slow background _retry_failed_servers() still runs
# afterwards for any servers that still fail.
_FAST_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.5, 4.5)


class ConnectionManager:
    """Manages the lifecycle of all MCP server connections.

    Features:
    - Prioritized startup: stdio first (fast), then HTTP (slower)
    - Progressive registry sync: tools available as each server connects
    - Dynamic speed tracking: records connection time per server
    - Background retry: failed servers retry with exponential backoff
    - Manual reconnect: reconnect any server on demand
    """

    def __init__(
        self,
        config: HubConfig,
        registry: CapabilityRegistry,
    ) -> None:
        self._config = config
        self._registry = registry
        self._connections: dict[str, MCPConnection] = {}
        self._failed: dict[str, str] = {}
        self._connect_times: dict[str, float] = {}
        self._retry_task: asyncio.Task | None = None
        self._shutdown = False
        # Serializes all mutation operations (add/remove/modify/sync).
        # Reads (route_tool_call, registry lookups) are lock-free — atomic swap
        # on registry means reads see consistent old-or-new state, never partial.
        self._lock: asyncio.Lock = asyncio.Lock()
        # Optional notifier — set by HubRuntime so we can fire
        # notifications/tools/list_changed when the registry actually changes.
        # When None (e.g. in unit tests), changes are silent.
        self._notifier: Any | None = None
        # W1-P2: one supervisor per backend (populated lazily; see _start_retry_loop).
        self._supervisors: dict[str, ConnectionSupervisor] = {}
        # W1-P4: in-process lifecycle event bus — all connections subscribe to it.
        self._event_bus: LifecycleEventBus = LifecycleEventBus()
        # W1-P4: per-connection bus unsubscribe callables (cleaned up on reconnect).
        self._bus_unsubs: dict[str, Any] = {}
        # W2-P1: in-flight event per backend — prevents double-spawn under
        # concurrent connect_all / connect_one calls for the same backend.
        # A second concurrent caller awaits the event instead of spawning.
        self._connect_events: dict[str, asyncio.Event] = {}
        # W3-P1: capability cache — populated by evict(), cleared by disconnect/remove.
        self._evicted_caps: dict[str, dict[str, Any]] = {}
        # W3-P2: idle reaper (spawn=="lazy" only; no-op when idle_ttl_seconds==0).
        self._reaper: IdleReaper = IdleReaper(
            config=config,
            evict_fn=self.evict,
            get_backends_fn=lambda: self._config.mcp_servers,
            is_live_fn=lambda n: n in self._connections and self._connections[n].is_connected,
            # CRIT-5: never reap a backend with an in-flight routed call. The
            # connection's own in_flight_count (used for drain) is the single
            # source of truth — race-tight (incremented synchronously at dispatch).
            has_inflight_fn=lambda n: (
                n in self._connections and self._connections[n].in_flight_count > 0
            ),
        )
        # W1-P4: optional webhook dispatcher (created if config.webhooks is non-empty).
        self._webhook_dispatcher: WebhookDispatcher | None = (
            WebhookDispatcher(list(config.webhooks))
            if config.webhooks
            else None
        )
        if self._webhook_dispatcher is not None:
            self._event_bus.register_consumer(self._webhook_dispatcher.enqueue)

    def set_notifier(self, notifier: Any | None) -> None:
        """Attach a ChangeNotifier. HubRuntime wires this after construction."""
        self._notifier = notifier

    def mark_activity(self, name: str) -> None:
        """Record backend activity — resets idle timer (W3-P2)."""
        self._reaper.mark_activity(name)

    @property
    def config(self) -> HubConfig:
        """Current effective config (mutable: add_server/remove_server/replace_server
        update this in place so subsequent reload diffs see the latest state)."""
        return self._config

    @property
    def connections(self) -> dict[str, MCPConnection]:
        return self._connections

    @property
    def connected_count(self) -> int:
        return sum(1 for c in self._connections.values() if c.is_connected)

    @property
    def failed_servers(self) -> dict[str, str]:
        return dict(self._failed)

    @property
    def connect_times(self) -> dict[str, float]:
        return dict(self._connect_times)

    def _subscribe_bus_to_conn(self, name: str, conn: MCPConnection) -> None:
        """Subscribe the event bus to *conn*, cleaning up any prior subscription.

        Called whenever a new :class:`MCPConnection` is stored in
        ``self._connections`` so that all lifecycle transitions flow into the bus.
        If a prior subscription exists for *name* (e.g. after a reconnect that
        creates a fresh connection object), the old subscription is cancelled first
        to avoid double-delivery.
        """
        old_unsub = self._bus_unsubs.pop(name, None)
        if old_unsub is not None:
            old_unsub()
        self._bus_unsubs[name] = conn.subscribe(self._event_bus.emit)

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a point-in-time health snapshot for all configured backends.

        Each entry contains the same fields as the supervisor's
        :meth:`~slm_mcp_hub.resilience.supervisor.ConnectionSupervisor.health_snapshot`
        plus the ``lifecycle`` state string from the connection.

        W1-P4 — consumed by the event bus health aggregator, the ``/api/health``
        endpoint (extended in this packet), and the CLI ``status`` command.

        Returns
        -------
        dict[str, dict[str, Any]]
            ``{server_name: {lifecycle, needs_attention, consecutive_failures,
            restart_count, last_error, last_transition_ts, breaker_open,
            breaker_open_cycles}}``
        """
        snapshot: dict[str, dict[str, Any]] = {}
        for srv in self._config.mcp_servers:
            conn = self._connections.get(srv.name)
            supervisor = self._supervisors.get(srv.name)
            snapshot[srv.name] = {
                "lifecycle": (
                    conn.state.value
                    if conn is not None
                    else ConnectionState.DISCONNECTED.value
                ),
                "needs_attention": supervisor.needs_attention if supervisor else False,
                "consecutive_failures": (
                    supervisor.consecutive_failures if supervisor else 0
                ),
                "restart_count": supervisor.restart_count if supervisor else 0,
                "last_error": supervisor.last_error if supervisor else None,
                "last_transition_ts": (
                    supervisor.last_transition_ts if supervisor else 0.0
                ),
                "breaker_open": supervisor.breaker_open if supervisor else False,
                "breaker_open_cycles": (
                    supervisor.breaker_open_cycles if supervisor else 0
                ),
            }
        return snapshot

    async def connect_all(self) -> dict[str, str]:
        """Connect to all enabled MCP servers with prioritized ordering.

        Stdio servers connect first (local, fast), then HTTP servers
        (network, potentially slower). Each server's tools become available
        immediately after it connects. Failed servers are retried in background.

        Returns dict of {server_name: error_message} for initially failed servers.
        """
        # W1-P4: start webhook dispatcher (if configured) before any connections
        # so events from the initial connect burst are captured.
        if self._webhook_dispatcher is not None:
            await self._webhook_dispatcher.start()

        enabled = [s for s in self._config.mcp_servers if s.enabled]

        if not enabled:
            logger.info("No MCP servers configured")
            return {}

        stdio = [s for s in enabled if s.transport == "stdio"]
        http = [s for s in enabled if s.transport != "stdio"]

        logger.info(
            "Connecting to %d MCP servers (%d stdio, %d http)...",
            len(enabled), len(stdio), len(http),
        )

        # W2-P1: semaphore caps concurrent _connect_timed calls to prevent the
        # startup thundering-herd (N subprocesses / port-bind races at once).
        # The semaphore is local to this connect_all invocation so independent
        # reload calls don't share a cap.  Phase ordering is enforced by running
        # two separate gather calls — http tasks do NOT start until the stdio
        # gather resolves completely.
        sem = asyncio.Semaphore(self._config.startup_max_concurrency)

        async def _bounded(s: MCPServerConfig) -> None:
            async with sem:
                await self._connect_timed(s)

        # Phase 1: stdio servers — local processes, connect fast.
        # Fully awaited before Phase 2 starts (ordering guarantee).
        if stdio:
            await asyncio.gather(*(_bounded(s) for s in stdio))
            logger.info(
                "Stdio phase: %d connected, %d tools",
                self.connected_count, self._registry.tool_count,
            )

        # Phase 2: HTTP servers — network, may be slower.
        # Only reached after ALL stdio tasks have completed.
        if http:
            await asyncio.gather(*(_bounded(s) for s in http))

        logger.info(
            "All phases: %d/%d servers, %d tools",
            self.connected_count, len(enabled), self._registry.tool_count,
        )

        # Start background retry for any failed servers
        if self._failed:
            self._start_retry_loop()

        await self._reaper.start()  # W3-P2: no-op when idle_ttl_seconds==0

        return dict(self._failed)

    async def reconnect(self, server_name: str) -> tuple[bool, str]:
        """Reconnect a single server by name. Returns (success, message)."""
        server_config = next(
            (s for s in self._config.mcp_servers if s.name == server_name),
            None,
        )
        if server_config is None:
            return False, f"Server '{server_name}' not found in config"

        # Disconnect if already connected
        existing = self._connections.get(server_name)
        if existing and existing.is_connected:
            await self._disconnect_one(server_name)

        # Connect fresh
        await self._connect_timed(server_config)

        if server_name in self._failed:
            return False, f"Failed: {self._failed[server_name]}"

        tool_count = len(self._connections[server_name].capabilities.get("tools", []))
        return True, f"Connected: {tool_count} tools"

    async def connect_one(self, server_name: str) -> bool:
        """Connect to a single server by name. Returns True on success."""
        ok, _ = await self.reconnect(server_name)
        return ok

    async def disconnect_all(self) -> None:
        """Disconnect all MCP server connections and stop retry loop."""
        self._shutdown = True

        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            self._retry_task = None

        await self._reaper.stop()  # W3-P2: cancel + await reaper task

        # W1-P4: stop webhook dispatcher (if running) and clear bus subscriptions.
        if self._webhook_dispatcher is not None:
            await self._webhook_dispatcher.stop()
        for _unsub in list(self._bus_unsubs.values()):
            try:
                _unsub()
            except Exception:
                pass
        self._bus_unsubs.clear()

        # W1-P2: stop all per-backend supervisors before disconnecting.
        # Per-supervisor try/except so a failed stop on one backend never
        # silently masks a failure on another (MINOR 1 fix: no return_exceptions).
        if self._supervisors:
            for _sup_name, _sup in list(self._supervisors.items()):
                try:
                    await _sup.stop()
                except Exception:
                    logger.exception(
                        "Error stopping supervisor for backend: %s", _sup_name
                    )
            self._supervisors.clear()

        tasks = [self._disconnect_one(name) for name in list(self._connections)]
        if tasks:
            await asyncio.gather(*tasks)

        self._connections = {}
        self._failed = {}
        self._evicted_caps.clear()  # W3-P1: prevent stale cache on reuse
        self._registry.clear()
        logger.info("All MCP connections closed")

    async def disconnect_one(self, server_name: str) -> None:
        """Disconnect a single server and remove it from the live connection map.

        W3-P1: clears evicted capability cache. W3-P2: forgets activity tracking.
        """
        async with self._lock:
            await self._disconnect_one(server_name)
            self._connections.pop(server_name, None)
            self._failed.pop(server_name, None)
            self._connect_times.pop(server_name, None)
            self._evicted_caps.pop(server_name, None)  # W3-P1
            self._reaper.forget(server_name)  # W3-P2
            self._sync_registry()

    async def evict(self, name: str, drain_timeout_s: float = 5.0) -> None:
        """Evict a backend — free its subprocess/RAM while retaining capabilities.

        W3-P1: pinned backends (spawn=="pinned" OR always_on=True) are never
        evicted. A missing backend is a safe no-op. On success, capabilities
        are deep-copied into ``_evicted_caps`` before teardown so
        ``_sync_registry`` continues to surface the cached tools via
        search_tools/list_servers. Only an explicit evict() populates
        _evicted_caps — connect failures must never call this.

        Args:
            name: Server name to evict.
            drain_timeout_s: Max seconds to wait for in-flight calls (default 5).
        """
        srv_cfg = next(
            (s for s in self._config.mcp_servers if s.name == name), None
        )
        if srv_cfg is not None and srv_cfg.is_pinned:
            logger.warning(
                "evict(%s) called on a pinned backend — no-op (spawn=%r, always_on=%r)",
                name, srv_cfg.spawn, srv_cfg.always_on,
            )
            return

        conn = self._connections.get(name)
        if conn is None:
            logger.debug("evict(%s): not in active connections — no-op", name)
            return

        # Deep-copy before teardown so the cache is not a live reference.
        self._evicted_caps[name] = copy.deepcopy(conn.capabilities)
        logger.info(
            "Evicting %s — cached %d tools, freeing subprocess",
            name, len(self._evicted_caps[name].get("tools", [])),
        )

        try:
            await conn.drain_and_disconnect(timeout_s=drain_timeout_s)
        except Exception:
            logger.exception("drain_and_disconnect error evicting %s", name)

        async with self._lock:
            # W3-P2: clear activity tracking so a later reconnect
            # (route OR admin/manager.reconnect) re-seeds a FRESH timestamp
            # instead of inheriting this stale one and being reaped next sweep.
            self._reaper.forget(name)
            self._sync_registry()

    async def add_server(self, server_config: MCPServerConfig) -> tuple[bool, str]:
        """Add and connect a new server at runtime. No hub restart required.

        Returns (success, message). On success, the server's tools are
        immediately available via hub__call_tool. Other connections are
        untouched — kite SSE sessions survive this call.
        """
        async with self._lock:
            name = server_config.name
            if name in self._connections and self._connections[name].is_connected:
                return False, f"Server '{name}' is already connected"

            # Persist to in-memory config so reconnect/status pick it up
            existing_names = {s.name for s in self._config.mcp_servers}
            if name not in existing_names:
                from dataclasses import replace as dc_replace
                new_servers = tuple(self._config.mcp_servers) + (server_config,)
                self._config = dc_replace(self._config, mcp_servers=new_servers)

            await self._connect_timed(server_config)

        if name in self._failed:
            return False, f"Failed to connect: {self._failed[name]}"
        tool_count = len(self._connections[name].capabilities.get("tools", []))
        return True, f"Connected: {tool_count} tools"

    async def remove_server(self, server_name: str, drain_timeout_s: float = 30.0) -> tuple[bool, str]:
        """Drain in-flight calls then disconnect and deregister a server.

        Existing connections to OTHER servers are untouched. New tool calls
        to this server are rejected immediately (DRAINING state). In-flight
        calls are given drain_timeout_s to complete before force-disconnect.
        """
        conn = self._connections.get(server_name)
        if conn is None:
            return False, f"Server '{server_name}' not found in active connections"

        # Drain outside the lock — drain waits on asyncio events which may
        # themselves need the event loop. Holding the lock during drain would
        # deadlock any concurrent add_server call.
        await conn.drain_and_disconnect(timeout_s=drain_timeout_s)

        async with self._lock:
            self._connections.pop(server_name, None)
            self._failed.pop(server_name, None)
            self._connect_times.pop(server_name, None)
            self._evicted_caps.pop(server_name, None)  # W3-P1: drop cache on removal
            self._reaper.forget(server_name)  # W3-P2: drop activity tracking
            # Remove from in-memory config
            from dataclasses import replace as dc_replace
            new_servers = tuple(s for s in self._config.mcp_servers if s.name != server_name)
            self._config = dc_replace(self._config, mcp_servers=new_servers)
            self._sync_registry()

        logger.info("Removed server: %s", server_name)
        return True, f"Removed and deregistered: {server_name}"

    async def replace_server(self, server_config: MCPServerConfig, drain_timeout_s: float = 30.0) -> tuple[bool, str]:
        """Restart a single server in-place (e.g. after env/config change).

        Drains the old connection, then connects fresh with the new config.
        Other connections are untouched.
        """
        name = server_config.name
        old_conn = self._connections.get(name)

        if old_conn is not None:
            await old_conn.drain_and_disconnect(timeout_s=drain_timeout_s)
            async with self._lock:
                self._connections.pop(name, None)
                self._failed.pop(name, None)
                # Replace config entry
                from dataclasses import replace as dc_replace
                new_servers = tuple(
                    server_config if s.name == name else s
                    for s in self._config.mcp_servers
                )
                self._config = dc_replace(self._config, mcp_servers=new_servers)

        ok, msg = await self.add_server(server_config)
        return ok, msg

    async def fast_retry_failed(self) -> dict[str, str]:
        """Quick retry pass for servers that failed in connect_all().

        Schedule: 0.5s → 1.5s → 4.5s gaps between attempts (Charter C1).
        Called explicitly by `slm-hub start` cold path so that unit tests
        which simulate connection failures aren't slowed by 6.5s of sleep.

        After this returns, the slow background _retry_failed_servers()
        loop continues to retry anything still failing.
        """
        for delay in _FAST_RETRY_DELAYS_S:
            if not self._failed or self._shutdown:
                break
            await asyncio.sleep(delay)
            failed_names = list(self._failed.keys())
            logger.info(
                "Fast cold-start retry (delay %.1fs) for %d servers: %s",
                delay, len(failed_names), failed_names,
            )
            for name in failed_names:
                srv_cfg = next(
                    (s for s in self._config.mcp_servers if s.name == name),
                    None,
                )
                if srv_cfg is None:
                    continue
                # Clean up the previous failed connection object before retrying
                old = self._connections.pop(name, None)
                if old is not None:
                    try:
                        await old.disconnect()
                    except Exception:
                        pass
                async with self._lock:
                    await self._connect_timed(srv_cfg)
        return dict(self._failed)

    def get_server_status(self) -> list[dict[str, Any]]:
        """Get status of all servers including connection times.

        Delegates to the pure :func:`~slm_mcp_hub.federation.status.build_server_status`
        (extracted W3-P2 to keep this module under the 800-line cap).
        """
        return build_server_status(
            self._config.mcp_servers,
            self._connections,
            self._supervisors,
            self._evicted_caps,
            self._connect_times,
            self._failed,
        )

    async def ensure_connected(self, name: str) -> bool:
        """Reconnect an evicted backend on demand (W3-P3). Idempotent via W2-P1 gate."""
        conn = self._connections.get(name)
        if conn is not None and conn.is_connected:
            return True
        srv_cfg = next(
            (s for s in self._config.mcp_servers if s.name == name), None
        )
        if srv_cfg is None:
            return False
        if not srv_cfg.is_pinned:
            await self._apply_lru_cap(name)
        await self._connect_timed(srv_cfg)
        conn = self._connections.get(name)
        return conn is not None and conn.is_connected

    async def _apply_lru_cap(self, connecting_name: str) -> None:
        """Evict the LRU non-pinned backend if the max_live_backends cap is hit (W3-P3).

        SOFT cap by design (hardened in v0.3): unlocked check->evict, so concurrent
        ensure_connected() can transiently overshoot by the concurrency degree (bounded,
        never unbounded; the reaper re-trims). Reconnect-path only; boot harvests then
        trims. Strict hard cap (dedicated capacity lock) is a W7 follow-up.
        """
        if self._config.max_live_backends == 0:
            return
        pinned = {s.name for s in self._config.mcp_servers if s.is_pinned}
        live = [
            n for n, c in self._connections.items()
            if c.is_connected and n not in pinned and n != connecting_name
        ]
        if len(live) < self._config.max_live_backends:
            return
        from slm_mcp_hub.federation.lru import select_lru_victim  # noqa: PLC0415
        victim = select_lru_victim(live, self._reaper._last_activity)
        if victim is not None:
            await self.evict(victim)

    # -- Internal --

    async def _connect_timed(
        self,
        server_config: MCPServerConfig,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Connect to one server with timeout, track time, sync registry.

        If connection takes longer than timeout_seconds, mark as failed
        and move on — don't block other servers.

        W2-P1 idempotency guard: if a concurrent caller is already running
        this method for the same backend (e.g. two concurrent connect_all
        calls, or a connect_all racing a connect_one), the second caller
        awaits the completion event and returns without spawning a second
        subprocess.  This is a JOIN, not a no-op: the second caller still
        returns once the connection attempt finishes.
        """
        name = server_config.name

        # --- W2-P1 idempotency gate ---
        # The check-and-insert is asyncio-atomic (no await between them), so a
        # second concurrent caller that reaches this point always sees the event
        # created by the first caller.  It awaits the event (JOIN) and returns
        # immediately once the first caller's connection attempt completes.
        if name in self._connect_events:
            await self._connect_events[name].wait()
            return

        connect_done = asyncio.Event()
        self._connect_events[name] = connect_done
        # ------------------------------

        start = time.monotonic()
        try:
            conn = MCPConnection(server_config)
            self._connections[name] = conn
            # W1-P4: subscribe the event bus to this new connection.
            self._subscribe_bus_to_conn(name, conn)

            try:
                await asyncio.wait_for(conn.connect(), timeout=timeout_seconds)
                elapsed = time.monotonic() - start
                self._connect_times[name] = elapsed
                self._failed.pop(name, None)
                self._evicted_caps.pop(name, None)  # W3-P1: live caps take over
                self._reaper.seed_activity(name)  # W3-P2: start idle timer
                logger.info(
                    "Connected to %s: %d tools (%.1fs)",
                    name,
                    len(conn.capabilities.get("tools", [])),
                    elapsed,
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - start
                self._connect_times[name] = elapsed
                self._failed[name] = f"Connection timed out after {timeout_seconds:.0f}s"
                logger.warning("Timeout connecting to %s after %.0fs", name, timeout_seconds)
                try:
                    await conn.disconnect()
                except Exception:
                    pass
            except Exception as exc:
                elapsed = time.monotonic() - start
                self._connect_times[name] = elapsed
                self._failed[name] = str(exc)
                logger.warning("Failed to connect to %s (%.1fs): %s", name, elapsed, exc)

            self._sync_registry()
        finally:
            # Always release the idempotency gate so subsequent legitimate
            # reconnect() calls are not permanently blocked.
            connect_done.set()
            self._connect_events.pop(name, None)

    async def _disconnect_one(self, server_name: str) -> None:
        """Disconnect a single server."""
        conn = self._connections.get(server_name)
        if conn is None:
            return
        try:
            await conn.disconnect()
        except Exception as exc:
            logger.warning("Error disconnecting %s: %s", server_name, exc)

    def _sync_registry(self) -> None:
        """Sync capabilities into the registry.

        W3-P1: merges live caps (from is_connected backends) with evicted caps
        (from _evicted_caps) so a backend's tools remain discoverable after
        eviction. Live caps override evicted caps when both exist.
        Fires the change notifier when the registered set actually changes.
        """
        server_caps: dict[str, dict[str, Any]] = {}
        # Evicted (cached, not-live) caps come first; live caps override.
        for evicted_name, caps in self._evicted_caps.items():
            server_caps[evicted_name] = caps
        for name, conn in self._connections.items():
            if conn.is_connected:
                server_caps[name] = conn.capabilities
        changed = self._registry.sync(server_caps)

        if changed and self._notifier is not None:
            # Schedule the notification — debounce inside the notifier
            # coalesces multiple syncs during startup or bulk reload.
            try:
                asyncio.create_task(self._notifier.notify_tools_changed())
            except RuntimeError:
                # No running event loop (e.g., test path that called sync
                # synchronously outside an async context). Silent skip.
                pass

    def _start_retry_loop(self) -> None:
        """Delegate to manager_retry.start_retry_loop (W8-P6)."""
        from slm_mcp_hub.federation import manager_retry  # noqa: PLC0415
        manager_retry.start_retry_loop(self)

    async def _ensure_supervisors(self) -> None:
        """Delegate to manager_retry.ensure_supervisors (W8-P6)."""
        from slm_mcp_hub.federation import manager_retry  # noqa: PLC0415
        await manager_retry.ensure_supervisors(self)

    async def _supervisor_fleet_coordinator(self) -> None:
        """Delegate to manager_retry.supervisor_fleet_coordinator (W8-P6)."""
        from slm_mcp_hub.federation import manager_retry  # noqa: PLC0415
        await manager_retry.supervisor_fleet_coordinator(self)

    async def _retry_failed_servers(self) -> None:
        """Backward-compat shim: delegate to manager_retry.retry_failed_servers (W8-P6)."""
        from slm_mcp_hub.federation import manager_retry  # noqa: PLC0415
        await manager_retry.retry_failed_servers(self)
