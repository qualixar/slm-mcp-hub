"""W5-P2 — Queue-based SSE event stream bridge.

``EventStreamBridge`` bridges :class:`~slm_mcp_hub.resilience.events.LifecycleEventBus`
(synchronous fan-out) to SSE clients (async stream). Each call to :meth:`stream`
creates an isolated SSE session with its own bounded :class:`asyncio.Queue` and a
synchronous consumer registered on the bus.

Non-blocking guarantee
----------------------
The consumer callback uses :meth:`asyncio.Queue.put_nowait` **only** — it
NEVER awaits anything. Because :meth:`LifecycleEventBus.emit` is synchronous
and calls consumers synchronously, a dead or lagging SSE client can NEVER
block ``emit()`` or any other consumer.

Drop-oldest on overflow
-----------------------
When the per-client queue is full (``queue_maxsize`` reached), the **oldest**
event is removed via :meth:`asyncio.Queue.get_nowait` before the newest is
enqueued. A WARNING is logged for every drop. This prevents unbounded queue
growth and ensures the client sees recent events once it catches up.

Keepalive
---------
When no events arrive for :data:`SSE_KEEPALIVE_INTERVAL_S` seconds,
:meth:`stream` yields ``': keepalive\\n\\n'`` to keep the TCP connection
alive through proxies that close idle streams.

Resource safety
---------------
The ``finally`` block in :meth:`stream` calls the unsubscribe callable
returned by :meth:`~.LifecycleEventBus.register_consumer`, ensuring the
consumer is deregistered on client disconnect (generator closed or
cancelled). No queue reference leaks after disconnect.

Security
--------
:func:`_event_to_sse_data` uses an explicit whitelist of safe fields:
``server``, ``from_state``, ``to_state``, ``reason``, ``ts``,
``failure_class``, ``attempt``. No configuration, environment variables,
headers, tokens, credentials, or command paths reach the SSE stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slm_mcp_hub.resilience.events import LifecycleEventBus
    from slm_mcp_hub.resilience.lifecycle import LifecycleEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constant (patched in tests via monkeypatch)
# ---------------------------------------------------------------------------

#: Seconds of queue silence before stream() emits a keepalive comment.
#: Patched to a small value in unit tests to avoid long waits.
#: Default 15 s keeps connections alive through typical proxy timeouts.
SSE_KEEPALIVE_INTERVAL_S: float = 15.0


# ---------------------------------------------------------------------------
# Serialisation — explicit safe-fields whitelist
# ---------------------------------------------------------------------------


def _event_to_sse_data(event: "LifecycleEvent") -> str:
    """Serialise *event* to a complete SSE chunk string.

    Format::

        event: lifecycle
        data: <json>

    (Two trailing newlines included to mark the end of the SSE event.)

    Only the following fields are included — this is an **explicit whitelist**:

    * ``server`` — name of the MCP server
    * ``from_state`` — state before the transition (string value)
    * ``to_state`` — state after the transition (string value)
    * ``reason`` — human-readable explanation
    * ``ts`` — Unix timestamp
    * ``failure_class`` — optional classifier (may be ``null``)
    * ``attempt`` — optional retry counter (may be ``null``)

    No configuration, environment variables, headers, tokens, credentials,
    or command paths are included.

    Parameters
    ----------
    event:
        Immutable :class:`~slm_mcp_hub.resilience.lifecycle.LifecycleEvent`
        to serialise.

    Returns
    -------
    str
        Complete SSE chunk, ready to encode and stream to the client.
    """
    payload: dict[str, object] = {
        "server": event.server,
        "from_state": event.from_state.value,
        "to_state": event.to_state.value,
        "reason": event.reason,
        "ts": event.ts,
        "failure_class": event.failure_class,
        "attempt": event.attempt,
    }
    return f"event: lifecycle\ndata: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# EventStreamBridge
# ---------------------------------------------------------------------------


class EventStreamBridge:
    """Bridges :class:`~slm_mcp_hub.resilience.events.LifecycleEventBus` to SSE clients.

    Each call to :meth:`stream` creates a new per-client session:

    1. A bounded :class:`asyncio.Queue` is created.
    2. A synchronous consumer is registered on *bus* via
       :meth:`~.LifecycleEventBus.register_consumer`.
    3. The consumer does ``queue.put_nowait(event)`` (non-blocking).
       On :exc:`asyncio.QueueFull`, the **oldest** item is dropped first
       (``get_nowait`` then ``put_nowait``) and a WARNING is logged.
    4. :meth:`stream` drains the queue asynchronously and yields SSE chunks.
    5. On client disconnect (generator closed or cancelled), ``finally``
       calls the unsubscribe callable — no consumer or queue leaks.

    Parameters
    ----------
    bus:
        The :class:`~slm_mcp_hub.resilience.events.LifecycleEventBus` to
        subscribe to for lifecycle events.
    queue_maxsize:
        Maximum events buffered per client before drop-oldest triggers.
        Sourced from :attr:`~slm_mcp_hub.core.config.HubConfig.event_queue_maxsize`
        (default ``256``).
    """

    def __init__(
        self,
        bus: "LifecycleEventBus",
        queue_maxsize: int = 256,
    ) -> None:
        self._bus = bus
        # Clamp to >=1 so the non-blocking bound can NEVER be silently disabled:
        # asyncio.Queue treats maxsize <= 0 as UNBOUNDED, which would defeat
        # drop-oldest and let a dead client grow memory without bound.
        # A non-positive event_queue_maxsize (config typo) is coerced to 1.
        self._queue_maxsize = max(1, queue_maxsize)

    async def stream(self) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted strings for one connected client.

        Each invocation is isolated: its own queue, its own consumer, its own
        keepalive timer. Multiple concurrent calls to ``stream()`` are safe —
        each client sees every event independently.

        Yields
        ------
        str
            ``'event: lifecycle\\ndata: {json}\\n\\n'`` for lifecycle events,
            ``': keepalive\\n\\n'`` after :data:`SSE_KEEPALIVE_INTERVAL_S` idle.

        Notes
        -----
        The generator's ``finally`` block is guaranteed to run on:

        * Normal exhaustion (``StopAsyncIteration``).
        * :meth:`aclose` called by the framework on client disconnect.
        * :exc:`asyncio.CancelledError` when the response task is cancelled
          by Starlette/FastAPI on client disconnect.

        In all cases ``unsubscribe()`` is called, removing the consumer from
        the bus and allowing the queue to be garbage-collected.
        """
        # Per-client bounded queue — created fresh for each connected client.
        queue: asyncio.Queue["LifecycleEvent"] = asyncio.Queue(
            maxsize=self._queue_maxsize
        )

        # Capture maxsize in closure so the warning log can include it without
        # accessing self (avoids a potential reference cycle).
        _maxsize = self._queue_maxsize

        def _consumer(event: "LifecycleEvent") -> None:
            """Synchronous, non-blocking consumer.

            Called by :meth:`~.LifecycleEventBus.emit` on the synchronous
            lifecycle path. MUST NEVER await or block.

            On queue full: drop the oldest event (``get_nowait`` + WARNING),
            then enqueue the newest via ``put_nowait``.

            Safety proof (single-threaded asyncio, no await):
            If ``put_nowait`` raises ``QueueFull``, the queue has ``maxsize``
            items (≥1).  ``get_nowait`` on a non-empty queue always succeeds.
            After removing one item the queue has ``maxsize-1`` items, so the
            subsequent ``put_nowait`` always succeeds.  No other code can touch
            this queue between these two calls because asyncio is single-threaded
            and we never yield (no await).
            """
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop-oldest: remove the head to make room for the newest event.
                # get_nowait() is guaranteed to succeed — queue is full (≥1 item).
                # put_nowait() is guaranteed to succeed — queue now has maxsize-1 items.
                queue.get_nowait()
                queue.put_nowait(event)
                logger.warning(
                    "SSE client queue full (maxsize=%d); dropping oldest event "
                    "to enqueue server=%r (%s -> %s). "
                    "Client is too slow or has disconnected.",
                    _maxsize,
                    event.server,
                    event.from_state.value,
                    event.to_state.value,
                )

        # Register the consumer — returns an unsubscribe callable.
        unsubscribe = self._bus.register_consumer(_consumer)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=SSE_KEEPALIVE_INTERVAL_S,
                    )
                    yield _event_to_sse_data(event)
                except asyncio.TimeoutError:
                    # No events for SSE_KEEPALIVE_INTERVAL_S — emit a comment
                    # to keep the TCP connection alive through idle-connection
                    # timeouts in proxies (nginx, HAProxy, etc.).
                    yield ": keepalive\n\n"
        finally:
            # Unsubscribe regardless of how the generator exits (aclose,
            # CancelledError, or normal exhaustion). This is the ONLY place
            # unsubscribe() is called — idempotent by design.
            unsubscribe()
