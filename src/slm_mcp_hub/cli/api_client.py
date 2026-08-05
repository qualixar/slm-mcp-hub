"""Shared HTTP helpers for CLI → hub management REST API.

Centralises base-URL resolution and authentication so every CLI command that
talks to the running hub's management API attaches the API key when one is
configured.

Why this exists (GitHub #29)
----------------------------
The hub's HTTP middleware (:func:`slm_mcp_hub.server.http_server` —
``require_api_key``) rejects every request except ``/api/health`` with 401 when
``SLM_HUB_API_KEY`` is set. The CLI commands (``tools``, ``status --verbose``,
``reconnect``, ``server *``, ``servers``, ``health``, ``warm``, ``stop``)
previously issued bare ``httpx`` calls with no auth header, so they always
returned 401 against an auth-enabled deployment. Plain ``status`` worked only
because it hits the exempt ``/api/health`` endpoint — masking the bug locally.

:func:`hub_headers` reads ``SLM_HUB_API_KEY`` at call time and returns the
``X-SLM-Hub-API-Key`` header the middleware expects, or an empty mapping when
no key is set (so loopback deployments with auth disabled are unchanged).
"""

from __future__ import annotations

import os

from slm_mcp_hub.core.config import load_config

#: Header the hub middleware reads for management-API authentication.
#: Must match ``http_server.require_api_key`` (case-insensitive on the wire;
#: lowercase here to mirror the server-side ``request.headers.get`` lookup).
API_KEY_HEADER = "x-slm-hub-api-key"

#: Environment variable holding the management-API key.
API_KEY_ENV = "SLM_HUB_API_KEY"


def hub_url() -> str:
    """Return the running hub's base URL, e.g. ``http://127.0.0.1:52414``."""
    cfg = load_config()
    return f"http://{cfg.host}:{cfg.port}"


def hub_headers() -> dict[str, str]:
    """Return auth headers for a management-API call.

    Reads :data:`API_KEY_ENV` from the environment at call time. When it is set
    and non-blank, returns ``{API_KEY_HEADER: <key>}``. When unset or
    whitespace-only, returns an empty dict so unauthenticated loopback hubs keep
    working exactly as before.

    Returning a fresh dict per call keeps the result safe to mutate/merge at the
    call site and picks up any change to the environment between invocations.
    """
    key = os.environ.get(API_KEY_ENV, "").strip()
    return {API_KEY_HEADER: key} if key else {}
