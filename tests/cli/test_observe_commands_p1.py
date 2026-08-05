"""W5-P1 TDD — CLI observe commands unit tests.

TDD: written BEFORE implementation. All tests use httpx mocking via
unittest.mock.patch — NO live network calls. Verifies:
- servers_cmd renders table with correct header
- health_cmd exits 1 on needs_attention, 0 on all-healthy
- warm_cmd POSTs to correct URL, prints message
- stop_cmd POSTs to correct URL, prints message
- warm_cmd idempotent when already connected
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# Sample server data for responses
_SERVER_HEALTHY = {
    "name": "srv-a",
    "transport": "stdio",
    "connected": True,
    "lifecycle": "connected",
    "tools": 3,
    "restart_count": 0,
    "uptime_seconds": 300.0,
    "p95_latency_ms": 12.5,
    "ram_bytes": 10_485_760,
    "consecutive_failures": 0,
    "needs_attention": False,
    "last_error": None,
}

_SERVER_FLAGGED = {
    "name": "srv-b",
    "transport": "stdio",
    "connected": False,
    "lifecycle": "error",
    "tools": 0,
    "restart_count": 5,
    "uptime_seconds": 0.0,
    "p95_latency_ms": 0.0,
    "ram_bytes": None,
    "consecutive_failures": 3,
    "needs_attention": True,
    "last_error": "Connection refused",
}


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock httpx Response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# servers_cmd tests
# ---------------------------------------------------------------------------


class TestServersCmd:
    def test_servers_cmd_renders_table(self, runner: CliRunner) -> None:
        """CliRunner invocation of servers_cmd with mocked httpx returning fixture data
        produces a table with header NAME STATE UPTIME RST P95ms RAM TOOLS."""
        from slm_mcp_hub.cli.observe_commands import servers_cmd

        mock_resp = _mock_response({"servers": [_SERVER_HEALTHY]})

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(servers_cmd, [])

        assert result.exit_code == 0
        output = result.output
        assert "NAME" in output
        assert "STATE" in output
        assert "UPTIME" in output
        assert "RST" in output
        assert "P95ms" in output
        assert "RAM" in output
        assert "TOOLS" in output

    def test_servers_cmd_shows_server_name(self, runner: CliRunner) -> None:
        """servers_cmd table rows contain the server name."""
        from slm_mcp_hub.cli.observe_commands import servers_cmd

        mock_resp = _mock_response({"servers": [_SERVER_HEALTHY]})

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(servers_cmd, [])

        assert "srv-a" in result.output

    def test_servers_cmd_hub_not_running(self, runner: CliRunner) -> None:
        """servers_cmd prints message when hub is unreachable."""
        import httpx

        from slm_mcp_hub.cli.observe_commands import servers_cmd

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            result = runner.invoke(servers_cmd, [])

        assert "not running" in result.output.lower()

    def test_servers_cmd_empty_server_list(self, runner: CliRunner) -> None:
        """servers_cmd prints 'No servers' when list is empty."""
        from slm_mcp_hub.cli.observe_commands import servers_cmd

        mock_resp = _mock_response({"servers": []})

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(servers_cmd, [])

        assert result.exit_code == 0
        assert "no servers" in result.output.lower()

    def test_servers_cmd_json_flag(self, runner: CliRunner) -> None:
        """servers_cmd --json outputs raw JSON array."""
        import json

        from slm_mcp_hub.cli.observe_commands import servers_cmd

        mock_resp = _mock_response({"servers": [_SERVER_HEALTHY]})

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(servers_cmd, ["--json"])

        assert result.exit_code == 0
        # Should be parseable JSON
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["name"] == "srv-a"


# ---------------------------------------------------------------------------
# health_cmd tests
# ---------------------------------------------------------------------------


class TestHealthCmd:
    def test_health_cmd_exits_1_when_flagged(self, runner: CliRunner) -> None:
        """health_cmd exits with code 1 when any server has needs_attention=True."""
        from slm_mcp_hub.cli.observe_commands import health_cmd

        mock_resp = _mock_response({"servers": [_SERVER_HEALTHY, _SERVER_FLAGGED]})

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(health_cmd, [])

        assert result.exit_code == 1

    def test_health_cmd_exits_0_when_all_healthy(self, runner: CliRunner) -> None:
        """health_cmd exits with code 0 when all servers are healthy."""
        from slm_mcp_hub.cli.observe_commands import health_cmd

        mock_resp = _mock_response({"servers": [_SERVER_HEALTHY]})

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(health_cmd, [])

        assert result.exit_code == 0
        assert "healthy" in result.output.lower()

    def test_health_cmd_exits_1_hub_not_running(self, runner: CliRunner) -> None:
        """health_cmd exits 1 when hub is unreachable."""
        import httpx

        from slm_mcp_hub.cli.observe_commands import health_cmd

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            result = runner.invoke(health_cmd, [])

        assert result.exit_code == 1

    def test_health_cmd_shows_flagged_server_name(self, runner: CliRunner) -> None:
        """health_cmd shows the name of servers needing attention."""
        from slm_mcp_hub.cli.observe_commands import health_cmd

        mock_resp = _mock_response({"servers": [_SERVER_HEALTHY, _SERVER_FLAGGED]})

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(health_cmd, [])

        assert "srv-b" in result.output


# ---------------------------------------------------------------------------
# warm_cmd tests
# ---------------------------------------------------------------------------


class TestWarmCmd:
    def test_warm_cmd_posts_to_correct_url(self, runner: CliRunner) -> None:
        """warm_cmd POSTs to /api/servers/{name}/warm — verify URL via httpx mock."""
        from slm_mcp_hub.cli.observe_commands import warm_cmd

        mock_resp = _mock_response({
            "success": True,
            "server": "srv-a",
            "message": "Connected successfully"
        })

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            result = runner.invoke(warm_cmd, ["srv-a"])

        assert result.exit_code == 0
        call_args = mock_post.call_args
        assert "/api/servers/srv-a/warm" in call_args[0][0]

    def test_warm_cmd_idempotent_already_connected(self, runner: CliRunner) -> None:
        """When /api/servers/{name}/warm returns {success: True, message: 'Already connected...'},
        warm_cmd prints the message and exits 0 — no error."""
        from slm_mcp_hub.cli.observe_commands import warm_cmd

        mock_resp = _mock_response({
            "success": True,
            "server": "srv-a",
            "message": "Already connected — no action taken"
        })

        with patch("httpx.post", return_value=mock_resp):
            result = runner.invoke(warm_cmd, ["srv-a"])

        assert result.exit_code == 0
        assert "Already connected" in result.output

    def test_warm_cmd_hub_not_running(self, runner: CliRunner) -> None:
        """warm_cmd prints 'Hub is not running.' and exits 1 on ConnectError."""
        import httpx

        from slm_mcp_hub.cli.observe_commands import warm_cmd

        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            result = runner.invoke(warm_cmd, ["srv-a"])

        assert result.exit_code == 1
        assert "not running" in result.output.lower()

    def test_warm_cmd_failure_response(self, runner: CliRunner) -> None:
        """warm_cmd exits 1 when success=False in response."""
        from slm_mcp_hub.cli.observe_commands import warm_cmd

        mock_resp = _mock_response({
            "success": False,
            "server": "srv-unknown",
            "message": "Server not found"
        })

        with patch("httpx.post", return_value=mock_resp):
            result = runner.invoke(warm_cmd, ["srv-unknown"])

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# stop_cmd tests
# ---------------------------------------------------------------------------


class TestStopCmd:
    def test_stop_cmd_posts_to_correct_url(self, runner: CliRunner) -> None:
        """stop_cmd POSTs to /api/servers/{name}/stop — verify URL via httpx mock."""
        from slm_mcp_hub.cli.observe_commands import stop_cmd

        mock_resp = _mock_response({
            "success": True,
            "server": "srv-a",
            "message": "Eviction requested — backend will be stopped if not pinned"
        })

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            result = runner.invoke(stop_cmd, ["srv-a"])

        assert result.exit_code == 0
        call_args = mock_post.call_args
        assert "/api/servers/srv-a/stop" in call_args[0][0]

    def test_stop_cmd_hub_not_running(self, runner: CliRunner) -> None:
        """stop_cmd prints 'Hub is not running.' and exits 1 on ConnectError."""
        import httpx

        from slm_mcp_hub.cli.observe_commands import stop_cmd

        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            result = runner.invoke(stop_cmd, ["srv-a"])

        assert result.exit_code == 1
        assert "not running" in result.output.lower()

    def test_stop_cmd_success_output(self, runner: CliRunner) -> None:
        """stop_cmd prints 'Stop: {name} — {message}' and exits 0 on success."""
        from slm_mcp_hub.cli.observe_commands import stop_cmd

        mock_resp = _mock_response({
            "success": True,
            "server": "srv-a",
            "message": "Eviction requested — backend will be stopped if not pinned"
        })

        with patch("httpx.post", return_value=mock_resp):
            result = runner.invoke(stop_cmd, ["srv-a"])

        assert result.exit_code == 0
        assert "srv-a" in result.output
