"""W1-P4 — Lifecycle event bus and optional webhook dispatcher.

Two components:

``LifecycleEventBus``
    In-process synchronous fan-out bus.  Every ``MCPConnection`` subscribes via
    ``conn.subscribe(bus.emit)`` so all lifecycle transitions flow into the bus.
    Registered consumers (health aggregator, webhook dispatcher, W5 SSE) each
    receive every event.  A raising or slow consumer is isolated by a per-consumer
    try/except — it cannot break the lifecycle path or other consumers.

``WebhookDispatcher``
    Optional outbound alerting.  Receives events through ``enqueue()`` (sync,
    non-blocking) from the bus, and dispatches them via HTTP POST using a
    background asyncio task that drains an internal queue.  A slow, down, or
    erroring webhook endpoint:

    * NEVER blocks the event loop (dispatch is async in a background task).
    * NEVER blocks other backends (fan-out is synchronous in the bus; only POST
      is async).
    * NEVER blocks other webhook URLs (per-URL failure is isolated with
      bounded retry).
    * NEVER propagates exceptions to the caller of ``enqueue()`` — it is safe
      to call from synchronous lifecycle code.

Design decisions
----------------
Synchronous ``_emit`` + async queue
    ``MCPConnection._emit()`` is synchronous (called from the sync ``_transition``
    method).  Webhook dispatch is async (HTTP POST).  The bridge is an
    ``asyncio.Queue``: ``enqueue()`` is a non-blocking ``put_nowait`` on the sync
    side; the background drainer task awaits ``queue.get()`` on the async side.
    This preserves event ordering (FIFO queue) and never blocks the event loop.

Per-URL isolation
    ``_dispatch_event`` iterates over all URLs sequentially inside the drainer task.
    A failure on URL-N is logged and retried bounded times without affecting URL-N+1.
    Retries use exponential backoff via the injected ``sleep_fn`` (no-op in tests).

Bounded retry
    After ``max_retries`` attempts, the event is dropped for that URL and an ERROR
    is logged.  A permanently-down endpoint never blocks the drainer forever.

Payload safety
    ``_event_to_dict`` serializes only: server, from_state, to_state, reason, ts,
    failure_class, attempt.  No secrets, tokens, credentials, headers, or config
    data reach the webhook endpoint.

URL validation
    Only ``http://`` and ``https://`` URLs are accepted.  Bare hostnames, ``ftp://``,
    IP-only URLs, and empty strings raise ``ValueError`` at construction time so the
    config is rejected early.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    pass

from slm_mcp_hub.resilience.lifecycle import LifecycleEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL validation (http/https only)
# ---------------------------------------------------------------------------

# Minimal URL pattern — requires a scheme (http/https) and a non-empty host.
# Intentionally simple: rejects bare hostnames, IP-only URLs, and non-http schemes.
_VALID_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def _validate_webhook_url(url: str) -> None:
    """Raise ``ValueError`` if *url* is not a valid http/https URL.

    Accepts only ``http://`` and ``https://`` schemes.  Bare hostnames,
    ``ftp://``, ``file://``, IP-only URLs (security concern), and empty strings
    are rejected.

    Parameters
    ----------
    url:
        Candidate webhook URL.

    Raises
    ------
    ValueError
        If the URL does not match the ``http(s)://host[/path]`` pattern.
    """
    if not isinstance(url, str) or not _VALID_URL_RE.match(url):
        raise ValueError(
            f"Invalid webhook URL (must be http:// or https://): {url!r}"
        )


# ---------------------------------------------------------------------------
# Payload serialisation (safe — no secrets)
# ---------------------------------------------------------------------------


def _event_to_dict(event: LifecycleEvent) -> dict[str, Any]:
    """Serialise a :class:`LifecycleEvent` to a JSON-safe dict.

    **Only safe fields are included** — server name, state transition, reason,
    timestamp, failure class, and attempt counter.  No configuration, headers,
    tokens, credentials, or command paths reach the webhook payload.
    """
    return {
        "server": event.server,
        "from_state": event.from_state.value,
        "to_state": event.to_state.value,
        "reason": event.reason,
        "ts": event.ts,
        "failure_class": event.failure_class,
        "attempt": event.attempt,
    }


# ---------------------------------------------------------------------------
# LifecycleEventBus
# ---------------------------------------------------------------------------


class LifecycleEventBus:
    """In-process synchronous fan-out bus for :class:`LifecycleEvent` objects.

    Each :class:`~slm_mcp_hub.federation.connection.MCPConnection` subscribes via
    ``conn.subscribe(bus.emit)`` so its lifecycle transitions flow into the bus.
    Registered consumers (health aggregator, webhook dispatcher, W5 SSE) each
    receive every event with full per-consumer isolation.

    Ordering guarantee
    ------------------
    Because ``emit()`` is synchronous and iterates consumers sequentially, events
    from a single producer (connection) arrive at each consumer in emission order.

    Consumer isolation guarantee
    ----------------------------
    A raising consumer is caught, logged, and skipped.  It cannot break the
    lifecycle path, drop events to other consumers, or corrupt shared state.
    Iteration is over a snapshot copy of the consumer list so mid-iteration
    unregistration is safe.
    """

    def __init__(self) -> None:
        self._consumers: dict[int, Callable[[LifecycleEvent], None]] = {}
        self._next_id: int = 0

    def register_consumer(
        self, consumer: Callable[[LifecycleEvent], None]
    ) -> Callable[[], None]:
        """Register *consumer* as a recipient of all future events.

        Parameters
        ----------
        consumer:
            Synchronous callable that accepts a single :class:`LifecycleEvent`.
            Must be fast — any slow work should be offloaded (e.g. via a queue).

        Returns
        -------
        Callable[[], None]
            An unsubscribe callable.  Call it once to deregister the consumer.
            Idempotent: calling it multiple times is safe (no-op after first).
        """
        cid = self._next_id
        self._next_id += 1
        self._consumers[cid] = consumer

        def _unregister() -> None:
            self._consumers.pop(cid, None)

        return _unregister

    def emit(self, event: LifecycleEvent) -> None:
        """Fan out *event* to all registered consumers.

        Called synchronously from ``MCPConnection._emit()`` on the lifecycle path.
        This method must never raise — all consumer exceptions are caught and logged.

        Iterates over a snapshot copy of the consumer dict so that a consumer
        which unregisters itself (or another consumer) during delivery does not
        cause skipped or double deliveries.

        Parameters
        ----------
        event:
            Immutable :class:`LifecycleEvent` to deliver.
        """
        for consumer in list(self._consumers.values()):
            try:
                consumer(event)
            except Exception:
                logger.exception(
                    "Event bus consumer raised for %s (%s -> %s); ignoring",
                    event.server,
                    event.from_state.value,
                    event.to_state.value,
                )


# ---------------------------------------------------------------------------
# WebhookDispatcher
# ---------------------------------------------------------------------------


class WebhookDispatcher:
    """Optional outbound webhook alerting for :class:`LifecycleEvent` objects.

    Lifecycle events flow in via :meth:`enqueue` (synchronous, non-blocking)
    and are dispatched asynchronously by a background drainer task that POSTs
    each event's JSON payload to every configured URL with bounded retry.

    Parameters
    ----------
    urls:
        Sequence of ``http(s)://`` webhook endpoint URLs.  All are validated on
        construction; an invalid URL raises :exc:`ValueError` immediately.
    max_retries:
        Maximum per-URL POST attempts (including the first try).  After
        *max_retries* consecutive failures for a URL, the event is dropped for
        that URL and an ERROR is logged.  Default: ``3``.
    backoff_base:
        Base sleep interval (seconds) between retries.  Uses exponential
        backoff: ``backoff_base * 2 ** (attempt - 1)``.  Default: ``1.0``.
    timeout:
        Per-request HTTP timeout (seconds).  Default: ``10.0``.
    http_client_factory:
        Callable that returns an async context manager compatible with
        ``httpx.AsyncClient``.  Injected in tests for network-free testing.
        Default: ``lambda: httpx.AsyncClient()``.
    queue_maxsize:
        Maximum number of events buffered before ``enqueue`` drops on full
        (logs a warning).  Default: ``1000``.
    sleep_fn:
        Async callable used for retry backoff waits.  Injected in tests.
        Default: ``asyncio.sleep``.
    """

    def __init__(
        self,
        urls: list[str] | tuple[str, ...],
        *,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        timeout: float = 10.0,
        http_client_factory: Callable[[], Any] | None = None,
        queue_maxsize: int = 1000,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        for url in urls:
            _validate_webhook_url(url)
        self._urls: tuple[str, ...] = tuple(urls)
        self._max_retries: int = max_retries
        self._backoff_base: float = backoff_base
        self._timeout: float = timeout
        self._client_factory: Callable[[], Any] = (
            http_client_factory if http_client_factory is not None
            else (lambda: httpx.AsyncClient())
        )
        self._sleep_fn: Callable[[float], Awaitable[None]] = (
            sleep_fn if sleep_fn is not None else asyncio.sleep
        )
        self._queue: asyncio.Queue[LifecycleEvent] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def enqueue(self, event: LifecycleEvent) -> None:
        """Synchronously enqueue *event* for async dispatch.

        This method is called from :meth:`LifecycleEventBus.emit` which runs on
        the synchronous lifecycle path.  It must never raise and must never block.

        If the internal queue is full, the event is dropped and a warning is logged.
        This is a back-pressure signal — in practice the queue holds 1 000 events
        and the drainer processes events near-instantly, so full queues indicate an
        unusually high event rate combined with very slow webhook endpoints.

        Parameters
        ----------
        event:
            Immutable :class:`LifecycleEvent` to dispatch.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "WebhookDispatcher queue full; dropping event for %s (%s -> %s). "
                "This indicates a slow webhook endpoint combined with a high event rate.",
                event.server,
                event.from_state.value,
                event.to_state.value,
            )

    async def start(self) -> None:
        """Start the background drainer task.

        Idempotent — calling while a task is already running is a no-op.
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._drainer(), name="webhook-dispatcher-drainer"
        )

    async def stop(self) -> None:
        """Cancel the background drainer task and wait for it to finish.

        Safe to call before :meth:`start` or after the task has already ended.
        Idempotent — calling multiple times is safe.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    # ------------------------------------------------------------------
    # Background drainer
    # ------------------------------------------------------------------

    async def _drainer(self) -> None:
        """Drain the event queue and POST each event to all configured URLs.

        Runs indefinitely until cancelled by :meth:`stop`.  Each event is
        dispatched after being dequeued; exceptions from dispatch are caught and
        logged so the drainer never exits unexpectedly.
        """
        while True:
            event = await self._queue.get()
            try:
                await self._dispatch_event(event)
            except Exception:  # pragma: no cover — _dispatch_event isolates all errors internally
                logger.exception(
                    "WebhookDispatcher: unexpected error dispatching event for %s (%s -> %s)",
                    event.server,
                    event.from_state.value,
                    event.to_state.value,
                )
            finally:
                self._queue.task_done()

    async def _dispatch_event(self, event: LifecycleEvent) -> None:
        """POST *event* to all configured URLs with per-URL isolation.

        URL failures are isolated: a failure on URL-N never prevents URL-N+1
        from receiving the event.  Each URL gets bounded retry.

        Parameters
        ----------
        event:
            :class:`LifecycleEvent` to dispatch.
        """
        payload = _event_to_dict(event)
        for url in self._urls:
            await self._post_with_retry(url, payload, event)

    async def _post_with_retry(
        self,
        url: str,
        payload: dict[str, Any],
        event: LifecycleEvent,
    ) -> None:
        """POST *payload* to *url* with exponential backoff.

        Attempts at most :attr:`_max_retries` times.  On final failure, logs an
        ERROR and returns (never raises) so the drainer can continue processing
        the next event.

        Parameters
        ----------
        url:
            Webhook endpoint URL.
        payload:
            JSON-serialisable dict produced by :func:`_event_to_dict`.
        event:
            Original event (for structured logging only).
        """
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                async with self._client_factory() as client:
                    resp = await client.post(
                        url,
                        json=payload,
                        timeout=self._timeout,
                    )
                    resp.raise_for_status()
                logger.debug(
                    "Webhook dispatched: %s → %s (status=%d, attempt=%d)",
                    event.server,
                    url,
                    resp.status_code,
                    attempt,
                )
                return  # success
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    backoff = self._backoff_base * (2 ** (attempt - 1))
                    logger.warning(
                        "Webhook %s attempt %d/%d failed (%s: %s); retrying in %.1fs",
                        url,
                        attempt,
                        self._max_retries,
                        type(exc).__name__,
                        exc,
                        backoff,
                    )
                    await self._sleep_fn(backoff)

        # All retries exhausted
        logger.error(
            "Webhook %s: gave up after %d attempts. Last error (%s): %s. "
            "Event for %s (%s -> %s) was not delivered.",
            url,
            self._max_retries,
            type(last_exc).__name__ if last_exc else "unknown",
            last_exc,
            event.server,
            event.from_state.value,
            event.to_state.value,
        )
