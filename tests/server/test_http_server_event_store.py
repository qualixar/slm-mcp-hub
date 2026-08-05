"""W4-P3 tests — http_server event store wiring.

TDD: written BEFORE implementation. Verifies:
1. _build_sdk_asgi wires InMemoryEventStore when hub_config.event_store_enabled=True.
2. _build_sdk_asgi passes event_store=None when hub_config.event_store_enabled=False.
3. _build_sdk_asgi passes event_store=None when hub_config=None (backward compat).
4. Integration replay: InMemoryEventStore replays missed events with no gap/dup.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from slm_mcp_hub.core.config import HubConfig  # noqa: E402
from slm_mcp_hub.server.http_server import _build_sdk_asgi
from slm_mcp_hub.streaming.event_store import InMemoryEventStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sdk_server():
    """Create a minimal mock SDK Server."""
    mock = MagicMock()
    # StreamableHTTPSessionManager calls app.lifespan — mock it
    mock.lifespan = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# _build_sdk_asgi event store wiring tests
# ---------------------------------------------------------------------------


class TestBuildSdkAsgiEventStoreWiring:
    def test_build_sdk_asgi_wires_event_store_when_enabled(self) -> None:
        """_build_sdk_asgi(sdk_server, hub_config=HubConfig(event_store_enabled=True))
        returns a StreamableHTTPSessionManager with event_store set to an InMemoryEventStore.
        """
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(event_store_enabled=True)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.event_store is not None
        assert isinstance(session_manager.event_store, InMemoryEventStore)

    def test_build_sdk_asgi_no_event_store_when_disabled(self) -> None:
        """_build_sdk_asgi(hub_config=HubConfig(event_store_enabled=False)) returns
        StreamableHTTPSessionManager with event_store=None.
        """
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(event_store_enabled=False)

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert session_manager.event_store is None

    def test_build_sdk_asgi_no_event_store_when_no_hub_config(self) -> None:
        """_build_sdk_asgi without hub_config preserves backward compat: event_store=None."""
        sdk_server = _make_sdk_server()

        _, session_manager = _build_sdk_asgi(sdk_server)

        assert session_manager.event_store is None

    def test_build_sdk_asgi_event_store_respects_max_events_per_stream(self) -> None:
        """InMemoryEventStore is constructed with max_events_per_stream from hub_config."""
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(
            event_store_enabled=True,
            event_store_max_events_per_stream=42,
        )

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert isinstance(session_manager.event_store, InMemoryEventStore)
        assert session_manager.event_store._max_events_per_stream == 42

    def test_build_sdk_asgi_event_store_respects_max_streams(self) -> None:
        """InMemoryEventStore is constructed with max_streams from hub_config."""
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(
            event_store_enabled=True,
            event_store_max_streams=77,
        )

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert isinstance(session_manager.event_store, InMemoryEventStore)
        assert session_manager.event_store._max_streams == 77

    def test_build_sdk_asgi_event_store_respects_ttl(self) -> None:
        """InMemoryEventStore is constructed with stream_ttl_s from hub_config."""
        sdk_server = _make_sdk_server()
        hub_config = HubConfig(
            event_store_enabled=True,
            event_store_stream_ttl_s=999.0,
        )

        _, session_manager = _build_sdk_asgi(sdk_server, hub_config=hub_config)

        assert isinstance(session_manager.event_store, InMemoryEventStore)
        assert session_manager.event_store._stream_ttl_s == 999.0


# ---------------------------------------------------------------------------
# Integration replay test — proves no gap/dup via InMemoryEventStore directly
# ---------------------------------------------------------------------------


class TestEventStoreIntegrationReplay:
    async def test_event_store_integration_replay(self) -> None:
        """HARD CASE: Full integration — InMemoryEventStore stores events 0–9.
        Client received events 0–3 (Last-Event-ID=3). Reconnect replays 4–9
        without gap or duplication.
        """
        store = InMemoryEventStore(max_events_per_stream=500)

        # Store 10 events for one stream
        from unittest.mock import AsyncMock
        msgs = [AsyncMock() for _ in range(10)]
        for _i, m in enumerate(msgs):
            await store.store_event("session-stream", m)

        # Simulate: client received events 0–3, last_event_id = "3"
        received_ids: list[int] = []

        async def send_callback(event_message):
            received_ids.append(int(event_message.event_id))

        stream_id = await store.replay_events_after("3", send_callback)

        assert stream_id == "session-stream"
        # Events 4–9 replayed: exactly 6, no gap, no dup
        assert received_ids == [4, 5, 6, 7, 8, 9], (
            f"Expected [4,5,6,7,8,9] but got {received_ids} — gap or dup detected"
        )

    async def test_event_store_integration_replay_all(self) -> None:
        """Client reconnects with last_event_id='-1' (never received anything).
        All stored events are replayed.
        """
        store = InMemoryEventStore(max_events_per_stream=500)

        from unittest.mock import AsyncMock
        for _ in range(5):
            await store.store_event("stream-1", AsyncMock())

        received: list = []

        async def callback(em):
            received.append(em)

        await store.replay_events_after("-1", callback)
        assert len(received) == 5


# ---------------------------------------------------------------------------
# HubConfig event_store fields — round-trip and defaults
# ---------------------------------------------------------------------------


class TestHubConfigEventStoreFields:
    def test_hub_config_event_store_defaults(self) -> None:
        """HubConfig has correct defaults for event_store_* fields."""
        cfg = HubConfig()
        assert cfg.event_store_enabled is True
        assert cfg.event_store_max_events_per_stream == 500
        assert cfg.event_store_max_streams == 200
        assert cfg.event_store_stream_ttl_s == 7200.0

    def test_hub_config_event_store_disabled_by_flag(self) -> None:
        """event_store_enabled=False is accepted."""
        cfg = HubConfig(event_store_enabled=False)
        assert cfg.event_store_enabled is False

    def test_hub_config_event_store_custom_values(self) -> None:
        """Custom values are stored correctly."""
        cfg = HubConfig(
            event_store_enabled=True,
            event_store_max_events_per_stream=100,
            event_store_max_streams=50,
            event_store_stream_ttl_s=3600.0,
        )
        assert cfg.event_store_max_events_per_stream == 100
        assert cfg.event_store_max_streams == 50
        assert cfg.event_store_stream_ttl_s == 3600.0

    def test_hub_config_round_trip(self, tmp_path) -> None:
        """Event store config fields survive save_config → load_config round trip."""
        from slm_mcp_hub.core.config import load_config, save_config

        config_path = tmp_path / "config.json"
        cfg = HubConfig(
            event_store_enabled=False,
            event_store_max_events_per_stream=250,
            event_store_max_streams=100,
            event_store_stream_ttl_s=1800.0,
        )
        save_config(cfg, config_path=config_path)
        loaded = load_config(config_path=config_path)

        assert loaded.event_store_enabled is False
        assert loaded.event_store_max_events_per_stream == 250
        assert loaded.event_store_max_streams == 100
        assert loaded.event_store_stream_ttl_s == 1800.0
