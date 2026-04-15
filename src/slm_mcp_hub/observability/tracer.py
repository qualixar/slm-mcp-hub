"""Request tracer — records tool call traces for debugging.

Gap 6: Observability — trace every request through the hub.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

from slm_mcp_hub.core.constants import TRACE_RING_BUFFER_SIZE


@dataclass(frozen=True)
class TraceSpan:
    """Immutable record of a traced tool call."""

    trace_id: str
    session_id: str
    server_name: str
    tool_name: str
    start_time: float
    end_time: float
    duration_ms: int
    success: bool
    cached: bool
    cost_cents: float


class RequestTracer:
    """In-memory ring buffer of recent request traces.

    Fixed size — no disk growth. Oldest traces evicted automatically.
    """

    def __init__(self, max_traces: int = TRACE_RING_BUFFER_SIZE) -> None:
        self._buffer: deque[TraceSpan] = deque(maxlen=max_traces)
        self._pending: dict[str, dict[str, Any]] = {}
        self._max_traces = max_traces

    @property
    def size(self) -> int:
        return len(self._buffer)

    def start_trace(self, session_id: str, server_name: str, tool_name: str) -> str:
        """Start a new trace. Returns trace_id."""
        trace_id = str(uuid.uuid4())
        self._pending[trace_id] = {
            "session_id": session_id,
            "server_name": server_name,
            "tool_name": tool_name,
            "start_time": time.time(),
        }
        return trace_id

    def end_trace(
        self,
        trace_id: str,
        success: bool,
        cached: bool = False,
        cost_cents: float = 0.0,
    ) -> TraceSpan | None:
        """End a trace and record the span. Returns None if trace_id not found."""
        pending = self._pending.pop(trace_id, None)
        if pending is None:
            return None

        end_time = time.time()
        duration_ms = int((end_time - pending["start_time"]) * 1000)

        span = TraceSpan(
            trace_id=trace_id,
            session_id=pending["session_id"],
            server_name=pending["server_name"],
            tool_name=pending["tool_name"],
            start_time=pending["start_time"],
            end_time=end_time,
            duration_ms=duration_ms,
            success=success,
            cached=cached,
            cost_cents=cost_cents,
        )
        self._buffer.append(span)
        return span

    def get_trace(self, trace_id: str) -> TraceSpan | None:
        """Get a specific trace by ID."""
        for span in self._buffer:
            if span.trace_id == trace_id:
                return span
        return None

    def get_recent(self, n: int = 20) -> list[TraceSpan]:
        """Get the N most recent traces (newest first)."""
        traces = list(self._buffer)
        traces.reverse()
        return traces[:n]

    def get_stats(self) -> dict[str, Any]:
        """Return tracer statistics."""
        if not self._buffer:
            return {"total_traces": 0, "pending": len(self._pending)}

        durations = [s.duration_ms for s in self._buffer]
        successes = sum(1 for s in self._buffer if s.success)
        cached = sum(1 for s in self._buffer if s.cached)

        return {
            "total_traces": len(self._buffer),
            "pending": len(self._pending),
            "success_rate": round(successes / len(self._buffer), 3),
            "cache_rate": round(cached / len(self._buffer), 3),
            "avg_duration_ms": round(sum(durations) / len(durations), 1),
            "max_traces": self._max_traces,
        }
