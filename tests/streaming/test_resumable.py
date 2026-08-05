"""W4-P3 tests — ResumableCallContext and TokenPersistence.

TDD: written BEFORE implementation. Verifies:
1. Token roundtrip — on_token_update saves, get_token returns.
2. clear() empties the token.
3. Custom TokenPersistence receives save/load/delete calls.
4. First call to get_token on fresh context returns None.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from slm_mcp_hub.streaming.resumable import (
    ResumableCallContext,
    TokenPersistence,
)

# ---------------------------------------------------------------------------
# TokenPersistence protocol check
# ---------------------------------------------------------------------------


class TestTokenPersistenceProtocol:
    def test_custom_class_satisfies_protocol(self) -> None:
        """A class implementing save/load/delete satisfies TokenPersistence."""

        class MyPersistence:
            async def save(self, call_id: str, token: str) -> None: ...
            async def load(self, call_id: str) -> str | None: ...
            async def delete(self, call_id: str) -> None: ...

        assert isinstance(MyPersistence(), TokenPersistence)

    def test_partial_class_fails_protocol(self) -> None:
        """A class missing 'delete' does NOT satisfy TokenPersistence."""

        class Partial:
            async def save(self, call_id: str, token: str) -> None: ...
            async def load(self, call_id: str) -> str | None: ...
            # no delete

        assert not isinstance(Partial(), TokenPersistence)


# ---------------------------------------------------------------------------
# ResumableCallContext — in-memory default
# ---------------------------------------------------------------------------


class TestResumableCallContextDefault:
    async def test_first_call_returns_none(self) -> None:
        """Fresh ResumableCallContext.get_token() returns None (no prior token)."""
        ctx = ResumableCallContext("call-abc")
        result = await ctx.get_token()
        assert result is None

    async def test_token_roundtrip(self) -> None:
        """on_token_update('tok1') saves; get_token() returns 'tok1'."""
        ctx = ResumableCallContext("call-xyz")
        await ctx.on_token_update("tok1")
        result = await ctx.get_token()
        assert result == "tok1"

    async def test_token_update_multiple_times(self) -> None:
        """Multiple updates — only the latest token is returned."""
        ctx = ResumableCallContext("call-multi")
        await ctx.on_token_update("tok-a")
        await ctx.on_token_update("tok-b")
        await ctx.on_token_update("tok-c")
        assert await ctx.get_token() == "tok-c"

    async def test_clear_empties_token(self) -> None:
        """After successful call, clear() empties the token; get_token() returns None."""
        ctx = ResumableCallContext("call-done")
        await ctx.on_token_update("saved-tok")
        assert await ctx.get_token() == "saved-tok"

        await ctx.clear()
        assert await ctx.get_token() is None

    async def test_clear_on_fresh_context_is_noop(self) -> None:
        """clear() on a context that has never been updated is safe (no crash)."""
        ctx = ResumableCallContext("call-fresh")
        await ctx.clear()  # must not raise
        assert await ctx.get_token() is None


# ---------------------------------------------------------------------------
# ResumableCallContext — custom persistence
# ---------------------------------------------------------------------------


class TestResumableCallContextCustomPersistence:
    async def test_custom_persistence_save_called(self) -> None:
        """Injected TokenPersistence receives save call with correct args."""
        mock_persistence = AsyncMock()
        mock_persistence.load = AsyncMock(return_value=None)
        mock_persistence.save = AsyncMock()
        mock_persistence.delete = AsyncMock()

        ctx = ResumableCallContext("call-custom", persistence=mock_persistence)
        await ctx.on_token_update("my-token")

        mock_persistence.save.assert_called_once_with("call-custom", "my-token")

    async def test_custom_persistence_load_called_on_first_get(self) -> None:
        """get_token() loads from persistence on first call (cache miss)."""
        mock_persistence = AsyncMock()
        mock_persistence.load = AsyncMock(return_value="persisted-token")
        mock_persistence.save = AsyncMock()
        mock_persistence.delete = AsyncMock()

        ctx = ResumableCallContext("call-load", persistence=mock_persistence)
        result = await ctx.get_token()

        mock_persistence.load.assert_called_once_with("call-load")
        assert result == "persisted-token"

    async def test_custom_persistence_load_not_repeated(self) -> None:
        """After first get_token, subsequent calls use cached value (no re-load)."""
        mock_persistence = AsyncMock()
        mock_persistence.load = AsyncMock(return_value="cached-tok")
        mock_persistence.save = AsyncMock()
        mock_persistence.delete = AsyncMock()

        ctx = ResumableCallContext("call-cache", persistence=mock_persistence)
        await ctx.get_token()
        await ctx.get_token()
        await ctx.get_token()

        # load called only ONCE (first get_token)
        assert mock_persistence.load.call_count == 1

    async def test_custom_persistence_delete_called_on_clear(self) -> None:
        """clear() calls persistence.delete with the call_id."""
        mock_persistence = AsyncMock()
        mock_persistence.load = AsyncMock(return_value=None)
        mock_persistence.save = AsyncMock()
        mock_persistence.delete = AsyncMock()

        ctx = ResumableCallContext("call-del", persistence=mock_persistence)
        await ctx.on_token_update("tok")
        await ctx.clear()

        mock_persistence.delete.assert_called_once_with("call-del")

    async def test_custom_persistence_full_lifecycle(self) -> None:
        """Full lifecycle: update → get → clear with all persistence calls verified."""
        mock_persistence = AsyncMock()
        mock_persistence.load = AsyncMock(return_value=None)
        mock_persistence.save = AsyncMock()
        mock_persistence.delete = AsyncMock()

        ctx = ResumableCallContext("lifecycle-call", persistence=mock_persistence)

        # First get: None (no prior token)
        assert await ctx.get_token() is None

        # Update token
        await ctx.on_token_update("session-token-v1")
        assert await ctx.get_token() == "session-token-v1"

        # Update again
        await ctx.on_token_update("session-token-v2")
        assert await ctx.get_token() == "session-token-v2"

        # Clear
        await ctx.clear()
        assert await ctx.get_token() is None

        # Verify call sequence
        assert mock_persistence.save.call_count == 2
        mock_persistence.delete.assert_called_once()


# ---------------------------------------------------------------------------
# Concurrency safety — basic
# ---------------------------------------------------------------------------


class TestConcurrencySafety:
    async def test_concurrent_updates_are_safe(self) -> None:
        """Multiple concurrent on_token_update calls do not crash or corrupt state."""
        import asyncio

        ctx = ResumableCallContext("concurrent-call")

        # Fire 20 concurrent updates
        tokens = [f"tok-{i}" for i in range(20)]
        await asyncio.gather(*[ctx.on_token_update(t) for t in tokens])

        # Must have SOME token (one of the concurrent winners)
        result = await ctx.get_token()
        assert result is not None
        assert result in tokens
