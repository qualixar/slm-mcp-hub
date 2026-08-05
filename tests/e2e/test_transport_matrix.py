"""P08 — E2E transport matrix: 4 non-OAuth cells + inventory equivalence.

Cells proven with REAL subprocess/ASGI/loopback-socket transport:
    Cell 1: downstream=stdio   × upstream=stdio   (Hub mcp → upstream subprocess)
    Cell 2: downstream=stdio   × upstream=HTTP    (Hub mcp → upstream uvicorn)
    Cell 3: downstream=HTTP    × upstream=stdio   (Hub start --sdk-mode → upstream subprocess)
    Cell 4: downstream=HTTP    × upstream=HTTP    (Hub start --sdk-mode → upstream uvicorn)

Inventory equivalence: stdio-downstream and HTTP-downstream federating the same
upstream set must expose identical tools/resources/prompts after normalizing
transport metadata (server names, namespacing).

Meta-MCP nuance: the Hub exposes 3 meta-tools (search_tools/call_tool/list_servers)
rather than direct passthrough of upstream tool names.  Conformance content scenarios
that expect the upstream tool names to appear at the Hub's tools/list level are
architecturally N/A.  Content is proven via call_tool federating to real upstreams.

All transport boundaries are real:
- Upstream stdio: child subprocess communicating over process stdio
- Upstream HTTP: uvicorn child process on a real loopback TCP port
- Hub stdio downstream: Hub `slm-hub mcp` subprocess on process stdio
- Hub HTTP downstream: Hub `slm-hub start --sdk-mode` subprocess on loopback TCP port
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
# Reference upstream MCP server script
# ---------------------------------------------------------------------------

_UPSTREAM_SCRIPT = '''#!/usr/bin/env python3
"""Reference upstream MCP server — stdio or HTTP transport.

Usage: python upstream.py stdio|http [port] [name]
"""
import sys, anyio, base64, uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.resources import FunctionResource
from mcp.server.mcpserver.prompts.base import Prompt as _SrvPrompt
from mcp.server.mcpserver.prompts.base import PromptArgument as _SrvPromptArg
from mcp.types import ImageContent, PromptMessage, TextContent, GetPromptResult

MODE = sys.argv[1] if len(sys.argv) > 1 else "stdio"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 0
NAME = sys.argv[3] if len(sys.argv) > 3 else f"upstream-{MODE}"

# Minimal 1×1 red PNG, base64-encoded.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "/5+hHgAHggJ/PchI6QAAAABJRU5ErkJggg=="
)

app = MCPServer(NAME)


@app.tool(description="Echo the given text back unchanged.")
async def echo_text(text: str) -> str:
    """Echo the given text back."""
    return text


@app.tool(description="Return a minimal red PNG as image bytes.")
async def get_image() -> ImageContent:
    """Return a tiny PNG image."""
    return ImageContent(type="image", data=_TINY_PNG_B64, mimeType="image/png")


app.add_resource(FunctionResource(
    uri="data://sample",
    name="Sample Resource",
    description="A sample data resource for E2E verification.",
    mime_type="text/plain",
    fn=lambda: "sample-resource-content",
))


async def _greet(name: str) -> GetPromptResult:
    return GetPromptResult(messages=[
        PromptMessage(role="user", content=TextContent(type="text", text=f"Hello, {name}!"))
    ])


app.add_prompt(_SrvPrompt(
    name="greet",
    description="Greet someone by name.",
    arguments=[_SrvPromptArg(name="name", description="Name to greet.", required=True)],
    fn=_greet,
))


def _run_stdio() -> None:
    anyio.run(app.run_stdio_async)


def _run_http() -> None:
    import uvicorn
    starlette = app.streamable_http_app(host="127.0.0.1")
    config = uvicorn.Config(
        starlette, host="127.0.0.1", port=PORT, log_level="error",
    )
    server = uvicorn.Server(config)
    anyio.run(server.serve)


if MODE == "stdio":
    _run_stdio()
else:
    _run_http()
'''

# ---------------------------------------------------------------------------
# Port helpers
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
# Upstream server fixtures
# ---------------------------------------------------------------------------


def _reap(proc: subprocess.Popen) -> None:
    """Terminate a child process, escalating to kill if it will not exit.

    Prevents hung children and held loopback ports from leaking across tests
    under load (a plain ``wait(timeout=5)`` after ``terminate()`` would raise
    and leave the process alive).
    """
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture(scope="module")
def upstream_script_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write the upstream server script to a temp file (shared for module scope)."""
    tmp = tmp_path_factory.mktemp("upstream")
    script = tmp / "upstream_server.py"
    script.write_text(_UPSTREAM_SCRIPT)
    return script


@pytest.fixture()
def upstream_stdio(upstream_script_path: Path) -> Any:
    """Start a stdio upstream MCP server subprocess. Yields the Popen object."""
    proc = subprocess.Popen(
        [sys.executable, str(upstream_script_path), "stdio", "0", "up-stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.3)
    yield proc
    _reap(proc)


@pytest.fixture()
def upstream_http(upstream_script_path: Path) -> Any:
    """Start an HTTP upstream MCP server subprocess. Yields (proc, port)."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(upstream_script_path), "http", str(port), "up-http"],
        stderr=subprocess.PIPE,
    )
    assert _wait_for_http_port("127.0.0.1", port), f"HTTP upstream never ready on port {port}"
    time.sleep(0.5)  # Let uvicorn finish initialising its MCP session manager
    yield proc, port
    _reap(proc)


# ---------------------------------------------------------------------------
# Hub config + process fixtures
# ---------------------------------------------------------------------------


def _write_hub_config(
    tmp_path: Path,
    *,
    port: int,
    stdio_script: Path | None = None,
    http_upstream_port: int | None = None,
) -> Path:
    """Write a Hub config.json pointing at the given upstream servers."""
    servers: dict[str, Any] = {}
    if stdio_script is not None:
        servers["up-stdio"] = {
            "command": sys.executable,
            "args": [str(stdio_script), "stdio", "0", "up-stdio"],
        }
    if http_upstream_port is not None:
        servers["up-http"] = {
            "url": f"http://127.0.0.1:{http_upstream_port}/mcp",
        }
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "log_level": "WARNING",
        "mcpServers": servers,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg))
    cfg_file.chmod(0o600)
    return cfg_file


@pytest.fixture()
def hub_stdio_proc(
    tmp_path: Path,
    upstream_script_path: Path,
    upstream_http: Any,
) -> Any:
    """Hub stdio downstream (slm-hub mcp) federating both upstream kinds."""
    _, http_port = upstream_http
    hub_port = _free_port()
    _write_hub_config(
        tmp_path,
        port=hub_port,
        stdio_script=upstream_script_path,
        http_upstream_port=http_port,
    )
    env = {**os.environ, "SLM_HUB_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "slm_mcp_hub.cli.main", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    # Give Hub time to connect to both upstreams (federation is async background task)
    time.sleep(5.0)
    yield proc
    _reap(proc)


@pytest.fixture()
def hub_http_proc(
    tmp_path: Path,
    upstream_script_path: Path,
    upstream_http: Any,
) -> Any:
    """Hub HTTP downstream (slm-hub start --sdk-mode) federating both upstream kinds."""
    _, http_port = upstream_http
    hub_port = _free_port()
    _write_hub_config(
        tmp_path,
        port=hub_port,
        stdio_script=upstream_script_path,
        http_upstream_port=http_port,
    )
    env = {**os.environ, "SLM_HUB_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "slm_mcp_hub.cli.main",
            "start", "--port", str(hub_port), "--sdk-mode",
        ],
        stderr=subprocess.PIPE,
        env=env,
    )
    assert _wait_hub_http_ready(hub_port), f"Hub HTTP never ready on port {hub_port}"
    time.sleep(4.0)  # Let federation connect in background
    yield proc, hub_port
    _reap(proc)


# ---------------------------------------------------------------------------
# Single-upstream fixtures (for cell-specific tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def hub_stdio_only_stdio(
    tmp_path: Path,
    upstream_script_path: Path,
) -> Any:
    """Hub stdio downstream federating ONLY the stdio upstream."""
    hub_port = _free_port()
    _write_hub_config(tmp_path, port=hub_port, stdio_script=upstream_script_path)
    env = {**os.environ, "SLM_HUB_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "slm_mcp_hub.cli.main", "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    time.sleep(5.0)
    yield proc
    _reap(proc)


@pytest.fixture()
def hub_stdio_only_http(
    tmp_path: Path,
    upstream_http: Any,
) -> Any:
    """Hub stdio downstream federating ONLY the HTTP upstream."""
    _, http_port = upstream_http
    hub_port = _free_port()
    _write_hub_config(tmp_path, port=hub_port, http_upstream_port=http_port)
    env = {**os.environ, "SLM_HUB_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "slm_mcp_hub.cli.main", "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    time.sleep(5.0)
    yield proc
    _reap(proc)


@pytest.fixture()
def hub_http_only_stdio(
    tmp_path: Path,
    upstream_script_path: Path,
) -> Any:
    """Hub HTTP downstream federating ONLY the stdio upstream."""
    hub_port = _free_port()
    _write_hub_config(tmp_path, port=hub_port, stdio_script=upstream_script_path)
    env = {**os.environ, "SLM_HUB_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "slm_mcp_hub.cli.main", "start",
         "--port", str(hub_port), "--sdk-mode"],
        stderr=subprocess.PIPE, env=env,
    )
    assert _wait_hub_http_ready(hub_port), "Hub HTTP (stdio upstream) never ready"
    time.sleep(5.0)
    yield proc, hub_port
    _reap(proc)


@pytest.fixture()
def hub_http_only_http(
    tmp_path: Path,
    upstream_http: Any,
) -> Any:
    """Hub HTTP downstream federating ONLY the HTTP upstream."""
    _, http_port = upstream_http
    hub_port = _free_port()
    _write_hub_config(tmp_path, port=hub_port, http_upstream_port=http_port)
    env = {**os.environ, "SLM_HUB_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "slm_mcp_hub.cli.main", "start",
         "--port", str(hub_port), "--sdk-mode"],
        stderr=subprocess.PIPE, env=env,
    )
    assert _wait_hub_http_ready(hub_port), "Hub HTTP (http upstream) never ready"
    time.sleep(5.0)
    yield proc, hub_port
    _reap(proc)


# ---------------------------------------------------------------------------
# stdio MCP session helpers
# ---------------------------------------------------------------------------

_INIT_REQ = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "e2e-test", "version": "0.1"},
    },
}


def _stdio_send(proc: subprocess.Popen, req: dict[str, Any]) -> None:
    """Write a JSON-RPC request to the process stdin."""
    proc.stdin.write((json.dumps(req) + "\n").encode())
    proc.stdin.flush()


def _stdio_recv(proc: subprocess.Popen, req_id: int, timeout: float = 10.0) -> dict[str, Any] | None:
    """Read from process stdout until we see the response for req_id (skip notifications)."""
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
        # Skip notifications — they are expected (tools/list_changed, etc.)
    return None


def _stdio_rpc(
    proc: subprocess.Popen,
    req_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Send one JSON-RPC request and return the matching response."""
    req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    _stdio_send(proc, req)
    resp = _stdio_recv(proc, req_id, timeout=timeout)
    assert resp is not None, f"No response to method={method!r} id={req_id} within {timeout}s"
    return resp


def _stdio_init(proc: subprocess.Popen) -> dict[str, Any]:
    """Perform the MCP initialize handshake on a Hub stdio downstream."""
    resp = _stdio_rpc(proc, 1, "initialize", _INIT_REQ["params"])
    assert "result" in resp, f"Initialize failed: {resp}"
    return resp


def _stdio_wait_tools(
    proc: subprocess.Popen,
    min_count: int,
    start_id: int = 100,
    timeout: float = 12.0,
    poll_interval: float = 1.0,
    query: str = "echo",
) -> list[dict[str, Any]]:
    """Poll search_tools until at least min_count tools appear or timeout."""
    deadline = time.monotonic() + timeout
    req_id = start_id
    while time.monotonic() < deadline:
        resp = _stdio_rpc(proc, req_id, "tools/call", {
            "name": "search_tools",
            "arguments": {"query": query},
        })
        req_id += 1
        result = _extract_text_json(resp)
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if len(tools) >= min_count:
            return tools
        time.sleep(poll_interval)
    return []


def _extract_text_json(resp: dict[str, Any]) -> Any:
    """Extract the JSON value from a single-text-content tool result."""
    content = resp.get("result", {}).get("content", [])
    if content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


# ---------------------------------------------------------------------------
# HTTP MCP session helpers (Hub HTTP downstream in SDK mode)
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
    # Fallback: plain JSON (non-SDK mode)
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
    """Send one HTTP MCP request and return the JSON-RPC response dict."""
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    resp = httpx.post(
        f"http://127.0.0.1:{port}/mcp",
        json=body,
        headers=_HTTP_HEADERS,
        timeout=timeout,
    )
    parsed = _sse_parse(resp.text)
    assert parsed is not None, f"Could not parse response: status={resp.status_code}, body={resp.text[:200]}"
    return parsed


def _http_wait_tools(
    port: int,
    min_count: int,
    timeout: float = 15.0,
    poll_interval: float = 1.0,
    query: str = "echo",
) -> list[dict[str, Any]]:
    """Poll search_tools on the HTTP downstream until min_count tools appear."""
    deadline = time.monotonic() + timeout
    req_id = 200
    while time.monotonic() < deadline:
        resp = _http_rpc(port, "tools/call", {
            "name": "search_tools",
            "arguments": {"query": query},
        }, req_id=req_id)
        req_id += 1
        result = _extract_text_json(resp)
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if len(tools) >= min_count:
            return tools
        time.sleep(poll_interval)
    return []


# ---------------------------------------------------------------------------
# Cell 1: stdio downstream × stdio upstream
# ---------------------------------------------------------------------------


class TestCell1StdioDownstreamStdioUpstream:
    """Cell 1: Hub stdio downstream ↔ upstream stdio subprocess.

    Transport boundary: Hub subprocess stdin/stdout (NDJSON) → stdlib process stdio.
    """

    def test_c1_initialize(self, hub_stdio_only_stdio: subprocess.Popen) -> None:
        """Hub stdio downstream performs MCP handshake correctly."""
        proc = hub_stdio_only_stdio
        resp = _stdio_init(proc)
        assert resp["result"]["serverInfo"]["name"] == "slm-mcp-hub"

    def test_c1_meta_tools_list(self, hub_stdio_only_stdio: subprocess.Popen) -> None:
        """Hub stdio downstream reports exactly 3 meta-tools."""
        proc = hub_stdio_only_stdio
        _stdio_init(proc)
        resp = _stdio_rpc(proc, 2, "tools/list")
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {"search_tools", "call_tool", "list_servers"}

    def test_c1_federated_echo_via_stdio_upstream(self, hub_stdio_only_stdio: subprocess.Popen) -> None:
        """Cell 1: call_tool routes echo_text through real stdio upstream subprocess."""
        proc = hub_stdio_only_stdio
        _stdio_init(proc)
        # Wait for federation to expose the upstream tool
        tools = _stdio_wait_tools(proc, min_count=1)
        assert tools, "stdio upstream tool not discovered"
        namespaced = tools[0]["tool"]  # e.g. "up_stdio__echo_text"

        resp = _stdio_rpc(proc, 50, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"text": "cell1-probe"}},
        })
        content = resp["result"]["content"]
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "cell1-probe"

    def test_c1_image_bytes_federated(self, hub_stdio_only_stdio: subprocess.Popen) -> None:
        """Image bytes returned by upstream are faithfully proxied via call_tool."""
        proc = hub_stdio_only_stdio
        _stdio_init(proc)
        # Find get_image tool (search specifically for "image")
        tools = _stdio_wait_tools(proc, min_count=1, query="image")
        image_tool = next(
            (t["tool"] for t in tools if "image" in t["tool"]),
            None,
        )
        assert image_tool, f"get_image not found in {tools}"

        resp = _stdio_rpc(proc, 51, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": image_tool, "arguments": {}},
        })
        content = resp["result"]["content"]
        assert content[0]["type"] == "image"
        assert content[0]["mimeType"] == "image/png"
        import base64
        image_bytes = base64.b64decode(content[0]["data"])
        assert image_bytes[:4] == b"\x89PNG", "Expected PNG magic bytes"

    def test_c1_list_servers_shows_upstream(self, hub_stdio_only_stdio: subprocess.Popen) -> None:
        """list_servers meta-tool reports the upstream stdio server once connected."""
        proc = hub_stdio_only_stdio
        _stdio_init(proc)
        _stdio_wait_tools(proc, min_count=1)
        resp = _stdio_rpc(proc, 60, "tools/call", {
            "name": "list_servers",
            "arguments": {},
        })
        result = _extract_text_json(resp)
        # Exactly one upstream is federated in this fixture (up-stdio).
        assert result.get("server_count") == 1, (
            f"expected exactly 1 federated server, got {result}"
        )
        # The single upstream must be identifiable by name. Tool namespacing
        # sanitizes the config key "up-stdio" to "up_stdio" (hyphen -> underscore),
        # and list_servers keys each entry under "server".
        servers = result.get("servers")
        assert servers, f"list_servers returned no servers array: {result}"
        names = {s.get("server") if isinstance(s, dict) else s for s in servers}
        assert "up_stdio" in names, f"up_stdio not reported by list_servers: {names}"


# ---------------------------------------------------------------------------
# Cell 2: stdio downstream × HTTP upstream
# ---------------------------------------------------------------------------


class TestCell2StdioDownstreamHttpUpstream:
    """Cell 2: Hub stdio downstream ↔ upstream HTTP subprocess.

    Transport boundary: Hub subprocess stdio → HTTP loopback TCP socket.
    """

    def test_c2_echo_via_http_upstream(self, hub_stdio_only_http: subprocess.Popen) -> None:
        """Cell 2: call_tool routes echo_text through real HTTP upstream subprocess."""
        proc = hub_stdio_only_http
        _stdio_init(proc)
        tools = _stdio_wait_tools(proc, min_count=1)
        assert tools, "HTTP upstream tool not discovered via stdio downstream"
        namespaced = tools[0]["tool"]  # e.g. "up_http__echo_text"

        resp = _stdio_rpc(proc, 50, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"text": "cell2-probe"}},
        })
        content = resp["result"]["content"]
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "cell2-probe"

    def test_c2_image_bytes_federated(self, hub_stdio_only_http: subprocess.Popen) -> None:
        """Image bytes from HTTP upstream faithfully proxied through stdio downstream."""
        proc = hub_stdio_only_http
        _stdio_init(proc)
        tools = _stdio_wait_tools(proc, min_count=1, query="image")
        image_tool = next((t["tool"] for t in tools if "image" in t["tool"]), None)
        assert image_tool, f"get_image not found in {tools}"

        resp = _stdio_rpc(proc, 51, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": image_tool, "arguments": {}},
        })
        content = resp["result"]["content"]
        assert content[0]["type"] == "image"
        assert content[0]["mimeType"] == "image/png"
        import base64
        assert base64.b64decode(content[0]["data"])[:4] == b"\x89PNG"

    def test_c2_search_tools_finds_http_upstream_tools(self, hub_stdio_only_http: subprocess.Popen) -> None:
        """search_tools finds tools from the HTTP upstream via stdio downstream."""
        proc = hub_stdio_only_http
        _stdio_init(proc)
        tools = _stdio_wait_tools(proc, min_count=1)
        assert any("http" in t["server"].lower() or "http" in t["tool"].lower() for t in tools), (
            f"No HTTP upstream tool found; got {[t['tool'] for t in tools]}"
        )


# ---------------------------------------------------------------------------
# Cell 3: HTTP downstream × stdio upstream
# ---------------------------------------------------------------------------


class TestCell3HttpDownstreamStdioUpstream:
    """Cell 3: Hub HTTP downstream (SDK mode) ↔ upstream stdio subprocess.

    Transport boundary: HTTP loopback TCP socket → Hub ASGI → upstream subprocess stdio.
    """

    def test_c3_initialize(self, hub_http_only_stdio: Any) -> None:
        """Hub HTTP downstream (SDK mode) returns valid initialize result."""
        _, port = hub_http_only_stdio
        resp = _http_rpc(port, "initialize", _INIT_REQ["params"], req_id=1)
        assert resp["result"]["serverInfo"]["name"] == "slm-mcp-hub"

    def test_c3_meta_tools_list(self, hub_http_only_stdio: Any) -> None:
        """Hub HTTP downstream reports exactly 3 meta-tools."""
        _, port = hub_http_only_stdio
        resp = _http_rpc(port, "tools/list", req_id=2)
        names = {t["name"] for t in resp["result"]["tools"]}
        assert names == {"search_tools", "call_tool", "list_servers"}

    def test_c3_echo_via_stdio_upstream(self, hub_http_only_stdio: Any) -> None:
        """Cell 3: call_tool routes echo_text through real stdio upstream via HTTP downstream."""
        _, port = hub_http_only_stdio
        tools = _http_wait_tools(port, min_count=1)
        assert tools, "stdio upstream tool not discovered via HTTP downstream"
        namespaced = tools[0]["tool"]

        resp = _http_rpc(port, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"text": "cell3-probe"}},
        }, req_id=50)
        content = resp["result"]["content"]
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "cell3-probe"

    def test_c3_image_bytes_federated(self, hub_http_only_stdio: Any) -> None:
        """Image bytes from stdio upstream faithfully proxied via HTTP downstream."""
        _, port = hub_http_only_stdio
        tools = _http_wait_tools(port, min_count=1, query="image")
        image_tool = next((t["tool"] for t in tools if "image" in t["tool"]), None)
        assert image_tool, f"get_image not found in {tools}"

        resp = _http_rpc(port, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": image_tool, "arguments": {}},
        }, req_id=51)
        content = resp["result"]["content"]
        assert content[0]["type"] == "image"
        assert content[0]["mimeType"] == "image/png"
        import base64
        assert base64.b64decode(content[0]["data"])[:4] == b"\x89PNG"


# ---------------------------------------------------------------------------
# Cell 4: HTTP downstream × HTTP upstream
# ---------------------------------------------------------------------------


class TestCell4HttpDownstreamHttpUpstream:
    """Cell 4: Hub HTTP downstream (SDK mode) ↔ upstream HTTP subprocess.

    Transport boundary: client HTTP → Hub ASGI/TCP → upstream HTTP/TCP.
    Two distinct real TCP loopback connections.
    """

    def test_c4_echo_via_http_upstream(self, hub_http_only_http: Any) -> None:
        """Cell 4: call_tool routes echo_text through HTTP upstream via HTTP downstream."""
        _, port = hub_http_only_http
        tools = _http_wait_tools(port, min_count=1)
        assert tools, "HTTP upstream tool not discovered via HTTP downstream"
        namespaced = tools[0]["tool"]

        resp = _http_rpc(port, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"text": "cell4-probe"}},
        }, req_id=50)
        content = resp["result"]["content"]
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "cell4-probe"

    def test_c4_prompt_arg_substitution(self, hub_http_only_http: Any) -> None:
        """Prompt argument substitution is preserved by the Hub's federation layer."""
        _, port = hub_http_only_http
        _http_wait_tools(port, min_count=1)
        # Prompts are namespaced on the Hub: prompts/list returns all upstream prompts
        resp = _http_rpc(port, "prompts/list", req_id=10)
        prompts = resp["result"]["prompts"]
        assert prompts, "No prompts discovered from HTTP upstream"
        # The greet prompt should be there (namespaced)
        greet = next((p for p in prompts if "greet" in p["name"]), None)
        assert greet, f"greet prompt not found; got {[p['name'] for p in prompts]}"

        # get the prompt with argument substitution
        resp2 = _http_rpc(port, "prompts/get", {
            "name": greet["name"],
            "arguments": {"name": "E2E"},
        }, req_id=11)
        messages = resp2["result"]["messages"]
        assert messages
        text = messages[0]["content"]["text"]
        assert "E2E" in text, f"Argument substitution failed; got: {text!r}"

    def test_c4_resources_list(self, hub_http_only_http: Any) -> None:
        """resources/list returns namespaced resources from HTTP upstream."""
        _, port = hub_http_only_http
        _http_wait_tools(port, min_count=1)
        resp = _http_rpc(port, "resources/list", req_id=20)
        resources = resp["result"]["resources"]
        assert resources, "No resources discovered from HTTP upstream"
        assert any("sample" in r["uri"] for r in resources), (
            f"Expected data://sample resource; got {[r['uri'] for r in resources]}"
        )

    def test_c4_resource_read(self, hub_http_only_http: Any) -> None:
        """resources/read returns the resource content from HTTP upstream."""
        _, port = hub_http_only_http
        _http_wait_tools(port, min_count=1)
        resp = _http_rpc(port, "resources/list", req_id=30)
        resources = resp["result"]["resources"]
        sample_uri = next((r["uri"] for r in resources if "sample" in r["uri"]), None)
        assert sample_uri, f"No sample resource; got {[r['uri'] for r in resources]}"

        resp2 = _http_rpc(port, "resources/read", {"uri": sample_uri}, req_id=31)
        contents = resp2["result"]["contents"]
        assert contents
        text = str(contents[0].get("text", ""))
        assert "sample-resource-content" in text, (
            f"resources/read returned unexpected content (wrong body federated "
            f"or empty): {text!r}"
        )


# ---------------------------------------------------------------------------
# Inventory equivalence
# ---------------------------------------------------------------------------


class TestInventoryEquivalence:
    """Both downstreams (stdio + HTTP) federating the same upstreams must yield
    identical inventories after normalizing transport-specific metadata."""

    def _normalize_tools(self, tools: list[dict[str, Any]]) -> list[str]:
        return sorted(t["tool"] for t in tools)

    def _normalize_resources(self, resources: list[dict[str, Any]]) -> list[str]:
        return sorted(r["uri"] for r in resources)

    def _normalize_prompts(self, prompts: list[dict[str, Any]]) -> list[str]:
        return sorted(p["name"] for p in prompts)

    def test_tool_inventory_equivalent(
        self,
        hub_stdio_proc: subprocess.Popen,
        hub_http_proc: Any,
    ) -> None:
        """search_tools(query='') returns the complete federated tool inventory
        from both downstreams (stdio and HTTP) and the inventories must match.

        Empty query returns ALL tools from ALL federated upstreams.
        Each upstream contributes 2 tools (echo_text + get_image) × 2 upstreams = 4 total.
        """
        proc = hub_stdio_proc
        _, http_port = hub_http_proc

        _stdio_init(proc)
        # Use empty query to get full inventory from both upstreams
        stdio_tools = _stdio_wait_tools(proc, min_count=4, timeout=15.0, query="")
        assert len(stdio_tools) >= 4, (
            f"stdio downstream found only {len(stdio_tools)} tools (expected 4 from 2 upstreams): "
            f"{[t['tool'] for t in stdio_tools]}"
        )

        http_tools = _http_wait_tools(http_port, min_count=4, timeout=15.0, query="")
        assert len(http_tools) >= 4, (
            f"HTTP downstream found only {len(http_tools)} tools (expected 4 from 2 upstreams): "
            f"{[t['tool'] for t in http_tools]}"
        )

        assert self._normalize_tools(stdio_tools) == self._normalize_tools(http_tools), (
            f"Tool inventories differ:\n"
            f"  stdio: {self._normalize_tools(stdio_tools)}\n"
            f"  http:  {self._normalize_tools(http_tools)}"
        )

    def test_resource_inventory_equivalent(
        self,
        hub_stdio_proc: subprocess.Popen,
        hub_http_proc: Any,
    ) -> None:
        """resources/list returns the same federated resources from both downstreams."""
        proc = hub_stdio_proc
        _, http_port = hub_http_proc

        _stdio_init(proc)
        # Wait for both upstreams to be federating (at least 4 tools: 2 tools × 2 upstreams)
        _stdio_wait_tools(proc, min_count=4, timeout=15.0, query="")
        _http_wait_tools(http_port, min_count=4, timeout=15.0, query="")

        # Get resources via stdio downstream
        resp_s = _stdio_rpc(proc, 300, "resources/list")
        stdio_resources = resp_s["result"]["resources"]

        # Get resources via HTTP downstream
        resp_h = _http_rpc(http_port, "resources/list", req_id=300)
        http_resources = resp_h["result"]["resources"]

        # Non-vacuous guard: both upstreams expose data://sample, so a working
        # federation yields >=2 resources on EACH downstream. Without this the
        # equality below passes trivially ([] == []) if resource federation is
        # broken on both sides.
        assert len(stdio_resources) >= 2, (
            f"stdio downstream federated {len(stdio_resources)} resources; expected "
            f">=2 from 2 upstreams: {self._normalize_resources(stdio_resources)}"
        )
        assert len(http_resources) >= 2, (
            f"HTTP downstream federated {len(http_resources)} resources; expected "
            f">=2 from 2 upstreams: {self._normalize_resources(http_resources)}"
        )
        assert any("sample" in r["uri"] for r in stdio_resources), (
            f"Expected a data://sample resource; got {self._normalize_resources(stdio_resources)}"
        )

        assert self._normalize_resources(stdio_resources) == self._normalize_resources(http_resources), (
            f"Resource inventories differ:\n"
            f"  stdio: {self._normalize_resources(stdio_resources)}\n"
            f"  http:  {self._normalize_resources(http_resources)}"
        )

    def test_prompt_inventory_equivalent(
        self,
        hub_stdio_proc: subprocess.Popen,
        hub_http_proc: Any,
    ) -> None:
        """prompts/list returns the same federated prompts from both downstreams."""
        proc = hub_stdio_proc
        _, http_port = hub_http_proc

        _stdio_init(proc)
        _stdio_wait_tools(proc, min_count=4, timeout=15.0, query="")
        _http_wait_tools(http_port, min_count=4, timeout=15.0, query="")

        resp_s = _stdio_rpc(proc, 400, "prompts/list")
        stdio_prompts = resp_s["result"]["prompts"]

        resp_h = _http_rpc(http_port, "prompts/list", req_id=400)
        http_prompts = resp_h["result"]["prompts"]

        # Non-vacuous guard: both upstreams expose the 'greet' prompt, so a
        # working federation yields >=2 prompts on EACH downstream. Prevents the
        # equality below from passing trivially when prompt federation is broken.
        assert len(stdio_prompts) >= 2, (
            f"stdio downstream federated {len(stdio_prompts)} prompts; expected "
            f">=2 from 2 upstreams: {self._normalize_prompts(stdio_prompts)}"
        )
        assert len(http_prompts) >= 2, (
            f"HTTP downstream federated {len(http_prompts)} prompts; expected "
            f">=2 from 2 upstreams: {self._normalize_prompts(http_prompts)}"
        )
        assert any("greet" in p["name"] for p in stdio_prompts), (
            f"Expected a 'greet' prompt; got {self._normalize_prompts(stdio_prompts)}"
        )

        assert self._normalize_prompts(stdio_prompts) == self._normalize_prompts(http_prompts), (
            f"Prompt inventories differ:\n"
            f"  stdio: {self._normalize_prompts(stdio_prompts)}\n"
            f"  http:  {self._normalize_prompts(http_prompts)}"
        )

    def test_meta_mcp_nuance_documented(
        self,
        hub_http_proc: Any,
    ) -> None:
        """Meta-MCP nuance: tools/list returns only the 3 Hub meta-tools, not
        upstream tools by name.  Upstream tools are federated via call_tool.

        The official conformance content scenarios (tools-call-image, etc.) that
        expect upstream tool names at the Hub's tools/list level are
        architecturally N/A for the standalone Hub meta-interface.  This test
        documents the behaviour explicitly rather than treating it as a defect.
        """
        _, http_port = hub_http_proc
        # Wait for full inventory (4 tools from 2 upstreams each with 2 tools)
        _http_wait_tools(http_port, min_count=4, timeout=15.0, query="")
        resp = _http_rpc(http_port, "tools/list", req_id=500)
        meta_names = {t["name"] for t in resp["result"]["tools"]}
        # Hub exposes exactly 3 meta-tools:
        assert meta_names == {"search_tools", "call_tool", "list_servers"}, (
            f"Expected exactly the 3 Hub meta-tools; got {meta_names}. "
            "Upstream tool names are exposed via call_tool federation, not via tools/list. "
            "This is the intended Meta-MCP architecture (saves ~150K tokens per session)."
        )
        # Upstream tools are reachable through call_tool:
        upstream_tools = _http_wait_tools(http_port, min_count=4, timeout=5.0, query="")
        assert len(upstream_tools) >= 4, (
            f"Expected 4+ upstream tools via search_tools, got {len(upstream_tools)}"
        )
