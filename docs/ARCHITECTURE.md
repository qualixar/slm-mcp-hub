# Architecture Guide

## The Problem

Without a hub, every AI client session spawns its own MCP subprocesses:

```
Claude Session 1  →  38 MCP processes  (~2 GB RAM)
Claude Session 2  →  38 MCP processes  (~2 GB RAM)
VS Code Copilot   →  38 MCP processes  (~2 GB RAM)
Cursor            →  38 MCP processes  (~2 GB RAM)
Agent Team (x3)   →  38 MCP processes  (~2 GB RAM each)
                     ─────────────────
                     266 processes, ~13 GB RAM
```

Each session starts from zero. No shared backends, no coordination.

## The Solution

One hub process manages all MCP backends. Every client connects to the hub:

```
Claude Session 1  ──┐
Claude Session 2  ──┤
VS Code Copilot   ──┼──→  SLM MCP Hub (1 process)  →  38 MCP backends
Cursor            ──┤         │                          (shared)
Agent Team (x3)   ──┘         │
                              ├── Unified call pipeline (gate + timeout + metrics)
                              ├── RAM governance (lazy spawn / eviction)
                              ├── Observability (p95, dashboard, SSE events)
                              ├── SLM Plugin (session learning)
                              └── Mesh Plugin (cross-machine coordination)
                     ─────────────────
                     39 processes, ~2 GB RAM
```

## Transport Mode

### Stateless (default)

Modern MCP `2026-07-28`. No session tracking, no server-side event store. Requests are handled per-call — no `Mcp-Session-Id` required. This is the right default: the hub can restart cleanly with no state to recover.

### Stateful (opt-in)

Set `SLM_HUB_STATEFUL=1` or `transport_stateful: true`. The SDK's
`StreamableHTTPSessionManager` runs with `stateless=False`, activating
`InMemoryEventStore` for resumable streaming. The client↔hub `Last-Event-ID`
reconnect path is handled automatically by the SDK. The hub→backend resumable
retry (see below) is layered on top.

## Unified Call Pipeline

Every tool call flows through one dispatch path in `FederationRouter._dispatch_call`:

```
call arrives
    │
    ▼
_resolve_connection_async
    │  registry lookup + live connection check
    │  transparent on-demand reconnect if backend was evicted
    ▼
BackendConcurrencyGate.acquire(server_name)
    │  per-backend CapacityLimiter (default 10 concurrent calls)
    │  prevents one slow backend from blocking calls to others
    ▼
timeout class resolution
    │  fast=30s / default=120s / extended=600s / unbounded=None
    │  per-server config; override_s takes precedence
    ▼
streaming vs. non-streaming dispatch
    │  non-streaming: conn.call_tool (backward-compatible default)
    │  streaming: conn.call_tool_streaming when progress_callback,
    │             resumption_context, or non-default timeout_class
    ▼
run_with_safe_resume (streaming path)
    │  token-gated one-shot retry on CONNECTION_CLOSED
    │  no token captured → fail cleanly, no retry
    ▼
MetricsCollector.record(server_name, duration_ms, success)
    │  p95 latency + call count, per backend
    ▼
activity_fn(server_name)
    │  resets idle reaper timestamp on call completion
    ▼
RouteResult → caller
```

Metrics and activity tracking are fail-open: a bug in either never breaks a real call.

## Resumable Streaming

Two distinct resumption mechanisms, each covering a different leg:

**Client↔Hub (SDK, stateful mode only)**
The SDK's `InMemoryEventStore` stores outbound SSE events. When a client
reconnects with `Last-Event-ID`, the SDK replays the stored events
automatically. This is off in stateless mode — there is no event store.

**Hub→Backend (safe token-gated, stateful mode)**
When a backend connection drops mid-stream (`MCPError code=CONNECTION_CLOSED`),
the router retries once if and only if the backend had issued a resumption token.
A token means the backend acknowledged progress and can continue from that point.
Without a token, the call fails cleanly — the hub does not blindly re-execute a
tool whose idempotency is unknown. The retry is bounded at one attempt.

## RAM Governance

```
spawn: lazy (default)
    hub startup: connect, harvest tools, disconnect subprocess
    first routed call: reconnect transparently
    idle past idle_ttl_seconds: evict subprocess, tools stay registered
    max_live_backends exceeded: evict LRU non-pinned backend

spawn: pinned (always_on)
    subprocess stays alive; idle eviction does not apply
```

Evicted backends remain discoverable through the capability registry. The next
call reconnects them without client intervention.

## Routing Modes

Two modes run on the same hub process simultaneously.

### Federated Mode

**Endpoint:** `/mcp` — one entry in the client config.

```json
{
  "mcpServers": {
    "hub": {"type": "http", "url": "http://127.0.0.1:52414/mcp"}
  }
}
```

Three meta-tools: `search_tools`, `call_tool`, `list_servers`. All backends
discoverable and callable through these three. Namespace: `server__tool`.
Backward-compatible `hub__` prefix aliases accepted.

Use when context size matters or when many backends are connected.

### Transparent Proxy Mode

**Endpoint:** `/mcp/{server_name}` — one entry per backend.

```json
{
  "mcpServers": {
    "github":   {"type": "http", "url": "http://127.0.0.1:52414/mcp/github"},
    "context7": {"type": "http", "url": "http://127.0.0.1:52414/mcp/context7"}
  }
}
```

Original tool names, zero behavior change. Use for migration testing or when
a client requires the backend's native tool surface.

## Observability

The hub exposes three observability surfaces:

| Surface | Access | Data |
|---|---|---|
| `GET /api/servers/enriched` | API key required | Per-backend state, uptime, restart count, tool count, p95 latency |
| `GET /api/events` | API key required | SSE stream of lifecycle events (connect, disconnect, error, evict) |
| Admin dashboard | `http://127.0.0.1:52414/admin` | Same as enriched, rendered as HTML |

CLI mirrors: `slm-hub servers`, `slm-hub health`, `slm-hub warm <server>`,
`slm-hub stop <server>`.

The unauthenticated `/api/health` endpoint reports only status and version.

## Plugin System

Plugins auto-discover via Python `entry_points` on startup. Errors in one plugin
do not affect the hub or other plugins.

### Plugin Lifecycle Hooks

```python
class HubPlugin(ABC):
    async def on_hub_start(self, hub) -> None: ...
    async def on_hub_stop(self) -> None: ...
    async def on_tool_call_after(self, ...) -> None: ...
    async def on_session_start(self, ...) -> None: ...
    async def on_session_end(self, ...) -> None: ...
    async def on_mcp_connect(self, ...) -> None: ...
    async def on_mcp_disconnect(self, ...) -> None: ...
```

### SLM Plugin

Connects to the SuperLocalMemory daemon at `localhost:8765` via HTTP.

| Hook | Action |
|---|---|
| `on_hub_start` | Health check; disables plugin if daemon is unavailable |
| `on_tool_call_after` | Logs tool call to the SLM learning pipeline |
| `on_session_start` | Recalls context from past sessions |
| `on_session_end` | Logs session summary |

When SLM is not installed or the daemon is not running, all hooks are no-ops.
The hub works fully standalone.

### Mesh Plugin

Connects to the SLM daemon's mesh endpoints (`localhost:8765/mesh/*`).
Registers as a mesh peer on startup, broadcasts tool usage and session events,
and provides distributed locking via `POST /mesh/lock` for conflict prevention
when multiple sessions access the same resource.

### Coexistence Model

```
Claude Code hooks → direct MCP (stdio)  → mcp__superlocalmemory__session_init
Hub SLM plugin   → HTTP API             → localhost:8765/api/v3/tool-event
Hub Mesh plugin  → HTTP API             → localhost:8765/mesh/send
SLM Dashboard    → HTTP                 → localhost:8765/
```

Same daemon, multiple access paths, no conflicts.

## Backend Transport Support

| Transport | Downstream | Upstream |
|---|---|---|
| stdio | Yes (`slm-hub mcp`) | Yes (command-based servers) |
| Streamable HTTP | Yes (`/mcp`) | Yes |
| SSE | No | Yes (no OAuth combination) |
| OAuth 2.0 HTTP | — | Yes (`slm-hub auth login`) |

## Secrets and Environment Variables

The hub loads secrets from `~/.slm-mcp-hub/secrets.env` and `~/.claude-secrets.env`
on startup. `${VAR}` and `${env:VAR}` placeholders in config resolve only when a
backend connection is created — the hub persists the placeholder, not the value.
