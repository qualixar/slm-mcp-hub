"""W5-P1 — Observability CLI commands.

New top-level commands (mirrors existing 'slm-hub reconnect'):
  slm-hub warm <name>   — connect if not live (idempotent)
  slm-hub stop <name>   — evict: free RAM, retain caps

New top-level commands (enhanced views):
  slm-hub servers       — table with 6 signals for all backends
  slm-hub health        — fleet health snapshot: attention flags only

All commands POST/GET the hub HTTP API and require the hub to be running.
Network calls use httpx with generous timeouts; httpx.ConnectError = hub offline.
"""

from __future__ import annotations

import click
import httpx

from slm_mcp_hub.cli.api_client import hub_headers, hub_url


def _hub_url() -> str:
    """Return the hub base URL from config (e.g. 'http://127.0.0.1:8765')."""
    return hub_url()


def _fmt_uptime(seconds: float) -> str:
    """Format uptime seconds into human-readable string (e.g. '1h 4m', '30s')."""
    if seconds <= 0:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _fmt_ram(ram_bytes: int | None) -> str:
    """Format RAM bytes to compact string or '  —  ' when None."""
    if ram_bytes is None:
        return "  —  "
    return f"{ram_bytes / 1_048_576:5.1f}M"


@click.command("servers")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def servers_cmd(as_json: bool) -> None:
    """Show all backends with 6 signals: state, uptime, restarts, P95 ms, RAM, tools.

    Calls GET /api/servers/enriched (W5-P1) for enriched status.
    Falls back to /api/servers/detail (existing) if enriched endpoint unavailable.
    """
    url = _hub_url()
    headers = hub_headers()
    try:
        resp = httpx.get(f"{url}/api/servers/enriched", headers=headers, timeout=10.0)
        if resp.status_code == 404:
            resp = httpx.get(f"{url}/api/servers/detail", headers=headers, timeout=10.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        click.echo("Hub is not running. Start with: slm-hub start")
        return
    except Exception as exc:
        click.echo(f"Error: {exc}")
        return

    data = resp.json()
    servers = data.get("servers", [])

    if as_json:
        import json

        click.echo(json.dumps(servers, indent=2))
        return

    if not servers:
        click.echo("No servers configured.")
        return

    # Header row
    header = (
        f"{'NAME':<28} {'STATE':<14} {'UPTIME':>9} {'RST':>4} "
        f"{'P95ms':>7} {'RAM':>7} {'TOOLS':>6}"
    )
    click.echo(header)
    click.echo("-" * len(header))

    for s in sorted(servers, key=lambda x: x.get("name", "")):
        name = s.get("name", "?")[:28]
        state = s.get("lifecycle", "?")[:14]
        uptime = _fmt_uptime(s.get("uptime_seconds", 0.0))
        restarts = s.get("restart_count", 0)
        p95 = s.get("p95_latency_ms", 0.0)
        ram = _fmt_ram(s.get("ram_bytes"))
        tools = s.get("tools", 0)
        attention = "!" if s.get("needs_attention") else " "
        click.echo(
            f"{attention}{name:<28} {state:<14} {uptime:>9} {restarts:>4} "
            f"{p95:>7.1f} {ram:>7} {tools:>6}"
        )
        if s.get("last_error") and not s.get("connected"):
            click.echo(f"    └─ {s['last_error']}")


@click.command("health")
@click.option("--all", "show_all", is_flag=True, help="Show all servers, not just flagged")
def health_cmd(show_all: bool) -> None:
    """Fleet health snapshot: shows servers needing attention (use --all for all).

    Exits with code 1 if any server has needs_attention=True or is disconnected.
    Exits with code 0 when all servers are connected and healthy.
    """
    url = _hub_url()
    headers = hub_headers()
    try:
        resp = httpx.get(f"{url}/api/servers/enriched", headers=headers, timeout=10.0)
        if resp.status_code == 404:
            resp = httpx.get(f"{url}/api/servers/detail", headers=headers, timeout=10.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        click.echo("Hub is not running. Start with: slm-hub start")
        raise SystemExit(1) from None
    except Exception as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from None

    servers = resp.json().get("servers", [])
    flagged = [
        s for s in servers if s.get("needs_attention") or not s.get("connected")
    ]

    display = servers if show_all else flagged

    if not display:
        click.echo("All servers healthy.")
        return

    for s in display:
        icon = "!" if s.get("needs_attention") else ("x" if not s.get("connected") else " ")
        click.echo(
            f"[{icon}] {s['name']:<28} {s.get('lifecycle', '?'):<14} "
            f"restarts={s.get('restart_count', 0)} "
            f"failures={s.get('consecutive_failures', 0)}"
        )
        if s.get("last_error"):
            click.echo(f"     └─ {s['last_error']}")
        if s.get("auth_required"):
            click.echo(f"     └─ {s.get('next_action', 'slm-hub auth login ...')}")

    if flagged:
        raise SystemExit(1)


@click.command("warm")
@click.argument("server_name")
def warm_cmd(server_name: str) -> None:
    """Connect a backend if not currently live (idempotent warm-up).

    Returns success even if already connected (idempotent).
    Use 'reconnect' to force-disconnect + reconnect an already-live backend.
    """
    url = _hub_url()
    try:
        resp = httpx.post(
            f"{url}/api/servers/{server_name}/warm",
            headers=hub_headers(),
            timeout=60.0,
        )
        data = resp.json()
        if data.get("success"):
            click.echo(f"Warm: {server_name} — {data.get('message', '')}")
        else:
            click.echo(f"Failed: {data.get('message', 'unknown error')}")
            raise SystemExit(1)
    except httpx.ConnectError:
        click.echo("Hub is not running. Start with: slm-hub start")
        raise SystemExit(1) from None


@click.command("stop")
@click.argument("server_name")
@click.option("--force", is_flag=True, help="Evict even if in-flight calls are active")
def stop_cmd(server_name: str, force: bool) -> None:
    """Evict a backend: free subprocess/RAM while keeping its tools discoverable.

    Tools remain cached and routable — next call transparently restarts the backend.
    Use 'slm-hub server remove' to permanently remove a server from the fleet.

    Note: Pinned backends (spawn=pinned or always_on=True) are not evicted by the
    manager — this command returns success but the manager silently no-ops for pinned.
    """
    url = _hub_url()
    try:
        resp = httpx.post(
            f"{url}/api/servers/{server_name}/stop",
            headers=hub_headers(),
            timeout=30.0,
        )
        data = resp.json()
        if data.get("success"):
            click.echo(f"Stop: {server_name} — {data.get('message', '')}")
        else:
            click.echo(f"Failed: {data.get('message', 'unknown error')}")
            raise SystemExit(1)
    except httpx.ConnectError:
        click.echo("Hub is not running. Start with: slm-hub start")
        raise SystemExit(1) from None
