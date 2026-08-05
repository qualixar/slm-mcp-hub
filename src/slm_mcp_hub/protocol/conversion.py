"""Conversion functions between neutral product models and wire/SDK types.

Three conversion directions:
  1. neutral → wire dict       (byte-for-byte identical to mcp_endpoint.py output)
  2. neutral → SDK mcp.types   (for P03 inbound SDK server adapter)
  3. SDK mcp.types → neutral   (for P04 outbound SDK client adapter)

Every conversion that handles a tagged union (content blocks, resource
contents) raises ``ValueError`` on unknown types — no silent pass-through.
"""

from __future__ import annotations

from typing import Any

import mcp.types as t

from slm_mcp_hub.protocol.models import (
    CallToolOutcome,
    DiscoverOutcome,
    InitializeOutcome,
    PromptGetOutcome,
    PromptsListOutcome,
    ResourceReadOutcome,
    ResourcesListOutcome,
    ResourceTemplatesListOutcome,
    ToolsListOutcome,
)

# ---------------------------------------------------------------------------
# Direction 1: neutral → wire dicts
# ---------------------------------------------------------------------------

def tools_list_to_wire(outcome: ToolsListOutcome) -> dict[str, Any]:
    """Convert a ToolsListOutcome to the ``tools/list`` wire result dict."""
    return {"tools": list(outcome.tools)}


def call_tool_outcome_to_wire(outcome: CallToolOutcome) -> dict[str, Any]:
    """Convert a CallToolOutcome to the ``tools/call`` wire result dict.

    A routed (federated) outcome carries the upstream server's verbatim result
    in ``raw``; it is returned unchanged so unmodelled keys (``structuredContent``,
    ``_meta``, an explicit ``isError: false``) survive exactly as the original
    hand-rolled endpoint forwarded them.

    For Hub-generated outcomes (``raw is None``) ``isError`` is included only
    when True, matching the existing meta-tool handler behaviour.
    """
    if outcome.raw is not None:
        return outcome.raw
    wire: dict[str, Any] = {"content": list(outcome.content)}
    if outcome.is_error:
        wire["isError"] = True
    return wire


def resources_list_to_wire(outcome: ResourcesListOutcome) -> dict[str, Any]:
    """Convert a ResourcesListOutcome to the ``resources/list`` wire dict."""
    return {"resources": list(outcome.resources)}


def resource_templates_list_to_wire(outcome: ResourceTemplatesListOutcome) -> dict[str, Any]:
    """Convert a ResourceTemplatesListOutcome to the wire dict."""
    return {"resourceTemplates": list(outcome.resource_templates)}


def resource_read_to_wire(outcome: ResourceReadOutcome) -> dict[str, Any]:
    """Return the raw result dict — no transformation needed for P02."""
    return outcome.raw


def prompts_list_to_wire(outcome: PromptsListOutcome) -> dict[str, Any]:
    """Convert a PromptsListOutcome to the ``prompts/list`` wire dict."""
    return {"prompts": list(outcome.prompts)}


def prompt_get_to_wire(outcome: PromptGetOutcome) -> dict[str, Any]:
    """Return the raw result dict — no transformation needed for P02."""
    return outcome.raw


def initialize_to_wire(outcome: InitializeOutcome) -> dict[str, Any]:
    """Convert an InitializeOutcome to the ``initialize`` wire result dict."""
    return {
        "protocolVersion": outcome.protocol_version,
        "capabilities": dict(outcome.capabilities),
        "serverInfo": {
            "name": outcome.server_name,
            "version": outcome.server_version,
        },
    }


def discover_to_wire(outcome: DiscoverOutcome) -> dict[str, Any]:
    """Convert a DiscoverOutcome to the ``server/discover`` wire result dict."""
    return {
        "supportedVersions": list(outcome.supported_versions),
        "capabilities": dict(outcome.capabilities),
        "serverInfo": {
            "name": outcome.server_name,
            "version": outcome.server_version,
        },
        "instructions": outcome.instructions,
    }


# ---------------------------------------------------------------------------
# Direction 2 helper: SDK content block → neutral wire-format dict
# ---------------------------------------------------------------------------

def sdk_content_block_to_dict(block: t.ContentBlock) -> dict[str, Any]:
    """Convert an SDK ``ContentBlock`` to a wire-format dict.

    Covers all five types in the ``ContentBlock`` union (mcp==2.0.0):
    TextContent, ImageContent, AudioContent, ResourceLink, EmbeddedResource.

    Raises ``ValueError`` for any unrecognised type — no silent pass-through.
    """
    if isinstance(block, t.TextContent):
        return {"type": "text", "text": block.text}

    if isinstance(block, t.ImageContent):
        return {"type": "image", "data": block.data, "mimeType": block.mime_type}

    if isinstance(block, t.AudioContent):
        return {"type": "audio", "data": block.data, "mimeType": block.mime_type}

    if isinstance(block, t.ResourceLink):
        d: dict[str, Any] = {"type": "resource_link", "uri": block.uri, "name": block.name}
        if block.description is not None:
            d["description"] = block.description
        if block.mime_type is not None:
            d["mimeType"] = block.mime_type
        return d

    if isinstance(block, t.EmbeddedResource):
        resource = block.resource
        if isinstance(resource, t.TextResourceContents):
            res_dict: dict[str, Any] = {
                "uri": resource.uri,
                "text": resource.text,
            }
            if resource.mime_type is not None:
                res_dict["mimeType"] = resource.mime_type
        elif isinstance(resource, t.BlobResourceContents):
            res_dict = {"uri": resource.uri, "blob": resource.blob}
            if resource.mime_type is not None:
                res_dict["mimeType"] = resource.mime_type
        else:
            raise ValueError(f"Unknown resource content type: {type(resource).__name__!r}")
        return {"type": "resource", "resource": res_dict}

    raise ValueError(
        f"Unknown content block type: {type(block).__name__!r}. "
        "Only TextContent, ImageContent, AudioContent, ResourceLink, and "
        "EmbeddedResource are supported."
    )


# ---------------------------------------------------------------------------
# Direction 3: SDK mcp.types → neutral
# ---------------------------------------------------------------------------

def sdk_call_tool_result_to_outcome(
    result: t.CallToolResult,
    server_name: str,
) -> CallToolOutcome:
    """Convert an SDK ``CallToolResult`` to a ``CallToolOutcome``.

    Each content block is converted via ``sdk_content_block_to_dict``, which
    raises ``ValueError`` for unknown types.
    """
    content = tuple(sdk_content_block_to_dict(b) for b in result.content)
    return CallToolOutcome(
        content=content,
        is_error=bool(result.is_error),
        server_name=server_name,
    )


# ---------------------------------------------------------------------------
# Direction 2: neutral → SDK mcp.types (for P03 inbound adapter)
# ---------------------------------------------------------------------------

def dict_to_sdk_content_block(d: dict[str, Any]) -> t.ContentBlock:
    """Convert a wire-format content-block dict to an SDK ``ContentBlock``.

    Raises ``ValueError`` for unknown type tags — no silent pass-through.
    """
    block_type = d.get("type")

    if block_type == "text":
        return t.TextContent(type="text", text=d["text"])

    if block_type == "image":
        return t.ImageContent(type="image", data=d["data"], mime_type=d["mimeType"])

    if block_type == "audio":
        return t.AudioContent(type="audio", data=d["data"], mime_type=d["mimeType"])

    if block_type == "resource_link":
        return t.ResourceLink(type="resource_link", uri=d["uri"], name=d["name"])

    if block_type == "resource":
        return _dict_to_embedded_resource(d["resource"])

    raise ValueError(
        f"Unknown content block type: {block_type!r}. "
        "Supported: 'text', 'image', 'audio', 'resource_link', 'resource'."
    )


def _dict_to_embedded_resource(resource_dict: dict[str, Any]) -> t.EmbeddedResource:
    """Build an ``EmbeddedResource`` from a wire-format resource dict.

    Raises ``ValueError`` when neither ``text`` nor ``blob`` is present.
    """
    uri = resource_dict.get("uri", "")
    mime = resource_dict.get("mimeType")

    if "text" in resource_dict:
        contents: t.TextResourceContents | t.BlobResourceContents = t.TextResourceContents(
            uri=uri, text=resource_dict["text"], mime_type=mime
        )
    elif "blob" in resource_dict:
        contents = t.BlobResourceContents(uri=uri, blob=resource_dict["blob"], mime_type=mime)
    else:
        raise ValueError(
            f"Unknown resource content — neither 'text' nor 'blob' key found in {resource_dict!r}."
        )
    return t.EmbeddedResource(type="resource", resource=contents)


def _dict_to_resource_contents(c: dict[str, Any]) -> t.TextResourceContents | t.BlobResourceContents:
    """Build a resource-contents object from a wire-format dict."""
    uri = c.get("uri", "")
    mime = c.get("mimeType")
    if "text" in c:
        return t.TextResourceContents(uri=uri, text=c["text"], mime_type=mime)
    if "blob" in c:
        return t.BlobResourceContents(uri=uri, blob=c["blob"], mime_type=mime)
    raise ValueError(
        f"Unknown resource content — neither 'text' nor 'blob' key found in {c!r}."
    )


def tools_list_to_sdk(outcome: ToolsListOutcome) -> t.ListToolsResult:
    """Convert a ``ToolsListOutcome`` to an SDK ``ListToolsResult``."""
    sdk_tools = [
        t.Tool(name=tool["name"], description=tool.get("description"), input_schema=tool.get("inputSchema", {}))
        for tool in outcome.tools
    ]
    return t.ListToolsResult(tools=sdk_tools)


def call_tool_outcome_to_sdk(outcome: CallToolOutcome) -> t.CallToolResult:
    """Convert a ``CallToolOutcome`` to an SDK ``CallToolResult``.

    Raises ``ValueError`` via ``dict_to_sdk_content_block`` for unknown types.
    """
    content_blocks = [dict_to_sdk_content_block(d) for d in outcome.content]
    return t.CallToolResult(content=content_blocks, is_error=outcome.is_error)


def resource_read_to_sdk(outcome: ResourceReadOutcome) -> t.ReadResourceResult:
    """Convert a ``ResourceReadOutcome`` to an SDK ``ReadResourceResult``.

    Parses the raw result dict from the router.  Raises ``ValueError`` for
    unknown resource content shapes.
    """
    contents = [
        _dict_to_resource_contents(c)
        for c in outcome.raw.get("contents", [])
    ]
    return t.ReadResourceResult(contents=contents)


def prompt_get_to_sdk(outcome: PromptGetOutcome) -> t.GetPromptResult:
    """Convert a ``PromptGetOutcome`` to an SDK ``GetPromptResult``.

    Parses the raw result dict from the router.  Raises ``ValueError`` via
    ``dict_to_sdk_content_block`` for unknown content types.
    """
    messages = []
    for msg in outcome.raw.get("messages", []):
        content_dict = msg.get("content", {})
        content_block = dict_to_sdk_content_block(content_dict)
        messages.append(t.PromptMessage(role=msg["role"], content=content_block))
    description = outcome.raw.get("description")
    return t.GetPromptResult(messages=messages, description=description)
