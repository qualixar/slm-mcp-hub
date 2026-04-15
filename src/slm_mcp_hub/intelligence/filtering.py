"""Smart tool filtering — context-aware tool exposure.

Gap 4: Smart Tool Filtering — project-type detection, frequency ranking,
Meta-MCP pattern for token savings.

Includes deterministic activity classification:
13 categories from tool names + keywords. No LLM calls.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Activity classification — 13 deterministic categories.
# Maps tool name patterns to activity categories.
ACTIVITY_CATEGORIES: dict[str, list[str]] = {
    "coding": ["edit", "write", "create_file", "update_file"],
    "debugging": ["bash", "evaluate_script", "console"],
    "testing": ["pytest", "vitest", "jest", "test"],
    "planning": ["plan", "todo", "task"],
    "delegation": ["agent", "spawn"],
    "git_ops": ["git", "commit", "push", "branch", "pull_request"],
    "research": ["web_search", "perplexity", "gemini-search", "tavily", "exa"],
    "documentation": ["docs", "readme", "markdown"],
    "build_deploy": ["build", "docker", "deploy", "npm"],
    "data": ["sql", "database", "sqlite", "duckdb"],
    "media": ["image", "video", "audio", "speech"],
    "memory": ["remember", "recall", "observe", "forget"],
    "exploration": ["read", "grep", "glob", "search", "query"],
}


def classify_activity(tool_name: str) -> str:
    """Classify a tool call into an activity category.

    Deterministic — no LLM calls. Returns 'general' if no match.
    """
    lower = tool_name.lower()
    for category, patterns in ACTIVITY_CATEGORIES.items():
        for pattern in patterns:
            if pattern in lower:
                return category
    return "general"

# Project type → relevant server name prefixes
PROJECT_TYPE_HINTS: dict[str, list[str]] = {
    "python": ["context7", "github", "sqlite", "gemini", "superlocalmemory", "semantic_scholar"],
    "typescript": ["context7", "github", "playwright", "gemini", "superlocalmemory"],
    "javascript": ["context7", "github", "playwright", "gemini", "superlocalmemory"],
    "web": ["context7", "playwright", "sharp", "gemini", "github", "stitch"],
    "rust": ["context7", "github", "gemini", "superlocalmemory"],
    "go": ["context7", "github", "gemini", "superlocalmemory"],
    "java": ["context7", "github", "gemini", "superlocalmemory"],
    "docs": ["context7", "gemini", "obsidian", "zotero"],
    "data": ["sqlite", "duckdb", "gemini", "github"],
}

# File extensions → project type
EXT_TO_PROJECT_TYPE: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".astro": "web", ".html": "web", ".css": "web", ".scss": "web", ".svelte": "web", ".vue": "web",
    ".rs": "rust",
    ".go": "go",
    ".java": "java", ".kt": "java",
    ".md": "docs", ".mdx": "docs", ".rst": "docs",
    ".sql": "data", ".parquet": "data", ".csv": "data",
}


def detect_project_type(project_path: str) -> str | None:
    """Detect project type from file extensions in the directory.

    Scans top-level and one level deep. Returns the most common type.
    """
    if not project_path:
        return None

    path = Path(project_path)
    if not path.is_dir():
        return None

    type_counts: Counter[str] = Counter()
    try:
        for child in path.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_file():
                pt = EXT_TO_PROJECT_TYPE.get(child.suffix.lower())
                if pt:
                    type_counts[pt] += 1
            elif child.is_dir() and not child.name.startswith("."):
                # One level deep
                try:
                    for grandchild in child.iterdir():
                        if grandchild.is_file():
                            pt = EXT_TO_PROJECT_TYPE.get(grandchild.suffix.lower())
                            if pt:
                                type_counts[pt] += 1
                except PermissionError:
                    continue
    except PermissionError:
        return None

    if not type_counts:
        return None

    return type_counts.most_common(1)[0][0]


def get_relevant_servers(project_type: str | None) -> list[str]:
    """Get server name prefixes relevant to a project type."""
    if project_type is None:
        return []
    return PROJECT_TYPE_HINTS.get(project_type, [])


class ToolFilter:
    """Filters and ranks tools based on context and usage frequency.

    Standalone mode: project-type detection + frequency ranking.
    With SLM plugin: learned patterns override heuristics.
    """

    def __init__(self, max_direct_tools: int = 20) -> None:
        self._max_direct = max_direct_tools
        self._usage_counts: Counter[str] = Counter()

    def record_usage(self, namespaced_tool: str) -> None:
        """Record a tool usage for frequency ranking."""
        self._usage_counts[namespaced_tool] += 1

    def get_top_tools(self, n: int | None = None) -> list[str]:
        """Get the most frequently used tools."""
        limit = n or self._max_direct
        return [tool for tool, _ in self._usage_counts.most_common(limit)]

    def filter_tools(
        self,
        all_tools: list[dict[str, Any]],
        project_path: str = "",
    ) -> list[dict[str, Any]]:
        """Filter and rank tools based on project context and usage.

        Returns tools sorted by relevance: project-relevant first,
        then by frequency, then alphabetically.
        """
        project_type = detect_project_type(project_path)
        relevant_servers = get_relevant_servers(project_type)

        def sort_key(tool: dict[str, Any]) -> tuple[int, int, str]:
            name = tool.get("name", "")
            # Priority 1: Is it from a relevant server?
            server_prefix = name.split("__")[0] if "__" in name else ""
            is_relevant = 0 if server_prefix in relevant_servers else 1
            # Priority 2: How frequently used? (higher = better, negate for sort)
            freq = -self._usage_counts.get(name, 0)
            # Priority 3: Alphabetical
            return (is_relevant, freq, name)

        return sorted(all_tools, key=sort_key)

    def get_stats(self) -> dict[str, Any]:
        """Return filter statistics."""
        return {
            "total_unique_tools_used": len(self._usage_counts),
            "top_10_tools": self._usage_counts.most_common(10),
            "max_direct_tools": self._max_direct,
        }
