"""SLM Memory & Learning Plugin for SLM MCP Hub.

Communicates with the SLM daemon via HTTP API (localhost:8765).
No Python import of superlocalmemory required — pure HTTP integration.

Verified endpoints (April 15, 2026):
  GET  /status                → daemon health
  POST /api/v3/tool-event     → log tool calls (learning pipeline input)
  POST /api/v3/recall/trace   → recall context with channel scores
  POST /api/search            → search memories
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from slm_mcp_hub.plugins.base import HubPlugin

if TYPE_CHECKING:
    from slm_mcp_hub.core.hub import HubOrchestrator

logger = logging.getLogger(__name__)

RING_BUFFER_MAX = 10_000
DEFAULT_SLM_URL = "http://localhost:8765"
HTTP_TIMEOUT = 2.0


@dataclass(frozen=True)
class ToolObservation:
    """A single observed tool call."""

    session_id: str
    server: str
    tool: str
    duration_ms: int
    success: bool
    timestamp: float


@dataclass(frozen=True)
class SessionSummary:
    """Summary of a completed session."""

    session_id: str
    tool_counts: dict[str, int]
    total_duration_ms: int
    total_calls: int
    project_path: str | None


class SLMPlugin(HubPlugin):
    """SLM memory and learning integration via HTTP API.

    Connects to the SLM daemon at localhost:8765 (configurable via
    SLM_DAEMON_URL env var). No Python import of superlocalmemory needed.

    When the daemon is reachable:
    - Logs every tool call to /api/v3/tool-event (learning pipeline)
    - Recalls past context on session start via /api/v3/recall/trace
    - Logs session summaries on end via /api/v3/tool-event

    When the daemon is NOT reachable, all hooks are no-ops
    but the local ring buffer still tracks observations.
    """

    def __init__(self, slm_url: str | None = None) -> None:
        self._slm_url = slm_url or os.environ.get("SLM_DAEMON_URL", DEFAULT_SLM_URL)
        self._available = False
        self._hub: HubOrchestrator | None = None
        self._client: httpx.AsyncClient | None = None
        self._observations: list[ToolObservation] = []
        self._session_contexts: dict[str, dict[str, Any]] = {}
        self._session_tool_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._session_durations: dict[str, int] = defaultdict(int)

    @property
    def name(self) -> str:
        return "slm"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def available(self) -> bool:
        return self._available

    @property
    def observation_count(self) -> int:
        return len(self._observations)

    @property
    def slm_url(self) -> str:
        return self._slm_url

    async def on_hub_start(self, hub: HubOrchestrator) -> None:
        """Check if SLM daemon is reachable via HTTP."""
        self._hub = hub
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

        try:
            resp = await self._client.get(f"{self._slm_url}/status")
            if resp.status_code == 200:
                data = resp.json()
                self._available = data.get("status") == "running"
                if self._available:
                    logger.info(
                        "SLM plugin connected to daemon at %s (mode=%s, facts=%d)",
                        self._slm_url,
                        data.get("mode", "?"),
                        data.get("fact_count", 0),
                    )
                else:
                    logger.warning("SLM daemon at %s not in running state", self._slm_url)
            else:
                logger.warning("SLM daemon returned %d", resp.status_code)
                self._available = False
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.info("SLM plugin: daemon not reachable at %s — standalone mode (%s)", self._slm_url, exc)
            self._available = False
        except Exception as exc:
            logger.warning("SLM plugin: unexpected error checking daemon: %s", exc)
            self._available = False

    async def on_hub_stop(self) -> None:
        """Close HTTP client."""
        self._available = False
        if self._client:
            await self._client.aclose()
            self._client = None

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
        """Observe tool call: store locally + send to SLM daemon."""
        observation = ToolObservation(
            session_id=session_id,
            server=server,
            tool=tool,
            duration_ms=duration_ms,
            success=success,
            timestamp=time.time(),
        )
        self._store_observation(observation)

        tool_key = f"{server}__{tool}"
        self._session_tool_counts[session_id][tool_key] += 1
        self._session_durations[session_id] += duration_ms

        if not self._available or not self._client:
            return

        # Fire-and-forget POST to SLM daemon (don't block tool routing)
        try:
            asyncio.create_task(self._post_tool_event(
                tool_name=tool_key,
                event_type="complete" if success else "error",
                duration_ms=duration_ms,
                session_id=session_id,
            ))
        except Exception as exc:
            logger.warning("SLM tool-event fire failed: %s", exc)

    async def on_session_start(self, session_id: str, client_info: dict[str, Any]) -> None:
        """Recall relevant context from SLM for this session."""
        project_path = client_info.get("project_path")
        self._session_contexts[session_id] = {
            "project_path": project_path,
            "started_at": time.time(),
        }

        if not self._available or not self._client:
            return

        try:
            resp = await self._client.post(
                f"{self._slm_url}/api/v3/recall/trace",
                json={
                    "query": f"hub session {project_path or 'unknown'}",
                    "limit": 5,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                self._session_contexts[session_id]["recalled"] = data
                logger.debug(
                    "SLM recall for session %s: %d results in %dms",
                    session_id,
                    data.get("result_count", 0),
                    data.get("retrieval_time_ms", 0),
                )
        except Exception as exc:
            logger.warning("SLM recall on session start failed: %s", exc)

    async def on_session_end(self, session_id: str) -> None:
        """Summarize session and log to SLM daemon."""
        tool_counts = dict(self._session_tool_counts.pop(session_id, {}))
        total_duration = self._session_durations.pop(session_id, 0)
        ctx = self._session_contexts.pop(session_id, {})
        total_calls = sum(tool_counts.values())

        summary = SessionSummary(
            session_id=session_id,
            tool_counts=tool_counts,
            total_duration_ms=total_duration,
            total_calls=total_calls,
            project_path=ctx.get("project_path"),
        )

        if not self._available or not self._client:
            return

        tools_list = ", ".join(sorted(tool_counts.keys())[:20])
        try:
            asyncio.create_task(self._post_tool_event(
                tool_name="hub__session_summary",
                event_type="session_end",
                session_id=session_id,
                output_summary=(
                    f"{total_calls} calls, {total_duration}ms total, "
                    f"tools: {tools_list}"
                ),
                project_path=ctx.get("project_path", ""),
            ))
        except Exception as exc:
            logger.warning("SLM session summary failed: %s", exc)

    def get_learned_tools(self, project_path: str | None = None) -> set[str]:
        """Return tool names observed in recent history."""
        cutoff = time.time() - (7 * 24 * 3600)
        return {
            f"{obs.server}__{obs.tool}"
            for obs in self._observations
            if obs.timestamp >= cutoff
        }

    def get_warm_up_predictions(self) -> list[str]:
        """Return MCP server names predicted to be needed soon."""
        if not self._observations:
            return []

        server_counts: dict[str, int] = defaultdict(int)
        cutoff = time.time() - (24 * 3600)
        for obs in self._observations:
            if obs.timestamp >= cutoff:
                server_counts[obs.server] += 1

        sorted_servers = sorted(server_counts.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in sorted_servers[:5]]

    # -- Private helpers --

    def _store_observation(self, observation: ToolObservation) -> None:
        """Store observation in ring buffer, evicting oldest if full."""
        self._observations.append(observation)
        if len(self._observations) > RING_BUFFER_MAX:
            self._observations = self._observations[-RING_BUFFER_MAX:]

    async def _post_tool_event(
        self,
        tool_name: str,
        event_type: str = "complete",
        duration_ms: int = 0,
        session_id: str = "",
        input_summary: str = "",
        output_summary: str = "",
        project_path: str = "",
    ) -> None:
        """POST a tool event to the SLM daemon. Fire-and-forget."""
        if not self._client:
            return
        try:
            await self._client.post(
                f"{self._slm_url}/api/v3/tool-event",
                json={
                    "tool_name": tool_name,
                    "event_type": event_type,
                    "input_summary": input_summary[:500],
                    "output_summary": output_summary[:500],
                    "session_id": session_id,
                    "project_path": project_path,
                },
            )
        except Exception as exc:
            logger.debug("SLM tool-event POST failed: %s", exc)
