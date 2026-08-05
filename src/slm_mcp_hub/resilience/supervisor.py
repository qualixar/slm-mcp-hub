"""W1-P2 — ConnectionSupervisor: per-backend supervision with backoff + circuit breaker.

One ``ConnectionSupervisor`` instance per MCP backend.  Each supervisor owns
exactly one :class:`~slm_mcp_hub.federation.connection.MCPConnection` and
runs its lifecycle in an isolated asyncio task.  A slow or broken backend
cannot stall any other backend (no head-of-line blocking).

Design
------
The supervisor orchestrates retries *around* the existing
:meth:`MCPConnection.connect` without rewriting its internal state machine.
A successful ``connect()`` leaves the connection in ``CONNECTED`` state;
the supervisor resets its failure counters and waits for a drop or stop
signal.

Backoff
~~~~~~~
Full-jitter exponential backoff (prevents thundering herd)::

    sleep = rng.uniform(0, min(backoff_max, backoff_base * backoff_factor ** attempt))

``rng`` and ``sleep_fn`` are injected for deterministic testing without real
wall-clock waits.

Circuit breaker
~~~~~~~~~~~~~~~
After ``failure_threshold`` consecutive failed connect cycles the breaker
opens (``CIRCUIT_OPEN`` state).  The supervisor probes at ``backoff_max``
interval (half-open semantics).  One successful connect closes the breaker.
After ``escalation_after`` distinct open cycles, ``needs_attention`` is set
(picked up by health / alerting — W1-P4) but probing continues silently.

Failure taxonomy (minimal — full classifier is W1-P3)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- ``AUTH_REQUIRED`` (detected via ``conn.state`` after ``connect()`` returns
  without raising): stop retrying; wait for external re-trigger (auth login).
  No backoff storm.
- ``TERMINAL`` (``failure_class="TERMINAL"`` on the lifecycle event emitted
  inside ``connect()``): call ``mark_failed()`` and stop.  Do not burn
  retries on non-retryable errors.
- Everything else → transient; backoff + retry.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.resilience.classifier import FailureClass, classify_failure
from slm_mcp_hub.resilience.lifecycle import LifecycleEvent

logger = logging.getLogger(__name__)


class ConnectionSupervisor:
    """Owns the lifecycle of one :class:`MCPConnection`.

    Parameters
    ----------
    conn:
        The connection object this supervisor manages.  The supervisor
        installs itself as ``conn.on_event`` to capture lifecycle events.
    failure_threshold:
        Consecutive connect failures before the circuit breaker opens.
    escalation_after:
        Number of distinct breaker-open cycles before ``needs_attention``
        is asserted (alerting hook for W1-P4).
    backoff_base:
        Base interval (seconds) for the exponential backoff formula.
    backoff_factor:
        Multiplicative factor per retry attempt.
    backoff_max:
        Maximum sleep (seconds) — also used as the probe interval when the
        breaker is open (half-open).
    rng:
        :class:`random.Random` instance used for jitter.  Inject a seeded
        instance in tests for deterministic schedules.
    sleep_fn:
        Async callable used for all waits.  Inject a no-op coroutine in
        tests to avoid real wall-clock delays.
    """

    def __init__(
        self,
        conn: MCPConnection,
        *,
        failure_threshold: int = 5,
        escalation_after: int = 3,
        backoff_base: float = 1.0,
        backoff_factor: float = 2.0,
        backoff_max: float = 60.0,
        rng: random.Random | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._conn = conn
        self._failure_threshold = failure_threshold
        self._escalation_after = escalation_after
        self._backoff_base = backoff_base
        self._backoff_factor = backoff_factor
        self._backoff_max = backoff_max
        self._rng: random.Random = rng if rng is not None else random.Random()
        self._sleep_fn: Callable[[float], Awaitable[None]] = sleep_fn or asyncio.sleep

        # --- Health surface (read externally; written only from supervised task) ---
        self.consecutive_failures: int = 0
        self.needs_attention: bool = False
        self.restart_count: int = 0
        self.last_error: str | None = None
        self.last_transition_ts: float = 0.0
        self.breaker_open: bool = False
        self.breaker_open_cycles: int = 0

        # --- Internal ---
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._drop_event: asyncio.Event | None = None
        # Last failure_class captured from conn's lifecycle events
        self._last_failure_class: str | None = None

        # W1-P4: subscribe instead of overwriting on_event.
        # Using subscribe() coexists with any other subscribers (event bus,
        # debug hooks, test listeners set via on_event = ...) rather than
        # silently replacing them (the single-slot W1-P1/P2 limitation).
        # _unsub is called in stop() to deregister cleanly.
        self._unsub: Callable[[], None] = self._conn.subscribe(self._capture_event)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        """Proxy for the underlying connection's current state."""
        return self._conn.state

    async def start(self) -> None:
        """Start the supervised loop in its own isolated asyncio task.

        Idempotent: calling while a task is already running is a no-op.
        """
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._supervised_loop(),
            name=f"supervisor:{self._conn.name}",
        )

    async def stop(self) -> None:
        """Signal stop and wait for the supervised task to finish.

        Safe to call before :meth:`start` or after the task has already ended.
        W1-P4: also unregisters the lifecycle-event subscriber so the connection
        does not hold a dangling reference to this (stopped) supervisor.
        """
        if self._stop_event is not None:
            self._stop_event.set()
        if self._drop_event is not None:
            self._drop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # W1-P4: deregister from the connection's subscriber set.
        # Idempotent — calling _unsub() multiple times is safe.
        self._unsub()

    def health_snapshot(self) -> dict[str, Any]:
        """Return a point-in-time snapshot of supervisor health fields.

        Used by :meth:`ConnectionManager.get_server_status` to include
        per-backend supervisor health in the status output.
        """
        return {
            "state": self._conn.state.value,
            "consecutive_failures": self.consecutive_failures,
            "needs_attention": self.needs_attention,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "last_transition_ts": self.last_transition_ts,
            "breaker_open": self.breaker_open,
            "breaker_open_cycles": self.breaker_open_cycles,
        }

    # ------------------------------------------------------------------
    # Backoff formula
    # ------------------------------------------------------------------

    def _compute_backoff(self, attempt: int) -> float:
        """Full-jitter backoff: ``uniform(0, min(max, base * factor ** attempt))``.

        Parameters
        ----------
        attempt:
            0-indexed retry number.  Attempt 0 gives ``uniform(0, base)``.
        """
        cap = min(
            self._backoff_max,
            self._backoff_base * (self._backoff_factor ** attempt),
        )
        return self._rng.uniform(0, cap)

    # ------------------------------------------------------------------
    # Lifecycle event capture
    # ------------------------------------------------------------------

    def _capture_event(self, event: LifecycleEvent) -> None:
        """Handle a lifecycle event emitted by the connection's ``_transition``.

        Tracks the last ``failure_class`` so the supervisor can determine
        whether a ``ConnectionError`` is transient or terminal.
        Also detects unexpected drops from ``CONNECTED`` to signal the
        ``_drop_event`` (re-enters the connect loop for reconnection).
        """
        self.last_transition_ts = event.ts
        if event.failure_class is not None:
            self._last_failure_class = event.failure_class

        # Any transition OUT of CONNECTED (including to DISCONNECTED when the
        # manager's disconnect_all tears down the connection) fires the drop
        # event.  The supervisor distinguishes graceful stop (stop_event) from
        # unexpected drop by checking stop_event after waking.
        if event.from_state == ConnectionState.CONNECTED and self._drop_event is not None:
            self._drop_event.set()

    # ------------------------------------------------------------------
    # Supervised loop
    # ------------------------------------------------------------------

    async def _supervised_loop(self) -> None:
        """Main supervision loop — runs exclusively in its own asyncio task.

        Each iteration:
        1. If the breaker is open, transition through RECONNECTING before
           calling ``connect()`` (uses the designed CIRCUIT_OPEN→RECONNECTING
           edge in the lifecycle table).
        2. Call ``conn.connect()``.
        3. On success (CONNECTED): reset counters; wait for drop or stop.
        4. On AUTH_REQUIRED: stop (wait for external re-trigger).
        5. On ConnectionError: pass to ``_handle_connect_failure`` which calls
           :func:`~slm_mcp_hub.resilience.classifier.classify_failure` on the
           exception (including its cause chain) to decide TRANSIENT backoff or
           TERMINAL mark-and-stop.  This is the W1-P3 classifier wiring.
        6. On any other exception: classify via ``classify_failure`` and apply
           the appropriate transition (AUTH → wait, TERMINAL → mark_failed,
           TRANSIENT → mark_failed for safety since non-ConnectionErrors are
           unexpected here).
        """
        assert self._stop_event is not None
        stop = self._stop_event

        while not stop.is_set():
            # Reset per-attempt state
            self._drop_event = asyncio.Event()
            self._last_failure_class = None

            # Before a probe when breaker is open, traverse the designed
            # CIRCUIT_OPEN → RECONNECTING edge so the lifecycle table is happy.
            if self.breaker_open and self._conn.state == ConnectionState.CIRCUIT_OPEN:
                self._conn.enter_reconnecting(self.consecutive_failures)

            # --- Attempt connect ---
            try:
                await self._conn.connect()
            except asyncio.CancelledError:
                # Task is being stopped — propagate and exit cleanly.
                raise
            except ConnectionError as exc:
                # W1-P3: pass the exception to _handle_connect_failure so
                # classify_failure() can inspect the full cause chain.
                await self._handle_connect_failure(exc, stop)
                continue
            except Exception as exc:
                # Non-ConnectionError: use classify_failure to decide the transition
                # rather than blindly marking TERMINAL for every unexpected exception.
                fc = classify_failure(exc)
                if fc == FailureClass.AUTH:  # pragma: no cover
                    # Very unusual path (OAuthAuthRequiredError normally caught
                    # inside connect() before propagating), but handle cleanly.
                    logger.info(
                        "Supervisor %s: AUTH (unexpected exception path) — "
                        "stopping until externally re-triggered",
                        self._conn.name,
                    )
                    await stop.wait()
                    return
                logger.exception(
                    "Supervisor %s: non-ConnectionError during connect "
                    "(classified as %s): %s",
                    self._conn.name,
                    fc.value,
                    exc,
                )
                self._conn.mark_failed(
                    reason=f"{fc.value} non-ConnectionError: {type(exc).__name__}"
                )
                return

            # --- connect() returned without raising ---
            if self._conn.state == ConnectionState.AUTH_REQUIRED:
                logger.info(
                    "Supervisor %s: AUTH_REQUIRED — stopping until externally re-triggered",
                    self._conn.name,
                )
                # Block here; an external trigger (auth login + supervisor.start())
                # will restart supervision.  This prevents a tight retry storm.
                await stop.wait()
                return  # pragma: no cover

            if self._conn.state != ConnectionState.CONNECTED:
                # Unexpected state — shouldn't happen with a well-formed connect().
                # Log and loop (will retry after the next backoff).
                logger.warning(
                    "Supervisor %s: connect() returned but state is %s (expected CONNECTED); "
                    "treating as transient",
                    self._conn.name,
                    self._conn.state.value,
                )
                await self._sleep_fn(self._compute_backoff(self.consecutive_failures))
                continue

            # --- Connect succeeded ---
            self._on_connect_success()

            # Wait until the connection drops or stop is signaled.
            # When the connection transitions out of CONNECTED, _capture_event
            # fires _drop_event, waking this wait.
            await self._wait_for_drop_or_stop(stop)

            if stop.is_set():
                return  # pragma: no cover

            # Drop detected — log and loop to reconnect.
            logger.info(
                "Supervisor %s: connection dropped, re-entering connect loop "
                "(consecutive_failures=%d)",
                self._conn.name,
                self.consecutive_failures,
            )
            # consecutive_failures stays at 0 here since we reset on success.
            # The next connect attempt will start counting fresh.

    def _on_connect_success(self) -> None:
        """Reset all failure-tracking state on a successful connect."""
        self.consecutive_failures = 0
        self.breaker_open = False
        self.needs_attention = False
        self.last_error = None
        self.restart_count += 1
        logger.info(
            "Supervisor %s: connected (restart_count=%d)",
            self._conn.name,
            self.restart_count,
        )

    async def _handle_connect_failure(
        self, exc: BaseException, stop: asyncio.Event
    ) -> None:
        """React to a failed connect() attempt.

        W1-P3: calls :func:`~slm_mcp_hub.resilience.classifier.classify_failure`
        on the caught exception (inspecting its full cause chain) to decide the
        retry policy, replacing the W1-P2 ``_last_failure_class or "TRANSIENT"``
        inline heuristic.

        Routes:
        - AUTH    → stop and wait for external re-trigger (no backoff storm).
        - TERMINAL → ``mark_failed()`` + stop (no retry; admin action required).
        - TRANSIENT → backoff sleep + enter RECONNECTING / CIRCUIT_OPEN.

        Parameters
        ----------
        exc:
            The :exc:`ConnectionError` caught by the supervised loop.
        stop:
            The supervisor's stop event (set on AUTH/TERMINAL to exit the loop).
        """
        fc = classify_failure(exc)
        self.last_error = (
            f"failure_class={fc.value}, exc={type(exc).__name__}, "
            f"state={self._conn.state.value}"
        )

        if fc == FailureClass.AUTH:  # pragma: no cover
            # AUTH via exception path (should be rare — OAuthAuthRequiredError is
            # normally caught inside MCPConnection.connect() and reflected as
            # AUTH_REQUIRED state without raising).  Handle gracefully: stop
            # retrying and wait for external re-trigger (e.g. auth login).
            logger.info(
                "Supervisor %s: AUTH_REQUIRED (exception path) — "
                "stopping until externally re-triggered",
                self._conn.name,
            )
            await stop.wait()
            return

        if fc == FailureClass.TERMINAL:
            logger.error(
                "Supervisor %s: terminal failure (%s) — stopping supervision",
                self._conn.name,
                type(exc).__name__,
            )
            self._conn.mark_failed(
                reason=f"Terminal failure ({type(exc).__name__}): {exc}"
            )
            # Signal stop so the outer while loop exits on the next iteration
            # check rather than continuing to retry a non-retryable failure.
            stop.set()
            return

        # --- Transient failure ---
        self.consecutive_failures += 1

        if self.consecutive_failures >= self._failure_threshold and not self.breaker_open:
            # Trip the circuit breaker for the first time in this cycle.
            self._conn.enter_circuit_open()
            self.breaker_open = True
            self.breaker_open_cycles += 1
            if self.breaker_open_cycles >= self._escalation_after:
                self.needs_attention = True
            logger.warning(
                "Supervisor %s: circuit breaker OPEN "
                "(failures=%d, open_cycles=%d, needs_attention=%s)",
                self._conn.name,
                self.consecutive_failures,
                self.breaker_open_cycles,
                self.needs_attention,
            )
            backoff = self._backoff_max

        elif self.breaker_open:
            # Probe failed — breaker stays open.  Each probe failure counts as
            # one additional open cycle; escalate to needs_attention once the
            # cumulative cycle count crosses the threshold.
            self._conn.enter_circuit_open()
            self.breaker_open_cycles += 1
            if not self.needs_attention and self.breaker_open_cycles >= self._escalation_after:
                self.needs_attention = True
                logger.warning(
                    "Supervisor %s: needs_attention asserted "
                    "(open_cycles=%d >= escalation_after=%d)",
                    self._conn.name,
                    self.breaker_open_cycles,
                    self._escalation_after,
                )
            logger.debug(
                "Supervisor %s: probe failed; breaker remains open "
                "(failures=%d, open_cycles=%d)",
                self._conn.name,
                self.consecutive_failures,
                self.breaker_open_cycles,
            )
            backoff = self._backoff_max

        else:
            # Normal transient retry: enter RECONNECTING for health visibility.
            self._conn.enter_reconnecting(self.consecutive_failures)
            backoff = self._compute_backoff(self.consecutive_failures - 1)
            logger.debug(
                "Supervisor %s: transient failure #%d, backoff=%.2fs",
                self._conn.name,
                self.consecutive_failures,
                backoff,
            )

        await self._interruptible_sleep(backoff, stop)

    async def _interruptible_sleep(self, duration: float, stop: asyncio.Event) -> None:
        """Sleep for *duration* using the injected sleep function.

        In production (default ``asyncio.sleep``), task cancellation via
        :meth:`stop` delivers an immediate ``CancelledError`` that propagates
        up and exits the supervised loop cleanly — no extra event needed.

        In tests, inject a fast/no-op sleep so the backoff schedule completes
        without real wall-clock waits.  The ``stop`` parameter is accepted for
        interface uniformity but is not polled directly; it is checked at each
        loop iteration by the caller.
        """
        await self._sleep_fn(duration)

    async def _wait_for_drop_or_stop(self, stop: asyncio.Event) -> None:
        """Wait until either the stop event fires or the connection drops.

        Level-triggered guard: if the connection is already not CONNECTED when
        this method is entered (e.g. a drop fired while ``connect()`` was in
        flight, or between the loop arming ``_drop_event`` and this await), the
        method returns immediately rather than blocking on an event that may
        already have been missed.  This closes the race where a CONNECTED→X
        transition fires ``_capture_event`` before ``_drop_event`` was created
        (when it was still ``None``) or before ``asyncio.wait`` was entered.

        Both events (stop and drop) wake this coroutine.  Properly cancels the
        task for whichever condition fires second to avoid task leaks.
        """
        assert self._drop_event is not None

        # Level-triggered: treat an already-disconnected connection as an
        # immediate drop signal — do not wait for the edge event.
        if not self._conn.is_connected:
            return

        stop_task = asyncio.create_task(stop.wait(), name="sup-stop-wait")
        drop_task = asyncio.create_task(
            self._drop_event.wait(), name="sup-drop-wait"
        )

        try:
            _done, pending = await asyncio.wait(
                {stop_task, drop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            stop_task.cancel()
            drop_task.cancel()
            raise
        finally:
            for t in (stop_task, drop_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
