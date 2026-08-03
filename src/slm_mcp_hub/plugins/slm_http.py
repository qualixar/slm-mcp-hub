"""Shared authenticated HTTP client contract for SLM daemon plugins."""

from __future__ import annotations

import os

import httpx

SLM_API_KEY_ENV = "SLM_API_KEY"
SLM_API_KEY_HEADER = "X-SLM-API-Key"
SLM_AUTH_REJECTION_STATUSES = frozenset({401, 403})


def create_slm_http_client(
    timeout: float,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create an SLM client with optional API-key authentication.

    The credential remains environment-backed and is never copied into hub config.
    An empty key intentionally sends no header for trusted loopback deployments.
    """
    api_key = os.environ.get(SLM_API_KEY_ENV, "").strip()
    headers = {SLM_API_KEY_HEADER: api_key} if api_key else {}
    return httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        transport=transport,
    )


def is_slm_auth_rejection(response: httpx.Response) -> bool:
    """Return whether the daemon rejected the configured authentication."""
    return response.status_code in SLM_AUTH_REJECTION_STATUSES
