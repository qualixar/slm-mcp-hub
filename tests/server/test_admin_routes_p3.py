"""W5-P3 TDD — admin_routes dashboard + warm/stop control route tests.

TDD: written BEFORE implementation. Tests MUST FAIL until routes are added
to admin_routes.py.

Test plan (per LLD §12 W5-P3):
1. GET /dashboard returns 200 with Content-Type: text/html.
2. When dashboard_enabled=False, GET /dashboard returns 404.
3. SECURITY: HubConfig.dashboard_bind default is '127.0.0.1', NOT '0.0.0.0'.
4. POST /api/servers/{name}/warm calls ensure_connected(name) when not connected.
   (LLD names this test 'posts_to_manager_reconnect'; impl uses ensure_connected
   per LLD §12 W5-P3 reconciliation note — use the idempotent method.)
5. POST /api/servers/{name}/warm is idempotent when already connected —
   returns 'Already connected...', does NOT call ensure_connected().
6. POST /api/servers/{name}/stop calls evict() exactly once. Returns {success: True}.
7. POST /api/servers/{name}/stop on pinned backend: evict() called (no-op in
   manager), response {success: True, message: contains 'not pinned'}.
   Route does NOT check is_pinned — manager handles that guard.
8. SECURITY: control routes require api-key (401 without key when key set).
"""

from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(
    *,
    connected_names: list[str] | None = None,
    ensure_connected_result: bool = True,
) -> MagicMock:
    """Build a mock ConnectionManager with specified connected backends."""
    mgr = MagicMock()

    connections: dict[str, MagicMock] = {}
    for name in (connected_names or []):
        conn = MagicMock()
        conn.is_connected = True
        connections[name] = conn
    mgr._connections = connections

    # get_server_status() for dashboard rendering
    names = connected_names or ["srv-a"]
    mgr.get_server_status.return_value = [
        {
            "name": n,
            "transport": "stdio",
            "connected": True,
            "lifecycle": "connected",
            "tools": 2,
            "restart_count": 0,
            "consecutive_failures": 0,
            "needs_attention": False,
            "last_error": None,
        }
        for n in names
    ]

    # Async control methods — use AsyncMock for await compatibility
    mgr.ensure_connected = AsyncMock(return_value=ensure_connected_result)
    mgr.evict = AsyncMock(return_value=None)

    return mgr


def _make_app(
    mgr: Any,
    *,
    dashboard_enabled: bool = True,
    api_key: str | None = None,
) -> FastAPI:
    """Build a FastAPI test app with admin router.

    When api_key is set, adds an x-slm-hub-api-key middleware that returns
    401 for requests missing the key — mirrors http_server.py behaviour.
    """
    from slm_mcp_hub.server.admin_routes import make_admin_router

    app = FastAPI()

    if api_key is not None:
        _key = api_key  # capture for closure

        @app.middleware("http")
        async def _require_key(request: Request, call_next: Any) -> Response:
            supplied = request.headers.get("x-slm-hub-api-key", "")
            if not supplied or not secrets.compare_digest(supplied, _key):
                return JSONResponse(status_code=401, content={"error": "Unauthorized"})
            return await call_next(request)

    router = make_admin_router(
        conn_manager=mgr,
        event_stream_bridge=None,
        metrics=None,
        dashboard_enabled=dashboard_enabled,
    )
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Dashboard route tests
# ---------------------------------------------------------------------------


class TestDashboardRoute:
    async def test_dashboard_returns_html(self) -> None:
        """GET /dashboard returns status 200 and Content-Type: text/html."""
        mgr = _make_manager(connected_names=["srv-a"])
        app = _make_app(mgr, dashboard_enabled=True)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        content_type = resp.headers.get("content-type", "")
        assert "text/html" in content_type, (
            f"Expected Content-Type: text/html, got {content_type!r}"
        )

    async def test_dashboard_disabled_returns_404(self) -> None:
        """When dashboard_enabled=False, GET /dashboard returns 404."""
        mgr = _make_manager(connected_names=["srv-a"])
        app = _make_app(mgr, dashboard_enabled=False)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard")

        assert resp.status_code == 404, (
            f"Expected 404 when dashboard_enabled=False, got {resp.status_code}"
        )

    def test_dashboard_localhost_bind_default(self) -> None:
        """SECURITY: HubConfig.dashboard_bind default is '127.0.0.1', NOT '0.0.0.0'.

        This test verifies the security default without any constructor arg.
        Setting dashboard_bind='0.0.0.0' exposes admin controls to the network —
        that must be an EXPLICIT opt-in, never the default.
        """
        from slm_mcp_hub.core.config import HubConfig

        config = HubConfig()
        assert config.dashboard_bind == "127.0.0.1", (
            f"SECURITY: dashboard_bind default must be '127.0.0.1' (localhost only). "
            f"Got {config.dashboard_bind!r} — exposing admin controls to '0.0.0.0' "
            f"without explicit user opt-in is a security vulnerability."
        )


# ---------------------------------------------------------------------------
# Warm route tests
# ---------------------------------------------------------------------------


class TestWarmRoute:
    async def test_warm_route_posts_to_manager_reconnect(self) -> None:
        """POST /api/servers/{name}/warm calls ensure_connected(name) when not connected.

        LLD names this test 'posts_to_manager_reconnect'; W5-P3 scope uses the
        idempotent ensure_connected() path (W3-P3) rather than reconnect() to
        avoid force-disconnecting an already-live backend. This test verifies
        ensure_connected() is called exactly once with the correct server name.
        """
        mgr = _make_manager(connected_names=[])  # server NOT connected
        app = _make_app(mgr)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/servers/srv-a/warm")

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True, f"Expected success=True, got: {data}"
        mgr.ensure_connected.assert_called_once_with("srv-a")

    async def test_warm_route_idempotent_when_connected(self) -> None:
        """POST /api/servers/{name}/warm on an already-connected backend does NOT call
        ensure_connected(). Returns {success: True, message: 'Already connected...'}."""
        mgr = _make_manager(connected_names=["srv-a"])  # already connected
        app = _make_app(mgr)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/servers/srv-a/warm")

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True, f"Expected success=True, got: {data}"
        assert "already connected" in data.get("message", "").lower(), (
            f"Expected 'Already connected' in message, got {data.get('message')!r}"
        )
        mgr.ensure_connected.assert_not_called()

    async def test_warm_route_failure_returns_success_false(self) -> None:
        """When the backend is not connected AND ensure_connected() returns False
        (e.g. server name not in config / connect failed), the warm route returns
        {success: False} with a failure message — a clean error, not a 500."""
        mgr = _make_manager(connected_names=[], ensure_connected_result=False)
        app = _make_app(mgr)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/servers/ghost/warm")

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is False, f"Expected success=False, got: {data}"
        assert "failed" in data.get("message", "").lower()
        mgr.ensure_connected.assert_called_once_with("ghost")


# ---------------------------------------------------------------------------
# Stop route tests
# ---------------------------------------------------------------------------


class TestStopRoute:
    async def test_stop_route_calls_evict(self) -> None:
        """POST /api/servers/{name}/stop calls conn_manager.evict(name) exactly once.
        Verify via mock. Returns {success: True}."""
        mgr = _make_manager()
        app = _make_app(mgr)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/servers/srv-a/stop")

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True, f"Expected success=True, got: {data}"
        mgr.evict.assert_called_once_with("srv-a")

    async def test_stop_route_pinned_backend(self) -> None:
        """POST /api/servers/{name}/stop on a pinned backend: evict() is called
        (no-op inside manager), response is {success: True, message: contains 'not pinned'}.

        The route does NOT check is_pinned — evict() handles the pinned guard
        internally (W3 contract). This test verifies the route delegates fully.
        """
        mgr = _make_manager()
        app = _make_app(mgr)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/servers/pinned-srv/stop")

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True, f"Expected success=True, got: {data}"
        assert "not pinned" in data.get("message", "").lower(), (
            f"Expected 'not pinned' in response message, got {data.get('message')!r}"
        )
        # Route must call evict exactly once — manager handles the pinned guard
        mgr.evict.assert_called_once_with("pinned-srv")


# ---------------------------------------------------------------------------
# Security: api-key on control routes
# ---------------------------------------------------------------------------


class TestControlRoutesApiKey:
    async def test_control_routes_require_api_key(self) -> None:
        """With SLM_HUB_API_KEY set, POST /api/servers/{name}/warm returns 401 when
        the key header is absent. Same for /stop.

        The api-key middleware (from http_server.py) protects all routes. Control
        routes MUST NOT be reachable without the key when auth is enabled.
        """
        mgr = _make_manager()
        api_key = "test-secret-key-p3"
        app = _make_app(mgr, api_key=api_key)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # warm without key → must return 401
            resp_warm = await client.post("/api/servers/srv-a/warm")
            assert resp_warm.status_code == 401, (
                f"SECURITY: expected 401 (warm, no api-key), got {resp_warm.status_code}. "
                f"Control routes MUST be protected by api-key middleware."
            )

            # stop without key → must return 401
            resp_stop = await client.post("/api/servers/srv-a/stop")
            assert resp_stop.status_code == 401, (
                f"SECURITY: expected 401 (stop, no api-key), got {resp_stop.status_code}. "
                f"Control routes MUST be protected by api-key middleware."
            )
