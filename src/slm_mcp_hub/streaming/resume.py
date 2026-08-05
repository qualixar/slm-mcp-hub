"""Safe token-gated retry helper for hub→backend resumable streaming.

W8-P4: Every streaming call through the router now receives a
``ResumableCallContext`` so that mid-stream connection drops can be recovered
ONCE — without risking double-execution of non-idempotent tools.

SAFETY-CRITICAL design decision (resume-only-with-token):
Tool calls may be non-idempotent. A blind retry after a mid-stream failure
could double-execute a tool. Therefore the retry is strictly bounded:

1. Catch the REAL transport-drop exception: ``MCPError`` when
   ``exc.code == CONNECTION_CLOSED`` (from ``mcp_types.CONNECTION_CLOSED``).
   This is what ``mcp.shared.jsonrpc_dispatcher`` raises at lines 336 and 400
   when the connection EOF is detected — NOT ``ResumptionError``.
   ``ResumptionError`` is also caught defensively (client-side resumption failure),
   but it is NOT the primary production path.

2. For any other ``MCPError`` code (e.g. ``INVALID_PARAMS``, ``METHOD_NOT_FOUND``,
   ``REQUEST_TIMEOUT``): re-raise immediately. These are protocol/tool rejections,
   not transient transport drops — retrying them would be wrong and unsafe.

3. Retry ONCE and only if ``await ctx.get_token()`` is not None.
   A token means the backend already acknowledged progress: the SDK can
   CONTINUE the stream from that point, not restart from scratch.

4. If no token was captured before the error, re-raise.
   The router's ``except Exception`` converts it to a soft ``RouteResult``.
   Executing an unknown-idempotency tool again is unsafe.

5. ``asyncio.CancelledError`` is ``BaseException``, not ``Exception``.
   It is NOT caught by the inner except clause and propagates unconditionally
   to the caller's ``CancelScope``. Never retried.

6. M-01 (stale token): ``ctx.clear()`` runs in an outer ``finally`` so it
   fires on ALL terminal paths — success, no-token re-raise, retry failure,
   non-transient MCPError, RuntimeError, and even ``CancelledError``. A caller
   who injects an external ``ResumableCallContext`` never sees a stale token
   from a previous failed call.

Honesty note — SDK-automatic vs. hub-added:
- SDK (stateful mode, ``stateless=False``): handles the client↔hub
  Last-Event-ID reconnect automatically via ``StreamableHTTPSessionManager``
  and ``InMemoryEventStore``. This module does NOT touch that path.
- This module handles the hub→backend leg ONLY. When the backend connection
  drops mid-stream, the MCP dispatcher raises ``MCPError(code=CONNECTION_CLOSED)``.
  This module intercepts it (only when a token was captured), and calls
  ``call_tool_streaming`` ONCE more with that token so the backend can continue
  from where it left off. ``ResumptionError`` is also caught defensively for
  client-side transport failures, but the primary real-world path is ``MCPError``.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.client.streamable_http import ResumptionError
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from slm_mcp_hub.streaming.resumable import ResumableCallContext

__all__ = ["run_with_safe_resume"]

logger = logging.getLogger(__name__)


async def run_with_safe_resume(
    conn: Any,
    cap_name: str,
    arguments: dict[str, Any],
    *,
    ctx: ResumableCallContext,
    effective_timeout: float | None,
    progress_callback: Any | None,
) -> dict[str, Any]:
    """Call ``conn.call_tool_streaming`` with a one-shot token-gated resume.

    Invokes ``conn.call_tool_streaming`` with the current resumption token from
    ``ctx`` (``None`` on the first call for a fresh context). If the call raises
    a transient transport error AND a token was captured during the attempt,
    retries ONCE with that token so the backend continues from where it left off.

    ``ctx.clear()`` is called in a ``finally`` block so the context is always
    cleaned up — success, failure, or ``CancelledError`` (M-01 fix: no stale
    token left in an injected caller-provided context).

    Transient exception classes (triggering the retry gate):
        - ``MCPError`` with ``code == CONNECTION_CLOSED``: the MCP dispatcher
          raises this on connection EOF (the real production path).
        - ``ResumptionError``: caught defensively for client-side transport failures.
        - Any ``MCPError`` with a DIFFERENT code (``INVALID_PARAMS`` etc.) is NOT
          transient — it propagates immediately, no retry.

    Safety invariant:
        Without a captured token, any transient error is re-raised. The router's
        ``except Exception`` block converts this to a soft ``RouteResult`` without
        executing the tool a second time.

    Args:
        conn: Object implementing ``call_tool_streaming`` (typically
            ``MCPConnection`` or a duck-typed test double).
        cap_name: The backend's original (non-namespaced) tool name.
        arguments: Tool argument dict, forwarded unchanged.
        ctx: ``ResumableCallContext`` tracking the resumption token for this call.
            Typically a fresh auto-created context (``get_token()`` returns ``None``).
            Cleared in ``finally`` on ALL terminal paths.
        effective_timeout: ``read_timeout_seconds`` forwarded to the transport.
            ``None`` means no timeout (UNBOUNDED timeout class).
        progress_callback: Optional progress callback forwarded to the transport.

    Returns:
        The raw result ``dict`` from ``call_tool_streaming``.

    Raises:
        MCPError: When the transient error had no captured token (re-raised, safe),
            OR when the single retry also fails (bounded at 1), OR when the code
            is not ``CONNECTION_CLOSED`` (protocol rejection, propagated immediately).
        ResumptionError: When caught defensively and no token was captured.
        Any other Exception: Non-transient errors propagate unchanged.
        asyncio.CancelledError: Always propagates — ``BaseException``, never retried.
            ``ctx.clear()`` still runs via ``finally`` before propagation.
    """
    try:
        try:
            result = await conn.call_tool_streaming(
                cap_name,
                arguments,
                read_timeout_seconds=effective_timeout,
                progress_callback=progress_callback,
                resumption_token=await ctx.get_token(),
                on_resumption_token=ctx.on_token_update,
            )
        except (MCPError, ResumptionError) as exc:
            # Non-transient MCPError: protocol/tool rejection, not a transport drop.
            # INVALID_PARAMS, METHOD_NOT_FOUND, REQUEST_TIMEOUT, etc. must not be
            # retried — they will fail the same way on a second attempt.
            if isinstance(exc, MCPError) and exc.code != CONNECTION_CLOSED:
                raise
            # Transport drop (CONNECTION_CLOSED) or client-side ResumptionError.
            # Only retry when the backend already acknowledged progress with a token.
            token = await ctx.get_token()
            if token is None:
                # No token captured — unsafe to retry a potentially non-idempotent tool.
                logger.warning(
                    "Streaming call %r dropped with no resumption token — "
                    "not retrying (non-idempotency safety).",
                    cap_name,
                )
                raise
            logger.info(
                "Streaming call %r dropped (token=%r) — resuming once.",
                cap_name,
                token,
            )
            # Single retry — any exception from this call propagates unchanged.
            result = await conn.call_tool_streaming(
                cap_name,
                arguments,
                read_timeout_seconds=effective_timeout,
                progress_callback=progress_callback,
                resumption_token=token,
                on_resumption_token=ctx.on_token_update,
            )
    finally:
        # M-01: clear on ALL terminal paths so no stale token survives in an
        # injected caller-provided context. CancelledError propagates through
        # this finally after ctx.clear() runs.
        await ctx.clear()

    return result
