"""P06 — Callback server security tests.

Tests cover:
- Binds only to 127.0.0.1 (loopback)
- OS-ephemeral port by default
- Fixed-port override for pre-registered redirect URIs
- One-shot: rejects second (replay) request
- Method validation: only GET
- Path validation: only /callback or /
- Request-size limit (8 KB)
- Port conflict fails cleanly
- Returns AuthorizationCodeResult with code/state/iss
- redirect_uri property is correct

RED phase: fails with ImportError until auth/callback.py is created.
"""
from __future__ import annotations

import asyncio
import socket
from urllib.parse import urlencode

import pytest

from slm_mcp_hub.auth.callback import CallbackError, CallbackServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return a free port by briefly binding then releasing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _http_get(host: str, port: int, path: str, *, extra_headers: str = "") -> bytes:
    """Send a raw GET request and return the full response bytes."""
    reader, writer = await asyncio.open_connection(host, port)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"{extra_headers}"
        f"Connection: close\r\n"
        f"\r\n"
    )
    writer.write(request.encode())
    await writer.drain()
    data = await reader.read(4096)
    writer.close()
    await writer.wait_closed()
    return data


async def _http_post(host: str, port: int, path: str, body: bytes = b"") -> bytes:
    """Send a raw POST request and return the full response bytes."""
    reader, writer = await asyncio.open_connection(host, port)
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body
    writer.write(request)
    await writer.drain()
    data = await reader.read(4096)
    writer.close()
    await writer.wait_closed()
    return data


# ---------------------------------------------------------------------------
# Binding tests
# ---------------------------------------------------------------------------


class TestCallbackServerBinding:
    async def test_binds_to_loopback_only(self):
        """Server must bind to 127.0.0.1, never to 0.0.0.0."""
        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            # Verify the actual socket address
            assert cb.actual_host == "127.0.0.1"

    async def test_ephemeral_port_assigned(self):
        """Default port=0 results in a non-zero OS-assigned port."""
        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            assert cb.actual_port > 0

    async def test_redirect_uri_uses_actual_port(self):
        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            uri = cb.redirect_uri
            assert uri.startswith("http://127.0.0.1:")
            assert str(cb.actual_port) in uri
            assert "/callback" in uri

    async def test_fixed_port_override(self):
        """--callback-port compatibility: fixed port is respected."""
        port = _free_port()
        async with CallbackServer(host="127.0.0.1", port=port) as cb:
            assert cb.actual_port == port

    async def test_non_loopback_host_raises(self):
        """Binding to non-loopback is refused at construction time."""
        with pytest.raises(ValueError, match="loopback"):
            CallbackServer(host="0.0.0.0", port=0)

    async def test_port_conflict_raises_callback_error(self):
        """If the requested port is already in use, fails cleanly."""
        port = _free_port()
        # Hold the port ourselves so the callback server cannot bind
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", port))
            blocker.listen(1)
            with pytest.raises(CallbackError, match="port"):
                async with CallbackServer(host="127.0.0.1", port=port):
                    pass
        finally:
            blocker.close()


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------


class TestCallbackRequestHandling:
    async def test_returns_authorization_code_result(self):
        """A valid GET /callback?code=abc&state=xyz returns AuthorizationCodeResult."""
        from mcp.shared.auth import AuthorizationCodeResult

        params = urlencode({"code": "test_code_abc", "state": "test_state_xyz"})
        path = f"/callback?{params}"

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            handler_task = asyncio.create_task(cb.callback_handler())
            await asyncio.sleep(0.05)  # Let server start
            await _http_get("127.0.0.1", cb.actual_port, path)
            result = await asyncio.wait_for(handler_task, timeout=3.0)

        assert isinstance(result, AuthorizationCodeResult)
        assert result.code == "test_code_abc"
        assert result.state == "test_state_xyz"

    async def test_iss_parameter_passed_through(self):
        """RFC 9207 iss parameter is captured and returned."""
        params = urlencode({"code": "c", "state": "s", "iss": "https://as.example.com"})
        path = f"/callback?{params}"

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            handler_task = asyncio.create_task(cb.callback_handler())
            await asyncio.sleep(0.05)
            await _http_get("127.0.0.1", cb.actual_port, path)
            result = await asyncio.wait_for(handler_task, timeout=3.0)

        assert result.iss == "https://as.example.com"

    async def test_root_path_also_accepted(self):
        """/ as path is accepted in addition to /callback."""
        params = urlencode({"code": "rootcode", "state": "rootstate"})
        path = f"/?{params}"

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            handler_task = asyncio.create_task(cb.callback_handler())
            await asyncio.sleep(0.05)
            await _http_get("127.0.0.1", cb.actual_port, path)
            result = await asyncio.wait_for(handler_task, timeout=3.0)

        assert result.code == "rootcode"

    async def test_200_response_sent(self):
        """Server responds with 200 OK to the browser."""
        params = urlencode({"code": "c", "state": "s"})
        path = f"/callback?{params}"

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            handler_task = asyncio.create_task(cb.callback_handler())
            await asyncio.sleep(0.05)
            response = await _http_get("127.0.0.1", cb.actual_port, path)
            await asyncio.wait_for(handler_task, timeout=3.0)

        assert b"200" in response or b"200 OK" in response


# ---------------------------------------------------------------------------
# Security: one-shot and replay rejection
# ---------------------------------------------------------------------------


class TestCallbackSecurity:
    async def test_second_request_rejected(self):
        """Replay attack: second request returns 400-series response."""
        params = urlencode({"code": "c", "state": "s"})
        path = f"/callback?{params}"

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            handler_task = asyncio.create_task(cb.callback_handler())
            await asyncio.sleep(0.05)

            # First request — should succeed
            resp1 = await _http_get("127.0.0.1", cb.actual_port, path)
            await asyncio.wait_for(handler_task, timeout=3.0)

            # Second request (replay) — must be rejected
            resp2 = await _http_get("127.0.0.1", cb.actual_port, path)

        assert b"200" in resp1
        # Second request must get an error response (4xx)
        assert b"400" in resp2 or b"403" in resp2 or b"410" in resp2 or b"404" in resp2

    async def test_post_method_rejected(self):
        """POST to callback must be rejected (OAuth redirect is always GET)."""
        params = urlencode({"code": "c", "state": "s"})
        path = f"/callback?{params}"

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            # Send a POST directly (not via callback_handler — this is a bad actor)
            resp = await _http_post("127.0.0.1", cb.actual_port, path)

        assert b"405" in resp or b"400" in resp

    async def test_oversized_request_rejected(self):
        """Request body/URI >8 KB must be rejected to prevent memory exhaustion."""
        # 9 KB query string — well over the 8 KB limit
        huge_param = "x" * (9 * 1024)
        path = f"/callback?code=c&state=s&junk={huge_param}"

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            resp = await _http_get("127.0.0.1", cb.actual_port, path)

        # Expect 413 or 400
        assert b"413" in resp or b"400" in resp or b"414" in resp

    async def test_unknown_path_rejected(self):
        """Any path other than / or /callback must return 404."""
        path = "/admin/secret?code=c&state=s"

        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            resp = await _http_get("127.0.0.1", cb.actual_port, path)

        assert b"404" in resp or b"400" in resp

    async def test_callback_handler_timeout_is_bounded(self):
        """callback_handler raises asyncio.TimeoutError if no request arrives."""
        async with CallbackServer(host="127.0.0.1", port=0) as cb:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(cb.callback_handler(), timeout=0.1)


# ---------------------------------------------------------------------------
# Internal path coverage — direct unit tests for _handle_connection internals
# ---------------------------------------------------------------------------


class TestCallbackServerInternals:
    """Direct unit tests for internal call paths not reachable via the network."""

    async def test_aexit_without_server_is_noop(self):
        """__aexit__ on an unstarted server (_server is None) must not crash."""
        cb = CallbackServer(host="127.0.0.1", port=0)
        # _server is None — covers the `if self._server is not None` False branch
        await cb.__aexit__(None, None, None)
        assert cb._server is None

    async def test_callback_handler_without_start_raises(self):
        """callback_handler() called without async with raises CallbackError."""
        cb = CallbackServer(host="127.0.0.1", port=0)
        # _result is None because __aenter__ was never called
        with pytest.raises(CallbackError, match="not started"):
            await cb.callback_handler()

    async def test_handle_connection_propagates_exception_to_future(self):
        """_process_request exception is captured and stored in the result future."""
        from unittest.mock import AsyncMock, MagicMock

        cb = CallbackServer(host="127.0.0.1", port=0)
        loop = asyncio.get_running_loop()
        cb._result = loop.create_future()
        cb._used = False

        # Reader with exception set — read() raises immediately, not TimeoutError
        reader = asyncio.StreamReader()
        reader.set_exception(RuntimeError("forced read error"))

        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(return_value=None)

        await cb._handle_connection(reader, writer)

        assert cb._result.done()
        with pytest.raises(RuntimeError, match="forced read error"):
            cb._result.result()

    async def test_handle_connection_writer_close_exception_swallowed(self):
        """Exception from writer.close() in finally block is silently swallowed."""
        from unittest.mock import AsyncMock, MagicMock

        cb = CallbackServer(host="127.0.0.1", port=0)
        loop = asyncio.get_running_loop()
        cb._result = loop.create_future()
        cb._used = False

        reader = asyncio.StreamReader()
        params = "code=c&state=s"
        request = f"GET /callback?{params} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        reader.feed_data(request.encode())
        reader.feed_eof()

        writer = MagicMock()
        writer.write = MagicMock()
        # writer.close() raises — must be silently swallowed by the finally block
        writer.close = MagicMock(side_effect=RuntimeError("close failed"))
        writer.wait_closed = AsyncMock(return_value=None)

        # Must not propagate the close() exception
        await cb._handle_connection(reader, writer)

    async def test_process_request_timeout_sends_408(self):
        """reader.read() timeout causes _process_request to send 408 Request Timeout."""
        from unittest.mock import AsyncMock, MagicMock, patch

        cb = CallbackServer(host="127.0.0.1", port=0)
        loop = asyncio.get_running_loop()
        cb._result = loop.create_future()
        cb._used = False

        reader = asyncio.StreamReader()  # No data — would block indefinitely

        writer = MagicMock()
        writer.write = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(return_value=None)

        async def _force_timeout(coro, timeout):  # noqa: ARG001
            """Simulate asyncio.TimeoutError from the read call."""
            coro.close()
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", _force_timeout):
            await cb._process_request(reader, writer)

        assert writer.write.called
        written = writer.write.call_args[0][0]
        assert b"408" in written

    async def test_process_request_malformed_line_sends_400(self):
        """A request whose first line has no spaces returns 400 Bad Request."""
        from unittest.mock import AsyncMock, MagicMock

        cb = CallbackServer(host="127.0.0.1", port=0)
        loop = asyncio.get_running_loop()
        cb._result = loop.create_future()
        cb._used = False

        reader = asyncio.StreamReader()
        # No space in the first line → parts = ["BADREQUEST"] → len(parts) < 2
        reader.feed_data(b"BADREQUEST\r\n\r\n")
        reader.feed_eof()

        writer = MagicMock()
        writer.write = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(return_value=None)

        await cb._process_request(reader, writer)

        written = writer.write.call_args[0][0]
        assert b"400" in written

    async def test_process_request_result_already_done_skips_set(self):
        """When the result future is already done, set_result is safely skipped."""
        from unittest.mock import AsyncMock, MagicMock

        from mcp.shared.auth import AuthorizationCodeResult

        cb = CallbackServer(host="127.0.0.1", port=0)
        loop = asyncio.get_running_loop()
        cb._result = loop.create_future()
        # Pre-set the future — simulates a race where another request already won
        cb._result.set_result(
            AuthorizationCodeResult(code="first_winner", state=None, iss=None)
        )
        cb._used = False  # _used guard not triggered — testing the done() branch

        reader = asyncio.StreamReader()
        params = "code=second&state=s"
        request = f"GET /callback?{params} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        reader.feed_data(request.encode())
        reader.feed_eof()

        writer = MagicMock()
        writer.write = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(return_value=None)

        # Must NOT raise asyncio.InvalidStateError from calling set_result on done future
        await cb._process_request(reader, writer)

        # Original result must be preserved
        assert cb._result.result().code == "first_winner"

    async def test_concurrent_process_requests_one_200_one_410(self):
        """TOCTOU fix: two concurrent _process_request calls → exactly one 200, one 410.

        Without the atomicity fix (_used set after await), both coroutines pass the
        `if self._used` check before either sets it, producing two 200 responses.
        With the asyncio.Lock fix, the check-and-set is atomic: the second coroutine
        sees _used=True and immediately returns 410.
        """
        from unittest.mock import AsyncMock, MagicMock

        cb = CallbackServer(host="127.0.0.1", port=0)
        loop = asyncio.get_running_loop()
        cb._result = loop.create_future()
        # _used starts False (default)

        written_responses: list[bytes] = []

        def make_writer():
            writer = MagicMock()
            writer.write = MagicMock(
                side_effect=lambda d: written_responses.append(d)
            )
            writer.close = MagicMock()
            writer.wait_closed = AsyncMock(return_value=None)
            return writer

        request_data = (
            b"GET /callback?code=c&state=s HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        )

        # Readers start EMPTY so both tasks block at the read and can be
        # truly concurrent before data arrives
        reader1 = asyncio.StreamReader()
        reader2 = asyncio.StreamReader()

        task1 = asyncio.create_task(
            cb._process_request(reader1, make_writer())
        )
        task2 = asyncio.create_task(
            cb._process_request(reader2, make_writer())
        )

        # Give both tasks a chance to start and reach their await points
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Feed data simultaneously — both readers can now proceed
        reader1.feed_data(request_data)
        reader1.feed_eof()
        reader2.feed_data(request_data)
        reader2.feed_eof()

        await asyncio.gather(task1, task2)

        count_200 = sum(1 for r in written_responses if b"200" in r)
        count_410 = sum(1 for r in written_responses if b"410" in r)

        assert count_200 == 1, (
            f"Expected exactly one 200 OK, got {count_200} "
            f"(TOCTOU: both connections grabbed the slot)"
        )
        assert count_410 == 1, (
            f"Expected exactly one 410 Gone, got {count_410}"
        )

    async def test_handle_connection_exception_with_done_result_no_crash(self):
        """Exception in _process_request when result is done — skips set_exception.

        Covers the False branch of `if self._result is not None and not self._result.done()`.
        """
        from unittest.mock import AsyncMock, MagicMock

        from mcp.shared.auth import AuthorizationCodeResult

        cb = CallbackServer(host="127.0.0.1", port=0)
        loop = asyncio.get_running_loop()
        cb._result = loop.create_future()
        cb._result.set_result(
            AuthorizationCodeResult(code="done_already", state=None, iss=None)
        )
        cb._used = False

        reader = asyncio.StreamReader()
        reader.set_exception(RuntimeError("error on already-done future"))

        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(return_value=None)

        # Must not crash — set_exception is safely skipped
        await cb._handle_connection(reader, writer)
        assert cb._result.result().code == "done_already"
