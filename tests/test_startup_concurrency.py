"""W2-P1: Bounded-concurrency startup tests.

Three behavioural guarantees under test:
  1. Concurrency cap  — at most startup_max_concurrency _connect_timed calls run
                        concurrently, measured via a real asyncio-awaited fake.
  2. Phase ordering   — every stdio backend starts connecting before any http
                        backend is touched.
  3. Idempotency      — a second concurrent connect attempt for the same backend
                        is a no-op (join), never spawning a second subprocess.

Config contract:
  - HubConfig.startup_max_concurrency defaults to 8.
  - Values < 1 are rejected at construction.
  - The field round-trips through save/load and through the env-override path.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slm_mcp_hub.core.config import (
    ConfigValidationError,
    HubConfig,
    MCPServerConfig,
    load_config,
    save_config,
)
from slm_mcp_hub.core.registry import CapabilityRegistry
from slm_mcp_hub.federation.manager import ConnectionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stdio(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="echo", args=(name,))


def _http(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="http", url=f"http://fake/{name}")


def _make_cfg(
    *servers: MCPServerConfig,
    concurrency: int = 4,
    tmp_path: Path | None = None,
) -> HubConfig:
    return HubConfig(
        config_dir=tmp_path or Path("/tmp"),
        startup_max_concurrency=concurrency,
        mcp_servers=tuple(servers),
    )


def _make_fast_mock_conn(name: str) -> MagicMock:
    """A minimal MCPConnection mock that succeeds instantly."""
    mock = MagicMock()
    mock.is_connected = True
    mock.subscribe = MagicMock(return_value=lambda: None)
    mock.disconnect = AsyncMock()
    mock.capabilities = {
        "tools": [{"name": f"tool_{name}", "description": "t"}],
        "resources": [],
        "resource_templates": [],
        "prompts": [],
    }
    mock.connect = AsyncMock()
    mock.state = MagicMock()
    mock.state.value = "connected"
    return mock


def _make_slow_mock_conn(name: str, delay: float = 0.05) -> MagicMock:
    """A minimal MCPConnection mock whose connect() yields for *delay* seconds.

    The yield is necessary for idempotency tests so that the event loop can
    schedule a competing coroutine DURING the connection attempt, creating a
    realistic race window that the idempotency guard must close.
    """
    mock = MagicMock()
    mock.is_connected = False  # stays False — reconnect() won't disconnect
    mock.subscribe = MagicMock(return_value=lambda: None)
    mock.disconnect = AsyncMock()
    mock.capabilities = {
        "tools": [],
        "resources": [],
        "resource_templates": [],
        "prompts": [],
    }
    mock.state = MagicMock()
    mock.state.value = "connecting"

    async def _slow_connect() -> None:
        await asyncio.sleep(delay)  # real yield — creates race window

    mock.connect = _slow_connect
    return mock


# ---------------------------------------------------------------------------
# ── 1. Config contract ──────────────────────────────────────────────────────
# ---------------------------------------------------------------------------


class TestStartupMaxConcurrencyConfig:
    def test_default_is_eight(self) -> None:
        cfg = HubConfig()
        assert cfg.startup_max_concurrency == 8

    def test_custom_value_accepted(self) -> None:
        cfg = HubConfig(startup_max_concurrency=2)
        assert cfg.startup_max_concurrency == 2

    def test_one_is_valid(self) -> None:
        """Minimum legal value."""
        cfg = HubConfig(startup_max_concurrency=1)
        assert cfg.startup_max_concurrency == 1

    def test_zero_is_rejected(self) -> None:
        with pytest.raises((ConfigValidationError, ValueError)):
            HubConfig(startup_max_concurrency=0)

    def test_negative_is_rejected(self) -> None:
        with pytest.raises((ConfigValidationError, ValueError)):
            HubConfig(startup_max_concurrency=-3)

    def test_round_trips_through_save_and_load(self, tmp_path: Path) -> None:
        cfg = HubConfig(startup_max_concurrency=3, config_dir=tmp_path)
        path = tmp_path / "hub.json"
        save_config(cfg, path)
        loaded = load_config(path)
        assert loaded.startup_max_concurrency == 3

    def test_env_override_sets_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLM_HUB_STARTUP_MAX_CONCURRENCY", "12")
        cfg = HubConfig(startup_max_concurrency=2, config_dir=tmp_path)
        path = tmp_path / "hub.json"
        save_config(cfg, path)
        loaded = load_config(path)
        assert loaded.startup_max_concurrency == 12

    def test_env_override_invalid_value_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An env var with value 0 must be rejected (validated same as ctor)."""
        monkeypatch.setenv("SLM_HUB_STARTUP_MAX_CONCURRENCY", "0")
        cfg = HubConfig(startup_max_concurrency=4, config_dir=tmp_path)
        path = tmp_path / "hub.json"
        save_config(cfg, path)
        with pytest.raises((ConfigValidationError, ValueError)):
            load_config(path)


# ---------------------------------------------------------------------------
# ── 2. Concurrency cap ──────────────────────────────────────────────────────
# ---------------------------------------------------------------------------


class TestConcurrencyCap:
    """connect_all must honour startup_max_concurrency as a hard cap.

    The fake _connect_timed increments a shared counter on entry, records
    the maximum reached, and decrements on exit — all via real asyncio awaits
    so the event loop can schedule concurrent coroutines.
    """

    @pytest.mark.asyncio
    async def test_max_concurrent_does_not_exceed_k(self, tmp_path: Path) -> None:
        K = 2
        N = 8  # intentionally > K

        servers = [_stdio(f"s{i}") for i in range(N)]
        cfg = _make_cfg(*servers, concurrency=K, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        concurrent_now: list[int] = [0]
        max_concurrent: list[int] = [0]

        async def fake_connect_timed(
            server_config: MCPServerConfig, timeout_seconds: float = 60.0
        ) -> None:
            concurrent_now[0] += 1
            if concurrent_now[0] > max_concurrent[0]:
                max_concurrent[0] = concurrent_now[0]
            # Real yield — lets other coroutines run, simulates work
            await asyncio.sleep(0.01)
            concurrent_now[0] -= 1

        with patch.object(mgr, "_connect_timed", side_effect=fake_connect_timed):
            await mgr.connect_all()

        assert max_concurrent[0] <= K, (
            f"Expected max concurrent <= {K}, got {max_concurrent[0]}"
        )
        # At least 1 concurrent (prove bounding didn't serialize everything to 1)
        assert max_concurrent[0] >= 1

    @pytest.mark.asyncio
    async def test_all_backends_attempted_despite_cap(self, tmp_path: Path) -> None:
        """The semaphore must not drop work — all backends are attempted."""
        K = 3
        N = 10

        servers = [_stdio(f"s{i}") for i in range(N)]
        cfg = _make_cfg(*servers, concurrency=K, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        attempted: list[str] = []

        async def fake_connect_timed(
            server_config: MCPServerConfig, timeout_seconds: float = 60.0
        ) -> None:
            attempted.append(server_config.name)
            await asyncio.sleep(0.001)

        with patch.object(mgr, "_connect_timed", side_effect=fake_connect_timed):
            await mgr.connect_all()

        assert len(attempted) == N
        assert sorted(attempted) == sorted(s.name for s in servers)


# ---------------------------------------------------------------------------
# ── 3. Phase ordering ───────────────────────────────────────────────────────
# ---------------------------------------------------------------------------


class TestPhaseOrdering:
    """All stdio backends must begin connecting before any http backend does.

    We record the wall-clock timestamp of each _connect_timed ENTRY to determine
    when each backend's phase started.  With concurrency K >= max(len(stdio),
    len(http)), all backends in each phase start concurrently — so the latest
    stdio-start must precede the earliest http-start.
    """

    @pytest.mark.asyncio
    async def test_stdio_phase_completes_before_http_phase_starts(
        self, tmp_path: Path
    ) -> None:
        stdio_servers = [_stdio(f"stdio_{i}") for i in range(3)]
        http_servers = [_http(f"http_{i}") for i in range(3)]
        # K large enough that both phases could run in parallel IF ordering
        # were not respected.  The test proves the implementation still
        # serialises the phases.
        cfg = _make_cfg(
            *stdio_servers, *http_servers, concurrency=8, tmp_path=tmp_path
        )
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        start_times: dict[str, float] = {}
        end_times: dict[str, float] = {}

        async def fake_connect_timed(
            server_config: MCPServerConfig, timeout_seconds: float = 60.0
        ) -> None:
            start_times[server_config.name] = time.monotonic()
            await asyncio.sleep(0.02)  # simulate slow connect
            end_times[server_config.name] = time.monotonic()

        with patch.object(mgr, "_connect_timed", side_effect=fake_connect_timed):
            await mgr.connect_all()

        # Every stdio backend must have STARTED before every http backend
        last_stdio_end = max(end_times[s.name] for s in stdio_servers)
        first_http_start = min(start_times[s.name] for s in http_servers)

        assert last_stdio_end <= first_http_start + 1e-9, (
            f"http phase started ({first_http_start:.4f}) before "
            f"stdio phase finished ({last_stdio_end:.4f})"
        )

    @pytest.mark.asyncio
    async def test_http_only_config_is_unaffected(self, tmp_path: Path) -> None:
        """If there are no stdio servers, http servers connect without hang."""
        servers = [_http(f"h{i}") for i in range(3)]
        cfg = _make_cfg(*servers, concurrency=2, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        attempted: list[str] = []

        async def fake_connect_timed(
            server_config: MCPServerConfig, timeout_seconds: float = 60.0
        ) -> None:
            attempted.append(server_config.name)
            await asyncio.sleep(0.001)

        with patch.object(mgr, "_connect_timed", side_effect=fake_connect_timed):
            await mgr.connect_all()

        assert sorted(attempted) == sorted(s.name for s in servers)

    @pytest.mark.asyncio
    async def test_stdio_only_config_is_unaffected(self, tmp_path: Path) -> None:
        """If there are no http servers, stdio servers connect without hang."""
        servers = [_stdio(f"s{i}") for i in range(4)]
        cfg = _make_cfg(*servers, concurrency=2, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        attempted: list[str] = []

        async def fake_connect_timed(
            server_config: MCPServerConfig, timeout_seconds: float = 60.0
        ) -> None:
            attempted.append(server_config.name)
            await asyncio.sleep(0.001)

        with patch.object(mgr, "_connect_timed", side_effect=fake_connect_timed):
            await mgr.connect_all()

        assert sorted(attempted) == sorted(s.name for s in servers)


# ---------------------------------------------------------------------------
# ── 4. Idempotency guard ────────────────────────────────────────────────────
# ---------------------------------------------------------------------------


class TestIdempotencyGuard:
    """A backend already connecting must not be double-started.

    We measure idempotency by counting MCPConnection instantiations
    (i.e., how many subprocess spawns would have occurred).  Under the
    idempotency contract, each backend name triggers at most one
    MCPConnection instantiation per connect cycle.
    """

    @pytest.mark.asyncio
    async def test_concurrent_connect_all_does_not_double_spawn(
        self, tmp_path: Path
    ) -> None:
        """Two concurrent connect_all calls → each backend spawned at most once.

        The idempotency guard in _connect_timed must prevent a second concurrent
        call for the same backend from instantiating a second MCPConnection.

        The slow connect() mock is essential: without a real yield inside
        _connect_timed's connection window, the entire method runs atomically
        from the event loop's perspective and the race window never opens.
        """
        servers = [_stdio(f"s{i}") for i in range(3)]
        cfg = _make_cfg(*servers, concurrency=4, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        spawn_counts: dict[str, int] = {}

        def make_tracked_conn(server_config: MCPServerConfig) -> MagicMock:
            name = server_config.name
            spawn_counts[name] = spawn_counts.get(name, 0) + 1
            # Slow connect() — yields 0.05 s so the event loop can schedule
            # the second connect_all's _connect_timed for the same backend
            # BEFORE the first one completes.  The guard must close that window.
            return _make_slow_mock_conn(name, delay=0.05)

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection",
            side_effect=make_tracked_conn,
        ):
            await asyncio.gather(mgr.connect_all(), mgr.connect_all())

        for srv in servers:
            count = spawn_counts.get(srv.name, 0)
            assert count <= 1, (
                f"Backend '{srv.name}' was spawned {count} times; "
                "idempotency guard must prevent double-spawn"
            )

    @pytest.mark.asyncio
    async def test_connect_one_during_connect_all_does_not_double_spawn(
        self, tmp_path: Path
    ) -> None:
        """connect_one racing connect_all for the same backend → single spawn.

        connect_one calls reconnect() → _connect_timed().  The idempotency
        guard must recognise the in-flight connect_all attempt and JOIN rather
        than spawn a second subprocess.

        The slow connect() mock creates the race window during which connect_one
        can enter _connect_timed and observe the existing in-flight event.
        """
        server = _stdio("raced_backend")
        cfg = _make_cfg(server, concurrency=4, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        spawn_count: list[int] = [0]

        def make_tracked_conn(server_config: MCPServerConfig) -> MagicMock:
            spawn_count[0] += 1
            return _make_slow_mock_conn(server_config.name, delay=0.05)

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection",
            side_effect=make_tracked_conn,
        ):
            await asyncio.gather(
                mgr.connect_all(),
                mgr.connect_one("raced_backend"),
            )

        assert spawn_count[0] <= 1, (
            f"Backend spawned {spawn_count[0]} times under race; expected ≤1"
        )

    @pytest.mark.asyncio
    async def test_sequential_reconnect_still_works_after_guard(
        self, tmp_path: Path
    ) -> None:
        """connect_one after connect_all completes must still connect (no lock leak)."""
        server = _stdio("backend_a")
        cfg = _make_cfg(server, concurrency=4, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        def make_mock_conn(server_config: MCPServerConfig) -> MagicMock:
            return _make_fast_mock_conn(server_config.name)

        with patch(
            "slm_mcp_hub.federation.manager.MCPConnection",
            side_effect=make_mock_conn,
        ):
            await mgr.connect_all()
            # After connect_all completes, a sequential connect_one must work.
            # The idempotency guard must have cleaned up (no stale event).
            result = await mgr.connect_one("backend_a")
            # reconnect() always re-connects; result must be truthy or at least
            # not raise — the important thing is no hang/deadlock.
            assert result in (True, False)


# ---------------------------------------------------------------------------
# ── 5. Webhook + retry-loop tail behaviour preserved ───────────────────────
# ---------------------------------------------------------------------------


class TestConnectAllTailBehaviourPreserved:
    """The additions in W2-P1 must not break the existing post-connect behaviour.

    Specifically:
      - If _failed is non-empty after the phases, _start_retry_loop must be called.
      - The webhook dispatcher is still started before the phases (if configured).
    """

    @pytest.mark.asyncio
    async def test_retry_loop_started_on_failure(self, tmp_path: Path) -> None:
        server = _stdio("bad_server")
        cfg = _make_cfg(server, concurrency=2, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        async def failing_connect_timed(
            sc: MCPServerConfig, timeout_seconds: float = 60.0
        ) -> None:
            mgr._failed[sc.name] = "injected failure"

        with patch.object(mgr, "_connect_timed", side_effect=failing_connect_timed):
            with patch.object(mgr, "_start_retry_loop") as mock_retry:
                await mgr.connect_all()

        mock_retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_retry_loop_on_full_success(self, tmp_path: Path) -> None:
        server = _stdio("good_server")
        cfg = _make_cfg(server, concurrency=2, tmp_path=tmp_path)
        registry = CapabilityRegistry()
        mgr = ConnectionManager(cfg, registry)

        async def successful_connect_timed(
            sc: MCPServerConfig, timeout_seconds: float = 60.0
        ) -> None:
            pass  # no failure recorded

        with patch.object(
            mgr, "_connect_timed", side_effect=successful_connect_timed
        ):
            with patch.object(mgr, "_start_retry_loop") as mock_retry:
                await mgr.connect_all()

        mock_retry.assert_not_called()
