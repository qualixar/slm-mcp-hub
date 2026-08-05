"""W5-P1/P2/P3 — Admin routes: observability + control endpoints.

Mounted by create_app() in http_server.py via app.include_router(admin_router).
All routes inherit the existing require_api_key middleware from the parent FastAPI
app (no exemption — admin routes are protected behind the same key guard).

W5-P1 routes (READ ONLY):
  GET /api/servers/enriched  — enriched status with uptime, RAM, P95 latency

W5-P2 routes (SSE stream):
  GET /api/events            — SSE lifecycle event stream

W5-P3 routes (control + dashboard):
  GET  /dashboard                    — HTML admin dashboard (api-key protected)
  POST /api/servers/{name}/warm      — connect if not live (idempotent)
  POST /api/servers/{name}/stop      — evict: free RAM, retain caps

Design:
- make_admin_router() is a factory to avoid global state; called once in create_app().
- conn_manager=None → enriched route + control routes + dashboard NOT registered → 404.
- event_stream_bridge=None → P2 events route returns 503.
- metrics=None → p95_latency_ms = 0.0 for all entries.
- dashboard_enabled=False → /dashboard route NOT registered → 404.
- All params are keyword-only with safe defaults — create_app() backward compat preserved.
- SECURITY: /dashboard + warm/stop all require the api-key — the parent app's
  require_api_key middleware guards EVERY path except {API_PREFIX}/health. The
  api-key is the real guard. dashboard_bind (default 127.0.0.1) is a config value
  and is NOT separately socket-enforced (the app binds to HubConfig.host); enforcing
  or removing it is a W7 item. When no api-key is configured, management routes are
  open — the existing hub model (localhost-bound by default).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, StreamingResponse

from slm_mcp_hub.core.constants import API_PREFIX

logger = logging.getLogger(__name__)


def make_admin_router(
    conn_manager: Any | None,
    event_stream_bridge: Any | None,
    metrics: Any | None,
    dashboard_enabled: bool = True,
) -> APIRouter:
    """Factory: create and return the W5 admin APIRouter.

    W5-P1 implementation: registers GET /api/servers/enriched only when
    conn_manager is provided. Other routes (SSE, warm, stop, dashboard) are
    deferred to P2/P3.

    Args:
        conn_manager: The ConnectionManager instance. When None, the enriched
            route is NOT registered — GET /api/servers/enriched returns 404.
        event_stream_bridge: Reserved for P2 (SSE stream). Unused in P1.
        metrics: Optional MetricsCollector; when None, p95_latency_ms is 0.0.
        dashboard_enabled: Reserved for P3 (dashboard HTML). Unused in P1.

    Returns:
        Configured FastAPI APIRouter with W5-P1 routes.
    """
    router = APIRouter()

    # ── W5-P2: SSE lifecycle event stream ─────────────────────────────────
    # When event_stream_bridge is provided, streams lifecycle events as SSE.
    # When None, returns 503 to signal the bridge is not wired.
    @router.get(f"{API_PREFIX}/events")
    async def lifecycle_events() -> StreamingResponse:
        """Server-Sent Events stream of LifecycleEvents.

        Each connected client receives a dedicated event queue via
        EventStreamBridge. A slow or dead client NEVER blocks emit().

        Returns 503 when event_stream_bridge is not available.
        """
        if event_stream_bridge is None:
            return StreamingResponse(
                content=iter([]),
                status_code=503,
                media_type="text/event-stream",
            )

        async def _sse_generator() -> Any:
            async for chunk in event_stream_bridge.stream():
                yield chunk.encode("utf-8")

        return StreamingResponse(
            content=_sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
            },
        )

    # ── W5-P1/P3: Routes requiring conn_manager ───────────────────────────
    # All routes in this block require an active ConnectionManager.
    # When conn_manager=None, this entire block is skipped — all routes
    # return 404 (the routes simply do not exist in the router).
    if conn_manager is not None:

        # ── W5-P3: Dashboard HTML ─────────────────────────────────────────
        # Only registered when dashboard_enabled=True. When False, the route
        # is not added → FastAPI returns 404 naturally.
        # SECURITY: this route is protected by the parent app's api-key middleware
        # (guards every path except /api/health). dashboard_bind is a config value
        # only — NOT separately socket-enforced (the app binds to HubConfig.host);
        # the api-key is the real guard. W7: enforce dashboard_bind or drop it.
        if dashboard_enabled:
            @router.get("/dashboard")
            async def dashboard_page() -> HTMLResponse:
                """Admin dashboard: 6-signal status table for all backends.

                Renders a static HTML page with auto-refresh (10s). All backend-
                derived strings are HTML-escaped to prevent XSS.
                Returns Content-Type: text/html.
                """
                from slm_mcp_hub.observability.dashboard import render_dashboard_html
                from slm_mcp_hub.observability.status_enriched import (
                    enrich_server_status,
                )

                raw_status = conn_manager.get_server_status()
                enriched = enrich_server_status(
                    raw_status,
                    conn_manager._connections,
                    metrics,
                )
                html_content = render_dashboard_html(enriched)
                return HTMLResponse(content=html_content)

        # ── W5-P1: Enriched status endpoint ──────────────────────────────
        @router.get(f"{API_PREFIX}/servers/enriched")
        async def servers_enriched() -> dict[str, Any]:
            """Per-server detail enriched with uptime, RAM, P95 latency.

            Superset of /api/servers/detail — includes three new W5 fields per entry:
              uptime_seconds  (float, seconds since last connect; 0.0 when not live)
              p95_latency_ms  (float, from MetricsCollector; 0.0 when not wired)
              ram_bytes       (int or None; None for HTTP backends / psutil absent)

            Inherits require_api_key middleware from parent FastAPI app.
            """
            from slm_mcp_hub.observability.status_enriched import enrich_server_status

            raw_status = conn_manager.get_server_status()
            enriched = enrich_server_status(
                raw_status,
                conn_manager._connections,
                metrics,
            )
            return {"servers": enriched}

        # ── W5-P3: Warm route — idempotent connect ────────────────────────
        # SECURITY: protected by parent api-key middleware.
        # Idempotency: if already connected, returns immediately without calling
        # the manager — no unnecessary disconnection of a live backend.
        # When not connected: calls ensure_connected() (W3-P3 idempotent reconnect).
        @router.post(f"{API_PREFIX}/servers/{{name}}/warm")
        async def warm_server(name: str) -> dict[str, Any]:
            """Connect a backend if not currently live (idempotent warm-up).

            Returns {success: True, message: 'Already connected...'} immediately
            when the backend is already live — does NOT call ensure_connected().

            When not connected: calls conn_manager.ensure_connected(name) which
            is idempotent via the W2-P1 concurrent-connect gate.
            """
            conn = conn_manager._connections.get(name)
            if conn is not None and conn.is_connected:
                return {
                    "success": True,
                    "message": "Already connected — no action taken",
                }
            success = await conn_manager.ensure_connected(name)
            if success:
                return {
                    "success": True,
                    "message": f"Warm: '{name}' connected successfully",
                }
            return {
                "success": False,
                "message": f"Failed to connect to '{name}'",
            }

        # ── W5-P3: Stop route — evict backend ────────────────────────────
        # SECURITY: protected by parent api-key middleware.
        # Delegation: route calls evict() unconditionally — the manager's
        # evict() handles the pinned-backend guard (W3 contract). The route
        # does NOT check is_pinned itself.
        @router.post(f"{API_PREFIX}/servers/{{name}}/stop")
        async def stop_server(name: str) -> dict[str, Any]:
            """Evict a backend: free subprocess/RAM while retaining capabilities.

            Tools remain cached and routable — next call restarts the backend.
            Pinned backends (spawn='pinned' / always_on=True) are no-ops inside
            evict() — the route always returns success and delegates the guard.
            """
            await conn_manager.evict(name)
            return {
                "success": True,
                "message": "Eviction requested — backend will be stopped if not pinned",
            }

    return router
