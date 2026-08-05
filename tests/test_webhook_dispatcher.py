"""W1-P4 — WebhookDispatcher tests.

TDD: written FIRST; expected to FAIL until W1-P4 implementation lands.

Covers:
- HubConfig.webhooks field (optional, default empty)
- WebhookDispatcher: URL validation, POST on event, retry, bounded retry, backoff
- Failure isolation: a failing URL must not block lifecycle, event loop, other URLs
- Payload safety: no secrets/tokens in the posted JSON
- Event ordering: events arrive at webhook endpoint in emission order
- Slow endpoint: timeout respected; never blocks the event loop
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import pytest

from slm_mcp_hub.core.config import HubConfig
from slm_mcp_hub.federation.connection import ConnectionState
from slm_mcp_hub.resilience.events import WebhookDispatcher, _event_to_dict
from slm_mcp_hub.resilience.lifecycle import LifecycleEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    server: str = "srv",
    from_state: ConnectionState = ConnectionState.DISCONNECTED,
    to_state: ConnectionState = ConnectionState.CONNECTING,
    reason: str = "test",
    failure_class: str | None = None,
    attempt: int | None = None,
) -> LifecycleEvent:
    return LifecycleEvent(
        server=server,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        ts=time.time(),
        failure_class=failure_class,
        attempt=attempt,
    )


# ---------------------------------------------------------------------------
# Mock HTTP infrastructure
# ---------------------------------------------------------------------------


class _MockResponse:
    """Minimal httpx.Response-alike for test injection."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://example.com/webhook"),
                response=httpx.Response(self.status_code),
            )


class _MockHTTPClient:
    """Injectable async HTTP client that records calls and returns scripted responses."""

    def __init__(self, responses: list[Any]) -> None:
        """
        responses: list where each item is either:
          - An int (status code) → returns _MockResponse(status_code)
          - An Exception subclass or instance → raises it
          - A callable → calls it with (url, json) → returns result
        """
        self._responses = list(responses)
        self._call_idx = 0
        self.posted: list[dict[str, Any]] = []

    async def post(
        self, url: str, *, json: Any = None, timeout: float | None = None
    ) -> _MockResponse:
        self.posted.append({"url": url, "json": json, "timeout": timeout})
        if self._call_idx < len(self._responses):
            item = self._responses[self._call_idx]
        else:
            item = 200  # default success after scripted responses exhausted
        self._call_idx += 1

        if isinstance(item, type) and issubclass(item, Exception):
            raise item("mock error")
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(url, json)
        return _MockResponse(int(item))

    async def __aenter__(self) -> "_MockHTTPClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_factory(responses: list[Any]) -> tuple[Any, _MockHTTPClient]:
    """Return (factory, shared_client) for injection into WebhookDispatcher."""
    client = _MockHTTPClient(responses)

    def _factory() -> _MockHTTPClient:
        return client

    return _factory, client


def _noop_sleep() -> Any:
    async def _s(_: float) -> None:
        await asyncio.sleep(0)

    return _s


# ---------------------------------------------------------------------------
# 1. HubConfig.webhooks field
# ---------------------------------------------------------------------------


class TestHubConfigWebhooksField:
    """HubConfig.webhooks field — optional, default empty, immutable."""

    def test_webhooks_defaults_empty(self) -> None:
        cfg = HubConfig()
        assert cfg.webhooks == ()

    def test_webhooks_accepts_tuple_of_urls(self) -> None:
        cfg = HubConfig(webhooks=("http://hook.example.com/events",))
        assert cfg.webhooks == ("http://hook.example.com/events",)

    def test_webhooks_is_immutable(self) -> None:
        cfg = HubConfig(webhooks=("http://hook.example.com/",))
        assert isinstance(cfg.webhooks, tuple)

    def test_hubconfig_without_webhooks_still_valid(self) -> None:
        """Default HubConfig (no webhooks) must construct without error."""
        cfg = HubConfig()
        assert hasattr(cfg, "webhooks")


# ---------------------------------------------------------------------------
# 2. WebhookDispatcher URL validation
# ---------------------------------------------------------------------------


class TestWebhookURLValidation:
    """Only http(s) URLs are accepted; others raise ValueError on construction."""

    def test_valid_http_url(self) -> None:
        WebhookDispatcher(["http://example.com/hook"])  # must not raise

    def test_valid_https_url(self) -> None:
        WebhookDispatcher(["https://example.com/hook"])  # must not raise

    def test_empty_url_list(self) -> None:
        WebhookDispatcher([])  # must not raise

    def test_invalid_ftp_url_raises(self) -> None:
        with pytest.raises(ValueError, match="http"):
            WebhookDispatcher(["ftp://example.com/hook"])

    def test_invalid_bare_host_raises(self) -> None:
        with pytest.raises(ValueError):
            WebhookDispatcher(["example.com/hook"])

    def test_invalid_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            WebhookDispatcher([""])


# ---------------------------------------------------------------------------
# 3. WebhookDispatcher — basic dispatch
# ---------------------------------------------------------------------------


class TestWebhookDispatcherBasic:
    """WebhookDispatcher POSTs LifecycleEvent JSON to configured URLs."""

    @pytest.mark.asyncio
    async def test_posts_on_event(self) -> None:
        factory, client = _make_factory([200])
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        event = _make_event()
        dispatcher.enqueue(event)
        # Allow drainer task to process
        await asyncio.sleep(0.05)
        await dispatcher.stop()
        assert len(client.posted) >= 1
        assert client.posted[0]["url"] == "http://example.com/hook"

    @pytest.mark.asyncio
    async def test_payload_contains_server_field(self) -> None:
        factory, client = _make_factory([200])
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        event = _make_event(server="my-backend")
        dispatcher.enqueue(event)
        await asyncio.sleep(0.05)
        await dispatcher.stop()
        assert client.posted[0]["json"]["server"] == "my-backend"

    @pytest.mark.asyncio
    async def test_payload_contains_state_fields(self) -> None:
        factory, client = _make_factory([200])
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        event = _make_event(
            from_state=ConnectionState.DISCONNECTED,
            to_state=ConnectionState.CONNECTING,
        )
        dispatcher.enqueue(event)
        await asyncio.sleep(0.05)
        await dispatcher.stop()
        payload = client.posted[0]["json"]
        assert payload["from_state"] == "disconnected"
        assert payload["to_state"] == "connecting"

    @pytest.mark.asyncio
    async def test_multiple_urls_all_receive_event(self) -> None:
        """Event is POSTed to every configured URL."""
        factory, client = _make_factory([200, 200])
        dispatcher = WebhookDispatcher(
            ["http://url1.example.com/hook", "http://url2.example.com/hook"],
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        dispatcher.enqueue(_make_event())
        await asyncio.sleep(0.05)
        await dispatcher.stop()
        posted_urls = {r["url"] for r in client.posted}
        assert "http://url1.example.com/hook" in posted_urls
        assert "http://url2.example.com/hook" in posted_urls


# ---------------------------------------------------------------------------
# 4. Payload safety — no secrets
# ---------------------------------------------------------------------------


class TestWebhookPayloadSafety:
    """Payload must only contain safe lifecycle fields — no secrets/tokens."""

    def test_event_to_dict_contains_only_safe_fields(self) -> None:
        event = _make_event(
            server="srv",
            from_state=ConnectionState.DISCONNECTED,
            to_state=ConnectionState.CONNECTING,
            reason="test reason",
            failure_class="TRANSIENT",
            attempt=2,
        )
        payload = _event_to_dict(event)
        allowed_keys = {"server", "from_state", "to_state", "reason", "ts", "failure_class", "attempt"}
        assert set(payload.keys()) == allowed_keys

    def test_event_to_dict_no_token_fields(self) -> None:
        event = _make_event()
        payload = _event_to_dict(event)
        dangerous_words = {"token", "secret", "password", "key", "credential", "auth_token"}
        for k in payload.keys():
            assert k.lower() not in dangerous_words, f"Dangerous field in payload: {k}"

    def test_payload_is_json_serializable(self) -> None:
        event = _make_event(failure_class="TRANSIENT", attempt=1)
        payload = _event_to_dict(event)
        # Must not raise
        serialized = json.dumps(payload)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# 5. WebhookDispatcher — retry behavior
# ---------------------------------------------------------------------------


class TestWebhookDispatcherRetry:
    """WebhookDispatcher retries up to max_retries on failure."""

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self) -> None:
        """Transient failure followed by success: should POST twice (1 fail + 1 success)."""
        factory, client = _make_factory([ConnectionError("down"), 200])
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            max_retries=3,
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        dispatcher.enqueue(_make_event())
        await asyncio.sleep(0.1)
        await dispatcher.stop()
        assert client._call_idx == 2  # 1 fail + 1 success

    @pytest.mark.asyncio
    async def test_bounded_retry_gives_up_after_max(self) -> None:
        """Permanently failing endpoint: stops after max_retries attempts."""
        always_fail = [ConnectionError("down")] * 10  # more than max
        factory, client = _make_factory(always_fail)
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            max_retries=3,
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        dispatcher.enqueue(_make_event())
        await asyncio.sleep(0.1)
        await dispatcher.stop()
        assert client._call_idx == 3  # exactly max_retries attempts, no more

    @pytest.mark.asyncio
    async def test_http_error_retried(self) -> None:
        """5xx HTTP error triggers retry."""
        factory, client = _make_factory([500, 200])
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            max_retries=3,
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        dispatcher.enqueue(_make_event())
        await asyncio.sleep(0.1)
        await dispatcher.stop()
        assert client._call_idx == 2


# ---------------------------------------------------------------------------
# 6. Failure isolation
# ---------------------------------------------------------------------------


class TestWebhookFailureIsolation:
    """A failing webhook must NEVER block lifecycle, event loop, or other webhooks."""

    @pytest.mark.asyncio
    async def test_failing_webhook_does_not_raise_to_enqueue(self) -> None:
        """enqueue() must never raise even if the dispatch eventually fails."""
        always_fail = [ConnectionError("down")] * 10
        factory, client = _make_factory(always_fail)
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            max_retries=2,
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        # enqueue must not raise regardless of webhook health
        for _ in range(3):
            dispatcher.enqueue(_make_event())  # must not raise
        await asyncio.sleep(0.1)
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_failing_url_does_not_block_succeeding_url(self) -> None:
        """Per-URL isolation: failure of URL1 does not prevent URL2 delivery."""
        call_log: list[str] = []

        class IsolationClient:
            async def post(
                self, url: str, *, json: Any = None, timeout: float | None = None
            ) -> _MockResponse:
                call_log.append(url)
                if "fail" in url:
                    raise ConnectionError("intentional fail")
                return _MockResponse(200)

            async def __aenter__(self) -> "IsolationClient":
                return self

            async def __aexit__(self, *args: Any) -> None:
                pass

        dispatcher = WebhookDispatcher(
            ["http://fail.example.com/hook", "http://ok.example.com/hook"],
            max_retries=2,
            http_client_factory=lambda: IsolationClient(),
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        dispatcher.enqueue(_make_event())
        await asyncio.sleep(0.1)
        await dispatcher.stop()
        # The succeeding URL must have been called despite the failing URL
        assert "http://ok.example.com/hook" in call_log

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        """Calling stop() twice must not raise."""
        dispatcher = WebhookDispatcher(["http://example.com/hook"])
        await dispatcher.start()
        await dispatcher.stop()
        await dispatcher.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """Calling start() twice must not raise or create duplicate tasks."""
        dispatcher = WebhookDispatcher(["http://example.com/hook"])
        await dispatcher.start()
        await dispatcher.start()  # idempotent
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_enqueue_drops_on_full_queue_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the internal queue is full, events are dropped with a warning log."""
        factory, client = _make_factory([200] * 20)
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
            queue_maxsize=1,  # tiny queue — second enqueue before drainer starts = QueueFull
        )
        # Do NOT start drainer yet — first event fills queue, second triggers QueueFull
        dispatcher.enqueue(_make_event(reason="fills-queue"))
        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.resilience.events"):
            dispatcher.enqueue(_make_event(reason="should-drop"))
        await dispatcher.start()
        await asyncio.sleep(0.05)
        await dispatcher.stop()
        # A warning about queue full must have been emitted
        assert any("queue full" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_enqueue_does_not_block_caller(self) -> None:
        """enqueue() returns immediately (synchronous, no await)."""
        factory, client = _make_factory([200])
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        before = time.monotonic()
        dispatcher.enqueue(_make_event())  # must return instantly
        elapsed = time.monotonic() - before
        await dispatcher.stop()
        # enqueue() is sync and non-blocking — should return in microseconds
        assert elapsed < 0.1, f"enqueue() blocked for {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# 7. Event ordering
# ---------------------------------------------------------------------------


class TestWebhookEventOrdering:
    """Events are dispatched in the order they are enqueued (FIFO queue)."""

    @pytest.mark.asyncio
    async def test_events_dispatched_in_order(self) -> None:
        received_reasons: list[str] = []

        class OrderingClient:
            async def post(
                self, url: str, *, json: Any = None, timeout: float | None = None
            ) -> _MockResponse:
                received_reasons.append(json["reason"])
                return _MockResponse(200)

            async def __aenter__(self) -> "OrderingClient":
                return self

            async def __aexit__(self, *args: Any) -> None:
                pass

        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            http_client_factory=lambda: OrderingClient(),
            sleep_fn=_noop_sleep(),
        )
        await dispatcher.start()
        for i in range(5):
            dispatcher.enqueue(_make_event(reason=f"evt-{i}"))
        await asyncio.sleep(0.1)
        await dispatcher.stop()
        assert received_reasons == [f"evt-{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# 8. WebhookDispatcher + LifecycleEventBus integration
# ---------------------------------------------------------------------------


class TestWebhookBusIntegration:
    """WebhookDispatcher as a LifecycleEventBus consumer."""

    @pytest.mark.asyncio
    async def test_bus_routes_event_to_dispatcher(self) -> None:
        """Registering dispatcher.enqueue as a bus consumer routes events to it."""
        from slm_mcp_hub.resilience.events import LifecycleEventBus

        factory, client = _make_factory([200])
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        bus = LifecycleEventBus()
        bus.register_consumer(dispatcher.enqueue)

        await dispatcher.start()
        bus.emit(_make_event(server="bus-srv"))
        await asyncio.sleep(0.05)
        await dispatcher.stop()

        assert len(client.posted) >= 1
        assert client.posted[0]["json"]["server"] == "bus-srv"

    @pytest.mark.asyncio
    async def test_dispatcher_failure_does_not_affect_bus_emit(self) -> None:
        """A webhook failure must not block or raise in the bus.emit() call path."""
        from slm_mcp_hub.resilience.events import LifecycleEventBus

        other_received: list[LifecycleEvent] = []
        always_fail = [ConnectionError("down")] * 10
        factory, client = _make_factory(always_fail)
        dispatcher = WebhookDispatcher(
            ["http://example.com/hook"],
            max_retries=1,
            http_client_factory=factory,
            sleep_fn=_noop_sleep(),
        )
        bus = LifecycleEventBus()
        bus.register_consumer(dispatcher.enqueue)
        bus.register_consumer(other_received.append)

        await dispatcher.start()
        bus.emit(_make_event())
        await asyncio.sleep(0.05)
        await dispatcher.stop()

        # Other consumer still received the event despite webhook failure
        assert len(other_received) == 1
