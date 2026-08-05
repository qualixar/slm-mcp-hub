"""W5-P1 TDD — admin_routes GET /api/servers/enriched tests.

TDD: written BEFORE implementation. Verifies:
1. GET /api/servers/enriched returns enriched status with uptime_seconds
2. uptime_seconds field is a float, not a string
3. Route not registered when conn_manager=None → 404

All tests use httpx.AsyncClient for FastAPI TestClient pattern.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn_manager(servers: list[dict[str, Any]] | None = None) -> MagicMock:
    """Build a minimal mock ConnectionManager."""
    mgr = MagicMock()
    mgr.get_server_status.return_value = servers or [
        {
            "name": "srv-a",
            "transport": "stdio",
            "connected": True,
            "lifecycle": "connected",
            "tools": 3,
            "restart_count": 1,
            "consecutive_failures": 0,
            "needs_attention": False,
            "last_error": None,
        }
    ]
    # _connections used by enrich_server_status
    mgr._connections = {}
    return mgr


def _make_test_app(conn_manager: Any) -> FastAPI:
    """Build minimal FastAPI app with admin router mounted."""
    from slm_mcp_hub.server.admin_routes import make_admin_router

    app = FastAPI()
    router = make_admin_router(conn_manager=conn_manager, event_stream_bridge=None, metrics=None)
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestServersEnrichedEndpoint:
    @pytest.mark.asyncio
    async def test_servers_enriched_returns_all_backends(self) -> None:
        """GET /api/servers/enriched returns a 'servers' list with uptime_seconds
        present for every entry."""
        mgr = _make_conn_manager()
        app = _make_test_app(mgr)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/servers/enriched")

        assert resp.status_code == 200
        data = resp.json()
        assert "servers" in data
        servers = data["servers"]
        assert len(servers) == 1
        assert servers[0]["name"] == "srv-a"
        assert "uptime_seconds" in servers[0]

    @pytest.mark.asyncio
    async def test_servers_enriched_uptime_field_type(self) -> None:
        """uptime_seconds in each entry is a float (or None), never a string."""
        mgr = _make_conn_manager()
        # Inject a mock connection with uptime
        conn = MagicMock()
        conn.is_connected = True
        conn.uptime_seconds = 120.5
        conn.process_pid = None
        mgr._connections = {"srv-a": conn}

        app = _make_test_app(mgr)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/servers/enriched")

        assert resp.status_code == 200
        servers = resp.json()["servers"]
        uptime = servers[0]["uptime_seconds"]
        assert isinstance(uptime, (int, float)), f"expected float, got {type(uptime)}"
        assert not isinstance(uptime, str)

    @pytest.mark.asyncio
    async def test_servers_enriched_404_without_conn_manager(self) -> None:
        """When conn_manager=None is passed to make_admin_router(),
        GET /api/servers/enriched returns 404 (route not registered)."""
        from slm_mcp_hub.server.admin_routes import make_admin_router

        app = FastAPI()
        router = make_admin_router(conn_manager=None, event_stream_bridge=None, metrics=None)
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/servers/enriched")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_servers_enriched_p95_latency_ms_present(self) -> None:
        """Enriched entries include p95_latency_ms field."""
        mgr = _make_conn_manager()
        app = _make_test_app(mgr)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/servers/enriched")

        assert resp.status_code == 200
        srv = resp.json()["servers"][0]
        assert "p95_latency_ms" in srv
        assert isinstance(srv["p95_latency_ms"], (int, float))

    @pytest.mark.asyncio
    async def test_servers_enriched_ram_bytes_present(self) -> None:
        """Enriched entries include ram_bytes field (may be None for HTTP backends)."""
        mgr = _make_conn_manager()
        app = _make_test_app(mgr)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/servers/enriched")

        assert resp.status_code == 200
        srv = resp.json()["servers"][0]
        assert "ram_bytes" in srv
        # ram_bytes is int or None
        assert srv["ram_bytes"] is None or isinstance(srv["ram_bytes"], int)

    @pytest.mark.asyncio
    async def test_sse_events_returns_503_when_bridge_none(self) -> None:
        """GET /api/events returns 503 when event_stream_bridge=None (P2 deferred).
        Covers admin_routes.py lines 71-72: the bridge-None guard."""
        from slm_mcp_hub.server.admin_routes import make_admin_router

        app = FastAPI()
        router = make_admin_router(conn_manager=None, event_stream_bridge=None, metrics=None)
        app.include_router(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/events")

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_servers_enriched_multiple_backends(self) -> None:
        """GET /api/servers/enriched returns enriched data for ALL configured backends."""
        mgr = _make_conn_manager(
            servers=[
                {"name": "srv-a", "transport": "stdio", "connected": True,
                 "lifecycle": "connected", "tools": 2, "restart_count": 0,
                 "consecutive_failures": 0, "needs_attention": False, "last_error": None},
                {"name": "srv-b", "transport": "http", "connected": False,
                 "lifecycle": "disconnected", "tools": 5, "restart_count": 3,
                 "consecutive_failures": 1, "needs_attention": True, "last_error": "timeout"},
            ]
        )
        app = _make_test_app(mgr)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/servers/enriched")

        assert resp.status_code == 200
        servers = resp.json()["servers"]
        assert len(servers) == 2
        names = {s["name"] for s in servers}
        assert names == {"srv-a", "srv-b"}
        for s in servers:
            assert "uptime_seconds" in s
            assert "p95_latency_ms" in s
            assert "ram_bytes" in s
