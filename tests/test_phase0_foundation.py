"""Phase 0 Foundation Tests — TDD RED phase.

Tests for: config, storage, hub orchestrator, plugin discovery.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from slm_mcp_hub.core.config import (
    HubConfig,
    MCPServerConfig,
    generate_default_config,
    import_claude_config,
    import_vscode_config,
    load_config,
    save_config,
)
from slm_mcp_hub.core.constants import DEFAULT_PORT, NAMESPACE_DELIMITER, VERSION
from slm_mcp_hub.core.hub import HubOrchestrator, HubState, get_hub, reset_hub
from slm_mcp_hub.storage.database import HubDatabase
from slm_mcp_hub.storage.schema import SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path):
    """Temporary directory for test configs and databases."""
    return tmp_path


@pytest.fixture
def sample_claude_json(tmp_dir):
    """Create a sample claude.json file."""
    data = {
        "mcpServers": {
            "github": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            },
            "context7": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp"],
            },
            "tavily": {
                "type": "http",
                "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=test123",
            },
        }
    }
    path = tmp_dir / "claude.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def sample_vscode_json(tmp_dir):
    """Create a sample VS Code mcp.json file."""
    data = {
        "servers": {
            "github": {
                "url": "https://api.githubcopilot.com/mcp/",
            },
            "perplexity": {
                "command": "npx",
                "args": ["-y", "server-perplexity-ask"],
                "env": {"API_KEY": "${env:PERPLEXITY_API_KEY}"},
            },
        }
    }
    path = tmp_dir / "mcp.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def sample_hub_config(tmp_dir):
    """Create a sample hub config JSON."""
    data = {
        "host": "127.0.0.1",
        "port": 9999,
        "mcpServers": {
            "test-server": {
                "command": "echo",
                "args": ["hello"],
            }
        },
        "log_level": "DEBUG",
    }
    path = tmp_dir / "config.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture(autouse=True)
def cleanup_hub():
    """Reset hub singleton after each test."""
    yield
    reset_hub()


# ---------------------------------------------------------------------------
# Constants Tests
# ---------------------------------------------------------------------------

class TestConstants:
    def test_version_set(self):
        assert VERSION == "0.1.2"

    def test_default_port(self):
        assert DEFAULT_PORT == 52414

    def test_namespace_delimiter(self):
        assert NAMESPACE_DELIMITER == "__"


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_config_load_default(self, tmp_dir):
        """Default config generated when no file exists."""
        config = load_config(tmp_dir / "nonexistent.json")
        assert config.port == DEFAULT_PORT
        assert config.host == "127.0.0.1"
        assert len(config.mcp_servers) == 0

    def test_config_load_custom(self, sample_hub_config):
        """Custom config loaded correctly."""
        config = load_config(sample_hub_config)
        assert config.port == 9999
        assert config.log_level == "DEBUG"
        assert len(config.mcp_servers) == 1
        assert config.mcp_servers[0].name == "test-server"

    def test_config_import_claude(self, sample_claude_json):
        """Import from claude.json format."""
        servers = import_claude_config(sample_claude_json)
        assert len(servers) == 3
        names = {s.name for s in servers}
        assert names == {"github", "context7", "tavily"}

        github = next(s for s in servers if s.name == "github")
        assert github.transport == "stdio"
        assert github.command == "npx"

        tavily = next(s for s in servers if s.name == "tavily")
        assert tavily.transport == "http"
        assert "tavily" in tavily.url

    def test_config_import_vscode(self, sample_vscode_json):
        """Import from VS Code mcp.json format."""
        servers = import_vscode_config(sample_vscode_json)
        assert len(servers) == 2
        names = {s.name for s in servers}
        assert names == {"github", "perplexity"}

    def test_config_env_override(self, tmp_dir, monkeypatch):
        """SLM_HUB_PORT overrides config port."""
        monkeypatch.setenv("SLM_HUB_PORT", "12345")
        config = load_config(tmp_dir / "nonexistent.json")
        assert config.port == 12345

    def test_config_save_and_reload(self, tmp_dir):
        """Config round-trips through save/load."""
        server = MCPServerConfig(
            name="test", transport="stdio", command="echo", args=("hello",)
        )
        config = HubConfig(port=8080, mcp_servers=(server,))
        path = tmp_dir / "test-config.json"
        save_config(config, path)

        reloaded = load_config(path)
        assert reloaded.port == 8080
        assert len(reloaded.mcp_servers) == 1
        assert reloaded.mcp_servers[0].name == "test"

    def test_config_immutable(self):
        """HubConfig is frozen (immutable)."""
        config = HubConfig()
        with pytest.raises(AttributeError):
            config.port = 9999  # type: ignore[misc]

    def test_generate_default_config(self, tmp_dir):
        """Generate and save default config."""
        path = tmp_dir / "default.json"
        config = generate_default_config(path)
        assert path.exists()
        assert config.port == DEFAULT_PORT

    def test_env_var_resolution(self, sample_claude_json, monkeypatch):
        """${VAR} placeholders resolved from environment."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
        servers = import_claude_config(sample_claude_json)
        github = next(s for s in servers if s.name == "github")
        assert github.env.get("GITHUB_TOKEN") == "ghp_test123"

    def test_save_config_with_env_dict(self, tmp_dir):
        """Round-trip save/load with srv.env set (line 213)."""
        server = MCPServerConfig(
            name="github", transport="stdio", command="npx",
            args=("-y", "@mcp/server-github"),
            env={"GITHUB_TOKEN": "abc123"},
        )
        config = HubConfig(mcp_servers=(server,))
        path = tmp_dir / "env-config.json"
        save_config(config, path)

        raw = json.loads(path.read_text())
        assert raw["mcpServers"]["github"]["env"] == {"GITHUB_TOKEN": "abc123"}

        reloaded = load_config(path)
        assert reloaded.mcp_servers[0].env == {"GITHUB_TOKEN": "abc123"}

    def test_save_config_always_on(self, tmp_dir):
        """Round-trip save/load with always_on=True (line 220)."""
        server = MCPServerConfig(
            name="slm", transport="stdio", command="slm",
            always_on=True,
        )
        config = HubConfig(mcp_servers=(server,))
        path = tmp_dir / "always-on.json"
        save_config(config, path)

        raw = json.loads(path.read_text())
        assert raw["mcpServers"]["slm"]["always_on"] is True

    def test_save_config_no_cache(self, tmp_dir):
        """Round-trip save/load with no_cache=True (line 222)."""
        server = MCPServerConfig(
            name="live-srv", transport="stdio", command="echo",
            no_cache=True,
        )
        config = HubConfig(mcp_servers=(server,))
        path = tmp_dir / "no-cache.json"
        save_config(config, path)

        raw = json.loads(path.read_text())
        assert raw["mcpServers"]["live-srv"]["no_cache"] is True

    def test_save_config_cost_per_call(self, tmp_dir):
        """Round-trip save/load with cost_per_call_cents > 0 (line 224)."""
        server = MCPServerConfig(
            name="perplexity", transport="stdio", command="npx",
            cost_per_call_cents=1.5,
        )
        config = HubConfig(mcp_servers=(server,))
        path = tmp_dir / "cost.json"
        save_config(config, path)

        raw = json.loads(path.read_text())
        assert raw["mcpServers"]["perplexity"]["cost_per_call_cents"] == 1.5

    def test_save_config_http_with_headers(self, tmp_dir):
        """Round-trip save/load with HTTP server and headers (line 218/224)."""
        server = MCPServerConfig(
            name="http-srv", transport="http",
            url="https://api.example.com/mcp",
            headers={"Authorization": "Bearer tok123"},
        )
        config = HubConfig(mcp_servers=(server,))
        path = tmp_dir / "http-headers.json"
        save_config(config, path)

        raw = json.loads(path.read_text())
        assert raw["mcpServers"]["http-srv"]["headers"] == {"Authorization": "Bearer tok123"}
        assert raw["mcpServers"]["http-srv"]["url"] == "https://api.example.com/mcp"


# ---------------------------------------------------------------------------
# Database Tests
# ---------------------------------------------------------------------------

class TestDatabase:
    def test_database_create(self, tmp_dir):
        """Database created with all tables."""
        db = HubDatabase(tmp_dir / "test.db")
        db.open()
        try:
            assert db.table_exists("hub_config")
            assert db.table_exists("mcp_servers")
            assert db.table_exists("sessions")
            assert db.table_exists("tool_calls")
            assert db.table_exists("cache")
            assert db.table_exists("cost_budgets")
            assert db.table_exists("audit_log")
            assert db.table_exists("metrics")
            assert db.table_exists("schema_version")
        finally:
            db.close()

    def test_database_migration(self, tmp_dir):
        """Schema migration runs and records version."""
        db = HubDatabase(tmp_dir / "test.db")
        db.open()
        try:
            row = db.fetch_one("SELECT MAX(version) as v FROM schema_version")
            assert row is not None
            assert row["v"] == SCHEMA_VERSION
        finally:
            db.close()

    def test_database_insert_and_fetch(self, tmp_dir):
        """Insert and fetch a row."""
        db = HubDatabase(tmp_dir / "test.db")
        db.open()
        try:
            import time
            db.insert("hub_config", {
                "key": "test_key",
                "value": json.dumps({"foo": "bar"}),
                "updated_at": time.time(),
            })
            row = db.fetch_one("SELECT * FROM hub_config WHERE key = ?", ("test_key",))
            assert row is not None
            assert json.loads(row["value"]) == {"foo": "bar"}
        finally:
            db.close()

    def test_database_wal_mode(self, tmp_dir):
        """WAL mode enabled."""
        db = HubDatabase(tmp_dir / "test.db")
        db.open()
        try:
            row = db.fetch_one("PRAGMA journal_mode")
            assert row is not None
            assert row[0] == "wal"
        finally:
            db.close()

    def test_database_reopen_no_duplicate_migration(self, tmp_dir):
        """Reopening database doesn't re-apply migrations."""
        db_path = tmp_dir / "test.db"
        db = HubDatabase(db_path)
        db.open()
        db.close()

        db2 = HubDatabase(db_path)
        db2.open()
        try:
            rows = db2.fetch_all("SELECT * FROM schema_version")
            assert len(rows) == 1  # Only one version entry
        finally:
            db2.close()

    def test_database_connection_not_opened(self, tmp_dir):
        """Accessing connection property before open() raises RuntimeError (line 51)."""
        db = HubDatabase(tmp_dir / "test.db")
        with pytest.raises(RuntimeError, match="Database not opened"):
            _ = db.connection

    def test_database_executemany(self, tmp_dir):
        """executemany inserts multiple rows (line 60)."""
        db = HubDatabase(tmp_dir / "test.db")
        db.open()
        try:
            import time
            params = [
                ("key1", json.dumps({"v": 1}), time.time()),
                ("key2", json.dumps({"v": 2}), time.time()),
            ]
            db.executemany(
                "INSERT INTO hub_config (key, value, updated_at) VALUES (?, ?, ?)",
                params,
            )
            db.commit()
            rows = db.fetch_all("SELECT * FROM hub_config")
            assert len(rows) == 2
        finally:
            db.close()

    def test_database_insert_invalid_table(self, tmp_dir):
        """insert with invalid table name raises ValueError (line 79)."""
        db = HubDatabase(tmp_dir / "test.db")
        db.open()
        try:
            with pytest.raises(ValueError, match="Invalid table name"):
                db.insert("nonexistent_table", {"key": "val"})
        finally:
            db.close()

    def test_database_insert_invalid_column(self, tmp_dir):
        """insert with invalid column name raises ValueError (line 83)."""
        db = HubDatabase(tmp_dir / "test.db")
        db.open()
        try:
            with pytest.raises(ValueError, match="Invalid column name"):
                db.insert("hub_config", {"key; DROP TABLE": "bad"})
        finally:
            db.close()

    def test_database_commit(self, tmp_dir):
        """commit method works (line 93)."""
        db = HubDatabase(tmp_dir / "test.db")
        db.open()
        try:
            import time
            db.execute(
                "INSERT INTO hub_config (key, value, updated_at) VALUES (?, ?, ?)",
                ("manual_key", json.dumps({"x": 1}), time.time()),
            )
            db.commit()
            row = db.fetch_one("SELECT * FROM hub_config WHERE key = ?", ("manual_key",))
            assert row is not None
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Hub Orchestrator Tests
# ---------------------------------------------------------------------------

class TestHubOrchestrator:
    @pytest.mark.asyncio
    async def test_hub_start_stop(self, tmp_dir):
        """Hub starts and stops cleanly."""
        config = HubConfig(config_dir=tmp_dir)
        async with HubOrchestrator(config) as hub:
            assert hub.state == HubState.READY
            assert hub.uptime_seconds > 0
        assert hub.state == HubState.STOPPED

    @pytest.mark.asyncio
    async def test_hub_singleton(self, tmp_dir):
        """Only one hub per process."""
        config = HubConfig(config_dir=tmp_dir)
        async with HubOrchestrator(config):
            with pytest.raises(RuntimeError, match="Only one"):
                HubOrchestrator(config)

    @pytest.mark.asyncio
    async def test_hub_status(self, tmp_dir):
        """Hub status returns correct info."""
        config = HubConfig(config_dir=tmp_dir, port=9999)
        async with HubOrchestrator(config) as hub:
            status = hub.get_status()
            assert status["state"] == "ready"
            assert status["port"] == 9999
            assert status["version"] == VERSION
            assert status["mcp_servers_configured"] == 0

    @pytest.mark.asyncio
    async def test_hub_database_created(self, tmp_dir):
        """Hub creates database on start."""
        config = HubConfig(config_dir=tmp_dir)
        async with HubOrchestrator(config) as hub:
            assert (tmp_dir / "hub.db").exists()
            assert hub.db.table_exists("tool_calls")

    @pytest.mark.asyncio
    async def test_plugin_discovery(self, tmp_dir):
        """Entry points scanned, built-in plugins discovered."""
        config = HubConfig(config_dir=tmp_dir)
        async with HubOrchestrator(config) as hub:
            plugin_names = {p.name for p in hub.plugins}
            assert "slm" in plugin_names
            assert "mesh" in plugin_names

    @pytest.mark.asyncio
    async def test_hub_events(self, tmp_dir):
        """Hub emits lifecycle events."""
        events_received = []
        config = HubConfig(config_dir=tmp_dir)
        hub = HubOrchestrator(config)
        hub.on("hub_ready", lambda: events_received.append("ready"))
        hub.on("hub_stopped", lambda: events_received.append("stopped"))

        await hub.start()
        assert "ready" in events_received

        await hub.stop()
        assert "stopped" in events_received

    def test_hub_uptime_not_started(self, tmp_dir):
        """uptime_seconds returns 0.0 when hub not started (line 57/70)."""
        config = HubConfig(config_dir=tmp_dir)
        hub = HubOrchestrator(config)
        assert hub.uptime_seconds == 0.0

    def test_get_hub_returns_instance(self, tmp_dir):
        """get_hub() returns the instance when hub exists (line 280)."""
        config = HubConfig(config_dir=tmp_dir)
        hub = HubOrchestrator(config)
        result = get_hub()
        assert result is hub

    def test_get_hub_returns_none_after_reset(self):
        """get_hub() returns None when no hub exists."""
        reset_hub()
        assert get_hub() is None
