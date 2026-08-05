"""W5-P1 TDD — enrich_server_status() unit tests.

TDD: written BEFORE implementation. Tests cover:
- uptime_seconds added correctly (connected vs not-connected)
- p95_latency_ms from MetricsCollector or 0.0 fallback
- ram_bytes via psutil or None fallback
- immutability (input not mutated)
- never-raises contract
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_entry(name: str = "server-a", **kwargs: Any) -> dict[str, Any]:
    """Build a minimal status dict as build_server_status() would produce."""
    base: dict[str, Any] = {
        "name": name,
        "transport": "stdio",
        "connected": True,
        "lifecycle": "connected",
        "tools": 3,
        "restart_count": 0,
        "consecutive_failures": 0,
        "needs_attention": False,
        "last_error": None,
    }
    base.update(kwargs)
    return base


def _make_conn(
    *,
    is_connected: bool = True,
    uptime: float = 100.0,
    transport: str = "stdio",
    pid: int | None = 1234,
) -> MagicMock:
    """Build a mock MCPConnection."""
    conn = MagicMock()
    conn.is_connected = is_connected
    conn.uptime_seconds = uptime if is_connected else 0.0
    conn.process_pid = pid if is_connected and transport == "stdio" else None
    return conn


def _make_metrics(p95: float = 45.5) -> MagicMock:
    """Build a mock MetricsCollector returning p95 value."""
    mc = MagicMock()
    mc.get_server_metrics.return_value = {"p95_duration_ms": p95}
    return mc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnrichUptimeSeconds:
    def test_enrich_adds_uptime_seconds(self) -> None:
        """enrich_server_status() adds uptime_seconds field to each entry.
        A connected MCPConnection with _connected_at set 100s ago yields
        uptime_seconds ~100."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a")]
        connections = {"srv-a": _make_conn(uptime=100.0)}

        result = enrich_server_status(entries, connections)

        assert len(result) == 1
        assert "uptime_seconds" in result[0]
        assert abs(result[0]["uptime_seconds"] - 100.0) < 1.0

    def test_enrich_uptime_zero_when_not_connected(self) -> None:
        """When MCPConnection.is_connected == False, uptime_seconds == 0.0."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-b", connected=False)]
        connections = {"srv-b": _make_conn(is_connected=False, uptime=0.0)}

        result = enrich_server_status(entries, connections)

        assert result[0]["uptime_seconds"] == 0.0

    def test_enrich_uptime_zero_when_no_connection(self) -> None:
        """When there is no MCPConnection for a server, uptime_seconds == 0.0."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-c")]
        result = enrich_server_status(entries, {})

        assert result[0]["uptime_seconds"] == 0.0

    def test_enrich_uptime_never_negative(self) -> None:
        """uptime_seconds is never negative — even for freshly connected backends."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        conn = _make_conn(uptime=0.0)
        conn.uptime_seconds = 0.0
        entries = [_make_entry("srv-d")]
        result = enrich_server_status(entries, {"srv-d": conn})

        assert result[0]["uptime_seconds"] >= 0.0


class TestEnrichP95Latency:
    def test_enrich_p95_from_metrics_collector(self) -> None:
        """When MetricsCollector is provided, enrich_server_status() reads
        get_server_metrics(name)['p95_duration_ms'] and writes it as p95_latency_ms."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a")]
        connections = {"srv-a": _make_conn()}
        metrics = _make_metrics(p95=45.5)

        result = enrich_server_status(entries, connections, metrics=metrics)

        assert result[0]["p95_latency_ms"] == 45.5
        metrics.get_server_metrics.assert_called_once_with("srv-a")

    def test_enrich_p95_zero_when_no_metrics(self) -> None:
        """When metrics=None, p95_latency_ms == 0.0 for all entries."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a"), _make_entry("srv-b")]
        connections = {"srv-a": _make_conn(), "srv-b": _make_conn()}

        result = enrich_server_status(entries, connections, metrics=None)

        assert result[0]["p95_latency_ms"] == 0.0
        assert result[1]["p95_latency_ms"] == 0.0

    def test_enrich_multiple_entries_metrics(self) -> None:
        """Each entry gets its own p95_latency_ms from metrics."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        mc = MagicMock()
        mc.get_server_metrics.side_effect = lambda name: {
            "p95_duration_ms": 10.0 if name == "srv-a" else 20.0
        }

        entries = [_make_entry("srv-a"), _make_entry("srv-b")]
        connections = {"srv-a": _make_conn(), "srv-b": _make_conn()}

        result = enrich_server_status(entries, connections, metrics=mc)

        assert result[0]["p95_latency_ms"] == 10.0
        assert result[1]["p95_latency_ms"] == 20.0


class TestEnrichRamBytes:
    def test_enrich_ram_bytes_none_without_psutil(self) -> None:
        """When psutil is not installed (mock ImportError), ram_bytes is None.
        No exception is raised — graceful degradation."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a")]
        connections = {"srv-a": _make_conn(pid=9999)}

        with patch.dict(sys.modules, {"psutil": None}):
            result = enrich_server_status(entries, connections)

        assert result[0]["ram_bytes"] is None  # graceful degradation

    def test_enrich_ram_bytes_none_for_http_backend(self) -> None:
        """For a backend with transport=='http', process_pid returns None
        → ram_bytes is None."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-http", transport="http")]
        conn = _make_conn(transport="http", pid=None)
        connections = {"srv-http": conn}

        result = enrich_server_status(entries, connections)

        assert result[0]["ram_bytes"] is None

    def test_enrich_ram_bytes_uses_psutil_when_available(self) -> None:
        """When psutil is available and pid is valid, ram_bytes is an integer."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a")]
        conn = _make_conn(pid=1234)
        connections = {"srv-a": conn}

        mock_psutil = MagicMock()
        mock_process = MagicMock()
        mock_process.memory_info.return_value.rss = 16_384_000
        mock_psutil.Process.return_value = mock_process
        mock_psutil.NoSuchProcess = ProcessLookupError
        mock_psutil.AccessDenied = PermissionError

        with patch.dict(sys.modules, {"psutil": mock_psutil}):
            result = enrich_server_status(entries, connections)

        assert result[0]["ram_bytes"] == 16_384_000

    def test_enrich_ram_bytes_none_when_disconnected(self) -> None:
        """ram_bytes is None when the connection is not active."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a", connected=False)]
        conn = _make_conn(is_connected=False, pid=None)
        connections = {"srv-a": conn}

        result = enrich_server_status(entries, connections)

        assert result[0]["ram_bytes"] is None


class TestEnrichImmutability:
    def test_enrich_is_immutable(self) -> None:
        """Input list[dict] is NOT mutated. Each entry in the returned list is a NEW dict."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a")]
        original_keys = set(entries[0].keys())
        original_id = id(entries[0])

        connections = {"srv-a": _make_conn()}
        result = enrich_server_status(entries, connections)

        # Original entry unchanged
        assert set(entries[0].keys()) == original_keys
        assert "uptime_seconds" not in entries[0]
        assert "p95_latency_ms" not in entries[0]
        assert "ram_bytes" not in entries[0]

        # Result is a NEW object
        assert id(result[0]) != original_id

    def test_enrich_returns_new_list(self) -> None:
        """The returned list is a new list object, not the same reference."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a")]
        result = enrich_server_status(entries, {})

        assert result is not entries

    def test_enrich_empty_input(self) -> None:
        """enrich_server_status([]) returns an empty list without error."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        result = enrich_server_status([], {})
        assert result == []


class TestEnrichNeverRaises:
    def test_enrich_raises_never_on_metrics_exception(self) -> None:
        """enrich_server_status() catches all internal exceptions; never raises.
        Mock get_server_metrics to raise RuntimeError; assert result still has
        p95_latency_ms=0.0."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        mc = MagicMock()
        mc.get_server_metrics.side_effect = RuntimeError("simulated crash")

        entries = [_make_entry("srv-a")]
        connections = {"srv-a": _make_conn()}

        # Must NOT raise
        result = enrich_server_status(entries, connections, metrics=mc)

        assert result[0]["p95_latency_ms"] == 0.0

    def test_enrich_raises_never_on_psutil_exception(self) -> None:
        """If psutil.Process() raises unexpectedly, ram_bytes is None — no propagation."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        entries = [_make_entry("srv-a")]
        conn = _make_conn(pid=1234)

        mock_psutil = MagicMock()
        mock_psutil.Process.side_effect = RuntimeError("unexpected")
        mock_psutil.NoSuchProcess = ProcessLookupError
        mock_psutil.AccessDenied = PermissionError

        with patch.dict(sys.modules, {"psutil": mock_psutil}):
            result = enrich_server_status(entries, {"srv-a": conn})

        assert result[0]["ram_bytes"] is None  # graceful, no raise

    def test_enrich_raises_never_on_uptime_exception(self) -> None:
        """If conn.uptime_seconds raises, uptime_seconds is 0.0 — no propagation."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        conn = MagicMock()
        conn.is_connected = True
        type(conn).uptime_seconds = property(lambda self: (_ for _ in ()).throw(RuntimeError("broken")))
        conn.process_pid = None

        entries = [_make_entry("srv-a")]

        result = enrich_server_status(entries, {"srv-a": conn})

        assert result[0]["uptime_seconds"] == 0.0

    def test_enrich_raises_never_on_process_pid_exception(self) -> None:
        """If conn.process_pid raises, ram_bytes is None — no propagation.
        Covers status_enriched.py lines 45-46: except Exception: return None."""
        from slm_mcp_hub.observability.status_enriched import enrich_server_status

        conn = MagicMock()
        conn.is_connected = True
        # Make process_pid property raise an exception
        type(conn).process_pid = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("pid access denied"))
        )
        type(conn).uptime_seconds = property(lambda self: 60.0)

        entries = [_make_entry("srv-a")]

        result = enrich_server_status(entries, {"srv-a": conn})

        assert result[0]["ram_bytes"] is None  # graceful, no raise


class TestOutboundClientProcessPid:
    """Unit tests for OutboundClient.process_pid (W5-P1 new property).

    Covers outbound.py lines 134-139 — the new process_pid property.
    These lines are NOT covered by status_enriched tests (those mock conn directly).
    """

    def test_process_pid_http_transport_returns_none(self) -> None:
        """HTTP transport has no subprocess → process_pid is None immediately."""
        from slm_mcp_hub.core.config import MCPServerConfig
        from slm_mcp_hub.protocol.outbound import OutboundClient

        cfg = MCPServerConfig(name="srv", transport="http", url="http://localhost:9000")
        client = OutboundClient(cfg)

        assert client.process_pid is None

    def test_process_pid_stdio_not_connected_returns_none(self) -> None:
        """stdio transport but _client is None (not connected) → process_pid is None."""
        from slm_mcp_hub.core.config import MCPServerConfig
        from slm_mcp_hub.protocol.outbound import OutboundClient

        cfg = MCPServerConfig(name="srv", transport="stdio", command="echo")
        client = OutboundClient(cfg)
        # _client starts as None in __init__; process_pid should return None

        assert client.process_pid is None

    def test_process_pid_stdio_no_transport_attribute_returns_none(self) -> None:
        """stdio transport, client connected but SDK doesn't expose _transport.
        AttributeError is caught → returns None. This is the real mcp==2.0.0 path.
        Covers the try/except AttributeError block in outbound.py lines 136-139."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.core.config import MCPServerConfig
        from slm_mcp_hub.protocol.outbound import OutboundClient

        cfg = MCPServerConfig(name="srv", transport="stdio", command="echo")
        client = OutboundClient(cfg)
        # Inject a mock client with spec=[] so ANY attribute access raises AttributeError
        client._client = MagicMock(spec=[])  # type: ignore[assignment]

        assert client.process_pid is None


class TestMCPConnectionProcessPid:
    """Unit tests for MCPConnection.process_pid (W5-P1 new property).

    Covers the delegation path in connection.py lines 151-153.
    """

    def test_connection_process_pid_none_when_no_outbound(self) -> None:
        """When MCPConnection._outbound is None, process_pid returns None."""
        from slm_mcp_hub.core.config import MCPServerConfig
        from slm_mcp_hub.federation.connection import MCPConnection

        cfg = MCPServerConfig(name="srv", transport="stdio", command="echo")
        conn = MCPConnection(cfg)
        # _outbound is None after construction (not connected)

        assert conn.process_pid is None

    def test_connection_process_pid_delegates_to_outbound(self) -> None:
        """MCPConnection.process_pid delegates to OutboundClient.process_pid."""
        from unittest.mock import MagicMock

        from slm_mcp_hub.core.config import MCPServerConfig
        from slm_mcp_hub.federation.connection import MCPConnection

        cfg = MCPServerConfig(name="srv", transport="stdio", command="echo")
        conn = MCPConnection(cfg)

        mock_outbound = MagicMock()
        mock_outbound.process_pid = 5678
        conn._outbound = mock_outbound  # type: ignore[assignment]

        assert conn.process_pid == 5678
