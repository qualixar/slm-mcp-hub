"""One-shot loopback callback server for OAuth2 authorization code flow.

Design invariants
-----------------
* **Loopback only.** The server binds exclusively to 127.0.0.1 (or localhost /
  ::1).  Binding to 0.0.0.0 or any external interface is refused at
  construction time.
* **Ephemeral port by default.** ``port=0`` lets the OS choose a free port
  (safe for parallel test runs and production use).  A fixed port can be
  provided for providers whose pre-registration requires an exact redirect URI.
* **One-shot.** The server accepts exactly one authorization-code callback.
  Any subsequent connection receives a 410 Gone response.  This prevents
  replay attacks: an attacker who catches the callback URL cannot re-submit it.
* **Bounded lifetime.** ``callback_handler()`` raises ``asyncio.TimeoutError``
  if no request arrives within the configured timeout (default 120 s).
* **Request validation.**
  - Method must be GET.
  - Path must be ``/callback`` or ``/``.
  - Total request data is capped at ``_MAX_REQUEST_BYTES``.
* **Clean port-conflict failure.** If the requested port is already in use,
  ``__aenter__`` raises ``CallbackError`` before any socket is left open.
* **No secret in state.** This module owns only the local network boundary and
  parameter extraction.  PKCE, state-match, iss, resource, and token exchange
  are validated entirely by the SDK ``OAuthClientProvider``.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

from mcp.shared.auth import AuthorizationCodeResult

logger = logging.getLogger(__name__)

_MAX_REQUEST_BYTES = 8192  # 8 KB — prevents memory exhaustion on oversized requests
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_PATHS = frozenset({"/callback", "/"})
_CALLBACK_TIMEOUT_S = 120.0


class CallbackError(RuntimeError):
    """Raised when the callback server cannot start or receives an invalid request.

    The message never contains token or credential material.
    """


class CallbackServer:
    """One-shot loopback HTTP server that captures OAuth authorization codes.

    Usage (CLI login path only)::

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            provider = build_login_provider(..., callback_server=cb)
            result = await cb.callback_handler()

    Attributes accessible after ``__aenter__``:
        actual_host  — bound host (always the requested loopback host)
        actual_port  — OS-assigned port (> 0 even when port=0 was requested)
        redirect_uri — full ``http://{host}:{port}/callback`` URI

    Only one ``CallbackServer`` instance is used per authorization flow.
    ``callback_handler()`` is safe to pass directly to ``OAuthClientProvider``.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"CallbackServer host must be a loopback address "
                f"(one of {sorted(_LOOPBACK_HOSTS)}), got {host!r}"
            )
        self._host = host
        self._port = port
        self._actual_port: int = port
        self._server: asyncio.Server | None = None
        self._result: asyncio.Future[AuthorizationCodeResult] | None = None
        self._used = False
        # One-shot lock: atomically check-and-set _used so two concurrent
        # connections cannot both pass the replay guard before either sets it
        # (TOCTOU fix — the race window was at `await reader.read()`).
        self._one_shot_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Properties (available after __aenter__)
    # ------------------------------------------------------------------

    @property
    def actual_host(self) -> str:
        return self._host

    @property
    def actual_port(self) -> int:
        return self._actual_port

    @property
    def redirect_uri(self) -> str:
        return f"http://{self._host}:{self._actual_port}/callback"

    # ------------------------------------------------------------------
    # Async context manager lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "CallbackServer":
        self._result = asyncio.get_running_loop().create_future()
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                host=self._host,
                port=self._port,
            )
        except OSError as exc:
            raise CallbackError(
                f"Cannot bind callback server to {self._host}:{self._port} "
                f"(port conflict or permission error): {exc}"
            ) from exc

        sock = self._server.sockets[0]
        self._actual_port = sock.getsockname()[1]
        logger.debug(
            "OAuth callback server bound to %s:%d",
            self._host,
            self._actual_port,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.debug("OAuth callback server shut down")

    # ------------------------------------------------------------------
    # Public: callback handler for OAuthClientProvider
    # ------------------------------------------------------------------

    async def callback_handler(self) -> AuthorizationCodeResult:
        """Awaitable callback handler to pass to ``OAuthClientProvider``.

        Waits for one authorization-code GET request then returns the parsed
        result.  Raises ``asyncio.TimeoutError`` if no request arrives within
        ``_CALLBACK_TIMEOUT_S`` seconds.
        """
        if self._result is None:
            raise CallbackError("CallbackServer is not started; use as 'async with'")
        return await asyncio.wait_for(self._result, timeout=_CALLBACK_TIMEOUT_S)

    # ------------------------------------------------------------------
    # Internal: per-connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one incoming TCP connection from the browser."""
        try:
            await self._process_request(reader, writer)
        except Exception as exc:
            logger.debug("OAuth callback: error processing request: %s", exc)
            if self._result is not None and not self._result.done():
                self._result.set_exception(exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _process_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Validate and parse one HTTP request; set the result future."""
        # One-shot guard — atomically claim the first connection slot.
        # The lock prevents two concurrent connections from both passing the
        # `if self._used` check before either sets it (TOCTOU: the race window
        # was at `await reader.read()` in the pre-fix code).
        async with self._one_shot_lock:
            if self._used:
                _send_response(writer, 410, "Gone", b"Authorization already completed.")
                return
            self._used = True

        # Read request with hard size cap
        try:
            raw = await asyncio.wait_for(
                reader.read(_MAX_REQUEST_BYTES + 1), timeout=10.0
            )
        except asyncio.TimeoutError:
            _send_response(writer, 408, "Request Timeout", b"Timed out reading request.")
            return

        if len(raw) > _MAX_REQUEST_BYTES:
            _send_response(writer, 413, "Request Too Large", b"Request too large.")
            return

        request_str = raw.decode("latin-1", errors="replace")
        first_line, *_ = request_str.split("\r\n", 1)
        parts = first_line.split(" ", 2)

        if len(parts) < 2:
            _send_response(writer, 400, "Bad Request", b"Malformed request line.")
            return

        method, raw_path = parts[0], parts[1]

        if method != "GET":
            _send_response(writer, 405, "Method Not Allowed", b"Only GET is accepted.")
            return

        parsed = urlparse(raw_path)
        if parsed.path not in _ALLOWED_PATHS:
            _send_response(writer, 404, "Not Found", b"Path not found.")
            return

        params = parse_qs(parsed.query, keep_blank_values=False)
        code = _first(params, "code") or ""
        state = _first(params, "state")
        iss = _first(params, "iss")

        result = AuthorizationCodeResult(code=code, state=state, iss=iss)

        _send_response(
            writer,
            200,
            "OK",
            b"Authorization complete. You may close this tab.",
        )

        if self._result is not None and not self._result.done():
            self._result.set_result(result)
            logger.debug(
                "OAuth callback: captured authorization code (state present: %s)",
                state is not None,
            )


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _first(params: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key* in a parse_qs dict, or None."""
    values = params.get(key)
    if not values:
        return None
    return values[0] or None


def _send_response(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    body: bytes,
) -> None:
    """Write a minimal HTTP/1.1 response."""
    response = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body
    writer.write(response)
