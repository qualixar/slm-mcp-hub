"""Session manager — creates, tracks, and destroys client sessions."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from slm_mcp_hub.core.constants import MAX_SESSIONS, SESSION_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionInfo:
    """Immutable snapshot of a client session."""

    session_id: str
    client_name: str
    connected_at: float
    last_activity: float
    project_path: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)


class SessionManager:
    """Manages client sessions with timeout and capacity limits.

    Each AI client (Claude Code, Copilot, etc.) gets a unique session
    when it connects to the hub's /mcp endpoint.
    """

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        timeout_seconds: int = SESSION_TIMEOUT_SECONDS,
    ) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._max_sessions = max_sessions
        self._timeout_seconds = timeout_seconds

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def create_session(
        self,
        client_name: str = "unknown",
        project_path: str = "",
        permissions: dict[str, Any] | None = None,
    ) -> str:
        """Create a new session and return the session_id.

        Raises ValueError if max sessions reached.
        """
        self._cleanup_expired()

        if len(self._sessions) >= self._max_sessions:
            raise ValueError(
                f"Max sessions ({self._max_sessions}) reached. "
                "Close an existing session first."
            )

        session_id = str(uuid.uuid4())
        now = time.time()
        info = SessionInfo(
            session_id=session_id,
            client_name=client_name,
            connected_at=now,
            last_activity=now,
            project_path=project_path,
            permissions=permissions or {},
        )
        self._sessions[session_id] = info
        logger.info("Session created: %s (client=%s)", session_id[:8], client_name)
        return session_id

    def get_session(self, session_id: str) -> SessionInfo | None:
        """Get session info, or None if not found/expired."""
        info = self._sessions.get(session_id)
        if info is None:
            return None
        if self._is_expired(info):
            self._remove(session_id)
            return None
        return info

    def touch(self, session_id: str) -> bool:
        """Update last_activity timestamp. Returns False if session not found."""
        info = self._sessions.get(session_id)
        if info is None:
            return False
        if self._is_expired(info):
            self._remove(session_id)
            return False
        # Create new immutable SessionInfo with updated timestamp
        updated = SessionInfo(
            session_id=info.session_id,
            client_name=info.client_name,
            connected_at=info.connected_at,
            last_activity=time.time(),
            project_path=info.project_path,
            permissions=info.permissions,
        )
        self._sessions[session_id] = updated
        return True

    def destroy_session(self, session_id: str) -> bool:
        """Destroy a session. Returns True if it existed."""
        return self._remove(session_id)

    def list_sessions(self) -> list[SessionInfo]:
        """Return all active (non-expired) sessions."""
        self._cleanup_expired()
        return list(self._sessions.values())

    def get_stats(self) -> dict[str, Any]:
        """Return session statistics."""
        self._cleanup_expired()
        return {
            "active_sessions": len(self._sessions),
            "max_sessions": self._max_sessions,
            "timeout_seconds": self._timeout_seconds,
            "sessions": [
                {
                    "session_id": s.session_id[:8] + "...",
                    "client_name": s.client_name,
                    "connected_at": s.connected_at,
                    "last_activity": s.last_activity,
                    "project_path": s.project_path,
                }
                for s in self._sessions.values()
            ],
        }

    def _is_expired(self, info: SessionInfo) -> bool:
        return (time.time() - info.last_activity) > self._timeout_seconds

    def _remove(self, session_id: str) -> bool:
        if session_id in self._sessions:
            info = self._sessions.pop(session_id)
            logger.info("Session removed: %s (client=%s)", session_id[:8], info.client_name)
            return True
        return False

    def _cleanup_expired(self) -> None:
        expired = [
            sid for sid, info in self._sessions.items() if self._is_expired(info)
        ]
        for sid in expired:
            self._remove(sid)
