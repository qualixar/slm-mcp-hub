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

P03 addition: ``SdkStdioServer`` wraps the official ``mcp.server.stdio.stdio_server()``
context manager + ``Server.run()`` so the Hub can speak the 2026-07-28 wire protocol
over stdio without hand-rolling JSON-RPC dispatch.  It accepts a pre-built
``mcp.server.lowlevel.Server`` (produced by ``build_sdk_server()`` in
``protocol/inbound.py``) and delegates all business logic to ``HubProductOperations``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server as SdkServer

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

            # JSON-RPC requests are objects.  JSON itself permits arrays,
            # strings, numbers, and null, but forwarding one of those values
            # to the endpoint would turn a client validation error into a
            # server-side AttributeError when it accesses request fields.
            if not isinstance(body, dict):
                await self._send_error(writer, None, -32600, "Invalid Request")
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
        sys.__stdout__ = sys.stderr  # type: ignore[misc,assignment]

        return reader, writer


# ---------------------------------------------------------------------------
# P03: SdkStdioServer — official SDK transport over stdio
# ---------------------------------------------------------------------------


class SdkStdioServer:
    """Serve MCP over stdin/stdout using the official SDK transport.

    Uses ``mcp.server.stdio.stdio_server()`` to manage stdin/stdout
    redirection and frame parsing, then delegates to an
    ``mcp.server.lowlevel.Server`` for JSON-RPC dispatch.

    This is the stdio counterpart of the HTTP SDK path in ``http_server.py``.
    All business logic is handled by the ``Server`` (built via
    ``build_sdk_server()`` in ``protocol/inbound.py``) which delegates to
    ``HubProductOperations``.

    Usage::

        sdk_server = build_sdk_server(ops)
        stdio_srv = SdkStdioServer(sdk_server=sdk_server)
        anyio.run(stdio_srv.run)

    Security:
    - No secrets stored; no auth tokens in this class.
    - SDK handles MCP-level auth (initialize handshake).
    - Stdout protection is delegated to ``stdio_server()`` which redirects
      fd 0 and fd 1 to null devices while serving.
    """

    def __init__(self, sdk_server: SdkServer) -> None:  # type: ignore[type-arg]
        """
        Args:
            sdk_server: A configured ``mcp.server.lowlevel.Server`` instance,
                typically produced by ``build_sdk_server(ops)`` from
                ``protocol/inbound.py``.
        """
        self._sdk_server = sdk_server

    async def run(self) -> None:
        """Run the SDK stdio server until the client closes the connection.

        Wraps stdin/stdout with the official SDK's ``stdio_server()`` context,
        then calls ``Server.run()`` with the SDK initialization options.

        This coroutine is intended to be the top-level entry point; run it via
        ``anyio.run(srv.run)`` or ``asyncio.run(srv.run())``.
        """
        from mcp.server.stdio import stdio_server as sdk_stdio_server

        init_opts = self._sdk_server.create_initialization_options()
        async with sdk_stdio_server() as (read_stream, write_stream):
            await self._sdk_server.run(
                read_stream,
                write_stream,
                init_opts,
                raise_exceptions=False,
            )
