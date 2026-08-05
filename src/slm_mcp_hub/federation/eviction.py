"""W3-P2 — IdleReaper: background idle-eviction loop for lazy MCP backends.

Eviction eligibility (non-negotiable):
- ``spawn == "lazy"``: eligible — evicted when idle time > idle_ttl_seconds.
- ``spawn == "eager"``: NEVER evicted (stays connected regardless of age).
- ``spawn == "pinned"`` OR ``always_on=True`` (is_pinned=True): NEVER evicted.
- ``idle_ttl_seconds == 0``: reaper is DISABLED — start() is a no-op.

Design principles (CRIT pre-fixes):
1. CRIT-1 — Just-connected backend safety: ``seed_activity`` initialises the
   timestamp when a backend connects; ``_check_and_evict`` conservatively skips
   backends with no timestamp (``last is None``), preventing instant eviction.
2. CRIT-2 — Serial eviction blocked: multiple idle backends are evicted via
   ``asyncio.gather(..., return_exceptions=True)`` so one slow drain cannot
   block the others.
3. CRIT-3 — Task leak on stop: ``stop()`` sets ``self._task = None`` before
   awaiting the cancel, so a concurrent ``stop()`` sees no task to cancel.
4. CRIT-4 — Activity marked on failure: activity is marked at call COMPLETION
   (success or error) ONLY when the connection was live and the call was
   dispatched (inside the router's ``finally``); the "not found" /
   "not connected" fast-return paths do NOT call activity_fn.
5. In-flight eviction race: a backend with an
   in-flight routed call is NEVER evicted, regardless of TTL. ``has_inflight_fn``
   (wired to ``MCPConnection.in_flight_count``) gates every sweep so the reaper
   can never disconnect a backend from under a running call — the drain grace
   in ``evict()`` is far too short (5s) for a long-running backend.
6. Stale-timestamp re-eviction: ``manager.evict()`` calls
   ``forget()`` so a later reconnect re-seeds a FRESH timestamp; without this a
   non-route reconnect (``manager.reconnect``/admin warm) would inherit a stale
   pre-eviction timestamp and be reaped on the very next sweep.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Awaitable

if TYPE_CHECKING:
    from slm_mcp_hub.core.config import HubConfig, MCPServerConfig

logger = logging.getLogger(__name__)


class IdleReaper:
    """Background task that evicts idle lazy MCP backends to free RAM.

    Attributes
    ----------
    _last_activity:
        Maps backend name → monotonic timestamp of last routed call.
        Seeded to current time when a backend connects so a just-connected
        backend is never instantly evicted.

    Clock and sleep are injected via ``time_fn`` / ``sleep_fn`` so tests
    can drive the TTL without real wall-clock waiting.
    """

    def __init__(
        self,
        config: HubConfig,
        evict_fn: Callable[[str], Awaitable[None]],
        get_backends_fn: Callable[[], Iterable[MCPServerConfig]],
        is_live_fn: Callable[[str], bool],
        interval: float = 30.0,
        time_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        has_inflight_fn: Callable[[str], bool] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        config:
            Hub configuration — reads ``idle_ttl_seconds``.
        evict_fn:
            Coroutine called to evict a backend by name.  Must be
            ``ConnectionManager.evict`` (or a compatible stub in tests).
        get_backends_fn:
            Returns the current list of ``MCPServerConfig`` objects.
        is_live_fn:
            Returns True if the named backend is currently connected.
        interval:
            Seconds between each reaper sweep.  Defaults to 30 s.
        time_fn:
            Monotonic clock.  Defaults to ``time.monotonic``.
            Inject a :class:`FakeClock` in tests to drive TTL without sleep.
        sleep_fn:
            Async sleep.  Defaults to ``asyncio.sleep``.
            Inject a no-op stub in tests so the loop completes instantly.
        has_inflight_fn:
            Returns True if the named backend currently has an in-flight routed
            call.  Wired to ``MCPConnection.in_flight_count > 0``.  A backend
            with in-flight work is NEVER evicted (CRIT-5 race guard).  Defaults
            to None (treated as "no in-flight" — backward compatible).
        """
        self._config = config
        self._evict_fn = evict_fn
        self._get_backends_fn = get_backends_fn
        self._is_live_fn = is_live_fn
        self._has_inflight_fn = has_inflight_fn
        self._interval = interval
        self._time_fn: Callable[[], float] = time_fn if time_fn is not None else time.monotonic
        self._sleep_fn: Callable[[float], Awaitable[None]] = (
            sleep_fn if sleep_fn is not None else asyncio.sleep
        )
        self._last_activity: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Activity tracking API (called by the manager / router)
    # ------------------------------------------------------------------

    def mark_activity(self, name: str) -> None:
        """Record the current clock time as last activity for ``name``.

        Called by :class:`~slm_mcp_hub.federation.router.FederationRouter`
        on every successful routed call to keep a backend alive.
        """
        self._last_activity[name] = self._time_fn()

    def seed_activity(self, name: str) -> None:
        """Initialise activity for ``name`` without overwriting an existing entry.

        Called by ``ConnectionManager._connect_timed`` after a successful
        connect so that a just-connected backend never appears instantly idle.
        Does NOT overwrite an existing timestamp — existing activity wins.
        """
        if name not in self._last_activity:
            self._last_activity[name] = self._time_fn()

    def forget(self, name: str) -> None:
        """Remove activity tracking for ``name`` (backend removed/disconnected).

        A no-op if ``name`` is unknown.
        """
        self._last_activity.pop(name, None)

    # ------------------------------------------------------------------
    # Background task lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background reaper loop.  Idempotent.

        No-op when ``idle_ttl_seconds == 0`` (reaper disabled) or when the
        loop is already running.
        """
        if self._config.idle_ttl_seconds == 0:
            logger.debug("IdleReaper disabled (idle_ttl_seconds=0) — not starting")
            return
        if self._task is not None and not self._task.done():
            logger.debug("IdleReaper already running — ignoring duplicate start()")
            return
        self._task = asyncio.create_task(self._loop(), name="idle-reaper")
        logger.debug(
            "IdleReaper started (ttl=%ds, interval=%.1fs)",
            self._config.idle_ttl_seconds,
            self._interval,
        )

    async def stop(self) -> None:
        """Cancel and await the reaper task.  Leaves no pending asyncio tasks.

        Idempotent: safe to call when the reaper was never started or has
        already stopped.

        CRIT-3 design: ``self._task`` is set to ``None`` BEFORE the cancel
        so a concurrent ``stop()`` call sees no task to double-cancel.
        """
        task = self._task
        self._task = None  # clear first — concurrent stop() sees None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.debug("IdleReaper stopped")

    @property
    def is_running(self) -> bool:
        """True if the background task exists and is not yet done."""
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # Internal reaper loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main reaper loop — sleep, then sweep for idle backends.

        A sweep-time error (e.g. a transient fault in a callback) is logged and
        swallowed so one bad sweep can never kill the reaper for the life of the
        hub.  ``CancelledError`` is re-raised so ``stop()`` can cancel cleanly.
        """
        while True:
            await self._sleep_fn(self._interval)
            try:
                await self._check_and_evict()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — deliberate: keep the reaper alive
                logger.exception("IdleReaper sweep failed; continuing")

    async def _check_and_evict(self) -> None:
        """Identify and evict all idle lazy backends in a single sweep.

        Eligibility (applied in order):
        1. ``backend.spawn != "lazy"`` → skip (eager / pinned never evicted).
        2. ``backend.is_pinned`` → skip (safety net for always_on=True edge case).
        3. ``not is_live_fn(name)`` → skip (already evicted or not connected).
        4. ``has_inflight_fn(name)`` → skip (in-flight call — CRIT-5 race guard).
        5. ``last is None`` → skip (never seeded; conservatively skip).
        6. ``now - last <= idle_ttl_seconds`` → skip (still within TTL).
        7. Otherwise → evict.

        Evictions run concurrently via ``asyncio.gather`` with
        ``return_exceptions=True`` so one slow drain cannot block others.
        (CRIT-2 fix.)
        """
        if self._config.idle_ttl_seconds == 0:
            return

        now = self._time_fn()
        ttl = float(self._config.idle_ttl_seconds)
        to_evict: list[str] = []

        for backend in self._get_backends_fn():
            # Only lazy backends are eligible.
            if backend.spawn != "lazy":
                continue
            # Belt-and-suspenders: skip always_on=True even when spawn=="lazy".
            if backend.is_pinned:
                continue
            # Skip non-live backends (already evicted, not yet connected, etc.).
            if not self._is_live_fn(backend.name):
                continue
            # Never evict a backend with an in-flight routed call (CRIT-5).
            # The 5s drain grace in evict() would force-cancel a long call.
            if self._has_inflight_fn is not None and self._has_inflight_fn(backend.name):
                continue
            # Skip unseeded backends — conservative: avoid instant eviction.
            last = self._last_activity.get(backend.name)
            if last is None:
                continue
            # Evict strictly when idle time EXCEEDS TTL (not >=).
            if now - last > ttl:
                to_evict.append(backend.name)

        if not to_evict:
            return

        logger.info(
            "IdleReaper: evicting %d idle lazy backend(s): %s",
            len(to_evict),
            to_evict,
        )

        # Concurrent eviction — one slow drain must not block others (CRIT-2).
        results: list[Any] = await asyncio.gather(
            *(self._evict_fn(name) for name in to_evict),
            return_exceptions=True,
        )
        for name, result in zip(to_evict, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "IdleReaper: eviction of %r raised an exception: %s",
                    name,
                    result,
                )
