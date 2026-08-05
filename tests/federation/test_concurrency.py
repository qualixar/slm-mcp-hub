"""W4-P2 tests — BackendConcurrencyGate (concurrency.py).

TDD: written BEFORE implementation. Run first to confirm RED.
"""

from __future__ import annotations

import time

import anyio


class TestBackendConcurrencyGate:
    """BackendConcurrencyGate: per-backend CapacityLimiter semantics."""

    async def test_capacity_limiter_limits_concurrent_slots(self) -> None:
        """BackendConcurrencyGate with max_concurrency=1 allows only 1 concurrent
        acquire; second blocks until the first slot is released."""
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate

        gate = BackendConcurrencyGate(default_max_concurrency=1)
        limiter = gate.acquire("test_backend")

        task2_got_slot = False
        hold_event = anyio.Event()

        async def task1() -> None:
            async with limiter:
                # Verify slot is held before task2 can acquire
                await anyio.sleep(0)  # yield once so task2 can attempt
                await hold_event.wait()

        async def task2() -> None:
            nonlocal task2_got_slot
            async with limiter:
                task2_got_slot = True

        async with anyio.create_task_group() as tg:
            tg.start_soon(task1)
            await anyio.sleep(0.01)  # Let task1 acquire the slot

            assert gate.current_usage("test_backend") == 1
            assert not task2_got_slot

            tg.start_soon(task2)
            await anyio.sleep(0.01)  # task2 should still be blocked

            assert not task2_got_slot, "Task2 must be blocked while slot is held"
            hold_event.set()  # Release slot from task1

        assert task2_got_slot, "Task2 must get the slot after task1 releases"
        assert gate.current_usage("test_backend") == 0

    async def test_gate_released_on_cancel(self) -> None:
        """HARD CASE: acquire gate slot, then cancel the coroutine inside the slot.
        Assert gate.current_usage returns 0 after cancel — no slot leak."""
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate

        gate = BackendConcurrencyGate(default_max_concurrency=3)
        limiter = gate.acquire("backend")

        async def long_task() -> None:
            async with limiter:
                await anyio.sleep(10.0)  # sleep long; will be cancelled

        with anyio.CancelScope() as scope:
            scope.deadline = anyio.current_time() + 0.1
            await long_task()

        assert scope.cancelled_caught
        # CRITICAL: slot must be released even on cancellation (RAII via __aexit__)
        assert gate.current_usage("backend") == 0, (
            "Gate slot leaked after cancellation — CapacityLimiter RAII failed"
        )

    async def test_slow_backend_does_not_block_fast(self) -> None:
        """HARD CASE — NO-HOL guarantee: two tasks to DIFFERENT backends.

        slow_backend sleeps 0.3s (simulates a long backend call).
        fast_backend sleeps 0.01s.
        Both run concurrently via anyio.create_task_group.
        Assert fast_backend completes BEFORE slow_backend finishes.
        Per-backend limiters (limit=1 each) ensure independence.
        """
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate

        gate = BackendConcurrencyGate(default_max_concurrency=1)

        completion_times: dict[str, float] = {}
        start_time = time.monotonic()

        async def slow_task() -> None:
            async with gate.acquire("slow_backend"):
                await anyio.sleep(0.3)
            completion_times["slow"] = time.monotonic() - start_time

        async def fast_task() -> None:
            async with gate.acquire("fast_backend"):
                await anyio.sleep(0.01)
            completion_times["fast"] = time.monotonic() - start_time

        async with anyio.create_task_group() as tg:
            tg.start_soon(slow_task)
            tg.start_soon(fast_task)

        # fast should complete before slow — HOL blocking is absent
        assert "fast" in completion_times
        assert "slow" in completion_times
        assert completion_times["fast"] < completion_times["slow"], (
            f"fast ({completion_times['fast']:.3f}s) must complete before "
            f"slow ({completion_times['slow']:.3f}s)"
        )
        # fast should complete well within 0.15s (not waiting for slow's 0.3s)
        assert completion_times["fast"] < 0.15, (
            f"fast_backend took {completion_times['fast']:.3f}s — "
            "should be < 0.15s (no HOL blocking)"
        )

    async def test_null_gate_is_transparent(self) -> None:
        """_NullGate context manager does not block and exits immediately."""
        from slm_mcp_hub.federation.router import _NullGate

        gate = _NullGate()
        entered = False

        async with gate:
            entered = True

        assert entered

    async def test_different_backends_use_different_limiters(self) -> None:
        """Two backends have independent limiters; saturating one does not
        affect the other."""
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate

        gate = BackendConcurrencyGate(default_max_concurrency=1)

        # Saturate backend_a's limiter
        limiter_a = gate.acquire("backend_a")
        limiter_b = gate.acquire("backend_b")

        # Acquire backend_a's slot
        hold_a = anyio.Event()
        b_done = anyio.Event()

        async def hold_backend_a() -> None:
            async with limiter_a:
                await hold_a.wait()

        async def use_backend_b() -> None:
            async with limiter_b:
                # backend_b should proceed even while backend_a slot is held
                b_done.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(hold_backend_a)
            await anyio.sleep(0.01)  # backend_a holds its slot

            assert gate.current_usage("backend_a") == 1
            assert gate.current_usage("backend_b") == 0

            tg.start_soon(use_backend_b)
            # backend_b should complete without waiting for backend_a
            await anyio.sleep(0.05)
            assert b_done.is_set(), (
                "backend_b must not be blocked by backend_a's limiter"
            )

            hold_a.set()  # release backend_a

    def test_stats_returns_correct_snapshot(self) -> None:
        """gate.stats() returns current in_use and max for all created gates."""
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate

        gate = BackendConcurrencyGate(
            default_max_concurrency=5,
            per_server_overrides={"special": 2},
        )

        # Access two backends to populate their records lazily
        _ = gate.acquire("default_server")
        _ = gate.acquire("special")

        stats = gate.stats()
        assert stats["default_server"]["max"] == 5
        assert stats["default_server"]["in_use"] == 0
        assert stats["special"]["max"] == 2
        assert stats["special"]["in_use"] == 0

    def test_current_usage_before_any_acquire_returns_zero(self) -> None:
        """current_usage for an unknown server returns 0 (gate not yet created)."""
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate

        gate = BackendConcurrencyGate()
        assert gate.current_usage("ghost_server") == 0

    def test_per_server_override_applied(self) -> None:
        """BackendConcurrencyGate respects per_server_overrides."""
        from slm_mcp_hub.federation.concurrency import BackendConcurrencyGate

        gate = BackendConcurrencyGate(
            default_max_concurrency=10,
            per_server_overrides={"gemini": 3},
        )
        record_gemini = gate.get_or_create("gemini")
        record_default = gate.get_or_create("other_server")

        assert record_gemini.max_concurrency == 3
        assert record_default.max_concurrency == 10
