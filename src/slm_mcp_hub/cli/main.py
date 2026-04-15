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


@cli.command()
@click.option("--port", type=int, default=None, help="Port to listen on")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
def start(port: int | None, config_path: Path | None, log_level: str) -> None:
    """Start the hub server."""
    _setup_logging(log_level)
    _load_secrets()
    config = load_config(config_path)

    if port:
        # Create new config with overridden port (immutable)
        config = replace(config, port=port)

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
            # Wire subsystems
            registry = CapabilityRegistry()
            conn_manager = ConnectionManager(config, registry)

            # Connect to all configured MCP servers
            failed = await conn_manager.connect_all()

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
            )

            click.echo(f"SLM MCP Hub v{VERSION} running on http://{config.host}:{config.port}/mcp")
            click.echo(f"  MCP servers: {conn_manager.connected_count}/{len(config.mcp_servers)} connected")
            click.echo(f"  Tools: {registry.tool_count}")
            click.echo(f"  Plugins: {len(hub.plugins)}")
            if failed:  # pragma: no cover — only when MCP servers fail to start
                for name, err in failed.items():
                    click.echo(f"  WARNING: {name} failed: {err}")
            click.echo("Press Ctrl+C to stop.")

            # Write PID file
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(os.getpid()))

            # Start uvicorn
            uvi_config = uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                log_level=config.log_level.lower(),
            )
            server = uvicorn.Server(uvi_config)

            try:
                await server.serve()  # pragma: no cover — blocking server loop
            except asyncio.CancelledError:  # pragma: no cover
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
    """Show hub status."""
    if PID_FILE.exists():
        click.echo("Hub is running")
        config = load_config()
        click.echo(f"  Port: {config.port}")
        click.echo(f"  MCP servers configured: {len(config.mcp_servers)}")
        click.echo(f"  Config: {CONFIG_FILE}")
    else:
        click.echo("Hub is not running")
        click.echo(f"  Start with: slm-hub start")


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


cli.add_command(setup)
cli.add_command(network)


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
