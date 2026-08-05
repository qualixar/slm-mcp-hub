"""W5-P3 TDD — CLI warm_cmd / stop_cmd output contract tests.

These tests verify the output format and error handling of warm_cmd and
stop_cmd against the response shape produced by the W5-P3 routes.
All network calls are mocked via httpx.patch — no live server required.

The CLI commands (warm_cmd, stop_cmd) in observe_commands.py are NOT
modified in W5-P3 (not in the allowed-files list). These tests confirm that
the existing commands handle the W5-P3 route response shape correctly.

Test plan (per LLD §12 W5-P3):
1. warm_cmd success: prints 'Warm: {name} — {message}', exits 0.
2. stop_cmd success: prints 'Stop: {name} — {message}', exits 0.
3. warm_cmd ConnectError: prints 'Hub is not running.', exits 1.
4. stop_cmd ConnectError: prints 'Hub is not running.', exits 1.
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


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock httpx Response object with json() return value."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# warm_cmd tests
# ---------------------------------------------------------------------------


class TestWarmCmdP3:
    def test_warm_cmd_success_output(self, runner: CliRunner) -> None:
        """warm_cmd with a successful HTTP response prints 'Warm: {name} — {message}'
        and exits 0.

        Verifies that the CLI correctly formats the W5-P3 route's success response:
        {success: True, message: '...'}
        """
        from slm_mcp_hub.cli.observe_commands import warm_cmd

        mock_resp = _mock_response({
            "success": True,
            "message": "Connection established — 3 tools registered",
        })

        with patch("httpx.post", return_value=mock_resp):
            result = runner.invoke(warm_cmd, ["srv-alpha"])

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput: {result.output}"
        )
        assert "Warm:" in result.output, (
            f"Expected 'Warm:' prefix in output.\nOutput: {result.output}"
        )
        assert "srv-alpha" in result.output, (
            f"Expected server name 'srv-alpha' in output.\nOutput: {result.output}"
        )
        assert "Connection established" in result.output, (
            f"Expected message text in output.\nOutput: {result.output}"
        )

    def test_warm_cmd_hub_not_running(self, runner: CliRunner) -> None:
        """warm_cmd with ConnectError prints 'Hub is not running.' and exits 1."""
        import httpx

        from slm_mcp_hub.cli.observe_commands import warm_cmd

        with patch("httpx.post", side_effect=httpx.ConnectError("Connection refused")):
            result = runner.invoke(warm_cmd, ["srv-alpha"])

        assert result.exit_code == 1, (
            f"Expected exit 1 on ConnectError, got {result.exit_code}"
        )
        assert "Hub is not running" in result.output, (
            f"Expected 'Hub is not running' in output.\nOutput: {result.output}"
        )


# ---------------------------------------------------------------------------
# stop_cmd tests
# ---------------------------------------------------------------------------


class TestStopCmdP3:
    def test_stop_cmd_success_output(self, runner: CliRunner) -> None:
        """stop_cmd with a successful HTTP response prints 'Stop: {name} — {message}'
        and exits 0.

        Verifies that the CLI correctly formats the W5-P3 route's success response:
        {success: True, message: 'Eviction requested — backend will be stopped if not pinned'}
        """
        from slm_mcp_hub.cli.observe_commands import stop_cmd

        mock_resp = _mock_response({
            "success": True,
            "message": "Eviction requested — backend will be stopped if not pinned",
        })

        with patch("httpx.post", return_value=mock_resp):
            result = runner.invoke(stop_cmd, ["srv-beta"])

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput: {result.output}"
        )
        assert "Stop:" in result.output, (
            f"Expected 'Stop:' prefix in output.\nOutput: {result.output}"
        )
        assert "srv-beta" in result.output, (
            f"Expected server name 'srv-beta' in output.\nOutput: {result.output}"
        )
        assert "Eviction requested" in result.output, (
            f"Expected message text in output.\nOutput: {result.output}"
        )

    def test_stop_cmd_hub_not_running(self, runner: CliRunner) -> None:
        """stop_cmd with ConnectError prints 'Hub is not running.' and exits 1."""
        import httpx

        from slm_mcp_hub.cli.observe_commands import stop_cmd

        with patch("httpx.post", side_effect=httpx.ConnectError("Connection refused")):
            result = runner.invoke(stop_cmd, ["srv-beta"])

        assert result.exit_code == 1, (
            f"Expected exit 1 on ConnectError, got {result.exit_code}"
        )
        assert "Hub is not running" in result.output, (
            f"Expected 'Hub is not running' in output.\nOutput: {result.output}"
        )
