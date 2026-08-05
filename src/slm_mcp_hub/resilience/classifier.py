"""W1-P3 — Failure taxonomy: classify exceptions into retry-policy buckets.

``classify_failure`` is a **pure function** — no I/O, no state, no side effects.
It maps any ``BaseException`` to one of three :class:`FailureClass` values that
drive the supervisor's next transition:

- ``TRANSIENT`` — retry with backoff (circuit-breaker guards runaway loops).
- ``AUTH`` — stop retrying; wait for an external auth-login trigger (no storm).
- ``TERMINAL`` — call ``mark_failed``; no retry; admin action required.

Default (unrecognized exception)
---------------------------------
Unrecognized exceptions are classified as **TRANSIENT** (conservative).

Rationale: an unknown exception may be intermittent (e.g., a bug that surfaces
only under load).  Classifying as TERMINAL would silently drop the backend
forever on the first unfamiliar error.  Instead, TRANSIENT lets the backoff +
circuit-breaker surface a persistently-failing backend via ``needs_attention``
without abandoning it silently.  The circuit-breaker acts as a safety valve —
after ``failure_threshold`` consecutive failures the breaker opens, probing at a
slow interval and asserting ``needs_attention`` for operator alerting (W1-P4).

Classification ordering (within ``classify_failure``)
------------------------------------------------------
1. **AUTH** — ``OAuthAuthRequiredError`` anywhere in the cause chain. Checked
   first to prevent an auth sentinel being misrouted as TERMINAL or TRANSIENT.
2. **TERMINAL** — types that unambiguously require admin action: bad binary path
   (``FileNotFoundError``), non-executable binary (``PermissionError``), path is
   a directory (``IsADirectoryError``), unsupported transport
   (``NotImplementedError``), missing dependency (``ImportError``).  The full
   cause chain is walked so a ``ConnectionError`` wrapping
   ``FileNotFoundError`` (as produced by ``MCPConnection.connect()``) is still
   classified as TERMINAL.  CRITICAL: TERMINAL types are checked BEFORE the
   broad ``OSError`` / ``ConnectionError`` catch-alls because several TERMINAL
   types are ``OSError`` subclasses — checking TERMINAL first prevents
   ``FileNotFoundError`` from being misclassified as TRANSIENT.
3. **TRANSIENT** — network/IO/timeout exceptions: ``ConnectionError``,
   ``TimeoutError``, ``asyncio.TimeoutError``, ``OSError`` and their subclasses.
4. **DEFAULT** — everything else → TRANSIENT (conservative; see above).
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

from slm_mcp_hub.auth.broker import OAuthAuthRequiredError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Failure class taxonomy
# ---------------------------------------------------------------------------


class FailureClass(str, Enum):
    """Failure taxonomy used by the supervisor to pick the next transition.

    ``str`` mixin ensures values round-trip through JSON and can be compared
    with legacy string constants (e.g. ``failure_class == "TRANSIENT"``).
    """

    TRANSIENT = "TRANSIENT"
    AUTH = "AUTH"
    TERMINAL = "TERMINAL"


# ---------------------------------------------------------------------------
# Terminal exception types (checked BEFORE the broad OSError/ConnectionError)
# ---------------------------------------------------------------------------

# These exceptions unambiguously indicate a non-retryable failure that requires
# operator/admin action:
#   - FileNotFoundError  → stdio command binary does not exist
#   - PermissionError    → stdio command is not executable
#   - IsADirectoryError  → stdio path points to a directory, not a binary
#   - NotImplementedError→ unknown / unsupported transport declared in config
#   - ImportError        → missing transport or protocol module
#                          (ModuleNotFoundError is a subclass of ImportError)
#
# ORDERING NOTE: FileNotFoundError, PermissionError, and IsADirectoryError are
# all subclasses of OSError.  If we checked the broad _TRANSIENT_EXC_TYPES
# (which includes OSError) first, these would be misclassified as TRANSIENT.
# The TERMINAL tuple MUST be checked before the TRANSIENT catch-alls.
_TERMINAL_EXC_TYPES: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    PermissionError,
    IsADirectoryError,
    NotImplementedError,
    ImportError,  # includes ModuleNotFoundError (its subclass)
)

# Transient network/IO/timeout exceptions — retry with backoff.
_TRANSIENT_EXC_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,      # and subclasses: ConnectionRefusedError, ConnectionResetError, …
    TimeoutError,         # and asyncio.TimeoutError (alias in Python ≥ 3.11)
    asyncio.TimeoutError, # explicit for clarity on older Pythons
    OSError,              # base for all network/IO OS errors
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_failure(exc: BaseException) -> FailureClass:
    """Classify *exc* into a :class:`FailureClass` that drives the next supervisor transition.

    Pure function — reads only the exception hierarchy and cause chain; no I/O,
    no module-level state, no side effects.

    Classification order
    --------------------
    1. AUTH  — ``OAuthAuthRequiredError`` (or cause) → no retry storm.
    2. TERMINAL — bad binary path, permission denied, unsupported transport,
       missing dependency.  Checks both the surface exception and its entire
       cause chain (``__cause__`` / ``__context__``), so a ``ConnectionError``
       wrapping ``FileNotFoundError`` is correctly classified as TERMINAL.
    3. TRANSIENT — connection/network/timeout errors → backoff + retry.
    4. DEFAULT (unrecognized) → TRANSIENT (conservative; see module docstring).

    Parameters
    ----------
    exc:
        The exception to classify.  Callers are responsible for re-raising
        ``asyncio.CancelledError`` / ``KeyboardInterrupt`` before calling this.

    Returns
    -------
    FailureClass
        The appropriate retry-policy class.
    """
    chain: list[BaseException] = _cause_chain(exc)

    # Step 1: AUTH — check surface and all causes before anything else.
    if any(isinstance(e, OAuthAuthRequiredError) for e in chain):
        logger.debug("classify_failure: AUTH — OAuthAuthRequiredError in chain: %r", exc)
        return FailureClass.AUTH

    # Step 2: TERMINAL — walk the whole cause chain.
    #
    # CRITICAL: checked BEFORE the broad _TRANSIENT_EXC_TYPES because
    # FileNotFoundError / PermissionError / IsADirectoryError are all OSError
    # subclasses — checking TERMINAL first ensures they are not misrouted to
    # TRANSIENT by the broad OSError catch-all below.
    if any(isinstance(e, _TERMINAL_EXC_TYPES) for e in chain):
        logger.debug(
            "classify_failure: TERMINAL — terminal type found in chain: %r", exc
        )
        return FailureClass.TERMINAL

    # Step 3: TRANSIENT — network/IO/timeout errors.
    if any(isinstance(e, _TRANSIENT_EXC_TYPES) for e in chain):
        logger.debug(
            "classify_failure: TRANSIENT — network/timeout type in chain: %r", exc
        )
        return FailureClass.TRANSIENT

    # Step 4: DEFAULT — unrecognized exception → TRANSIENT (conservative).
    logger.debug(
        "classify_failure: TRANSIENT (default — unrecognized %s): %r",
        type(exc).__name__,
        exc,
    )
    return FailureClass.TRANSIENT


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """Return *exc* plus all its chained causes in order.

    Traversal prefers ``__cause__`` (explicit ``raise X from Y``) over
    ``__context__`` (implicit exception chaining), and respects
    ``__suppress_context__`` (``raise X from None``).

    Stops at 20 hops to guard against artificially deep or circular chains.
    A cycle-detection set ensures termination even on hand-crafted circular
    chains (e.g. in tests).

    Parameters
    ----------
    exc:
        The root exception to start from.  The surface exception is always
        the first element of the returned list.

    Returns
    -------
    list[BaseException]
        Ordered list: ``[exc, cause1, cause2, ...]``.
    """
    _MAX_DEPTH = 20
    seen: list[BaseException] = []
    seen_ids: set[int] = set()
    current: BaseException | None = exc

    while current is not None and len(seen) < _MAX_DEPTH:
        exc_id = id(current)
        if exc_id in seen_ids:
            break  # circular chain detected
        seen.append(current)
        seen_ids.add(exc_id)

        # Prefer explicit cause (__cause__) over implicit context (__context__).
        # When __suppress_context__ is True (raised via ``raise X from None``),
        # the implicit __context__ is intentionally hidden — do not walk it.
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
        else:
            current = None

    return seen
