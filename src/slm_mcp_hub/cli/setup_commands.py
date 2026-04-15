"""Setup and network discovery CLI commands for SLM MCP Hub."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from slm_mcp_hub.core.constants import DEFAULT_PORT
from slm_mcp_hub.discovery.auto_register import AutoRegister, RegistrationPlan
from slm_mcp_hub.discovery.client_detector import ClientDetector
from slm_mcp_hub.discovery.network import (
    SERVICE_TYPE,
    NetworkDiscovery,
    is_zeroconf_available,
)


@click.group()
def setup() -> None:
    """Setup wizard for AI client integration."""


@setup.command("detect")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def setup_detect(as_json: bool) -> None:
    """Detect installed AI clients."""
    detector = ClientDetector()
    clients = detector.detect_all()

    if as_json:
        data = [
            {
                "name": c.name,
                "display_name": c.display_name,
                "config_path": str(c.config_path),
                "mcp_count": c.mcp_count,
                "hub_registered": c.hub_registered,
            }
            for c in clients
        ]
        click.echo(json.dumps(data, indent=2))
        return

    if not clients:
        click.echo("No AI clients detected.")
        return

    click.echo(f"Detected {len(clients)} AI client(s):\n")
    click.echo(f"  {'Client':<22} {'MCPs':>5}  {'Hub?':>5}  Config Path")
    click.echo(f"  {'─' * 22} {'─' * 5}  {'─' * 5}  {'─' * 40}")
    for c in clients:
        hub_flag = "Yes" if c.hub_registered else "No"
        click.echo(f"  {c.display_name:<22} {c.mcp_count:>5}  {hub_flag:>5}  {c.config_path}")


@setup.command("register")
@click.option("--client", "client_name", default=None, help="Register a specific client")
@click.option("--url", "hub_url", default=None, help="Hub URL to register")
@click.option("--dry-run", is_flag=True, help="Show what would change without modifying")
@click.option("--all", "register_all", is_flag=True, help="Register with all detected clients")
def setup_register(
    client_name: str | None,
    hub_url: str | None,
    dry_run: bool,
    register_all: bool,
) -> None:
    """Register the hub with AI clients."""
    detector = ClientDetector()
    clients = detector.detect_all()

    if not clients:
        click.echo("No AI clients detected. Nothing to register.")
        return

    registrar = AutoRegister(hub_url)
    targets = clients if register_all else tuple(
        c for c in clients if c.name == client_name
    )

    if not targets:
        click.echo(f"Client '{client_name}' not detected. Use 'slm-hub setup detect' to list.")
        return

    for client in targets:
        if dry_run:
            plan = registrar.plan(client)
            _display_plan(plan)
        else:
            result = registrar.register(
                client,
                mcp_key=_mcp_key_for(client),
                dry_run=False,
            )
            if hasattr(result, "success"):
                if result.success:
                    if result.error == "already_registered":
                        click.echo(f"  {client.display_name}: already registered")
                    else:
                        click.echo(f"  {client.display_name}: registered (backup: {result.backup_path})")
                else:
                    click.echo(f"  {client.display_name}: FAILED — {result.error}")


@setup.command("unregister")
@click.option("--client", "client_name", default=None, help="Unregister a specific client")
@click.option("--all", "unregister_all", is_flag=True, help="Unregister from all clients")
def setup_unregister(client_name: str | None, unregister_all: bool) -> None:
    """Remove hub from AI client configurations."""
    detector = ClientDetector()
    clients = detector.detect_all()
    registrar = AutoRegister()

    targets = clients if unregister_all else tuple(
        c for c in clients if c.name == client_name
    )

    for client in targets:
        result = registrar.unregister(client, mcp_key=_mcp_key_for(client))
        if result.success:
            if result.error == "not_registered":
                click.echo(f"  {client.display_name}: not registered")
            else:
                click.echo(f"  {client.display_name}: unregistered")
        else:
            click.echo(f"  {client.display_name}: FAILED — {result.error}")


@setup.command("import")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("--format", "fmt", type=click.Choice(["auto", "claude", "vscode"]), default="auto")
def setup_import(file_path: Path, fmt: str) -> None:
    """Import MCP servers from a client config file into the hub."""
    if fmt == "auto":
        content = file_path.read_text()
        if "mcpServers" in content:
            fmt = "claude"
        elif "servers" in content:
            fmt = "vscode"
        else:
            click.echo("Could not auto-detect format. Use --format claude or --format vscode")
            sys.exit(1)

    registrar = AutoRegister()
    result = registrar.import_mcps(file_path, config_format=fmt)

    click.echo(f"Source: {result.source_name} ({result.total_in_source} servers)")
    click.echo(f"Imported: {result.imported_count}")
    click.echo(f"Skipped (duplicates): {result.skipped_count}")


def _display_plan(plan: RegistrationPlan) -> None:
    """Display a registration plan."""
    if plan.already_registered:
        click.echo(f"  {plan.client_name}: already registered (no changes needed)")
        return
    click.echo(f"  {plan.client_name}:")
    click.echo(f"    Config: {plan.config_path}")
    click.echo(f"    Backup: {plan.backup_path}")
    click.echo(f"    Add entry: {json.dumps(plan.hub_entry)}")


def _mcp_key_for(client: DetectedClient) -> str:
    """Return the MCP config key for a client's format."""
    if client.config_format == "vscode":
        return "mcp.servers"
    return "mcpServers"


@click.group()
def network() -> None:
    """Network discovery commands."""


@network.command("discover")
@click.option("--timeout", type=float, default=3.0, help="Discovery timeout in seconds")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def network_discover(timeout: float, as_json: bool) -> None:
    """Discover hub instances on the local network."""
    if not is_zeroconf_available():
        click.echo("Zeroconf not installed. Install with: pip install slm-mcp-hub[network]")
        return

    discovery = NetworkDiscovery()
    hubs = discovery.discover(timeout_seconds=timeout)

    if as_json:
        data = [
            {
                "host": h.host,
                "port": h.port,
                "version": h.version,
                "mcp_count": h.mcp_count,
                "hostname": h.hostname,
                "address": h.address,
            }
            for h in hubs
        ]
        click.echo(json.dumps(data, indent=2))
        return

    if not hubs:
        click.echo("No hubs found on the local network.")
        return

    click.echo(f"Found {len(hubs)} hub(s):\n")
    for h in hubs:
        click.echo(f"  {h.hostname} @ {h.address}:{h.port} (v{h.version}, {h.mcp_count} MCPs)")


@network.command("info")
def network_info() -> None:
    """Show this hub's network identity."""
    import socket as _socket

    hostname = _socket.gethostname()
    click.echo(f"Hostname: {hostname}")
    click.echo(f"Zeroconf: {'available' if is_zeroconf_available() else 'not installed'}")
    click.echo(f"Service type: {SERVICE_TYPE}")
