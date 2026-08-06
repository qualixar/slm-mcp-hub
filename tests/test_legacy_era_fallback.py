"""Regression: strict legacy stdio servers must survive the era-detection probe.

SDK 2.0.0's ``Client(mode="auto")`` sends ``server/discover`` before
``initialize`` to detect the protocol era. Rust ``rmcp``-based servers treat an
unknown pre-initialize method as a protocol violation and close the connection,
so the probe kills the process and the hub reports only a nested
``ExceptionGroup`` wrapping ``MCPError(-32000, 'Connection closed')``.

Observed in production on 2026-08-06: black-widow (pplx-mcp 0.11.0, rmcp)
connected fine with ``mode="legacy"`` and failed every time with ``mode="auto"``,
while 24 of 26 other stdio backends were unaffected. The hub must therefore keep
``auto`` as the default and fall back to ``legacy`` rather than abandoning era
detection wholesale.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.protocol.outbound import OutboundClient

FIXTURE = Path(__file__).parent / "fixtures" / "strict_legacy_server.py"


def _strict_legacy_config(name: str = "strict-legacy") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=(str(FIXTURE),),
    )


@pytest.mark.anyio
async def test_connects_to_server_that_closes_on_discover_probe() -> None:
    """The whole point: a server that hangs up on ``server/discover`` still connects."""
    client = OutboundClient(_strict_legacy_config())
    try:
        await client.connect()

        names = [t["name"] for t in client.capabilities["tools"]]
        assert "ping" in names, names

        # The fallback must be pinned, so reconnects skip the doomed probe.
        assert client._connect_mode == "legacy"

        result = await client.call_tool("ping", {})
        assert "pong" in str(result)
    finally:
        await client.disconnect()


@pytest.mark.anyio
async def test_fixture_really_does_close_on_discover() -> None:
    """Guard the guard.

    If the fixture ever starts *answering* ``server/discover`` instead of dying,
    the test above would pass for the wrong reason and the regression could
    return unnoticed.
    """
    import json
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    probe = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
    )
    out, err = proc.communicate(probe + "\n", timeout=30)

    assert proc.returncode != 0, "fixture must exit non-zero on a pre-initialize method"
    assert out.strip() == "", f"fixture must not reply to the probe, got: {out!r}"
    assert "ExpectedInitializeRequest" in err
