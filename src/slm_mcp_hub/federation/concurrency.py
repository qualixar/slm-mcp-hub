"""Per-backend concurrency gate for W4 no-HOL-block guarantee.

``BackendConcurrencyGate`` holds one ``anyio.CapacityLimiter`` per backend
name.  Default per-backend concurrency = 10 (overridable via
``MCPServerConfig.max_concurrency``).

NO-HOL guarantee: the hub dispatches each incoming MCP request in its own
anyio task (handled by ``StreamableHTTPSessionManager``).  Each task calls
``route_streaming_call``, which acquires the **per-backend** limiter via
``async with gate.acquire(server_name): ...``.  Two calls to different
backends use **independent** ``CapacityLimiter`` objects, so a 30-minute
Gemini call never delays a 100 ms GitHub call.

RAII cancellation safety: ``anyio.CapacityLimiter`` implements ``__aexit__``
which always releases the slot — even on ``Cancelled``.  Verified by:
    tests/federation/test_concurrency.py::test_gate_released_on_cancel
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

import anyio

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level defaults (importable as constants)
# ---------------------------------------------------------------------------

DEFAULT_PER_BACKEND_CONCURRENCY: Final[int] = 10
"""Default concurrent-slot cap per backend when no per-server override is set."""


# ---------------------------------------------------------------------------
# Internal record type
# ---------------------------------------------------------------------------


@dataclass
class _GateRecord:
    """Holds the CapacityLimiter and configuration for one backend.

    ``limiter`` is used as an async context manager:
        ``async with record.limiter: ...``
    """

    limiter: anyio.CapacityLimiter
    max_concurrency: int


# ---------------------------------------------------------------------------
# BackendConcurrencyGate
# ---------------------------------------------------------------------------


class BackendConcurrencyGate:
    """Manages per-backend ``anyio.CapacityLimiter`` instances.

    Created once per hub and injected into ``FederationRouter``.

    Usage in ``route_streaming_call``::

        async with gate.acquire(server_name):
            result = await conn.call_tool_streaming(...)

    The context manager is the ``CapacityLimiter`` itself — anyio's RAII
    implementation guarantees the slot is released in ``__aexit__`` even
    when the outer task is cancelled.

    Parameters
    ----------
    default_max_concurrency:
        Slot cap applied to any backend not listed in ``per_server_overrides``.
    per_server_overrides:
        Optional mapping of ``{server_name: max_concurrency}`` for backends
        that need a tighter or wider cap than the default.
    """

    def __init__(
        self,
        default_max_concurrency: int = DEFAULT_PER_BACKEND_CONCURRENCY,
        per_server_overrides: dict[str, int] | None = None,
    ) -> None:
        self._default = default_max_concurrency
        self._overrides: dict[str, int] = per_server_overrides or {}
        self._gates: dict[str, _GateRecord] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create(self, server_name: str) -> _GateRecord:
        """Return the existing gate record or create one lazily.

        ``anyio.CapacityLimiter`` construction is synchronous and safe to
        call from any context.

        Parameters
        ----------
        server_name:
            The backend identifier (matches ``MCPServerConfig.name``).
        """
        if server_name not in self._gates:
            max_c = self._overrides.get(server_name, self._default)
            self._gates[server_name] = _GateRecord(
                limiter=anyio.CapacityLimiter(max_c),
                max_concurrency=max_c,
            )
        return self._gates[server_name]

    def acquire(self, server_name: str) -> anyio.CapacityLimiter:
        """Return the ``CapacityLimiter`` for ``server_name``.

        Use as an async context manager::

            async with gate.acquire("gemini"):
                ...

        Each call to ``acquire("gemini")`` returns the **same** limiter
        object for that server — the call is idempotent and O(1) after the
        first creation.

        Parameters
        ----------
        server_name:
            Backend identifier.

        Returns
        -------
        anyio.CapacityLimiter
            The per-backend limiter; use via ``async with``.
        """
        return self.get_or_create(server_name).limiter

    def current_usage(self, server_name: str) -> int:
        """Number of borrowed slots for ``server_name`` at this instant.

        Returns 0 if the gate for ``server_name`` has not been created yet
        (i.e. no call has been made to that backend).

        Parameters
        ----------
        server_name:
            Backend identifier.
        """
        record = self._gates.get(server_name)
        if record is None:
            return 0
        return int(record.limiter.borrowed_tokens)

    def stats(self) -> dict[str, dict[str, int]]:
        """Snapshot of all gate stats for health/diagnostics endpoints.

        Returns
        -------
        dict
            ``{server_name: {"max": N, "in_use": M}}`` for every backend
            that has been accessed at least once.
        """
        return {
            name: {
                "max": rec.max_concurrency,
                "in_use": int(rec.limiter.borrowed_tokens),
            }
            for name, rec in self._gates.items()
        }
