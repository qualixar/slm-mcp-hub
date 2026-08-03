"""Authentication contract for SLM daemon HTTP clients."""

from __future__ import annotations

import httpx
import pytest

from slm_mcp_hub.plugins.slm_http import create_slm_http_client


@pytest.mark.asyncio
async def test_client_sends_api_key_to_strict_daemon(monkeypatch) -> None:
    monkeypatch.setenv("SLM_API_KEY", "api-key-sentinel")
    requests: list[httpx.Request] = []

    def strict_daemon(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("X-SLM-API-Key") != "api-key-sentinel":
            return httpx.Response(403, json={"detail": "forbidden"})
        return httpx.Response(200, json={"ok": True})

    async with create_slm_http_client(
        timeout=2.0,
        transport=httpx.MockTransport(strict_daemon),
    ) as client:
        response = await client.post("http://slm.test/api/v3/tool-event", json={})

    assert response.status_code == 200
    assert requests[0].headers["X-SLM-API-Key"] == "api-key-sentinel"


@pytest.mark.asyncio
async def test_client_omits_auth_header_for_trusted_loopback(monkeypatch) -> None:
    monkeypatch.delenv("SLM_API_KEY", raising=False)
    requests: list[httpx.Request] = []

    def trusted_daemon(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with create_slm_http_client(
        timeout=2.0,
        transport=httpx.MockTransport(trusted_daemon),
    ) as client:
        response = await client.get("http://127.0.0.1:8765/status")

    assert response.status_code == 200
    assert "X-SLM-API-Key" not in requests[0].headers
