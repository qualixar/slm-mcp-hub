"""Namespace engine for tool/resource/prompt name federation."""

from __future__ import annotations

import re

from slm_mcp_hub.core.constants import NAMESPACE_DELIMITER

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9]")


def safe_server_id(server_name: str) -> str:
    """Convert a server name to a safe alphanumeric ID for namespacing.

    Replaces all non-alphanumeric characters with underscores.
    """
    return _SAFE_NAME_RE.sub("_", server_name)


def make_unique_id(base_id: str, existing_ids: set[str]) -> str:
    """Ensure *base_id* is unique within *existing_ids*.

    Appends ``_2``, ``_3``, … if the base already exists.
    """
    if base_id not in existing_ids:
        return base_id
    counter = 2
    while f"{base_id}_{counter}" in existing_ids:
        counter += 1
    return f"{base_id}_{counter}"


def namespace_name(server_id: str, original_name: str) -> str:
    """Create a namespaced capability name.

    >>> namespace_name("github", "create_issue")
    'github__create_issue'
    """
    return f"{server_id}{NAMESPACE_DELIMITER}{original_name}"


def parse_namespaced(namespaced: str) -> tuple[str, str]:
    """Reverse a namespaced name into (server_id, original_name).

    Returns the split at the *first* delimiter occurrence so that
    original names containing the delimiter are preserved.

    >>> parse_namespaced("github__create_issue")
    ('github', 'create_issue')
    >>> parse_namespaced("remote__github__search")
    ('remote', 'github__search')
    """
    parts = namespaced.split(NAMESPACE_DELIMITER, 1)
    if len(parts) != 2:
        raise ValueError(
            f"Invalid namespaced name (no '{NAMESPACE_DELIMITER}' delimiter): {namespaced}"
        )
    return parts[0], parts[1]


def namespace_tool(server_id: str, tool_def: dict) -> dict:
    """Return a *new* tool definition with the name namespaced.

    The original definition is never mutated.
    """
    return {**tool_def, "name": namespace_name(server_id, tool_def["name"])}


def namespace_resource(server_id: str, resource_def: dict) -> dict:
    """Return a *new* resource definition with the URI namespaced."""
    return {**resource_def, "uri": namespace_name(server_id, resource_def["uri"])}


def namespace_resource_template(server_id: str, template_def: dict) -> dict:
    """Return a *new* resource template with the uriTemplate namespaced."""
    return {
        **template_def,
        "uriTemplate": namespace_name(server_id, template_def["uriTemplate"]),
    }


def namespace_prompt(server_id: str, prompt_def: dict) -> dict:
    """Return a *new* prompt definition with the name namespaced."""
    return {**prompt_def, "name": namespace_name(server_id, prompt_def["name"])}
