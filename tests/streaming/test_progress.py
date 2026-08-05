"""W4-P1 tests — ProgressBridge and make_progress_bridge.

TDD: these tests are written BEFORE the implementation. They verify:
1. ProgressBridge forwards backend progress to ServerSession.send_progress_notification.
2. When progress_token is None, no forwarding occurs (no AttributeError).
3. If send_progress_notification raises, ProgressBridge swallows it (client disconnect case).
4. make_progress_bridge returns None when server_session or progress_token is absent.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from slm_mcp_hub.streaming.progress import ProgressBridge, make_progress_bridge


class TestProgressBridge:
    """Unit tests for ProgressBridge."""

    async def test_progress_bridge_forwards_to_session(self) -> None:
        """ProgressBridge.__call__ invokes ServerSession.send_progress_notification
        with the correct token, progress, total, message."""
        mock_session = AsyncMock()
        mock_session.send_progress_notification = AsyncMock()

        bridge = ProgressBridge(
            server_session=mock_session,
            progress_token="tok_abc",
            related_request_id="req_1",
        )

        await bridge(progress=0.5, total=1.0, message="halfway")

        mock_session.send_progress_notification.assert_called_once_with(
            progress_token="tok_abc",
            progress=0.5,
            total=1.0,
            message="halfway",
            related_request_id="req_1",
        )

    async def test_progress_bridge_integer_token(self) -> None:
        """ProgressBridge works with integer progress_token (JSON-RPC allows int)."""
        mock_session = AsyncMock()
        mock_session.send_progress_notification = AsyncMock()

        bridge = ProgressBridge(
            server_session=mock_session,
            progress_token=42,
        )

        await bridge(progress=0.1, total=None, message=None)

        mock_session.send_progress_notification.assert_called_once_with(
            progress_token=42,
            progress=0.1,
            total=None,
            message=None,
            related_request_id=None,  # no related_request_id set
        )

    async def test_progress_bridge_no_token_suppresses(self) -> None:
        """When progress_token is None, ProgressBridge.__call__ does NOT call
        ServerSession.send_progress_notification (no AttributeError, no forwarding)."""
        mock_session = AsyncMock()
        mock_session.send_progress_notification = AsyncMock()

        bridge = ProgressBridge(
            server_session=mock_session,
            progress_token=None,
        )

        await bridge(progress=0.3, total=1.0, message="some progress")

        mock_session.send_progress_notification.assert_not_called()

    async def test_progress_bridge_no_token_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """When progress_token is None, ProgressBridge logs at DEBUG level."""
        mock_session = AsyncMock()
        bridge = ProgressBridge(server_session=mock_session, progress_token=None)

        with caplog.at_level(logging.DEBUG, logger="slm_mcp_hub.streaming.progress"):
            await bridge(progress=0.3, total=1.0, message="msg")

        assert any("no client progressToken" in rec.message or "suppressed" in rec.message
                   for rec in caplog.records)

    async def test_progress_bridge_session_error_is_swallowed(self) -> None:
        """If send_progress_notification raises (client disconnected), ProgressBridge
        logs at DEBUG and does NOT re-raise — the backend call must not fail because
        the client left early."""
        mock_session = AsyncMock()
        mock_session.send_progress_notification = AsyncMock(
            side_effect=RuntimeError("client gone")
        )

        bridge = ProgressBridge(
            server_session=mock_session,
            progress_token="tok",
        )

        # Must not raise
        await bridge(progress=0.5, total=1.0, message="oops")

        # send_progress_notification was called (the error came from it)
        mock_session.send_progress_notification.assert_called_once()

    async def test_progress_bridge_session_error_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """A suppressed session error is logged at DEBUG level."""
        mock_session = AsyncMock()
        mock_session.send_progress_notification = AsyncMock(
            side_effect=RuntimeError("connection reset")
        )
        bridge = ProgressBridge(server_session=mock_session, progress_token="t")

        with caplog.at_level(logging.DEBUG, logger="slm_mcp_hub.streaming.progress"):
            await bridge(progress=0.0, total=1.0, message=None)

        assert any("disconnect" in rec.message.lower() or "failed" in rec.message.lower()
                   for rec in caplog.records)

    async def test_progress_bridge_multiple_calls(self) -> None:
        """Multiple consecutive progress notifications are each forwarded."""
        mock_session = AsyncMock()
        mock_session.send_progress_notification = AsyncMock()

        bridge = ProgressBridge(server_session=mock_session, progress_token="tok")
        events = [(0.25, 1.0, "q1"), (0.5, 1.0, "q2"), (0.75, 1.0, "q3"), (1.0, 1.0, "done")]
        for p, t, m in events:
            await bridge(progress=p, total=t, message=m)

        assert mock_session.send_progress_notification.call_count == 4

    async def test_progress_bridge_related_request_id_str_conversion(self) -> None:
        """related_request_id is converted to str when not None."""
        mock_session = AsyncMock()
        mock_session.send_progress_notification = AsyncMock()

        bridge = ProgressBridge(
            server_session=mock_session,
            progress_token="tok",
            related_request_id=99,  # integer — should become "99"
        )
        await bridge(progress=0.5, total=1.0, message=None)

        call_kwargs = mock_session.send_progress_notification.call_args.kwargs
        assert call_kwargs["related_request_id"] == "99"


class TestMakeProgressBridge:
    """Unit tests for the make_progress_bridge factory."""

    async def test_make_progress_bridge_returns_none_without_session(self) -> None:
        """make_progress_bridge(server_session=None, ...) returns None."""
        result = make_progress_bridge(server_session=None, progress_token="tok")
        assert result is None

    async def test_make_progress_bridge_returns_none_without_token(self) -> None:
        """make_progress_bridge(progress_token=None, ...) returns None."""
        mock_session = AsyncMock()
        result = make_progress_bridge(server_session=mock_session, progress_token=None)
        assert result is None

    async def test_make_progress_bridge_returns_bridge_when_both_present(self) -> None:
        """make_progress_bridge returns a ProgressBridge when both args are set."""
        mock_session = AsyncMock()
        bridge = make_progress_bridge(server_session=mock_session, progress_token="tok")
        assert bridge is not None
        assert isinstance(bridge, ProgressBridge)

    async def test_make_progress_bridge_bridge_is_callable(self) -> None:
        """The returned ProgressBridge can be called as ProgressFnT."""
        mock_session = AsyncMock()
        mock_session.send_progress_notification = AsyncMock()
        bridge = make_progress_bridge(
            server_session=mock_session,
            progress_token="tok",
            related_request_id="r1",
        )
        assert bridge is not None
        await bridge(progress=0.5, total=1.0, message="test")
        mock_session.send_progress_notification.assert_called_once()
