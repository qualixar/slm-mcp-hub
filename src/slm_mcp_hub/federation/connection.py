"""MCP connection manager — manages one MCP server connection.

The upstream transport is handled exclusively by OutboundClient, which wraps
the official ``mcp`` SDK ``Client(mode="auto")`` for both stdio and Streamable
HTTP upstreams.  All hand-rolled JSON-RPC machinery (subprocess management,
pending-future maps, session-header logic, SSE/JSON parsing) has been removed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from slm_mcp_hub.auth.broker import OAuthAuthRequiredError
from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.protocol.models import NegotiatedPeer
from slm_mcp_hub.protocol.outbound import OutboundClient
from slm_mcp_hub.resilience.lifecycle import LifecycleEvent, is_valid_transition

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    # ------------------------------------------------------------------
    # Legacy states — kept exactly as-is; ALL existing semantics preserved.
    # ------------------------------------------------------------------
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DRAINING = "draining"
    ERROR = "error"
    AUTH_REQUIRED = "auth_required"

    # ------------------------------------------------------------------
    # W1-P1 additive lifecycle states (LLD §2)
    # Used by the W1-P2 supervisor; not yet traversed by legacy code paths.
    # ------------------------------------------------------------------
    STARTING = "starting"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    CIRCUIT_OPEN = "circuit_open"
    STOPPED = "stopped"
    FAILED = "failed"


class MCPConnection:
    """Manages a single MCP server connection (stdio or Streamable HTTP).

    Delegates all upstream transport to :class:`OutboundClient`, which uses the
    official MCP SDK ``Client(mode="auto")``.  The public interface (connect,
    disconnect, drain_and_disconnect, call_tool, read_resource, get_prompt) is
    unchanged from prior versions.

    ``_in_flight`` tracks requests currently executing via the SDK path so that
    ``drain_and_disconnect`` can wait for them to complete before tearing down.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._state = ConnectionState.DISCONNECTED
        self._capabilities: dict[str, Any] = {
            "tools": [],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }
        self._connected_at: float = 0.0
        # In-flight request counter for drain semantics.
        self._in_flight: int = 0
        # Set when drain_and_disconnect is waiting for _in_flight to reach 0.
        self._drain_event: asyncio.Event | None = None
        # Serializes concurrent drain_and_disconnect calls per connection.
        self._drain_lock: asyncio.Lock | None = None
        # SDK-backed outbound client (set by connect(); None until then).
        self._outbound: OutboundClient | None = None

        # W1-P4: multi-subscriber fan-out event bus.
        # Replaces the single-slot on_event callback (W1-P1).
        # Use subscribe() for new code; on_event property retained for back-compat.
        self._subscribers: dict[int, Callable[[LifecycleEvent], None]] = {}
        self._next_sub_id: int = 0
        # Tracks the subscriber ID registered via the on_event property setter
        # so reassignment/removal correctly replaces only the primary slot.
        self._primary_sub_id: int | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._capabilities

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def is_draining(self) -> bool:
        return self._state == ConnectionState.DRAINING

    @property
    def in_flight_count(self) -> int:
        return self._in_flight

    @property
    def is_auth_required(self) -> bool:
        return self._state == ConnectionState.AUTH_REQUIRED

    @property
    def negotiated_peer(self) -> NegotiatedPeer | None:
        """Protocol peer negotiated by the SDK during connect().

        Returns None for connections not yet established.
        """
        if self._outbound is not None:
            return self._outbound.negotiated_peer
        return None

    @property
    def uptime_seconds(self) -> float:
        if self._connected_at == 0:
            return 0.0
        return time.time() - self._connected_at

    @property
    def process_pid(self) -> int | None:
        """Return subprocess PID for stdio backends via OutboundClient.

        Delegates to OutboundClient.process_pid. Returns None when:
        - not connected (_outbound is None)
        - HTTP/SSE transport (no subprocess)
        - psutil unavailable or SDK internals changed

        W5-P1 addition for RAM measurement via psutil.
        """
        if self._outbound is None:
            return None
        return self._outbound.process_pid

    # ------------------------------------------------------------------
    # W1-P4 — multi-subscriber fan-out (back-compat on_event property)
    # ------------------------------------------------------------------

    @property
    def on_event(self) -> Callable[[LifecycleEvent], None] | None:
        """Back-compat: return the primary subscriber or ``None``.

        Existing code that reads ``conn.on_event`` to check whether a callback
        is installed continues to work.  Returns the callable that was last
        assigned via the setter, or ``None`` if no primary subscriber is set.

        New code should use :meth:`subscribe` directly.
        """
        if self._primary_sub_id is not None:
            return self._subscribers.get(self._primary_sub_id)
        return None

    @on_event.setter
    def on_event(
        self, cb: Callable[[LifecycleEvent], None] | None
    ) -> None:
        """Back-compat: register or replace the primary subscriber.

        Assigning a callable registers it as the "primary" subscriber slot.
        A subsequent assignment replaces the previous primary without affecting
        any other subscribers registered via :meth:`subscribe`.
        Assigning ``None`` removes the primary subscriber.

        W1-P4 migration note: the supervisor now uses :meth:`subscribe` so its
        drop-watch coexists with the event bus (the previous ``on_event``
        assignment silently replaced all prior listeners — the single-slot
        limitation tracked in the W1-P2 review).
        """
        # Remove the existing primary subscriber (if any)
        if self._primary_sub_id is not None:
            self._subscribers.pop(self._primary_sub_id, None)
            self._primary_sub_id = None

        if cb is not None:
            self._primary_sub_id = self._next_sub_id
            self._next_sub_id += 1
            self._subscribers[self._primary_sub_id] = cb

    def subscribe(
        self, cb: Callable[[LifecycleEvent], None]
    ) -> Callable[[], None]:
        """Register *cb* as a subscriber for lifecycle events on this connection.

        All registered subscribers receive every event emitted by
        :meth:`_transition`.  A raising subscriber is caught and logged;
        it cannot break the lifecycle path or other subscribers.

        Parameters
        ----------
        cb:
            Synchronous callable accepting a single :class:`LifecycleEvent`.

        Returns
        -------
        Callable[[], None]
            An unsubscribe callable.  Call it once to remove *cb* from the
            subscriber set.  Idempotent — subsequent calls are no-ops.
        """
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        self._subscribers[sub_id] = cb

        def _unsub() -> None:
            self._subscribers.pop(sub_id, None)

        return _unsub

    def _emit(self, event: LifecycleEvent) -> None:
        """Fan out *event* to all registered subscribers with per-subscriber isolation.

        Iterates over a snapshot copy of the subscriber dict so that a subscriber
        which unregisters itself (or another subscriber) during delivery does not
        cause skipped or double deliveries.

        A raising subscriber is logged using the same message text as the W1-P1
        single-slot implementation so that existing test assertions on log messages
        continue to pass.

        Parameters
        ----------
        event:
            Immutable :class:`LifecycleEvent` emitted by :meth:`_transition`.
        """
        for cb in list(self._subscribers.values()):
            try:
                cb(event)
            except Exception:
                logger.exception(
                    "on_event callback raised for %s (%s -> %s); ignoring",
                    self.name,
                    event.from_state.value,
                    event.to_state.value,
                )

    # ------------------------------------------------------------------
    # W1-P1 — single state-mutation point
    # ------------------------------------------------------------------

    def _transition(
        self,
        to_state: ConnectionState,
        reason: str,
        *,
        failure_class: str | None = None,
        attempt: int | None = None,
    ) -> LifecycleEvent:
        """Transition to *to_state* and emit a :class:`LifecycleEvent`.

        This is the **only** place that mutates ``self._state``.  All
        production lifecycle paths (connect, disconnect, drain) call this
        method rather than writing to ``self._state`` directly.

        Unexpected transitions (edges not in :data:`LIFECYCLE_TRANSITIONS`)
        are **logged as warnings but allowed** — fail-open ensures that
        existing flows and test setup patterns are never broken.

        Parameters
        ----------
        to_state:
            The target :class:`ConnectionState`.
        reason:
            Short human-readable description of why the transition occurred.
        failure_class:
            Optional failure classifier string (e.g. ``"TRANSIENT"``).
            Populated by W1-P3 classifier; ``None`` in legacy paths.
        attempt:
            Optional retry-attempt counter. Populated by W1-P2 supervisor;
            ``None`` in legacy paths.

        Returns
        -------
        LifecycleEvent
            The immutable event record that was emitted.
        """
        from_state = self._state

        # Self-loops (same → same) occur in concurrent drain scenarios and
        # degenerate disconnect-on-disconnected calls.  They are not design
        # violations — just no-ops at the state-machine level.  Skip the
        # warning so we don't flood logs with noise from expected races.
        if from_state != to_state and not is_valid_transition(from_state, to_state):
            logger.warning(
                "Unexpected transition on %s: %s -> %s (%s)",
                self.name,
                from_state.value,
                to_state.value,
                reason,
            )

        self._state = to_state

        event = LifecycleEvent(
            server=self.name,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            ts=time.time(),
            failure_class=failure_class,
            attempt=attempt,
        )

        # W1-P4: fan out to all subscribers via _emit (replaces single-slot
        # on_event call; back-compat maintained by the on_event property setter
        # which registers via the same subscriber dict).
        # Observers must NEVER break the lifecycle path — _emit's per-subscriber
        # try/except ensures a raising callback cannot corrupt the state we just set.
        self._emit(event)

        return event

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the MCP server via SDK OutboundClient.

        Uses the official ``mcp`` SDK ``Client(mode="auto")`` for both stdio
        and Streamable HTTP transports.

        Raises:
            ConnectionError: If the server cannot be reached or the MCP
                initialization handshake fails.
        """
        if self._state == ConnectionState.CONNECTED:
            return

        self._transition(ConnectionState.CONNECTING, reason="connect() called")

        outbound = OutboundClient(self._config)
        try:
            await outbound.connect()
        except OAuthAuthRequiredError:
            # auth_required is a clean, expected state — NOT a crash.
            # The connection remains unusable until the user runs auth login.
            self._transition(
                ConnectionState.AUTH_REQUIRED,
                reason="OAuthAuthRequiredError — awaiting user login",
                failure_class="AUTH",
            )
            return
        except ConnectionError:
            self._transition(
                ConnectionState.ERROR,
                reason="ConnectionError during connect",
                failure_class="TRANSIENT",
            )
            raise
        except Exception as exc:
            self._transition(
                ConnectionState.ERROR,
                reason=f"Unexpected error during connect: {type(exc).__name__}",
                failure_class="TERMINAL",
            )
            raise ConnectionError(
                f"MCP {self.name} initialization failed ({type(exc).__name__})"
            ) from exc

        self._outbound = outbound
        self._capabilities = outbound.capabilities
        self._transition(
            ConnectionState.CONNECTED,
            reason="MCP init handshake complete",
        )
        self._connected_at = time.time()
        logger.info(
            "Connected to MCP: %s (%d tools, %d resources, %d prompts)",
            self.name,
            len(self._capabilities["tools"]),
            len(self._capabilities["resources"]),
            len(self._capabilities["prompts"]),
        )

    # ------------------------------------------------------------------
    # W1-P2 supervisor hooks (thin public API over _transition)
    # Each method delegates entirely to _transition — no logic here.
    # The supervisor calls these instead of touching _transition directly.
    # ------------------------------------------------------------------

    def enter_reconnecting(self, attempt: int) -> "LifecycleEvent":
        """Supervisor hook: enter RECONNECTING state for a retry backoff.

        Called by :class:`~slm_mcp_hub.resilience.supervisor.ConnectionSupervisor`
        before sleeping between connection attempts.  Exposes the attempt
        counter so lifecycle events carry it for observability (W1-P4).

        Parameters
        ----------
        attempt:
            0-indexed retry attempt number (passed through to the event).

        Returns
        -------
        LifecycleEvent
            The immutable event emitted by :meth:`_transition`.
        """
        return self._transition(
            ConnectionState.RECONNECTING,
            reason=f"Supervisor: entering reconnect backoff (attempt={attempt})",
            attempt=attempt,
        )

    def enter_circuit_open(self) -> "LifecycleEvent":
        """Supervisor hook: enter CIRCUIT_OPEN state (breaker tripped).

        Called by the supervisor after ``failure_threshold`` consecutive
        connect failures.  The connection is still live at the state-machine
        level; the supervisor will probe at ``backoff_max`` intervals.

        Returns
        -------
        LifecycleEvent
            The immutable event emitted by :meth:`_transition`.
        """
        return self._transition(
            ConnectionState.CIRCUIT_OPEN,
            reason="Supervisor: circuit breaker opened",
            failure_class="CIRCUIT_OPEN",
        )

    def mark_failed(self, reason: str) -> "LifecycleEvent":
        """Supervisor hook: enter FAILED state (terminal — no retry).

        Called by the supervisor when the failure classifier determines that
        the error is non-retryable (bad config, unknown transport, etc.).
        The supervisor stops after this call.

        Parameters
        ----------
        reason:
            Human-readable description of the terminal failure.

        Returns
        -------
        LifecycleEvent
            The immutable event emitted by :meth:`_transition`.
        """
        return self._transition(
            ConnectionState.FAILED,
            reason=reason,
            failure_class="TERMINAL",
        )

    async def disconnect(self) -> None:
        """Disconnect from the MCP server.

        Closes the SDK OutboundClient and resets connection state.
        """
        if self._outbound is not None:
            try:
                await self._outbound.disconnect()
            except Exception as exc:
                logger.debug(
                    "Error closing outbound client for %s: %s", self.name, exc
                )
            self._outbound = None

        self._transition(ConnectionState.DISCONNECTED, reason="disconnect() called")
        self._connected_at = 0.0
        logger.info("Disconnected from MCP: %s", self.name)

    async def drain_and_disconnect(self, timeout_s: float = 30.0) -> None:
        """Stop accepting new requests, wait for in-flight calls, then disconnect.

        Serialized per connection via _drain_lock so concurrent callers don't
        overwrite each other's drain event.  The second caller simply waits
        for the first to complete, then sees state=DISCONNECTED and returns.
        """
        if self._drain_lock is None:
            self._drain_lock = asyncio.Lock()

        async with self._drain_lock:
            # After acquiring the lock, re-check state in case a prior drain
            # already disconnected us.
            if self._state not in (ConnectionState.CONNECTED, ConnectionState.DRAINING):
                await self.disconnect()
                return

            self._transition(ConnectionState.DRAINING, reason="drain_and_disconnect() called")
            if self._in_flight > 0:
                self._drain_event = asyncio.Event()
                logger.info(
                    "Draining %s: %d in-flight calls, waiting up to %.0fs",
                    self.name, self._in_flight, timeout_s,
                )
                try:
                    await asyncio.wait_for(self._drain_event.wait(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Drain timeout for %s after %.0fs — forcing disconnect "
                        "with %d in-flight",
                        self.name, timeout_s, self._in_flight,
                    )

            await self.disconnect()

    # ------------------------------------------------------------------
    # RPC delegation
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_s: float | None = None,  # noqa: ARG002 — kept for API compatibility
    ) -> dict[str, Any]:
        """Call a tool on this MCP server and return the result."""
        self._check_callable()
        return await self._dispatch(self._outbound.call_tool(tool_name, arguments))  # type: ignore[union-attr]

    async def call_tool_streaming(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        read_timeout_seconds: float | None = None,
        progress_callback: Any | None = None,
        resumption_token: str | None = None,
        on_resumption_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Call a tool with progress, timeout, and resumption support.

        Delegates to OutboundClient.call_tool_streaming and wraps in _dispatch
        for in-flight tracking (drain semantics remain correct: the finally block
        in _dispatch decrements _in_flight even on anyio structural cancellation).

        Raises:
            ConnectionError: If draining or outbound is None.
            anyio.get_cancelled_exc_class(): Propagates through — NOT swallowed.
        """
        self._check_callable()
        return await self._dispatch(
            self._outbound.call_tool_streaming(  # type: ignore[union-attr]
                tool_name,
                arguments,
                read_timeout_seconds=read_timeout_seconds,
                progress_callback=progress_callback,
                resumption_token=resumption_token,
                on_resumption_token=on_resumption_token,
            )
        )

    async def read_resource(
        self,
        uri: str,
        timeout_s: float | None = None,  # noqa: ARG002 — kept for API compatibility
    ) -> dict[str, Any]:
        """Read a resource from this MCP server."""
        self._check_callable()
        return await self._dispatch(self._outbound.read_resource(uri))  # type: ignore[union-attr]

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout_s: float | None = None,  # noqa: ARG002 — kept for API compatibility
    ) -> dict[str, Any]:
        """Get a prompt from this MCP server."""
        self._check_callable()
        return await self._dispatch(self._outbound.get_prompt(name, arguments))  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_callable(self) -> None:
        """Raise ConnectionError if this connection cannot accept new requests."""
        if self._state == ConnectionState.DRAINING:
            raise ConnectionError(
                f"MCP {self.name} is draining — no new requests accepted"
            )
        if self._outbound is None:
            raise ConnectionError(f"MCP {self.name} not connected")

    async def _dispatch(self, coro: Any) -> Any:
        """Run *coro* while tracking the in-flight count for drain semantics."""
        self._in_flight += 1
        try:
            return await coro
        finally:
            self._in_flight -= 1
            if self._in_flight == 0 and self._drain_event is not None:
                self._drain_event.set()
