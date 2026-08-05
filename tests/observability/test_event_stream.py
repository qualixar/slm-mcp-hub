"""W5-P2 TDD — EventStreamBridge unit tests.

TDD: written BEFORE implementation. All tests must FAIL before
observability/event_stream.py exists, then PASS after implementation.

Test plan (per LLD §12 W5-P2):
1. stream yields one SSE chunk per emit with the event's fields.
2. HARD: slow/dead client never blocks emit() (put_nowait guarantee).
3. drop-oldest on overflow (queue retains newest, WARNINGs logged).
4. unsubscribe on disconnect (finally block, no leaked consumer).
5. keepalive emitted after idle (SSE_KEEPALIVE_INTERVAL_S patched).
6. multiple clients isolated (each queue independent).
7. SSE data contains NO secrets (explicit whitelist assertion).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from slm_mcp_hub.resilience.events import LifecycleEventBus
from slm_mcp_hub.resilience.lifecycle import LifecycleEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(server: str = "srv-a") -> LifecycleEvent:
    """Build a minimal LifecycleEvent using ConnectionState enum values."""
    from slm_mcp_hub.federation.connection import ConnectionState

    return LifecycleEvent(
        server=server,
        from_state=ConnectionState.DISCONNECTED,
        to_state=ConnectionState.CONNECTED,
        reason="test-reason",
        ts=1_700_000_000.0,
        failure_class=None,
        attempt=None,
    )


# ---------------------------------------------------------------------------
# Test 1 — stream yields one SSE chunk per emit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_receives_event_after_emit() -> None:
    """CORE: after bus.emit(event), stream() yields one 'event: lifecycle\\ndata: {...}\\n\\n'
    chunk containing the event's safe fields."""
    from slm_mcp_hub.observability.event_stream import EventStreamBridge

    bus = LifecycleEventBus()
    bridge = EventStreamBridge(bus=bus, queue_maxsize=10)
    event = _make_event("srv-a")

    gen = bridge.stream()

    # Emit into the queue (synchronous put_nowait via consumer).
    # Consumer isn't registered until the generator starts (first __anext__).
    # Strategy: schedule the __anext__ task, yield to event loop to start the
    # generator and register the consumer, then emit.
    chunk_holder: list[str] = []

    async def _advance() -> None:
        chunk_holder.append(await gen.__anext__())

    task = asyncio.create_task(_advance())
    await asyncio.sleep(0)  # allow generator to start and register consumer
    bus.emit(event)
    await task  # wait for the yield

    await gen.aclose()

    assert len(chunk_holder) == 1
    chunk = chunk_holder[0]
    assert chunk.startswith("event: lifecycle\ndata: ")
    assert chunk.endswith("\n\n")

    data = json.loads(chunk.split("data: ", 1)[1].rstrip())
    assert data["server"] == "srv-a"
    assert data["from_state"] == "disconnected"
    assert data["to_state"] == "connected"
    assert data["reason"] == "test-reason"
    assert data["ts"] == pytest.approx(1_700_000_000.0)


# ---------------------------------------------------------------------------
# Test 2 — HARD: slow client never blocks emit()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_client_does_not_block_emit() -> None:
    """HARD CASE (non-blocking guarantee):
    - EventStreamBridge with queue_maxsize=2.
    - Second SYNCHRONOUS consumer records timestamps for each emit call.
    - The bridge queue is NOT drained (simulate a lagging/dead client).
    - 10 events are emitted via bus.emit().
    - Assert the second consumer was called for ALL 10 events.
    - Assert total emit() wall time < 0.1s (put_nowait is synchronous).
    This proves emit() is NEVER slowed by a lagging SSE client.
    """
    from slm_mcp_hub.observability.event_stream import EventStreamBridge

    bus = LifecycleEventBus()
    bridge = EventStreamBridge(bus=bus, queue_maxsize=2)

    # Independent second consumer — records timestamps for all 10 target emits.
    timestamps: list[float] = []
    bus.register_consumer(lambda _: timestamps.append(time.monotonic()))

    gen = bridge.stream()

    # Use task+sleep(0) pattern: start the generator task so it registers
    # the bridge consumer and begins awaiting queue.get().
    first_chunk_holder: list[str] = []

    async def _get_one() -> None:
        first_chunk_holder.append(await gen.__anext__())

    first_task = asyncio.create_task(_get_one())
    # Yield to event loop: generator starts, consumer is registered, generator
    # suspends at asyncio.wait_for(queue.get(), ...) — no event has arrived yet.
    await asyncio.sleep(0)

    # Reset timestamps before the 10 target emits (ignore any prior counts).
    timestamps.clear()

    # Emit 10 events synchronously WITHOUT draining the bridge queue.
    # srv-0 is absorbed by the pending getter (asyncio.Queue pending-getter path);
    # srv-1..9 fill the bounded queue (maxsize=2), triggering drop-oldest for most.
    # ALL bridge-consumer operations are synchronous put_nowait/get_nowait — no await.
    #
    # Timing proof: we mock logger.warning during the 10 emits so that logging
    # overhead (which IS synchronous CPU work, NOT asyncio blocking) does not
    # contaminate the wall-time measurement. The intent of the timing check is
    # to prove NO asyncio await happens, not to benchmark logging.
    from unittest.mock import patch as _patch

    import slm_mcp_hub.observability.event_stream as _es_mod

    with _patch.object(_es_mod.logger, "warning"):
        start = time.monotonic()
        for i in range(10):
            bus.emit(_make_event(f"srv-{i}"))
        elapsed = time.monotonic() - start

    # Let the pending task complete (it gets one event from the queue).
    await asyncio.wait_for(first_task, timeout=1.0)

    await gen.aclose()

    # Second consumer received ALL 10 events (independent of bridge queue state).
    assert len(timestamps) == 10, f"Expected 10, got {len(timestamps)}"
    # 10 synchronous put_nowait/get_nowait calls with no logging: sub-millisecond.
    assert elapsed < 0.1, f"emit() blocked: {elapsed:.4f}s — put_nowait must never block"


# ---------------------------------------------------------------------------
# Test 3 — drop-oldest on overflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_drop_oldest_on_overflow(caplog: Any) -> None:
    """When queue_maxsize=3 and 5 events are emitted without draining:
    - Queue retains the 3 MOST RECENT events (oldest 2 dropped).
    - A WARNING is logged for each dropped event.
    - Consuming the queue yields events 2, 3, 4 in order.
    """
    import logging

    from slm_mcp_hub.observability.event_stream import EventStreamBridge

    bus = LifecycleEventBus()
    bridge = EventStreamBridge(bus=bus, queue_maxsize=3)

    gen = bridge.stream()

    # Step 1: register the consumer and advance past the first yield using
    # the task+sleep(0)+emit pattern so the generator is suspended at 'yield'
    # (NOT at 'await queue.get()') before the 5 test events are emitted.
    # This prevents the asyncio.Queue direct-transfer optimisation from absorbing
    # one event invisibly into the getter future rather than the bounded queue.
    prime_chunk_holder: list[str] = []

    async def _get_prime() -> None:
        prime_chunk_holder.append(await gen.__anext__())

    prime_task = asyncio.create_task(_get_prime())
    # Generator starts, registers consumer, suspends at asyncio.wait_for(queue.get()).
    await asyncio.sleep(0)

    # Emit the "prime" event — goes directly to the waiting getter (direct transfer).
    bus.emit(_make_event("srv-prime"))
    # Await the task: generator resumes from queue.get() with srv-prime,
    # yields the SSE chunk, then suspends at 'yield' (NOT at 'await queue.get()').
    await asyncio.wait_for(prime_task, timeout=1.0)
    assert "srv-prime" in prime_chunk_holder[0]

    # Step 2: emit 5 events WITHOUT advancing the generator.
    # The generator is suspended at 'yield' so there is NO pending getter —
    # all 5 events go directly into the bounded queue (no direct-transfer bypass).
    # Queue fill pattern (maxsize=3):
    #   srv-0 → queue=[0]        (size 1)
    #   srv-1 → queue=[0,1]      (size 2)
    #   srv-2 → queue=[0,1,2]    (size 3, full)
    #   srv-3 → drop 0, WARNING  → queue=[1,2,3]
    #   srv-4 → drop 1, WARNING  → queue=[2,3,4]
    with caplog.at_level(logging.WARNING):
        for i in range(5):
            bus.emit(_make_event(f"srv-{i}"))

    # Step 3: drain 3 chunks (the 3 newest: srv-2, srv-3, srv-4).
    chunks: list[str] = []
    for _ in range(3):
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        chunks.append(chunk)

    await gen.aclose()

    # Verify server names in order.
    servers_received = [
        json.loads(c.split("data: ", 1)[1].rstrip())["server"] for c in chunks
    ]
    assert servers_received == ["srv-2", "srv-3", "srv-4"], (
        f"Expected newest-3 events [srv-2, srv-3, srv-4], got: {servers_received}"
    )

    # Exactly 2 WARNINGs must have been logged (one per dropped event).
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) >= 2, (
        f"Expected >=2 drop-WARNING logs, got {len(warnings)}: "
        f"{[r.message for r in warnings]}"
    )


# ---------------------------------------------------------------------------
# Test 4 — unsubscribe on client disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsubscribe_on_client_disconnect() -> None:
    """CRITICAL: when stream() generator is closed (aclose()):
    - The finally block calls unsubscribe().
    - bus._consumers is empty after aclose().
    - Further emit() calls do NOT invoke the removed consumer.
    - No leaked reference to the queue.
    """
    from slm_mcp_hub.observability.event_stream import EventStreamBridge

    bus = LifecycleEventBus()
    bridge = EventStreamBridge(bus=bus, queue_maxsize=10)

    gen = bridge.stream()

    # Start the generator via task+sleep(0) to register the consumer
    # before emitting any events.
    first_chunk_holder: list[str] = []

    async def _get_first() -> None:
        first_chunk_holder.append(await gen.__anext__())

    first_task = asyncio.create_task(_get_first())
    await asyncio.sleep(0)  # generator starts, consumer registered

    # Consumer must be registered at this point.
    assert len(bus._consumers) == 1

    # Emit an event so the pending getter is satisfied and the task completes.
    bus.emit(_make_event("srv-first"))
    await asyncio.wait_for(first_task, timeout=1.0)
    assert "srv-first" in first_chunk_holder[0]

    # Consumer is still registered (generator is suspended at 'yield', not done).
    assert len(bus._consumers) == 1

    # Simulate client disconnect — close the generator.
    await gen.aclose()

    # Consumer must be unsubscribed after aclose().
    assert len(bus._consumers) == 0, (
        f"Expected 0 consumers after aclose(), got {len(bus._consumers)}"
    )

    # Further emits must not raise and must not invoke the removed consumer.
    call_count: list[int] = []
    bus.register_consumer(lambda _: call_count.append(1))
    bus.emit(_make_event("srv-after"))
    # Only the newly registered consumer should be called (1 call).
    assert len(call_count) == 1


# ---------------------------------------------------------------------------
# Test 5 — keepalive emitted after idle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keepalive_emitted_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no events arrive for >SSE_KEEPALIVE_INTERVAL_S seconds,
    stream() yields ': keepalive\\n\\n'. Patched to 0.05s for test speed."""
    import slm_mcp_hub.observability.event_stream as es_module

    monkeypatch.setattr(es_module, "SSE_KEEPALIVE_INTERVAL_S", 0.05)

    from slm_mcp_hub.observability.event_stream import EventStreamBridge

    bus = LifecycleEventBus()
    bridge = EventStreamBridge(bus=bus, queue_maxsize=10)

    gen = bridge.stream()
    # No events emitted — generator should produce a keepalive after 0.05s.
    chunk = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    await gen.aclose()

    assert chunk == ": keepalive\n\n", f"Expected keepalive, got: {chunk!r}"


# ---------------------------------------------------------------------------
# Test 6 — multiple clients isolated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_clients_isolated() -> None:
    """With 3 concurrent SSE clients and 5 emitted events:
    - Each client's queue receives all 5 events independently.
    - Draining client A does NOT drain client B or C's queue.
    - Unsubscribing A (via aclose) does NOT affect B or C.
    """
    from slm_mcp_hub.observability.event_stream import EventStreamBridge

    bus = LifecycleEventBus()
    bridge = EventStreamBridge(bus=bus, queue_maxsize=10)

    gen_a = bridge.stream()
    gen_b = bridge.stream()
    gen_c = bridge.stream()

    # Start all three generators so their consumers are registered.
    # Use tasks + sleep(0) pattern.
    started_a: list[str] = []
    started_b: list[str] = []
    started_c: list[str] = []

    async def _start_and_wait(gen: Any, holder: list[str]) -> None:
        chunk = await gen.__anext__()
        holder.append(chunk)

    task_a = asyncio.create_task(_start_and_wait(gen_a, started_a))
    task_b = asyncio.create_task(_start_and_wait(gen_b, started_b))
    task_c = asyncio.create_task(_start_and_wait(gen_c, started_c))

    # Yield to event loop so all three generators start and register their consumers.
    await asyncio.sleep(0)

    # All 3 consumers should be registered now.
    assert len(bus._consumers) == 3

    # Emit 5 events — each consumer should get all 5.
    for i in range(5):
        bus.emit(_make_event(f"srv-{i}"))

    # Drain task_a, task_b, task_c (they get srv-0 each — first event).
    await asyncio.gather(task_a, task_b, task_c)

    # Drain remaining 4 events from each generator.
    for _ in range(4):
        await asyncio.wait_for(gen_a.__anext__(), timeout=1.0)
        await asyncio.wait_for(gen_b.__anext__(), timeout=1.0)
        await asyncio.wait_for(gen_c.__anext__(), timeout=1.0)

    # Close A — should not affect B or C.
    await gen_a.aclose()
    assert len(bus._consumers) == 2  # B and C still subscribed

    # Close B and C.
    await gen_b.aclose()
    await gen_c.aclose()
    assert len(bus._consumers) == 0  # All unsubscribed


# ---------------------------------------------------------------------------
# Test 7 — SSE data contains NO secrets
# ---------------------------------------------------------------------------


def test_sse_data_contains_no_secrets() -> None:
    """_event_to_sse_data() output contains ONLY safe fields:
    server, from_state, to_state, reason, ts, failure_class, attempt.
    It must NOT contain 'env', 'headers', 'token', 'password', 'command'.
    """
    from slm_mcp_hub.observability.event_stream import _event_to_sse_data

    event = _make_event("srv-safe")
    chunk = _event_to_sse_data(event)

    # Structural checks.
    assert chunk.startswith("event: lifecycle\ndata: ")
    assert chunk.endswith("\n\n")

    # Parse the JSON payload.
    json_part = chunk.split("data: ", 1)[1].rstrip()
    data = json.loads(json_part)

    # Only these fields are allowed.
    allowed_keys = {"server", "from_state", "to_state", "reason", "ts", "failure_class", "attempt"}
    extra_keys = set(data.keys()) - allowed_keys
    assert not extra_keys, f"Unexpected keys in SSE data: {extra_keys}"

    # Forbidden field names must not appear anywhere in the raw chunk text.
    forbidden_patterns = ("env", "headers", "token", "password", "command")
    chunk_lower = chunk.lower()
    for pattern in forbidden_patterns:
        assert pattern not in chunk_lower, (
            f"Forbidden field {pattern!r} found in SSE chunk: {chunk!r}"
        )


# ---------------------------------------------------------------------------
# Test 8 — non-positive queue_maxsize is clamped to >=1 (bound never disabled)
# ---------------------------------------------------------------------------


def test_nonpositive_maxsize_is_clamped() -> None:
    """A config typo (event_queue_maxsize <= 0) MUST NOT silently disable the
    non-blocking bound: asyncio.Queue treats maxsize<=0 as UNBOUNDED, which would
    defeat drop-oldest. The bridge clamps to >=1."""
    from slm_mcp_hub.observability.event_stream import EventStreamBridge

    bus = LifecycleEventBus()
    assert EventStreamBridge(bus, queue_maxsize=0)._queue_maxsize == 1
    assert EventStreamBridge(bus, queue_maxsize=-7)._queue_maxsize == 1
    # A positive value passes through unchanged.
    assert EventStreamBridge(bus, queue_maxsize=5)._queue_maxsize == 5
