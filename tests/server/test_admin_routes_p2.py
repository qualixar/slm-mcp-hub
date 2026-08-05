"""W5-P2 TDD — admin_routes GET /api/events SSE endpoint tests.

TDD: written BEFORE implementation. Tests must FAIL until the live SSE route
is wired in admin_routes.py and EventStreamBridge exists.

Test plan (per LLD §12 W5-P2):
1. GET /api/events returns Content-Type: text/event-stream when bridge present.
2. GET /api/events returns 503 when event_stream_bridge=None.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FiniteMockBridge:
    """Mock EventStreamBridge that yields exactly one event chunk then stops."""

    async def stream(self) -> AsyncGenerator[str, None]:
        yield "event: lifecycle\ndata: {\"server\": \"test\", \"ts\": 0}\n\n"


class _InfiniteMockBridge:
    """Mock EventStreamBridge that yields events indefinitely (never stops)."""

    async def stream(self) -> AsyncGenerator[str, None]:
        while True:
            yield "event: lifecycle\ndata: {\"server\": \"test\", \"ts\": 0}\n\n"


def _make_app_with_bridge(bridge: Any) -> FastAPI:
    """Build a minimal FastAPI app with make_admin_router(event_stream_bridge=bridge)."""
    from slm_mcp_hub.server.admin_routes import make_admin_router

    app = FastAPI()
    router = make_admin_router(
        conn_manager=None,
        event_stream_bridge=bridge,
        metrics=None,
    )
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Test 1 — /api/events returns text/event-stream when bridge present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_endpoint_returns_sse_content_type() -> None:
    """GET /api/events returns Content-Type: text/event-stream when event_stream_bridge is set."""
    app = _make_app_with_bridge(_FiniteMockBridge())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200
            content_type = resp.headers.get("content-type", "")
            assert "text/event-stream" in content_type, (
                f"Expected text/event-stream, got {content_type!r}"
            )


# ---------------------------------------------------------------------------
# Test 2 — /api/events returns 503 without event_bus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_endpoint_503_without_event_bus() -> None:
    """When event_stream_bridge=None, GET /api/events returns 503."""
    app = _make_app_with_bridge(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/events")

    assert resp.status_code == 503, (
        f"Expected 503 without event_bus, got {resp.status_code}"
    )
