# Changelog

All notable changes to SLM MCP Hub will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-08-05

### Fixed

- CLI management commands now authenticate against auth-enabled hubs. When
  `SLM_HUB_API_KEY` is set, `slm-hub tools`, `status --verbose`, `reconnect`,
  the `server` subcommands (`list`, `reload`, `add`, `remove`, `modify`,
  `status`), and the observability commands (`servers`, `health`, `warm`,
  `stop`) attach the `X-SLM-Hub-API-Key` header the hub requires. Previously
  these issued unauthenticated requests and returned `401 Unauthorized` on any
  deployment with hub auth enabled — only `status` (which hits the exempt
  `/api/health` endpoint) worked. Base-URL and header resolution are now
  centralised in `slm_mcp_hub.cli.api_client`. ([#29])

[#29]: https://github.com/qualixar/slm-mcp-hub/issues/29

## [0.3.0] - 2026-08-05

Universal MCP hub: federate stdio, HTTP, and SSE servers — including
OAuth-protected ones — through a single endpoint, on the official MCP Python
SDK and the `2026-07-28` protocol, with per-backend RAM governance, long-running
call support, and a runtime observability surface.

### Added

- OAuth 2.0 login for upstream MCP servers, with tokens stored in the OS
  keychain. New `slm-hub auth` command group: `login`, `status [--json]`, and
  `logout`. `login` is the only command that opens a browser; no command ever
  prints a token, refresh token, client secret, or authorization code.
- Streamable HTTP serving on the official SDK, alongside stdio, so the hub can
  run as either transport and federate upstreams over either transport.
- Per-backend spawn policy for RAM control. Mark a server `spawn: lazy` to
  harvest its tools at startup and then evict it once it has been idle past
  `idle_ttl_seconds` — freeing its subprocess while its tools stay discoverable
  and callable; the next routed call reconnects it transparently. A
  `max_live_backends` cap evicts the least-recently-used non-pinned backend when
  the limit is reached. `spawn: pinned` (or `always_on`) servers stay hot.
- Unified call pipeline with per-backend concurrency gate (default 10 concurrent
  calls per backend) and per-server timeout classes — `fast` (30 s) /
  `default` (120 s) / `extended` (600 s) / `unbounded`. Progress notifications
  are forwarded from backend to client in real time on both transport modes.
  Per-server p95 latency and call metrics are recorded on every dispatch.
  Resumable streaming is available in stateful mode (see Transport below).
- A runtime observability surface. `GET /api/servers/enriched` reports each
  backend's state, uptime, restart count, and tool count; `GET /api/events`
  streams lifecycle events over SSE without a slow reader ever stalling the hub;
  a localhost admin dashboard renders the same table; and new CLI commands
  `servers`, `health`, `warm`, and `stop` read status and warm or stop a backend
  at runtime. Every admin route requires the hub API key.
- Legacy SSE upstreams. `type: sse` servers now connect through the SSE client
  (they were previously misrouted to the Streamable HTTP client). The
  unsupported SSE-plus-OAuth combination is rejected at configuration time.
- End-to-end coverage of the full transport matrix (stdio/HTTP downstream ×
  stdio/HTTP/OAuth-HTTP/SSE upstream) and of the lazy spawn, idle eviction, and
  on-demand reconnect cycle, exercised with real processes.

### Transport

The default transport mode is now **stateless** (MCP `2026-07-28`): no session
tracking, no server-side event store, no resumable replay. This replaces the
v0.2.x stateful default.

Set `SLM_HUB_STATEFUL=1` (or `transport_stateful: true` in config) to enable
stateful sessions. Stateful mode activates the SDK's `InMemoryEventStore` and
allows resumable streaming on the client↔hub leg. On the hub→backend leg, a
one-shot retry fires when a backend drops mid-stream — but only when the backend
had issued a resumption token (meaning it can continue, not restart). Without a
token, the call fails cleanly. Resumable streaming is a stateful-mode feature;
in the default stateless mode there is no resumption.

Migration from v0.2.x: if you relied on stateful sessions, add
`SLM_HUB_STATEFUL=1`. Remove any `SLM_HUB_STATELESS=1` from your environment —
it is no longer used.

### Changed

- Inbound and outbound MCP now run on the official `mcp==2.0.0` SDK behind a
  transport-neutral protocol layer. Requires Python 3.11 or newer.
- The hub continues to expose three meta-tools (`search_tools`, `call_tool`,
  `list_servers`) as its interface; upstream tools are federated through
  `call_tool` rather than re-listed by name.

### Security

- Upstream OAuth metadata and callback URLs are restricted to HTTPS or exact
  loopback HTTP, with SSRF defenses: private/reserved/link-local and
  IPv4-mapped IPv6 addresses are blocked, DNS-rebinding is checked across all
  resolved IPs, and resolution failure fails closed.
- A downstream client's `Authorization` header is never forwarded to an
  upstream server; the hub uses only its own stored token upstream.
- Token refresh is serialized across tasks and processes.
- The unauthenticated health endpoint reports only status and version; it no
  longer exposes the bind host, port, or loaded plugin names.
- The OAuth callback host is validated as loopback at configuration time, and the
  authorization URL's query string (which carries the CSRF `state` nonce) is kept
  out of logs.
- Admin dashboard output HTML-escapes every backend-derived value, and the
  lifecycle event stream emits an explicit field allowlist — no configuration,
  headers, tokens, or environment values reach a client.

### Dependencies

- Adds `mcp==2.0.0`, `keyring>=25.7,<26`, and `filelock>=3.32,<4` as runtime
  dependencies.

## [0.2.6] - 2026-08-03

### Security

- Preserve unresolved `${VAR}` values across load, save, CLI mutation, import,
  snapshots, and restore; materialize secrets only when a backend connection is
  created. Fixes #11.
- Authenticate direct SLM and Mesh daemon calls with `X-SLM-API-Key` from
  `SLM_API_KEY`; surface 401/403 without logging the key. Fixes #10.
- Refuse non-loopback binds without `SLM_HUB_API_KEY` and authenticate MCP,
  transparent proxy, and management routes when hub authentication is enabled.
- Validate and sanitize proxy, management, HTTP, and stdio protocol boundaries.

### Added

- Optional legacy sessionless mode through `SLM_HUB_STATELESS=1`.
- MCP `2026-07-28` stateless request handling, per-request metadata checks, and
  `server/discover`, while retaining legacy stateful negotiation. Fixes #8.
- Streamable HTTP `DELETE /mcp` session termination and opt-in restart recovery
  through `SLM_HUB_SESSION_RECOVERY=1`, adapted from PR #15.
- CI, dependency review, CodeQL, package-content checks, isolated artifact
  installation, and a coverage floor above 95%.

### Fixed

- Honor `SLM_HUB_CONFIG_DIR` at the point of use across config, snapshots, PID,
  logs, watchdog, and database paths, completing the compatible parts of PR #1.
- Normalize and reconstruct flattened `call_tool` arguments with strict invalid
  parameter handling. Integrates PR #12 and PR #13.
- Validate initialize input before session creation. Integrates PR #14.
- Respect backend-advertised MCP capabilities. Integrates PR #9.
- Return JSON-RPC `Invalid Request` for valid JSON scalars and arrays on stdio
  instead of raising an unhandled exception.

### Packaging and documentation

- Align Python runtime, plugin, wheel, npm, and CLI versions at 0.2.6.
- Replace the npm global-pip fallback with an exact-version, package-local
  virtual environment; installation failures now fail the npm install.
- Remove generated bytecode, metadata, backup, and archive files from packages.
- Replace dead documentation links and unsupported comparative claims with
  tested behavior and explicit security guidance.

## [0.2.5] - 2026-07-04

### Fixed
- **HTTP client error handling for string errors** (`federation/connection.py`): Safely handle cases where HTTP MCP servers return error fields as strings (like `{"error": "Unauthorized"}`) instead of JSON-RPC error objects. Previously, calling `err.get("code")` caused `AttributeError: 'str' object has no attribute 'get'` and crashed the initialization flow. Now handles string errors cleanly.

## [0.2.4] - 2026-07-04

### Fixed
- **[CRITICAL] Universal client compatibility — any IDE/agent now works** (`server/http_server.py`, `session/manager.py`): Sessions were never created when a client sent its own `Mcp-Session-Id` header on `initialize`. Any MCP client that provides its own session ID (Antigravity IDE, Cursor, Windsurf, GitHub Copilot Chat, custom agents) received `404 Session not found` on every subsequent call. The hub now always registers the session on `initialize`, honouring client-provided IDs. Claude Code (which lets the server generate the ID) was the only client unaffected. **5 regression tests added.**
- **HTTP notification sent to empty URL** (`federation/connection.py`): `_send_notification_http` posted `notifications/initialized` to `""` instead of `self._http_url`, silently dropping the notification for all HTTP/SSE federated MCP servers. **2 regression tests added.**
- **Non-dict capability result crashes server connection** (`federation/connection.py`): When an HTTP MCP server returns a bare string or list as the `result` of `tools/list`, `resources/list`, etc. (e.g. higgsfield returns `"pong"`), calling `.get()` raised `AttributeError: 'str' object has no attribute 'get'` — marking the server as permanently ERROR. Now degrades gracefully with a warning. **3 regression tests added.**

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
