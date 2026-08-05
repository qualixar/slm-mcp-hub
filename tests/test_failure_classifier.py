"""W1-P3 — Failure classifier unit tests.

TDD: written BEFORE classifier.py existed (RED phase).  Each test maps one or
more representative exceptions to the expected :class:`FailureClass`.

Coverage target: **100% lines** on ``resilience/classifier.py``.

Test structure
--------------
- :class:`TestAuthClass` — ``OAuthAuthRequiredError`` directly and via cause chain.
- :class:`TestTerminalClass` — bad binary path, permission, unsupported transport,
  missing module, and connection.py-style re-wrapping.
- :class:`TestTransientClass` — network/IO/timeout exceptions.
- :class:`TestDefaultTransient` — unrecognized exceptions default to TRANSIENT.
- :class:`TestCauseChain` — root-cause inspection (``__cause__`` / ``__context__``).
- :class:`TestFailureClassEnum` — enum properties (str mixin, values).
- :class:`TestCauseChainHelper` — ``_cause_chain`` edge cases (cycle guard, depth).
"""

from __future__ import annotations

import asyncio

from slm_mcp_hub.auth.broker import OAuthAuthRequiredError
from slm_mcp_hub.resilience.classifier import (
    FailureClass,
    _cause_chain,
    classify_failure,
)

# ---------------------------------------------------------------------------
# AUTH class
# ---------------------------------------------------------------------------


class TestAuthClass:
    """OAuthAuthRequiredError → AUTH (direct and via cause chain)."""

    def test_oauth_auth_required_error_direct(self) -> None:
        exc = OAuthAuthRequiredError("login required")
        assert classify_failure(exc) == FailureClass.AUTH

    def test_oauth_auth_required_subclass(self) -> None:
        class MyAuthError(OAuthAuthRequiredError):
            pass

        exc = MyAuthError("subclass also auth")
        assert classify_failure(exc) == FailureClass.AUTH

    def test_oauth_auth_required_as_explicit_cause(self) -> None:
        """ConnectionError wrapping OAuthAuthRequiredError → AUTH (cause chain walk)."""
        cause = OAuthAuthRequiredError("auth barrier")
        exc = ConnectionError("connect failed")
        exc.__cause__ = cause
        assert classify_failure(exc) == FailureClass.AUTH

    def test_auth_takes_priority_over_terminal_in_cause_chain(self) -> None:
        """AUTH outranks TERMINAL — auth in cause chain wins."""
        auth_cause = OAuthAuthRequiredError("login needed")
        outer = FileNotFoundError("bad binary")  # would be TERMINAL alone
        outer.__cause__ = auth_cause
        assert classify_failure(outer) == FailureClass.AUTH


# ---------------------------------------------------------------------------
# TERMINAL class
# ---------------------------------------------------------------------------


class TestTerminalClass:
    """Non-retryable failures → TERMINAL (admin action required)."""

    def test_file_not_found_error(self) -> None:
        """stdio command binary does not exist."""
        exc = FileNotFoundError("/usr/local/bin/bad-server")
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_permission_error(self) -> None:
        """stdio command is not executable."""
        exc = PermissionError("Permission denied: /opt/mcp-server")
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_is_a_directory_error(self) -> None:
        """stdio path points to a directory, not a binary."""
        exc = IsADirectoryError("/opt/mcp-dir")
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_not_implemented_error(self) -> None:
        """Unknown / unsupported transport declared in config."""
        exc = NotImplementedError("unknown transport: ftp")
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_import_error(self) -> None:
        """Missing transport/protocol module."""
        exc = ImportError("No module named 'mcp.transports.ftp'")
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_module_not_found_error(self) -> None:
        """ModuleNotFoundError is a subclass of ImportError → TERMINAL."""
        exc = ModuleNotFoundError("No module named 'custom_transport'")
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_connection_error_wrapping_file_not_found(self) -> None:
        """connection.py re-wraps non-ConnectionError as ConnectionError via __cause__.

        MCPConnection.connect() catches FileNotFoundError, transitions to ERROR
        with failure_class=TERMINAL, then raises ConnectionError(...) from exc.
        The supervisor sees the outer ConnectionError; we walk the cause chain
        to find the terminal root cause.
        """
        cause = FileNotFoundError("No such file: /bad/mcp-server")
        exc = ConnectionError(
            "MCP init-server initialization failed (FileNotFoundError)"
        )
        exc.__cause__ = cause
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_connection_error_wrapping_permission_error(self) -> None:
        cause = PermissionError("Permission denied: /secure/mcp-server")
        exc = ConnectionError("MCP server initialization failed (PermissionError)")
        exc.__cause__ = cause
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_connection_error_wrapping_not_implemented(self) -> None:
        cause = NotImplementedError("Transport 'grpc' is not supported")
        exc = ConnectionError("MCP init failed (NotImplementedError)")
        exc.__cause__ = cause
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_connection_error_wrapping_import_error(self) -> None:
        cause = ImportError("No module named 'mcp.transports.websocket'")
        exc = ConnectionError("MCP init failed (ImportError)")
        exc.__cause__ = cause
        assert classify_failure(exc) == FailureClass.TERMINAL

    def test_terminal_beats_transient_in_cause_chain(self) -> None:
        """If both transient and terminal appear in chain, first-match (AUTH>TERMINAL) wins.

        This verifies TERMINAL is checked before broad TRANSIENT catch-alls.
        """
        # Outer: OSError (would be TRANSIENT alone)
        # Inner cause: FileNotFoundError (TERMINAL)
        inner = FileNotFoundError("bad binary")
        outer = OSError("os-level error")
        outer.__cause__ = inner
        # TERMINAL wins because we check TERMINAL before TRANSIENT catch-all
        assert classify_failure(outer) == FailureClass.TERMINAL


# ---------------------------------------------------------------------------
# TRANSIENT class
# ---------------------------------------------------------------------------


class TestTransientClass:
    """Retryable network/IO/timeout exceptions → TRANSIENT."""

    def test_plain_connection_error(self) -> None:
        assert classify_failure(ConnectionError("connection refused")) == FailureClass.TRANSIENT

    def test_connection_refused_error(self) -> None:
        """Subclass of ConnectionError — must resolve as TRANSIENT not TERMINAL."""
        assert classify_failure(ConnectionRefusedError("port closed")) == FailureClass.TRANSIENT

    def test_connection_reset_error(self) -> None:
        assert classify_failure(ConnectionResetError("peer reset connection")) == FailureClass.TRANSIENT

    def test_broken_pipe_error(self) -> None:
        assert classify_failure(BrokenPipeError("pipe broken")) == FailureClass.TRANSIENT

    def test_timeout_error(self) -> None:
        assert classify_failure(TimeoutError("timed out")) == FailureClass.TRANSIENT

    def test_asyncio_timeout_error(self) -> None:
        """asyncio.TimeoutError (Python 3.11+ alias for TimeoutError) → TRANSIENT."""
        assert classify_failure(asyncio.TimeoutError()) == FailureClass.TRANSIENT

    def test_os_error_generic(self) -> None:
        """Generic OSError (e.g. ECONNRESET, network unreachable) → TRANSIENT."""
        assert classify_failure(OSError("Network unreachable")) == FailureClass.TRANSIENT

    def test_os_error_errno_econnrefused(self) -> None:
        import errno
        exc = OSError(errno.ECONNREFUSED, "Connection refused")
        assert classify_failure(exc) == FailureClass.TRANSIENT

    def test_connection_error_with_no_terminal_cause(self) -> None:
        """Pure ConnectionError with no special cause → TRANSIENT."""
        exc = ConnectionError("transient network blip")
        assert classify_failure(exc) == FailureClass.TRANSIENT

    def test_connection_error_wrapping_os_error(self) -> None:
        """OSError cause (non-terminal) → TRANSIENT."""
        cause = OSError("EHOSTUNREACH")
        exc = ConnectionError("failed to connect")
        exc.__cause__ = cause
        assert classify_failure(exc) == FailureClass.TRANSIENT


# ---------------------------------------------------------------------------
# Default: unrecognized exceptions → TRANSIENT
# ---------------------------------------------------------------------------


class TestDefaultTransient:
    """Unrecognized exceptions fall through to TRANSIENT (conservative default).

    Rationale: unknown exceptions may be intermittent.  Classifying as TERMINAL
    would silently drop the backend on the first unfamiliar error.  TRANSIENT
    lets the backoff + circuit-breaker surface ``needs_attention`` for operators.
    """

    def test_generic_exception_is_transient(self) -> None:
        assert classify_failure(Exception("something unexpected")) == FailureClass.TRANSIENT

    def test_runtime_error_is_transient(self) -> None:
        assert classify_failure(RuntimeError("mystery error")) == FailureClass.TRANSIENT

    def test_value_error_is_transient(self) -> None:
        """ValueError (no terminal signal) → TRANSIENT (not assumed config error)."""
        assert classify_failure(ValueError("unexpected value")) == FailureClass.TRANSIENT

    def test_key_error_is_transient(self) -> None:
        assert classify_failure(KeyError("missing key")) == FailureClass.TRANSIENT

    def test_attribute_error_is_transient(self) -> None:
        assert classify_failure(AttributeError("no attr")) == FailureClass.TRANSIENT

    def test_mock_exception_is_transient(self) -> None:
        """Fully unknown exception class (e.g. from a plugin) → TRANSIENT."""

        class PluginException(Exception):
            pass

        assert classify_failure(PluginException("plugin error")) == FailureClass.TRANSIENT


# ---------------------------------------------------------------------------
# Cause-chain traversal edge cases
# ---------------------------------------------------------------------------


class TestCauseChain:
    """Cause-chain traversal: __cause__, __context__, nesting, and cycle guard."""

    def test_deeply_nested_terminal_cause(self) -> None:
        """Terminal type buried 3 levels deep in __cause__ chain → TERMINAL."""
        root = FileNotFoundError("binary not found")
        mid = RuntimeError("wrapper")
        mid.__cause__ = root
        outer = ConnectionError("connect failed")
        outer.__cause__ = mid
        assert classify_failure(outer) == FailureClass.TERMINAL

    def test_context_chain_traversed(self) -> None:
        """__context__ (implicit exception chaining) also inspected.

        Simulates what Python does when ``raise outer`` fires inside an
        ``except FileNotFoundError`` block: Python sets ``outer.__context__ =
        inner`` and leaves ``outer.__suppress_context__ = False``.
        We set this up manually to avoid a ``raise X`` inside an except clause
        (which ruff B904 flags).
        """
        inner = FileNotFoundError("bad binary")
        outer = ConnectionError("connect failed")
        # Manually simulate implicit exception chaining
        outer.__context__ = inner
        outer.__suppress_context__ = False  # default; context is NOT suppressed
        assert classify_failure(outer) == FailureClass.TERMINAL

    def test_suppress_context_respected(self) -> None:
        """When __suppress_context__ is True (raise X from None), __context__ not walked."""
        inner = FileNotFoundError("bad binary")
        outer = ConnectionError("different error, suppressed context")
        # Simulate 'raise outer from None' — suppress_context is True
        outer.__context__ = inner
        outer.__suppress_context__ = True
        # Without walking __context__, only outer (ConnectionError) is seen → TRANSIENT
        assert classify_failure(outer) == FailureClass.TRANSIENT

    def test_explicit_cause_preferred_over_context(self) -> None:
        """__cause__ takes priority over __context__ in the chain walk."""
        transient_cause = OSError("network blip")  # explicit cause → TRANSIENT
        terminal_context = FileNotFoundError("bad binary")  # context → TERMINAL

        exc = ConnectionError("connect failed")
        exc.__cause__ = transient_cause  # explicit cause: transient
        exc.__context__ = terminal_context  # implicit context: terminal

        # __cause__ is walked first; transient_cause has no further terminal causes.
        # Result: TRANSIENT (OSError → TRANSIENT, no terminal in __cause__ sub-chain).
        # Note: __context__ of exc is NOT walked because __cause__ is set (exc has __cause__).
        assert classify_failure(exc) == FailureClass.TRANSIENT


# ---------------------------------------------------------------------------
# _cause_chain helper edge cases
# ---------------------------------------------------------------------------


class TestCauseChainHelper:
    """Direct tests of the _cause_chain() internal helper."""

    def test_single_exception_returns_list_of_one(self) -> None:
        exc = ValueError("lone")
        chain = _cause_chain(exc)
        assert chain == [exc]

    def test_two_level_explicit_cause(self) -> None:
        root = FileNotFoundError("root")
        outer = ConnectionError("outer")
        outer.__cause__ = root
        chain = _cause_chain(outer)
        assert chain == [outer, root]

    def test_cycle_guard_stops_at_20(self) -> None:
        """Artificially deep chain (≥ 20 unique links) stops at the depth cap."""
        chain_head = Exception("e0")
        current = chain_head
        for i in range(25):
            child = Exception(f"e{i + 1}")
            current.__cause__ = child
            current = child
        result = _cause_chain(chain_head)
        assert len(result) == 20  # hard cap

    def test_circular_chain_terminates(self) -> None:
        """Circular __cause__ chain terminates without infinite loop."""
        exc_a = Exception("a")
        exc_b = Exception("b")
        exc_a.__cause__ = exc_b
        exc_b.__cause__ = exc_a  # cycle: a → b → a …
        result = _cause_chain(exc_a)
        # Should see exc_a, then exc_b, then detect exc_a again → stop
        assert len(result) == 2
        assert exc_a in result
        assert exc_b in result


# ---------------------------------------------------------------------------
# FailureClass enum properties
# ---------------------------------------------------------------------------


class TestFailureClassEnum:
    """FailureClass is a str-mixin Enum — values must be usable as strings."""

    def test_values_are_strings(self) -> None:
        assert FailureClass.TRANSIENT == "TRANSIENT"
        assert FailureClass.AUTH == "AUTH"
        assert FailureClass.TERMINAL == "TERMINAL"

    def test_three_members(self) -> None:
        assert len(FailureClass) == 3

    def test_enum_equality_with_string(self) -> None:
        assert FailureClass.TRANSIENT == "TRANSIENT"
        assert FailureClass.AUTH == "AUTH"
        assert FailureClass.TERMINAL == "TERMINAL"

    def test_enum_in_set_lookup(self) -> None:
        terminal_classes = {FailureClass.TERMINAL}
        assert FailureClass.TERMINAL in terminal_classes
        assert FailureClass.TRANSIENT not in terminal_classes
