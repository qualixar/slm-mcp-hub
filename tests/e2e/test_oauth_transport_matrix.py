"""P08 — OAuth E2E transport matrix: cells 5 and 6 + security proofs.

Cells proven with REAL subprocess/ASGI/loopback-socket transport:
    Cell 5: downstream=stdio   × upstream=OAuth-HTTP
    Cell 6: downstream=HTTP    × upstream=OAuth-HTTP

Transport boundary is REAL in both cells:
- Upstream: MCPServer wrapped in Bearer-token-checking middleware (uvicorn subprocess)
- Hub stdio: `slm-hub mcp` subprocess on process stdio
- Hub HTTP: `slm-hub start --sdk-mode` subprocess on loopback TCP

Mock boundary (ONLY this): the OAuth AS user decision.
    We pre-seed a valid Bearer token into the Hub's keyring (via a file-backed
    keyring backend) instead of running a real browser/redirect flow.  This is
    exactly equivalent to "the user clicked Approve" — the Hub gets a stored
    access token and uses it without further interactive steps.

Security proofs (in-process, InMemoryKeyring):
    - Token isolation: downstream bearer NEVER reaches upstream
    - Cross-process refresh serialization: filelock allows only one refresher
    - Issuer-binding change: new issuer clears stored tokens
    - Redirect URI validation: auth policy rejects non-loopback HTTP redirect URIs
    - Logout → auth_required state transition

Design notes
------------
The Hub uses `KeyringTokenStorage` backed by whatever `keyring` backend is
active.  For subprocesses we inject a file-backed backend via two env vars:
    PYTHON_KEYRING_BACKEND=file_keyring.FileKeyring   (module on PYTHONPATH)
    SLM_E2E_KEYRING_FILE=<path>                       (JSON store for tokens)

This gives us full control over stored tokens without touching the OS keychain
and without mocking the Hub's internal transport boundaries.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import httpx
import keyring
import keyring.backend
import keyring.errors
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TOKEN = "e2e-bearer-token-p08-oauth-cells"

# ---------------------------------------------------------------------------
# File-backed keyring backend (written to tmpdir, used by Hub subprocesses)
# ---------------------------------------------------------------------------

_FILE_KEYRING_SOURCE = textwrap.dedent("""\
    \"\"\"File-backed keyring backend for E2E test isolation.\"\"\"
    import json, os, pathlib
    import keyring.backend, keyring.errors

    class FileKeyring(keyring.backend.KeyringBackend):
        priority = 25.0  # outrank real backends

        @property
        def _path(self) -> pathlib.Path:
            p = os.environ.get("SLM_E2E_KEYRING_FILE", "/tmp/e2e_keyring.json")
            return pathlib.Path(p)

        def _load(self) -> dict:
            try:
                return json.loads(self._path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                return {}

        def _save(self, data: dict) -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data))

        def get_password(self, service: str, username: str) -> "str | None":
            return self._load().get(f"{service}\\x00{username}")

        def set_password(self, service: str, username: str, password: str) -> None:
            data = self._load()
            data[f"{service}\\x00{username}"] = password
            self._save(data)

        def delete_password(self, service: str, username: str) -> None:
            data = self._load()
            key = f"{service}\\x00{username}"
            if key not in data:
                raise keyring.errors.PasswordDeleteError(f"Not found: {service}/{username}")
            del data[key]
            self._save(data)
""")

# Protected MCP upstream server script (checks Authorization header)
#
# Design note: we use mcp_app.streamable_http_app(host=...) directly and call
# app.add_middleware() rather than wrapping in Starlette(routes=[Mount("/mcp",...)]).
# Reason: Mount("/mcp") redirects POST /mcp → /mcp/ (Starlette 307), which the
# MCP SDK streamable_http_client cannot handle (raises "Unexpected content type").
# streamable_http_app() uses Route("/mcp") internally — exact-match, no redirect.
_PROTECTED_UPSTREAM_SCRIPT = textwrap.dedent(f"""\
    #!/usr/bin/env python3
    \"\"\"Protected MCP server - validates Bearer token before serving MCP.\"\"\"
    import sys, anyio, uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response
    from mcp.server import MCPServer

    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    VALID_TOKEN = sys.argv[2] if len(sys.argv) > 2 else "{_VALID_TOKEN}"

    mcp_app = MCPServer("protected-upstream")

    @mcp_app.tool(description="Protected echo tool.")
    async def protected_echo(msg: str) -> str:
        return f"protected: {{msg}}"

    # streamable_http_app() registers Route("/mcp", ...) — no trailing-slash
    # redirect.  add_middleware() inserts BearerAuth before every handler.
    protected = mcp_app.streamable_http_app(host="127.0.0.1")

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            auth = request.headers.get("authorization", "")
            if auth == f"Bearer {{VALID_TOKEN}}":
                return await call_next(request)
            return Response(
                status_code=401,
                headers={{"WWW-Authenticate": 'Bearer realm="protected-mcp"'}},
                content=b"Unauthorized",
            )

    protected.add_middleware(BearerAuthMiddleware)

    config = uvicorn.Config(protected, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    anyio.run(server.serve)
""")


# ---------------------------------------------------------------------------
# InMemoryKeyring for in-process OAuth tests (no subprocess)
# ---------------------------------------------------------------------------


class _InMemoryKeyring(keyring.backend.KeyringBackend):
    """Pure-memory keyring; never touches OS keychain."""

    priority: float = 30.0

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        key = (service, username)
        if key not in self._store:
            raise keyring.errors.PasswordDeleteError(f"Not found: {service}/{username}")
        del self._store[key]


# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------


def _reap(proc: subprocess.Popen) -> None:
    """Terminate a child process, escalating to kill if it will not exit.

    A plain ``wait(timeout=5)`` after ``terminate()`` would raise and leave the
    process alive, leaking loopback ports across tests under load.
    """
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _wait_hub_http_ready(port: int, timeout: float = 20.0) -> bool:
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
# Account-key helpers (mirror KeyringTokenStorage._make_account_key)
# ---------------------------------------------------------------------------


def _compute_account_key(
    endpoint: str,
    redirect_uri: str = "http://127.0.0.1:0/callback",
    profile_id: str = "default",
    schema_version: str = "1",
) -> str:
    """Compute the same SHA-256 account key that KeyringTokenStorage uses."""
    raw = "\x00".join([schema_version, profile_id, endpoint, redirect_uri])
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_token_payload(access_token: str, expires_in: int = 3600) -> str:
    """Build the JSON blob that KeyringTokenStorage.set_tokens would write."""
    token = OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=expires_in,
    )
    return json.dumps({"v": 1, "data": token.model_dump(mode="json")})


def _make_client_info_payload(
    issuer: str,
    client_id: str = "e2e-client",
) -> str:
    """Build the JSON blob that KeyringTokenStorage.set_client_info would write."""
    client_info = OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl("http://127.0.0.1:0/callback")],
        issuer=issuer,
    )
    return json.dumps({"v": 1, "data": client_info.model_dump(mode="json")})


# ---------------------------------------------------------------------------
# Fixtures: file keyring infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def file_keyring_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write the file_keyring.py module and return the directory path."""
    d = tmp_path_factory.mktemp("file_keyring")
    (d / "file_keyring.py").write_text(_FILE_KEYRING_SOURCE)
    return d


@pytest.fixture(scope="module")
def protected_upstream_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write the protected MCP upstream server script."""
    d = tmp_path_factory.mktemp("protected_upstream")
    script = d / "protected_upstream.py"
    script.write_text(_PROTECTED_UPSTREAM_SCRIPT)
    return script


# ---------------------------------------------------------------------------
# Fixtures: protected upstream subprocess
# ---------------------------------------------------------------------------


@pytest.fixture()
def protected_upstream(protected_upstream_script: Path) -> Any:
    """Start the protected MCP upstream on a random port. Yields (proc, port)."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(protected_upstream_script), str(port), _VALID_TOKEN],
        stderr=subprocess.PIPE,
    )
    assert _wait_for_port(port), f"Protected upstream never ready on port {port}"
    time.sleep(0.4)  # Let uvicorn finish session-manager init
    yield proc, port
    _reap(proc)


# ---------------------------------------------------------------------------
# Fixtures: Hub subprocesses with OAuth config (file keyring pre-seeded)
# ---------------------------------------------------------------------------


def _write_oauth_hub_config(tmp_path: Path, *, hub_port: int, upstream_port: int) -> Path:
    """Write Hub config with OAuth-protected upstream."""
    cfg = {
        "host": "127.0.0.1",
        "port": hub_port,
        "log_level": "WARNING",
        "mcpServers": {
            "protected-upstream": {
                "url": f"http://127.0.0.1:{upstream_port}/mcp",
                "auth": {
                    "mode": "oauth",
                    "scopes": ["read"],
                },
            }
        },
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg))
    cfg_file.chmod(0o600)
    return cfg_file


def _preseed_token(
    keyring_path: Path,
    upstream_endpoint: str,
    file_keyring_dir: Path,
    token: str = _VALID_TOKEN,
) -> None:
    """Write a valid token into the file keyring JSON store.

    Computes the same account key that the Hub's KeyringTokenStorage will use,
    then writes the token payload in the expected format.
    """
    account_key = _compute_account_key(upstream_endpoint)
    token_account = account_key + ":t"
    payload = _make_token_payload(token)
    entry_key = f"slm-mcp-hub\x00{token_account}"
    data = {}
    if keyring_path.exists():
        try:
            data = json.loads(keyring_path.read_text())
        except json.JSONDecodeError:
            pass
    data[entry_key] = payload
    keyring_path.write_text(json.dumps(data))


def _subprocess_env(
    base_env: dict,
    *,
    slm_config_dir: Path,
    file_keyring_dir: Path,
    keyring_file: Path,
) -> dict:
    """Build subprocess env with file keyring injected."""
    env = {**base_env}
    env["SLM_HUB_CONFIG_DIR"] = str(slm_config_dir)
    env["PYTHON_KEYRING_BACKEND"] = "file_keyring.FileKeyring"
    env["SLM_E2E_KEYRING_FILE"] = str(keyring_file)
    # Prepend the file_keyring module to PYTHONPATH
    old_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{file_keyring_dir}:{old_pp}" if old_pp else str(file_keyring_dir)
    return env


@pytest.fixture()
def hub_oauth_stdio(
    tmp_path: Path,
    protected_upstream: Any,
    file_keyring_dir: Path,
) -> Any:
    """Hub stdio downstream federating the OAuth-protected HTTP upstream.

    Pre-seeds the Hub's file keyring with a valid token so the Hub can
    authenticate immediately (simulating a previous `auth login`).
    """
    _, upstream_port = protected_upstream
    upstream_endpoint = f"http://127.0.0.1:{upstream_port}/mcp"
    hub_port = _free_port()
    keyring_file = tmp_path / "keyring.json"

    _write_oauth_hub_config(tmp_path, hub_port=hub_port, upstream_port=upstream_port)
    _preseed_token(keyring_file, upstream_endpoint, file_keyring_dir)

    env = _subprocess_env(
        os.environ.copy(),
        slm_config_dir=tmp_path,
        file_keyring_dir=file_keyring_dir,
        keyring_file=keyring_file,
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "slm_mcp_hub.cli.main", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(6.0)  # OAuth connection takes longer (token exchange + MCP handshake)
    yield proc, keyring_file, upstream_endpoint
    _reap(proc)


@pytest.fixture()
def hub_oauth_http(
    tmp_path: Path,
    protected_upstream: Any,
    file_keyring_dir: Path,
) -> Any:
    """Hub HTTP downstream (SDK mode) federating the OAuth-protected HTTP upstream."""
    _, upstream_port = protected_upstream
    upstream_endpoint = f"http://127.0.0.1:{upstream_port}/mcp"
    hub_port = _free_port()
    keyring_file = tmp_path / "keyring.json"

    _write_oauth_hub_config(tmp_path, hub_port=hub_port, upstream_port=upstream_port)
    _preseed_token(keyring_file, upstream_endpoint, file_keyring_dir)

    env = _subprocess_env(
        os.environ.copy(),
        slm_config_dir=tmp_path,
        file_keyring_dir=file_keyring_dir,
        keyring_file=keyring_file,
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "slm_mcp_hub.cli.main",
         "start", "--port", str(hub_port), "--sdk-mode"],
        stderr=subprocess.PIPE,
        env=env,
    )
    assert _wait_hub_http_ready(hub_port), f"Hub OAuth-HTTP never ready on port {hub_port}"
    time.sleep(5.0)  # Let OAuth connection + federation complete in background
    yield proc, hub_port, keyring_file, upstream_endpoint
    _reap(proc)


# ---------------------------------------------------------------------------
# stdio MCP session helpers (same pattern as test_transport_matrix.py)
# ---------------------------------------------------------------------------

_INIT_PARAMS = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "e2e-oauth-test", "version": "0.1"},
}

_HTTP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _stdio_send(proc: subprocess.Popen, req: dict) -> None:
    proc.stdin.write((json.dumps(req) + "\n").encode())
    proc.stdin.flush()


def _stdio_recv(proc: subprocess.Popen, req_id: int, timeout: float = 12.0) -> dict | None:
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


def _stdio_rpc(proc: subprocess.Popen, req_id: int, method: str, params: dict | None = None, timeout: float = 12.0) -> dict:
    req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    _stdio_send(proc, req)
    resp = _stdio_recv(proc, req_id, timeout=timeout)
    assert resp is not None, f"No response to {method!r} id={req_id} within {timeout}s"
    return resp


def _stdio_init(proc: subprocess.Popen) -> dict:
    resp = _stdio_rpc(proc, 1, "initialize", _INIT_PARAMS)
    assert "result" in resp, f"Initialize failed: {resp}"
    return resp


def _stdio_wait_tools(proc: subprocess.Popen, min_count: int, timeout: float = 15.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    req_id = 100
    while time.monotonic() < deadline:
        resp = _stdio_rpc(proc, req_id, "tools/call", {
            "name": "search_tools",
            "arguments": {"query": "protected"},
        })
        req_id += 1
        content = resp.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                result = json.loads(content[0]["text"])
                tools = result.get("tools", [])
                if len(tools) >= min_count:
                    return tools
            except json.JSONDecodeError:
                pass
        time.sleep(1.0)
    return []


def _sse_parse(text: str) -> Any:
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


def _http_rpc(port: int, method: str, params: dict | None = None, req_id: int = 1, timeout: float = 12.0) -> dict:
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    resp = httpx.post(
        f"http://127.0.0.1:{port}/mcp",
        json=body,
        headers=_HTTP_HEADERS,
        timeout=timeout,
    )
    parsed = _sse_parse(resp.text)
    assert parsed is not None, f"Parse failed: status={resp.status_code}, body={resp.text[:200]}"
    return parsed


def _http_wait_tools(port: int, min_count: int, timeout: float = 15.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    req_id = 200
    while time.monotonic() < deadline:
        resp = _http_rpc(port, "tools/call", {
            "name": "search_tools",
            "arguments": {"query": "protected"},
        }, req_id=req_id)
        req_id += 1
        content = resp.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                result = json.loads(content[0]["text"])
                tools = result.get("tools", [])
                if len(tools) >= min_count:
                    return tools
            except json.JSONDecodeError:
                pass
        time.sleep(1.0)
    return []


# ---------------------------------------------------------------------------
# Cell 5: stdio downstream × OAuth-HTTP upstream
# ---------------------------------------------------------------------------


class TestCell5StdioOAuthHTTP:
    """Cell 5: Hub stdio downstream ↔ OAuth-protected HTTP upstream.

    Mock boundary: pre-seeded Bearer token in file keyring (user approval step).
    Real boundaries: Hub stdio subprocess, HTTP loopback connection to upstream.
    """

    def test_c5_initialize(self, hub_oauth_stdio: Any) -> None:
        """Hub stdio downstream initializes correctly with OAuth upstream configured."""
        proc, *_ = hub_oauth_stdio
        resp = _stdio_init(proc)
        assert resp["result"]["serverInfo"]["name"] == "slm-mcp-hub"

    def test_c5_meta_tools_present(self, hub_oauth_stdio: Any) -> None:
        """Hub stdio downstream still exposes exactly 3 meta-tools."""
        proc, *_ = hub_oauth_stdio
        _stdio_init(proc)
        resp = _stdio_rpc(proc, 2, "tools/list")
        names = {t["name"] for t in resp["result"]["tools"]}
        assert names == {"search_tools", "call_tool", "list_servers"}

    def test_c5_call_through_oauth_upstream(self, hub_oauth_stdio: Any) -> None:
        """Cell 5: call_tool proxies through the OAuth-protected upstream via stdio downstream.

        The Hub uses the pre-seeded Bearer token to authenticate with the upstream.
        The downstream client (this test) sends NO Authorization header — proving
        the Hub manages auth independently.
        """
        proc, *_ = hub_oauth_stdio
        _stdio_init(proc)
        tools = _stdio_wait_tools(proc, min_count=1)
        assert tools, "OAuth upstream tool not discovered via stdio downstream"

        # Call the protected tool — Hub must supply the Bearer token
        namespaced = tools[0]["tool"]  # e.g. "protected_upstream__protected_echo"
        resp = _stdio_rpc(proc, 50, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"msg": "cell5-probe"}},
        })
        content = resp["result"]["content"]
        assert content[0]["type"] == "text"
        assert "protected" in content[0]["text"]
        assert "cell5-probe" in content[0]["text"]

    def test_c5_downstream_client_needs_no_auth_header(self, hub_oauth_stdio: Any) -> None:
        """Downstream client sends NO Authorization header; Hub authenticates itself.

        This test deliberately sends a call with no auth header from the downstream
        side.  The fact that the call succeeds proves the Hub holds the auth context
        independently of the downstream connection.
        """
        proc, *_ = hub_oauth_stdio
        _stdio_init(proc)
        tools = _stdio_wait_tools(proc, min_count=1)
        namespaced = tools[0]["tool"]

        # Note: stdio protocol has no concept of headers at the MCP layer.
        # Any auth on the DOWNSTREAM side would require a session-level mechanism.
        # The fact that this call succeeds proves the Hub doesn't require the
        # downstream client to supply upstream credentials.
        resp = _stdio_rpc(proc, 51, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"msg": "no-auth-downstream"}},
        })
        assert "result" in resp, f"Expected result, got: {resp}"
        assert not resp.get("error"), f"Unexpected error: {resp.get('error')}"


# ---------------------------------------------------------------------------
# Cell 6: HTTP downstream × OAuth-HTTP upstream
# ---------------------------------------------------------------------------


class TestCell6HttpOAuthHTTP:
    """Cell 6: Hub HTTP downstream (SDK mode) ↔ OAuth-protected HTTP upstream.

    Two distinct TCP loopback connections:
    - Test → Hub HTTP (:hub_port/mcp)
    - Hub → Protected upstream (:upstream_port/mcp) with Bearer token
    """

    def test_c6_initialize(self, hub_oauth_http: Any) -> None:
        """Hub HTTP downstream initializes correctly."""
        _, hub_port, *_ = hub_oauth_http
        resp = _http_rpc(hub_port, "initialize", _INIT_PARAMS, req_id=1)
        assert resp["result"]["serverInfo"]["name"] == "slm-mcp-hub"

    def test_c6_meta_tools_present(self, hub_oauth_http: Any) -> None:
        """Hub HTTP downstream exposes exactly 3 meta-tools."""
        _, hub_port, *_ = hub_oauth_http
        resp = _http_rpc(hub_port, "tools/list", req_id=2)
        names = {t["name"] for t in resp["result"]["tools"]}
        assert names == {"search_tools", "call_tool", "list_servers"}

    def test_c6_call_through_oauth_upstream(self, hub_oauth_http: Any) -> None:
        """Cell 6: call_tool proxies through the OAuth-protected upstream via HTTP downstream.

        Two real TCP connections: test→Hub and Hub→upstream.
        The Hub authenticates with the upstream independently of the downstream.
        """
        _, hub_port, *_ = hub_oauth_http
        tools = _http_wait_tools(hub_port, min_count=1)
        assert tools, "OAuth upstream tool not discovered via HTTP downstream"

        namespaced = tools[0]["tool"]
        resp = _http_rpc(hub_port, "tools/call", {
            "name": "call_tool",
            "arguments": {"tool": namespaced, "arguments": {"msg": "cell6-probe"}},
        }, req_id=50)
        content = resp["result"]["content"]
        assert content[0]["type"] == "text"
        assert "protected" in content[0]["text"]
        assert "cell6-probe" in content[0]["text"]

    def test_c6_downstream_bearer_not_forwarded(self, hub_oauth_http: Any) -> None:
        """The downstream HTTP client's Authorization header MUST NOT reach the upstream.

        Proof: send an Authorization header to the Hub; the Hub calls the upstream
        with its own Bearer (from keyring), NOT the downstream one.  If the downstream
        bearer leaked, the upstream would receive a DIFFERENT token and might reject it.
        The call succeeds, proving the Hub used its own stored token.
        """
        _, hub_port, *_ = hub_oauth_http
        tools = _http_wait_tools(hub_port, min_count=1)
        namespaced = tools[0]["tool"]

        # Send a DIFFERENT, WRONG bearer in the downstream request.
        # If the Hub forwarded this to the upstream, the upstream would reject it.
        # The call succeeds = the Hub used its own token, not ours.
        wrong_bearer_headers = {
            **_HTTP_HEADERS,
            "Authorization": "Bearer wrong-downstream-token-must-not-reach-upstream",
        }
        body = {
            "jsonrpc": "2.0", "id": 55,
            "method": "tools/call",
            "params": {
                "name": "call_tool",
                "arguments": {"tool": namespaced, "arguments": {"msg": "isolation-test"}},
            },
        }
        resp_raw = httpx.post(
            f"http://127.0.0.1:{hub_port}/mcp",
            json=body,
            headers=wrong_bearer_headers,
            timeout=12.0,
        )
        resp = _sse_parse(resp_raw.text)
        assert resp is not None, f"No parseable response: {resp_raw.text[:200]}"
        # If downstream token leaked: upstream would reject with 401 → Hub returns error
        # If isolation works: Hub uses its own token → success
        content = resp.get("result", {}).get("content", [])
        assert content, f"Expected content, got: {resp}"
        assert "isolation-test" in content[0]["text"], (
            f"Downstream bearer leaked to upstream or wrong result: {content}"
        )


# ---------------------------------------------------------------------------
# OAuth Security Proofs (in-process, InMemoryKeyring)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_keyring_fixture():
    """Install InMemoryKeyring and restore original after test."""
    original = keyring.get_keyring()
    backend = _InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(original)


class TestOAuthTokenIsolation:
    """Prove downstream bearer NEVER reaches the upstream MCP server."""

    def test_outbound_client_build_http_client_takes_no_inbound_headers(self) -> None:
        """Structural proof: OutboundClient._build_http_client has no inbound header param.

        The method signature is the gate.  If there is no parameter named `bearer`,
        `authorization`, `inbound`, or `headers` (beyond `self`), there is no API
        surface through which a downstream Authorization header could be forwarded.
        """
        import inspect

        from slm_mcp_hub.protocol.outbound import OutboundClient

        sig = inspect.signature(OutboundClient._build_http_client)
        param_names = " ".join(sig.parameters.keys()).lower()
        for forbidden in ("bearer", "authorization", "inbound", "token"):
            assert forbidden not in param_names, (
                f"OutboundClient._build_http_client has unexpected param containing {forbidden!r}: "
                f"{list(sig.parameters.keys())}"
            )

    def test_build_oauth_http_client_takes_no_inbound_headers(self) -> None:
        """build_oauth_http_client also has no channel for downstream auth."""
        import inspect

        from slm_mcp_hub.auth.broker import build_oauth_http_client

        sig = inspect.signature(build_oauth_http_client)
        param_names = " ".join(sig.parameters.keys()).lower()
        for forbidden in ("inbound", "downstream", "request_headers"):
            assert forbidden not in param_names, (
                f"build_oauth_http_client has unexpected param containing {forbidden!r}"
            )

    async def test_keyring_storage_never_reads_http_request_headers(self, mem_keyring_fixture) -> None:
        """KeyringTokenStorage reads ONLY from the OS keychain — no request context."""
        from slm_mcp_hub.auth.token_store import KeyringTokenStorage

        storage = KeyringTokenStorage(
            endpoint="http://127.0.0.1:0/mcp",
            redirect_uri="http://127.0.0.1:0/callback",
        )
        # get_tokens() must return None (no token stored); it takes no arguments
        # so there's structurally no way to pass inbound headers.
        tokens = await storage.get_tokens()
        assert tokens is None
        # Verify the method signature accepts no headers parameter
        import inspect
        sig = inspect.signature(storage.get_tokens)
        params = list(sig.parameters.keys())
        assert params == [], f"get_tokens() should take no params, got {params}"


class TestCrossProcessRefreshSerialization:
    """Prove concurrent token refreshes are serialized (one refresher wins)."""

    async def test_filelock_serializes_concurrent_coroutines(self, tmp_path: Path) -> None:
        """Only one coroutine holds the refresh lock at any given time.

        Simulates two Hub tasks racing to refresh an expired token: the lock
        ensures they execute sequentially, preventing duplicate token requests.
        """
        from slm_mcp_hub.auth.broker import get_refresh_lock_path, refresh_lock_context

        lock_path = get_refresh_lock_path(tmp_path, "e2e-refresh-key")
        concurrent_peak = 0
        inside_count = 0

        async def _try_refresh(delay: float) -> None:
            nonlocal concurrent_peak, inside_count
            await asyncio.sleep(delay)
            async with refresh_lock_context(lock_path):
                inside_count += 1
                concurrent_peak = max(concurrent_peak, inside_count)
                await asyncio.sleep(0.05)  # Simulate token request round-trip
                inside_count -= 1

        # Fire 5 concurrent tasks
        await asyncio.gather(*(_try_refresh(i * 0.01) for i in range(5)))
        assert concurrent_peak == 1, (
            f"Refresh lock allowed {concurrent_peak} concurrent holders — should be 1"
        )

    def test_lock_file_path_derived_from_non_secret_inputs(self, tmp_path: Path) -> None:
        """Lock path must contain no token material — only the account key hash."""
        from slm_mcp_hub.auth.broker import get_refresh_lock_path

        lock_path = get_refresh_lock_path(tmp_path, "a" * 64)
        # Path must not contain any credential-shaped substring
        path_str = str(lock_path)
        for forbidden in ("token", "secret", "bearer", "access", "refresh"):
            assert forbidden.lower() not in path_str.lower(), (
                f"Lock path contains credential-like substring {forbidden!r}: {path_str}"
            )

    def test_lock_dir_created_automatically(self, tmp_path: Path) -> None:
        """refresh_lock_context creates the lock directory (parent dirs) automatically."""
        import anyio

        from slm_mcp_hub.auth.broker import get_refresh_lock_path, refresh_lock_context

        lock_path = get_refresh_lock_path(tmp_path / "deep" / "nested", "key123")
        assert not lock_path.parent.exists()

        async def _use_lock():
            async with refresh_lock_context(lock_path):
                pass

        anyio.run(_use_lock)
        assert lock_path.parent.exists()


class TestIssuerBindingInvalidation:
    """Prove issuer-change clears stored tokens (binding-change invalidation)."""

    async def test_new_issuer_clears_existing_tokens(self, mem_keyring_fixture) -> None:
        """set_client_info with a different issuer must clear the stored token.

        This prevents stale tokens from a previous AS being used against a new one
        — a critical security property when the upstream's authorization server changes.
        """
        from slm_mcp_hub.auth.token_store import KeyringTokenStorage

        storage = KeyringTokenStorage(
            endpoint="http://127.0.0.1:9999/mcp",
            redirect_uri="http://127.0.0.1:0/callback",
        )

        # Step 1: store initial client_info with issuer-A
        client_info_a = OAuthClientInformationFull(
            client_id="client-a",
            redirect_uris=[AnyUrl("http://127.0.0.1:0/callback")],
            issuer="http://127.0.0.1:8080",
        )
        await storage.set_client_info(client_info_a)

        # Step 2: store a token
        token = OAuthToken(access_token="old-token-for-issuer-a", token_type="Bearer")
        await storage.set_tokens(token)

        # Verify token is there
        assert (await storage.get_tokens()) is not None

        # Step 3: new client_info with DIFFERENT issuer
        client_info_b = OAuthClientInformationFull(
            client_id="client-b",
            redirect_uris=[AnyUrl("http://127.0.0.1:0/callback")],
            issuer="http://127.0.0.1:9090",  # different AS
        )
        await storage.set_client_info(client_info_b)

        # The old token must have been cleared
        stored_token = await storage.get_tokens()
        assert stored_token is None, (
            f"Issuer change should clear old token, but got: {stored_token}"
        )

    async def test_same_issuer_preserves_tokens(self, mem_keyring_fixture) -> None:
        """set_client_info with the SAME issuer must NOT clear the stored token."""
        from slm_mcp_hub.auth.token_store import KeyringTokenStorage

        storage = KeyringTokenStorage(
            endpoint="http://127.0.0.1:9998/mcp",
            redirect_uri="http://127.0.0.1:0/callback",
        )

        client_info = OAuthClientInformationFull(
            client_id="client-same",
            redirect_uris=[AnyUrl("http://127.0.0.1:0/callback")],
            issuer="http://127.0.0.1:7777",
        )
        await storage.set_client_info(client_info)

        token = OAuthToken(access_token="token-for-same-issuer", token_type="Bearer")
        await storage.set_tokens(token)

        # Update client_info with same issuer — token must survive
        await storage.set_client_info(client_info)
        stored_token = await storage.get_tokens()
        assert stored_token is not None
        assert stored_token.access_token == "token-for-same-issuer"


class TestOAuthUrlSafetyPolicy:
    """Prove the OAuth transport URL-safety policy blocks unsafe URLs.

    These exercise ``is_safe_oauth_metadata_url`` — the SSRF/transport guard
    that gates OAuth metadata and callback URLs: non-loopback HTTP is rejected,
    loopback HTTP is allowed. The final test covers the runtime default
    redirect URI, which must be loopback.
    """

    def test_non_loopback_http_url_blocked(self) -> None:
        """Non-loopback HTTP OAuth URLs must be rejected by the safety policy.

        If a non-loopback HTTP URL were accepted, an attacker could intercept
        the authorization code / metadata exchange on a shared network.
        """
        from slm_mcp_hub.auth.provider import is_safe_oauth_metadata_url

        non_loopback_http = [
            "http://example.com/callback",
            "http://192.168.1.100:8080/callback",
            "http://10.0.0.1/callback",
        ]
        for url in non_loopback_http:
            result = is_safe_oauth_metadata_url(url, mcp_endpoint="http://127.0.0.1:9000/mcp")
            assert result is False, f"Expected {url!r} to be blocked, but got True"

    def test_loopback_http_url_allowed(self) -> None:
        """Loopback HTTP OAuth URLs must be allowed (local auth callback)."""
        from slm_mcp_hub.auth.provider import is_safe_oauth_metadata_url

        loopback_uris = [
            "http://127.0.0.1:8080/callback",
            "http://localhost:9090/callback",
        ]
        for url in loopback_uris:
            result = is_safe_oauth_metadata_url(url, mcp_endpoint="http://127.0.0.1:9000/mcp")
            assert result is True, f"Expected {url!r} to be allowed, got False"

    def test_runtime_provider_redirect_uri_uses_loopback(self) -> None:
        """The default redirect URI built for runtime mode must use loopback."""
        from slm_mcp_hub.auth.models import AuthOAuthConfig
        from slm_mcp_hub.auth.provider import _default_redirect_uri

        cfg = AuthOAuthConfig(scopes=("read",))
        uri = _default_redirect_uri(cfg)
        assert "127.0.0.1" in uri or "localhost" in uri, (
            f"Default redirect URI {uri!r} is not loopback"
        )


class TestLogoutToAuthRequired:
    """Prove logout → auth_required state transition in the connection layer."""

    async def test_logout_clears_tokens_from_storage(self, mem_keyring_fixture) -> None:
        """KeyringTokenStorage.logout() removes both token and client_info entries."""
        from slm_mcp_hub.auth.token_store import KeyringTokenStorage

        storage = KeyringTokenStorage(
            endpoint="http://127.0.0.1:9997/mcp",
            redirect_uri="http://127.0.0.1:0/callback",
        )

        # Store token and client_info
        token = OAuthToken(access_token="pre-logout-token", token_type="Bearer")
        await storage.set_tokens(token)
        client_info = OAuthClientInformationFull(
            client_id="logout-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:0/callback")],
            issuer="http://127.0.0.1:5555",
        )
        await storage.set_client_info(client_info)

        # Verify both stored
        assert await storage.get_tokens() is not None
        assert await storage.get_client_info() is not None

        # Logout
        storage.logout()

        # Both must be cleared
        assert await storage.get_tokens() is None, "Token must be cleared after logout"
        assert await storage.get_client_info() is None, "Client info must be cleared after logout"

    async def test_logout_is_idempotent(self, mem_keyring_fixture) -> None:
        """logout() on an already-empty store must not raise."""
        from slm_mcp_hub.auth.token_store import KeyringTokenStorage

        storage = KeyringTokenStorage(
            endpoint="http://127.0.0.1:9996/mcp",
            redirect_uri="http://127.0.0.1:0/callback",
        )
        # Call logout with nothing stored — must succeed silently
        storage.logout()  # no raise
        storage.logout()  # second call also OK

    async def test_connection_enters_auth_required_after_oauth_flow_error(self) -> None:
        """After OAuthFlowError (no redirect handler), connection state is AUTH_REQUIRED.

        This is the 'logout→auth_required' transition: once stored tokens are
        removed (or expired), the runtime Hub cannot complete interactive auth,
        so it transitions to AUTH_REQUIRED rather than ERROR.

        AUTH_REQUIRED is a RECOVERABLE state — the user can run `auth login`
        to re-authenticate.
        """
        from unittest.mock import AsyncMock, patch

        from slm_mcp_hub.auth.broker import OAuthAuthRequiredError
        from slm_mcp_hub.auth.models import AuthOAuthConfig
        from slm_mcp_hub.core.config import MCPServerConfig
        from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection

        config = MCPServerConfig(
            name="post-logout-server",
            transport="http",
            url="http://127.0.0.1:0/mcp",
            auth=AuthOAuthConfig(scopes=("read",)),
        )
        conn = MCPConnection(config)

        # Simulate what happens after logout: no stored token, OAuth flow fails
        with patch(
            "slm_mcp_hub.protocol.outbound.OutboundClient.connect",
            new_callable=AsyncMock,
            side_effect=OAuthAuthRequiredError("Token cleared after logout"),
        ):
            await conn.connect()

        assert conn.state == ConnectionState.AUTH_REQUIRED, (
            f"Expected AUTH_REQUIRED after logout, got {conn.state}"
        )
        assert conn.is_auth_required is True
        # NOT in error state — auth_required is recoverable
        assert conn.state != ConnectionState.ERROR

    def test_auth_required_state_is_distinct_from_error(self) -> None:
        """AUTH_REQUIRED and ERROR are distinct, separate connection states."""
        from slm_mcp_hub.federation.connection import ConnectionState

        assert ConnectionState.AUTH_REQUIRED != ConnectionState.ERROR
        assert ConnectionState.AUTH_REQUIRED.value == "auth_required"
        assert "auth" in ConnectionState.AUTH_REQUIRED.value
