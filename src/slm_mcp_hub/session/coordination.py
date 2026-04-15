"""Cross-session coordination — lock/conflict prevention for shared MCPs."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT = 30.0  # seconds


@dataclass(frozen=True)
class LockInfo:
    """Immutable lock record."""

    resource: str
    session_id: str
    acquired_at: float
    timeout_seconds: float


class SessionCoordinator:
    """Standalone cross-session lock/conflict prevention.

    Prevents two sessions from writing to the same stateful MCP
    simultaneously (e.g., concurrent sqlite writes).

    Upgraded to SLM Mesh distributed locks in Phase 6 plugin.
    """

    def __init__(self) -> None:
        self._locks: dict[str, LockInfo] = {}

    def acquire(
        self,
        resource: str,
        session_id: str,
        timeout_seconds: float = DEFAULT_LOCK_TIMEOUT,
    ) -> bool:
        """Acquire a lock on a resource for a session.

        Returns True if lock acquired, False if already held by another session.
        Auto-releases expired locks.
        """
        self._cleanup_expired()

        existing = self._locks.get(resource)
        if existing is not None:
            if existing.session_id == session_id:
                return True  # Already held by same session
            return False  # Held by another session

        self._locks[resource] = LockInfo(
            resource=resource,
            session_id=session_id,
            acquired_at=time.time(),
            timeout_seconds=timeout_seconds,
        )
        logger.debug("Lock acquired: %s by session %s", resource, session_id[:8])
        return True

    def release(self, resource: str, session_id: str) -> bool:
        """Release a lock. Returns True if released, False if not held."""
        existing = self._locks.get(resource)
        if existing is None:
            return False
        if existing.session_id != session_id:
            return False  # Can't release another session's lock

        del self._locks[resource]
        logger.debug("Lock released: %s by session %s", resource, session_id[:8])
        return True

    def is_locked(self, resource: str) -> bool:
        """Check if a resource is currently locked (non-expired)."""
        self._cleanup_expired()
        return resource in self._locks

    def get_lock_holder(self, resource: str) -> str | None:
        """Get the session_id holding the lock, or None."""
        self._cleanup_expired()
        lock = self._locks.get(resource)
        return lock.session_id if lock else None

    def get_locks(self) -> list[LockInfo]:
        """Return all active (non-expired) locks."""
        self._cleanup_expired()
        return list(self._locks.values())

    def release_all_for_session(self, session_id: str) -> int:
        """Release all locks held by a session (e.g., on disconnect). Returns count."""
        to_release = [r for r, l in self._locks.items() if l.session_id == session_id]
        for resource in to_release:
            del self._locks[resource]
        if to_release:
            logger.debug("Released %d locks for session %s", len(to_release), session_id[:8])
        return len(to_release)

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [
            r for r, l in self._locks.items()
            if (now - l.acquired_at) > l.timeout_seconds
        ]
        for resource in expired:
            logger.debug("Lock expired: %s", resource)
            del self._locks[resource]
