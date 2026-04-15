"""Permission engine — per-session role-based tool access control.

Gap 8: Permission Model — deny dangerous tools, scope by session/project.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class PermissionAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"


@dataclass(frozen=True)
class PermissionRule:
    """A single permission rule."""

    scope: str           # "global", "session:<pattern>", "project:<pattern>"
    server: str          # server name or "*"
    tools: tuple[str, ...] = ("*",)  # tool names or ("*",)
    action: PermissionAction = PermissionAction.ALLOW

    @property
    def specificity(self) -> int:
        """Higher = more specific. project > session > global."""
        if self.scope.startswith("project:"):
            return 3
        if self.scope.startswith("session:"):
            return 2
        return 1  # global


class PermissionResult:
    """Result of a permission check."""

    __slots__ = ("action", "rule", "message")

    def __init__(self, action: PermissionAction, rule: PermissionRule | None = None, message: str = "") -> None:
        self.action = action
        self.rule = rule
        self.message = message

    @property
    def allowed(self) -> bool:
        return self.action != PermissionAction.DENY


class PermissionEngine:
    """Evaluates permission rules against session context.

    Default: allow everything. Rules add restrictions.
    """

    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self._rules = list(rules) if rules else []

    def add_rule(self, rule: PermissionRule) -> None:
        self._rules.append(rule)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def check(
        self,
        server_name: str,
        tool_name: str,
        client_name: str = "",
        project_path: str = "",
    ) -> PermissionResult:
        """Check if a tool call is permitted.

        Finds the most specific matching rule. Default: allow.
        """
        matching: list[PermissionRule] = []

        for rule in self._rules:
            if not self._matches_scope(rule.scope, client_name, project_path):
                continue
            if not self._matches_server(rule.server, server_name):
                continue
            if not self._matches_tool(rule.tools, tool_name):
                continue
            matching.append(rule)

        if not matching:
            return PermissionResult(PermissionAction.ALLOW)

        # Most specific rule wins
        best = max(matching, key=lambda r: (r.specificity, len(r.tools[0]) if r.tools else 0))

        if best.action == PermissionAction.DENY:
            msg = f"Permission denied: {server_name}/{tool_name} blocked by rule scope={best.scope}"
            logger.warning(msg)
            return PermissionResult(best.action, best, msg)

        if best.action == PermissionAction.WARN:
            msg = f"Permission warning: {server_name}/{tool_name} flagged by rule scope={best.scope}"
            logger.info(msg)
            return PermissionResult(best.action, best, msg)

        return PermissionResult(best.action, best)

    @staticmethod
    def _matches_scope(scope: str, client_name: str, project_path: str) -> bool:
        if scope == "global":
            return True
        if scope.startswith("session:"):
            pattern = scope[len("session:"):]
            return fnmatch.fnmatch(client_name.lower(), pattern.lower())
        if scope.startswith("project:"):
            pattern = scope[len("project:"):]
            return fnmatch.fnmatch(project_path, pattern)
        return False

    @staticmethod
    def _matches_server(rule_server: str, actual_server: str) -> bool:
        if rule_server == "*":
            return True
        return fnmatch.fnmatch(actual_server, rule_server)

    @staticmethod
    def _matches_tool(rule_tools: tuple[str, ...], actual_tool: str) -> bool:
        for pattern in rule_tools:
            if pattern == "*" or fnmatch.fnmatch(actual_tool, pattern):
                return True
        return False

    @classmethod
    def from_config(cls, rules_data: list[dict[str, Any]]) -> PermissionEngine:
        """Create engine from config JSON format."""
        rules = []
        for r in rules_data:
            rules.append(PermissionRule(
                scope=r.get("scope", "global"),
                server=r.get("server", "*"),
                tools=tuple(r.get("tools", ["*"])),
                action=PermissionAction(r.get("action", "allow")),
            ))
        return cls(rules)
