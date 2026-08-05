"""W2-P2: Non-blocking serve + readiness gating tests.

Behavioural guarantees under test:

  1. start_background_connect() returns immediately — the caller is never
     blocked waiting for slow backends.
  2. The background connect task is tracked on the runtime (_bg_connect_task).
  3. start_background_connect() is idempotent while a task is in flight —
     second call is a no-op that returns the same task reference.
  4. A new task is created when the previous task already finished (restart
     case).
  5. An optional post_connect callback receives the failure dict after
     connect_all completes.
  6. An exception in post_connect is swallowed (logged) and does NOT propagate
     out of the background task.
  7. stop() cancels an in-flight background connect task cleanly.
  8. stop() is idempotent — double-call does not raise.
  9. stop() works even if start_background_connect() was never called.
  10. After stop(), _bg_connect_task.done() is True (no asyncio task leak).
  11. connect_all() awaitable signature is unchanged — existing callers work.

No real subprocesses are spawned; all "slow" connects are asyncio-awaited
fakes controlled by asyncio.Event / asyncio.sleep.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from slm_mcp_hub.core.config import HubConfig
from slm_mcp_hub.core.hub import reset_hub

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_hub_singleton():
    """Ensure the hub singleton is clean before and after each test."""
    reset_hub()
    yield
    reset_hub()


def _make_config() -> HubConfig:
    return HubConfig(host="127.0.0.1", port=52414, mcp_servers=())


# ---------------------------------------------------------------------------
# TestStartBackgroundConnect
# ---------------------------------------------------------------------------


class TestStartBackgroundConnect:
    """start_background_connect() launches connect_all as a tracked task."""

    @pytest.mark.asyncio
    async def test_returns_immediately_with_slow_connect(self):
        """start_background_connect returns before the connect coroutine finishes."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        connect_started = asyncio.Event()
        connect_release = asyncio.Event()

        async def slow_connect_all() -> dict[str, str]:
            connect_started.set()
            await connect_release.wait()
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", slow_connect_all):
                # This must return without blocking
                runtime.start_background_connect()

                # Task is in flight — started, not done
                await asyncio.wait_for(connect_started.wait(), timeout=1.0)
                assert runtime._bg_connect_task is not None
                assert not runtime._bg_connect_task.done()

                # Clean up
                connect_release.set()
                await runtime.stop()

    @pytest.mark.asyncio
    async def test_task_is_tracked_on_runtime(self):
        """After start_background_connect(), _bg_connect_task is set."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        released = asyncio.Event()

        async def slow_connect() -> dict[str, str]:
            await released.wait()
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            assert runtime._bg_connect_task is None  # starts None

            with patch.object(runtime._conn_manager, "connect_all", slow_connect):
                runtime.start_background_connect()
                assert runtime._bg_connect_task is not None  # now tracked

                released.set()
                await runtime.stop()

    @pytest.mark.asyncio
    async def test_idempotent_when_task_in_flight(self):
        """Second call while task is in flight returns the same task, no new spawn."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        connect_started = asyncio.Event()
        release = asyncio.Event()
        call_count = 0

        async def slow_connect() -> dict[str, str]:
            nonlocal call_count
            call_count += 1
            connect_started.set()
            await release.wait()
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", slow_connect):
                runtime.start_background_connect()
                # Wait for the task coroutine to actually start executing
                await asyncio.wait_for(connect_started.wait(), timeout=1.0)
                first_task = runtime._bg_connect_task
                assert call_count == 1  # connect_all started exactly once

                runtime.start_background_connect()  # idempotent — no-op
                second_task = runtime._bg_connect_task

                assert first_task is second_task  # same object, no new spawn
                assert call_count == 1  # still 1, not 2

                release.set()
                await runtime.stop()

    @pytest.mark.asyncio
    async def test_new_task_created_when_previous_done(self):
        """start_background_connect creates a new task if the previous one finished."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        call_count = 0

        async def instant_connect() -> dict[str, str]:
            nonlocal call_count
            call_count += 1
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", instant_connect):
                runtime.start_background_connect()
                first_task = runtime._bg_connect_task
                await asyncio.sleep(0.05)  # let first task finish
                assert first_task is not None and first_task.done()

                runtime.start_background_connect()  # should create a new task
                second_task = runtime._bg_connect_task
                # Give the second task a chance to run while still inside the patch
                await asyncio.sleep(0.05)

            await runtime.stop()

        assert second_task is not first_task
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_post_connect_callback_called_with_failure_dict(self):
        """post_connect hook receives the exact failure dict from connect_all."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        captured: dict[str, str] = {}

        async def connect_with_failure() -> dict[str, str]:
            return {"bad-server": "connection refused"}

        async def post_connect(failed: dict[str, str]) -> None:
            captured.update(failed)

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", connect_with_failure):
                runtime.start_background_connect(post_connect=post_connect)
                await asyncio.sleep(0.05)
                await runtime.stop()

        assert captured == {"bad-server": "connection refused"}

    @pytest.mark.asyncio
    async def test_post_connect_exception_is_swallowed(self):
        """A RuntimeError in post_connect does not propagate; task finishes normally."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        async def instant_connect() -> dict[str, str]:
            return {}

        async def bad_hook(failed: dict[str, str]) -> None:
            raise RuntimeError("hook explosion")

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", instant_connect):
                runtime.start_background_connect(post_connect=bad_hook)
                await asyncio.sleep(0.05)
                task = runtime._bg_connect_task

            await runtime.stop()

        assert task is not None
        assert task.done()
        assert not task.cancelled()
        # Task completed normally (exception swallowed) — .exception() returns None
        assert task.exception() is None


# ---------------------------------------------------------------------------
# TestStop
# ---------------------------------------------------------------------------


class TestStop:
    """stop() cancels the background connect task then disconnects cleanly."""

    @pytest.mark.asyncio
    async def test_cancels_in_flight_task(self):
        """stop() cancels the background connect task if still running."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        connect_started = asyncio.Event()
        cancelled_flag = asyncio.Event()

        async def slow_connect() -> dict[str, str]:
            connect_started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled_flag.set()
                raise
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", slow_connect):
                runtime.start_background_connect()
                await asyncio.wait_for(connect_started.wait(), timeout=1.0)
                await runtime.stop()

        assert cancelled_flag.is_set(), "Background connect task was not cancelled"

    @pytest.mark.asyncio
    async def test_task_done_after_stop(self):
        """After stop(), _bg_connect_task.done() is True — no asyncio task leak."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        release = asyncio.Event()

        async def slow_connect() -> dict[str, str]:
            await release.wait()
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", slow_connect):
                runtime.start_background_connect()
                await runtime.stop()
                # Task must be done — no leak / no orphaned pending task
                assert runtime._bg_connect_task is not None
                assert runtime._bg_connect_task.done()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        """Calling stop() twice does not raise."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        async def instant_connect() -> dict[str, str]:
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", instant_connect):
                runtime.start_background_connect()
                await runtime.stop()
                await runtime.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_stop_after_connect_completes(self):
        """stop() after the connect task finishes works cleanly."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        async def instant_connect() -> dict[str, str]:
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", instant_connect):
                runtime.start_background_connect()
                await asyncio.sleep(0.05)  # let it finish
                assert runtime._bg_connect_task is not None
                assert runtime._bg_connect_task.done()
                # stop() should not raise even though the task is already done
                await runtime.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_background_connect(self):
        """stop() works even if start_background_connect was never called."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            assert runtime._bg_connect_task is None
            # Must not raise
            await runtime.stop()


# ---------------------------------------------------------------------------
# TestConnectAllUnchanged
# ---------------------------------------------------------------------------


class TestConnectAllUnchanged:
    """connect_all() awaitable signature is unchanged — programmatic callers work."""

    @pytest.mark.asyncio
    async def test_direct_await_returns_empty_dict_no_servers(self):
        """await runtime.connect_all() still works (no config servers → empty dict)."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            result = await runtime.connect_all()
            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_direct_await_works_while_bg_task_in_flight(self):
        """await runtime.connect_all() can be called even while bg task is running."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        release = asyncio.Event()
        bg_started = asyncio.Event()

        async def slow_connect() -> dict[str, str]:
            bg_started.set()
            await release.wait()
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", slow_connect):
                runtime.start_background_connect()
                await asyncio.wait_for(bg_started.wait(), timeout=1.0)

                # While bg is in flight, the direct await path should also work
                # (uses same W2-P1 idempotency — joins the in-flight attempt)
                release.set()
                result = await runtime.connect_all()
                assert isinstance(result, dict)

                await runtime.stop()


# ---------------------------------------------------------------------------
# TestReadinessBehavior
# ---------------------------------------------------------------------------


class TestReadinessBehavior:
    """Serve is reachable before a slow backend finishes; registry is queryable."""

    @pytest.mark.asyncio
    async def test_registry_queryable_while_slow_backend_connecting(self):
        """Tool count and status queries work while a slow backend is still connecting."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        slow_started = asyncio.Event()

        async def slow_connect() -> dict[str, str]:
            slow_started.set()
            await asyncio.sleep(10)  # still connecting
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", slow_connect):
                # start_background_connect returns immediately
                runtime.start_background_connect()
                await asyncio.wait_for(slow_started.wait(), timeout=1.0)

                # Hub is "serving" — registry is queryable right now
                tool_count = runtime.registry.tool_count
                assert isinstance(tool_count, int)  # no hang, no block

                connected = runtime.conn_manager.connected_count
                assert isinstance(connected, int)

                status = runtime.get_status()
                assert "servers_connected" in status
                assert "tools_registered" in status

                # Background task is still in flight
                assert runtime._bg_connect_task is not None
                assert not runtime._bg_connect_task.done()

                # Shutdown cleanly
                await runtime.stop()
                assert runtime._bg_connect_task.done()

    @pytest.mark.asyncio
    async def test_shutdown_mid_connect_no_orphaned_tasks(self):
        """stop() mid-connect leaves no pending asyncio tasks (no warnings)."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        started = asyncio.Event()

        async def very_slow_connect() -> dict[str, str]:
            started.set()
            await asyncio.sleep(60)
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", very_slow_connect):
                runtime.start_background_connect()
                await asyncio.wait_for(started.wait(), timeout=1.0)
                await runtime.stop()

        # After stop: task must be done (no pending coroutine left in the event loop)
        task = runtime._bg_connect_task
        assert task is not None
        assert task.done()


# ---------------------------------------------------------------------------
# TestDefensiveBranches — cover the three guard paths not exercised above
# ---------------------------------------------------------------------------


class TestDefensiveBranches:
    """Cover runtime.py lines 156-157, 173-174, 199-200."""

    @pytest.mark.asyncio
    async def test_start_background_connect_ignored_after_stop(self):
        """start_background_connect() is a no-op once stop() has been called (line 156-157)."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        async def instant_connect() -> dict[str, str]:
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", instant_connect):
                await runtime.stop()  # mark runtime stopped
                assert runtime._stopped is True
                task_before = runtime._bg_connect_task
                runtime.start_background_connect()  # should be silently ignored
                task_after = runtime._bg_connect_task

        # No new task created — _stopped guard fired
        assert task_before is task_after

    @pytest.mark.asyncio
    async def test_unexpected_exception_in_connect_all_is_logged_not_propagated(self):
        """An unexpected non-CancelledError from connect_all is caught and logged (lines 173-174)."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        async def exploding_connect() -> dict[str, str]:
            raise RuntimeError("connect_all exploded unexpectedly")

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(runtime._conn_manager, "connect_all", exploding_connect):
                runtime.start_background_connect()
                await asyncio.sleep(0.05)  # let the task complete
                task = runtime._bg_connect_task

            await runtime.stop()

        # The exception was swallowed — task is done and returns normally (None)
        assert task is not None
        assert task.done()
        assert not task.cancelled()
        assert task.exception() is None  # exception was caught inside _bg_task

    @pytest.mark.asyncio
    async def test_stop_handles_non_cancelled_error_from_task(self):
        """stop() swallows a non-CancelledError raised by the task during cancel (lines 199-200)."""
        from slm_mcp_hub.core.hub import HubOrchestrator
        from slm_mcp_hub.lifecycle.runtime import HubRuntime

        started = asyncio.Event()

        async def connect_that_raises_on_cancel() -> dict[str, str]:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError as exc:
                # Re-raise as a different exception to exercise the except Exception branch
                raise RuntimeError("unexpected during cancel") from exc
            return {}

        config = _make_config()
        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)
            with patch.object(
                runtime._conn_manager, "connect_all", connect_that_raises_on_cancel
            ):
                runtime.start_background_connect()
                await asyncio.wait_for(started.wait(), timeout=1.0)
                # stop() must not propagate the RuntimeError from the task
                await runtime.stop()

        # Task is done; stop() completed without raising
        assert runtime._bg_connect_task is not None
        assert runtime._bg_connect_task.done()
