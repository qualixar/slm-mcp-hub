"""W4-P2 tests — per-server timeout class + NO-HOL concurrency via router.

TDD: written BEFORE implementation. Run first to confirm RED.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import anyio

from slm_mcp_hub.core.config import MCPServerConfig
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.connection import ConnectionState, MCPConnection
from slm_mcp_hub.federation.router import FederationRouter, RouteResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reg(servers: dict[str, list[str]] | None = None) -> CapabilityRegistry:
    """Build a registry with given server→tool-names map.

    Defaults to one backend 'backend' exposing 'tool'.
    """
    if servers is None:
        servers = {"backend": ["tool"]}
    reg = CapabilityRegistry()
    reg.sync({
        sname: {
            "tools": [{"name": t, "description": t} for t in tools],
            "resources": [],
            "prompts": [],
            "resource_templates": [],
        }
        for sname, tools in servers.items()
    })
    return reg


def _make_config(name: str = "backend", timeout_class: str = "default") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="http",
        url=f"http://127.0.0.1:1/{name}",
        timeout_class=timeout_class,
    )


def _make_conn(config: MCPServerConfig) -> MCPConnection:
    conn = MCPConnection(config)
    conn._state = ConnectionState.CONNECTED
    return conn


class _CapturingOutbound:
    """Records read_timeout_seconds passed by the router."""

    def __init__(self, sleep_s: float = 0.0) -> None:
        self._sleep_s = sleep_s
        self.received_timeout: float | None | str = "NOT_SET"

    async def call_tool_streaming(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        read_timeout_seconds: float | None = None,
        progress_callback: Any = None,
        resumption_token: Any = None,
        on_resumption_token: Any = None,
    ) -> dict[str, Any]:
        self.received_timeout = read_timeout_seconds
        if self._sleep_s > 0:
            await anyio.sleep(self._sleep_s)
        return {"content": [{"type": "text", "text": "ok"}]}


# ---------------------------------------------------------------------------
# Timeout class resolution
# ---------------------------------------------------------------------------


class TestTimeoutClassResolution:
    """route_streaming_call resolves correct read_timeout_seconds per class."""

    async def test_route_streaming_call_unbounded_timeout_is_none(self) -> None:
        """Server configured with timeout_class='unbounded'. route_streaming_call
        passes read_timeout_seconds=None to conn.call_tool_streaming."""
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        reg = _make_reg()
        config = _make_config(timeout_class="unbounded")
        conn = _make_conn(config)
        outbound = _CapturingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(
            reg,
            {"backend": conn},
            timeout_registry=TimeoutRegistry(),
        )
        result = await router.route_streaming_call("backend__tool", {})

        assert result.success is True
        assert outbound.received_timeout is None, (
            f"UNBOUNDED class must pass read_timeout_seconds=None, "
            f"got {outbound.received_timeout!r}"
        )

    async def test_route_streaming_call_fast_timeout_is_30s(self) -> None:
        """Server configured with timeout_class='fast'. route_streaming_call
        passes read_timeout_seconds=30.0."""
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        reg = _make_reg()
        config = _make_config(timeout_class="fast")
        conn = _make_conn(config)
        outbound = _CapturingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(
            reg,
            {"backend": conn},
            timeout_registry=TimeoutRegistry(),
        )
        result = await router.route_streaming_call("backend__tool", {})

        assert result.success is True
        assert outbound.received_timeout == 30.0, (
            f"FAST class must pass read_timeout_seconds=30.0, "
            f"got {outbound.received_timeout!r}"
        )

    async def test_route_streaming_call_extended_timeout_is_600s(self) -> None:
        """Server with timeout_class='extended' → read_timeout_seconds=600.0."""
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        reg = _make_reg()
        config = _make_config(timeout_class="extended")
        conn = _make_conn(config)
        outbound = _CapturingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn}, timeout_registry=TimeoutRegistry())
        await router.route_streaming_call("backend__tool", {})
        assert outbound.received_timeout == 600.0

    async def test_route_streaming_call_default_timeout_is_120s(self) -> None:
        """Server with timeout_class='default' → read_timeout_seconds=120.0."""
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        reg = _make_reg()
        config = _make_config(timeout_class="default")
        conn = _make_conn(config)
        outbound = _CapturingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn}, timeout_registry=TimeoutRegistry())
        await router.route_streaming_call("backend__tool", {})
        assert outbound.received_timeout == 120.0

    async def test_timeout_override_takes_precedence_over_class(self) -> None:
        """timeout_override_s=45.0 wins over 'extended' class timeout of 600.0."""
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        reg = _make_reg()
        config = _make_config(timeout_class="extended")
        conn = _make_conn(config)
        outbound = _CapturingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn}, timeout_registry=TimeoutRegistry())
        await router.route_streaming_call("backend__tool", {}, timeout_override_s=45.0)
        assert outbound.received_timeout == 45.0

    async def test_no_timeout_registry_uses_default_timeout_s(self) -> None:
        """When timeout_registry=None, router uses DEFAULT_TOOL_TIMEOUT_S (120s)
        regardless of server timeout_class — backward-compat path."""
        from slm_mcp_hub.core.constants import DEFAULT_TOOL_TIMEOUT_S

        reg = _make_reg()
        # Even though config says unbounded, with no registry the fallback applies
        config = _make_config(timeout_class="unbounded")
        conn = _make_conn(config)
        outbound = _CapturingOutbound()
        conn._outbound = outbound  # type: ignore[assignment]

        # No timeout_registry (backward compat)
        router = FederationRouter(reg, {"backend": conn})
        await router.route_streaming_call("backend__tool", {})
        # Should use DEFAULT_TOOL_TIMEOUT_S not the class
        assert outbound.received_timeout == DEFAULT_TOOL_TIMEOUT_S


# ---------------------------------------------------------------------------
# HARD CASE: UNBOUNDED call does NOT time out
# ---------------------------------------------------------------------------


class TestUnboundedCallBehavior:
    """UNBOUNDED class calls complete normally; DEFAULT class would time out."""

    async def test_route_streaming_call_30min_call_does_not_timeout(self) -> None:
        """HARD CASE: UNBOUNDED-class backend.

        Mock conn.call_tool_streaming sleeps 0.1s (fast in test, represents 30min).
        read_timeout_seconds=None → call completes without TimeoutError.

        Contrast: simulated DEFAULT class with a 0.05s timeout → assert
        the same 0.1s sleep raises a timeout (caught in the error result).
        """
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        reg = _make_reg()

        # --- UNBOUNDED: should succeed ---
        config_unbounded = _make_config(timeout_class="unbounded")
        conn_u = _make_conn(config_unbounded)
        outbound_u = _CapturingOutbound(sleep_s=0.1)
        conn_u._outbound = outbound_u  # type: ignore[assignment]

        router_u = FederationRouter(
            reg, {"backend": conn_u}, timeout_registry=TimeoutRegistry()
        )
        result_u = await router_u.route_streaming_call("backend__tool", {})

        assert result_u.success is True, "UNBOUNDED call should not time out"
        assert outbound_u.received_timeout is None

    async def test_simulated_timeout_with_forced_short_deadline(self) -> None:
        """A call wrapped in a short CancelScope deadline times out as expected.

        This contrasts with the UNBOUNDED case: when the router uses
        read_timeout_seconds=None, an external CancelScope is the only
        way to cancel the call — which is correct by design.
        """
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        reg = _make_reg()
        config = _make_config(timeout_class="unbounded")
        conn = _make_conn(config)

        class _VerySlowOutbound:
            async def call_tool_streaming(self, *a: Any, **kw: Any) -> dict[str, Any]:
                await anyio.sleep(10.0)  # simulate long backend
                return {"content": []}  # pragma: no cover

        conn._outbound = _VerySlowOutbound()  # type: ignore[assignment]

        router = FederationRouter(reg, {"backend": conn}, timeout_registry=TimeoutRegistry())

        start = time.monotonic()
        with anyio.CancelScope() as scope:
            scope.deadline = anyio.current_time() + 0.1
            await router.route_streaming_call("backend__tool", {})

        elapsed = time.monotonic() - start
        assert scope.cancelled_caught, "CancelScope must cancel the long call"
        assert elapsed < 0.3, f"Cancellation took too long: {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# HARD CASE: NO-HOL blocking via the router
# ---------------------------------------------------------------------------


class TestNoHOLBlockingViaRouter:
    """Per-backend gate ensures slow backend A never blocks fast backend B."""

    async def test_no_hol_blocking_via_router(self) -> None:
        """Two concurrent route_streaming_call to DIFFERENT backends.

        slow_server: blocked until signaled (represents 30min Gemini call).
        fast_server: returns in ~0.01s.

        BackendConcurrencyGate with limit=1 per backend.
        Fast server MUST complete while slow server is still blocked.
        """
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        reg = _make_reg({"slow_server": ["slow_tool"], "fast_server": ["fast_tool"]})
        slow_config = _make_config("slow_server", "default")
        fast_config = _make_config("fast_server", "fast")

        slow_conn = _make_conn(slow_config)
        fast_conn = _make_conn(fast_config)

        slow_release = anyio.Event()
        fast_done = anyio.Event()

        class _SlowOutbound:
            async def call_tool_streaming(self, *a: Any, **kw: Any) -> dict[str, Any]:
                await slow_release.wait()
                return {"content": [{"type": "text", "text": "slow"}]}

        class _FastOutbound:
            async def call_tool_streaming(self, *a: Any, **kw: Any) -> dict[str, Any]:
                return {"content": [{"type": "text", "text": "fast"}]}

        slow_conn._outbound = _SlowOutbound()  # type: ignore[assignment]
        fast_conn._outbound = _FastOutbound()  # type: ignore[assignment]

        gate = BackendConcurrencyGate(default_max_concurrency=1)
        router = FederationRouter(
            reg,
            {"slow_server": slow_conn, "fast_server": fast_conn},
            concurrency_gate=gate,
            timeout_registry=TimeoutRegistry(),
        )

        fast_result: list[RouteResult] = []
        slow_result: list[RouteResult] = []

        start = time.monotonic()

        async def slow_call() -> None:
            r = await router.route_streaming_call("slow_server__slow_tool", {})
            slow_result.append(r)

        async def fast_call() -> None:
            r = await router.route_streaming_call("fast_server__fast_tool", {})
            fast_result.append(r)
            fast_done.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(slow_call)
            await anyio.sleep(0.01)  # Let slow acquire its slot

            tg.start_soon(fast_call)

            # Wait for fast to complete (should be quick, not blocked by slow)
            await fast_done.wait()
            elapsed_fast = time.monotonic() - start

            assert fast_result, "Fast call must complete"
            assert fast_result[0].success, "Fast call must succeed"
            assert elapsed_fast < 0.2, (
                f"Fast call took {elapsed_fast:.3f}s — HOL blocking detected!"
            )

            # slow_server's slot is still held, fast_server's is free
            assert gate.current_usage("slow_server") == 1
            assert gate.current_usage("fast_server") == 0

            slow_release.set()  # Release slow now

        assert slow_result and slow_result[0].success, "Slow call must eventually succeed"

    async def test_gate_slot_released_after_cancel_in_router(self) -> None:
        """When route_streaming_call is cancelled, the per-backend gate slot is released."""
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate

        reg = _make_reg()
        config = _make_config()
        conn = _make_conn(config)

        class _BlockingOutbound:
            async def call_tool_streaming(self, *a: Any, **kw: Any) -> dict[str, Any]:
                await anyio.sleep(10.0)  # long-running
                return {}  # pragma: no cover

        conn._outbound = _BlockingOutbound()  # type: ignore[assignment]

        gate = BackendConcurrencyGate(default_max_concurrency=2)
        router = FederationRouter(reg, {"backend": conn}, concurrency_gate=gate)

        with anyio.CancelScope() as scope:
            scope.deadline = anyio.current_time() + 0.1
            await router.route_streaming_call("backend__tool", {})

        assert scope.cancelled_caught
        # CRITICAL: slot must be released on cancel (no leak)
        assert gate.current_usage("backend") == 0, (
            "Gate slot leaked after router-level cancellation"
        )


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    """MCPServerConfig timeout_class and max_concurrency survive save/load JSON."""

    def test_config_round_trip_timeout_class(self, tmp_path: Path) -> None:
        """MCPServerConfig with timeout_class='extended' round-trips through
        save_config/load_config JSON without loss."""
        from slm_mcp_hub.core.config import (
            HubConfig,
            MCPServerConfig,
            load_config,
            save_config,
        )

        config_path = tmp_path / "config.json"

        server = MCPServerConfig(
            name="gemini",
            transport="http",
            url="http://127.0.0.1:9999/mcp",
            timeout_class="extended",
            max_concurrency=3,
        )
        hub_config = HubConfig(
            mcp_servers=(server,),
            config_dir=tmp_path,
        )
        save_config(hub_config, config_path)

        loaded = load_config(config_path)

        assert len(loaded.mcp_servers) == 1
        loaded_server = loaded.mcp_servers[0]
        assert loaded_server.timeout_class == "extended"
        assert loaded_server.max_concurrency == 3

    def test_config_round_trip_unbounded(self, tmp_path: Path) -> None:
        """timeout_class='unbounded' round-trips correctly."""
        from slm_mcp_hub.core.config import (
            HubConfig,
            MCPServerConfig,
            load_config,
            save_config,
        )

        config_path = tmp_path / "config.json"
        server = MCPServerConfig(
            name="deep_research",
            transport="http",
            url="http://127.0.0.1:9999/mcp",
            timeout_class="unbounded",
        )
        save_config(HubConfig(mcp_servers=(server,), config_dir=tmp_path), config_path)
        loaded = load_config(config_path)
        assert loaded.mcp_servers[0].timeout_class == "unbounded"

    def test_config_round_trip_defaults_preserved(self, tmp_path: Path) -> None:
        """A server with no timeout_class/max_concurrency in JSON loads default values."""
        import json

        from slm_mcp_hub.core.config import load_config
        from slm_mcp_hub.federation.timeouts import TIMEOUT_CLASS_DEFAULT

        # Write a config that omits timeout_class and max_concurrency
        raw = {
            "mcpServers": {
                "old_server": {"url": "http://127.0.0.1:8000/mcp", "type": "http"}
            }
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(raw))

        loaded = load_config(config_path)
        assert len(loaded.mcp_servers) == 1
        server = loaded.mcp_servers[0]
        assert server.timeout_class == TIMEOUT_CLASS_DEFAULT
        assert server.max_concurrency == 10
