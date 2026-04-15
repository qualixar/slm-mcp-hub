"""Standalone learning engine — frequency, success rates, tool chains.

Gap 12: Learning Integration (standalone portion).
No SLM dependency. With SLM plugin, gains predictive capabilities.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CHAIN_WINDOW_SECONDS = 60.0  # Tools called within 60s = potential chain


@dataclass(frozen=True)
class ToolCallRecord:
    """Immutable record of a tool call."""

    session_id: str
    server_name: str
    tool_name: str
    duration_ms: int
    success: bool
    cost_cents: float
    timestamp: float


class LearningEngine:
    """Standalone learning from tool call patterns.

    Tracks frequency, success rates, tool chains, and slow tools.
    No SLM dependency — uses only in-memory data + SQLite.
    With SLM plugin (Phase 6), feeds into assertion engine.
    """

    def __init__(self, slow_threshold_ms: int = 10_000) -> None:
        self._records: list[ToolCallRecord] = []
        self._frequency: Counter[str] = Counter()
        self._success_counts: dict[str, tuple[int, int]] = {}  # tool → (successes, total)
        self._slow_threshold = slow_threshold_ms
        self._max_records = 10_000  # In-memory limit

    def record(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        duration_ms: int,
        success: bool,
        cost_cents: float = 0.0,
    ) -> None:
        """Record a tool call for learning."""
        rec = ToolCallRecord(
            session_id=session_id,
            server_name=server_name,
            tool_name=tool_name,
            duration_ms=duration_ms,
            success=success,
            cost_cents=cost_cents,
            timestamp=time.time(),
        )

        # Trim if over limit (keep recent)
        if len(self._records) >= self._max_records:
            self._records = self._records[self._max_records // 2:]

        self._records.append(rec)

        namespaced = f"{server_name}__{tool_name}"
        self._frequency[namespaced] += 1

        prev_success, prev_total = self._success_counts.get(namespaced, (0, 0))
        self._success_counts[namespaced] = (
            prev_success + (1 if success else 0),
            prev_total + 1,
        )

    def get_frequency_ranking(self, n: int = 20) -> list[tuple[str, int]]:
        """Get the N most frequently used tools."""
        return self._frequency.most_common(n)

    def get_success_rates(self) -> dict[str, float]:
        """Get success rate per tool (0.0–1.0)."""
        rates = {}
        for tool, (successes, total) in self._success_counts.items():
            rates[tool] = successes / total if total > 0 else 0.0
        return rates

    def get_slow_tools(self) -> list[tuple[str, float]]:
        """Get tools with average duration above the slow threshold.

        Returns list of (tool_name, avg_duration_ms).
        """
        durations: dict[str, list[int]] = {}
        for rec in self._records:
            ns = f"{rec.server_name}__{rec.tool_name}"
            durations.setdefault(ns, []).append(rec.duration_ms)

        slow = []
        for tool, durs in durations.items():
            avg = sum(durs) / len(durs)
            if avg > self._slow_threshold:
                slow.append((tool, round(avg, 1)))

        return sorted(slow, key=lambda x: -x[1])

    def detect_chains(self, session_id: str, min_count: int = 3) -> list[tuple[str, str, int]]:
        """Detect tool chains — sequential tools within CHAIN_WINDOW_SECONDS.

        Returns list of (tool_a, tool_b, count) where tool_b often follows tool_a.
        Only returns chains that occurred at least min_count times.
        """
        session_records = [r for r in self._records if r.session_id == session_id]
        session_records.sort(key=lambda r: r.timestamp)

        pair_counts: Counter[tuple[str, str]] = Counter()
        for i in range(len(session_records) - 1):
            a = session_records[i]
            b = session_records[i + 1]
            if (b.timestamp - a.timestamp) <= CHAIN_WINDOW_SECONDS:
                key_a = f"{a.server_name}__{a.tool_name}"
                key_b = f"{b.server_name}__{b.tool_name}"
                pair_counts[(key_a, key_b)] += 1

        chains = [
            (a, b, count)
            for (a, b), count in pair_counts.most_common()
            if count >= min_count
        ]
        return chains

    def get_stats(self) -> dict[str, Any]:
        """Return learning statistics."""
        return {
            "total_records": len(self._records),
            "unique_tools": len(self._frequency),
            "top_5_tools": self._frequency.most_common(5),
            "slow_tools_count": len(self.get_slow_tools()),
        }
