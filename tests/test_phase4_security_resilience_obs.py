"""Phase 4 Tests — Security, Resilience, Observability. 100% target."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from slm_mcp_hub.observability.metrics import MetricsCollector, ServerMetrics
from slm_mcp_hub.observability.tracer import RequestTracer, TraceSpan
from slm_mcp_hub.resilience.watchdog import (
    generate_launchd_plist,
    generate_systemd_unit,
    is_running,
    read_pid_file,
    remove_pid_file,
    write_pid_file,
)
from slm_mcp_hub.security.audit import AuditLogger
from slm_mcp_hub.security.permissions import (
    PermissionAction,
    PermissionEngine,
    PermissionResult,
    PermissionRule,
)
from slm_mcp_hub.storage.database import HubDatabase


# ===========================================================================
# Permission Engine Tests
# ===========================================================================

class TestPermissionRule:
    def test_global_specificity(self):
        r = PermissionRule(scope="global", server="*")
        assert r.specificity == 1

    def test_session_specificity(self):
        r = PermissionRule(scope="session:claude*", server="*")
        assert r.specificity == 2

    def test_project_specificity(self):
        r = PermissionRule(scope="project:*/prod/*", server="*")
        assert r.specificity == 3


class TestPermissionEngine:
    def test_allow_default(self):
        pe = PermissionEngine()
        result = pe.check("github", "search")
        assert result.allowed is True
        assert result.action == PermissionAction.ALLOW

    def test_deny_global(self):
        pe = PermissionEngine([
            PermissionRule(scope="global", server="filesystem", tools=("delete_file",), action=PermissionAction.DENY),
        ])
        result = pe.check("filesystem", "delete_file")
        assert result.allowed is False
        assert "denied" in result.message.lower()

    def test_deny_does_not_affect_other_tools(self):
        pe = PermissionEngine([
            PermissionRule(scope="global", server="filesystem", tools=("delete_file",), action=PermissionAction.DENY),
        ])
        assert pe.check("filesystem", "read_file").allowed is True

    def test_deny_session_scope(self):
        pe = PermissionEngine([
            PermissionRule(scope="session:copilot*", server="sqlite", tools=("*",), action=PermissionAction.DENY),
        ])
        result = pe.check("sqlite", "query", client_name="Copilot Chat")
        assert result.allowed is False

    def test_session_scope_no_match(self):
        pe = PermissionEngine([
            PermissionRule(scope="session:copilot*", server="sqlite", tools=("*",), action=PermissionAction.DENY),
        ])
        result = pe.check("sqlite", "query", client_name="Claude Code")
        assert result.allowed is True  # Rule doesn't match Claude

    def test_deny_project_scope(self):
        pe = PermissionEngine([
            PermissionRule(scope="project:*/production/*", server="*", tools=("*",), action=PermissionAction.DENY),
        ])
        result = pe.check("sqlite", "delete", project_path="/app/production/db")
        assert result.allowed is False

    def test_specificity_override(self):
        pe = PermissionEngine([
            PermissionRule(scope="global", server="sqlite", tools=("*",), action=PermissionAction.DENY),
            PermissionRule(scope="project:*/dev/*", server="sqlite", tools=("*",), action=PermissionAction.ALLOW),
        ])
        # Project-scope allow overrides global deny
        result = pe.check("sqlite", "query", project_path="/app/dev/test")
        assert result.allowed is True

    def test_wildcard_server(self):
        pe = PermissionEngine([
            PermissionRule(scope="global", server="*", tools=("delete_*",), action=PermissionAction.DENY),
        ])
        assert pe.check("filesystem", "delete_file").allowed is False
        assert pe.check("github", "delete_branch").allowed is False

    def test_warn_action(self):
        pe = PermissionEngine([
            PermissionRule(scope="global", server="perplexity", tools=("*",), action=PermissionAction.WARN),
        ])
        result = pe.check("perplexity", "search")
        assert result.allowed is True
        assert result.action == PermissionAction.WARN
        assert "warning" in result.message.lower()

    def test_from_config(self):
        config = [
            {"scope": "global", "server": "filesystem", "tools": ["delete_file"], "action": "deny"},
            {"scope": "session:test*", "server": "*", "tools": ["*"], "action": "allow"},
        ]
        pe = PermissionEngine.from_config(config)
        assert pe.rule_count == 2
        assert pe.check("filesystem", "delete_file").allowed is False

    def test_rule_count(self):
        pe = PermissionEngine()
        assert pe.rule_count == 0
        pe.add_rule(PermissionRule(scope="global", server="*"))
        assert pe.rule_count == 1

    def test_tie_breaking_by_tool_length(self):
        """When two rules have same specificity, longer tool pattern wins (line 99)."""
        pe = PermissionEngine([
            PermissionRule(scope="global", server="*", tools=("*",), action=PermissionAction.ALLOW),
            PermissionRule(scope="global", server="*", tools=("delete_file",), action=PermissionAction.DENY),
        ])
        # "delete_file" is more specific (longer) than "*", so DENY should win
        result = pe.check("filesystem", "delete_file")
        assert result.action == PermissionAction.DENY

    def test_unknown_scope_returns_false(self):
        """Unknown scope pattern returns False from _matches_scope (line 123)."""
        pe = PermissionEngine([
            PermissionRule(scope="unknown:pattern", server="*", tools=("*",), action=PermissionAction.DENY),
        ])
        # Rule with unknown scope should never match
        result = pe.check("github", "search")
        assert result.allowed is True  # Default allow since rule doesn't match

    def test_server_mismatch_continues(self):
        """Rule skipped when server doesn't match (line 90 continue)."""
        pe = PermissionEngine([
            PermissionRule(scope="global", server="filesystem", tools=("*",), action=PermissionAction.DENY),
        ])
        # github != filesystem, so the rule is skipped, default ALLOW
        result = pe.check("github", "search")
        assert result.allowed is True


class TestPermissionResult:
    def test_allowed(self):
        r = PermissionResult(PermissionAction.ALLOW)
        assert r.allowed is True

    def test_denied(self):
        r = PermissionResult(PermissionAction.DENY, message="blocked")
        assert r.allowed is False


# ===========================================================================
# Audit Logger Tests
# ===========================================================================

class TestAuditLogger:
    @pytest.fixture
    def db(self, tmp_path):
        database = HubDatabase(tmp_path / "test.db")
        database.open()
        yield database
        database.close()

    def test_log_and_query(self, db):
        al = AuditLogger(db)
        al.log("session-1", "tool_call", {"tool": "search", "server": "github"})
        entries = al.query()
        assert len(entries) == 1
        assert entries[0]["action"] == "tool_call"
        assert entries[0]["details"]["tool"] == "search"

    def test_query_by_session(self, db):
        al = AuditLogger(db)
        al.log("s1", "tool_call", {"tool": "a"})
        al.log("s2", "tool_call", {"tool": "b"})
        entries = al.query(session_id="s1")
        assert len(entries) == 1
        assert entries[0]["session_id"] == "s1"

    def test_query_by_action(self, db):
        al = AuditLogger(db)
        al.log("s1", "tool_call", {})
        al.log("s1", "permission_denied", {})
        entries = al.query(action="permission_denied")
        assert len(entries) == 1

    def test_query_since(self, db):
        al = AuditLogger(db)
        al.log("s1", "old_event", {})
        entries = al.query(since=time.time() + 100)  # Future = no results
        assert len(entries) == 0

    def test_cleanup(self, db):
        al = AuditLogger(db)
        # Insert an old entry manually
        db.insert("audit_log", {
            "session_id": "old",
            "action": "ancient",
            "details": "{}",
            "timestamp": time.time() - (31 * 86400),  # 31 days ago
        })
        al.log("new", "recent", {})
        deleted = al.cleanup(retention_days=30)
        assert deleted == 1
        remaining = al.query()
        assert len(remaining) == 1
        assert remaining[0]["session_id"] == "new"

    def test_log_no_details(self, db):
        al = AuditLogger(db)
        al.log("s1", "event")
        entries = al.query()
        assert entries[0]["details"] == {}

    def test_log_exception_handling(self, db):
        """AuditLogger.log swallows exceptions (lines 35-36)."""
        al = AuditLogger(db)
        # Close the database so insert will fail
        db.close()
        # Should not raise - fire-and-forget with error logging
        al.log("s1", "action", {"test": True})


# ===========================================================================
# Watchdog Tests
# ===========================================================================

class TestWatchdog:
    def test_generate_launchd_plist(self):
        plist = generate_launchd_plist(52414)
        assert "com.qualixar.slm-mcp-hub" in plist
        assert "52414" in plist
        assert "KeepAlive" in plist
        assert "<?xml" in plist

    def test_generate_systemd_unit(self):
        unit = generate_systemd_unit(52414)
        assert "SLM MCP Hub" in unit
        assert "52414" in unit
        assert "Restart=always" in unit

    def test_pid_file_lifecycle(self, tmp_path, monkeypatch):
        pid_path = tmp_path / "test.pid"
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.PID_FILE", pid_path)
        write_pid_file()
        assert pid_path.exists()
        pid = read_pid_file()
        assert pid == os.getpid()
        remove_pid_file()
        assert not pid_path.exists()

    def test_read_pid_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.PID_FILE", tmp_path / "nope.pid")
        assert read_pid_file() is None

    def test_read_pid_file_invalid(self, tmp_path, monkeypatch):
        pid_path = tmp_path / "bad.pid"
        pid_path.write_text("not_a_number")
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.PID_FILE", pid_path)
        assert read_pid_file() is None

    def test_is_running_no_pid(self, tmp_path, monkeypatch):
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.PID_FILE", tmp_path / "nope.pid")
        assert is_running() is False

    def test_is_running_current_process(self, tmp_path, monkeypatch):
        pid_path = tmp_path / "test.pid"
        pid_path.write_text(str(os.getpid()))
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.PID_FILE", pid_path)
        assert is_running() is True

    def test_is_running_stale_pid(self, tmp_path, monkeypatch):
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("999999999")  # Very unlikely to exist
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.PID_FILE", pid_path)
        assert is_running() is False
        assert not pid_path.exists()  # Stale PID cleaned up

    def test_remove_pid_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("slm_mcp_hub.resilience.watchdog.PID_FILE", tmp_path / "nope.pid")
        remove_pid_file()  # No error

    def test_install_launchd(self, tmp_path, monkeypatch):
        """install_launchd writes plist file and returns path (lines 79-84)."""
        from slm_mcp_hub.resilience.watchdog import install_launchd
        # Override home to use tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        plist_path = install_launchd(port=55555)
        assert plist_path.exists()
        content = plist_path.read_text()
        assert "55555" in content
        assert "com.qualixar.slm-mcp-hub" in content

    def test_find_binary_fallback(self, monkeypatch):
        """_find_binary returns python -m fallback when binary not on PATH (line 130)."""
        from slm_mcp_hub.resilience.watchdog import _find_binary
        # Set PATH to empty so no slm-hub binary is found
        monkeypatch.setenv("PATH", "")
        result = _find_binary()
        assert "slm_mcp_hub.cli.main" in result
        assert "-m" in result


# ===========================================================================
# Request Tracer Tests
# ===========================================================================

class TestRequestTracer:
    def test_start_and_end(self):
        rt = RequestTracer()
        tid = rt.start_trace("s1", "github", "search")
        span = rt.end_trace(tid, success=True, cost_cents=0.5)
        assert span is not None
        assert span.session_id == "s1"
        assert span.server_name == "github"
        assert span.success is True
        assert span.cost_cents == 0.5
        assert span.duration_ms >= 0

    def test_get_trace(self):
        rt = RequestTracer()
        tid = rt.start_trace("s1", "github", "search")
        rt.end_trace(tid, success=True)
        found = rt.get_trace(tid)
        assert found is not None
        assert found.trace_id == tid

    def test_get_nonexistent_trace(self):
        rt = RequestTracer()
        assert rt.get_trace("no-such-id") is None

    def test_end_nonexistent_trace(self):
        rt = RequestTracer()
        assert rt.end_trace("no-such-id", success=False) is None

    def test_get_recent(self):
        rt = RequestTracer()
        for i in range(5):
            tid = rt.start_trace("s1", "github", f"tool{i}")
            rt.end_trace(tid, success=True)
        recent = rt.get_recent(3)
        assert len(recent) == 3
        assert recent[0].tool_name == "tool4"  # Newest first

    def test_ring_buffer_eviction(self):
        rt = RequestTracer(max_traces=3)
        for i in range(5):
            tid = rt.start_trace("s1", "s", f"t{i}")
            rt.end_trace(tid, success=True)
        assert rt.size == 3
        # Oldest (t0, t1) should be gone
        traces = rt.get_recent(10)
        names = [t.tool_name for t in traces]
        assert "t0" not in names
        assert "t4" in names

    def test_cached_trace(self):
        rt = RequestTracer()
        tid = rt.start_trace("s1", "github", "search")
        span = rt.end_trace(tid, success=True, cached=True)
        assert span.cached is True

    def test_stats_empty(self):
        rt = RequestTracer()
        stats = rt.get_stats()
        assert stats["total_traces"] == 0

    def test_stats_with_data(self):
        rt = RequestTracer()
        tid = rt.start_trace("s1", "github", "search")
        rt.end_trace(tid, success=True, cached=True, cost_cents=0.5)
        stats = rt.get_stats()
        assert stats["total_traces"] == 1
        assert stats["success_rate"] == 1.0
        assert stats["cache_rate"] == 1.0


# ===========================================================================
# Metrics Collector Tests
# ===========================================================================

class TestServerMetrics:
    def test_defaults(self):
        m = ServerMetrics()
        assert m.success_rate == 0.0
        assert m.avg_duration_ms == 0.0
        assert m.p95_duration_ms == 0.0
        assert m.cache_hit_rate == 0.0

    def test_p95_calculation(self):
        m = ServerMetrics()
        m.durations = list(range(1, 101))  # 1 to 100
        m.call_count = 100
        # p95 index = int(100 * 0.95) = 95 → value at sorted[95] = 96
        assert m.p95_duration_ms >= 95.0


class TestMetricsCollector:
    def test_record_increments(self):
        mc = MetricsCollector()
        mc.record("github", 50, True)
        mc.record("github", 100, True)
        metrics = mc.get_server_metrics("github")
        assert metrics["call_count"] == 2
        assert metrics["avg_duration_ms"] == 75.0

    def test_success_rate(self):
        mc = MetricsCollector()
        mc.record("github", 50, True)
        mc.record("github", 50, False)
        metrics = mc.get_server_metrics("github")
        assert metrics["success_rate"] == 0.5

    def test_cache_hit_rate(self):
        mc = MetricsCollector()
        mc.record("github", 50, True, cached=True)
        mc.record("github", 50, True, cached=False)
        metrics = mc.get_server_metrics("github")
        assert metrics["cache_hit_rate"] == 0.5

    def test_cost_tracking(self):
        mc = MetricsCollector()
        mc.record("perplexity", 200, True, cost_cents=1.0)
        mc.record("perplexity", 300, True, cost_cents=1.0)
        metrics = mc.get_server_metrics("perplexity")
        assert metrics["total_cost_cents"] == 2.0

    def test_hub_metrics(self):
        mc = MetricsCollector()
        mc.record("github", 50, True, cost_cents=0)
        mc.record("context7", 30, True, cost_cents=0)
        hub = mc.get_hub_metrics()
        assert hub["total_calls"] == 2
        assert hub["active_servers"] == 2
        assert hub["uptime_seconds"] >= 0  # May round to 0.0 if test runs fast

    def test_unknown_server(self):
        mc = MetricsCollector()
        metrics = mc.get_server_metrics("nonexistent")
        assert metrics["call_count"] == 0

    def test_all_server_metrics(self):
        mc = MetricsCollector()
        mc.record("github", 50, True)
        mc.record("context7", 30, True)
        all_metrics = mc.get_all_server_metrics()
        assert len(all_metrics) == 2
        names = {m["server"] for m in all_metrics}
        assert names == {"github", "context7"}

    def test_max_duration(self):
        mc = MetricsCollector()
        mc.record("github", 50, True)
        mc.record("github", 500, True)
        metrics = mc.get_server_metrics("github")
        assert metrics["max_duration_ms"] == 500

    def test_p95_in_metrics(self):
        mc = MetricsCollector()
        for i in range(100):
            mc.record("github", i * 10, True)
        metrics = mc.get_server_metrics("github")
        assert metrics["p95_duration_ms"] > 0

    def test_duration_history_bounded(self):
        mc = MetricsCollector()
        # deque maxlen=1000 bounds the duration history automatically
        for i in range(1100):
            mc.record("github", i, True)
        m = mc._servers["github"]
        assert len(m.durations) <= 1000
