"""Per-server timeout classes for W4 long-running calls.

TimeoutClass is a named policy bucket. TimeoutRegistry maps class names to
timeout values and is populated from operator-supplied overrides merged with
built-in defaults.

Pure module — no async, no I/O.  Unit-tested exhaustively in
tests/federation/test_timeouts.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in class name constants (imported by constants.py + config.py)
# ---------------------------------------------------------------------------

TIMEOUT_CLASS_FAST: Final[str] = "fast"
TIMEOUT_CLASS_DEFAULT: Final[str] = "default"
TIMEOUT_CLASS_EXTENDED: Final[str] = "extended"
TIMEOUT_CLASS_UNBOUNDED: Final[str] = "unbounded"

#: All valid timeout class names — validated by validate_server_config.
VALID_TIMEOUT_CLASSES: Final[frozenset[str]] = frozenset(
    {TIMEOUT_CLASS_FAST, TIMEOUT_CLASS_DEFAULT, TIMEOUT_CLASS_EXTENDED, TIMEOUT_CLASS_UNBOUNDED}
)


# ---------------------------------------------------------------------------
# TimeoutPolicy — immutable value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeoutPolicy:
    """Resolved policy for one timeout class.

    Attributes
    ----------
    timeout_s:
        Seconds passed as ``read_timeout_seconds`` to the SDK call.
        ``None`` = wait forever (UNBOUNDED class). The SDK still sends
        ``notifications/cancelled`` when an outer anyio ``CancelScope`` fires.
    keepalive_interval_s:
        If set, outbound HTTP clients should send a keepalive ping every N
        seconds to prevent idle proxy resets on long streams.
        ``None`` = no keepalive (FAST / DEFAULT classes).
    """

    timeout_s: float | None
    keepalive_interval_s: float | None = None


# ---------------------------------------------------------------------------
# Built-in default policies (all overridable via operator config)
# ---------------------------------------------------------------------------

BUILTIN_TIMEOUT_POLICIES: Final[dict[str, TimeoutPolicy]] = {
    TIMEOUT_CLASS_FAST: TimeoutPolicy(timeout_s=30.0),
    TIMEOUT_CLASS_DEFAULT: TimeoutPolicy(timeout_s=120.0),
    TIMEOUT_CLASS_EXTENDED: TimeoutPolicy(timeout_s=600.0, keepalive_interval_s=55.0),
    TIMEOUT_CLASS_UNBOUNDED: TimeoutPolicy(timeout_s=None, keepalive_interval_s=55.0),
}


# ---------------------------------------------------------------------------
# TimeoutRegistry
# ---------------------------------------------------------------------------


class TimeoutRegistry:
    """Resolves timeout policies for named classes.

    Merges built-in defaults with operator-supplied overrides.  Immutable
    after construction — create one per hub startup and share via
    ``FederationRouter``.

    Parameters
    ----------
    overrides:
        Optional dict of ``{class_name: TimeoutPolicy}`` overrides.
        Keys must be one of the four built-in class names (unknown keys
        add new policies that can be selected via ``MCPServerConfig.timeout_class``).
    """

    def __init__(
        self,
        overrides: dict[str, TimeoutPolicy] | None = None,
    ) -> None:
        self._policies: dict[str, TimeoutPolicy] = {
            **BUILTIN_TIMEOUT_POLICIES,
            **(overrides or {}),
        }

    def resolve(self, class_name: str) -> TimeoutPolicy:
        """Return policy for ``class_name``; falls back to DEFAULT if unknown.

        A misconfigured ``class_name`` is logged as a warning and demoted
        to DEFAULT (fail-open: unknown class never causes a call to be
        rejected).
        """
        policy = self._policies.get(class_name)
        if policy is None:
            logger.warning(
                "Unknown timeout class %r; falling back to default", class_name
            )
            return self._policies[TIMEOUT_CLASS_DEFAULT]
        return policy

    def resolve_for_server(
        self,
        server_timeout_class: str,
        call_timeout_override_s: float | None,
    ) -> TimeoutPolicy:
        """Resolve effective policy; a per-call override takes precedence.

        Parameters
        ----------
        server_timeout_class:
            The ``timeout_class`` from ``MCPServerConfig``.
        call_timeout_override_s:
            Explicit seconds from the caller of ``route_streaming_call``.
            If not ``None``, replaces ``timeout_s`` while keeping
            ``keepalive_interval_s`` from the class policy.
        """
        policy = self.resolve(server_timeout_class)
        if call_timeout_override_s is not None:
            return TimeoutPolicy(
                timeout_s=call_timeout_override_s,
                keepalive_interval_s=policy.keepalive_interval_s,
            )
        return policy
