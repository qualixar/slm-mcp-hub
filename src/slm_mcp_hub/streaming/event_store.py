"""In-memory EventStore for hub-as-server resumable streaming (W4-P3).

One instance is shared across all sessions via StreamableHTTPSessionManager.

Bounded design:
- ``max_events_per_stream``: ring buffer (deque maxlen) per stream; oldest events evicted.
- ``max_streams``: hard cap; when a new stream is created beyond the cap, the
  oldest stream (by ``created_at``) is evicted entirely.
- ``stream_ttl_s``: streams idle longer than TTL are pruned lazily on every
  ``store_event`` call.

EventIds are globally sequential integers serialised as strings ("0", "1", "2", ...).
Global ordering allows O(n) replay-after lookup without a secondary sort.

Thread-safety: all mutations are protected by a single ``asyncio.Lock``.
``replay_events_after`` collects events under the lock, then invokes callbacks
OUTSIDE the lock to avoid holding it during potentially slow I/O.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Final

from mcp.server.streamable_http import (
    EventCallback,
    EventId,
    EventMessage,
    EventStore,
    StreamId,
)

# Re-exported for import convenience by callers
try:
    from mcp.server.streamable_http import JSONRPCMessage
except ImportError:  # pragma: no cover — SDK version guard
    JSONRPCMessage = object  # type: ignore[assignment,misc]

__all__ = [
    "InMemoryEventStore",
    "DEFAULT_MAX_EVENTS_PER_STREAM",
    "DEFAULT_MAX_STREAMS",
    "DEFAULT_STREAM_TTL_S",
]

DEFAULT_MAX_EVENTS_PER_STREAM: Final[int] = 500
DEFAULT_MAX_STREAMS: Final[int] = 200
DEFAULT_STREAM_TTL_S: Final[float] = 7200.0  # 2 hours — covers UNBOUNDED-class calls


@dataclass
class _StreamRecord:
    """Bounded ring buffer for one stream's events.

    ``events`` is a deque with ``maxlen`` set to ``max_size``; Python's deque
    automatically evicts the oldest item when the capacity is exceeded (ring buffer
    semantics — no explicit eviction code needed).

    ``created_at`` and ``last_event_at`` use ``time.monotonic()`` (no wall-clock
    drift, safe on systems without a real-time clock).
    """

    stream_id: StreamId
    events: deque  # deque of (event_id_str, EventMessage) pairs
    created_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    max_size: int = DEFAULT_MAX_EVENTS_PER_STREAM


class InMemoryEventStore(EventStore):
    """Thread-safe, bounded in-memory EventStore implementing the SDK ABC.

    Implements ``mcp.server.streamable_http.EventStore``.

    EventIds are globally sequential integers serialised as strings so that
    ``replay_events_after`` can determine ordering without a sort.

    **Replay algorithm** (no gap, no dup guarantee):

    1. Scan all streams for a stored event whose id exactly equals ``last_event_id``.
       If found in stream S, replay S's events with id > ``last_event_id``.
    2. If not found (event evicted from ring buffer, or sentinel id like ``"-1"``),
       collect streams whose *minimum stored* id > ``last_event_id``.
       If exactly ONE such stream exists, replay its events (best-effort partial
       recovery after ring-buffer eviction or sentinel replay).
    3. Otherwise return ``None`` — the stream has been evicted or the id is ambiguous.

    **Memory bound**: 200 streams × 500 events × ~200 bytes/event ≈ 20 MB worst case.

    Constructor args:
        max_events_per_stream: Ring buffer capacity per stream (oldest events evicted
            when full). Default 500.
        max_streams: Maximum concurrent streams. When a new stream is allocated
            beyond this cap, the oldest stream (by ``created_at``) is evicted entirely.
            Default 200.
        stream_ttl_s: Streams not touched for this many seconds are pruned lazily.
            Default 7200 (2 hours) — covers 30-minute UNBOUNDED calls with margin.
    """

    def __init__(
        self,
        max_events_per_stream: int = DEFAULT_MAX_EVENTS_PER_STREAM,
        max_streams: int = DEFAULT_MAX_STREAMS,
        stream_ttl_s: float = DEFAULT_STREAM_TTL_S,
    ) -> None:
        self._max_events_per_stream = max_events_per_stream
        self._max_streams = max_streams
        self._stream_ttl_s = stream_ttl_s
        # Dict preserves insertion order (Python 3.7+). Oldest stream is first.
        self._streams: dict[StreamId, _StreamRecord] = {}
        self._lock = asyncio.Lock()
        self._next_event_id: int = 0

    # ------------------------------------------------------------------
    # EventStore ABC implementation
    # ------------------------------------------------------------------

    async def store_event(
        self,
        stream_id: StreamId,
        message: object,  # JSONRPCMessage | None — typed as object for None support
    ) -> EventId:
        """Store one event; return its generated EventId.

        A ``None`` message is a priming event — the SDK calls ``store_event(stream_id,
        None)`` before the first real event to establish the stream's event sequence.
        Priming events are stored as sentinels so ``replay_events_after`` can locate
        the stream even when no real events have arrived yet. Priming sentinels are
        SKIPPED during replay (the send_callback is not invoked for them).
        """
        async with self._lock:
            self._prune_expired_streams()
            record = self._get_or_create_stream(stream_id)
            event_id = str(self._next_event_id)
            self._next_event_id += 1
            # EventMessage is a dataclass; message=None is accepted at runtime
            # even though the type annotation says JSONRPCMessage. This is intentional
            # for priming events (W4 LLD §6.2).
            em = EventMessage(message=message, event_id=event_id)  # type: ignore[arg-type]
            record.events.append((event_id, em))
            record.last_event_at = time.monotonic()
            return event_id

    async def replay_events_after(
        self,
        last_event_id: EventId,
        send_callback: EventCallback,
    ) -> StreamId | None:
        """Replay all events with id > ``last_event_id``; return stream_id or None.

        No lock is held during callback invocations — events are collected under the
        lock, then the lock is released before calling ``send_callback``. This prevents
        a slow downstream writer from blocking concurrent ``store_event`` calls.

        Returns ``None`` when:
        - ``last_event_id`` is not an integer string.
        - The owning stream has been evicted (max_streams cap or TTL).
        - Multiple ambiguous candidate streams exist (can't determine which one the
          client was connected to).
        """
        target_int = self._parse_event_id(last_event_id)
        if target_int is None:
            return None

        events_to_send: list[EventMessage] = []
        found_stream_id: StreamId | None = None

        async with self._lock:
            # ── Pass 1: find the stream that contains last_event_id in its buffer ──
            for sid, record in self._streams.items():
                for ev_id, _ in record.events:
                    if int(ev_id) == target_int:
                        found_stream_id = sid
                        break
                if found_stream_id is not None:
                    # Collect events strictly after target, skipping priming sentinels
                    events_to_send = [
                        em
                        for ev_id, em in record.events
                        if int(ev_id) > target_int and em.message is not None
                    ]
                    record.last_event_at = time.monotonic()
                    break

            # ── Pass 2: target not in any buffer ────────────────────────────────
            # Handle two sub-cases:
            #   (a) Ring-buffer eviction — target WAS in the stream, but evicted from
            #       the deque. The stream still exists, all remaining events have
            #       higher ids.
            #   (b) Sentinel (e.g. "-1") — client never received any event; replay
            #       all stored events.
            # In both cases: collect streams where min_stored_id > target.
            # If exactly ONE such stream exists, use it. Multiple → ambiguous → None.
            if found_stream_id is None:
                candidates: list[tuple[StreamId, _StreamRecord]] = []
                for sid, record in self._streams.items():
                    if not record.events:
                        continue
                    min_id = int(record.events[0][0])
                    if min_id > target_int:
                        candidates.append((sid, record))

                if len(candidates) == 1:
                    found_stream_id, record = candidates[0]
                    events_to_send = [
                        em
                        for ev_id, em in record.events
                        if int(ev_id) > target_int and em.message is not None
                    ]
                    record.last_event_at = time.monotonic()
                # else: 0 or >1 candidates → return None

        # Call callbacks OUTSIDE the lock to avoid blocking store_event callers
        for em in events_to_send:
            await send_callback(em)

        return found_stream_id

    # ------------------------------------------------------------------
    # Diagnostic property
    # ------------------------------------------------------------------

    @property
    def stream_count(self) -> int:
        """Current number of tracked streams. Diagnostic / test use only.

        Not lock-protected — call only from a single thread or in tests where
        there is no concurrent mutation.
        """
        return len(self._streams)

    # ------------------------------------------------------------------
    # Internal helpers (all called under _lock)
    # ------------------------------------------------------------------

    def _get_or_create_stream(self, stream_id: StreamId) -> _StreamRecord:
        """Return existing record or create one; evict oldest stream if at cap.

        Insertion order in ``_streams`` dict reflects creation order because Python
        dicts preserve insertion order. The oldest stream is therefore the first key.
        """
        if stream_id in self._streams:
            return self._streams[stream_id]

        if len(self._streams) >= self._max_streams:
            # Evict the oldest stream (first key in insertion-ordered dict)
            oldest_id = next(iter(self._streams))
            del self._streams[oldest_id]

        record = _StreamRecord(
            stream_id=stream_id,
            events=deque(maxlen=self._max_events_per_stream),
            max_size=self._max_events_per_stream,
        )
        self._streams[stream_id] = record
        return record

    def _prune_expired_streams(self) -> None:
        """Remove streams idle longer than ``stream_ttl_s``. Called under lock.

        Uses a list snapshot to avoid mutating the dict while iterating.
        """
        cutoff = time.monotonic() - self._stream_ttl_s
        expired = [
            sid
            for sid, record in self._streams.items()
            if record.last_event_at < cutoff
        ]
        for sid in expired:
            del self._streams[sid]

    @staticmethod
    def _parse_event_id(event_id: EventId) -> int | None:
        """Parse ``event_id`` as int; return None on failure.

        Handles non-integer strings gracefully — the SDK may pass unexpected values.
        """
        try:
            return int(event_id)
        except (ValueError, TypeError):
            return None
