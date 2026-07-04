"""MCP connection manager — manages one MCP server connection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from enum import Enum
from typing import Any

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.core.constants import DEFAULT_TOOL_TIMEOUT_S, MCP_REQUEST_TIMEOUT_MS, VERSION

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DRAINING = "draining"
    ERROR = "error"


class MCPConnection:
    """Manages a single MCP server connection (stdio or HTTP).

    For stdio: spawns a child process, communicates via JSON-RPC over stdin/stdout.
    For HTTP/SSE: connects to a remote URL (future phase).
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._state = ConnectionState.DISCONNECTED
        self._process: asyncio.subprocess.Process | None = None
        self._capabilities: dict[str, Any] = {
            "tools": [],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._connected_at: float = 0.0
        self._in_flight: int = 0
        self._drain_event: asyncio.Event | None = None
        # Serializes drain_and_disconnect calls per connection — prevents
        # concurrent drains from overwriting each other's _drain_event
        # and hanging the first caller until timeout.
        self._drain_lock: asyncio.Lock | None = None
        # Rolling tail of recent stderr lines for diagnostic error messages
        # when a child process exits unexpectedly.
        self._stderr_tail: deque[str] = deque(maxlen=20)

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def state(self) -> str:
        return self._state

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._capabilities

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def is_draining(self) -> bool:
        return self._state == ConnectionState.DRAINING

    @property
    def in_flight_count(self) -> int:
        return self._in_flight

    @property
    def uptime_seconds(self) -> float:
        if self._connected_at == 0:
            return 0.0
        return time.time() - self._connected_at

    async def connect(self) -> None:
        """Connect to the MCP server."""
        if self._state == ConnectionState.CONNECTED:
            return

        self._state = ConnectionState.CONNECTING

        if self._config.transport == "stdio":
            await self._connect_stdio()
        else:
            await self._connect_http()

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except ProcessLookupError:
                # Process already exited — nothing to do
                pass
            except asyncio.TimeoutError:
                # Force-kill, but tolerate the race where the kid just died
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            self._process = None

        # Close HTTP client if present
        if hasattr(self, "_http_client") and self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

        # Fail all pending requests
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("MCP server disconnected"))
        self._pending = {}

        self._state = ConnectionState.DISCONNECTED
        self._connected_at = 0.0
        logger.info("Disconnected from MCP: %s", self.name)

    async def drain_and_disconnect(self, timeout_s: float = 30.0) -> None:
        """Stop accepting new requests, wait for in-flight calls, then disconnect.

        Serialized per connection via _drain_lock so concurrent callers don't
        overwrite each other's drain event. The second caller simply waits
        for the first to complete, then sees state=DISCONNECTED and returns.

        Keeps kite SSE sessions alive — drain only affects this one server's
        connection, not the hub's other connections.
        """
        if self._drain_lock is None:
            self._drain_lock = asyncio.Lock()

        async with self._drain_lock:
            # After acquiring the lock, re-check state in case a prior drain
            # already disconnected us.
            if self._state not in (ConnectionState.CONNECTED, ConnectionState.DRAINING):
                await self.disconnect()
                return

            self._state = ConnectionState.DRAINING
            if self._in_flight > 0:
                self._drain_event = asyncio.Event()
                logger.info(
                    "Draining %s: %d in-flight calls, waiting up to %.0fs",
                    self.name, self._in_flight, timeout_s,
                )
                try:
                    await asyncio.wait_for(self._drain_event.wait(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Drain timeout for %s after %.0fs — forcing disconnect with %d in-flight",
                        self.name, timeout_s, self._in_flight,
                    )

            await self.disconnect()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        """Call a tool on this MCP server and return the result.

        Args:
            tool_name: Name of the tool to call.
            arguments: Arguments to pass to the tool.
            timeout_s: Per-call timeout in seconds. Uses server default if None.
        """
        return await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, timeout_s=timeout_s)

    async def read_resource(self, uri: str, timeout_s: float | None = None) -> dict[str, Any]:
        """Read a resource from this MCP server."""
        return await self._send_request("resources/read", {"uri": uri}, timeout_s=timeout_s)

    async def get_prompt(self, name: str, arguments: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        """Get a prompt from this MCP server."""
        return await self._send_request("prompts/get", {
            "name": name,
            "arguments": arguments,
        }, timeout_s=timeout_s)

    async def _connect_stdio(self) -> None:
        """Start a child process and perform MCP initialization handshake."""
        cmd = self._config.command
        args = list(self._config.args)

        env = dict(os.environ)
        env.update(self._config.env)

        try:
            self._process = await asyncio.create_subprocess_exec(
                cmd, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=10 * 1024 * 1024,  # 10MB readline buffer for large MCP responses
            )
        except FileNotFoundError:
            self._state = ConnectionState.ERROR
            raise ConnectionError(f"Command not found: {cmd}")
        except OSError as exc:
            self._state = ConnectionState.ERROR
            raise ConnectionError(f"Failed to start MCP {self.name}: {exc}")

        # Start reading stdout (JSON-RPC responses)
        self._reader_task = asyncio.create_task(self._read_stdout())
        # Drain stderr to prevent child process blocking on full pipe buffer
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # MCP initialization handshake
        try:
            init_result = await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "slm-mcp-hub", "version": VERSION},
            })

            # Send initialized notification (no response expected)
            await self._send_notification("notifications/initialized", {})

            # Discover capabilities
            await self._discover_capabilities()

            self._state = ConnectionState.CONNECTED
            self._connected_at = time.time()
            logger.info(
                "Connected to MCP: %s (%d tools, %d resources, %d prompts)",
                self.name,
                len(self._capabilities["tools"]),
                len(self._capabilities["resources"]),
                len(self._capabilities["prompts"]),
            )
        except Exception as exc:
            self._state = ConnectionState.ERROR
            await self.disconnect()
            raise ConnectionError(f"MCP {self.name} initialization failed: {exc}")

    async def _connect_http(self) -> None:
        """Connect to a remote HTTP MCP server via Streamable HTTP."""
        try:
            import httpx
        except ImportError:
            self._state = ConnectionState.ERROR
            raise ConnectionError(
                f"httpx required for HTTP transport. Install with: pip install httpx"
            )

        self._http_url = self._config.url
        self._http_client = httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/event-stream",
                **self._config.headers,
            },
            timeout=httpx.Timeout(MCP_REQUEST_TIMEOUT_MS / 1000),
        )
        self._http_session_id: str | None = None

        try:
            init_result = await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "slm-mcp-hub", "version": VERSION},
            })

            await self._send_notification("notifications/initialized", {})
            await self._discover_capabilities()

            self._state = ConnectionState.CONNECTED
            self._connected_at = time.time()
            logger.info(
                "Connected to HTTP MCP: %s (%d tools, %d resources, %d prompts)",
                self.name,
                len(self._capabilities["tools"]),
                len(self._capabilities["resources"]),
                len(self._capabilities["prompts"]),
            )
        except Exception as exc:
            self._state = ConnectionState.ERROR
            if hasattr(self, "_http_client"):
                await self._http_client.aclose()
            raise ConnectionError(f"HTTP MCP {self.name} initialization failed: {exc}")

    async def _discover_capabilities(self) -> None:
        """Discover all tools, resources, and prompts from the MCP server."""
        try:
            tools_result = await self._send_request("tools/list", {})
            # Guard: some HTTP MCPs return a non-dict result (e.g. a bare string).
            # Calling .get() on a str raises AttributeError and marks the server ERROR.
            if isinstance(tools_result, dict):
                self._capabilities["tools"] = tools_result.get("tools", [])
            else:
                logger.warning(
                    "tools/list for %s returned unexpected type %s — treating as no tools",
                    self.name, type(tools_result).__name__,
                )
        except Exception as exc:
            logger.warning("Failed to list tools for %s: %s", self.name, exc)

        try:
            res_result = await self._send_request("resources/list", {})
            if isinstance(res_result, dict):
                self._capabilities["resources"] = res_result.get("resources", [])
        except Exception as exc:
            logger.debug("No resources for %s: %s", self.name, exc)

        try:
            tmpl_result = await self._send_request("resources/templates/list", {})
            if isinstance(tmpl_result, dict):
                self._capabilities["resource_templates"] = tmpl_result.get("resourceTemplates", [])
        except Exception as exc:
            logger.debug("No resource templates for %s: %s", self.name, exc)

        try:
            prompts_result = await self._send_request("prompts/list", {})
            if isinstance(prompts_result, dict):
                self._capabilities["prompts"] = prompts_result.get("prompts", [])
        except Exception as exc:
            logger.debug("No prompts for %s: %s", self.name, exc)

    async def _send_request(self, method: str, params: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response.

        Args:
            method: JSON-RPC method name.
            params: Method parameters.
            timeout_s: Per-call timeout override. Uses server default if None.
        """
        if self._config.transport in ("http", "sse"):
            return await self._send_request_http(method, params)
        return await self._send_request_stdio(method, params, timeout_s=timeout_s)

    async def _send_request_stdio(self, method: str, params: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        """Send via stdio subprocess.

        Args:
            method: JSON-RPC method name.
            params: Method parameters.
            timeout_s: Per-call timeout in seconds. Falls back to MCP_REQUEST_TIMEOUT_MS
                       if None (long timeout for video gen / deep research).
        """
        if self._state == ConnectionState.DRAINING:
            raise ConnectionError(f"MCP {self.name} is draining — no new requests accepted")
        if not self._process or not self._process.stdin:
            raise ConnectionError(f"MCP {self.name} not connected")

        self._request_id += 1
        req_id = self._request_id

        message = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        self._in_flight += 1

        data = json.dumps(message) + "\n"
        self._process.stdin.write(data.encode())
        await self._process.stdin.drain()

        try:
            effective_timeout = timeout_s if timeout_s is not None else (MCP_REQUEST_TIMEOUT_MS / 1000)
            result = await asyncio.wait_for(future, timeout=effective_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP {self.name} request {method} timed out")
        finally:
            self._in_flight -= 1
            if self._in_flight == 0 and self._drain_event is not None:
                self._drain_event.set()

        if "error" in result:
            err = result["error"]
            raise RuntimeError(
                f"MCP {self.name} error: [{err.get('code', -1)}] {err.get('message', 'unknown')}"
            )

        return result.get("result", {})

    async def _send_request_http(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send via HTTP POST to remote MCP server."""
        if not hasattr(self, "_http_client"):
            raise ConnectionError(f"MCP {self.name} HTTP client not initialized")

        self._request_id += 1
        req_id = self._request_id

        message = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if hasattr(self, "_http_session_id") and self._http_session_id:
            headers["Mcp-Session-Id"] = self._http_session_id

        response = await self._http_client.post(self._http_url, json=message, headers=headers)

        # Capture session ID from response
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._http_session_id = session_id

        if response.status_code == 204:
            return {}

        # Handle SSE responses — extract the JSON-RPC message from event stream
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            result = self._parse_sse_response(response.text)
        else:
            result = response.json()

        if "error" in result:
            err = result["error"]
            raise RuntimeError(
                f"MCP {self.name} error: [{err.get('code', -1)}] {err.get('message', 'unknown')}"
            )

        return result.get("result", {})

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._config.transport in ("http", "sse"):
            await self._send_notification_http(method, params)
            return

        if not self._process or not self._process.stdin:
            raise ConnectionError(f"MCP {self.name} not connected")

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        data = json.dumps(message) + "\n"
        self._process.stdin.write(data.encode())
        await self._process.stdin.drain()

    async def _send_notification_http(self, method: str, params: dict[str, Any]) -> None:
        """Send notification via HTTP POST."""
        if not hasattr(self, "_http_client"):
            return

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        headers = {"Content-Type": "application/json"}
        if hasattr(self, "_http_session_id") and self._http_session_id:
            headers["Mcp-Session-Id"] = self._http_session_id

        try:
            await self._http_client.post(self._http_url, json=message, headers=headers)
        except Exception:
            pass  # Notifications don't require response

    @staticmethod
    def _parse_sse_response(text: str) -> dict[str, Any]:
        """Parse a Server-Sent Events response to extract JSON-RPC message."""
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str:
                    try:
                        return json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
        # Fallback: try parsing the whole response as JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": {"code": -32700, "message": "Could not parse SSE response"}}

    async def _drain_stderr(self) -> None:
        """Drain child process stderr; keep a rolling tail for diagnostics."""
        if not self._process or not self._process.stderr:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("[%s stderr] %s", self.name, text)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _exit_diagnostic(self) -> str:
        """Build a helpful error message including command + exit code + stderr tail.

        Surfaces actionable context when an MCP child process dies early:
        - what command we tried to run
        - the OS exit code if available
        - the last few stderr lines (often contain the actual error)
        """
        parts: list[str] = [f"MCP '{self.name}' child process exited"]

        exit_code = None
        if self._process is not None:
            exit_code = self._process.returncode
        if exit_code is not None:
            parts.append(f"with exit code {exit_code}")

        if self._config.transport == "stdio":
            cmd_summary = self._config.command
            if self._config.args:
                cmd_summary += " " + " ".join(self._config.args[:4])
                if len(self._config.args) > 4:
                    cmd_summary += " ..."
            parts.append(f"(command: {cmd_summary!r})")
        elif self._config.url:
            parts.append(f"(url: {self._config.url!r})")

        if self._stderr_tail:
            tail = " | ".join(list(self._stderr_tail)[-3:])
            parts.append(f"stderr tail: {tail}")
        else:
            parts.append("no stderr output captured — verify the command is an MCP server, not a one-shot command")

        return "; ".join(parts)

    async def _read_stdout(self) -> None:
        """Read JSON-RPC messages from the child process stdout."""
        assert self._process and self._process.stdout

        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    # EOF — child process exited; fail all pending futures
                    break

                text = line.decode("utf-8").strip()
                if not text:
                    continue

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON from %s: %s", self.name, text[:200])
                    continue

                req_id = msg.get("id")
                if req_id is not None and req_id in self._pending:
                    future = self._pending.pop(req_id)
                    if not future.done():
                        future.set_result(msg)
                elif "method" in msg:
                    logger.debug("Notification from %s: %s", self.name, msg.get("method"))

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Reader error for %s: %s", self.name, exc)
            self._state = ConnectionState.ERROR
        finally:
            # Fail all pending futures with a diagnostic that includes the
            # exit code, command, and last few stderr lines — saves a lot of
            # debugging when a misconfigured command silently fails.
            if self._pending:
                # Give stderr drain a tick to flush before we read the tail
                await asyncio.sleep(0)
                err = ConnectionError(self._exit_diagnostic())
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(err)
                self._pending = {}
