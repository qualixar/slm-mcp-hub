# Changelog

All notable changes to SLM MCP Hub will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.3] - 2026-06-07

### Added
- **Per-call timeout for tool invocations** (`DEFAULT_TOOL_TIMEOUT_S = 120`): Tool calls now have a 2-minute default timeout instead of the 60-minute ceiling. Quick searches fail fast instead of hanging for an hour. Overridable per-server via `timeout_s` parameter on `route_tool_call`.

### Fixed
- **Meta-tool rename from `hub__*` to bare names**: `hub__search_tools` → `search_tools`, `hub__call_tool` → `call_tool`, `hub__list_servers` → `list_servers`. Backward-compatible aliases preserved (`_META_TOOL_ALIASES`) so existing client code with `hub__` prefix continues to work. Fixes Grok CLI compatibility (Grok's MCP client couldn't parse `hub__` prefixed tool names).
- **`slm-hub tools` CLI returns empty output** (Bug A): Rewrote to use `/api/servers/detail` REST endpoint instead of POSTing to MCP without a session. Now correctly lists all 50 servers with their tools.
- **`call_tool` meta-tool can't route to meta-tools** (Bug C): When `call_tool(tool="list_servers")` is called, the hub now handles it locally instead of routing to the federation router (which doesn't know about meta-tools).
- **Updated tests for renamed meta-tools**: All 654 tests pass with the new naming convention.

## [0.2.1] - 2026-05-15

### Documentation
- Confirmed that MCP servers exposing both `X-API-KEY` header auth AND OAuth Bearer (e.g. Gamma's `https://mcp.gamma.app/mcp`) can be federated through the hub today via the existing per-server `headers` configuration. No new code required:

  ```json
  "gamma": {"type": "http", "url": "https://mcp.gamma.app/mcp", "headers": {"X-API-KEY": "<your-key>"}}
  ```

  After updating `config.json`, run `slm-hub server reload` to hot-add without restarting the hub.

Full OAuth-DCR (RFC 9728 / RFC 7591 / PKCE) federation for MCPs that *only* support OAuth Bearer (no API-key bypass) is a separate v0.3.0 feature.

### Fixed
- Minor: cosmetic cleanups in connection error messages and `add_server` / `reconnect` response strings.

## [0.2.0] - 2026-05-15

### Added — Lifecycle & Transport

#### Zero-restart hot-reload
- `slm-hub server add <name> [--command ... | --type http --url ...]` — adds and connects an MCP server without restarting the hub.
- `slm-hub server remove <name>` — drains in-flight tool calls (30s default), then disconnects and deregisters.
- `slm-hub server modify <name> [--env K=V] [--arg ARG] [--command ... | --url ...] [--enabled/--disabled]` — restarts a single server in-place after a config change.
- `slm-hub server list [--show-tools]` — lists configured servers with live connection status.
- `slm-hub server reload` — re-reads `config.json` from disk and applies the diff.
- `slm-hub server status [<name>]` — per-server detail or single-server lookup.
- New `lifecycle/` module: `config_diff`, `notifier`, `reloader`, `runtime`, drain semantics.
- MCP `notifications/tools/list_changed` is now emitted to subscribed clients within ≤1s of any registry change (debounced).
- Atomic config swap with `asyncio.Lock` — concurrent reload triggers serialize; invalid configs preserve current state.

#### Native stdio transport
- New `slm-hub mcp` command serves MCP JSON-RPC over stdin/stdout using NDJSON framing.
- Enables native Claude Desktop integration without a Node bridge — same federation, just stdin/stdout instead of HTTP.
- All logging routed to stderr in stdio mode; stdout is reserved for JSON-RPC frames.
- Session attribution via `SLM_HUB_AGENT_ID` env var.

#### Setup & discovery improvements
- `slm-hub setup detect` now finds Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS).
- `slm-hub setup register --client claude-desktop` writes a native stdio entry (`{"command": "slm-hub", "args": ["mcp"]}`) instead of an HTTP entry.

#### Status & observability
- `slm-hub status --verbose` shows per-server connected/disabled/failed state with last-error message.
- New `/api/servers/detail` endpoint exposes per-server lifecycle state via HTTP.
- New `/api/reload` endpoint triggers config reload from disk.

### Fixed
- `MCPEndpoint.initialize` correctly emits `notifications/tools/list_changed` (previously advertised `listChanged: True` but never sent notifications).
- `disconnect()` no longer raises `ProcessLookupError` when terminating a child process that already exited.
- Reader EOF now fails pending futures with `ConnectionError` immediately instead of hanging forever.
- Child process stderr is now drained to prevent pipe-buffer deadlock under verbose stdio MCPs.
- `disconnect_one()` now correctly removes the server entry from the live connection map.
- `asyncio.Lock` added around all `ConnectionManager` mutations to prevent registry-sync races.

### Changed
- Cold start now retries failed connections with a fast 0.5s/1.5s/4.5s schedule before falling back to the slower background retry loop.
- README and PyPI description updated to reflect new "first MCP gateway that learns, hot-reloads, and serves both stdio + HTTP natively" positioning.

### Backward compatibility
- `slm-hub start` HTTP transport continues to work identically. No config schema changes. All v0.1.x clients continue to work without modification.

## [0.1.0] - 2026-04-15

### Added

#### Core Gateway (Phases 0-2)
- Hub orchestrator with plugin architecture and singleton lifecycle
- Immutable configuration with env var resolution (`${VAR}` placeholders)
- SQLite storage with WAL mode and schema migrations
- MCP server federation with namespace isolation (`server__tool`)
- Stdio and HTTP transport support for MCP connections
- Session management with auto-expiry and coordination locks
- Streamable HTTP endpoint (`/mcp`) for MCP JSON-RPC protocol
- FastAPI-based management API (`/api/health`, `/api/status`, `/api/sessions`)

#### Intelligence (Phase 3)
- Intelligent caching with SHA-256 content-hash, TTL, and O(1) LRU eviction
- Cost tracking engine with per-tool pricing, session budgets, and cascade routing
- Smart tool filtering with project-type detection (13 activity categories)
- Lifecycle management with lazy MCP startup and idle shutdown
- Standalone learning engine with frequency stats, chain detection, and slow tool alerts

#### Security, Resilience, Observability (Phase 4)
- Permission engine with per-session role-based rules (ALLOW/DENY/WARN)
- Audit logger with SQLite-backed tamper trail
- Process watchdog with launchd (macOS) and systemd (Linux) auto-restart
- Request tracer with ring buffer and per-span timing
- Metrics collector with per-server success rate, p95 duration, cache hit rate

#### Discovery & Multi-Client Setup (Phase 5)
- Auto-detection of 5 AI clients: Claude Code, VS Code Copilot, Cursor, Windsurf, Codex CLI
- Auto-registration of hub with detected clients (backup before modify, dry-run mode)
- MCP config import from Claude Code and VS Code formats
- Network discovery via Zeroconf/mDNS (optional `[network]` dependency)
- Setup wizard CLI (`slm-hub setup detect/register/unregister/import`)

#### SLM Plugins (Phase 6)
- Plugin system via Python entry_points with error isolation
- SLM memory plugin: observe tool calls, recall session context, persist summaries
- SLM Mesh plugin: distributed locks, cross-machine routing, peer broadcast
- 6 hub notification hooks for full plugin lifecycle integration
- Predictive warm-up and learned tool filtering via SLM engine

### Security
- CORS restricted to localhost by default (not wildcard)
- SQL injection prevention via table allowlist and column validation
- Internal error messages sanitized (never leaked to clients)
- Atomic config writes with backup and restore on failure
