"""Federation router — routes tool/resource/prompt calls to the correct MCP server.

W8-P1: Unified call pipeline.
- _resolve_connection: extracted lookup+reconnect logic (shared by all routing methods).
- _dispatch_call: unified hot-path with concurrency gate, timeout class selection,
  streaming vs. non-streaming dispatch, metrics recording, and activity tracking.
- route_tool_call: now accepts progress_callback / resumption_context; delegates
  to _dispatch_call with force_streaming=False.
- route_streaming_call: keeps always-streaming contract; delegates to _dispatch_call
  with force_streaming=True.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mcp.shared.dispatcher import ProgressFnT

from slm_mcp_hub.core.constants import DEFAULT_TOOL_TIMEOUT_S, TIMEOUT_CLASS_DEFAULT
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import MCPConnection
from slm_mcp_hub.streaming.resumable import ResumableCallContext
from slm_mcp_hub.streaming.resume import run_with_safe_resume

if TYPE_CHECKING:
    from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate
    from slm_mcp_hub.federation.timeouts import TimeoutRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteResult:
    """Immutable result of a routed tool call."""

    result: dict[str, Any]
    server_name: str
    tool_name: str
    duration_ms: int
    success: bool
    cached: bool = False


class FederationRouter:
    """Routes requests to the correct MCP server via the capability registry.

    Holds a reference to the shared connection pool (dict of MCPConnections)
    and the capability registry.  Does NOT own these — the Hub does.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        connections: dict[str, MCPConnection],
        activity_fn: Callable[[str], None] | None = None,
        reconnect_fn: Callable[[str], Awaitable[bool]] | None = None,
        *,
        concurrency_gate: BackendConcurrencyGate | None = None,
        timeout_registry: TimeoutRegistry | None = None,
        metrics: Any | None = None,
    ) -> None:
        """
        Parameters
        ----------
        registry:
            Shared capability registry used for tool/resource/prompt lookup.
        connections:
            Live connection pool (dict of MCPConnections owned by the manager).
        activity_fn:
            W3-P2: optional callback invoked with the server name on each
            successful route.  Pass ``ConnectionManager.mark_activity`` to keep
            the idle reaper's last-activity timestamps up to date.
            Defaults to None (backward compatible — no activity tracked).
        reconnect_fn:
            W3-P3: optional async callback invoked with the server name when
            a route targets an evicted (cached-not-live) backend.  Pass
            ``ConnectionManager.ensure_connected`` to enable transparent
            on-demand reconnect.  Must return True on success.
            Defaults to None (backward compatible — no auto-reconnect).
        concurrency_gate:
            W4-P2: optional per-backend concurrency gate.  When provided,
            ``route_streaming_call`` acquires the backend-specific
            ``CapacityLimiter`` before dispatching — preventing HOL blocking
            across backends.  ``None`` → ``_NullGate`` (no-op, backward compat).
        timeout_registry:
            W4-P2: optional timeout registry.  When provided,
            ``route_streaming_call`` resolves ``read_timeout_seconds`` from
            the backend's ``MCPServerConfig.timeout_class`` instead of using
            the flat ``DEFAULT_TOOL_TIMEOUT_S``.  ``None`` → backward compat.
        metrics:
            W8-P5: optional MetricsCollector.  When provided, every
            ``_dispatch_call`` invocation records duration_ms and success into
            the collector.  ``None`` → no metrics (backward compat).
        """
        self._registry = registry
        self._connections = connections
        self._activity_fn = activity_fn
        self._reconnect_fn = reconnect_fn
        self._concurrency_gate = concurrency_gate
        self._timeout_registry = timeout_registry
        self._metrics = metrics

    # ------------------------------------------------------------------
    # W8-P1: Shared connection resolution helper
    # ------------------------------------------------------------------

    def _resolve_connection(
        self,
        namespaced_name: str,
    ) -> tuple[Any, MCPConnection | None, RouteResult | None]:
        """Lookup capability + live connection, with transparent reconnect.

        Returns ``(cap, conn, None)`` on success or ``(None, None, error_result)``
        on any failure. The caller must check for the error case and return early.

        The reconnect path is intentionally synchronous (returns a coroutine
        wrapper instead) — callers that need reconnect must await via
        ``_resolve_connection_async``.

        Note: reconnect_fn is async; callers must use ``_resolve_connection_async``
        when reconnect may be needed. This sync variant only checks registry and
        connection state without initiating reconnect.
        """
        cap = self._registry.lookup_tool(namespaced_name)
        if cap is None:
            return (
                None,
                None,
                RouteResult(
                    result={
                        "content": [
                            {"type": "text", "text": f"Tool not found: {namespaced_name}"}
                        ],
                        "isError": True,
                    },
                    server_name="unknown",
                    tool_name=namespaced_name,
                    duration_ms=0,
                    success=False,
                ),
            )
        conn = self._connections.get(cap.server_name)
        return cap, conn, None

    async def _resolve_connection_async(
        self,
        namespaced_name: str,
    ) -> tuple[Any, MCPConnection | None, RouteResult | None]:
        """Async variant of _resolve_connection that also attempts reconnect.

        Returns ``(cap, conn, None)`` on success, or ``(None, None, error_result)``.
        """
        cap, conn, err = self._resolve_connection(namespaced_name)
        if err is not None:
            return None, None, err

        # cap is not None here
        if conn is None or not conn.is_connected:
            is_draining = conn is not None and getattr(conn, "is_draining", False) is True
            if not is_draining and self._reconnect_fn is not None:
                try:
                    reconnected = await self._reconnect_fn(cap.server_name)
                except Exception:
                    reconnected = False
                    logger.warning(
                        "reconnect_fn raised for %s", cap.server_name, exc_info=True
                    )
                conn = self._connections.get(cap.server_name)
                if not reconnected or conn is None or not conn.is_connected:
                    return (
                        None,
                        None,
                        RouteResult(
                            result={
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"Server unavailable after reconnect: {cap.server_name}",
                                    }
                                ],
                                "isError": True,
                            },
                            server_name=cap.server_name,
                            tool_name=cap.original_name,
                            duration_ms=0,
                            success=False,
                        ),
                    )
                # conn is now live — fall through to dispatch
            else:
                msg = (
                    f"Server is shutting down: {cap.server_name}"
                    if is_draining
                    else f"Server not configured: {cap.server_name}"
                    if conn is None
                    else f"Server not connected: {cap.server_name}"
                )
                return (
                    None,
                    None,
                    RouteResult(
                        result={"content": [{"type": "text", "text": msg}], "isError": True},
                        server_name=cap.server_name,
                        tool_name=cap.original_name,
                        duration_ms=0,
                        success=False,
                    ),
                )

        return cap, conn, None

    # ------------------------------------------------------------------
    # W8-P1: Unified dispatch hot-path
    # ------------------------------------------------------------------

    async def _dispatch_call(
        self,
        cap: Any,
        conn: MCPConnection,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None,
        progress_callback: ProgressFnT | None,
        resumption_context: Any | None,
        force_streaming: bool,
    ) -> RouteResult:
        """Unified dispatch — gate, timeout, streaming vs. non-streaming, metrics.

        Design invariants:
        - force_streaming=True (from route_streaming_call) → always calls call_tool_streaming.
        - force_streaming=False (from route_tool_call) → streaming only when
          progress_callback is not None OR timeout_class != TIMEOUT_CLASS_DEFAULT.
          Otherwise calls call_tool (backward-compat default path).
        - asyncio.CancelledError (BaseException) is NOT caught — structural cancellation
          propagates to the caller's CancelScope.
        - Metrics are recorded in finally with a guarding try/except so a metrics bug
          never breaks a real call.
        - activity_fn is called in finally (runs on success, error, AND cancellation).
        """
        effective_timeout = self._resolve_timeout(cap.server_name, timeout_s)

        # Guard: use isinstance to handle mock connections where _config is not a
        # real MCPServerConfig (plain MagicMock returns MagicMock for attributes).
        raw_class = getattr(getattr(conn, "_config", None), "timeout_class", None)
        timeout_class = raw_class if isinstance(raw_class, str) else TIMEOUT_CLASS_DEFAULT

        use_streaming = (
            force_streaming
            or progress_callback is not None
            or resumption_context is not None
            or timeout_class != TIMEOUT_CLASS_DEFAULT
        )

        # Initialize is_error=True as default so finally block works correctly
        # even if CancelledError (a BaseException) is raised before is_error is set.
        is_error: bool = True
        start = time.monotonic()
        try:
            # Acquire the gate INSIDE the try so a factory/acquire error yields a
            # soft RouteResult (and is metric-recorded) rather than escaping raw.
            gate = (
                self._concurrency_gate.acquire(cap.server_name)
                if self._concurrency_gate is not None
                else _NullGate()
            )
            async with gate:
                # Defensive: only a streaming-capable connection takes the streaming
                # path. MCPConnection always implements call_tool_streaming; a custom/
                # legacy connection missing it degrades safely to call_tool (progress/
                # resumption dropped) with a visible warning rather than AttributeError.
                stream_ok = use_streaming and hasattr(conn, "call_tool_streaming")
                if use_streaming and not stream_ok:
                    logger.warning(
                        "Streaming selected for %s but call_tool_streaming is "
                        "unavailable; falling back to call_tool "
                        "(progress/resumption dropped).",
                        cap.server_name,
                    )
                if stream_ok:
                    # W8-P4: auto-create ResumableCallContext when caller did not
                    # supply one. This makes the safe-retry path live on every
                    # streaming call — the context is ephemeral (UUID-keyed,
                    # in-process, cleared on success inside run_with_safe_resume).
                    ctx: ResumableCallContext = (
                        resumption_context
                        if resumption_context is not None
                        else ResumableCallContext(call_id=uuid4().hex)
                    )
                    result = await run_with_safe_resume(
                        conn,
                        cap.original_name,
                        arguments,
                        ctx=ctx,
                        effective_timeout=effective_timeout,
                        progress_callback=progress_callback,
                    )
                else:
                    result = await conn.call_tool(
                        cap.original_name, arguments, timeout_s=effective_timeout
                    )
            is_error = bool(result.get("isError", False))
            duration = int((time.monotonic() - start) * 1000)
            return RouteResult(
                result=result,
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=not is_error,
            )
        except Exception as exc:
            # Note: asyncio.CancelledError is a BaseException (not Exception) and
            # is NOT caught here — it propagates to the caller's CancelScope.
            duration = int((time.monotonic() - start) * 1000)
            if use_streaming:
                logger.error("Streaming tool call %s failed: %s", cap.original_name, exc)
            else:
                logger.error("Tool call %s failed: %s", cap.original_name, exc)
            return RouteResult(
                result={"content": [{"type": "text", "text": str(exc)}], "isError": True},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=False,
            )
        finally:
            duration_final = int((time.monotonic() - start) * 1000)
            # Guard: metrics/activity bugs must NEVER break a real call — both
            # are fail-open. A raising _activity_fn in this finally would discard
            # the pending return value and turn a successful call into a hard error.
            try:
                if self._metrics is not None:
                    self._metrics.record(
                        cap.server_name, duration_final, not is_error
                    )
            except Exception:
                logger.debug(
                    "Metrics record failed for %s", cap.server_name, exc_info=True
                )
            # W3-P2: mark activity at call COMPLETION (success or error) — the
            # backend was reachable and served (or errored on) the call, so it
            # is in-use, not idle. Marking at completion (not dispatch-start)
            # means a long call does not leave a stale start-timestamp that
            # would trigger instant post-call eviction.
            try:
                if self._activity_fn is not None:
                    self._activity_fn(cap.server_name)
            except Exception:
                logger.debug(
                    "activity_fn failed for %s", cap.server_name, exc_info=True
                )

    # ------------------------------------------------------------------
    # Public routing methods
    # ------------------------------------------------------------------

    async def route_tool_call(
        self,
        namespaced_name: str,
        arguments: dict[str, Any],
        timeout_s: float | None = None,
        *,
        progress_callback: ProgressFnT | None = None,
        resumption_context: Any | None = None,
    ) -> RouteResult:
        """Route a tool call to the correct MCP server.

        Args:
            namespaced_name: Full tool name (e.g. 'github__search_repositories').
            arguments: Tool arguments.
            timeout_s: Per-call timeout in seconds. Uses DEFAULT_TOOL_TIMEOUT_S if None.
            progress_callback: Optional ProgressFnT callback for backend progress events.
                When not None, forces the streaming dispatch path so the callback
                is forwarded to conn.call_tool_streaming.
            resumption_context: Optional resumption context (future use). When not None,
                forces the streaming dispatch path.

        Notes:
            - Default path (no progress_callback, default timeout_class): calls
              conn.call_tool — backward-compat with all pre-W8 tests.
            - Streaming path: calls conn.call_tool_streaming when
              progress_callback or resumption_context is given, or when the
              backend is configured with a non-default timeout_class.
        """
        cap, conn, err = await self._resolve_connection_async(namespaced_name)
        if err is not None:
            return err
        # _resolve_connection_async contract: returns an error RouteResult OR a
        # non-None (cap, conn). Use an explicit raise (not assert) so the invariant
        # holds under `python -O`, which strips assert statements.
        if cap is None or conn is None:
            raise RuntimeError(
                f"resolve returned no error but null cap/conn for {namespaced_name}"
            )
        return await self._dispatch_call(
            cap,
            conn,
            arguments,
            timeout_s=timeout_s,
            progress_callback=progress_callback,
            resumption_context=resumption_context,
            force_streaming=False,
        )

    async def route_resource_read(
        self,
        namespaced_uri: str,
    ) -> RouteResult:
        """Route a resource read to the correct MCP server."""
        cap = self._registry.lookup_resource(namespaced_uri)
        if cap is None:
            return RouteResult(
                result={},
                server_name="unknown",
                tool_name=namespaced_uri,
                duration_ms=0,
                success=False,
            )

        conn = self._connections.get(cap.server_name)
        if conn is None or not conn.is_connected:
            # W3-P3: attempt on-demand reconnect if enabled and backend is not draining.
            is_draining = conn is not None and getattr(conn, "is_draining", False) is True
            if not is_draining and self._reconnect_fn is not None:
                try:
                    reconnected = await self._reconnect_fn(cap.server_name)
                except Exception:
                    reconnected = False
                    logger.warning(
                        "reconnect_fn raised for %s", cap.server_name, exc_info=True
                    )
                conn = self._connections.get(cap.server_name)
                if not reconnected or conn is None or not conn.is_connected:
                    return RouteResult(
                        result={},
                        server_name=cap.server_name,
                        tool_name=cap.original_name,
                        duration_ms=0,
                        success=False,
                    )
            else:
                return RouteResult(
                    result={},
                    server_name=cap.server_name,
                    tool_name=cap.original_name,
                    duration_ms=0,
                    success=False,
                )

        start = time.monotonic()
        try:
            result = await conn.read_resource(cap.original_name)
            duration = int((time.monotonic() - start) * 1000)
            return RouteResult(
                result=result,
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=True,
            )
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            logger.error("Resource read %s failed: %s", namespaced_uri, exc)
            return RouteResult(
                result={},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=False,
            )
        finally:  # W3-P2: mark activity at completion (see route_tool_call)
            if self._activity_fn is not None:
                self._activity_fn(cap.server_name)

    async def route_prompt_get(
        self,
        namespaced_name: str,
        arguments: dict[str, Any],
    ) -> RouteResult:
        """Route a prompt get to the correct MCP server."""
        cap = self._registry.lookup_prompt(namespaced_name)
        if cap is None:
            return RouteResult(
                result={},
                server_name="unknown",
                tool_name=namespaced_name,
                duration_ms=0,
                success=False,
            )

        conn = self._connections.get(cap.server_name)
        if conn is None or not conn.is_connected:
            # W3-P3: attempt on-demand reconnect if enabled and backend is not draining.
            is_draining = conn is not None and getattr(conn, "is_draining", False) is True
            if not is_draining and self._reconnect_fn is not None:
                try:
                    reconnected = await self._reconnect_fn(cap.server_name)
                except Exception:
                    reconnected = False
                    logger.warning(
                        "reconnect_fn raised for %s", cap.server_name, exc_info=True
                    )
                conn = self._connections.get(cap.server_name)
                if not reconnected or conn is None or not conn.is_connected:
                    return RouteResult(
                        result={},
                        server_name=cap.server_name,
                        tool_name=cap.original_name,
                        duration_ms=0,
                        success=False,
                    )
            else:
                return RouteResult(
                    result={},
                    server_name=cap.server_name,
                    tool_name=cap.original_name,
                    duration_ms=0,
                    success=False,
                )

        start = time.monotonic()
        try:
            result = await conn.get_prompt(cap.original_name, arguments)
            duration = int((time.monotonic() - start) * 1000)
            return RouteResult(
                result=result,
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=True,
            )
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            logger.error("Prompt get %s failed: %s", namespaced_name, exc)
            return RouteResult(
                result={},
                server_name=cap.server_name,
                tool_name=cap.original_name,
                duration_ms=duration,
                success=False,
            )
        finally:  # W3-P2: mark activity at completion (see route_tool_call)
            if self._activity_fn is not None:
                self._activity_fn(cap.server_name)

    # ------------------------------------------------------------------
    # W4-P1: Long-running call with progress + cancellation passthrough
    # ------------------------------------------------------------------

    async def route_streaming_call(
        self,
        namespaced_name: str,
        arguments: dict[str, Any],
        *,
        progress_callback: ProgressFnT | None = None,
        timeout_override_s: float | None = None,
    ) -> RouteResult:
        """Route a long-running tool call with progress forwarding and cancellation.

        Always uses conn.call_tool_streaming (force_streaming=True) — this preserves
        the contract that existing P1/P2 test fakes (which implement only
        call_tool_streaming) keep passing unchanged.

        Args:
            namespaced_name: Full tool name (e.g. 'gemini__gemini-deep-research').
            arguments: Tool arguments dict.
            progress_callback: ProgressFnT callback for backend progress events.
            timeout_override_s: Per-call timeout override in seconds.
        """
        cap, conn, err = await self._resolve_connection_async(namespaced_name)
        if err is not None:
            return err
        # _resolve_connection_async contract: returns an error RouteResult OR a
        # non-None (cap, conn). Use an explicit raise (not assert) so the invariant
        # holds under `python -O`, which strips assert statements.
        if cap is None or conn is None:
            raise RuntimeError(
                f"resolve returned no error but null cap/conn for {namespaced_name}"
            )
        return await self._dispatch_call(
            cap,
            conn,
            arguments,
            timeout_s=timeout_override_s,
            progress_callback=progress_callback,
            resumption_context=None,
            force_streaming=True,
        )

    # ------------------------------------------------------------------
    # W4-P2 internal helpers
    # ------------------------------------------------------------------

    def _resolve_timeout(
        self,
        server_name: str,
        override_s: float | None,
    ) -> float | None:
        """Resolve effective read_timeout_seconds for a streaming call.

        Resolution order:
        1. ``timeout_registry`` not set → flat ``DEFAULT_TOOL_TIMEOUT_S``
           (or ``override_s`` if given) — backward compat with pre-W4 code.
        2. ``timeout_registry`` set → look up the backend's ``timeout_class``
           via the connection config; resolve via the registry; the call
           override replaces the class timeout_s if provided.
        3. UNBOUNDED class → returns ``None`` (no timeout).

        Parameters
        ----------
        server_name:
            The backend name used to look up ``MCPServerConfig.timeout_class``.
        override_s:
            Per-call timeout override (takes precedence over class timeout_s).
        """
        if self._timeout_registry is None:
            return override_s if override_s is not None else DEFAULT_TOOL_TIMEOUT_S

        conn = self._connections.get(server_name)
        class_name: str = TIMEOUT_CLASS_DEFAULT
        if conn is not None:
            raw = getattr(getattr(conn, "_config", None), "timeout_class", None)
            class_name = raw if isinstance(raw, str) else TIMEOUT_CLASS_DEFAULT

        policy = self._timeout_registry.resolve_for_server(class_name, override_s)
        return policy.timeout_s


class _NullGate:
    """No-op async context manager; used when no concurrency gate is configured.

    W4-P1 placeholder replaced by ``BackendConcurrencyGate.acquire(server_name)``
    when a gate is injected via the ``FederationRouter`` constructor.
    Kept for backward compatibility — routes without a gate behave identically
    to pre-W4 code.
    """

    async def __aenter__(self) -> "_NullGate":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass
