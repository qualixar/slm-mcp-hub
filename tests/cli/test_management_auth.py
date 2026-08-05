"""GitHub #29 regression: CLI management commands must attach the hub API key.

When ``SLM_HUB_API_KEY`` is set, every CLI command that hits a gated
management REST endpoint MUST send the ``X-SLM-Hub-API-Key`` header — otherwise
the hub middleware (``http_server.require_api_key``) returns 401. Plain
``status`` only touches ``/api/health`` (exempt), which masked the bug for
local users while breaking every auth-enabled remote deployment.

TDD: RED — written BEFORE the ``cli.api_client`` helper and the call-site
wiring exist.

Covers all three CLI modules and both verbs:
    * main.py         — ``tools`` (GET), ``reconnect`` (POST)
    * server_commands — ``server list`` (GET), ``server reload`` (POST)
    * observe_commands — ``servers`` (GET), ``warm`` (POST)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from click.testing import CliRunner

from slm_mcp_hub.cli.main import cli

runner = CliRunner()

_KEY_HEADER = "x-slm-hub-api-key"


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


class _Capture:
    """Records every (url, kwargs) an httpx verb is called with."""

    def __init__(self, payload: dict) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._payload = payload

    def __call__(self, url: str, *args: object, **kwargs: object) -> MagicMock:
        self.calls.append((url, dict(kwargs)))
        return _fake_response(self._payload)


# ---------------------------------------------------------------------------
# Unit: hub_headers()
# ---------------------------------------------------------------------------


class TestHubHeaders:
    def test_no_env_returns_empty(self, monkeypatch) -> None:
        monkeypatch.delenv("SLM_HUB_API_KEY", raising=False)
        from slm_mcp_hub.cli.api_client import hub_headers

        assert hub_headers() == {}

    def test_env_set_returns_header(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "s3cr3t")
        from slm_mcp_hub.cli.api_client import hub_headers

        assert hub_headers() == {_KEY_HEADER: "s3cr3t"}

    def test_blank_env_returns_empty(self, monkeypatch) -> None:
        """Whitespace-only key is treated as unset (no useless 401-bait header)."""
        monkeypatch.setenv("SLM_HUB_API_KEY", "   ")
        from slm_mcp_hub.cli.api_client import hub_headers

        assert hub_headers() == {}


# ---------------------------------------------------------------------------
# Integration: each gated command attaches the header when the key is set,
# and omits it (backward-compat loopback) when it is not.
# ---------------------------------------------------------------------------


class TestMainCommandsAuth:
    def test_tools_sends_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        cap = _Capture({"servers": []})
        monkeypatch.setattr(httpx, "get", cap)

        result = runner.invoke(cli, ["tools"])

        assert result.exit_code == 0, result.output
        assert cap.calls, "no HTTP GET issued by `tools`"
        for url, kwargs in cap.calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url

    def test_tools_no_key_omits_header(self, monkeypatch) -> None:
        monkeypatch.delenv("SLM_HUB_API_KEY", raising=False)
        cap = _Capture({"servers": []})
        monkeypatch.setattr(httpx, "get", cap)

        result = runner.invoke(cli, ["tools"])

        assert result.exit_code == 0, result.output
        for url, kwargs in cap.calls:
            assert _KEY_HEADER not in kwargs.get("headers", {}), url

    def test_reconnect_sends_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        cap = _Capture({"success": True, "message": "ok"})
        monkeypatch.setattr(httpx, "post", cap)

        result = runner.invoke(cli, ["reconnect", "backend"])

        assert result.exit_code == 0, result.output
        assert cap.calls, "no HTTP POST issued by `reconnect`"
        for url, kwargs in cap.calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url

    def test_status_verbose_sends_api_key(self, monkeypatch) -> None:
        """`status --verbose` fetches the gated /api/servers/detail — must auth."""
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        monkeypatch.setattr(
            "slm_mcp_hub.resilience.watchdog.is_running", lambda: True
        )
        monkeypatch.setattr(
            "slm_mcp_hub.resilience.watchdog.read_pid_file", lambda: 12345
        )
        cap = _Capture(
            {
                "version": "0.3.1",
                "state": "ready",
                "uptime_seconds": 0,
                "mcp_servers_configured": 0,
                "servers": [],
            }
        )
        monkeypatch.setattr(httpx, "get", cap)

        result = runner.invoke(cli, ["status", "--verbose"])

        assert result.exit_code == 0, result.output
        detail_calls = [c for c in cap.calls if "/api/servers/detail" in c[0]]
        assert detail_calls, "status --verbose never hit /api/servers/detail"
        for url, kwargs in detail_calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url


class TestServerCommandsAuth:
    def test_server_list_sends_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        cap = _Capture({"servers": []})
        monkeypatch.setattr(httpx, "get", cap)

        result = runner.invoke(cli, ["server", "list"])

        assert result.exit_code == 0, result.output
        assert cap.calls, "no HTTP GET issued by `server list`"
        for url, kwargs in cap.calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url

    def test_server_reload_sends_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        cap = _Capture({"success": True, "summary": "no changes"})
        monkeypatch.setattr(httpx, "post", cap)

        result = runner.invoke(cli, ["server", "reload"])

        assert result.exit_code == 0, result.output
        assert cap.calls, "no HTTP POST issued by `server reload`"
        for url, kwargs in cap.calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url


class TestObserveCommandsAuth:
    def test_servers_sends_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        cap = _Capture({"servers": []})
        monkeypatch.setattr(httpx, "get", cap)

        result = runner.invoke(cli, ["servers"])

        assert result.exit_code == 0, result.output
        assert cap.calls, "no HTTP GET issued by `servers`"
        for url, kwargs in cap.calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url

    def test_warm_sends_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        cap = _Capture({"success": True, "message": "warmed"})
        monkeypatch.setattr(httpx, "post", cap)

        result = runner.invoke(cli, ["warm", "backend"])

        assert result.exit_code == 0, result.output
        assert cap.calls, "no HTTP POST issued by `warm`"
        for url, kwargs in cap.calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url

    def test_health_sends_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        cap = _Capture({"servers": []})
        monkeypatch.setattr(httpx, "get", cap)

        result = runner.invoke(cli, ["health"])

        assert result.exit_code == 0, result.output
        assert cap.calls, "no HTTP GET issued by `health`"
        for url, kwargs in cap.calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url

    def test_stop_sends_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("SLM_HUB_API_KEY", "abc123")
        cap = _Capture({"success": True, "message": "stopped"})
        monkeypatch.setattr(httpx, "post", cap)

        result = runner.invoke(cli, ["stop", "backend"])

        assert result.exit_code == 0, result.output
        assert cap.calls, "no HTTP POST issued by `stop`"
        for url, kwargs in cap.calls:
            assert kwargs.get("headers", {}).get(_KEY_HEADER) == "abc123", url
