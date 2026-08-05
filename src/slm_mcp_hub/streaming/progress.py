"""Progress notification bridge for W4-P1.

When the hub proxies a long-running tool call:
  hub-client → [hub] → backend

The backend emits notifications/progress via the ProgressFnT callback passed to
ClientSession.send_request.  ProgressBridge re-emits these via
ServerSession.send_progress_notification so the hub-client sees live progress.

SDK symbols used (all verified in .venv, mcp==2.0.0):
  - mcp.server.session.ServerSession.send_progress_notification(
        progress_token, progress, total=None, message=None,
        related_request_id=None) -> None  [async]
  - mcp.shared.dispatcher.ProgressFnT: Protocol with
        async __call__(progress, total, message) -> None
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.session import ServerSession
from mcp.shared.dispatcher import ProgressFnT

logger = logging.getLogger(__name__)


class ProgressBridge:
    """Implements ProgressFnT; re-emits backend progress on the server session.

    The hub is simultaneously a client (to the backend) and a server (to its
    MCP client).  When the backend sends a notifications/progress event, the SDK
    delivers it by calling this object as a ProgressFnT callback.  ProgressBridge
    then calls ServerSession.send_progress_notification to forward the event to
    the hub's MCP client.

    Constructor args:
        server_session: The hub's ServerSession for the connected client.
        progress_token: Token from the client's original tools/call
            _meta.progressToken.  If None, progress events are logged but not
            forwarded (backend called without a client progressToken — acceptable).
        related_request_id: The hub's incoming JSON-RPC request id for this call.
            Converted to str before forwarding; None if not available.

    Design invariant — no block, no re-raise:
        - This method is async, so it never blocks the event loop.
        - All exceptions from send_progress_notification are caught and logged
          at DEBUG level.  A disconnected client must NOT abort the backend call.
    """

    def __init__(
        self,
        server_session: ServerSession,
        progress_token: str | int | None,
        related_request_id: str | int | None = None,
    ) -> None:
        self._session = server_session
        self._token = progress_token
        self._related_request_id = related_request_id

    async def __call__(
        self,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        """ProgressFnT implementation — forwards backend progress to the client session.

        If progress_token is None, the backend progress is suppressed (the client
        did not request progress notifications).  Any exception from the server
        session is swallowed so the backend call is not disrupted if the client
        disconnected mid-call.

        Note: asyncio.CancelledError (a BaseException) is NOT caught here — it
        propagates correctly through structural cancellation.
        """
        if self._token is None:
            logger.debug(
                "Backend progress — no client progressToken; suppressed",
            )
            return

        related = (
            str(self._related_request_id)
            if self._related_request_id is not None
            else None
        )
        try:
            await self._session.send_progress_notification(
                progress_token=self._token,
                progress=progress,
                total=total,
                message=message,
                related_request_id=related,
            )
        except Exception:
            # Deliberate broad-except: the client may have disconnected while
            # the backend call is still running.  A broken server session must
            # never abort the backend call.  The exception is logged at DEBUG
            # (not WARNING) to avoid flooding logs on expected disconnect events.
            logger.debug(
                "Failed to forward progress notification (client may have disconnected)",
                exc_info=True,
            )


def make_progress_bridge(
    server_session: ServerSession | None,
    progress_token: Any,
    related_request_id: Any = None,
) -> ProgressFnT | None:
    """Factory — return a ProgressBridge if conditions are met, else None.

    Returns None when:
    - server_session is None (non-SDK path, e.g. legacy JSON-RPC proxy)
    - progress_token is None (client did not request progress)

    When None is returned, the caller should pass progress_callback=None to
    conn.call_tool_streaming / OutboundClient.call_tool_streaming, which
    disables progress forwarding for that call.

    Args:
        server_session: The hub's ServerSession for the connected client, or None.
        progress_token: Token from the client's tools/call _meta.progressToken, or None.
        related_request_id: The hub's incoming request id, or None.

    Returns:
        A ProgressBridge callable satisfying ProgressFnT, or None.
    """
    if server_session is None or progress_token is None:
        return None
    return ProgressBridge(
        server_session=server_session,
        progress_token=progress_token,
        related_request_id=related_request_id,
    )
