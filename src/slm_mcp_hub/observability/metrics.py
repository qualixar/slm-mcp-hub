"""Metrics collector — aggregated performance metrics.

Gap 6: Observability — per-MCP and hub-wide metrics.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass  # Intentionally mutable: accumulator for hot-path metrics
class ServerMetrics:
    """Mutable metrics for a single MCP server."""

    call_count: int = 0
    success_count: int = 0
    total_duration_ms: int = 0
    max_duration_ms: int = 0
    cache_hits: int = 0
    total_cost_cents: float = 0.0
    durations: Any = field(default_factory=lambda: deque(maxlen=1000))

    @property
    def success_rate(self) -> float:
        return self.success_count / self.call_count if self.call_count > 0 else 0.0

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.call_count if self.call_count > 0 else 0.0

    @property
    def p95_duration_ms(self) -> float:
        if not self.durations:
            return 0.0
        sorted_d = sorted(self.durations)
        idx = int(len(sorted_d) * 0.95)
        return float(sorted_d[min(idx, len(sorted_d) - 1)])

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.call_count if self.call_count > 0 else 0.0


class MetricsCollector:
    """Collects per-MCP and hub-wide metrics. In-memory, never blocks."""

    def __init__(self) -> None:
        self._servers: dict[str, ServerMetrics] = defaultdict(ServerMetrics)
        self._total_calls: int = 0
        self._total_cost: float = 0.0
        self._started_at: float = time.time()

    def record(
        self,
        server_name: str,
        duration_ms: int,
        success: bool,
        cached: bool = False,
        cost_cents: float = 0.0,
    ) -> None:
        """Record a tool call metric."""
        m = self._servers[server_name]
        m.call_count += 1
        m.total_duration_ms += duration_ms
        m.max_duration_ms = max(m.max_duration_ms, duration_ms)
        if success:
            m.success_count += 1
        if cached:
            m.cache_hits += 1
        m.total_cost_cents += cost_cents

        # Duration history bounded by deque(maxlen=1000)
        m.durations.append(duration_ms)

        self._total_calls += 1
        self._total_cost += cost_cents

    def get_server_metrics(self, server_name: str) -> dict[str, Any]:
        """Get metrics for a specific server."""
        m = self._servers.get(server_name)
        if m is None:
            return {"server": server_name, "call_count": 0}
        return {
            "server": server_name,
            "call_count": m.call_count,
            "success_rate": round(m.success_rate, 3),
            "avg_duration_ms": round(m.avg_duration_ms, 1),
            "p95_duration_ms": round(m.p95_duration_ms, 1),
            "max_duration_ms": m.max_duration_ms,
            "cache_hit_rate": round(m.cache_hit_rate, 3),
            "total_cost_cents": round(m.total_cost_cents, 2),
        }

    def get_all_server_metrics(self) -> list[dict[str, Any]]:
        """Get metrics for all servers."""
        return [self.get_server_metrics(name) for name in sorted(self._servers.keys())]

    def get_hub_metrics(self) -> dict[str, Any]:
        """Get hub-wide aggregate metrics."""
        return {
            "total_calls": self._total_calls,
            "total_cost_cents": round(self._total_cost, 2),
            "active_servers": len(self._servers),
            "uptime_seconds": round(time.time() - self._started_at, 1),
        }
