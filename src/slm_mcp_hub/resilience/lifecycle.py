"""W1-P1 — Lifecycle event model and transition table.

Provides:
- :class:`LifecycleEvent` — immutable (frozen) record emitted on every state
  transition.  Consumed by health aggregators, event-bus clients (W1-P4),
  audit logs, and optional webhook dispatchers (W1-P4).
- :data:`LIFECYCLE_TRANSITIONS` — frozenset of ``(from_state, to_state)``
  pairs documenting every *designed* edge in the connection state machine.
- :func:`is_valid_transition` — O(1) query against the table.

Design notes
------------
The table captures the *designed* topology (LLD §2) plus all legacy edges
that the existing connect/disconnect code traverses.  At runtime,
``MCPConnection._transition`` LOGS a warning on an off-table edge rather than
hard-rejecting it — this preserves backward compatibility while giving
operators visibility into unexpected paths.

``LifecycleEvent`` is intentionally thin: it records what happened and why,
not what should happen next.  Routing decisions live in the supervisor (W1-P2)
and failure classifier (W1-P3).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slm_mcp_hub.federation.connection import ConnectionState


@dataclasses.dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """Immutable record of a single connection-state transition.

    Attributes
    ----------
    server:
        Name of the MCP server that changed state.
    from_state:
        The state the connection was in *before* the transition.
    to_state:
        The state the connection moved *into*.
    reason:
        Human-readable explanation of why the transition occurred.
    ts:
        Unix timestamp (``time.time()``) at the moment of the transition.
    failure_class:
        Optional classifier string (e.g. ``"TRANSIENT"``, ``"AUTH"``,
        ``"TERMINAL"``).  Set by the failure classifier in W1-P3.
    attempt:
        Optional retry attempt counter.  Set by the supervisor in W1-P2.
    """

    server: str
    from_state: "ConnectionState"
    to_state: "ConnectionState"
    reason: str
    ts: float
    failure_class: str | None = None
    attempt: int | None = None


def _build_transition_table() -> frozenset[tuple[str, str]]:
    """Return the frozenset of designed (from_value, to_value) edges.

    Using string values (not enum members) keeps the table importable without
    triggering a circular-import cycle between resilience.lifecycle and
    federation.connection.
    """
    # fmt: off
    return frozenset({
        # ----------------------------------------------------------------
        # Legacy edges (existing connect/disconnect code paths — MUST stay)
        # ----------------------------------------------------------------
        ("disconnected",  "connecting"),       # connect() entry
        ("connecting",    "connected"),        # successful init
        ("connecting",    "error"),            # ConnectionError during connect
        ("connecting",    "auth_required"),    # OAuthAuthRequiredError
        ("connected",     "draining"),         # drain_and_disconnect()
        ("connected",     "disconnected"),     # fast disconnect()
        ("draining",      "disconnected"),     # drain complete → disconnect
        ("error",         "disconnected"),     # reconnect clears error
        ("error",         "connecting"),       # retry after error
        ("auth_required", "disconnected"),     # logout / forced disconnect
        ("auth_required", "connecting"),       # user ran auth login, retrying

        # ----------------------------------------------------------------
        # New lifecycle edges (LLD §2 — used by W1-P2 supervisor)
        # ----------------------------------------------------------------
        # Start-up path
        ("disconnected",  "starting"),         # supervisor.start()
        ("starting",      "initializing"),     # transport connected, MCP init
        ("initializing",  "ready"),            # MCP handshake complete

        # Degraded / transient errors (still serving)
        ("ready",         "degraded"),         # partial error, still usable
        ("degraded",      "ready"),            # self-healed

        # Reconnect path
        ("ready",         "reconnecting"),     # transient drop
        ("degraded",      "reconnecting"),     # degraded past threshold
        ("connected",     "reconnecting"),     # legacy connected → reconnect
        ("reconnecting",  "initializing"),     # backoff elapsed, retry
        ("reconnecting",  "circuit_open"),     # breaker tripped

        # Circuit-breaker half-open → close
        ("circuit_open",  "reconnecting"),     # probe attempt

        # Graceful shutdown
        ("ready",         "draining"),         # drain from new READY state
        ("draining",      "stopped"),          # drain complete → idle

        # Terminal failure (from any live state)
        ("connecting",    "failed"),
        ("connected",     "failed"),
        ("starting",      "failed"),
        ("initializing",  "failed"),
        ("ready",         "failed"),
        ("degraded",      "failed"),
        ("reconnecting",  "failed"),
        ("circuit_open",  "failed"),
        ("draining",      "failed"),

        # AUTH_REQUIRED from new lifecycle states
        ("starting",      "auth_required"),
        ("initializing",  "auth_required"),
        ("ready",         "auth_required"),
    })
    # fmt: on


# Public read-only table — every designed edge in the state machine.
LIFECYCLE_TRANSITIONS: frozenset[tuple[str, str]] = _build_transition_table()


def is_valid_transition(from_state: "ConnectionState", to_state: "ConnectionState") -> bool:
    """Return True if ``(from_state, to_state)`` is a designed transition.

    Parameters
    ----------
    from_state:
        Current :class:`~slm_mcp_hub.federation.connection.ConnectionState`.
    to_state:
        Target :class:`~slm_mcp_hub.federation.connection.ConnectionState`.

    Returns
    -------
    bool
        ``True`` iff the pair appears in :data:`LIFECYCLE_TRANSITIONS`.

    Note
    ----
    A ``False`` return does **not** mean the transition is prevented at
    runtime — ``MCPConnection._transition`` is fail-open (logs a warning
    but proceeds).  This function is for validation, tests, and tooling.
    """
    return (from_state.value, to_state.value) in LIFECYCLE_TRANSITIONS
