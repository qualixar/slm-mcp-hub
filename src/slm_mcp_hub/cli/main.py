"""CLI entry point for SLM MCP Hub."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

import click

from slm_mcp_hub.cli.api_client import hub_headers
from slm_mcp_hub.cli.auth_commands import auth as auth_group
from slm_mcp_hub.cli.server_commands import server as server_group
from slm_mcp_hub.cli.setup_commands import network, setup
from slm_mcp_hub.core.config import (
    generate_default_config,
    import_claude_config,
    import_vscode_config,
    load_config,
    save_config,
)
from slm_mcp_hub.core.constants import (
    VERSION,
    get_config_file,
    get_pid_file,
    get_snapshots_dir,
)
from slm_mcp_hub.core.hub import HubOrchestrator

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


def _setup_logging(level: str, *, stderr_only: bool = False) -> None:
    """Configure logging. When stderr_only=True (stdio transport mode), all
    log output is forced to stderr so stdout stays NDJSON-only."""
    stream = sys.stderr if stderr_only else None
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=stream,
    )


@click.group()
@click.version_option(VERSION, prog_name="slm-mcp-hub")
def cli() -> None:
    """SLM MCP Hub — Local-first MCP gateway for federated connections."""


def _kill_existing_hub(config_host: str, config_port: int) -> None:
    """Kill any existing hub process — PID file + port check. Prevents zombies."""
    import signal
    import socket
    import time

    from slm_mcp_hub.resilience.watchdog import (
        is_running,
        read_pid_file,
        remove_pid_file,
    )

    if is_running():
        old_pid = read_pid_file()
        if old_pid is not None:
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
@click.option("--host", default=None, help="Host/IP to bind (overrides config)")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
@click.option(
    "--sdk-mode",
    is_flag=True,
    default=False,
    envvar="SLM_HUB_SDK_MODE",
    help=(
        "Use the official MCP SDK inbound transport (mcp.server.lowlevel.Server) "
        "instead of the hand-rolled JSON-RPC handler. "
        "Enables MCP 2026-07-28 Streamable HTTP conformance. "
        "Also honoured via SLM_HUB_SDK_MODE=1 env var."
    ),
)
def start(
    port: int | None,
    host: str | None,
    config_path: Path | None,
    log_level: str,
    sdk_mode: bool,
) -> None:
    """Start the hub server. Kills any existing hub first."""
    _setup_logging(log_level)
    _load_secrets()
    try:
        config = load_config(config_path)
    except json.JSONDecodeError as exc:
        source = config_path or get_config_file()
        raise click.ClickException(
            f"Invalid JSON in {source} at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc

    # Allow env var or CLI flag to enable SDK mode
    if not sdk_mode:
        sdk_mode = os.environ.get("SLM_HUB_SDK_MODE", "").lower() in {"1", "true", "yes", "on"}

    if port:
        config = replace(config, port=port)
    if host:
        config = replace(config, host=host)

    try:
        loopback = ipaddress.ip_address(config.host).is_loopback
    except ValueError:
        loopback = config.host.lower() == "localhost"
    hub_api_key = os.environ.get("SLM_HUB_API_KEY")
    if not loopback and not hub_api_key:
        raise click.ClickException(
            "Remote binding requires SLM_HUB_API_KEY; refusing unauthenticated exposure"
        )

    _kill_existing_hub(config.host, config.port)

    async def _run() -> None:
        import uvicorn

        from slm_mcp_hub.lifecycle.runtime import HubRuntime
        from slm_mcp_hub.server.http_server import create_app

        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)

            # P03: SDK mode wires the official mcp.server.lowlevel.Server as the
            # inbound transport, enabling MCP 2026-07-28 Streamable HTTP conformance.
            sdk_server_instance = None
            if sdk_mode:
                from slm_mcp_hub.protocol.inbound import build_sdk_server
                from slm_mcp_hub.protocol.product_operations import HubProductOperations

                ops = HubProductOperations(
                    registry=runtime.registry,
                    router=runtime.router,
                    hub=hub,
                )
                sdk_server_instance = build_sdk_server(ops)
                click.echo("  SDK mode: MCP 2026-07-28 inbound transport active")

            app = create_app(
                mcp_endpoint=runtime.mcp_endpoint,
                session_manager=runtime.session_manager,
                cors_origins=config.cors_origins,
                hub_status_fn=hub.get_status,
                proxy_endpoint=runtime.proxy,
                registry=runtime.registry,
                reloader=runtime.reloader,
                conn_manager=runtime.conn_manager,
                api_key=hub_api_key,
                sdk_server=sdk_server_instance,
                metrics=runtime.metrics,  # W8-P5: wire MetricsCollector to dashboard
            )

            pid_file = get_pid_file()
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(os.getpid()))

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

            # W2-P2: post_connect hook handles retry logic and status output after
            # connect_all completes in the background.  The hub serves immediately.
            async def _post_connect(failed: dict[str, str]) -> None:
                click.echo(
                    f"  Connected: {runtime.conn_manager.connected_count}/"
                    f"{len(config.mcp_servers)} servers, "
                    f"{runtime.registry.tool_count} tools"
                )
                # Fast cold-start retries (0.5s, 1.5s, 4.5s) for transient failures
                # like child processes that need extra startup time.
                if failed:
                    still_failed = await runtime.conn_manager.fast_retry_failed()
                    if still_failed:
                        for name, err in still_failed.items():
                            click.echo(f"  WARNING: {name}: {err}")
                    else:
                        click.echo(
                            f"  Connected after retries: "
                            f"{runtime.conn_manager.connected_count}/"
                            f"{len(config.mcp_servers)} servers, "
                            f"{runtime.registry.tool_count} tools"
                        )

            # Fire-and-track: hub serves immediately; backends connect in the background.
            runtime.start_background_connect(post_connect=_post_connect)

            try:
                await server.serve()
            except asyncio.CancelledError:
                pass
            finally:
                # Cancels background connect task (if still running), then disconnects.
                await runtime.stop()
                if pid_file.exists():
                    pid_file.unlink()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nHub stopped.")


@cli.command("mcp")
@click.option("--log-level", default="WARNING", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
def mcp_stdio(log_level: str) -> None:
    """Serve MCP JSON-RPC over stdin/stdout (NDJSON framing).

    For native integration with Claude Desktop and other stdio-only MCP
    clients. Federates the same MCP servers as `slm-hub start`, but
    transport is stdin/stdout instead of HTTP.

    DISCIPLINE: stdout is reserved for JSON-RPC frames. All logging
    goes to stderr. Default log level is WARNING (quiet) to avoid
    noise on stderr unless something goes wrong.
    """
    # stdio mode REQUIRES stderr-only logging — stdout is for JSON-RPC.
    _setup_logging(log_level, stderr_only=True)
    
    import warnings
    warnings.filterwarnings("ignore")
    
    _load_secrets()
    config = load_config()

    async def _run_stdio() -> None:
        from slm_mcp_hub.lifecycle.runtime import HubRuntime
        from slm_mcp_hub.server.stdio_server import StdioServer

        async with HubOrchestrator(config) as hub:
            runtime = HubRuntime(hub)

            # W2-P2: fire-and-track — hub serves immediately while backends connect
            # in the background.  First client request may arrive before all backends
            # are ready; hub__list_servers reflects current state at each call.
            runtime.start_background_connect()

            stdio_server = StdioServer(
                mcp_endpoint=runtime.mcp_endpoint,
                session_manager=runtime.session_manager,
                notifier=runtime.notifier,
            )

            try:
                await stdio_server.serve()
            except asyncio.CancelledError:
                pass
            finally:
                # Cancels background connect task (if still running), then disconnects.
                await runtime.stop()

    try:
        asyncio.run(_run_stdio())
    except KeyboardInterrupt:
        # Don't print anything — stdout is sacred.
        pass


@cli.command()
@click.option("-v", "--verbose", is_flag=True, help="Show per-server connection detail")
def status(verbose: bool) -> None:
    """Show hub status with actual process health check.

    With --verbose, fetches /api/servers/detail and shows per-server
    configured / connected / tools / last-error.
    """
    from slm_mcp_hub.resilience.watchdog import is_running, read_pid_file

    if is_running():
        pid = read_pid_file()
        config = load_config()

        import httpx
        try:
            resp = httpx.get(
                f"http://{config.host}:{config.port}/api/health",
                headers=hub_headers(),
                timeout=5.0,
            )
            health = resp.json()
            click.echo(f"Hub is running (PID {pid})")
            click.echo(f"  Version: {health.get('version', 'unknown')}")
            click.echo(f"  Port: {config.port}")
            click.echo(f"  State: {health.get('state', 'unknown')}")
            click.echo(f"  Uptime: {health.get('uptime_seconds', 0):.0f}s")
            click.echo(f"  MCP servers: {health.get('mcp_servers_configured', '?')}")
            click.echo(f"  Config: {get_config_file()}")

            if verbose:
                try:
                    detail_resp = httpx.get(
                        f"http://{config.host}:{config.port}/api/servers/detail",
                        headers=hub_headers(),
                        timeout=5.0,
                    )
                    servers = detail_resp.json().get("servers", [])
                    if servers:
                        click.echo("\n  Per-server detail:")
                        click.echo(f"    {'NAME':<28} {'TRANSPORT':<8} {'STATUS':<12} {'TOOLS':>6}")
                        for s in servers:
                            if not s["enabled"]:
                                status_str = "disabled"
                            elif s["connected"]:
                                status_str = "connected"
                            elif s.get("error"):
                                status_str = "failed"
                            else:
                                status_str = "pending"
                            click.echo(
                                f"    {s['name']:<28} {s['transport']:<8} "
                                f"{status_str:<12} {s['tools']:>6}"
                            )
                            if s.get("error") and not s["connected"]:
                                click.echo(f"      └─ {s['error']}")
                except Exception as exc:
                    click.echo(f"  (verbose fetch failed: {exc})")
        except httpx.ConnectError:
            click.echo(f"Hub PID {pid} exists but HTTP endpoint is unreachable")
            click.echo(f"  Port {config.port} not responding — hub may have crashed")
            click.echo("  Restart with: slm-hub start")
    else:
        click.echo("Hub is not running")
        click.echo("  Start with: slm-hub start")
        click.echo("  Install daemon: slm-hub daemon install")


@cli.command()
@click.argument("server_name")
def reconnect(server_name: str) -> None:
    """Reconnect a failed or disconnected MCP server."""
    import httpx

    try:
        config = load_config()
        resp = httpx.post(
            f"http://{config.host}:{config.port}/api/servers/{server_name}/reconnect",
            headers=hub_headers(),
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
    config_file = get_config_file()
    if config_file.exists():
        click.echo(f"Config already exists at {config_file}")
        if not click.confirm("Overwrite?"):
            return
    generate_default_config(config_file)
    click.echo(f"Default config created at {config_file}")


@config.command("snapshots")
def config_snapshots() -> None:
    """List all config snapshots (auto-saved before every change)."""
    from slm_mcp_hub.core.config import list_snapshots
    snapshots_dir = get_snapshots_dir()
    snaps = list_snapshots()
    if not snaps:
        click.echo(f"No snapshots in {snapshots_dir}")
        return
    click.echo(f"Snapshots in {snapshots_dir} (newest first):")
    for s in snaps:
        click.echo(f"  {s['name']:<35} {s['mcp_count']:>3} MCPs  {s['size']:>6} bytes")
    click.echo("\nRestore with: slm-hub config restore <snapshot-name>")


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
        click.echo("List available with: slm-hub config snapshots")


cli.add_command(setup)
cli.add_command(network)
cli.add_command(server_group)
cli.add_command(auth_group)

# W5-P1: Observability commands — servers, health, warm, stop.
from slm_mcp_hub.cli.observe_commands import (  # noqa: E402, PLC0415
    health_cmd,
    servers_cmd,
    stop_cmd,
    warm_cmd,
)

cli.add_command(servers_cmd)
cli.add_command(health_cmd)
cli.add_command(warm_cmd)
cli.add_command(stop_cmd)


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

    import subprocess

    from slm_mcp_hub.resilience.watchdog import LAUNCHD_LABEL, install_launchd

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
    import subprocess

    from slm_mcp_hub.resilience.watchdog import LAUNCHD_LABEL

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
    import subprocess

    from slm_mcp_hub.resilience.watchdog import LAUNCHD_LABEL, is_running, read_pid_file

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
    """List available tools from the running hub via REST API.

    Uses the /api/servers/detail endpoint (not MCP) so it works without
    establishing an MCP session — faster and more reliable.
    """
    import httpx

    config = load_config()
    try:
        resp = httpx.get(
            f"http://{config.host}:{config.port}/api/servers/detail",
            headers=hub_headers(),
            timeout=10.0,
        )
        resp.raise_for_status()
        servers = resp.json()

        if isinstance(servers, dict):
            servers = servers.get("servers", [])

        if query:
            q = query.lower()
            servers = [
                s for s in servers
                if q in s.get("name", "").lower()
                or q in str(s.get("tools", [])).lower()
            ]

        for srv in sorted(servers, key=lambda s: s.get("name", "")):
            name = srv.get("name", "?")
            tools_val = srv.get("tools", [])
            status = srv.get("status", "?")
            transport = srv.get("transport", "?")
            if isinstance(tools_val, list):
                click.echo(f"{name:<30} {transport:<8} {status:<12} ({len(tools_val)} tools)")
                for t in sorted(tools_val)[:5]:
                    click.echo(f"  └─ {t}")
                if len(tools_val) > 5:
                    click.echo(f"  ─ and {len(tools_val) - 5} more")
            else:
                click.echo(f"{name:<30} {transport:<8} {status:<12} (tool count: {tools_val})")

    except httpx.ConnectError:
        click.echo("Hub is not running. Start with: slm-hub start")
    except Exception as exc:
        click.echo(f"Error: {exc}")


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
