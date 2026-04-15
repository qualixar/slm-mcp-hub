"""Capability registry — central index of all federated tools/resources/prompts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from slm_mcp_hub.federation.namespace import (
    make_unique_id,
    namespace_name,
    namespace_prompt,
    namespace_resource,
    namespace_resource_template,
    namespace_tool,
    parse_namespaced,
    safe_server_id,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredCapability:
    """A single federated capability with routing metadata."""

    server_name: str
    server_id: str
    original_name: str
    definition: dict[str, Any]


class CapabilityRegistry:
    """Central registry of all namespaced capabilities across MCP servers.

    Thread-safe for reads (dict lookups).  Writes go through ``sync()``
    which rebuilds the maps atomically.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredCapability] = {}
        self._resources: dict[str, RegisteredCapability] = {}
        self._resource_templates: dict[str, RegisteredCapability] = {}
        self._prompts: dict[str, RegisteredCapability] = {}
        self._server_ids: dict[str, str] = {}  # server_name → server_id

    # -- Public read API -------------------------------------------------------

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    @property
    def resource_count(self) -> int:
        return len(self._resources)

    @property
    def prompt_count(self) -> int:
        return len(self._prompts)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all registered tool definitions (namespaced)."""
        return [cap.definition for cap in self._tools.values()]

    def list_resources(self) -> list[dict[str, Any]]:
        """Return all registered resource definitions (namespaced)."""
        return [cap.definition for cap in self._resources.values()]

    def list_resource_templates(self) -> list[dict[str, Any]]:
        """Return all registered resource template definitions (namespaced)."""
        return [cap.definition for cap in self._resource_templates.values()]

    def list_prompts(self) -> list[dict[str, Any]]:
        """Return all registered prompt definitions (namespaced)."""
        return [cap.definition for cap in self._prompts.values()]

    def lookup_tool(self, namespaced_name: str) -> RegisteredCapability | None:
        """Look up a tool by its namespaced name."""
        return self._tools.get(namespaced_name)

    def lookup_resource(self, namespaced_uri: str) -> RegisteredCapability | None:
        """Look up a resource by its namespaced URI."""
        return self._resources.get(namespaced_uri)

    def lookup_prompt(self, namespaced_name: str) -> RegisteredCapability | None:
        """Look up a prompt by its namespaced name."""
        return self._prompts.get(namespaced_name)

    def get_server_id(self, server_name: str) -> str | None:
        """Get the safe server_id for a given server_name."""
        return self._server_ids.get(server_name)

    # -- Sync API --------------------------------------------------------------

    def sync(self, servers: dict[str, dict[str, Any]]) -> bool:
        """Rebuild all registries from the given server capabilities.

        Args:
            servers: ``{server_name: {"tools": [...], "resources": [...],
                       "resource_templates": [...], "prompts": [...]}}``

        Returns:
            True if any capability changed, False if identical to before.
        """
        old_tool_keys = set(self._tools.keys())
        old_resource_keys = set(self._resources.keys())
        old_prompt_keys = set(self._prompts.keys())

        new_tools: dict[str, RegisteredCapability] = {}
        new_resources: dict[str, RegisteredCapability] = {}
        new_templates: dict[str, RegisteredCapability] = {}
        new_prompts: dict[str, RegisteredCapability] = {}
        new_server_ids: dict[str, str] = {}
        used_ids: set[str] = set()

        for server_name, caps in servers.items():
            sid = safe_server_id(server_name)
            sid = make_unique_id(sid, used_ids)
            used_ids.add(sid)
            new_server_ids[server_name] = sid

            for tool in caps.get("tools", []):
                ns_tool = namespace_tool(sid, tool)
                ns_name = ns_tool["name"]
                new_tools[ns_name] = RegisteredCapability(
                    server_name=server_name,
                    server_id=sid,
                    original_name=tool["name"],
                    definition=ns_tool,
                )

            for res in caps.get("resources", []):
                ns_res = namespace_resource(sid, res)
                ns_uri = ns_res["uri"]
                new_resources[ns_uri] = RegisteredCapability(
                    server_name=server_name,
                    server_id=sid,
                    original_name=res["uri"],
                    definition=ns_res,
                )

            for tmpl in caps.get("resource_templates", []):
                ns_tmpl = namespace_resource_template(sid, tmpl)
                ns_uri = ns_tmpl["uriTemplate"]
                new_templates[ns_uri] = RegisteredCapability(
                    server_name=server_name,
                    server_id=sid,
                    original_name=tmpl["uriTemplate"],
                    definition=ns_tmpl,
                )

            for prompt in caps.get("prompts", []):
                ns_prompt = namespace_prompt(sid, prompt)
                ns_name = ns_prompt["name"]
                new_prompts[ns_name] = RegisteredCapability(
                    server_name=server_name,
                    server_id=sid,
                    original_name=prompt["name"],
                    definition=ns_prompt,
                )

        # Atomic swap
        self._tools = new_tools
        self._resources = new_resources
        self._resource_templates = new_templates
        self._prompts = new_prompts
        self._server_ids = new_server_ids

        changed = (
            set(new_tools.keys()) != old_tool_keys
            or set(new_resources.keys()) != old_resource_keys
            or set(new_prompts.keys()) != old_prompt_keys
        )

        if changed:
            logger.info(
                "Registry synced: %d tools, %d resources, %d templates, %d prompts from %d servers",
                len(new_tools),
                len(new_resources),
                len(new_templates),
                len(new_prompts),
                len(servers),
            )

        return changed

    def clear(self) -> None:
        """Clear all registries."""
        self._tools.clear()
        self._resources.clear()
        self._resource_templates.clear()
        self._prompts.clear()
        self._server_ids.clear()
