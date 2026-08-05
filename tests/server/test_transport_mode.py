"""W8-P3 tests — transport_mode switch (stateless default, stateful opt-in).

TDD: RED first. Verifies:
1. resolve_stateful: hub_config=None -> False (safe default)
2. resolve_stateful: config.transport_stateful=False/True -> False/True
3. resolve_stateful: env SLM_HUB_STATEFUL=1/true/yes/on -> True (overrides config False)
4. resolve_stateful: env=0/false/no/off -> False (overrides config True)
5. resolve_stateful: env unset -> falls back to config value
6. _build_sdk_asgi integration: session_manager.stateless reflects transport mode
7. config.py round-trip: from_dict, to_dict, env-override rebuild preserve transport_stateful
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from slm_mcp_hub.core.config import HubConfig
from slm_mcp_hub.server.http_server import _build_sdk_asgi
from slm_mcp_hub.server.transport_mode import resolve_stateful

# ---------------------------------------------------------------------------
# Helper: minimal mock SDK server (reused from existing event-store tests)
# ---------------------------------------------------------------------------


def _make_sdk_server() -> MagicMock:
    """Create a minimal mock SDK Server for _build_sdk_asgi integration tests."""
    mock = MagicMock()
    # StreamableHTTPSessionManager calls app.lifespan — mock it out.
    mock.lifespan = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# resolve_stateful — pure function, no imports beyond os
# ---------------------------------------------------------------------------


class TestResolveStatefulNoneConfig:
    def test_none_hub_config_returns_false(self) -> None:
        """resolve_stateful(None) must return False — safest default."""
        assert resolve_stateful(None) is False


class TestResolveStatefulFromConfig:
    def test_config_transport_stateful_false_returns_false(self) -> None:
        """transport_stateful=False (default) resolves to stateful=False."""
        cfg = HubConfig(transport_stateful=False)
        assert resolve_stateful(cfg) is False

    def test_config_transport_stateful_true_returns_true(self) -> None:
        """transport_stateful=True resolves to stateful=True."""
        cfg = HubConfig(transport_stateful=True)
        assert resolve_stateful(cfg) is True

    def test_default_hub_config_is_stateless(self) -> None:
        """Unmodified HubConfig defaults to stateful=False (stateless transport)."""
        assert resolve_stateful(HubConfig()) is False


class TestResolveStatefulEnvOverride:
    """SLM_HUB_STATEFUL env var wins over config when set and non-empty."""

    @pytest.mark.parametrize("env_val", ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON"])
    def test_truthy_env_overrides_false_config(
        self, env_val: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SLM_HUB_STATEFUL={truthy} overrides config transport_stateful=False."""
        monkeypatch.setenv("SLM_HUB_STATEFUL", env_val)
        cfg = HubConfig(transport_stateful=False)
        assert resolve_stateful(cfg) is True

    @pytest.mark.parametrize("env_val", ["0", "false", "False", "FALSE", "no", "NO", "off", "OFF"])
    def test_falsy_env_overrides_true_config(
        self, env_val: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SLM_HUB_STATEFUL={falsy} overrides config transport_stateful=True."""
        monkeypatch.setenv("SLM_HUB_STATEFUL", env_val)
        cfg = HubConfig(transport_stateful=True)
        assert resolve_stateful(cfg) is False

    def test_env_unset_falls_back_to_config_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SLM_HUB_STATEFUL is absent, config decides (False case)."""
        monkeypatch.delenv("SLM_HUB_STATEFUL", raising=False)
        cfg = HubConfig(transport_stateful=False)
        assert resolve_stateful(cfg) is False

    def test_env_unset_falls_back_to_config_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SLM_HUB_STATEFUL is absent, config decides (True case)."""
        monkeypatch.delenv("SLM_HUB_STATEFUL", raising=False)
        cfg = HubConfig(transport_stateful=True)
        assert resolve_stateful(cfg) is True

    def test_empty_env_var_falls_back_to_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SLM_HUB_STATEFUL='' (set-but-empty) is treated as unset — config decides."""
        monkeypatch.setenv("SLM_HUB_STATEFUL", "")
        cfg = HubConfig(transport_stateful=True)
        assert resolve_stateful(cfg) is True

    @pytest.mark.parametrize("env_val", [" ", "   ", "\t", "\n", " \t "])
    def test_whitespace_only_env_falls_back_to_config(
        self, env_val: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SLM_HUB_STATEFUL=whitespace-only is treated as blank — config decides,
        not silently forced to stateless."""
        monkeypatch.setenv("SLM_HUB_STATEFUL", env_val)
        assert resolve_stateful(HubConfig(transport_stateful=True)) is True
        assert resolve_stateful(HubConfig(transport_stateful=False)) is False

    def test_env_unset_with_none_config_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SLM_HUB_STATEFUL absent + hub_config=None -> False."""
        monkeypatch.delenv("SLM_HUB_STATEFUL", raising=False)
        assert resolve_stateful(None) is False


# ---------------------------------------------------------------------------
# _build_sdk_asgi integration — session_manager.stateless reflects transport mode
# ---------------------------------------------------------------------------


class TestBuildSdkAsgiTransportMode:
    def test_stateless_default_stateful_false(self) -> None:
        """transport_stateful=False (default) -> session_manager.stateless is True."""
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(event_store_enabled=True, transport_stateful=False)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.stateless is True

    def test_stateful_true_sets_stateless_false(self) -> None:
        """transport_stateful=True -> session_manager.stateless is False."""
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(event_store_enabled=True, transport_stateful=True)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.stateless is False

    def test_stateful_true_with_event_store_enabled_wires_store(self) -> None:
        """transport_stateful=True + event_store_enabled=True -> event_store is not None."""
        from slm_mcp_hub.streaming.event_store import InMemoryEventStore

        sdk_server = _make_sdk_server()
        hub_config = HubConfig(event_store_enabled=True, transport_stateful=True)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.stateless is False
        assert session_manager.event_store is not None
        assert isinstance(session_manager.event_store, InMemoryEventStore)

    def test_env_override_flips_stateless_to_stateful(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SLM_HUB_STATEFUL=1 flips stateless=True config -> session_manager.stateless=False."""
        monkeypatch.setenv("SLM_HUB_STATEFUL", "1")
        sdk_server = _make_sdk_server()
        # config says False (stateless), env overrides to True (stateful)
        hub_config = HubConfig(event_store_enabled=True, transport_stateful=False)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.stateless is False

    def test_env_override_flips_stateful_to_stateless(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SLM_HUB_STATEFUL=0 flips stateful=True config -> session_manager.stateless=True."""
        monkeypatch.setenv("SLM_HUB_STATEFUL", "0")
        sdk_server = _make_sdk_server()
        # config says True (stateful), env overrides to False (stateless)
        hub_config = HubConfig(event_store_enabled=True, transport_stateful=True)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.stateless is True

    def test_no_hub_config_defaults_to_stateless(self) -> None:
        """_build_sdk_asgi(sdk_server) with no hub_config -> stateless=True (backward compat)."""
        sdk_server = _make_sdk_server()

        _, session_manager = _build_sdk_asgi(sdk_server)

        assert session_manager.stateless is True


# ---------------------------------------------------------------------------
# HubConfig round-trip — from_dict, to_dict, env-override rebuild
# ---------------------------------------------------------------------------


class TestHubConfigTransportStatefulField:
    def test_default_is_false(self) -> None:
        """HubConfig() has transport_stateful=False by default."""
        cfg = HubConfig()
        assert cfg.transport_stateful is False

    def test_explicit_true_stored(self) -> None:
        """HubConfig(transport_stateful=True) stores True."""
        cfg = HubConfig(transport_stateful=True)
        assert cfg.transport_stateful is True

    def test_from_dict_transport_stateful_true(self) -> None:
        """load_config parses 'transport_stateful': True from JSON -> transport_stateful=True."""
        import json
        from pathlib import Path

        from slm_mcp_hub.core.config import load_config

        def _write_load(tmp_dir: Path, stateful: bool) -> HubConfig:
            p = tmp_dir / "cfg.json"
            p.write_text(
                json.dumps({"host": "127.0.0.1", "port": 52414,
                            "servers": {}, "transport_stateful": stateful})
            )
            return load_config(config_path=p)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_load(Path(tmp), True)
        assert cfg.transport_stateful is True

    def test_from_dict_transport_stateful_default_false(self) -> None:
        """load_config without 'transport_stateful' key defaults to False."""
        import json
        import tempfile
        from pathlib import Path

        from slm_mcp_hub.core.config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cfg.json"
            p.write_text(
                json.dumps({"host": "127.0.0.1", "port": 52414, "servers": {}})
            )
            cfg = load_config(config_path=p)
        assert cfg.transport_stateful is False

    def test_to_dict_includes_transport_stateful(self) -> None:
        """save_config writes 'transport_stateful' into the JSON file."""
        import json
        import tempfile
        from pathlib import Path

        from slm_mcp_hub.core.config import save_config
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cfg.json"
            save_config(HubConfig(transport_stateful=False), config_path=p)
            d = json.loads(p.read_text())
        assert "transport_stateful" in d
        assert d["transport_stateful"] is False

    def test_save_load_round_trip_true(self, tmp_path: object) -> None:
        """transport_stateful=True survives save_config -> load_config round trip."""
        from slm_mcp_hub.core.config import load_config, save_config

        assert isinstance(tmp_path, __import__("pathlib").Path)
        config_path = tmp_path / "config.json"
        cfg = HubConfig(transport_stateful=True)
        save_config(cfg, config_path=config_path)
        loaded = load_config(config_path=config_path)
        assert loaded.transport_stateful is True

    def test_save_load_round_trip_false(self, tmp_path: object) -> None:
        """transport_stateful=False survives save_config -> load_config round trip."""
        from slm_mcp_hub.core.config import load_config, save_config

        assert isinstance(tmp_path, __import__("pathlib").Path)
        config_path = tmp_path / "config.json"
        cfg = HubConfig(transport_stateful=False)
        save_config(cfg, config_path=config_path)
        loaded = load_config(config_path=config_path)
        assert loaded.transport_stateful is False

    def test_env_override_rebuild_preserves_transport_stateful_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """transport_stateful survives an env-override rebuild (port changes)."""
        from slm_mcp_hub.core.config import _apply_env_overrides

        monkeypatch.setenv("SLM_HUB_PORT", "9999")
        cfg = HubConfig(transport_stateful=False)
        rebuilt = _apply_env_overrides(cfg)
        assert rebuilt.transport_stateful is False
        assert rebuilt.port == 9999

    def test_env_override_rebuild_preserves_transport_stateful_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """transport_stateful=True survives an env-override rebuild."""
        from slm_mcp_hub.core.config import _apply_env_overrides

        monkeypatch.setenv("SLM_HUB_PORT", "8888")
        cfg = HubConfig(transport_stateful=True)
        rebuilt = _apply_env_overrides(cfg)
        assert rebuilt.transport_stateful is True
        assert rebuilt.port == 8888

    def test_env_override_early_return_preserves_transport_stateful(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no env vars change, _apply_env_overrides returns config as-is (early path)."""
        from slm_mcp_hub.core.config import _apply_env_overrides

        # Remove all env vars that would trigger a rebuild
        for env_key in (
            "SLM_HUB_PORT", "SLM_HUB_HOST", "SLM_HUB_LOG_LEVEL",
            "SLM_HUB_CONFIG_DIR", "SLM_HUB_STARTUP_MAX_CONCURRENCY",
            "SLM_HUB_IDLE_TTL_SECONDS", "SLM_HUB_MAX_LIVE_BACKENDS",
        ):
            monkeypatch.delenv(env_key, raising=False)

        cfg = HubConfig(transport_stateful=True)
        # Must also set config_dir to match what _apply_env_overrides will read
        monkeypatch.setenv("SLM_HUB_CONFIG_DIR", str(cfg.config_dir))
        result = _apply_env_overrides(cfg)
        assert result.transport_stateful is True
