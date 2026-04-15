# Changelog

All notable changes to SLM MCP Hub will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
