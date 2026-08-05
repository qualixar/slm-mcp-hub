"""Client-leg resumption context for W4-P3.

``ResumableCallContext`` captures the ``resumption_token`` received from a backend
so that a mid-stream ``ResumptionError`` can restart the call with the token rather
than from scratch, avoiding data loss on the client ↔ hub ↔ backend path.

``TokenPersistence`` is a pluggable protocol for persisting tokens across process
restarts. The default implementation (``_InMemoryTokenPersistence``) is in-process
only and does not survive a hub restart — sufficient for the W4 in-process store.

Lifecycle:

    ctx = ResumableCallContext(call_id="some-uuid")
    # Pass ctx.on_token_update as on_resumption_token_update in ClientMessageMetadata
    # Pass ctx.get_token() as resumption_token on retry
    result = await outbound.call_tool_streaming(
        ...,
        resumption_token=await ctx.get_token(),
        on_resumption_token=ctx.on_token_update,
    )
    # On success:
    await ctx.clear()

Token caching: ``get_token`` caches the loaded value in memory after the first
call to avoid repeated ``persistence.load`` round-trips.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

__all__ = [
    "ResumableCallContext",
    "TokenPersistence",
]

logger = logging.getLogger(__name__)


@runtime_checkable
class TokenPersistence(Protocol):
    """Optional protocol for out-of-process resumption token storage.

    Implement this to persist tokens across hub process restarts
    (e.g. write to Redis, a local SQLite file, or shared memory).

    All methods are async to support non-blocking I/O backends.
    """

    async def save(self, call_id: str, token: str) -> None:
        """Persist the resumption token for ``call_id``."""
        ...  # pragma: no cover

    async def load(self, call_id: str) -> str | None:
        """Return the stored token for ``call_id``, or None if absent."""
        ...  # pragma: no cover

    async def delete(self, call_id: str) -> None:
        """Remove the stored token for ``call_id`` (e.g. after successful call)."""
        ...  # pragma: no cover


class _InMemoryTokenPersistence:
    """Default in-process token persistence.

    Simple dict-backed store. Does NOT survive process restart. Sufficient for
    the W4 single-process hub; replace with a Redis-backed implementation for
    multi-process or cross-restart resilience.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    async def save(self, call_id: str, token: str) -> None:
        self._tokens[call_id] = token

    async def load(self, call_id: str) -> str | None:
        return self._tokens.get(call_id)

    async def delete(self, call_id: str) -> None:
        self._tokens.pop(call_id, None)


class ResumableCallContext:
    """Tracks the ``resumption_token`` for one outbound hub→backend call.

    Thread-safe: all state is protected by an ``asyncio.Lock``.

    Token caching: once ``get_token`` loads from persistence, the value is
    cached in ``_current_token`` so subsequent calls are O(1) (no I/O).
    The cache is invalidated on ``on_token_update`` (new token received) and
    ``clear`` (call completed).

    Args:
        call_id: Stable identifier for this logical call (typically a UUID).
            Used as the key in the persistence store.
        persistence: Optional ``TokenPersistence`` implementation. Defaults to
            ``_InMemoryTokenPersistence`` (in-process, non-durable).
    """

    def __init__(
        self,
        call_id: str,
        persistence: TokenPersistence | None = None,
    ) -> None:
        self._call_id = call_id
        self._persistence: TokenPersistence = (
            persistence if persistence is not None else _InMemoryTokenPersistence()
        )
        # None = not yet loaded from persistence (cache miss sentinel).
        # After first get_token call: either a str token or _LOADED_NONE if
        # persistence returned None.
        self._current_token: str | None = None
        self._loaded: bool = False  # True once get_token has been called once
        self._lock = asyncio.Lock()

    async def get_token(self) -> str | None:
        """Return the current resumption token, or None for the first attempt.

        On the first call, loads from persistence (async I/O). Subsequent calls
        return the cached value without I/O.
        """
        async with self._lock:
            if not self._loaded:
                self._current_token = await self._persistence.load(self._call_id)
                self._loaded = True
            return self._current_token

    async def on_token_update(self, token: str) -> None:
        """Callback passed as ``on_resumption_token_update`` in ``ClientMessageMetadata``.

        Saves the new token to persistence and updates the in-memory cache.
        """
        async with self._lock:
            self._current_token = token
            self._loaded = True
            await self._persistence.save(self._call_id, token)
        logger.debug("Resumption token updated for call %s", self._call_id)

    async def clear(self) -> None:
        """Clear the token after a successful call completion.

        Removes from persistence and resets the in-memory cache so that a
        subsequent retry starts fresh (no stale token).
        """
        async with self._lock:
            self._current_token = None
            self._loaded = True  # stay "loaded" — the loaded value is now None
            await self._persistence.delete(self._call_id)
