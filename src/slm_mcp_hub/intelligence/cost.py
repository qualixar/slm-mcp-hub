"""Cost tracking engine for tool call budget enforcement.

Gap 3: Cost Intelligence — per-tool costs, session budgets,
cascade routing to cheaper alternatives.

Supports configurable cost tables — user can override any tool cost.
Pre-configured defaults for known metered MCPs.
Future: auto-updating model prices from external pricing databases.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Pre-configured costs for known metered MCPs (cents per call).
# Users override via config.json "cost_table" key.
# Future: auto-populate from LiteLLM model_cost database.
DEFAULT_COST_TABLE: dict[str, float] = {
    # Perplexity (metered, ~$1/1000 queries)
    "perplexity__perplexity_search": 1.0,
    "perplexity__perplexity_ask": 1.0,
    "perplexity__perplexity_research": 5.0,
    "perplexity__perplexity_reason": 2.0,
    # Tavily (metered)
    "tavily__tavily_search": 1.0,
    "tavily__tavily_crawl": 2.0,
    "tavily__tavily_extract": 1.0,
    # Exa (metered)
    "exa__web_search_exa": 1.0,
    "exa__crawling_exa": 2.0,
    # Gemini (paid tier, ~₹1K/mo)
    "gemini__gemini-deep-research": 5.0,
    "gemini__gemini-search": 0.5,
    "gemini__gemini-query": 0.3,
    "gemini__gemini-generate-image": 2.0,
    "gemini__gemini-generate-video": 12.0,
    # fal.ai (credits-based)
    "fal_ai__generate_image": 3.0,
    "fal_ai__generate_video": 10.0,
    "fal_ai__text_to_speech": 1.0,
    # Firecrawl (metered)
    "firecrawl__firecrawl_scrape": 1.0,
    "firecrawl__firecrawl_crawl": 3.0,
}


def load_cost_table_from_file(path: Path) -> dict[str, float]:
    """Load user cost overrides from a JSON file.

    File format: {"tool_name": cost_cents, ...}
    """
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return {k: float(v) for k, v in data.items()}
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.warning("Failed to load cost table from %s: %s", path, exc)
        return {}


@dataclass  # Intentionally NOT frozen: budget tracking requires in-place mutation for performance
class BudgetInfo:
    """Mutable budget tracker for a scope — accumulator, not a value object."""

    scope: str
    budget_cents: float
    spent_cents: float = 0.0
    period_start: float = field(default_factory=time.time)

    @property
    def remaining_cents(self) -> float:
        return max(0.0, self.budget_cents - self.spent_cents)

    @property
    def is_over_budget(self) -> bool:
        return self.spent_cents >= self.budget_cents > 0


@dataclass(frozen=True)
class CascadeOption:
    """One option in a cost cascade chain."""

    tool_name: str
    cost_cents: float


class CostEngine:
    """Track tool call costs and enforce budgets.

    Standalone — no SLM dependency.
    """

    def __init__(
        self,
        cost_table: dict[str, float] | None = None,
        cascades: dict[str, list[CascadeOption]] | None = None,
    ) -> None:
        self._cost_table: dict[str, float] = dict(DEFAULT_COST_TABLE)
        if cost_table:
            self._cost_table.update(cost_table)
        self._cascades: dict[str, list[CascadeOption]] = cascades or {}
        self._budgets: dict[str, BudgetInfo] = {}
        self._total_spent: float = 0.0
        self._call_count: int = 0

    @property
    def total_spent_cents(self) -> float:
        return self._total_spent

    @property
    def total_calls(self) -> int:
        return self._call_count

    def get_tool_cost(self, namespaced_tool: str) -> float:
        """Get the cost of a tool call in cents. Default 0 (free)."""
        return self._cost_table.get(namespaced_tool, 0.0)

    def set_tool_cost(self, namespaced_tool: str, cost_cents: float) -> None:
        """Set or update the cost of a tool."""
        self._cost_table[namespaced_tool] = cost_cents

    def set_budget(self, scope: str, budget_cents: float) -> None:
        """Set a budget for a scope (e.g., 'global', 'daily', 'session:xxx')."""
        self._budgets[scope] = BudgetInfo(
            scope=scope,
            budget_cents=budget_cents,
            period_start=time.time(),
        )

    def get_budget(self, scope: str) -> BudgetInfo | None:
        """Get budget info for a scope."""
        return self._budgets.get(scope)

    def can_afford(self, namespaced_tool: str, session_id: str = "") -> bool:
        """Check if a tool call is within all applicable budgets."""
        cost = self.get_tool_cost(namespaced_tool)
        if cost <= 0:
            return True  # Free tools always allowed

        # Check global budget
        global_budget = self._budgets.get("global")
        if global_budget and (global_budget.spent_cents + cost) > global_budget.budget_cents > 0:
            return False

        # Check session budget
        if session_id:
            session_budget = self._budgets.get(f"session:{session_id}")
            if session_budget and (session_budget.spent_cents + cost) > session_budget.budget_cents > 0:
                return False

        return True

    def record_cost(self, namespaced_tool: str, session_id: str = "") -> float:
        """Record a tool call cost. Returns the cost in cents."""
        cost = self.get_tool_cost(namespaced_tool)
        self._total_spent += cost
        self._call_count += 1

        # Update global budget
        global_budget = self._budgets.get("global")
        if global_budget:
            global_budget.spent_cents += cost

        # Update session budget
        if session_id:
            session_key = f"session:{session_id}"
            session_budget = self._budgets.get(session_key)
            if session_budget:
                session_budget.spent_cents += cost

        if cost > 0:
            logger.debug("Cost recorded: %s = %.1f cents (total=%.1f)", namespaced_tool, cost, self._total_spent)

        return cost

    def get_cascade(self, namespaced_tool: str) -> list[CascadeOption] | None:
        """Get the cost cascade chain for a tool (cheaper alternatives)."""
        return self._cascades.get(namespaced_tool)

    def set_cascade(self, namespaced_tool: str, options: list[CascadeOption]) -> None:
        """Set a cost cascade chain for a tool."""
        self._cascades[namespaced_tool] = options

    def find_affordable_alternative(
        self,
        namespaced_tool: str,
        session_id: str = "",
    ) -> str | None:
        """Find the cheapest affordable alternative in the cascade chain.

        Returns the namespaced tool name, or None if no alternative found.
        """
        cascade = self._cascades.get(namespaced_tool)
        if not cascade:
            return None

        for option in cascade:
            if self.can_afford(option.tool_name, session_id):
                return option.tool_name

        return None

    def get_stats(self) -> dict[str, Any]:
        """Return cost tracking statistics."""
        return {
            "total_spent_cents": round(self._total_spent, 2),
            "total_calls": self._call_count,
            "budgets": {
                scope: {
                    "budget_cents": b.budget_cents,
                    "spent_cents": round(b.spent_cents, 2),
                    "remaining_cents": round(b.remaining_cents, 2),
                    "is_over_budget": b.is_over_budget,
                }
                for scope, b in self._budgets.items()
            },
        }

    def reset_budget(self, scope: str) -> bool:
        """Reset spent amount for a budget scope. Returns False if not found."""
        budget = self._budgets.get(scope)
        if budget is None:
            return False
        budget.spent_cents = 0.0
        budget.period_start = time.time()
        return True
