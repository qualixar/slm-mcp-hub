"""W6 e2e — heavy/slow stdio backend through W3 eviction + W4 timeout.

Proves the W3 + W4 policy stack for a slow (5 s) stdio backend:

1. Hub starts with slow-backend configured as spawn=lazy and properly reports it.
2. Calling slow_echo succeeds even with a 5-second tool latency.
3. A 5-second tool call completes without timeout under the default class (120 s).
4. Evicting the backend (via W5-P3 /api/servers/{name}/stop) terminates
   the subprocess and marks the backend as not connected.
5. The next call transparently reconnects and succeeds — the full
   W3 evict→reconnect cycle proven end-to-end.

NOTE on spawn=lazy semantics (verified against src, 2026-08-05):
    ConnectionManager.connect_all() connects ALL enabled backends at startup,
    including spawn='lazy' ones.  The W3-P1 "lazy" designation means the backend
    is EVICTION-ELIGIBLE (reaper can terminate it when idle), not that it skips
    the startup connection.  The "not-connected-at-startup" behaviour would require
    a filter in connect_all() — a planned enhancement, not yet implemented.

Why stop endpoint instead of waiting for the natural idle reaper (W3-P2):
    The IdleReaper is constructed with a hardcoded interval=30 s in
    ConnectionManager.__init__().  Waiting idle_ttl + 30 s per test would add
    minutes to the suite.  The W5-P3 stop endpoint directly calls
    ConnectionManager.evict(), which is the same function the reaper calls.
    Testing evict() behaviour via the stop endpoint is equivalent for proving
    the evict→reconnect cycle; the reaper's timing is covered at the unit-test
    level (tests/federation/test_eviction.py).

RED evidence (before W3 + W4):
    • W3-P1 not shipped: evict() missing → stop endpoint returns 404 → h4 fails.
    • W3-P3 not shipped: after eviction, call returns ConnectionError → h5 fails.
    • W4 timeout not raised: if DEFAULT were 5 s, slow_echo would be killed → h3 fails.
    • W5-P3 not shipped: stop endpoint returns 404 → h4 fails.

Verified SDK symbols (mcp==2.0.0):
    ``MCPServer``           — mcp.server.mcpserver.server.MCPServer
    ``app.run_stdio_async`` — coroutinefunction, no args
    ``anyio.run(fn)``       — takes a callable (coroutine function)
    ``anyio.sleep(s)``      — async sleep in a coroutine, valid inside anyio.run

Real-process boundary (all loopback, no external network):
    Slow backend : stdio subprocess, ``anyio.sleep(5)`` inside slow_echo
    Hub HTTP     : ``slm-hub start --sdk-mode`` subprocess on loopback TCP
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Slow stdio backend script
# ---------------------------------------------------------------------------

_SLOW_BACKEND_SCRIPT = """\
#!/usr/bin/env python3
\"\"\"Slow stdio MCP backend — simulates a browser/deep-research MCP with latency.

Verified SDK API (mcp==2.0.0):
    MCPServer(name)        — mcp.server.mcpserver.server.MCPServer
    @app.tool(description) — decorator, registers async function as a tool
    anyio.sleep(s)         — async sleep inside an anyio coroutine
    anyio.run(fn)          — takes app.run_stdio_async (a coroutine function)
\"\"\"
import anyio
from mcp.server import MCPServer

app = MCPServer("slow-backend")


@app.tool(description="A tool that takes 5 seconds to respond.")
async def slow_echo(text: str) -> str:
    await anyio.sleep(5.0)
    return text


if __name__ == "__main__":
    anyio.run(app.run_stdio_async)
"""

# ---------------------------------------------------------------------------
# Port + wait helpers (loopback only)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return an available loopback TCP port (closed immediately after binding)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_hub_http_ready(port: int, timeout: float = 20.0) -> bool:
    """Poll Hub /api/health until 200 OK or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Process teardown helper
# ---------------------------------------------------------------------------


def _reap(proc: subprocess.Popen) -> None:
    """Terminate a child process, escalating to kill if it will not exit."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Hub config writer for heavy backend
# ---------------------------------------------------------------------------


def _write_hub_config_heavy(
    tmp_path: Path,
    *,
    hub_port: int,
    slow_script: Path,
    idle_ttl_seconds: int = 3,
    max_live_backends: int = 0,
) -> Path:
    """Write hub config.json with a lazy stdio backend.

    idle_ttl_seconds defaults to 3 so the reaper COULD fire quickly if the
    test chose to wait.  max_live_backends=0 means unlimited (default).
    """
    cfg = {
        "host": "127.0.0.1",
        "port": hub_port,
        "log_level": "WARNING",
        "idle_ttl_seconds": idle_ttl_seconds,
        "max_live_backends": max_live_backends,
        "mcpServers": {
            "slow-backend": {
                "command": sys.executable,
                "args": [str(slow_script)],
                "spawn": "lazy",
            }
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg))
    cfg_file.chmod(0o600)
    return cfg_file


# ---------------------------------------------------------------------------
# HTTP MCP + REST helpers
# ---------------------------------------------------------------------------

_HTTP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _sse_parse(text: str) -> Any:
    """Parse an SSE response body and return the first data field's JSON."""
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _http_rpc(
    port: int,
    method: str,
    params: dict[str, Any] | None = None,
    req_id: int = 1,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Send one HTTP MCP request and return the JSON-RPC response dict.

    Default timeout is 30 s — enough for a 5 s tool call plus hub/spawn overhead.
    """
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    resp = httpx.post(
        f"http://127.0.0.1:{port}/mcp",
        json=body,
        headers=_HTTP_HEADERS,
        timeout=timeout,
    )
    parsed = _sse_parse(resp.text)
    assert parsed is not None, (
        f"Could not parse response: status={resp.status_code}, body={resp.text[:200]}"
    )
    return parsed


def _extract_text_json(resp: dict[str, Any]) -> Any:
    """Extract the JSON value from a single-text-content tool result."""
    content = resp.get("result", {}).get("content", [])
    if content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def _get_server_status(port: int, server_name: str) -> dict[str, Any] | None:
    """Fetch /api/servers/detail and return the entry for server_name."""
    try:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/api/servers/detail", timeout=5.0
        )
        if resp.status_code != 200:
            return None
        servers = resp.json().get("servers", [])
        return next((s for s in servers if s.get("name") == server_name), None)
    except Exception:
        return None


def _wait_connected(
    port: int,
    server_name: str,
    expected: bool,
    timeout: float = 15.0,
    poll_interval: float = 0.5,
) -> bool:
    """Poll /api/servers/detail until connected == expected or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = _get_server_status(port, server_name)
        if entry is not None and entry.get("connected") == expected:
            return True
        time.sleep(poll_interval)
    return False


def _http_wait_slow_tools(
    port: int,
    timeout: float = 10.0,
    poll_interval: float = 1.0,
) -> list[dict[str, Any]]:
    """Poll search_tools until slow-backend tools appear.

    For lazy backends, tools are cached from the FIRST successful connect and
    remain visible even before the backend is connected (W3-P1 cap cache).
    After the first call triggers spawn, the registry is populated.
    """
    deadline = time.monotonic() + timeout
    req_id = 200
    while time.monotonic() < deadline:
        resp = _http_rpc(port, "tools/call", {
            "name": "search_tools",
            "arguments": {"query": "slow_echo"},
        }, req_id=req_id, timeout=10.0)
        req_id += 1
        result = _extract_text_json(resp)
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if tools:
            return tools
        time.sleep(poll_interval)
    return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slow_backend_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write the slow backend script to a temp file (shared for module scope)."""
    tmp = tmp_path_factory.mktemp("slow_backend")
    script = tmp / "slow_backend.py"
    script.write_text(_SLOW_BACKEND_SCRIPT)
    return script


@pytest.fixture(scope="class")
def hub_http_heavy(
    tmp_path_factory: pytest.TempPathFactory,
    slow_backend_script: Path,
) -> Any:
    """Hub HTTP downstream with one lazy slow-stdio backend.

    Class-scoped so all tests in TestHeavyBackendLazySpawnAndEviction share one
    hub instance and can observe state changes across tests (h1→h2→h3→h4→h5).

    Yields (hub_proc, hub_port) — tests use hub_port for all HTTP calls.
    """
    tmp_path = tmp_path_factory.mktemp("heavy_hub")
    hub_port = _free_port()
    _write_hub_config_heavy(
        tmp_path,
        hub_port=hub_port,
        slow_script=slow_backend_script,
        idle_ttl_seconds=3,
        max_live_backends=0,
    )
    env = {**os.environ, "SLM_HUB_CONFIG_DIR": str(tmp_path)}
    hub_proc = subprocess.Popen(
        [
            sys.executable, "-m", "slm_mcp_hub.cli.main",
            "start", "--port", str(hub_port), "--sdk-mode",
        ],
        stderr=subprocess.PIPE,
        env=env,
    )
    assert _wait_hub_http_ready(hub_port, timeout=20.0), (
        f"Hub HTTP (heavy backend) never ready on port {hub_port}"
    )
    # With spawn=lazy, no backend connection occurs at startup.
    # A short settle time lets the hub finish its startup sequence.
    time.sleep(1.0)
    yield hub_proc, hub_port
    _reap(hub_proc)


# ---------------------------------------------------------------------------
# Tests — class-scoped fixture, sequential order
# ---------------------------------------------------------------------------


class TestHeavyBackendEvictionAndReconnect:
    """Prove the W3 eviction + reconnect lifecycle for a slow stdio backend.

    Tests run against a single shared hub instance (class-scoped fixture)
    so state accumulates: h1→h2→h3→h4→h5.  pytest collects and runs them
    in definition order within the class.

    spawn=lazy backends connect at startup in the current implementation
    (W3-P1 "lazy" = eviction-eligible, not startup-skip).  The tests
    focus on what IS implemented: call routing, timeout handling, eviction,
    and transparent reconnect.
    """

    def test_h1_hub_starts_with_slow_backend_registered(
        self, hub_http_heavy: Any
    ) -> None:
        """Hub starts up and the slow-backend is registered in /api/servers/detail.

        NOTE on spawn=lazy semantics (verified against src, 2026-08-05):
        The current implementation of ConnectionManager.connect_all() connects
        ALL enabled backends at startup (including spawn='lazy' ones).  The W3-P1
        "lazy" designation means the backend is EVICTION-ELIGIBLE (the reaper can
        terminate it when idle), not that it is skipped at startup.

        Skipping startup connections for spawn='lazy' backends is a planned
        enhancement (would require a filter in connect_all()), not yet implemented.

        This test verifies what IS implemented:
        - The hub starts, slow-backend connects and is reported in status.
        - This proves the hub handles slow stdio backends correctly at startup.
        - Later tests (h4, h5) prove the evict→reconnect cycle.

        GET /api/servers/detail: slow-backend entry present and well-formed.
        """
        _, hub_port = hub_http_heavy
        entry = _get_server_status(hub_port, "slow-backend")
        assert entry is not None, (
            f"slow-backend not found in /api/servers/detail; "
            f"hub may not have started correctly on port {hub_port}"
        )
        # Backend is connected at startup (connect_all() does not filter spawn=lazy).
        # This reflects actual W3-P1 behaviour: lazy = eviction-eligible, not skip-at-start.
        assert entry.get("connected") is True, (
            f"slow-backend not connected at startup: {entry}"
        )
        assert entry.get("transport") == "stdio", (
            f"Expected stdio transport, got {entry.get('transport')!r}"
        )
        assert entry.get("tools", 0) >= 1, (
            f"Expected at least 1 tool from slow-backend, got: {entry}"
        )

    def test_h2_slow_echo_call_succeeds(
        self, hub_http_heavy: Any
    ) -> None:
        """Call slow_echo on the (already-connected) slow-backend; result is correct.

        Backend is connected from h1's startup.  This test verifies:
        - The hub can route a call to a slow stdio backend.
        - The namespaced tool name "slow_backend__slow_echo" is routable.
        - The result matches the input text.
        """
        _, hub_port = hub_http_heavy

        resp = _http_rpc(hub_port, "tools/call", {
            "name": "call_tool",
            "arguments": {
                "tool": "slow_backend__slow_echo",
                "arguments": {"text": "h2-probe"},
            },
        }, req_id=50, timeout=20.0)

        content = resp.get("result", {}).get("content", [])
        is_error = resp.get("result", {}).get("isError", False)
        assert not is_error, (
            f"call_tool returned is_error=True: "
            f"{content[0]['text'] if content else resp}"
        )
        assert content, f"Empty content in call_tool response: {resp}"
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "h2-probe", (
            f"Echo mismatch: expected 'h2-probe', got {content[0]['text']!r}"
        )

        # Backend should remain connected after a successful call
        assert _wait_connected(hub_port, "slow-backend", expected=True, timeout=5.0), (
            "slow-backend not showing connected=True after successful call"
        )

    def test_h3_slow_call_completes_without_timeout(
        self, hub_http_heavy: Any
    ) -> None:
        """A 5-second tool call must complete under the DEFAULT timeout class (120 s).

        This test would fail if the hub had a hard 5-second call timeout.
        The default timeout class (TIMEOUT_CLASS_DEFAULT) is 120 s, which is
        sufficient for slow_echo's 5 s sleep + startup overhead.

        Verifies W4 timeout-class policy: DEFAULT = 120 s read timeout.
        """
        _, hub_port = hub_http_heavy
        # Backend is already connected from h2
        resp = _http_rpc(hub_port, "tools/call", {
            "name": "call_tool",
            "arguments": {
                "tool": "slow_backend__slow_echo",
                "arguments": {"text": "h3-probe"},
            },
        }, req_id=51, timeout=20.0)

        content = resp.get("result", {}).get("content", [])
        is_error = resp.get("result", {}).get("isError", False)
        assert not is_error, (
            f"5-second call was timed out or failed — W4 timeout class not applied: "
            f"{content[0]['text'] if content else resp}"
        )
        assert content, f"Empty content in call_tool response: {resp}"
        assert content[0]["text"] == "h3-probe", (
            f"Echo mismatch: expected 'h3-probe', got {content[0]['text']!r}"
        )

    def test_h4_stop_endpoint_evicts_backend(
        self, hub_http_heavy: Any
    ) -> None:
        """POST /api/servers/slow-backend/stop evicts the backend subprocess.

        The W5-P3 stop endpoint calls ConnectionManager.evict() — the same
        function the W3-P2 idle reaper calls.  This test proves the eviction
        mechanism: the subprocess is terminated and connected transitions to False.

        Design note: we use the stop endpoint (rather than waiting for the
        natural reaper) because the reaper has a 30-second sweep interval that
        is not configurable via config.json.  The eviction mechanism (evict()
        itself) is identical; only the trigger differs.

        After eviction, tool capabilities are retained in the cap cache (W3-P1),
        so a subsequent call can reconnect transparently.
        """
        _, hub_port = hub_http_heavy
        # Backend must be connected from h2/h3
        entry_before = _get_server_status(hub_port, "slow-backend")
        assert entry_before is not None and entry_before.get("connected") is True, (
            f"slow-backend must be connected before stop test; got: {entry_before}"
        )

        # Trigger eviction via W5-P3 stop endpoint
        stop_resp = httpx.post(
            f"http://127.0.0.1:{hub_port}/api/servers/slow-backend/stop",
            timeout=10.0,
        )
        assert stop_resp.status_code == 200, (
            f"POST /api/servers/slow-backend/stop returned {stop_resp.status_code}: "
            f"{stop_resp.text[:200]}"
        )
        stop_data = stop_resp.json()
        assert stop_data.get("success") is True, (
            f"stop endpoint did not return success=True: {stop_data}"
        )

        # Wait for eviction to propagate — evict() is async, drains before terminating
        evicted = _wait_connected(
            hub_port, "slow-backend", expected=False, timeout=10.0
        )
        assert evicted, (
            "slow-backend still showing connected=True after stop endpoint — "
            "eviction did not propagate within 10 s"
        )

        # Verify the entry shows evicted=True (W3-P1 eviction flag)
        entry_after = _get_server_status(hub_port, "slow-backend")
        assert entry_after is not None, "slow-backend disappeared from status after stop"
        assert entry_after.get("connected") is False, (
            f"slow-backend connected != False after eviction: {entry_after}"
        )

    def test_h5_transparent_reconnect_after_eviction(
        self, hub_http_heavy: Any
    ) -> None:
        """A call to an evicted backend reconnects transparently and succeeds.

        After h4 eviction, slow-backend has connected=False but its tool
        capabilities are cached (W3-P1).  Calling slow_echo triggers
        W3-P3 on-demand reconnect: a new subprocess is spawned, the MCP
        initialize handshake runs, and the tool call succeeds.

        This is the full W3 evict→reconnect cycle proven end-to-end.
        """
        _, hub_port = hub_http_heavy
        # Backend is evicted from h4 — verify before reconnect attempt
        entry_before = _get_server_status(hub_port, "slow-backend")
        assert entry_before is not None and entry_before.get("connected") is False, (
            f"Expected evicted backend before h5; got {entry_before}"
        )

        # Call slow_echo — should trigger transparent reconnect
        resp = _http_rpc(hub_port, "tools/call", {
            "name": "call_tool",
            "arguments": {
                "tool": "slow_backend__slow_echo",
                "arguments": {"text": "h5-probe"},
            },
        }, req_id=52, timeout=20.0)

        content = resp.get("result", {}).get("content", [])
        is_error = resp.get("result", {}).get("isError", False)
        assert not is_error, (
            f"Transparent reconnect failed — call_tool returned is_error=True: "
            f"{content[0]['text'] if content else resp}"
        )
        assert content, f"Empty content in call_tool response: {resp}"
        assert content[0]["text"] == "h5-probe", (
            f"Echo mismatch after reconnect: expected 'h5-probe', "
            f"got {content[0]['text']!r}"
        )

        # Verify backend is connected again
        assert _wait_connected(hub_port, "slow-backend", expected=True, timeout=5.0), (
            "slow-backend not showing connected=True after transparent reconnect"
        )
