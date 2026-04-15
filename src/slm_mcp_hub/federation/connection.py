"""MCP connection manager — manages one MCP server connection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from enum import Enum
from typing import Any

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.core.constants import MCP_REQUEST_TIMEOUT_MS, VERSION

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
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
        self._connected_at: float = 0.0

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

        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._process.kill()
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

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on this MCP server and return the result."""
        return await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read a resource from this MCP server."""
        return await self._send_request("resources/read", {"uri": uri})

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get a prompt from this MCP server."""
        return await self._send_request("prompts/get", {
            "name": name,
            "arguments": arguments,
        })

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

        # Start reading stdout
        self._reader_task = asyncio.create_task(self._read_stdout())

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
            self._capabilities["tools"] = tools_result.get("tools", [])
        except Exception as exc:
            logger.warning("Failed to list tools for %s: %s", self.name, exc)

        try:
            res_result = await self._send_request("resources/list", {})
            self._capabilities["resources"] = res_result.get("resources", [])
        except Exception as exc:
            logger.debug("No resources for %s: %s", self.name, exc)

        try:
            tmpl_result = await self._send_request("resources/templates/list", {})
            self._capabilities["resource_templates"] = tmpl_result.get("resourceTemplates", [])
        except Exception as exc:
            logger.debug("No resource templates for %s: %s", self.name, exc)

        try:
            prompts_result = await self._send_request("prompts/list", {})
            self._capabilities["prompts"] = prompts_result.get("prompts", [])
        except Exception as exc:
            logger.debug("No prompts for %s: %s", self.name, exc)

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        if self._config.transport in ("http", "sse"):
            return await self._send_request_http(method, params)
        return await self._send_request_stdio(method, params)

    async def _send_request_stdio(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send via stdio subprocess."""
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

        data = json.dumps(message) + "\n"
        self._process.stdin.write(data.encode())
        await self._process.stdin.drain()

        try:
            timeout_s = MCP_REQUEST_TIMEOUT_MS / 1000
            result = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP {self.name} request {method} timed out")

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
            await self._http_client.post("", json=message, headers=headers)
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

    async def _read_stdout(self) -> None:
        """Read JSON-RPC messages from the child process stdout."""
        assert self._process and self._process.stdout

        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
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
                    # Server-initiated notification
                    logger.debug("Notification from %s: %s", self.name, msg.get("method"))

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Reader error for %s: %s", self.name, exc)
            self._state = ConnectionState.ERROR
