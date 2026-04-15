"""Audit logger — records all hub actions for compliance and debugging.

Every tool call, permission decision, and lifecycle event is logged.
Fire-and-forget writes — never blocks tool calls.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from slm_mcp_hub.core.constants import AUDIT_RETENTION_DAYS
from slm_mcp_hub.storage.database import HubDatabase

logger = logging.getLogger(__name__)


class AuditLogger:
    """Writes audit events to SQLite. Never blocks tool calls."""

    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def log(self, session_id: str, action: str, details: dict[str, Any] | None = None) -> None:
        """Log an audit event. Fire-and-forget — errors logged, not raised."""
        try:
            self._db.insert("audit_log", {
                "session_id": session_id,
                "action": action,
                "details": json.dumps(details or {}),
                "timestamp": time.time(),
            })
        except Exception as exc:
            logger.error("Audit write failed: %s", exc)

    def query(
        self,
        session_id: str | None = None,
        action: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit log with optional filters."""
        conditions = []
        params: list[Any] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM audit_log WHERE {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self._db.fetch_all(sql, tuple(params))
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "action": row["action"],
                "details": json.loads(row["details"]),
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    def cleanup(self, retention_days: int = AUDIT_RETENTION_DAYS) -> int:
        """Delete audit entries older than retention_days. Returns count deleted."""
        cutoff = time.time() - (retention_days * 86400)
        cursor = self._db.execute(
            "DELETE FROM audit_log WHERE timestamp < ?", (cutoff,)
        )
        self._db.commit()
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Audit cleanup: %d entries older than %d days removed", deleted, retention_days)
        return deleted
