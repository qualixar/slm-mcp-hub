"""Transport-neutral product models for the Hub protocol layer.

All types here are immutable frozen dataclasses. They must never contain:
- SDK (mcp.*) objects
- Access tokens, refresh tokens, client secrets, auth codes, PKCE verifiers
- Raw authorization headers

Rule: any field whose name contains "token", "secret", "credential", or
"password" is forbidden by the security invariants in 02-ARCHITECTURE-LLD.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping

# ---------------------------------------------------------------------------
# Protocol negotiation
# ---------------------------------------------------------------------------

class ProtocolEra(str, Enum):
    """MCP wire-protocol era, used for capability negotiation."""

    MODERN_2026 = "2026-07-28"
    LEGACY = "legacy"


@dataclass(frozen=True)
class NegotiatedPeer:
    """Outcome of protocol-version negotiation with a peer (upstream or downstream)."""

    era: ProtocolEra
    protocol_version: str
    capabilities: Mapping[str, object]


@dataclass(frozen=True)
class CachePolicy:
    """Caching directive for a federated result."""

    ttl_ms: int
    cache_scope: Literal["private", "public", "no-store"]


@dataclass(frozen=True)
class AuthorizationState:
    """Safe, credential-free snapshot of an upstream connection's auth state.

    This structure deliberately omits tokens, secrets, and headers. It carries
    only the metadata needed for status reporting and lifecycle decisions.
    """

    mode: Literal["none", "static_headers", "oauth"]
    status: Literal["not_required", "auth_required", "authorized", "error"]
    issuer: str | None
    resource: str | None
    scopes: tuple[str, ...]


# ---------------------------------------------------------------------------
# Product operation outcomes (neutral — no SDK types, no wire-specific keys)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CallToolOutcome:
    """Neutral result of any tool call — meta or routed.

    ``content`` is a tuple of JSON-object dicts using the wire-level content
    block shapes (``{"type": "text", "text": "..."}`` etc.).  The shapes are
    stable across all current SDK content block types; conversion.py validates
    and dispatches on the "type" field when building SDK objects.

    ``raw`` carries the upstream server's *verbatim* result dict for routed
    (federated) calls, so keys the Hub does not model — ``structuredContent``,
    ``_meta``, an explicit ``isError: false`` — survive round-trip unchanged.
    It is ``None`` for Hub-generated results (meta-tools, error envelopes),
    which are built from ``content``/``is_error``.
    """

    content: tuple[dict[str, Any], ...]
    is_error: bool
    server_name: str
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolsListOutcome:
    """Neutral tools/list result — always the three meta-tools."""

    tools: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ResourcesListOutcome:
    """Neutral resources/list result."""

    resources: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ResourceTemplatesListOutcome:
    """Neutral resources/templates/list result."""

    resource_templates: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ResourceReadOutcome:
    """Neutral resources/read result.

    ``raw`` is the unwrapped JSON-RPC result dict from the router (or SDK
    client in P04+).  The conversion layer parses it when building SDK types.
    """

    raw: dict[str, Any]


@dataclass(frozen=True)
class PromptsListOutcome:
    """Neutral prompts/list result."""

    prompts: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PromptGetOutcome:
    """Neutral prompts/get result.

    ``raw`` is the unwrapped JSON-RPC result dict from the router (or SDK
    client in P04+).
    """

    raw: dict[str, Any]


@dataclass(frozen=True)
class InitializeOutcome:
    """Neutral response to an MCP initialize request."""

    protocol_version: str
    capabilities: Mapping[str, object]
    server_name: str
    server_version: str


@dataclass(frozen=True)
class DiscoverOutcome:
    """Neutral response to a server/discover request (MCP 2026-07-28)."""

    supported_versions: tuple[str, ...]
    capabilities: Mapping[str, object]
    server_name: str
    server_version: str
    instructions: str
