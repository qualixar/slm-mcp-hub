"""Change notifier — transport-agnostic pub-sub for capability registry changes.

When ConnectionManager._sync_registry() detects a change (via the `changed`
flag CapabilityRegistry.sync() already returns), it fires this notifier.
Subscribers (one per connected MCP client session) receive a callback and
emit `notifications/tools/list_changed` over their respective transport
(HTTP SSE, stdio stdout, etc.).

The notifier itself is transport-blind — it only knows about subscribers
that opt in via subscribe(). The transport layer (Phase 5 stdio, Phase 3
HTTP SSE) registers/unregisters its own subscribers as clients connect
and disconnect.

Debounce window (per Thor's prior-art research, Traefik pattern): 2s
default. When 46+ MCPs reconnect at startup, the registry can change
many times in rapid succession; we coalesce into a single notification.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Subscriber callback: receives a dict {"method": "...", "params": {...}}.
# Returning a coroutine is supported; sync callbacks are also fine.
Subscriber = Callable[[dict[str, Any]], Awaitable[None] | None]


class ChangeNotifier:
    """In-process pub-sub for MCP lifecycle change notifications.

    Use one instance per HubRuntime. Subscribers add themselves via
    subscribe(); they receive coalesced notifications. Errors in one
    subscriber are isolated — they don't break delivery to others.
    """

    def __init__(self, debounce_seconds: float = 2.0) -> None:
        self._subscribers: dict[str, Subscriber] = {}
        self._debounce_seconds = debounce_seconds
        self._pending_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, subscriber_id: str, callback: Subscriber) -> None:
        """Register a subscriber. subscriber_id should be unique per client
        session (e.g., the MCP session UUID). Replaces existing subscriber
        with the same id."""
        self._subscribers[subscriber_id] = callback
        logger.debug(
            "Subscriber added: %s (%d total)", subscriber_id, len(self._subscribers),
        )

    def unsubscribe(self, subscriber_id: str) -> None:
        """Unregister a subscriber. Safe to call with an unknown id."""
        if self._subscribers.pop(subscriber_id, None) is not None:
            logger.debug(
                "Subscriber removed: %s (%d remaining)",
                subscriber_id, len(self._subscribers),
            )

    async def notify_tools_changed(self) -> None:
        """Schedule a tools/list_changed notification.

        Calls within the debounce window are coalesced into one notification.
        Returns immediately — actual fan-out happens in a background task.
        """
        async with self._lock:
            if self._pending_task and not self._pending_task.done():
                # Already scheduled — coalesce
                return
            self._pending_task = asyncio.create_task(
                self._debounce_then_broadcast(
                    {"method": "notifications/tools/list_changed"}
                )
            )

    async def _debounce_then_broadcast(self, notification: dict[str, Any]) -> None:
        """Wait debounce window, then broadcast to all subscribers."""
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return
        await self._broadcast(notification)

    async def _broadcast(self, notification: dict[str, Any]) -> None:
        """Send to every subscriber, isolating errors."""
        subscribers = list(self._subscribers.items())
        logger.info(
            "Broadcasting %s to %d subscribers",
            notification.get("method"), len(subscribers),
        )
        for sub_id, callback in subscribers:
            try:
                result = callback(notification)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning(
                    "Subscriber %s callback raised %s — keeping subscription, isolating error",
                    sub_id, exc,
                )

    async def shutdown(self) -> None:
        """Cancel any pending debounce + clear subscribers. Called by HubRuntime
        on hub shutdown."""
        async with self._lock:
            if self._pending_task and not self._pending_task.done():
                self._pending_task.cancel()
                try:
                    await self._pending_task
                except asyncio.CancelledError:
                    pass
            self._pending_task = None
            self._subscribers.clear()
