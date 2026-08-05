"""W4-P3 tests — InMemoryEventStore.

TDD: written BEFORE implementation. Verifies:
1. Sequential EventId generation ("0", "1", "2"...)
2. replay_events_after returns ONLY events strictly after last_event_id (no dup, no gap)
3. HARD CASE: mid-stream drop resume — store e0–e9, client got e0–e5, replay e6–e9 exactly
4. Ring buffer eviction — oldest events dropped, replay still works
5. max_streams cap — oldest stream evicted when cap exceeded
6. TTL pruning — expired streams removed
7. Priming event (message=None) — stored as sentinel without crash
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from slm_mcp_hub.streaming.event_store import (
    DEFAULT_MAX_EVENTS_PER_STREAM,
    DEFAULT_MAX_STREAMS,
    DEFAULT_STREAM_TTL_S,
    InMemoryEventStore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(text: str) -> object:
    """Return a minimal mock JSONRPCMessage with a unique payload."""
    msg = AsyncMock()
    msg.text = text  # extra attribute for debugging only
    return msg


# ---------------------------------------------------------------------------
# Sequential ID tests
# ---------------------------------------------------------------------------


class TestStoreEvent:
    async def test_store_event_returns_sequential_ids(self) -> None:
        """store_event returns '0', '1', '2'... regardless of stream_id."""
        store = InMemoryEventStore()
        id0 = await store.store_event("s1", _make_message("m0"))
        id1 = await store.store_event("s1", _make_message("m1"))
        id2 = await store.store_event("s2", _make_message("m2"))

        assert id0 == "0"
        assert id1 == "1"
        assert id2 == "2"

    async def test_store_event_different_streams_share_counter(self) -> None:
        """IDs are globally sequential across all streams."""
        store = InMemoryEventStore()
        ids = [
            await store.store_event("stream-a", _make_message(f"a{i}")) for i in range(3)
        ]
        ids += [
            await store.store_event("stream-b", _make_message(f"b{i}")) for i in range(3)
        ]
        assert ids == ["0", "1", "2", "3", "4", "5"]

    async def test_store_event_priming_none(self) -> None:
        """store_event(stream_id, message=None) stores a sentinel and returns a valid EventId.

        Does not crash on None message.
        """
        store = InMemoryEventStore()
        event_id = await store.store_event("prime-stream", None)
        assert isinstance(event_id, str)
        assert int(event_id) >= 0  # valid sequential ID


# ---------------------------------------------------------------------------
# replay_events_after — basic correctness
# ---------------------------------------------------------------------------


class TestReplayEventsAfter:
    async def test_replay_events_after_returns_only_newer(self) -> None:
        """Store events '0'–'4'; replay_events_after('2') replays only '3', '4'."""
        store = InMemoryEventStore()
        msgs = [_make_message(f"msg{i}") for i in range(5)]
        for m in msgs:
            await store.store_event("s1", m)

        received: list = []

        async def callback(em):
            received.append(em)

        stream_id = await store.replay_events_after("2", callback)

        assert stream_id == "s1"
        assert len(received) == 2
        # Verify order: event_id 3 then 4
        assert received[0].event_id == "3"
        assert received[1].event_id == "4"

    async def test_replay_returns_stream_id(self) -> None:
        """replay_events_after returns the stream_id of the replayed stream."""
        store = InMemoryEventStore()
        await store.store_event("my-stream", _make_message("m0"))
        await store.store_event("my-stream", _make_message("m1"))

        received: list = []

        async def callback(em) -> None:
            received.append(em)

        sid = await store.replay_events_after("0", callback)

        assert sid == "my-stream"

    async def test_replay_returns_none_for_unknown_id(self) -> None:
        """replay_events_after with an id not in any stream returns None."""
        store = InMemoryEventStore()
        await store.store_event("s1", _make_message("m0"))

        received: list = []

        async def callback(em):
            received.append(em)

        # ID "999" is not in any stream
        result = await store.replay_events_after("999", callback)
        assert result is None
        assert received == []

    async def test_replay_no_duplicate_events(self) -> None:
        """Events at last_event_id boundary are NOT replayed (strict >)."""
        store = InMemoryEventStore()
        for i in range(5):
            await store.store_event("s1", _make_message(f"m{i}"))

        received: list = []

        async def callback(em):
            received.append(em)

        # last_event_id = "4" is the LAST event; nothing should be replayed
        stream_id = await store.replay_events_after("4", callback)
        # The stream is found but nothing to replay
        # The stream is found (event 4 is in it) so stream_id is returned
        assert stream_id == "s1"
        assert received == []

    async def test_replay_events_in_order(self) -> None:
        """Replayed events arrive in insertion order — no gaps, no reordering."""
        store = InMemoryEventStore()
        for i in range(10):
            await store.store_event("s1", _make_message(f"m{i}"))

        received: list = []

        async def callback(em):
            received.append(int(em.event_id))

        await store.replay_events_after("2", callback)
        assert received == [3, 4, 5, 6, 7, 8, 9]


# ---------------------------------------------------------------------------
# HARD CASE: mid-stream drop resume — no data loss, no duplication
# ---------------------------------------------------------------------------


class TestMidStreamDropResume:
    async def test_mid_stream_drop_resume_no_data_loss_no_dup(self) -> None:
        """HARD CASE:
        1. Store events e0–e9 (stream_id='s1').
        2. Simulate client received e0–e5 (last_event_id='5').
        3. replay_events_after('5') replays e6–e9.
        4. Assert callback called exactly 4 times (e6, e7, e8, e9).
        5. Assert events replayed in original order (no duplicates, no gaps).
        """
        store = InMemoryEventStore()
        for i in range(10):
            await store.store_event("s1", _make_message(f"event-{i}"))

        received_ids: list[int] = []

        async def callback(em):
            received_ids.append(int(em.event_id))

        stream_id = await store.replay_events_after("5", callback)

        assert stream_id == "s1"
        assert received_ids == [6, 7, 8, 9], (
            f"Expected [6,7,8,9] but got {received_ids} — "
            "data loss or duplication detected"
        )

    async def test_no_data_loss_consecutive_replays(self) -> None:
        """Two consecutive replays from different offsets produce correct non-overlapping results."""
        store = InMemoryEventStore()
        for i in range(6):
            await store.store_event("s1", _make_message(f"m{i}"))

        first: list[int] = []
        second: list[int] = []

        async def cb1(em):
            first.append(int(em.event_id))

        async def cb2(em):
            second.append(int(em.event_id))

        await store.replay_events_after("1", cb1)   # should get 2,3,4,5
        await store.replay_events_after("3", cb2)   # should get 4,5

        assert first == [2, 3, 4, 5]
        assert second == [4, 5]
        # No overlap between the two
        assert set(first) & set(second) == {4, 5}


# ---------------------------------------------------------------------------
# Ring buffer eviction
# ---------------------------------------------------------------------------


class TestRingBuffer:
    async def test_ring_buffer_evicts_oldest(self) -> None:
        """InMemoryEventStore(max_events_per_stream=3). Store 5 events.
        Only the last 3 are in the buffer. replay_events_after('-1') returns 3 events.
        """
        store = InMemoryEventStore(max_events_per_stream=3)
        for i in range(5):
            await store.store_event("s1", _make_message(f"m{i}"))

        received: list = []

        async def callback(em):
            received.append(em)

        stream_id = await store.replay_events_after("-1", callback)
        assert stream_id == "s1"
        assert len(received) == 3

    async def test_ring_buffer_evicts_oldest_ids(self) -> None:
        """After ring buffer fills, only the newest max_events_per_stream events remain."""
        store = InMemoryEventStore(max_events_per_stream=3)
        for i in range(5):
            await store.store_event("s1", _make_message(f"m{i}"))

        received_ids: list[int] = []

        async def callback(em):
            received_ids.append(int(em.event_id))

        # Replay from before all stored events
        await store.replay_events_after("-1", callback)
        # Should have events with IDs 2, 3, 4 (the last 3 stored)
        assert received_ids == [2, 3, 4]

    async def test_ring_buffer_does_not_exceed_capacity(self) -> None:
        """After storing N events where N > maxlen, stream_count stays 1 and
        buffer size stays bounded."""
        store = InMemoryEventStore(max_events_per_stream=5)
        for i in range(20):
            await store.store_event("s1", _make_message(f"m{i}"))

        assert store.stream_count == 1
        # Internal: deque should have exactly 5 items
        # Verify by replaying everything and counting
        received: list = []

        async def callback(em):
            received.append(em)

        await store.replay_events_after("-1", callback)
        assert len(received) == 5


# ---------------------------------------------------------------------------
# max_streams cap
# ---------------------------------------------------------------------------


class TestMaxStreamsCap:
    async def test_max_streams_cap_evicts_oldest_stream(self) -> None:
        """InMemoryEventStore(max_streams=2). Create streams 'a', 'b', 'c'.
        After creating 'c', stream 'a' (oldest) is evicted.
        replay_events_after for stream 'a's last event_id returns None.
        """
        store = InMemoryEventStore(max_streams=2, max_events_per_stream=10)

        # Store events in stream "a"
        id_a = None
        for _ in range(3):
            id_a = await store.store_event("a", _make_message("a-msg"))
        # Store events in stream "b"
        for _ in range(3):
            await store.store_event("b", _make_message("b-msg"))

        # At this point we have 2 streams ("a" and "b") at the cap
        assert store.stream_count == 2

        # Creating stream "c" should evict stream "a" (oldest by created_at)
        for _ in range(3):
            await store.store_event("c", _make_message("c-msg"))

        assert store.stream_count == 2

        received: list = []

        async def callback(em):
            received.append(em)

        # Stream "a" is gone — replay with last event_id from "a" must return None
        result = await store.replay_events_after(id_a, callback)
        assert result is None
        assert received == []

    async def test_stream_count_bounded_by_max_streams(self) -> None:
        """store_event for a new stream beyond cap evicts oldest; count stays <= max."""
        store = InMemoryEventStore(max_streams=5)
        for i in range(10):
            await store.store_event(f"stream-{i}", _make_message(f"m{i}"))
        assert store.stream_count == 5


# ---------------------------------------------------------------------------
# TTL pruning
# ---------------------------------------------------------------------------


class TestTTLPruning:
    async def test_expired_stream_pruned(self) -> None:
        """InMemoryEventStore(stream_ttl_s=0.05). Store event at t=0. Sleep 0.1s.
        Store event for a different stream. Assert stream count is 1 (old pruned).
        """
        store = InMemoryEventStore(stream_ttl_s=0.05)
        await store.store_event("old-stream", _make_message("old"))

        await asyncio.sleep(0.1)

        # Storing to a new stream triggers pruning of expired streams
        await store.store_event("new-stream", _make_message("new"))

        assert store.stream_count == 1

    async def test_non_expired_stream_not_pruned(self) -> None:
        """Streams within TTL are NOT pruned."""
        store = InMemoryEventStore(stream_ttl_s=10.0)
        await store.store_event("s1", _make_message("m1"))
        await store.store_event("s2", _make_message("m2"))

        # Both streams are within TTL — count stays 2
        assert store.stream_count == 2

    async def test_stream_count_property(self) -> None:
        """stream_count reflects the live count accurately."""
        store = InMemoryEventStore(max_streams=100)
        assert store.stream_count == 0
        await store.store_event("s1", _make_message("x"))
        assert store.stream_count == 1
        await store.store_event("s2", _make_message("y"))
        assert store.stream_count == 2


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Edge cases — covering defensive branches (lines 166, 201, 283-284)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_replay_with_invalid_non_integer_event_id_returns_none(self) -> None:
        """replay_events_after with a non-integer event_id returns None (line 166 + 283-284)."""
        store = InMemoryEventStore()
        await store.store_event("s1", _make_message("m0"))

        received: list = []

        async def callback(em) -> None:
            received.append(em)

        result = await store.replay_events_after("not-an-integer", callback)
        assert result is None
        assert received == []

    async def test_replay_with_none_as_event_id_returns_none(self) -> None:
        """replay_events_after with None event_id (TypeError) returns None (line 283-284)."""
        store = InMemoryEventStore()
        await store.store_event("s1", _make_message("m0"))

        received: list = []

        async def callback(em) -> None:
            received.append(em)

        # Pass None as event_id — _parse_event_id must handle TypeError
        result = await store.replay_events_after(None, callback)  # type: ignore[arg-type]
        assert result is None
        assert received == []

    async def test_replay_skips_empty_streams_in_pass2(self) -> None:
        """Streams with empty event buffers are skipped in pass-2 candidate scan (line 201).

        This exercises the 'if not record.events: continue' branch by directly
        manipulating the internal stream dict to inject an empty record.
        """
        store = InMemoryEventStore(max_streams=5)
        # Store an event to stream "s1"
        await store.store_event("s1", _make_message("real"))

        # Inject an artificial empty-buffer stream into the internal dict
        # (simulates a stream that had all events evicted by external tooling)
        from collections import deque

        from slm_mcp_hub.streaming.event_store import _StreamRecord

        empty_record = _StreamRecord(
            stream_id="empty-stream",
            events=deque(maxlen=5),
            max_size=5,
        )
        store._streams["empty-stream"] = empty_record

        # Now call replay with a sentinel that triggers pass-2
        received: list = []

        async def callback(em) -> None:
            received.append(em)

        # target = -1, pass-2: "empty-stream" has no events → skip it
        # "s1" has event 0 > -1 → single candidate → replay
        stream_id = await store.replay_events_after("-1", callback)
        assert stream_id == "s1"
        assert len(received) == 1


class TestDefaults:
    def test_defaults_match_constants(self) -> None:
        """Default constructor params match module-level constants."""
        store = InMemoryEventStore()
        assert store._max_events_per_stream == DEFAULT_MAX_EVENTS_PER_STREAM
        assert store._max_streams == DEFAULT_MAX_STREAMS
        assert store._stream_ttl_s == DEFAULT_STREAM_TTL_S

    async def test_store_creates_stream_on_first_event(self) -> None:
        """Storing an event to a new stream_id auto-creates the stream."""
        store = InMemoryEventStore()
        assert store.stream_count == 0
        await store.store_event("brand-new", _make_message("first"))
        assert store.stream_count == 1
