"""Phase 3 Intelligence Core Tests — 100% coverage target."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from slm_mcp_hub.intelligence.cache import CacheEngine, CacheEntry, _make_cache_key
from slm_mcp_hub.intelligence.cost import BudgetInfo, CascadeOption, CostEngine
from slm_mcp_hub.intelligence.filtering import (
    ToolFilter,
    detect_project_type,
    get_relevant_servers,
)
from slm_mcp_hub.intelligence.learning import LearningEngine, ToolCallRecord
from slm_mcp_hub.intelligence.lifecycle import LifecycleManager


# ===========================================================================
# CacheEngine Tests
# ===========================================================================

class TestCacheKey:
    def test_deterministic(self):
        k1 = _make_cache_key("github", "search", {"q": "test"})
        k2 = _make_cache_key("github", "search", {"q": "test"})
        assert k1 == k2

    def test_different_args(self):
        k1 = _make_cache_key("github", "search", {"q": "a"})
        k2 = _make_cache_key("github", "search", {"q": "b"})
        assert k1 != k2

    def test_different_server(self):
        k1 = _make_cache_key("github", "search", {"q": "test"})
        k2 = _make_cache_key("context7", "search", {"q": "test"})
        assert k1 != k2

    def test_sorted_keys(self):
        k1 = _make_cache_key("s", "t", {"b": 2, "a": 1})
        k2 = _make_cache_key("s", "t", {"a": 1, "b": 2})
        assert k1 == k2  # Sorted, so order doesn't matter


class TestCacheEntry:
    def test_not_expired(self):
        entry = CacheEntry(key="k", result={"ok": True}, ttl_seconds=300)
        assert entry.is_expired is False
        assert entry.hit_count == 0

    def test_expired(self):
        entry = CacheEntry(key="k", result={}, ttl_seconds=0)
        time.sleep(0.01)
        assert entry.is_expired is True


class TestCacheEngine:
    def test_put_and_get(self):
        c = CacheEngine()
        c.put("github", "search", {"q": "test"}, {"content": [{"text": "result"}]})
        result = c.get("github", "search", {"q": "test"})
        assert result is not None
        assert result["content"][0]["text"] == "result"
        assert c.size == 1

    def test_miss(self):
        c = CacheEngine()
        assert c.get("github", "search", {"q": "a"}) is None
        assert c.miss_count == 1

    def test_ttl_expiry(self):
        c = CacheEngine(default_ttl=0)
        c.put("s", "t", {}, {"data": "old"})
        time.sleep(0.01)
        assert c.get("s", "t", {}) is None

    def test_no_cache_tool(self):
        c = CacheEngine()
        c.put("slm", "remember", {"content": "hi"}, {"ok": True})
        assert c.get("slm", "remember", {"content": "hi"}) is None
        assert c.size == 0  # Not stored

    def test_no_cache_namespaced(self):
        c = CacheEngine()
        assert c.is_cacheable("slm__remember") is False
        assert c.is_cacheable("slm__observe") is False
        assert c.is_cacheable("github__create_issue") is False

    def test_cacheable_tools(self):
        c = CacheEngine()
        assert c.is_cacheable("context7__query-docs") is True
        assert c.is_cacheable("github__search_code") is True

    def test_lru_eviction(self):
        c = CacheEngine(max_entries=2)
        c.put("s", "a", {}, {"a": 1})
        c.put("s", "b", {}, {"b": 2})
        c.put("s", "c", {}, {"c": 3})  # Should evict "a"
        assert c.get("s", "a", {}) is None
        assert c.get("s", "c", {}) is not None
        assert c.size == 2

    def test_hit_count(self):
        c = CacheEngine()
        c.put("s", "t", {"q": "x"}, {"r": 1})
        c.get("s", "t", {"q": "x"})
        c.get("s", "t", {"q": "x"})
        assert c.hit_count == 2
        assert c.miss_count == 0
        assert c.hit_rate > 0

    def test_hit_rate_zero(self):
        c = CacheEngine()
        assert c.hit_rate == 0.0

    def test_invalidate_server(self):
        c = CacheEngine()
        c.put("github", "a", {}, {"r": 1})
        c.put("github", "b", {}, {"r": 2})
        c.put("context7", "c", {}, {"r": 3})
        removed = c.invalidate("github")
        assert removed == 2
        assert c.size == 1

    def test_invalidate_tool(self):
        c = CacheEngine()
        c.put("github", "search", {"q": "a"}, {"r": 1})
        c.put("github", "search", {"q": "b"}, {"r": 2})
        c.put("github", "create", {}, {"r": 3})
        removed = c.invalidate("github", "search")
        assert removed == 2

    def test_clear(self):
        c = CacheEngine()
        c.put("s", "t", {}, {"r": 1})
        c.clear()
        assert c.size == 0

    def test_stats(self):
        c = CacheEngine()
        c.put("s", "t", {}, {"r": 1})
        c.get("s", "t", {})
        stats = c.get_stats()
        assert stats["size"] == 1
        assert stats["hits"] == 1
        assert stats["hit_rate"] > 0

    def test_custom_ttl(self):
        c = CacheEngine(default_ttl=300)
        c.put("s", "t", {}, {"r": 1}, ttl_seconds=3600)
        result = c.get("s", "t", {})
        assert result is not None

    def test_evict_lru_empty_access_order(self):
        """_evict_lru returns early when access_order is empty (line 192)."""
        c = CacheEngine(max_entries=2)
        # Manually clear access order but leave cache non-empty
        c._access_order.clear()
        c._evict_lru()  # Should not raise

    def test_cleanup_expired_removes_entries(self):
        """_cleanup_expired removes expired entries (line 206)."""
        c = CacheEngine(default_ttl=0)
        c.put("s", "tool1", {"a": 1}, {"r": 1})
        c.put("s", "tool2", {"b": 2}, {"r": 2})
        time.sleep(0.02)
        # get_stats calls _cleanup_expired internally
        stats = c.get_stats()
        assert stats["size"] == 0  # Both expired and cleaned


# ===========================================================================
# CostEngine Tests
# ===========================================================================

class TestBudgetInfo:
    def test_remaining(self):
        b = BudgetInfo(scope="global", budget_cents=100, spent_cents=30)
        assert b.remaining_cents == 70

    def test_over_budget(self):
        b = BudgetInfo(scope="global", budget_cents=100, spent_cents=100)
        assert b.is_over_budget is True

    def test_zero_budget_not_over(self):
        b = BudgetInfo(scope="global", budget_cents=0, spent_cents=0)
        assert b.is_over_budget is False


class TestCostEngine:
    def test_free_tool(self):
        ce = CostEngine()
        assert ce.get_tool_cost("context7__query-docs") == 0.0

    def test_metered_tool(self):
        ce = CostEngine()
        assert ce.get_tool_cost("perplexity__perplexity_search") == 1.0

    def test_custom_cost(self):
        ce = CostEngine(cost_table={"my_tool": 5.0})
        assert ce.get_tool_cost("my_tool") == 5.0

    def test_set_tool_cost(self):
        ce = CostEngine()
        ce.set_tool_cost("new_tool", 3.0)
        assert ce.get_tool_cost("new_tool") == 3.0

    def test_can_afford_free(self):
        ce = CostEngine()
        ce.set_budget("global", 0)
        assert ce.can_afford("context7__query-docs") is True  # Free always ok

    def test_can_afford_within_budget(self):
        ce = CostEngine()
        ce.set_budget("global", 100)
        assert ce.can_afford("perplexity__perplexity_search") is True

    def test_cannot_afford_over_budget(self):
        ce = CostEngine()
        ce.set_budget("global", 0.5)  # 0.5 cents budget
        assert ce.can_afford("perplexity__perplexity_search") is False  # Costs 1 cent

    def test_session_budget(self):
        ce = CostEngine()
        ce.set_budget("session:abc", 0.5)
        assert ce.can_afford("perplexity__perplexity_search", "abc") is False
        assert ce.can_afford("perplexity__perplexity_search", "xyz") is True  # Different session

    def test_record_cost(self):
        ce = CostEngine()
        ce.set_budget("global", 100)
        cost = ce.record_cost("perplexity__perplexity_search")
        assert cost == 1.0
        assert ce.total_spent_cents == 1.0
        assert ce.total_calls == 1
        budget = ce.get_budget("global")
        assert budget.spent_cents == 1.0

    def test_record_cost_session(self):
        ce = CostEngine()
        ce.set_budget("session:abc", 100)
        ce.record_cost("perplexity__perplexity_search", "abc")
        budget = ce.get_budget("session:abc")
        assert budget.spent_cents == 1.0

    def test_cascade(self):
        ce = CostEngine()
        ce.set_cascade("expensive_tool", [
            CascadeOption("free_tool", 0),
            CascadeOption("cheap_tool", 0.5),
        ])
        assert ce.get_cascade("expensive_tool") is not None
        alt = ce.find_affordable_alternative("expensive_tool")
        assert alt == "free_tool"

    def test_cascade_none(self):
        ce = CostEngine()
        assert ce.find_affordable_alternative("no_cascade") is None

    def test_cascade_all_over_budget(self):
        ce = CostEngine()
        ce.set_budget("global", 0)
        ce.set_cascade("tool", [CascadeOption("alt", 1.0)])
        # Budget is 0 but alt costs 1 cent — can't afford
        # Actually budget 0 means no enforcement (budget_cents=0, check: spent + cost > budget > 0)
        # So this actually passes because budget_cents=0 disables enforcement
        alt = ce.find_affordable_alternative("tool")
        assert alt == "alt"  # Budget 0 = no enforcement

    def test_cascade_all_truly_over_budget(self):
        """find_affordable_alternative returns None when all options exceed budget (line 211)."""
        ce = CostEngine()
        ce.set_budget("global", 0.1)  # Very small budget > 0 to enable enforcement
        ce.set_tool_cost("alt1", 5.0)
        ce.set_tool_cost("alt2", 10.0)
        ce.set_cascade("expensive", [
            CascadeOption("alt1", 5.0),
            CascadeOption("alt2", 10.0),
        ])
        alt = ce.find_affordable_alternative("expensive")
        assert alt is None  # All alternatives too expensive

    def test_get_stats(self):
        ce = CostEngine()
        ce.set_budget("global", 100)
        ce.record_cost("gemini__gemini-search")
        stats = ce.get_stats()
        assert stats["total_calls"] == 1
        assert "global" in stats["budgets"]

    def test_reset_budget(self):
        ce = CostEngine()
        ce.set_budget("global", 100)
        ce.record_cost("perplexity__perplexity_search")
        assert ce.reset_budget("global") is True
        budget = ce.get_budget("global")
        assert budget.spent_cents == 0.0

    def test_reset_nonexistent(self):
        ce = CostEngine()
        assert ce.reset_budget("nope") is False

    def test_get_budget_none(self):
        ce = CostEngine()
        assert ce.get_budget("nope") is None


# ===========================================================================
# ToolFilter Tests
# ===========================================================================

class TestDetectProjectType:
    def test_python_project(self, tmp_path):
        (tmp_path / "main.py").write_text("x=1")
        (tmp_path / "utils.py").write_text("y=2")
        assert detect_project_type(str(tmp_path)) == "python"

    def test_typescript_project(self, tmp_path):
        (tmp_path / "index.ts").write_text("")
        (tmp_path / "app.tsx").write_text("")
        assert detect_project_type(str(tmp_path)) == "typescript"

    def test_web_project(self, tmp_path):
        (tmp_path / "index.html").write_text("")
        (tmp_path / "style.css").write_text("")
        (tmp_path / "page.astro").write_text("")
        assert detect_project_type(str(tmp_path)) == "web"

    def test_empty_dir(self, tmp_path):
        assert detect_project_type(str(tmp_path)) is None

    def test_nonexistent_dir(self):
        assert detect_project_type("/nonexistent/path") is None

    def test_empty_string(self):
        assert detect_project_type("") is None

    def test_nested_files(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "app.py").write_text("")
        (sub / "utils.py").write_text("")
        assert detect_project_type(str(tmp_path)) == "python"

    def test_hidden_dirs_skipped(self, tmp_path):
        hidden = tmp_path / ".git"
        hidden.mkdir()
        (hidden / "config.py").write_text("")
        assert detect_project_type(str(tmp_path)) is None  # .git skipped

    def test_permission_error_top_level(self, tmp_path):
        """PermissionError on top-level iterdir returns None (lines 108-109)."""
        from unittest.mock import patch, PropertyMock

        # Create a path that raises PermissionError on iterdir
        with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            with patch.object(Path, "is_dir", return_value=True):
                result = detect_project_type(str(tmp_path))
        assert result is None

    def test_permission_error_nested_dir(self, tmp_path):
        """PermissionError on nested dir iterdir is caught (lines 106-107)."""
        import os
        # Create a subdirectory and make it unreadable
        sub = tmp_path / "restricted"
        sub.mkdir()
        (sub / "app.py").write_text("x=1")  # Would count as python if readable
        (tmp_path / "main.py").write_text("x=1")

        # Make sub unreadable
        os.chmod(sub, 0o000)
        try:
            result = detect_project_type(str(tmp_path))
            # Should still detect python from main.py, ignoring the restricted dir
            assert result == "python"
        finally:
            # Restore permissions for cleanup
            os.chmod(sub, 0o755)


class TestGetRelevantServers:
    def test_python(self):
        servers = get_relevant_servers("python")
        assert "context7" in servers
        assert "github" in servers

    def test_unknown(self):
        assert get_relevant_servers("cobol") == []

    def test_none(self):
        assert get_relevant_servers(None) == []


class TestToolFilter:
    def test_record_and_get_top(self):
        f = ToolFilter()
        f.record_usage("github__search")
        f.record_usage("github__search")
        f.record_usage("context7__query")
        top = f.get_top_tools(2)
        assert top[0] == "github__search"

    def test_filter_ranks_relevant(self, tmp_path):
        (tmp_path / "main.py").write_text("")
        f = ToolFilter()
        tools = [
            {"name": "context7__query", "description": "Query docs"},
            {"name": "playwright__click", "description": "Click"},
            {"name": "github__search", "description": "Search"},
        ]
        sorted_tools = f.filter_tools(tools, str(tmp_path))
        # context7 and github are python-relevant, playwright is not
        names = [t["name"] for t in sorted_tools]
        assert names.index("context7__query") < names.index("playwright__click")

    def test_filter_ranks_frequent(self):
        f = ToolFilter()
        f.record_usage("b__tool")
        f.record_usage("b__tool")
        f.record_usage("a__tool")
        tools = [
            {"name": "a__tool", "description": "A"},
            {"name": "b__tool", "description": "B"},
        ]
        sorted_tools = f.filter_tools(tools)
        assert sorted_tools[0]["name"] == "b__tool"  # More frequent

    def test_stats(self):
        f = ToolFilter()
        f.record_usage("a")
        stats = f.get_stats()
        assert stats["total_unique_tools_used"] == 1


# ===========================================================================
# LifecycleManager Tests
# ===========================================================================

class TestLifecycleManager:
    def test_initial_state(self):
        lm = LifecycleManager()
        assert lm.is_started("github") is False
        assert lm.needs_start("github") is True

    def test_record_start(self):
        lm = LifecycleManager()
        lm.record_start("github")
        assert lm.is_started("github") is True
        assert lm.needs_start("github") is False

    def test_record_stop(self):
        lm = LifecycleManager()
        lm.record_start("github")
        lm.record_stop("github")
        assert lm.is_started("github") is False

    def test_always_on(self):
        lm = LifecycleManager()
        lm.mark_always_on("slm")
        assert lm.is_always_on("slm") is True
        assert lm.is_always_on("github") is False

    def test_idle_servers(self):
        lm = LifecycleManager(idle_shutdown_seconds=0)
        lm.record_start("github")
        time.sleep(0.01)
        idle = lm.get_idle_servers()
        assert "github" in idle

    def test_always_on_never_idle(self):
        lm = LifecycleManager(idle_shutdown_seconds=0)
        lm.mark_always_on("slm")
        lm.record_start("slm")
        time.sleep(0.01)
        assert "slm" not in lm.get_idle_servers()

    def test_record_call_resets_idle(self):
        lm = LifecycleManager(idle_shutdown_seconds=100)
        lm.record_start("github")
        lm.record_call("github")
        assert lm.get_idle_servers() == []

    def test_status(self):
        lm = LifecycleManager()
        lm.record_start("github")
        lm.mark_always_on("slm")
        lm.record_start("slm")
        status = lm.get_status()
        assert status["started_count"] == 2
        assert status["always_on_count"] == 1
        assert status["servers"]["slm"]["always_on"] is True
        assert status["servers"]["slm"]["will_shutdown_in"] is None
        assert status["servers"]["github"]["always_on"] is False

    def test_record_stop_not_started(self):
        lm = LifecycleManager()
        lm.record_stop("never_started")  # No error


# ===========================================================================
# LearningEngine Tests
# ===========================================================================

class TestToolCallRecord:
    def test_immutable(self):
        rec = ToolCallRecord("s", "github", "search", 100, True, 0.0, time.time())
        with pytest.raises(AttributeError):
            rec.tool_name = "changed"  # type: ignore


class TestLearningEngine:
    def test_record_and_frequency(self):
        le = LearningEngine()
        le.record("s1", "github", "search", 50, True)
        le.record("s1", "github", "search", 60, True)
        le.record("s1", "context7", "query", 30, True)
        ranking = le.get_frequency_ranking(2)
        assert ranking[0][0] == "github__search"
        assert ranking[0][1] == 2

    def test_success_rates(self):
        le = LearningEngine()
        le.record("s1", "github", "search", 50, True)
        le.record("s1", "github", "search", 50, False)
        rates = le.get_success_rates()
        assert rates["github__search"] == 0.5

    def test_slow_tools(self):
        le = LearningEngine(slow_threshold_ms=100)
        le.record("s1", "gemini", "deep-research", 15000, True)
        le.record("s1", "gemini", "deep-research", 20000, True)
        le.record("s1", "github", "search", 50, True)
        slow = le.get_slow_tools()
        assert len(slow) == 1
        assert slow[0][0] == "gemini__deep-research"

    def test_chain_detection(self):
        le = LearningEngine()
        # Simulate alternating pattern: context7 → gemini repeated
        for _ in range(4):
            le.record("s1", "context7", "query", 50, True)
            le.record("s1", "gemini", "search", 100, True)
        chains = le.detect_chains("s1", min_count=3)
        assert len(chains) > 0
        chain_pairs = [(a, b) for a, b, _ in chains]
        assert ("context7__query", "gemini__search") in chain_pairs

    def test_chain_no_results(self):
        le = LearningEngine()
        assert le.detect_chains("empty_session") == []

    def test_stats(self):
        le = LearningEngine()
        le.record("s1", "a", "b", 50, True)
        stats = le.get_stats()
        assert stats["total_records"] == 1
        assert stats["unique_tools"] == 1

    def test_max_records_trimmed(self):
        le = LearningEngine()
        le._max_records = 10
        for i in range(15):
            le.record("s1", "s", f"t{i}", 50, True)
        assert len(le._records) <= 10

    def test_record_with_cost(self):
        le = LearningEngine()
        le.record("s1", "perplexity", "search", 200, True, cost_cents=1.0)
        assert le._records[0].cost_cents == 1.0

    def test_no_slow_tools(self):
        le = LearningEngine()
        le.record("s1", "fast", "tool", 10, True)
        assert le.get_slow_tools() == []
