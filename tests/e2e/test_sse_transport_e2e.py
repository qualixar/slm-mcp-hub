"""W6 e2e — Cell 7 (stdio-downstream × SSE-upstream) and
Cell 8 (HTTP-downstream × SSE-upstream).

Proves that the hub connects to, harvests capabilities from, routes calls
through, and tears down a real legacy-SSE upstream subprocess cleanly.

RED evidence (before W6-P1 fix):
    ``protocol/outbound.py::_build_client()`` fell through to
    ``_build_http_client()`` (Streamable-HTTP) when ``transport == "sse"``.
    A ``streamable_http_client`` aimed at GET /sse receives either a 405
    Method-Not-Allowed or a protocol mismatch, causing the connection to fail.
    These tests would have failed: tool not discovered, call_tool error.
    The W6-P1 fix adds ``_build_sse_client()`` that calls ``sse_client`` from
    ``mcp.client.sse``, which is the correct legacy-SSE protocol client.

Verified SDK symbols (mcp==2.0.0, verified against installed site-packages):
    ``MCPServer``          — mcp.server.mcpserver.server.MCPServer
    ``app.sse_app()``      — signature: (sse_path, message_path, host) → Starlette
    ``app.run_stdio_async``— coroutinefunction, no args; used for stdio servers
    ``anyio.run(fn)``      — takes a callable (coroutine function)
    ``uvicorn.Server.serve``— coroutinefunction; anyio.run(server.serve) is valid

Real-process boundary (all loopback, no external network):
    SSE upstream : child subprocess, uvicorn on 127.0.0.1:{port}
                   Serves GET /sse (SSE stream) + POST /messages/ (client→server)
    Hub stdio    : ``slm-hub mcp`` subprocess, communicates over process stdio
    Hub HTTP     : ``slm-hub start --sdk-mode`` subprocess on loopback TCP

Cell 7 drain test — the hardest assertion:
    SIGTERM the hub → wait for exit → verify SSE upstream subprocess is still
    alive (proc.poll() is None). The hub MUST NOT kill a URL-based upstream it
    did not spawn. This validates external-server lifecycle semantics: the hub
    closes its client-side AsyncExitStack (aclose()), which terminates the
    HTTP connection; the upstream server process is unaffected.
"""
from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# SSE upstream MCP server script
# ---------------------------------------------------------------------------

_SSE_UPSTREAM_SCRIPT = """\
#!/usr/bin/env python3
\"\"\"Minimal legacy-SSE MCP server for W6 e2e tests.

Verified SDK API (mcp==2.0.0):
    MCPServer — mcp.server.mcpserver.server.MCPServer
    app.sse_app(sse_path, message_path, host) → Starlette (not ASGI directly)
    uvicorn.Server(config).serve — coroutinefunction
    anyio.run(coroutine_fn) — takes a callable
\"\"\"
import sys
import anyio
import uvicorn
from mcp.server import MCPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8999
NAME = sys.argv[2] if len(sys.argv) > 2 else "up-sse"

app = MCPServer(NAME)


@app.tool(description="Echo text for SSE e2e tests.")
async def sse_echo(text: str) -> str:
    return text


if __name__ == "__main__":
    starlette = app.sse_app(
        sse_path="/sse",
        message_path="/messages/",
        host="127.0.0.1",
    )
    config = uvicorn.Config(
        starlette,
        host="127.0.0.1",
        port=PORT,
        log_level="error",
    )
    server = uvicorn.Server(config)
    anyio.run(server.serve)
"""

# ---------------------------------------------------------------------------
# Port + wait helpers (loopback only)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return an available loopback TCP port (closed immediately after binding)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_http_port(host: str, port: int, timeout: float = 15.0) -> bool:
    """Poll until a loopback port accepts connections or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


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
# Hub config writer for SSE upstream
# ---------------------------------------------------------------------------


def _write_hub_config_sse(
    tmp_path: Path,
    *,
    hub_port: int,
    sse_upstream_port: int,
) -> Path:
    """Write hub config.json pointing at the SSE upstream's /sse endpoint."""
    cfg = {
        "host": "127.0.0.1",
        "port": hub_port,
        "log_level": "WARNING",
        "mcpServers": {
            "up-sse": {
                "type": "sse",
                "url": f"http://127.0.0.1:{sse_upstream_port}/sse",
            }
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg))
    cfg_file.chmod(0o600)
    return cfg_file


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sse_upstream_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write the SSE upstream script to a temp file (shared for module scope)."""
    tmp = tmp_path_factory.mktemp("sse_upstream")
    script = tmp / "sse_upstream_server.py"
    script.write_text(_SSE_UPSTREAM_SCRIPT)
    return script


@pytest.fixture(scope="module")
def upstream_sse(sse_upstream_script: Path) -> Any:
    """Start the SSE upstream subprocess.  Shared across all tests in module.

    Yields (proc, port) — module-scoped so we do not pay the uvicorn startup
    cost on every test.  The upstream is resilient to hub reconnects; each
    hub connects/disconnects independently.
    """
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(sse_upstream_script), str(port), "up-sse"],
        stderr=subprocess.PIPE,
    )
    assert _wait_for_http_port("127.0.0.1", port, timeout=15.0), (
        f"SSE upstream never ready on port {port}"
    )
    # Extra settle time — uvicorn starts the ASGI app after TCP bind
    time.sleep(0.5)
    yield proc, port
    _reap(proc)


@pytest.fixture()
def hub_stdio_sse(tmp_path: Path, upstream_sse: Any) -> Any:
    """Hub stdio downstream (slm-hub mcp) federating the SSE upstream.

    Yields (hub_proc, sse_proc, sse_port) — caller needs sse_proc for the
    drain-survives test to assert the upstream is still alive after hub exits.
    """
    sse_proc, sse_port = upstream_sse
    hub_port = _free_port()
    _write_hub_config_sse(tmp_path, hub_port=hub_port, sse_upstream_port=sse_port)
    env = {**os.environ, "SLM_HUB_CONFIG_DIR": str(tmp_path)}
    hub_proc = subprocess.Popen(
        [sys.executable, "-m", "slm_mcp_hub.cli.main", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    # Give the hub time to connect to the SSE upstream.  SSE connection requires
    # an HTTP round trip (GET /sse), which is fast on loopback, but federation
    # is a background asyncio task started after the MCP handshake.
    time.sleep(5.0)
    yield hub_proc, sse_proc, sse_port
    _reap(hub_proc)


@pytest.fixture()
def hub_http_sse(tmp_path: Path, upstream_sse: Any) -> Any:
    """Hub HTTP downstream (slm-hub start --sdk-mode) federating the SSE upstream.

    Yields (hub_proc, sse_proc, sse_port, hub_port).
    """
    sse_proc, sse_port = upstream_sse
    hub_port = _free_port()
    _write_hub_config_sse(tmp_path, hub_port=hub_port, sse_upstream_port=sse_port)
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
        f"Hub HTTP never ready on port {hub_port}"
    )
    # Let federation connect to SSE upstream in background
    time.sleep(4.0)
    yield hub_proc, sse_proc, sse_port, hub_port
    _reap(hub_proc)


# ---------------------------------------------------------------------------
# stdio MCP session helpers
# ---------------------------------------------------------------------------

_INIT_PARAMS = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "w6-e2e-sse", "version": "0.1"},
}


def _stdio_send(proc: subprocess.Popen, req: dict[str, Any]) -> None:
    proc.stdin.write((json.dumps(req) + "\n").encode())
    proc.stdin.flush()


def _stdio_recv(
    proc: subprocess.Popen,
    req_id: int,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Read from stdout until the response for req_id arrives (skip notifications)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == req_id:
            return msg
    return None


def _stdio_rpc(
    proc: subprocess.Popen,
    req_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    _stdio_send(proc, req)
    resp = _stdio_recv(proc, req_id, timeout=timeout)
    assert resp is not None, (
        f"No response to method={method!r} id={req_id} within {timeout}s"
    )
    return resp


def _stdio_init(proc: subprocess.Popen) -> dict[str, Any]:
    resp = _stdio_rpc(proc, 1, "initialize", _INIT_PARAMS)
    assert "result" in resp, f"initialize failed: {resp}"
    return resp


def _extract_text_json(resp: dict[str, Any]) -> Any:
    """Extract the JSON value from a single-text-content tool result."""
    content = resp.get("result", {}).get("content", [])
    if content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def _stdio_wait_sse_tools(
    proc: subprocess.Popen,
    timeout: float = 15.0,
    poll_interval: float = 1.0,
) -> list[dict[str, Any]]:
    """Poll search_tools until the SSE upstream's echo tool appears or timeout."""
    deadline = time.monotonic() + timeout
    req_id = 100
    while time.monotonic() < deadline:
        resp = _stdio_rpc(proc, req_id, "tools/call", {
            "name": "search_tools",
            "arguments": {"query": "sse_echo"},
        })
        req_id += 1
        result = _extract_text_json(resp)
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if tools:
            return tools
        time.sleep(poll_interval)
    return []


# ---------------------------------------------------------------------------
# HTTP MCP session helpers
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
    timeout: float = 12.0,
) -> dict[str, Any]:
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


def _http_wait_sse_tools(
    port: int,
    timeout: float = 15.0,
    poll_interval: float = 1.0,
) -> list[dict[str, Any]]:
    """Poll search_tools on the HTTP downstream until the SSE echo tool appears."""
    deadline = time.monotonic() + timeout
    req_id = 200
    while time.monotonic() < deadline:
        resp = _http_rpc(port, "tools/call", {
            "name": "search_tools",
            "arguments": {"query": "sse_echo"},
        }, req_id=req_id)
        req_id += 1
        result = _extract_text_json(resp)
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if tools:
            return tools
        time.sleep(poll_interval)
    return []


# ---------------------------------------------------------------------------
# Cell 7: stdio downstream × SSE upstream
# ---------------------------------------------------------------------------


class TestCell7StdioDownstreamSSEUpstream:
    """Cell 7: Hub stdio downstream ↔ legacy-SSE upstream subprocess.

    Transport boundary: Hub subprocess stdin/stdout (NDJSON) → GET /sse + POST /messages/
    on loopback TCP.  Proves the W6-P1 SSE dispatch fix end-to-end.
    """

    def test_c7_initialize(self, hub_stdio_sse: Any) -> None:
        """Hub stdio downstream performs MCP initialize handshake correctly."""
        hub_proc, _, _ = hub_stdio_sse
        resp = _stdio_init(hub_proc)
        assert resp["result"]["serverInfo"]["name"] == "slm-mcp-hub", (
            f"Unexpected server name: {resp['result']['serverInfo']}"
        )

    def test_c7_sse_echo_via_call_tool(self, hub_stdio_sse: Any) -> None:
        """Cell 7: call_tool routes sse_echo through the real SSE upstream.

        This is the primary W6 proof: the hub connects to a legacy-SSE server
        (GET /sse), harvests its tools, and routes a call back through
        POST /messages/.  Before the W6-P1 fix, this would fail because
        _build_client() routed SSE→streamable_http_client (wrong protocol).
        """
        hub_proc, _, _ = hub_stdio_sse
        _stdio_init(hub_proc)
        tools = _stdio_wait_sse_tools(hub_proc)
        assert tools, (
            "SSE upstream tool (sse_echo) not discovered via stdio downstream; "
            "the SSE connect/harvest path failed"
        )
        # Namespaced: config key "up-sse" → safe_server_id → "up_sse"; tool "sse_echo"
        assert any("up_sse" in t["tool"] for t in tools), (
            f"Expected up_sse__sse_echo; got {[t['tool'] for t in tools]}"
        )
        namespaced = tools[0]["tool"]

        resp = _stdio_rpc(hub_proc, 50, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"text": "cell7-probe"}},
        })
        content = resp.get("result", {}).get("content", [])
        assert content, f"Empty content in call_tool response: {resp}"
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "cell7-probe", (
            f"Echo mismatch: expected 'cell7-probe', got {content[0]['text']!r}"
        )

    def test_c7_list_servers_shows_sse_upstream(self, hub_stdio_sse: Any) -> None:
        """list_servers meta-tool reports the SSE upstream after federation connects."""
        hub_proc, _, _ = hub_stdio_sse
        _stdio_init(hub_proc)
        # Wait for federation: search_tools confirms the tool is in the registry
        _stdio_wait_sse_tools(hub_proc)
        resp = _stdio_rpc(hub_proc, 60, "tools/call", {
            "name": "list_servers",
            "arguments": {},
        })
        result = _extract_text_json(resp)
        assert result.get("server_count") == 1, (
            f"Expected 1 federated server, got: {result}"
        )
        servers = result.get("servers", [])
        assert servers, f"list_servers returned no servers: {result}"
        server_names = {
            s["server"] if isinstance(s, dict) else s for s in servers
        }
        assert "up_sse" in server_names, (
            f"up_sse not in list_servers; got {server_names}"
        )

    def test_c7_drain_disconnects_sse_cleanly_upstream_survives(
        self, hub_stdio_sse: Any
    ) -> None:
        """SIGTERM the hub; the SSE upstream subprocess must remain alive.

        The hub connects to an SSE upstream it did NOT spawn (URL-based).
        On shutdown, the hub closes its AsyncExitStack (aclose on the sse_client
        context manager), which terminates the HTTP connection on the client side.
        The upstream server process is NOT killed — it is external to the hub.

        This test verifies external-server lifecycle semantics: hub closes
        cleanly without killing the upstream process.

        Design: we SIGTERM the hub proc (which triggers its MCP shutdown path),
        wait up to 5 s for it to exit, then confirm the SSE upstream is still
        accepting connections (proc.poll() is None).
        """
        hub_proc, sse_proc, sse_port = hub_stdio_sse
        _stdio_init(hub_proc)
        # Confirm SSE upstream was connected before the drain
        tools = _stdio_wait_sse_tools(hub_proc, timeout=10.0)
        assert tools, "SSE upstream not connected before drain test"

        # Terminate the hub
        hub_proc.terminate()
        try:
            hub_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub_proc.kill()
            hub_proc.wait(timeout=5)

        # Allow 2 s for the OS to propagate any cascading signals
        time.sleep(2.0)

        # Assert: SSE upstream process is still alive
        assert sse_proc.poll() is None, (
            "SSE upstream subprocess was killed when the hub exited — "
            "the hub must NOT terminate URL-based upstreams it did not spawn"
        )
        # Belt-and-suspenders: upstream still accepts TCP connections
        assert _wait_for_http_port("127.0.0.1", sse_port, timeout=3.0), (
            f"SSE upstream no longer accepting connections on port {sse_port} "
            "after hub exit — upstream process was killed or crashed"
        )


# ---------------------------------------------------------------------------
# Cell 8: HTTP downstream × SSE upstream
# ---------------------------------------------------------------------------


class TestCell8HttpDownstreamSSEUpstream:
    """Cell 8: Hub HTTP downstream (SDK mode) ↔ legacy-SSE upstream subprocess.

    Transport boundary: client HTTP → Hub ASGI/TCP → GET /sse + POST /messages/.
    Adds transport-field verification via /api/servers/detail endpoint.
    """

    def test_c8_initialize(self, hub_http_sse: Any) -> None:
        """Hub HTTP downstream (SDK mode) returns valid initialize result."""
        _, _, _, hub_port = hub_http_sse
        resp = _http_rpc(hub_port, "initialize", _INIT_PARAMS, req_id=1)
        assert resp["result"]["serverInfo"]["name"] == "slm-mcp-hub", (
            f"Unexpected server name: {resp['result']['serverInfo']}"
        )

    def test_c8_sse_echo_via_call_tool(self, hub_http_sse: Any) -> None:
        """Cell 8: call_tool routes sse_echo through the SSE upstream via HTTP downstream.

        Mirrors Cell 7 over an HTTP downstream.  Proves the full stack:
        HTTP client → Hub ASGI → sse_client → SSE upstream → sse_echo → response.
        """
        _, _, _, hub_port = hub_http_sse
        tools = _http_wait_sse_tools(hub_port)
        assert tools, (
            "SSE upstream tool (sse_echo) not discovered via HTTP downstream"
        )
        namespaced = tools[0]["tool"]

        resp = _http_rpc(hub_port, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"text": "cell8-probe"}},
        }, req_id=50)
        content = resp.get("result", {}).get("content", [])
        assert content, f"Empty content in call_tool response: {resp}"
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "cell8-probe", (
            f"Echo mismatch: expected 'cell8-probe', got {content[0]['text']!r}"
        )

    def test_c8_transport_field_reported_as_sse(self, hub_http_sse: Any) -> None:
        """GET /api/servers/detail reports transport == 'sse' for the SSE upstream.

        The /api/servers/detail endpoint returns per-backend status including
        the transport field from MCPServerConfig.  This verifies that the hub
        correctly identifies and reports the SSE transport in its status API.
        """
        _, _, _, hub_port = hub_http_sse
        # Ensure federation has connected
        _http_wait_sse_tools(hub_port)

        detail = httpx.get(
            f"http://127.0.0.1:{hub_port}/api/servers/detail", timeout=5.0
        )
        assert detail.status_code == 200, (
            f"GET /api/servers/detail returned {detail.status_code}"
        )
        servers = detail.json().get("servers", [])
        assert servers, f"No servers in /api/servers/detail: {detail.json()}"

        # Find up-sse entry (name stored as configured: "up-sse")
        sse_entry = next(
            (s for s in servers if "sse" in s.get("name", "")),
            None,
        )
        assert sse_entry is not None, (
            f"up-sse not found in /api/servers/detail: "
            f"{[s.get('name') for s in servers]}"
        )
        assert sse_entry.get("transport") == "sse", (
            f"Expected transport='sse', got {sse_entry.get('transport')!r} "
            f"in entry {sse_entry}"
        )
        assert sse_entry.get("connected") is True, (
            f"SSE upstream not connected in detail: {sse_entry}"
        )
