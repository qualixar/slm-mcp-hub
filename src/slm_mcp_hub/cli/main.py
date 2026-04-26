"""CLI entry point for SLM MCP Hub."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

import click

from slm_mcp_hub.core.config import (
    generate_default_config,
    import_claude_config,
    import_vscode_config,
    load_config,
    save_config,
)
from slm_mcp_hub.core.constants import CONFIG_FILE, PID_FILE, VERSION
from slm_mcp_hub.core.hub import HubOrchestrator
from slm_mcp_hub.cli.setup_commands import network, setup


SECRETS_PATHS = (
    Path.home() / ".claude-secrets.env",
    Path.home() / ".slm-mcp-hub" / "secrets.env",
)


def _load_secrets() -> None:
    """Load environment variables from secrets files.

    Searches ~/.claude-secrets.env (shared with Claude Code) and
    ~/.slm-mcp-hub/secrets.env (hub-specific). This ensures ${VAR}
    placeholders in MCP configs resolve to the same values Claude uses.
    """
    for secrets_path in SECRETS_PATHS:
        if not secrets_path.exists():
            continue
        try:
            with open(secrets_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        key = key.strip()
                        val = val.strip()
                        if key and key not in os.environ:
                            os.environ[key] = val
            logging.getLogger(__name__).info("Loaded secrets from %s", secrets_path)
        except OSError:
            pass


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.version_option(VERSION, prog_name="slm-mcp-hub")
def cli() -> None:
    """SLM MCP Hub — The World's First MCP Gateway That Learns."""


def _kill_existing_hub(config_host: str, config_port: int) -> None:
    """Kill any existing hub process — PID file + port check. Prevents zombies."""
    import signal
    import socket
    import time
    from slm_mcp_hub.resilience.watchdog import is_running, read_pid_file, remove_pid_file

    if is_running():
        old_pid = read_pid_file()
        click.echo(f"  Killing existing hub (PID {old_pid})...")
        try:
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.25)
                try:
                    os.kill(old_pid, 0)
                except ProcessLookupError:
                    break
            else:
                os.kill(old_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    remove_pid_file()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sock.connect_ex((config_host, config_port)) == 0:
            import subprocess
            result = subprocess.run(
                ["lsof", "-ti", f":{config_port}"],
                capture_output=True, text=True,
            )
            for pid_str in result.stdout.strip().split("\n"):
                if pid_str.strip():
                    orphan_pid = int(pid_str.strip())
                    if orphan_pid != os.getpid():
                        click.echo(f"  Killing orphan on port {config_port} (PID {orphan_pid})...")
                        try:
                            os.kill(orphan_pid, signal.SIGTERM)
                            time.sleep(1)
                            os.kill(orphan_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
    finally:
        sock.close()


@cli.command()
@click.option("--port", type=int, default=None, help="Port to listen on")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
def start(port: int | None, config_path: Path | None, log_level: str) -> None:
    """Start the hub server. Kills any existing hub first."""
    _setup_logging(log_level)
    _load_secrets()
    config = load_config(config_path)

    if port:
        config = replace(config, port=port)

    _kill_existing_hub(config.host, config.port)

    async def _run() -> None:
        import uvicorn

        from slm_mcp_hub.core.registry import CapabilityRegistry
        from slm_mcp_hub.federation.manager import ConnectionManager
        from slm_mcp_hub.federation.router import FederationRouter
        from slm_mcp_hub.server.http_server import create_app
        from slm_mcp_hub.server.mcp_endpoint import MCPEndpoint
        from slm_mcp_hub.server.proxy_endpoint import ProxyEndpoint
        from slm_mcp_hub.session.manager import SessionManager

        async with HubOrchestrator(config) as hub:
            registry = CapabilityRegistry()
            conn_manager = ConnectionManager(config, registry)

            router = FederationRouter(registry, conn_manager.connections)
            session_manager = SessionManager(
                max_sessions=config.max_sessions,
                timeout_seconds=config.session_timeout_seconds,
            )
            mcp_endpoint = MCPEndpoint(registry, router, session_manager, hub=hub)
            proxy = ProxyEndpoint(conn_manager, hub=hub)

            app = create_app(
                mcp_endpoint=mcp_endpoint,
                session_manager=session_manager,
                cors_origins=config.cors_origins,
                hub_status_fn=hub.get_status,
                proxy_endpoint=proxy,
                registry=registry,
            )

            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(os.getpid()))

            uvi_config = uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                log_level=config.log_level.lower(),
                timeout_keep_alive=300,
                timeout_graceful_shutdown=30,
            )
            server = uvicorn.Server(uvi_config)

            click.echo(f"SLM MCP Hub v{VERSION} on http://{config.host}:{config.port}/mcp")
            click.echo(f"  Configured: {len(config.mcp_servers)} MCP servers, {len(hub.plugins)} plugins")

            async def _connect_mcps_background() -> None:
                failed = await conn_manager.connect_all()
                click.echo(f"  Connected: {conn_manager.connected_count}/{len(config.mcp_servers)} servers, {registry.tool_count} tools")
                if failed:
                    for name, err in failed.items():
                        click.echo(f"  WARNING: {name}: {err}")

            asyncio.create_task(_connect_mcps_background())

            try:
                await server.serve()
            except asyncio.CancelledError:
                pass
            finally:
                await conn_manager.disconnect_all()
                if PID_FILE.exists():
                    PID_FILE.unlink()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nHub stopped.")


@cli.command()
def status() -> None:
    """Show hub status with actual process health check."""
    from slm_mcp_hub.resilience.watchdog import is_running, read_pid_file

    if is_running():
        pid = read_pid_file()
        config = load_config()

        import httpx
        try:
            resp = httpx.get(
                f"http://{config.host}:{config.port}/api/health",
                timeout=5.0,
            )
            health = resp.json()
            click.echo(f"Hub is running (PID {pid})")
            click.echo(f"  Version: {health.get('version', 'unknown')}")
            click.echo(f"  Port: {config.port}")
            click.echo(f"  State: {health.get('state', 'unknown')}")
            click.echo(f"  Uptime: {health.get('uptime_seconds', 0):.0f}s")
            click.echo(f"  MCP servers: {health.get('mcp_servers_configured', '?')}")
            click.echo(f"  Config: {CONFIG_FILE}")
        except httpx.ConnectError:
            click.echo(f"Hub PID {pid} exists but HTTP endpoint is unreachable")
            click.echo(f"  Port {config.port} not responding — hub may have crashed")
            click.echo(f"  Restart with: slm-hub start")
    else:
        click.echo("Hub is not running")
        click.echo(f"  Start with: slm-hub start")
        click.echo(f"  Install daemon: slm-hub daemon install")


@cli.command()
@click.argument("server_name")
def reconnect(server_name: str) -> None:
    """Reconnect a failed or disconnected MCP server."""
    import httpx

    try:
        config = load_config()
        resp = httpx.post(
            f"http://{config.host}:{config.port}/api/servers/{server_name}/reconnect",
            timeout=60.0,
        )
        data = resp.json()
        if data.get("success"):
            click.echo(f"Reconnected: {server_name} ({data.get('message', '')})")
        else:
            click.echo(f"Failed: {data.get('message', 'unknown error')}")
    except httpx.ConnectError:
        click.echo("Hub is not running. Start with: slm-hub start")
    except Exception as exc:
        click.echo(f"Error: {exc}")


@cli.group()
def config() -> None:
    """Configuration management."""


@config.command("show")
def config_show() -> None:
    """Display current configuration."""
    _setup_logging("WARNING")
    cfg = load_config()
    click.echo(f"Host: {cfg.host}")
    click.echo(f"Port: {cfg.port}")
    click.echo(f"Config dir: {cfg.config_dir}")
    click.echo(f"Log level: {cfg.log_level}")
    click.echo(f"Session timeout: {cfg.session_timeout_seconds}s")
    click.echo(f"Max sessions: {cfg.max_sessions}")
    click.echo(f"Cache TTL: {cfg.cache_ttl_seconds}s")
    click.echo(f"Idle shutdown: {cfg.idle_shutdown_seconds}s")
    click.echo(f"\nMCP Servers ({len(cfg.mcp_servers)}):")
    for srv in cfg.mcp_servers:
        status = "enabled" if srv.enabled else "disabled"
        if srv.transport == "stdio":
            click.echo(f"  {srv.name} [{srv.transport}] {srv.command} {' '.join(srv.args)} ({status})")
        else:
            click.echo(f"  {srv.name} [{srv.transport}] {srv.url} ({status})")


@config.command("import")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("--format", "fmt", type=click.Choice(["auto", "claude", "vscode"]), default="auto")
def config_import(file_path: Path, fmt: str) -> None:
    """Import MCP server definitions from Claude Code or VS Code config."""
    _setup_logging("WARNING")

    if fmt == "auto":
        content = file_path.read_text()
        if "mcpServers" in content:
            fmt = "claude"
        elif "servers" in content or "mcp.servers" in content:
            fmt = "vscode"
        else:
            click.echo("Could not auto-detect format. Use --format claude or --format vscode")
            sys.exit(1)

    if fmt == "claude":
        servers = import_claude_config(file_path)
    else:
        servers = import_vscode_config(file_path)

    click.echo(f"Found {len(servers)} MCP servers in {file_path}")

    # Load existing config or create default
    existing = load_config()
    existing_names = {s.name for s in existing.mcp_servers}

    new_servers = [s for s in servers if s.name not in existing_names]
    if not new_servers:
        click.echo("All servers already in config. Nothing to import.")
        return

    # Merge: existing + new
    merged = list(existing.mcp_servers) + new_servers
    updated = replace(existing, mcp_servers=tuple(merged))
    save_config(updated)
    click.echo(f"Imported {len(new_servers)} new servers. Total: {len(merged)}")


@config.command("init")
def config_init() -> None:
    """Generate default configuration file."""
    _setup_logging("WARNING")
    if CONFIG_FILE.exists():
        click.echo(f"Config already exists at {CONFIG_FILE}")
        if not click.confirm("Overwrite?"):
            return
    generate_default_config()
    click.echo(f"Default config created at {CONFIG_FILE}")


@config.command("snapshots")
def config_snapshots() -> None:
    """List all config snapshots (auto-saved before every change)."""
    from slm_mcp_hub.core.config import list_snapshots, SNAPSHOTS_DIR
    snaps = list_snapshots()
    if not snaps:
        click.echo(f"No snapshots in {SNAPSHOTS_DIR}")
        return
    click.echo(f"Snapshots in {SNAPSHOTS_DIR} (newest first):")
    for s in snaps:
        click.echo(f"  {s['name']:<35} {s['mcp_count']:>3} MCPs  {s['size']:>6} bytes")
    click.echo(f"\nRestore with: slm-hub config restore <snapshot-name>")


@config.command("restore")
@click.argument("snapshot_name")
def config_restore(snapshot_name: str) -> None:
    """Restore a config snapshot. Current config is auto-snapshotted first."""
    from slm_mcp_hub.core.config import restore_snapshot
    try:
        target = restore_snapshot(snapshot_name)
        click.echo(f"Restored {snapshot_name} -> {target}")
        click.echo("Restart the hub for the change to take effect:")
        click.echo("  launchctl kickstart -k gui/$(id -u)/com.qualixar.slm-mcp-hub")
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}")
        click.echo(f"List available with: slm-hub config snapshots")


cli.add_command(setup)
cli.add_command(network)


@cli.group()
def daemon() -> None:
    """Process supervision — auto-restart on crash."""


@daemon.command("install")
@click.option("--port", type=int, default=None, help="Port for the hub (default from config)")
def daemon_install(port: int | None) -> None:
    """Install launchd plist (macOS) for auto-restart on crash/boot."""
    import platform
    if platform.system() != "Darwin":
        click.echo("Launchd is macOS-only. Use systemd on Linux:")
        from slm_mcp_hub.resilience.watchdog import generate_systemd_unit
        cfg = load_config()
        click.echo(generate_systemd_unit(port or cfg.port))
        return

    from slm_mcp_hub.resilience.watchdog import install_launchd, LAUNCHD_LABEL
    import subprocess

    cfg = load_config()
    plist_path = install_launchd(port or cfg.port)
    click.echo(f"Plist written: {plist_path}")

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True)

    if result.returncode == 0:
        click.echo(f"Daemon installed and loaded: {LAUNCHD_LABEL}")
        click.echo("Hub will auto-start on boot and restart on crash.")
    else:
        click.echo(f"launchctl load failed: {result.stderr}")
        click.echo(f"Manual load: launchctl load -w {plist_path}")


@daemon.command("uninstall")
def daemon_uninstall() -> None:
    """Remove launchd plist and stop the daemon."""
    from slm_mcp_hub.resilience.watchdog import LAUNCHD_LABEL
    import subprocess

    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if not plist_path.exists():
        click.echo("Daemon not installed.")
        return

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink()
    click.echo(f"Daemon uninstalled: {LAUNCHD_LABEL}")


@daemon.command("status")
def daemon_status() -> None:
    """Check if the daemon is installed and running."""
    from slm_mcp_hub.resilience.watchdog import LAUNCHD_LABEL, is_running, read_pid_file
    import subprocess

    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    installed = plist_path.exists()

    result = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True, text=True,
    )
    loaded = result.returncode == 0

    running = is_running()
    pid = read_pid_file()

    click.echo(f"Plist installed: {'yes' if installed else 'no'}")
    click.echo(f"Launchd loaded:  {'yes' if loaded else 'no'}")
    click.echo(f"Process running: {'yes' if running else 'no'}{f' (PID {pid})' if pid else ''}")

    if not installed:
        click.echo("\nInstall with: slm-hub daemon install")
    elif not loaded:
        click.echo(f"\nLoad with: launchctl load -w {plist_path}")


@cli.command("tools")
@click.option("--query", "-q", default="", help="Search tools by keyword")
def tools_cmd(query: str) -> None:
    """List available tools from the running hub."""
    import httpx

    config = load_config()
    try:
        if query:
            resp = httpx.post(
                f"http://{config.host}:{config.port}/mcp",
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "tools/call",
                    "params": {"name": "hub__search_tools", "arguments": {"query": query}},
                },
                headers={"Content-Type": "application/json"},
                timeout=30.0,
            )
        else:
            resp = httpx.post(
                f"http://{config.host}:{config.port}/mcp",
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "tools/call",
                    "params": {"name": "hub__list_servers", "arguments": {}},
                },
                headers={"Content-Type": "application/json"},
                timeout=30.0,
            )

        data = resp.json()
        result = data.get("result", {})
        content = result.get("content", [])
        for block in content:
            if block.get("type") == "text":
                click.echo(block["text"])
    except httpx.ConnectError:
        click.echo("Hub is not running. Start with: slm-hub start")
    except Exception as exc:
        click.echo(f"Error: {exc}")


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
