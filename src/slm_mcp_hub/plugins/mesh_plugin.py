"""SLM Mesh Plugin for cross-session coordination and cross-machine sharing.

Communicates with the SLM daemon mesh endpoints via HTTP API (localhost:8765/mesh/*).
No Python import of superlocalmemory required — pure HTTP integration.

Verified endpoints (April 15, 2026):
  GET  /mesh/peers            → list mesh peers
  POST /mesh/register         → register as mesh peer
  POST /mesh/send             → send message to peers
  POST /mesh/lock             → acquire/release distributed lock
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from slm_mcp_hub.plugins.base import HubPlugin

if TYPE_CHECKING:
    from slm_mcp_hub.core.hub import HubOrchestrator

logger = logging.getLogger(__name__)

DEFAULT_SLM_URL = "http://localhost:8765"
HTTP_TIMEOUT = 2.0


@dataclass(frozen=True)
class MeshPeerInfo:
    """Information about a discovered mesh peer hub."""

    peer_id: str
    hostname: str
    port: int
    mcp_count: int
    last_seen: float


@dataclass(frozen=True)
class RemoteToolRoute:
    """Routing info for a tool on a remote hub."""

    peer_id: str
    server_name: str
    tool_name: str
    latency_ms: float


class MeshPlugin(HubPlugin):
    """SLM Mesh integration via HTTP API for cross-session coordination.

    Connects to the SLM daemon mesh endpoints at localhost:8765/mesh/*.
    No Python import of superlocalmemory needed.

    When the daemon mesh is reachable:
    - Registers hub as a mesh peer on startup
    - Broadcasts tool list changes to other sessions
    - Enables cross-machine MCP discovery
    - Provides distributed locking via HTTP

    When mesh is NOT available, all hooks are no-ops.
    """

    def __init__(self, slm_url: str | None = None) -> None:
        self._slm_url = slm_url or os.environ.get("SLM_DAEMON_URL", DEFAULT_SLM_URL)
        self._available = False
        self._hub: HubOrchestrator | None = None
        self._client: httpx.AsyncClient | None = None
        self._peer_id: str = ""
        self._session_id: str = str(uuid.uuid4())
        self._peers: dict[str, MeshPeerInfo] = {}
        self._remote_tools: dict[str, list[str]] = {}

    @property
    def name(self) -> str:
        return "mesh"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def available(self) -> bool:
        return self._available

    @property
    def peer_count(self) -> int:
        return len(self._peers)

    async def on_hub_start(self, hub: HubOrchestrator) -> None:
        """Register hub as a mesh peer via HTTP."""
        self._hub = hub
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

        try:
            resp = await self._client.post(
                f"{self._slm_url}/mesh/register",
                json={
                    "session_id": self._session_id,
                    "summary": "SLM MCP Hub",
                    "host": hub.config.host if hub else "127.0.0.1",
                    "port": hub.config.port if hub else 52414,
                    "project_path": "",
                    "agent_type": "mcp-hub",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                self._peer_id = data.get("peer_id", "")
                self._available = True
                logger.info("Mesh plugin registered as peer %s", self._peer_id)
            else:
                logger.warning("Mesh register returned %d: %s", resp.status_code, resp.text[:200])
                self._available = False
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.info("Mesh plugin: daemon not reachable at %s — disabled (%s)", self._slm_url, exc)
            self._available = False
        except Exception as exc:
            logger.warning("Mesh plugin: registration failed: %s", exc)
            self._available = False

    async def on_hub_stop(self) -> None:
        """Unregister from mesh and close client."""
        self._available = False
        self._peers = {}
        self._remote_tools = {}
        if self._client:
            await self._client.aclose()
            self._client = None

    async def on_tool_call_before(
        self,
        session_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Check if tool should be routed to a remote hub."""
        if not self._available:
            return None

        for peer_id, tools in self._remote_tools.items():
            if f"{server}__{tool}" in tools:
                return {
                    **args,
                    "_mesh_route": {
                        "peer_id": peer_id,
                        "server": server,
                        "tool": tool,
                    },
                }
        return None

    async def on_tool_call_after(
        self,
        session_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
        result: Any,
        duration_ms: int,
        success: bool,
    ) -> None:
        """Broadcast tool usage to mesh peers."""
        if not self._available:
            return
        await self._mesh_send(f"tool_used:{server}__{tool}:{duration_ms}ms")

    async def on_session_start(self, session_id: str, client_info: dict[str, Any]) -> None:
        """Notify mesh of new session."""
        if not self._available:
            return
        await self._mesh_send(f"session_start:{session_id}")

    async def on_session_end(self, session_id: str) -> None:
        """Notify mesh of session end."""
        if not self._available:
            return
        await self._mesh_send(f"session_end:{session_id}")

    async def on_mcp_connect(self, server_name: str) -> None:
        """Broadcast tool list change to mesh."""
        if not self._available:
            return
        await self._mesh_send(f"tool_list_changed:connect:{server_name}")

    async def on_mcp_disconnect(self, server_name: str) -> None:
        """Broadcast tool list change to mesh."""
        if not self._available:
            return
        await self._mesh_send(f"tool_list_changed:disconnect:{server_name}")

    def get_remote_tools(self) -> dict[str, list[str]]:
        """Return tools available on remote peers."""
        return dict(self._remote_tools)

    async def acquire_lock(self, resource: str, session_id: str, timeout_ms: int = 5000) -> bool:
        """Acquire a distributed lock via mesh HTTP API."""
        if not self._available or not self._client:
            return False

        try:
            resp = await self._client.post(
                f"{self._slm_url}/mesh/lock",
                json={
                    "file_path": resource,
                    "locked_by": session_id,
                    "action": "acquire",
                },
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("Mesh lock acquire failed: %s", exc)
            return False

    async def release_lock(self, resource: str, session_id: str) -> None:
        """Release a distributed lock via mesh HTTP API."""
        if not self._available or not self._client:
            return

        try:
            await self._client.post(
                f"{self._slm_url}/mesh/lock",
                json={
                    "file_path": resource,
                    "locked_by": session_id,
                    "action": "release",
                },
            )
        except Exception as exc:
            logger.warning("Mesh lock release failed: %s", exc)

    def update_peers(self, peers: dict[str, MeshPeerInfo]) -> None:
        """Update known mesh peers (called by mesh discovery)."""
        self._peers = dict(peers)

    def update_remote_tools(self, remote_tools: dict[str, list[str]]) -> None:
        """Update remote tool registry."""
        self._remote_tools = dict(remote_tools)

    # -- Private helpers --

    async def _mesh_send(self, content: str) -> None:
        """Send a broadcast message to mesh peers."""
        if not self._client:
            return
        try:
            await self._client.post(
                f"{self._slm_url}/mesh/send",
                json={
                    "from_peer": self._peer_id,
                    "to": "broadcast",
                    "content": content,
                    "type": "text",
                },
            )
        except Exception as exc:
            logger.debug("Mesh send failed: %s", exc)
