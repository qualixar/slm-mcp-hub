"""Intelligent caching engine for tool call results.

Gap 11: Intelligent Caching — TTL-based, content-hash matching,
LRU eviction. Reduces duplicate API calls by 30%+.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any

from slm_mcp_hub.core.constants import CACHE_DEFAULT_TTL_SECONDS, CACHE_MAX_ENTRIES

logger = logging.getLogger(__name__)

# Tools that should NEVER be cached (stateful / side-effecting)
DEFAULT_NO_CACHE_TOOLS: frozenset[str] = frozenset({
    "remember", "observe", "forget", "delete_memory", "update_memory",
    "session_init", "close_session", "run_maintenance",
    "mesh_send", "mesh_lock",
    "create_issue", "create_branch", "push_files",
    "create_record", "update_records", "delete_records",
})


def _make_cache_key(server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
    """Create a deterministic cache key from server + tool + args."""
    args_json = json.dumps(arguments, sort_keys=True, default=str)
    args_hash = hashlib.sha256(args_json.encode()).hexdigest()[:16]
    return f"{server_name}__{tool_name}__{args_hash}"


class CacheEntry:
    """A single cached tool call result."""

    __slots__ = ("key", "result", "created_at", "ttl_seconds", "hit_count")

    def __init__(self, key: str, result: dict[str, Any], ttl_seconds: int) -> None:
        self.key = key
        self.result = result
        self.created_at = time.time()
        self.ttl_seconds = ttl_seconds
        self.hit_count = 0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_seconds


class CacheEngine:
    """In-memory LRU cache for tool call results.

    Standalone — no SLM dependency. With SLM plugin,
    cache can be persisted to tiered storage.
    """

    def __init__(
        self,
        default_ttl: int = CACHE_DEFAULT_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES,
        no_cache_tools: frozenset[str] | None = None,
    ) -> None:
        self._cache: dict[str, CacheEntry] = {}
        self._access_order: OrderedDict[str, None] = OrderedDict()  # Most recent at end
        self._default_ttl = default_ttl
        self._max_entries = max_entries
        self._no_cache_tools = no_cache_tools or DEFAULT_NO_CACHE_TOOLS
        self._hits = 0
        self._misses = 0

    @property
    def size(self) -> int:
        return len(self._cache)

    @property
    def hit_count(self) -> int:
        return self._hits

    @property
    def miss_count(self) -> int:
        return self._misses

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    def is_cacheable(self, tool_name: str) -> bool:
        """Check if a tool's results should be cached."""
        # Check the original (non-namespaced) tool name
        base_name = tool_name.split("__")[-1] if "__" in tool_name else tool_name
        return base_name not in self._no_cache_tools

    def get(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Look up a cached result. Returns None on miss or expiry."""
        if not self.is_cacheable(tool_name):
            self._misses += 1
            return None

        key = _make_cache_key(server_name, tool_name, arguments)
        entry = self._cache.get(key)

        if entry is None:
            self._misses += 1
            return None

        if entry.is_expired:
            self._remove(key)
            self._misses += 1
            return None

        # Cache hit — update access order and hit count
        entry.hit_count += 1
        self._hits += 1
        self._touch_access(key)
        logger.debug("Cache HIT: %s (hits=%d)", key[:40], entry.hit_count)
        return entry.result

    def put(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        """Store a tool call result in cache."""
        if not self.is_cacheable(tool_name):
            return

        key = _make_cache_key(server_name, tool_name, arguments)
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl

        # Evict if at capacity
        while len(self._cache) >= self._max_entries:
            self._evict_lru()

        self._cache[key] = CacheEntry(key=key, result=result, ttl_seconds=ttl)
        self._touch_access(key)
        logger.debug("Cache PUT: %s (ttl=%ds)", key[:40], ttl)

    def invalidate(self, server_name: str, tool_name: str | None = None) -> int:
        """Invalidate cache entries for a server (optionally specific tool).

        Returns count of entries removed.
        """
        prefix = f"{server_name}__"
        if tool_name:
            prefix = f"{server_name}__{tool_name}__"

        keys_to_remove = [k for k in self._cache if k.startswith(prefix)]
        for key in keys_to_remove:
            self._remove(key)
        return len(keys_to_remove)

    def clear(self) -> None:
        """Clear all cache entries."""
        count = len(self._cache)
        self._cache.clear()
        self._access_order.clear()
        logger.info("Cache cleared (%d entries removed)", count)

    def get_stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        self._cleanup_expired()
        return {
            "size": len(self._cache),
            "max_entries": self._max_entries,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 3),
            "default_ttl": self._default_ttl,
        }

    def _touch_access(self, key: str) -> None:
        """Move key to end of access order (most recently used)."""
        self._access_order.pop(key, None)
        self._access_order[key] = None

    def _evict_lru(self) -> None:
        """Evict the least recently used entry."""
        if not self._access_order:
            return
        lru_key = next(iter(self._access_order))
        self._remove(lru_key)
        logger.debug("Cache LRU evict: %s", lru_key[:40])

    def _remove(self, key: str) -> None:
        """Remove an entry from cache and access order."""
        self._cache.pop(key, None)
        self._access_order.pop(key, None)

    def _cleanup_expired(self) -> None:
        """Remove all expired entries."""
        expired = [k for k, v in self._cache.items() if v.is_expired]
        for key in expired:
            self._remove(key)
