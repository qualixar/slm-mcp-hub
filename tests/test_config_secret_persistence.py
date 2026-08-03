"""Security regressions for unresolved configuration persistence."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from slm_mcp_hub.core.config import (
    ConfigValidationError,
    MCPServerConfig,
    load_config,
    materialize_server_config,
    parse_mcp_server,
    save_config,
)


@pytest.fixture()
def placeholder_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 52414,
                "mcpServers": {
                    "stdio-secret": {
                        "command": "${HUB_COMMAND}",
                        "args": ["--token", "${HUB_TOKEN}"],
                        "env": {
                            "TOKEN": "${HUB_TOKEN}",
                            "ALT_TOKEN": "${env:HUB_TOKEN}",
                        },
                    },
                    "http-secret": {
                        "type": "http",
                        "url": "https://example.test/${HUB_TOKEN}/mcp",
                        "headers": {
                            "Authorization": "Bearer ${HUB_TOKEN}",
                        },
                    },
                    "third-server": {
                        "command": "echo",
                    },
                },
            }
        )
    )
    return path


def test_load_keeps_placeholders_as_canonical_values(
    placeholder_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUB_COMMAND", "python-secret-command")
    monkeypatch.setenv("HUB_TOKEN", "resolved-sentinel")

    config = load_config(placeholder_config)
    stdio = next(server for server in config.mcp_servers if server.name == "stdio-secret")
    http = next(server for server in config.mcp_servers if server.name == "http-secret")

    assert stdio.command == "${HUB_COMMAND}"
    assert stdio.args == ("--token", "${HUB_TOKEN}")
    assert stdio.env == {
        "TOKEN": "${HUB_TOKEN}",
        "ALT_TOKEN": "${env:HUB_TOKEN}",
    }
    assert http.url == "https://example.test/${HUB_TOKEN}/mcp"
    assert http.headers == {"Authorization": "Bearer ${HUB_TOKEN}"}


def test_materialize_server_config_resolves_only_the_runtime_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUB_COMMAND", "python3")
    monkeypatch.setenv("HUB_TOKEN", "resolved-sentinel")
    canonical = MCPServerConfig(
        name="secret-server",
        transport="stdio",
        command="${HUB_COMMAND}",
        args=("--token=${HUB_TOKEN}",),
        env={"TOKEN": "${env:HUB_TOKEN}"},
        url="https://example.test/${HUB_TOKEN}",
        headers={"Authorization": "Bearer ${HUB_TOKEN}"},
    )

    runtime = materialize_server_config(canonical)

    assert runtime is not canonical
    assert runtime.command == "python3"
    assert runtime.args == ("--token=resolved-sentinel",)
    assert runtime.env == {"TOKEN": "resolved-sentinel"}
    assert runtime.url == "https://example.test/resolved-sentinel"
    assert runtime.headers == {"Authorization": "Bearer resolved-sentinel"}
    assert canonical.command == "${HUB_COMMAND}"
    assert canonical.env == {"TOKEN": "${env:HUB_TOKEN}"}


def test_materialize_leaves_missing_variables_unchanged() -> None:
    canonical = MCPServerConfig(
        name="missing-secret",
        transport="stdio",
        command="echo",
        env={"TOKEN": "${HUB_VARIABLE_THAT_DOES_NOT_EXIST}"},
    )

    runtime = materialize_server_config(canonical)

    assert runtime.env == {"TOKEN": "${HUB_VARIABLE_THAT_DOES_NOT_EXIST}"}


def test_load_save_round_trip_never_persists_resolved_values(
    placeholder_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HUB_COMMAND", "python-secret-command")
    monkeypatch.setenv("HUB_TOKEN", "resolved-sentinel")
    output = tmp_path / "saved.json"

    save_config(load_config(placeholder_config), output, force=True)
    saved = output.read_text()

    assert "resolved-sentinel" not in saved
    assert "python-secret-command" not in saved
    assert "${HUB_COMMAND}" in saved
    assert saved.count("${HUB_TOKEN}") == 4
    assert "${env:HUB_TOKEN}" in saved


def test_snapshot_of_placeholder_config_never_contains_resolved_values(
    placeholder_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HUB_COMMAND", "python-secret-command")
    monkeypatch.setenv("HUB_TOKEN", "resolved-sentinel")
    snapshot_dir = placeholder_config.parent / "snapshots"

    save_config(load_config(placeholder_config), placeholder_config, force=True)

    snapshots = list(snapshot_dir.glob("config-*.json"))
    assert len(snapshots) == 1
    snapshot = snapshots[0].read_text()
    assert "resolved-sentinel" not in snapshot
    assert "python-secret-command" not in snapshot
    assert "${HUB_TOKEN}" in snapshot


def test_config_and_snapshot_files_are_owner_only(placeholder_config: Path) -> None:
    placeholder_config.chmod(0o644)
    snapshot_dir = placeholder_config.parent / "snapshots"
    snapshot_dir.mkdir()
    legacy_snapshot = snapshot_dir / "config-20000101-000000-3mcps.json"
    legacy_snapshot.write_text(placeholder_config.read_text())
    legacy_snapshot.chmod(0o644)

    save_config(load_config(placeholder_config), placeholder_config, force=True)

    assert stat.S_IMODE(placeholder_config.stat().st_mode) == 0o600
    assert all(
        stat.S_IMODE(snapshot.stat().st_mode) == 0o600
        for snapshot in snapshot_dir.glob("config-*.json")
    )


@pytest.mark.parametrize(
    ("raw", "field_name"),
    [
        ({"command": 42}, "command"),
        ({"command": "echo", "args": ["ok", 42]}, "args"),
        ({"command": "echo", "env": {"TOKEN": 42}}, "env"),
        ({"url": 42}, "url"),
        ({"url": "https://example.test", "headers": {"Token": 42}}, "headers"),
        ({"url": "https://example.test", "type": "invalid"}, "transport"),
    ],
)
def test_parse_rejects_non_string_or_invalid_connection_fields(
    raw: dict,
    field_name: str,
) -> None:
    with pytest.raises(ConfigValidationError, match=field_name):
        parse_mcp_server("invalid-server", raw)


def test_programmatic_config_is_validated_before_materialization() -> None:
    invalid = MCPServerConfig(
        name="invalid-programmatic",
        transport="stdio",
        command="echo",
        env={"TOKEN": 42},  # type: ignore[dict-item]
    )

    with pytest.raises(ConfigValidationError, match="env"):
        materialize_server_config(invalid)


@pytest.mark.parametrize(
    "config",
    [
        MCPServerConfig(name="", transport="stdio", command="echo"),
        MCPServerConfig(
            name="invalid-boolean",
            transport="stdio",
            command="echo",
            enabled="yes",  # type: ignore[arg-type]
        ),
        MCPServerConfig(
            name="invalid-cost",
            transport="stdio",
            command="echo",
            cost_per_call_cents=-1,
        ),
    ],
)
def test_programmatic_config_rejects_invalid_metadata(
    config: MCPServerConfig,
) -> None:
    with pytest.raises(ConfigValidationError):
        materialize_server_config(config)


def test_parse_rejects_non_object_server_config() -> None:
    with pytest.raises(ConfigValidationError, match="object"):
        parse_mcp_server("invalid", [])  # type: ignore[arg-type]
