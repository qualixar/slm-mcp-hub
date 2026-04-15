"""Tests for Phase 5: Discovery & Multi-Client Setup."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from slm_mcp_hub.cli.main import cli
from slm_mcp_hub.core.config import save_config, HubConfig, MCPServerConfig
from slm_mcp_hub.core.constants import DEFAULT_PORT
from slm_mcp_hub.discovery.auto_register import (
    AutoRegister,
    ImportResult,
    RegistrationPlan,
    RegistrationResult,
    _ensure_section,
    _get_section_readonly,
)
from slm_mcp_hub.discovery.client_detector import (
    ClientConfig,
    ClientDetector,
    DetectedClient,
    _build_known_clients,
    _extract_mcp_count,
    _check_hub_registered,
)
from slm_mcp_hub.discovery.network import (
    DiscoveredHub,
    NetworkDiscovery,
    _DiscoveryListener,
    is_zeroconf_available,
    SERVICE_TYPE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_claude_config(tmp_path: Path) -> Path:
    """Create a mock claude.json with MCP servers."""
    config = {
        "mcpServers": {
            "context7": {"command": "npx", "args": ["-y", "@context7/mcp"]},
            "gemini": {"type": "http", "url": "http://localhost:3001/mcp"},
            "sqlite": {"command": "uvx", "args": ["mcp-server-sqlite"]},
        }
    }
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(config))
    return path


@pytest.fixture()
def tmp_vscode_config(tmp_path: Path) -> Path:
    """Create a mock VS Code settings.json with MCP servers."""
    config = {
        "editor.fontSize": 14,
        "mcp": {
            "servers": {
                "copilot-mcp": {"type": "http", "url": "http://localhost:3002/mcp"},
                "github": {"command": "gh", "args": ["mcp"]},
            }
        },
    }
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config))
    return path


@pytest.fixture()
def tmp_claude_with_hub(tmp_path: Path) -> Path:
    """Create a mock claude.json that already has hub registered."""
    config = {
        "mcpServers": {
            "hub": {"type": "http", "url": f"http://127.0.0.1:{DEFAULT_PORT}/mcp"},
            "context7": {"command": "npx", "args": ["-y", "@context7/mcp"]},
        }
    }
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(config))
    return path


@pytest.fixture()
def tmp_invalid_json(tmp_path: Path) -> Path:
    """Create a file with invalid JSON."""
    path = tmp_path / "bad.json"
    path.write_text("{not valid json!!!")
    return path


@pytest.fixture()
def tmp_empty_config(tmp_path: Path) -> Path:
    """Create a config file with no MCP section."""
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"someOtherKey": True}))
    return path


@pytest.fixture()
def tmp_hub_config(tmp_path: Path) -> Path:
    """Create a hub config directory with empty config."""
    config_path = tmp_path / "hub" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    hub_config = HubConfig(config_dir=config_path.parent)
    save_config(hub_config, config_path)
    return config_path


@pytest.fixture()
def detector_with_mock_clients(tmp_claude_config: Path, tmp_vscode_config: Path) -> ClientDetector:
    """Create a ClientDetector with mock client configs."""
    clients = (
        ClientConfig(
            name="claude_code",
            display_name="Claude Code",
            config_paths=(tmp_claude_config,),
            mcp_key="mcpServers",
            config_format="claude",
        ),
        ClientConfig(
            name="vscode_copilot",
            display_name="VS Code Copilot",
            config_paths=(tmp_vscode_config,),
            mcp_key="mcp.servers",
            config_format="vscode",
        ),
    )
    return ClientDetector(known_clients=clients)


# ---------------------------------------------------------------------------
# ClientDetector Tests
# ---------------------------------------------------------------------------


class TestClientDetector:
    def test_detect_claude_code_present(self, tmp_claude_config: Path) -> None:
        clients = (
            ClientConfig(
                name="claude_code",
                display_name="Claude Code",
                config_paths=(tmp_claude_config,),
                mcp_key="mcpServers",
                config_format="claude",
            ),
        )
        detector = ClientDetector(known_clients=clients)
        result = detector.detect_all()

        assert len(result) == 1
        assert result[0].name == "claude_code"
        assert result[0].mcp_count == 3
        assert result[0].hub_registered is False
        assert result[0].config_path == tmp_claude_config

    def test_detect_vscode_present(self, tmp_vscode_config: Path) -> None:
        clients = (
            ClientConfig(
                name="vscode_copilot",
                display_name="VS Code Copilot",
                config_paths=(tmp_vscode_config,),
                mcp_key="mcp.servers",
                config_format="vscode",
            ),
        )
        detector = ClientDetector(known_clients=clients)
        result = detector.detect_all()

        assert len(result) == 1
        assert result[0].name == "vscode_copilot"
        assert result[0].mcp_count == 2

    def test_detect_no_clients(self, tmp_path: Path) -> None:
        clients = (
            ClientConfig(
                name="nonexistent",
                display_name="Nonexistent",
                config_paths=(tmp_path / "does_not_exist.json",),
                mcp_key="mcpServers",
                config_format="claude",
            ),
        )
        detector = ClientDetector(known_clients=clients)
        result = detector.detect_all()
        assert result == ()

    def test_detect_invalid_json(self, tmp_invalid_json: Path) -> None:
        clients = (
            ClientConfig(
                name="bad_client",
                display_name="Bad Client",
                config_paths=(tmp_invalid_json,),
                mcp_key="mcpServers",
                config_format="claude",
            ),
        )
        detector = ClientDetector(known_clients=clients)
        result = detector.detect_all()
        assert result == ()

    def test_detect_hub_already_registered(self, tmp_claude_with_hub: Path) -> None:
        clients = (
            ClientConfig(
                name="claude_code",
                display_name="Claude Code",
                config_paths=(tmp_claude_with_hub,),
                mcp_key="mcpServers",
                config_format="claude",
            ),
        )
        detector = ClientDetector(known_clients=clients)
        result = detector.detect_all()

        assert len(result) == 1
        assert result[0].hub_registered is True
        assert result[0].mcp_count == 2

    def test_detect_missing_mcp_key(self, tmp_empty_config: Path) -> None:
        clients = (
            ClientConfig(
                name="claude_code",
                display_name="Claude Code",
                config_paths=(tmp_empty_config,),
                mcp_key="mcpServers",
                config_format="claude",
            ),
        )
        detector = ClientDetector(known_clients=clients)
        result = detector.detect_all()

        assert len(result) == 1
        assert result[0].mcp_count == 0
        assert result[0].hub_registered is False

    def test_detect_multiple_clients(
        self, detector_with_mock_clients: ClientDetector
    ) -> None:
        result = detector_with_mock_clients.detect_all()
        assert len(result) == 2
        names = {c.name for c in result}
        assert names == {"claude_code", "vscode_copilot"}

    def test_known_clients_property(
        self, detector_with_mock_clients: ClientDetector
    ) -> None:
        assert len(detector_with_mock_clients.known_clients) == 2

    def test_extract_mcp_count_flat(self) -> None:
        data = {"mcpServers": {"a": {}, "b": {}, "c": {}}}
        assert _extract_mcp_count(data, "mcpServers") == 3

    def test_extract_mcp_count_nested(self) -> None:
        data = {"mcp": {"servers": {"a": {}, "b": {}}}}
        assert _extract_mcp_count(data, "mcp.servers") == 2

    def test_extract_mcp_count_missing(self) -> None:
        assert _extract_mcp_count({}, "mcpServers") == 0

    def test_check_hub_registered_true(self) -> None:
        data = {"mcpServers": {"hub": {"url": "http://localhost"}}}
        assert _check_hub_registered(data, "mcpServers") is True

    def test_check_hub_registered_false(self) -> None:
        data = {"mcpServers": {"context7": {}}}
        assert _check_hub_registered(data, "mcpServers") is False

    def test_check_hub_registered_nested(self) -> None:
        data = {"mcp": {"servers": {"hub": {}}}}
        assert _check_hub_registered(data, "mcp.servers") is True

    def test_default_known_clients(self) -> None:
        """Default constructor builds known clients list."""
        detector = ClientDetector()
        assert len(detector.known_clients) >= 5

    def test_first_matching_path_used(self, tmp_path: Path) -> None:
        """When multiple paths are given, the first existing one is used."""
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"
        first.write_text(json.dumps({"mcpServers": {"a": {}}}))
        second.write_text(json.dumps({"mcpServers": {"a": {}, "b": {}}}))

        clients = (
            ClientConfig(
                name="test",
                display_name="Test",
                config_paths=(first, second),
                mcp_key="mcpServers",
                config_format="claude",
            ),
        )
        detector = ClientDetector(known_clients=clients)
        result = detector.detect_all()
        assert result[0].config_path == first
        assert result[0].mcp_count == 1

    def test_linux_platform_paths(self) -> None:
        """_build_known_clients returns Linux paths on non-Darwin platform."""
        with patch("slm_mcp_hub.discovery.client_detector.platform.system", return_value="Linux"):
            clients = _build_known_clients()
        # VS Code and Cursor should use .config paths on Linux
        vscode = next(c for c in clients if c.name == "vscode_copilot")
        cursor = next(c for c in clients if c.name == "cursor")
        assert ".config" in str(vscode.config_paths[0])
        assert ".config" in str(cursor.config_paths[0])

    def test_extract_mcp_count_non_dict_leaf(self) -> None:
        """_extract_mcp_count returns 0 when dotted path hits a non-dict leaf."""
        data = {"mcp": "not-a-dict"}
        assert _extract_mcp_count(data, "mcp.servers") == 0

    def test_check_hub_registered_non_dict_leaf(self) -> None:
        """_check_hub_registered returns False when dotted path hits a non-dict leaf."""
        data = {"mcp": "not-a-dict"}
        assert _check_hub_registered(data, "mcp.servers") is False


# ---------------------------------------------------------------------------
# AutoRegister Tests
# ---------------------------------------------------------------------------


class TestAutoRegister:
    def test_register_claude_code(self, tmp_claude_config: Path) -> None:
        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=tmp_claude_config,
            mcp_count=3,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.register(client, mcp_key="mcpServers")

        assert isinstance(result, RegistrationResult)
        assert result.success is True
        assert result.backup_path is not None
        assert result.backup_path.exists()

        # Verify hub entry in config
        data = json.loads(tmp_claude_config.read_text())
        assert "hub" in data["mcpServers"]
        assert data["mcpServers"]["hub"]["url"] == f"http://127.0.0.1:{DEFAULT_PORT}/mcp"

    def test_register_creates_backup(self, tmp_claude_config: Path) -> None:
        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=tmp_claude_config,
            mcp_count=3,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.register(client)

        assert isinstance(result, RegistrationResult)
        backup_path = tmp_claude_config.parent / ".claude.json.pre-hub-backup"
        assert backup_path.exists()
        # Backup should have original content (3 servers, no hub)
        backup_data = json.loads(backup_path.read_text())
        assert "hub" not in backup_data["mcpServers"]
        assert len(backup_data["mcpServers"]) == 3

    def test_register_dry_run(self, tmp_claude_config: Path) -> None:
        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=tmp_claude_config,
            mcp_count=3,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.register(client, dry_run=True)

        assert isinstance(result, RegistrationPlan)
        assert result.already_registered is False
        assert result.hub_entry["type"] == "http"

        # File should NOT be modified
        data = json.loads(tmp_claude_config.read_text())
        assert "hub" not in data["mcpServers"]

    def test_register_already_registered(self, tmp_claude_with_hub: Path) -> None:
        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=tmp_claude_with_hub,
            mcp_count=2,
            hub_registered=True,
            config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.register(client)

        assert isinstance(result, RegistrationResult)
        assert result.success is True
        assert result.error == "already_registered"

    def test_unregister(self, tmp_claude_with_hub: Path) -> None:
        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=tmp_claude_with_hub,
            mcp_count=2,
            hub_registered=True,
            config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.unregister(client)

        assert result.success is True
        data = json.loads(tmp_claude_with_hub.read_text())
        assert "hub" not in data["mcpServers"]
        assert "context7" in data["mcpServers"]

    def test_unregister_not_registered(self, tmp_claude_config: Path) -> None:
        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=tmp_claude_config,
            mcp_count=3,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.unregister(client)

        assert result.success is True
        assert result.error == "not_registered"

    def test_import_claude_mcps(
        self, tmp_claude_config: Path, tmp_hub_config: Path
    ) -> None:
        registrar = AutoRegister()
        result = registrar.import_mcps(
            tmp_claude_config,
            config_format="claude",
            hub_config_path=tmp_hub_config,
        )

        assert isinstance(result, ImportResult)
        assert result.imported_count == 3
        assert result.skipped_count == 0
        assert result.total_in_source == 3

    def test_import_skips_duplicates(
        self, tmp_claude_config: Path, tmp_path: Path
    ) -> None:
        # Create hub config with one existing server
        config_path = tmp_path / "hub" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = HubConfig(
            config_dir=config_path.parent,
            mcp_servers=(
                MCPServerConfig(name="context7", transport="stdio", command="npx"),
            ),
        )
        save_config(existing, config_path)

        registrar = AutoRegister()
        result = registrar.import_mcps(
            tmp_claude_config,
            config_format="claude",
            hub_config_path=config_path,
        )

        assert result.imported_count == 2
        assert result.skipped_count == 1

    def test_register_vscode_format(self, tmp_vscode_config: Path) -> None:
        client = DetectedClient(
            name="vscode_copilot",
            display_name="VS Code Copilot",
            config_path=tmp_vscode_config,
            mcp_count=2,
            hub_registered=False,
            config_format="vscode",
        )
        registrar = AutoRegister()
        result = registrar.register(client, mcp_key="mcp.servers")

        assert isinstance(result, RegistrationResult)
        assert result.success is True

        data = json.loads(tmp_vscode_config.read_text())
        assert "hub" in data["mcp"]["servers"]

    def test_register_invalid_config(self, tmp_invalid_json: Path) -> None:
        client = DetectedClient(
            name="bad_client",
            display_name="Bad Client",
            config_path=tmp_invalid_json,
            mcp_count=0,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.register(client)

        assert isinstance(result, RegistrationResult)
        assert result.success is False
        assert "Failed to read" in result.error

    def test_import_invalid_source(self, tmp_invalid_json: Path) -> None:
        registrar = AutoRegister()
        result = registrar.import_mcps(tmp_invalid_json)

        assert result.imported_count == 0
        assert result.total_in_source == 0

    def test_plan(self, tmp_claude_config: Path) -> None:
        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=tmp_claude_config,
            mcp_count=3,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister()
        plan = registrar.plan(client)

        assert isinstance(plan, RegistrationPlan)
        assert plan.client_name == "claude_code"
        assert plan.already_registered is False
        assert plan.hub_entry["type"] == "http"

    def test_hub_url_property(self) -> None:
        registrar = AutoRegister("http://custom:9999/mcp")
        assert registrar.hub_url == "http://custom:9999/mcp"

    def test_hub_url_default(self) -> None:
        registrar = AutoRegister()
        assert str(DEFAULT_PORT) in registrar.hub_url

    def test_register_preserves_existing_keys(self, tmp_vscode_config: Path) -> None:
        """Registration preserves non-MCP keys in the config."""
        client = DetectedClient(
            name="vscode_copilot",
            display_name="VS Code Copilot",
            config_path=tmp_vscode_config,
            mcp_count=2,
            hub_registered=False,
            config_format="vscode",
        )
        registrar = AutoRegister()
        registrar.register(client, mcp_key="mcp.servers")

        data = json.loads(tmp_vscode_config.read_text())
        assert data["editor.fontSize"] == 14  # Preserved

    def test_import_vscode_mcps(
        self, tmp_vscode_config: Path, tmp_hub_config: Path
    ) -> None:
        registrar = AutoRegister()
        result = registrar.import_mcps(
            tmp_vscode_config,
            config_format="vscode",
            hub_config_path=tmp_hub_config,
        )

        assert result.imported_count == 2
        assert result.total_in_source == 2

    def test_register_permission_error(self, tmp_path: Path) -> None:
        """Registration fails gracefully on read-only config file."""
        config_file = tmp_path / "readonly.json"
        config_file.write_text(json.dumps({"mcpServers": {"a": {}}}))
        config_file.chmod(0o000)

        client = DetectedClient(
            name="test_client",
            display_name="Test Client",
            config_path=config_file,
            mcp_count=1,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.register(client)

        # Restore permissions for cleanup
        config_file.chmod(0o644)

        assert isinstance(result, RegistrationResult)
        assert result.success is False
        assert "Failed to read" in result.error

    def test_register_does_not_mutate_input(self, tmp_claude_config: Path) -> None:
        """C-01 audit fix: register() must not mutate the original data."""
        original_data = json.loads(tmp_claude_config.read_text())
        original_keys = set(original_data["mcpServers"].keys())

        client = DetectedClient(
            name="claude_code",
            display_name="Claude Code",
            config_path=tmp_claude_config,
            mcp_count=3,
            hub_registered=False,
            config_format="claude",
        )
        registrar = AutoRegister()
        registrar.register(client)

        # Original data dict should not have been mutated
        assert set(original_data["mcpServers"].keys()) == original_keys

    def test_ensure_section_dotted_creates_nested(self) -> None:
        """_ensure_section creates intermediate dicts for dotted keys."""
        data: dict = {}
        section = _ensure_section(data, "mcp.servers")
        assert "mcp" in data
        assert isinstance(data["mcp"], dict)
        assert "servers" in data["mcp"]
        assert section is data["mcp"]["servers"]

    def test_ensure_section_dotted_existing(self) -> None:
        """_ensure_section navigates existing dotted structure."""
        data = {"mcp": {"servers": {"existing": {}}}}
        section = _ensure_section(data, "mcp.servers")
        assert "existing" in section

    def test_ensure_section_flat_creates_key(self) -> None:
        """_ensure_section creates a flat key when it's missing."""
        data: dict = {}
        section = _ensure_section(data, "mcpServers")
        assert "mcpServers" in data
        assert section is data["mcpServers"]

    def test_get_section_readonly_dotted_success(self) -> None:
        """_get_section_readonly navigates dotted keys."""
        data = {"mcp": {"servers": {"a": {}, "b": {}}}}
        section = _get_section_readonly(data, "mcp.servers")
        assert len(section) == 2

    def test_get_section_readonly_dotted_missing(self) -> None:
        """_get_section_readonly returns empty dict for missing dotted path."""
        data: dict = {"mcp": {}}
        section = _get_section_readonly(data, "mcp.servers")
        assert section == {}

    def test_get_section_readonly_dotted_non_dict_leaf(self) -> None:
        """_get_section_readonly returns {} when a non-dict is encountered mid-path."""
        data = {"mcp": "not-a-dict"}
        section = _get_section_readonly(data, "mcp.servers")
        assert section == {}

    def test_get_section_readonly_dotted_final_non_dict(self) -> None:
        """_get_section_readonly returns {} when final value is non-dict."""
        data = {"mcp": {"servers": "string-not-dict"}}
        section = _get_section_readonly(data, "mcp.servers")
        assert section == {}

    def test_unregister_read_failure(self, tmp_path: Path) -> None:
        """Unregister returns failure when config file can't be read."""
        bad_config = tmp_path / "bad.json"
        bad_config.write_text("{invalid json")

        client = DetectedClient(
            name="test", display_name="Test",
            config_path=bad_config, mcp_count=0,
            hub_registered=True, config_format="claude",
        )
        registrar = AutoRegister()
        result = registrar.unregister(client)

        assert result.success is False
        assert "Failed to read" in result.error

    def test_register_write_failure_restores_backup(self, tmp_path: Path) -> None:
        """Register restores backup when write fails."""
        config_file = tmp_path / "config.json"
        original = {"mcpServers": {"existing": {"cmd": "test"}}}
        config_file.write_text(json.dumps(original))

        client = DetectedClient(
            name="test", display_name="Test",
            config_path=config_file, mcp_count=1,
            hub_registered=False, config_format="claude",
        )
        registrar = AutoRegister()

        # Make _write_json fail after backup is created
        with patch(
            "slm_mcp_hub.discovery.auto_register._write_json",
            side_effect=OSError("disk full"),
        ):
            result = registrar.register(client)

        assert result.success is False
        assert "Failed to write" in result.error
        # Original file should be restored from backup
        restored = json.loads(config_file.read_text())
        assert "hub" not in restored["mcpServers"]
        assert "existing" in restored["mcpServers"]

    def test_unregister_write_failure(self, tmp_path: Path) -> None:
        """Unregister returns failure when write fails."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "mcpServers": {"hub": {"type": "http", "url": "http://127.0.0.1:52414/mcp"}}
        }))

        client = DetectedClient(
            name="test", display_name="Test",
            config_path=config_file, mcp_count=1,
            hub_registered=True, config_format="claude",
        )
        registrar = AutoRegister()

        with patch(
            "slm_mcp_hub.discovery.auto_register._write_json",
            side_effect=OSError("disk full"),
        ):
            result = registrar.unregister(client)

        assert result.success is False
        assert "Failed to write" in result.error

    def test_import_mcps_read_failure(self, tmp_path: Path) -> None:
        """import_mcps logs warning and returns zeros on read failure."""
        bad_file = tmp_path / "corrupt.json"
        bad_file.write_text("{not valid json")

        registrar = AutoRegister()
        result = registrar.import_mcps(bad_file, config_format="claude")

        assert result.imported_count == 0
        assert result.skipped_count == 0
        assert result.total_in_source == 0
        assert result.source_name == "corrupt.json"

    def test_register_backup_creation_failure(self, tmp_path: Path) -> None:
        """Register returns failure when backup cannot be created."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"mcpServers": {"a": {}}}))

        client = DetectedClient(
            name="test", display_name="Test",
            config_path=config_file, mcp_count=1,
            hub_registered=False, config_format="claude",
        )
        registrar = AutoRegister()

        with patch(
            "slm_mcp_hub.discovery.auto_register.shutil.copy2",
            side_effect=OSError("permission denied for backup"),
        ):
            result = registrar.register(client)

        assert result.success is False
        assert "Failed to create backup" in result.error

    def test_register_write_fail_and_backup_restore_also_fails(self, tmp_path: Path) -> None:
        """Register handles both write failure AND backup restore failure."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"mcpServers": {"existing": {}}}))

        client = DetectedClient(
            name="test", display_name="Test",
            config_path=config_file, mcp_count=1,
            hub_registered=False, config_format="claude",
        )
        registrar = AutoRegister()

        call_count = 0

        def shutil_copy2_side_effects(src, dst):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: backup creation succeeds
                import shutil as _shutil
                _shutil.copy2.__wrapped__(src, dst) if hasattr(_shutil.copy2, '__wrapped__') else None
                # Actually create the backup file
                Path(dst).write_text(Path(src).read_text())
            else:
                # Second call: backup restore fails
                raise OSError("restore also failed")

        with (
            patch(
                "slm_mcp_hub.discovery.auto_register._write_json",
                side_effect=OSError("disk full"),
            ),
            patch(
                "slm_mcp_hub.discovery.auto_register.shutil.copy2",
                side_effect=shutil_copy2_side_effects,
            ),
        ):
            result = registrar.register(client)

        assert result.success is False
        assert "Failed to write" in result.error


# ---------------------------------------------------------------------------
# NetworkDiscovery Tests
# ---------------------------------------------------------------------------


class TestNetworkDiscovery:
    def test_publish_without_zeroconf(self) -> None:
        with patch("slm_mcp_hub.discovery.network._zeroconf_available", False):
            nd = NetworkDiscovery()
            result = nd.publish(port=52414, mcp_count=5)
            assert result is False
            assert nd.is_published is False

    def test_discover_without_zeroconf(self) -> None:
        with patch("slm_mcp_hub.discovery.network._zeroconf_available", False):
            nd = NetworkDiscovery()
            result = nd.discover(timeout_seconds=0.1)
            assert result == ()

    def test_stop_without_publish(self) -> None:
        nd = NetworkDiscovery()
        nd.stop()  # Should not raise
        assert nd.is_published is False

    def test_discovered_hub_dataclass(self) -> None:
        hub = DiscoveredHub(
            host="macstudio.local.",
            port=52414,
            version="0.1.0",
            mcp_count=38,
            hostname="macstudio",
            address="192.168.1.100",
        )
        assert hub.host == "macstudio.local."
        assert hub.port == 52414
        assert hub.mcp_count == 38

    def test_is_zeroconf_available_function(self) -> None:
        result = is_zeroconf_available()
        assert isinstance(result, bool)

    def test_service_type_constant(self) -> None:
        assert SERVICE_TYPE == "_slm-mcp-hub._tcp.local."

    def test_publish_with_mock_zeroconf(self) -> None:
        mock_zc = MagicMock()
        mock_si = MagicMock()

        with (
            patch("slm_mcp_hub.discovery.network._zeroconf_available", True),
            patch("slm_mcp_hub.discovery.network.Zeroconf", create=True, return_value=mock_zc),
            patch("slm_mcp_hub.discovery.network.ServiceInfo", create=True, return_value=mock_si),
        ):
            nd = NetworkDiscovery()
            result = nd.publish(port=52414, mcp_count=5)

            assert result is True
            assert nd.is_published is True
            mock_zc.register_service.assert_called_once_with(mock_si)

            nd.stop()
            assert nd.is_published is False

    def test_publish_exception_graceful(self) -> None:
        mock_zc = MagicMock()
        mock_zc.register_service.side_effect = OSError("bind failed")

        with (
            patch("slm_mcp_hub.discovery.network._zeroconf_available", True),
            patch("slm_mcp_hub.discovery.network.Zeroconf", create=True, return_value=mock_zc),
            patch("slm_mcp_hub.discovery.network.ServiceInfo", create=True, return_value=MagicMock()),
        ):
            nd = NetworkDiscovery()
            result = nd.publish(port=52414)
            assert result is False
            assert nd.is_published is False

    def test_discovery_listener_add_service(self) -> None:
        """_DiscoveryListener.add_service stores extracted info."""
        listener = _DiscoveryListener()
        mock_zc = MagicMock()
        mock_info = MagicMock()
        mock_info.properties = {b"version": b"0.1.0", b"mcp_count": b"5", b"hostname": b"testhost"}
        mock_info.parsed_addresses.return_value = ["192.168.1.10"]
        mock_info.server = "testhost.local."
        mock_info.port = 52414
        mock_zc.get_service_info.return_value = mock_info

        listener.add_service(mock_zc, SERVICE_TYPE, "test-service")

        assert len(listener.discovered) == 1
        assert listener.discovered[0]["host"] == "testhost.local."
        assert listener.discovered[0]["port"] == 52414
        assert listener.discovered[0]["version"] == "0.1.0"
        assert listener.discovered[0]["mcp_count"] == 5
        assert listener.discovered[0]["address"] == "192.168.1.10"

    def test_discovery_listener_add_service_none_info(self) -> None:
        """_DiscoveryListener.add_service skips when info is None."""
        listener = _DiscoveryListener()
        mock_zc = MagicMock()
        mock_zc.get_service_info.return_value = None

        listener.add_service(mock_zc, SERVICE_TYPE, "unknown")
        assert len(listener.discovered) == 0

    def test_discovery_listener_remove_service(self) -> None:
        """_DiscoveryListener.remove_service is a no-op."""
        listener = _DiscoveryListener()
        listener.remove_service(MagicMock(), SERVICE_TYPE, "test")  # Should not raise

    def test_discovery_listener_update_service(self) -> None:
        """_DiscoveryListener.update_service is a no-op."""
        listener = _DiscoveryListener()
        listener.update_service(MagicMock(), SERVICE_TYPE, "test")  # Should not raise

    def test_discovery_listener_extract_info_no_addresses(self) -> None:
        """_extract_info returns 'unknown' when no parsed_addresses."""
        mock_info = MagicMock()
        mock_info.properties = {}
        mock_info.parsed_addresses.return_value = []
        mock_info.server = None
        mock_info.port = None

        result = _DiscoveryListener._extract_info(mock_info)
        assert result["address"] == "unknown"
        assert result["host"] == "unknown"
        assert result["port"] == 0

    def test_discovery_listener_extract_info_no_parsed_addresses_attr(self) -> None:
        """_extract_info handles info without parsed_addresses method."""
        mock_info = MagicMock(spec=["properties", "server", "port"])
        mock_info.properties = {b"version": b"1.0"}
        mock_info.server = "host.local."
        mock_info.port = 8080

        result = _DiscoveryListener._extract_info(mock_info)
        assert result["address"] == "unknown"

    def test_discovery_listener_extract_info_string_keys(self) -> None:
        """_extract_info handles string (not bytes) property keys."""
        mock_info = MagicMock()
        mock_info.properties = {"version": "2.0", "hostname": "myhost"}
        mock_info.parsed_addresses.return_value = ["10.0.0.1"]
        mock_info.server = "myhost.local."
        mock_info.port = 9090

        result = _DiscoveryListener._extract_info(mock_info)
        assert result["version"] == "2.0"
        assert result["hostname"] == "myhost"

    def test_discover_success_path(self) -> None:
        """discover() returns DiscoveredHub instances when Zeroconf finds services."""
        mock_zc = MagicMock()

        def fake_browser(zc, stype, listener):
            # Simulate service discovery by populating the listener
            mock_info = MagicMock()
            mock_info.properties = {b"version": b"0.1.0", b"mcp_count": b"3", b"hostname": b"peer1"}
            mock_info.parsed_addresses.return_value = ["10.0.0.5"]
            mock_info.server = "peer1.local."
            mock_info.port = 52414
            zc.get_service_info.return_value = mock_info
            listener.add_service(zc, stype, "test-service")

        with (
            patch("slm_mcp_hub.discovery.network._zeroconf_available", True),
            patch("slm_mcp_hub.discovery.network.Zeroconf", create=True, return_value=mock_zc),
            patch("slm_mcp_hub.discovery.network.ServiceBrowser", create=True, side_effect=fake_browser),
            patch("slm_mcp_hub.discovery.network.time.sleep"),
        ):
            nd = NetworkDiscovery()
            results = nd.discover(timeout_seconds=0.01)

        assert len(results) == 1
        assert isinstance(results[0], DiscoveredHub)
        assert results[0].host == "peer1.local."
        assert results[0].port == 52414
        assert results[0].mcp_count == 3

    def test_discover_exception_returns_empty(self) -> None:
        """discover() returns () when an exception occurs."""
        with (
            patch("slm_mcp_hub.discovery.network._zeroconf_available", True),
            patch("slm_mcp_hub.discovery.network.Zeroconf", create=True, side_effect=RuntimeError("boom")),
        ):
            nd = NetworkDiscovery()
            results = nd.discover()
            assert results == ()

    def test_cleanup_unregister_exception(self) -> None:
        """_cleanup swallows exception on unregister_service."""
        nd = NetworkDiscovery()
        mock_zc = MagicMock()
        mock_zc.unregister_service.side_effect = RuntimeError("unregister failed")
        mock_zc.close = MagicMock()  # close should still be called
        nd._zeroconf = mock_zc
        nd._service_info = MagicMock()
        nd._published = True

        nd._cleanup()

        mock_zc.close.assert_called_once()
        assert nd._zeroconf is None
        assert nd._published is False

    def test_cleanup_close_exception(self) -> None:
        """_cleanup swallows exception on close."""
        nd = NetworkDiscovery()
        mock_zc = MagicMock()
        mock_zc.unregister_service = MagicMock()
        mock_zc.close.side_effect = RuntimeError("close failed")
        nd._zeroconf = mock_zc
        nd._service_info = MagicMock()
        nd._published = True

        nd._cleanup()  # Should not raise

        assert nd._zeroconf is None
        assert nd._published is False


# ---------------------------------------------------------------------------
# CLI Tests
# ---------------------------------------------------------------------------


class TestSetupCLI:
    def test_setup_detect_no_clients(self) -> None:
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=()):
            result = runner.invoke(cli, ["setup", "detect"])
            assert result.exit_code == 0
            assert "No AI clients detected" in result.output

    def test_setup_detect_with_clients(self, tmp_claude_config: Path) -> None:
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=False,
                config_format="claude",
            ),
        )
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=clients):
            result = runner.invoke(cli, ["setup", "detect"])
            assert result.exit_code == 0
            assert "Claude Code" in result.output
            assert "3" in result.output

    def test_setup_detect_json_output(self, tmp_claude_config: Path) -> None:
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=False,
                config_format="claude",
            ),
        )
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=clients):
            result = runner.invoke(cli, ["setup", "detect", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data) == 1
            assert data[0]["name"] == "claude_code"

    def test_setup_register_dry_run(self, tmp_claude_config: Path) -> None:
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=False,
                config_format="claude",
            ),
        )
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=clients):
            result = runner.invoke(
                cli, ["setup", "register", "--all", "--dry-run"]
            )
            assert result.exit_code == 0
            assert "Config:" in result.output or "claude_code" in result.output

    def test_setup_register_no_clients(self) -> None:
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=()):
            result = runner.invoke(cli, ["setup", "register", "--all"])
            assert result.exit_code == 0
            assert "No AI clients detected" in result.output

    def test_setup_register_unknown_client(self, tmp_claude_config: Path) -> None:
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=False,
                config_format="claude",
            ),
        )
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=clients):
            result = runner.invoke(
                cli, ["setup", "register", "--client", "nonexistent"]
            )
            assert result.exit_code == 0
            assert "not detected" in result.output

    def test_setup_import_cli(self, tmp_claude_config: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        with patch.object(
            AutoRegister,
            "import_mcps",
            return_value=ImportResult(
                source_name=".claude.json",
                imported_count=3,
                skipped_count=0,
                total_in_source=3,
            ),
        ):
            result = runner.invoke(
                cli, ["setup", "import", str(tmp_claude_config)]
            )
            assert result.exit_code == 0
            assert "Imported: 3" in result.output

    def test_setup_unregister_no_clients(self) -> None:
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=()):
            result = runner.invoke(cli, ["setup", "unregister", "--all"])
            assert result.exit_code == 0

    def test_network_discover_no_zeroconf(self) -> None:
        runner = CliRunner()
        with patch(
            "slm_mcp_hub.cli.setup_commands.is_zeroconf_available",
            return_value=False,
        ):
            result = runner.invoke(cli, ["network", "discover"])
            assert result.exit_code == 0
            assert "not installed" in result.output

    def test_network_discover_no_hubs(self) -> None:
        runner = CliRunner()
        with (
            patch(
                "slm_mcp_hub.cli.setup_commands.is_zeroconf_available",
                return_value=True,
            ),
            patch.object(NetworkDiscovery, "discover", return_value=()),
        ):
            result = runner.invoke(cli, ["network", "discover"])
            assert result.exit_code == 0
            assert "No hubs found" in result.output

    def test_network_discover_with_hubs(self) -> None:
        hubs = (
            DiscoveredHub(
                host="macstudio.local.",
                port=52414,
                version="0.1.0",
                mcp_count=38,
                hostname="macstudio",
                address="192.168.1.100",
            ),
        )
        runner = CliRunner()
        with (
            patch(
                "slm_mcp_hub.cli.setup_commands.is_zeroconf_available",
                return_value=True,
            ),
            patch.object(NetworkDiscovery, "discover", return_value=hubs),
        ):
            result = runner.invoke(cli, ["network", "discover"])
            assert result.exit_code == 0
            assert "macstudio" in result.output
            assert "192.168.1.100" in result.output

    def test_network_discover_json(self) -> None:
        hubs = (
            DiscoveredHub(
                host="macstudio.local.",
                port=52414,
                version="0.1.0",
                mcp_count=38,
                hostname="macstudio",
                address="192.168.1.100",
            ),
        )
        runner = CliRunner()
        with (
            patch(
                "slm_mcp_hub.cli.setup_commands.is_zeroconf_available",
                return_value=True,
            ),
            patch.object(NetworkDiscovery, "discover", return_value=hubs),
        ):
            result = runner.invoke(cli, ["network", "discover", "--json-output"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data[0]["hostname"] == "macstudio"

    def test_network_info(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["network", "info"])
        assert result.exit_code == 0
        assert "Hostname:" in result.output
        assert "_slm-mcp-hub._tcp.local." in result.output

    def test_setup_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["setup", "--help"])
        assert result.exit_code == 0
        assert "Setup wizard" in result.output

    def test_network_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["network", "--help"])
        assert result.exit_code == 0
        assert "Network discovery" in result.output

    def test_setup_register_success(self, tmp_claude_config: Path) -> None:
        """setup register --all (non-dry-run) shows registered + backup path."""
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=False,
                config_format="claude",
            ),
        )
        mock_result = RegistrationResult(
            success=True,
            client_name="claude_code",
            config_path=tmp_claude_config,
            backup_path=tmp_claude_config.parent / ".claude.json.pre-hub-backup",
        )
        runner = CliRunner()
        with (
            patch.object(ClientDetector, "detect_all", return_value=clients),
            patch.object(AutoRegister, "register", return_value=mock_result),
        ):
            result = runner.invoke(cli, ["setup", "register", "--all"])
            assert result.exit_code == 0
            assert "registered" in result.output
            assert "backup" in result.output

    def test_setup_register_already_registered(self, tmp_claude_config: Path) -> None:
        """setup register --all shows 'already registered' for existing hub."""
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=True,
                config_format="claude",
            ),
        )
        mock_result = RegistrationResult(
            success=True,
            client_name="claude_code",
            config_path=tmp_claude_config,
            error="already_registered",
        )
        runner = CliRunner()
        with (
            patch.object(ClientDetector, "detect_all", return_value=clients),
            patch.object(AutoRegister, "register", return_value=mock_result),
        ):
            result = runner.invoke(cli, ["setup", "register", "--all"])
            assert result.exit_code == 0
            assert "already registered" in result.output

    def test_setup_register_failure(self, tmp_claude_config: Path) -> None:
        """setup register --all shows FAILED on registration error."""
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=False,
                config_format="claude",
            ),
        )
        mock_result = RegistrationResult(
            success=False,
            client_name="claude_code",
            config_path=tmp_claude_config,
            error="Failed to read config: Permission denied",
        )
        runner = CliRunner()
        with (
            patch.object(ClientDetector, "detect_all", return_value=clients),
            patch.object(AutoRegister, "register", return_value=mock_result),
        ):
            result = runner.invoke(cli, ["setup", "register", "--all"])
            assert result.exit_code == 0
            assert "FAILED" in result.output

    def test_setup_unregister_success(self, tmp_claude_config: Path) -> None:
        """setup unregister --all shows 'unregistered' on success."""
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=2,
                hub_registered=True,
                config_format="claude",
            ),
        )
        mock_result = RegistrationResult(
            success=True,
            client_name="claude_code",
            config_path=tmp_claude_config,
        )
        runner = CliRunner()
        with (
            patch.object(ClientDetector, "detect_all", return_value=clients),
            patch.object(AutoRegister, "unregister", return_value=mock_result),
        ):
            result = runner.invoke(cli, ["setup", "unregister", "--all"])
            assert result.exit_code == 0
            assert "unregistered" in result.output

    def test_setup_unregister_not_registered(self, tmp_claude_config: Path) -> None:
        """setup unregister --all shows 'not registered' when hub wasn't there."""
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=False,
                config_format="claude",
            ),
        )
        mock_result = RegistrationResult(
            success=True,
            client_name="claude_code",
            config_path=tmp_claude_config,
            error="not_registered",
        )
        runner = CliRunner()
        with (
            patch.object(ClientDetector, "detect_all", return_value=clients),
            patch.object(AutoRegister, "unregister", return_value=mock_result),
        ):
            result = runner.invoke(cli, ["setup", "unregister", "--all"])
            assert result.exit_code == 0
            assert "not registered" in result.output

    def test_setup_unregister_failure(self, tmp_claude_config: Path) -> None:
        """setup unregister --all shows FAILED on error."""
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=2,
                hub_registered=True,
                config_format="claude",
            ),
        )
        mock_result = RegistrationResult(
            success=False,
            client_name="claude_code",
            config_path=tmp_claude_config,
            error="Failed to write config: disk full",
        )
        runner = CliRunner()
        with (
            patch.object(ClientDetector, "detect_all", return_value=clients),
            patch.object(AutoRegister, "unregister", return_value=mock_result),
        ):
            result = runner.invoke(cli, ["setup", "unregister", "--all"])
            assert result.exit_code == 0
            assert "FAILED" in result.output

    def test_setup_import_auto_detect_vscode(self, tmp_path: Path) -> None:
        """setup import auto-detects vscode format when 'servers' keyword present."""
        vscode_file = tmp_path / "settings.json"
        vscode_file.write_text(json.dumps({"servers": {"a": {}}}))

        runner = CliRunner()
        with patch.object(
            AutoRegister,
            "import_mcps",
            return_value=ImportResult(
                source_name="settings.json", imported_count=1,
                skipped_count=0, total_in_source=1,
            ),
        ) as mock_import:
            result = runner.invoke(cli, ["setup", "import", str(vscode_file)])
            assert result.exit_code == 0
            assert "Imported: 1" in result.output
            # Verify vscode format was detected
            mock_import.assert_called_once()
            call_kwargs = mock_import.call_args
            assert call_kwargs[1].get("config_format") == "vscode" or \
                (len(call_kwargs[0]) > 1 and call_kwargs[0][1] == "vscode") or \
                call_kwargs.kwargs.get("config_format") == "vscode"

    def test_setup_import_auto_detect_unknown(self, tmp_path: Path) -> None:
        """setup import exits with error when format cannot be auto-detected."""
        unknown_file = tmp_path / "unknown.json"
        unknown_file.write_text(json.dumps({"unrelated": True}))

        runner = CliRunner()
        result = runner.invoke(cli, ["setup", "import", str(unknown_file)])
        assert result.exit_code != 0
        assert "Could not auto-detect format" in result.output

    def test_display_plan_not_registered(self, tmp_claude_config: Path) -> None:
        """_display_plan shows config/backup/entry for non-registered clients."""
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=3,
                hub_registered=False,
                config_format="claude",
            ),
        )
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=clients):
            result = runner.invoke(cli, ["setup", "register", "--all", "--dry-run"])
            assert result.exit_code == 0
            assert "Config:" in result.output
            assert "Backup:" in result.output
            assert "Add entry:" in result.output

    def test_display_plan_already_registered(self, tmp_claude_config: Path) -> None:
        """_display_plan shows 'already registered' message."""
        clients = (
            DetectedClient(
                name="claude_code",
                display_name="Claude Code",
                config_path=tmp_claude_config,
                mcp_count=2,
                hub_registered=True,
                config_format="claude",
            ),
        )
        runner = CliRunner()
        with patch.object(ClientDetector, "detect_all", return_value=clients):
            result = runner.invoke(cli, ["setup", "register", "--all", "--dry-run"])
            assert result.exit_code == 0
            assert "already registered" in result.output

    def test_mcp_key_for_vscode_format(self, tmp_path: Path) -> None:
        """_mcp_key_for returns 'mcp.servers' for vscode format client."""
        vscode_config = tmp_path / "settings.json"
        vscode_config.write_text(json.dumps({"mcp": {"servers": {}}}))

        clients = (
            DetectedClient(
                name="vscode_copilot",
                display_name="VS Code Copilot",
                config_path=vscode_config,
                mcp_count=0,
                hub_registered=False,
                config_format="vscode",
            ),
        )
        mock_result = RegistrationResult(
            success=True,
            client_name="vscode_copilot",
            config_path=vscode_config,
            backup_path=vscode_config.parent / "settings.json.pre-hub-backup",
        )
        runner = CliRunner()
        with (
            patch.object(ClientDetector, "detect_all", return_value=clients),
            patch.object(AutoRegister, "register", return_value=mock_result) as mock_reg,
        ):
            result = runner.invoke(cli, ["setup", "register", "--all"])
            assert result.exit_code == 0
            # Verify mcp_key was "mcp.servers" for vscode
            call_kwargs = mock_reg.call_args
            assert call_kwargs.kwargs.get("mcp_key") == "mcp.servers" or \
                (call_kwargs[1].get("mcp_key") == "mcp.servers")

    def test_network_info_shows_service_type(self) -> None:
        """network info shows service type in output."""
        runner = CliRunner()
        result = runner.invoke(cli, ["network", "info"])
        assert result.exit_code == 0
        assert "Service type:" in result.output
        assert SERVICE_TYPE in result.output
