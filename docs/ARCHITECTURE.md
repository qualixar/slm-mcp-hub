# Architecture Guide

## The Problem

Without a hub, every AI client session spawns its own MCP processes:

```
Claude Session 1  →  38 MCP processes  (~2GB RAM)
Claude Session 2  →  38 MCP processes  (~2GB RAM)
VS Code Copilot   →  38 MCP processes  (~2GB RAM)
Cursor            →  38 MCP processes  (~2GB RAM)
Agent Team (x3)   →  38 MCP processes  (~2GB RAM each)
                     ─────────────────
                     266 processes, ~13GB RAM
```

Each session starts from zero. No shared state, no caching, no coordination.

## The Solution

With the hub, one process manages all MCPs. Every client connects to the hub:

```
Claude Session 1  ──┐
Claude Session 2  ──┤
VS Code Copilot   ──┼──→  SLM MCP Hub (1 process)  →  38 MCP processes
Cursor            ──┤         │                          (shared)
Agent Team (x3)   ──┘         │
                              ├── Cache (dedup API calls)
                              ├── Cost tracking (budgets)
                              ├── SLM Plugin (learning)
                              ├── Mesh Plugin (coordination)
                              └── Observability (metrics)
                     ─────────────────
                     39 processes, ~2GB RAM
```

## Two Modes

The hub supports two modes simultaneously on the same process.

### Federated Mode (Recommended)

**Endpoint:** `/mcp` — One entry in claude.json. 3 meta-tools. Massive token savings.

```json
{
  "mcpServers": {
    "hub": {"type": "http", "url": "http://127.0.0.1:52414/mcp"}
  }
}
```

Claude gets 3 meta-tools: `hub__search_tools`, `hub__call_tool`, `hub__list_servers`. All 345+ tools discoverable and callable through these 3.

**Best for:** Production use, small-context models, cost optimization, SLM integration.

### Transparent Proxy Mode

**Endpoint:** `/mcp/{server_name}` — Per-server entries. Original tool names. Zero behavior change.

```json
{
  "mcpServers": {
    "context7": {"type": "http", "url": "http://127.0.0.1:52414/mcp/context7"},
    "github": {"type": "http", "url": "http://127.0.0.1:52414/mcp/github"}
  }
}
```

**Best for:** Migration testing, tool name compatibility.

## How Tool Calls Flow

### Federated Mode

```
1. Claude calls hub__call_tool(tool="github__search_repositories", arguments={...})
2. Claude Code sends JSON-RPC to http://127.0.0.1:52414/mcp
3. Hub looks up "github__search_repositories" in the capability registry
4. Registry maps it to the GitHub MCP server
5. Hub forwards to GitHub MCP process (stdin/stdout)
6. Result returns to Claude
7. Hub plugin fires on_tool_call_after → logs to SLM learning pipeline
```

### Transparent Proxy Mode

```
1. Claude calls mcp__context7__query-docs
2. Claude Code sends JSON-RPC to http://127.0.0.1:52414/mcp/context7
3. Hub forwards to context7 MCP process
4. Result returns UNMODIFIED
5. Hub plugin fires on_tool_call_after → logs to SLM learning pipeline
```

In both modes, the SLM plugin sees every tool call and logs it to the learning pipeline.

## Plugin System

The hub has a plugin architecture that auto-discovers plugins via Python entry_points on startup.

### Plugin Lifecycle Hooks

```python
class HubPlugin(ABC):
    async def on_hub_start(self, hub) -> None: ...      # Hub starting up
    async def on_hub_stop(self) -> None: ...             # Hub shutting down
    async def on_tool_call_after(self, ...) -> None: ... # After every tool call
    async def on_session_start(self, ...) -> None: ...   # New client connected
    async def on_session_end(self, ...) -> None: ...     # Client disconnected
    async def on_mcp_connect(self, ...) -> None: ...     # MCP server connected
    async def on_mcp_disconnect(self, ...) -> None: ...  # MCP server disconnected
```

### Built-In Plugins

#### SLM Plugin

Connects to the SuperLocalMemory daemon via HTTP API (`localhost:8765`).

| Hook | Action | Endpoint |
|:-----|:-------|:---------|
| `on_hub_start` | Check daemon health, set `_available` | `GET /status` |
| `on_tool_call_after` | Log tool call to learning pipeline | `POST /api/v3/tool-event` |
| `on_session_start` | Recall context from past sessions | `POST /api/v3/recall/trace` |
| `on_session_end` | Log session summary | `POST /api/v3/tool-event` |

The plugin also maintains a local ring buffer (10K observations) for `get_learned_tools()` and `get_warm_up_predictions()` — these work even without the SLM daemon.

When SLM is not installed or the daemon isn't running, all hooks are no-ops. The hub works fully standalone.

#### Mesh Plugin

Connects to the SLM daemon's mesh endpoints (`localhost:8765/mesh/*`).

| Hook | Action | Endpoint |
|:-----|:-------|:---------|
| `on_hub_start` | Register as mesh peer | `POST /mesh/register` |
| `on_tool_call_after` | Broadcast tool usage | `POST /mesh/send` |
| `on_session_start` | Notify peers | `POST /mesh/send` |
| `on_session_end` | Notify peers | `POST /mesh/send` |
| `on_mcp_connect` | Broadcast tool list change | `POST /mesh/send` |

Distributed locking via `POST /mesh/lock` prevents conflicts when multiple sessions access the same resource.

### Coexistence Model

The SLM daemon serves multiple consumers simultaneously:

```
Claude Code hooks → direct MCP (stdio)    → mcp__superlocalmemory__session_init
Hub SLM plugin   → HTTP API               → localhost:8765/api/v3/tool-event
Hub Mesh plugin  → HTTP API               → localhost:8765/mesh/send
SLM Dashboard    → HTTP                   → localhost:8765/
```

Same daemon, multiple protocols, zero conflicts.

## MCP Transport Support

| Transport | Status | Examples |
|:----------|:-------|:--------|
| **stdio** | Full support | npx, uvx, node, python commands |
| **HTTP** | Full support | Remote MCP servers with API keys |
| **SSE** | Full support | Servers returning text/event-stream |

## Secrets & Environment Variables

The hub loads secrets from `~/.claude-secrets.env` on startup (same file Claude Code uses). MCP configs support `${VAR}` placeholders that resolve at startup.

## What the Hub Adds (Both Modes)

- **Intelligent Caching** — SHA-256 content-hash keys, TTL, LRU eviction
- **Cost Tracking** — Per-tool costs, session budgets, cascade routing
- **Learning** — Usage patterns, tool frequency, with SLM: persistent across sessions
- **Cross-Session Coordination** — Distributed locks, conflict detection
- **Observability** — Per-server metrics, request tracing, audit log
- **Smart Tool Filtering** — Project-type detection (13 categories), frequency ranking
- **Lifecycle Management** — Lazy startup, idle shutdown, predictive warm-up (with SLM)
