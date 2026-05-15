"""CLI: `slm-hub server *` — hot-reload commands for managing MCP servers
without restarting the hub.

Design: config.json is the single source of truth. These commands edit
the file using the existing atomic save_config(), then POST /api/reload
to the running hub so it diffs disk vs in-memory and applies the changes.
"""

from __future__ import annotations

import sys
from dataclasses import replace as dc_replace

import click

from slm_mcp_hub.core.config import (
    MCPServerConfig,
    load_config,
    save_config,
)


def _hub_url() -> str:
    cfg = load_config()
    return f"http://{cfg.host}:{cfg.port}"


def _post_reload() -> dict:
    """POST /api/reload to the running hub. Returns the JSON response."""
    import httpx
    try:
        resp = httpx.post(f"{_hub_url()}/api/reload", timeout=120.0)
        return resp.json()
    except httpx.ConnectError:
        return {"success": False, "error": "Hub is not running"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _get_status_detail() -> dict:
    """GET /api/servers/detail from the running hub."""
    import httpx
    try:
        resp = httpx.get(f"{_hub_url()}/api/servers/detail", timeout=10.0)
        return resp.json()
    except httpx.ConnectError:
        return {"servers": None, "error": "Hub is not running"}
    except Exception as exc:
        return {"servers": None, "error": str(exc)}


def _parse_env_args(env_pairs: tuple[str, ...]) -> dict[str, str]:
    """Parse --env KEY=VALUE pairs into a dict."""
    out: dict[str, str] = {}
    for pair in env_pairs:
        if "=" not in pair:
            raise click.BadParameter(f"--env expects KEY=VALUE, got '{pair}'")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@click.group()
def server() -> None:
    """Manage federated MCP servers — hot-add, hot-remove, hot-modify."""


@server.command("list")
@click.option("--show-tools", is_flag=True, help="Show tool count per server")
def server_list(show_tools: bool) -> None:
    """List configured MCP servers and their live connection status."""
    data = _get_status_detail()
    servers = data.get("servers")

    if servers is None:
        # Hub not running — show disk config only
        click.echo(f"Hub not running ({data.get('error', 'unreachable')}); showing disk config:")
        cfg = load_config()
        for srv in cfg.mcp_servers:
            status = "enabled" if srv.enabled else "disabled"
            click.echo(f"  {srv.name:<28} [{srv.transport:<5}] {status}")
        return

    if not servers:
        click.echo("No servers configured.")
        return

    headers = f"{'NAME':<28} {'TRANSPORT':<8} {'STATUS':<12}"
    if show_tools:
        headers += f" {'TOOLS':>6}"
    click.echo(headers)
    click.echo("-" * (len(headers) + 4))
    for entry in servers:
        name = entry["name"]
        transport = entry["transport"]
        enabled = entry["enabled"]
        connected = entry["connected"]
        if not enabled:
            status = "disabled"
        elif connected:
            status = "connected"
        elif entry.get("error"):
            status = "failed"
        else:
            status = "pending"
        line = f"{name:<28} {transport:<8} {status:<12}"
        if show_tools:
            line += f" {entry['tools']:>6}"
        click.echo(line)
        if entry.get("error") and not connected:
            click.echo(f"  └─ {entry['error']}")


@server.command("add")
@click.argument("name")
@click.option("--command", help="Stdio: command to run")
@click.option("--arg", "args", multiple=True, help="Stdio: command arg (repeatable)")
@click.option("--env", "envs", multiple=True, help="KEY=VALUE env var (repeatable)")
@click.option("--type", "transport", type=click.Choice(["stdio", "http", "sse"]), default="stdio")
@click.option("--url", default="", help="HTTP/SSE: server URL")
@click.option("--header", "headers", multiple=True, help="HTTP/SSE: KEY=VALUE header (repeatable)")
@click.option("--disabled", is_flag=True, help="Add but leave disabled")
@click.option("--no-reload", is_flag=True, help="Edit disk only, skip hot-reload")
def server_add(
    name: str,
    command: str | None,
    args: tuple[str, ...],
    envs: tuple[str, ...],
    transport: str,
    url: str,
    headers: tuple[str, ...],
    disabled: bool,
    no_reload: bool,
) -> None:
    """Add a new MCP server. Hot-reloaded by default."""
    cfg = load_config()
    if any(s.name == name for s in cfg.mcp_servers):
        click.echo(f"Error: server '{name}' already exists. Use 'server modify' to change it.")
        sys.exit(1)

    if transport == "stdio":
        if not command:
            click.echo("Error: stdio transport requires --command")
            sys.exit(1)
    else:
        if not url:
            click.echo(f"Error: {transport} transport requires --url")
            sys.exit(1)

    new_srv = MCPServerConfig(
        name=name,
        transport=transport,
        command=command or "",
        args=tuple(args),
        env=_parse_env_args(envs),
        url=url,
        headers=_parse_env_args(headers),
        enabled=not disabled,
    )

    new_servers = tuple(cfg.mcp_servers) + (new_srv,)
    updated = dc_replace(cfg, mcp_servers=new_servers)
    save_config(updated)
    click.echo(f"Added '{name}' to config.")

    if no_reload or disabled:
        click.echo("Skipped hot-reload. Use 'slm-hub server reload' to apply.")
        return

    result = _post_reload()
    if result.get("success"):
        click.echo(f"Hot-reload applied: {result.get('summary', '')}")
    else:
        click.echo(f"Hot-reload failed: {result.get('error', 'unknown')}")


@server.command("remove")
@click.argument("name")
@click.option("--no-reload", is_flag=True, help="Edit disk only, skip hot-reload")
def server_remove(name: str, no_reload: bool) -> None:
    """Remove an MCP server. Hot-removes (drains in-flight) by default."""
    cfg = load_config()
    if not any(s.name == name for s in cfg.mcp_servers):
        click.echo(f"Error: server '{name}' not found.")
        sys.exit(1)

    new_servers = tuple(s for s in cfg.mcp_servers if s.name != name)
    updated = dc_replace(cfg, mcp_servers=new_servers)
    save_config(updated)
    click.echo(f"Removed '{name}' from config.")

    if no_reload:
        return

    result = _post_reload()
    if result.get("success"):
        click.echo(f"Hot-reload applied: {result.get('summary', '')}")
    else:
        click.echo(f"Hot-reload failed: {result.get('error', 'unknown')}")


@server.command("modify")
@click.argument("name")
@click.option("--enabled/--disabled", "enabled", default=None, help="Enable or disable")
@click.option("--env", "envs", multiple=True, help="KEY=VALUE env var (replaces all envs if any given)")
@click.option("--arg", "args", multiple=True, help="Replace args (repeatable)")
@click.option("--command", help="Replace command (stdio only)")
@click.option("--url", help="Replace URL (http/sse only)")
@click.option("--no-reload", is_flag=True, help="Edit disk only, skip hot-reload")
def server_modify(
    name: str,
    enabled: bool | None,
    envs: tuple[str, ...],
    args: tuple[str, ...],
    command: str | None,
    url: str | None,
    no_reload: bool,
) -> None:
    """Modify an existing MCP server. Drains in-flight then restarts."""
    cfg = load_config()
    target = next((s for s in cfg.mcp_servers if s.name == name), None)
    if target is None:
        click.echo(f"Error: server '{name}' not found.")
        sys.exit(1)

    updates: dict = {}
    if enabled is not None:
        updates["enabled"] = enabled
    if envs:
        updates["env"] = _parse_env_args(envs)
    if args:
        updates["args"] = tuple(args)
    if command is not None:
        updates["command"] = command
    if url is not None:
        updates["url"] = url

    if not updates:
        click.echo("No changes specified. Use --enabled/--disabled, --env, --arg, --command, or --url.")
        sys.exit(1)

    modified = dc_replace(target, **updates)
    new_servers = tuple(modified if s.name == name else s for s in cfg.mcp_servers)
    updated_cfg = dc_replace(cfg, mcp_servers=new_servers)
    save_config(updated_cfg)
    click.echo(f"Modified '{name}' in config.")

    if no_reload:
        return

    result = _post_reload()
    if result.get("success"):
        click.echo(f"Hot-reload applied: {result.get('summary', '')}")
    else:
        click.echo(f"Hot-reload failed: {result.get('error', 'unknown')}")


@server.command("reload")
def server_reload() -> None:
    """Re-read config.json from disk and apply the diff."""
    result = _post_reload()
    if result.get("success"):
        click.echo(f"Reload applied: {result.get('summary', '')}")
        if result.get("added"):
            click.echo(f"  + {', '.join(result['added'])}")
        if result.get("modified"):
            click.echo(f"  ~ {', '.join(result['modified'])}")
        if result.get("removed"):
            click.echo(f"  - {', '.join(result['removed'])}")
    else:
        click.echo(f"Reload failed: {result.get('error', 'unknown')}")
        sys.exit(1)


@server.command("status")
@click.argument("name", required=False)
def server_status(name: str | None) -> None:
    """Show detailed status of one or all servers."""
    data = _get_status_detail()
    servers = data.get("servers")
    if servers is None:
        click.echo(f"Hub not running ({data.get('error', 'unreachable')})")
        sys.exit(1)

    if name:
        target = next((s for s in servers if s["name"] == name), None)
        if target is None:
            click.echo(f"Server '{name}' not found.")
            sys.exit(1)
        for k, v in target.items():
            click.echo(f"  {k}: {v}")
    else:
        for s in servers:
            click.echo(f"{s['name']}: connected={s['connected']} tools={s['tools']} transport={s['transport']}")
