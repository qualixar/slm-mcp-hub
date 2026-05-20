"""Native stdio transport for the Hub — serves MCP JSON-RPC on stdin/stdout.

Wire format: newline-delimited JSON (NDJSON), matching the official MCP
Python SDK's stdio_server() — verified against
/opt/homebrew/lib/python3.14/site-packages/mcp/server/stdio.py.

Discipline (per Master Plan / Charter Feature B5):
- stdout is sacred — JSON-RPC responses only, never logging.
- All logs go to stderr (configured by caller before starting this server).
- Session attribution via SLM_HUB_AGENT_ID env var.

The transport reuses HubRuntime's MCPEndpoint (same as HTTP transport) and
subscribes to the runtime's notifier so it can forward
`notifications/tools/list_changed` to the client when the federated
registry changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)


class StdioServer:
    """Serve MCP JSON-RPC over stdin/stdout with NDJSON framing.

    One instance per `slm-hub mcp` invocation. The serve() coroutine runs
    until stdin EOF or a fatal error.
    """

    def __init__(
        self,
        mcp_endpoint: Any,
        session_manager: Any,
        notifier: Any | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._endpoint = mcp_endpoint
        self._sessions = session_manager
        self._notifier = notifier
        self._agent_id = agent_id or os.environ.get("SLM_HUB_AGENT_ID", "stdio-client")
        self._session_id: str | None = None
        self._write_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def serve(
        self,
        *,
        stdin: asyncio.StreamReader | None = None,
        stdout: Any | None = None,
    ) -> None:
        """Run the serve loop until EOF or stop signal.

        For production: stdin=None, stdout=None (uses sys.stdin/sys.stdout).
        For tests: pass async-friendly streams.
        """
        if stdin is None or stdout is None:
            reader, writer = await self._wrap_real_stdio()
        else:
            reader, writer = stdin, stdout

        # Create a session immediately. MCP `initialize` will reuse it.
        self._session_id = self._sessions.create_session(client_name=self._agent_id)

        # Subscribe to the notifier so registry changes propagate to this client.
        if self._notifier is not None:
            self._notifier.subscribe(
                self._session_id,
                lambda msg: self._send_notification(writer, msg),
            )

        logger.info(
            "stdio transport: serving session %s for agent=%s",
            self._session_id[:8] if self._session_id else "?", self._agent_id,
        )

        try:
            await self._serve_loop(reader, writer)
        finally:
            if self._notifier is not None and self._session_id:
                self._notifier.unsubscribe(self._session_id)
            if self._session_id:
                self._sessions.destroy_session(self._session_id)

    def stop(self) -> None:
        """Request graceful shutdown — serve loop exits at next iteration."""
        self._stop.set()

    async def _serve_loop(self, reader: Any, writer: Any) -> None:
        """Read NDJSON requests from reader, dispatch to endpoint, write
        NDJSON responses to writer."""
        while not self._stop.is_set():
            try:
                line = await reader.readline()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("stdio read error: %s", exc)
                break

            if not line:
                # EOF — client disconnected
                logger.info("stdio EOF — client disconnected")
                break

            text = line.decode("utf-8", errors="replace").strip() if isinstance(line, bytes) else line.strip()
            if not text:
                continue

            try:
                body = json.loads(text)
            except json.JSONDecodeError as exc:
                await self._send_error(writer, None, -32700, f"Parse error: {exc}")
                continue

            await self._handle_one(writer, body)

    async def _handle_one(self, writer: Any, body: dict[str, Any]) -> None:
        """Dispatch one JSON-RPC message to the endpoint and write the response."""
        try:
            result = await self._endpoint.handle_jsonrpc(self._session_id, body)
        except Exception as exc:
            logger.exception("Endpoint crashed")
            await self._send_error(writer, body.get("id"), -32603, f"Internal error: {exc}")
            return

        if result is None:
            # Notification — no response expected
            return
        await self._write_message(writer, result)

    async def _send_notification(self, writer: Any, msg: dict[str, Any]) -> None:
        """Forward a server-to-client JSON-RPC notification."""
        payload = {"jsonrpc": "2.0", **msg}
        try:
            await self._write_message(writer, payload)
        except Exception as exc:
            logger.warning("Failed to forward notification: %s", exc)

    async def _send_error(
        self, writer: Any, req_id: Any, code: int, message: str,
    ) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }
        await self._write_message(writer, payload)

    async def _write_message(self, writer: Any, payload: dict[str, Any]) -> None:
        """Serialize and write one NDJSON line. Serialized via _write_lock
        so concurrent responses + notifications don't interleave bytes.

        asyncio.StreamWriter requires bytes; test fakes typically accept str.
        We auto-detect via the presence of an async drain() method.
        """
        text = json.dumps(payload, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                drain = getattr(writer, "drain", None)
                if drain is not None:
                    # asyncio.StreamWriter — wants bytes
                    writer.write(text.encode("utf-8"))
                    await drain()
                else:
                    # Plain file-like object (test fallback) — accepts str
                    writer.write(text)
                    flush = getattr(writer, "flush", None)
                    if flush is not None:
                        flush()
            except Exception as exc:
                logger.warning("stdout write failed: %s", exc)

    async def _wrap_real_stdio(self) -> tuple[Any, Any]:
        """Wrap sys.stdin/sys.stdout into async-compatible streams."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        raw_stdout = sys.stdout
        transport, _ = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, raw_stdout,
        )
        writer = asyncio.StreamWriter(transport, _, reader, loop)
        
        # Redirect sys.stdout and sys.__stdout__ to sys.stderr to completely prevent
        # other python code, third-party libraries, or click from writing to stdout.
        sys.stdout = sys.stderr
        sys.__stdout__ = sys.stderr
        
        return reader, writer
