# SLM MCP Hub

[![PyPI](https://img.shields.io/pypi/v/slm-mcp-hub)](https://pypi.org/project/slm-mcp-hub/)
[![npm](https://img.shields.io/npm/v/slm-mcp-hub)](https://www.npmjs.com/package/slm-mcp-hub)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/slm-mcp-hub/)
[![Status](https://img.shields.io/badge/status-alpha-orange)](https://github.com/qualixar/slm-mcp-hub/issues)

A local-first MCP gateway that connects once to all your MCP backends and exposes them through a single endpoint. Part of Qualixar's work on AI Reliability Engineering.

**Alpha software.** File reproducible failures through [GitHub Issues](https://github.com/qualixar/slm-mcp-hub/issues).

## The problem it solves

Without a hub, every AI client session spawns its own MCP subprocesses. Five sessions with 38 configured servers means 190 processes and roughly 10 GB of RAM. The hub runs those processes once, shares them across every connected client, and adds governance, observability, and reliability on top.

## What ships in v0.3.0

### Unified call pipeline

Every tool call flows through one dispatch path. A per-backend concurrency gate (default 10 concurrent calls per backend) prevents one slow server from blocking calls to the others. Per-server timeout classes — `fast` (30 s), `default` (120 s), `extended` (600 s), `unbounded` — let a long-running server finish without cutting off the call at a flat ceiling. Backend `notifications/progress` are forwarded to the hub's client in real time on both transport modes. Per-server p95 latency and call metrics are recorded on every dispatch.

### Transport: stateless default, stateful opt-in

The default run mode is modern stateless MCP `2026-07-28`: no session tracking, no event store, no resumable replay. This is the right default for most deployments — stateless means the hub can restart cleanly with no session state to recover.

Set `SLM_HUB_STATEFUL=1` (or `transport_stateful: true` in config) to enable stateful sessions. Stateful mode activates resumable streaming: the SDK's `InMemoryEventStore` handles client↔hub `Last-Event-ID` stream resumption automatically. On the hub→backend leg, a safe one-shot retry fires when a backend drops mid-stream — but only when the backend had already issued a resumption token, meaning it can continue from that point rather than restart. A connection drop without a token fails cleanly; the call is not retried.

Resumable streaming is a stateful-mode feature. In the default stateless mode there is no resumption, by design.

### Observability

`GET /api/servers/enriched` reports each backend's live state, uptime, restart count, and tool count. `GET /api/events` streams lifecycle events over SSE without a slow reader ever stalling the hub. A localhost admin dashboard renders the same data in a browser. Runtime CLI commands: `slm-hub servers`, `slm-hub health`, `slm-hub warm <server>`, `slm-hub stop <server>`. All admin routes require the hub API key.

### RAM governance

Lazy spawn harvests a backend's tools at startup and starts its subprocess only when the first call arrives. Idle eviction shuts a backend down once it has been idle past `idle_ttl_seconds`, freeing the process while its tools stay discoverable and callable — the next routed call reconnects it transparently. An LRU cap evicts the least-recently-used non-pinned backend when the live process count hits `max_live_backends`. Mark a server `always_on` (or `spawn: pinned`) to keep it hot.

### Transport completeness

Backends connect over stdio, Streamable HTTP, SSE, or OAuth 2.0-protected HTTP (authorize once with `slm-hub auth login`; tokens stored in the OS keychain). Downstream clients connect over Streamable HTTP or native stdio. The combination of SSE backend and OAuth is rejected at configuration time.

### Federation

Three meta-tools — `search_tools`, `call_tool`, `list_servers` — let any client discover and invoke any tool across all connected backends through a single hub entry. Tools are namespaced as `server__tool`. Backward-compatible `hub__` prefix aliases are accepted.

## Install

Python 3.11 or newer.

```bash
pip install slm-mcp-hub
```

The npm shim installs the matching Python release into an isolated environment it owns:

```bash
npm install -g slm-mcp-hub
```

The two packages are release-locked. Installation fails rather than falling back to a mismatched version or modifying an externally managed Python install.

## Quick start

```bash
slm-hub config init
slm-hub setup detect
slm-hub setup import ~/.claude.json
slm-hub start
```

Default HTTP endpoint: `http://127.0.0.1:52414/mcp`. Health check: `http://127.0.0.1:52414/api/health`.

Native stdio, for clients that launch MCP servers as subprocesses:

```json
{
  "mcpServers": {
    "slm-hub": {
      "command": "slm-hub",
      "args": ["mcp"]
    }
  }
}
```

## Routing modes

**Federated mode** exposes three meta-tools. One hub entry in your client config, three tools to reach everything:

```bash
slm-hub setup register --client claude_code --mode federated
```

**Transparent mode** gives each backend its own route at `/mcp/{server-name}`. Original tool names, zero behavior change — useful for migration testing or clients that need the backend's native tool surface:

```bash
slm-hub setup register --client claude_code --mode transparent
```

Use federated mode when context size matters. Use transparent mode when a client requires the backend's original tool names or when you are testing before a full migration.

## Transport mode

The default is stateless. Stateless means no session IDs, no server-side event store, and no resumable streaming — and also no session state to manage or recover.

Enable stateful sessions when you need resumable streaming:

```bash
export SLM_HUB_STATEFUL=1
slm-hub start
```

Or in `config.json`:

```json
{
  "transport_stateful": true
}
```

**Resumable streaming** is only available in stateful mode. On the client↔hub leg, the SDK handles `Last-Event-ID` reconnection through `InMemoryEventStore` automatically. On the hub→backend leg, a one-shot retry fires if and only if the backend issued a resumption token before the connection dropped. Without a token, the call fails cleanly — the hub does not blindly re-execute a tool whose idempotency is unknown.

## Configuration

Default file: `~/.slm-mcp-hub/config.json`. Set `SLM_HUB_CONFIG_DIR` to move the full runtime directory, including config, database, PID, log, and snapshots.

```json
{
  "host": "127.0.0.1",
  "port": 52414,
  "transport_stateful": false,
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      },
      "timeout_class": "default"
    },
    "deep-research": {
      "type": "http",
      "url": "${RESEARCH_MCP_URL}",
      "headers": {
        "Authorization": "Bearer ${RESEARCH_MCP_TOKEN}"
      },
      "timeout_class": "extended"
    },
    "remote-oauth": {
      "type": "http",
      "url": "${REMOTE_MCP_URL}",
      "auth": { "mode": "oauth" }
    }
  },
  "plugins_enabled": ["slm", "mesh"]
}
```

JSON comments are not supported.

### Server fields

| Field | Purpose |
|---|---|
| `command` | Executable for a stdio server. |
| `args` | Argument array for a stdio server. |
| `env` | Environment passed to a stdio server. |
| `type` | `stdio`, `http`, or `sse`. Inferred when omitted. |
| `url` | Endpoint for an HTTP or SSE server. |
| `headers` | Request headers for an HTTP or SSE server. |
| `timeout_class` | `fast` (30 s), `default` (120 s), `extended` (600 s), `unbounded`. Defaults to `default`. |
| `enabled` | Whether the server may connect. |
| `always_on` / `spawn: pinned` | Prevents idle eviction. |
| `idle_ttl_seconds` | Idle eviction threshold for this server. |
| `no_cache` | Disables hub caching for this server. |
| `cost_per_call_cents` | Optional accounting value. |

### Hub environment variables

| Variable | Purpose |
|---|---|
| `SLM_HUB_CONFIG_DIR` | Runtime/config directory. |
| `SLM_HUB_HOST` | HTTP bind host. |
| `SLM_HUB_PORT` | HTTP bind port. |
| `SLM_HUB_LOG_LEVEL` | Logging level. |
| `SLM_HUB_API_KEY` | Required for non-loopback binds; authenticates MCP and all management routes. |
| `SLM_HUB_STATEFUL` | Set to `1` to enable stateful sessions and resumable streaming. Default is stateless. |

Secret values go in `~/.slm-mcp-hub/secrets.env` or `~/.claude-secrets.env`. `${VAR}` placeholders in config resolve only when a backend connection starts; the hub persists the placeholder, not the resolved value. If an older release wrote a literal secret into config, rotate that credential and remove the contaminated file manually.

### Safe changes and recovery

```bash
slm-hub server add example --command npx --arg -y --arg package-name
slm-hub server modify example --env TOKEN='${EXAMPLE_TOKEN}'
slm-hub server reload
slm-hub config snapshots
slm-hub config restore <snapshot-name>
```

Config writes are atomic. Existing non-trivial configs are snapshotted before any write. A large unexpected server-count drop is refused unless explicitly forced.

## Authentication

OAuth 2.0-protected upstream servers authorize once per server:

```bash
slm-hub auth login SERVER      # opens browser once to authorize
slm-hub auth status [SERVER]   # metadata only — never prints a token
slm-hub auth status --json
slm-hub auth logout SERVER
```

Tokens are stored in the OS keychain via `keyring` — a working keychain backend is required. `login` is the only command that opens a browser. No command prints an access token, refresh token, client secret, or authorization code. A downstream client's `Authorization` header is never forwarded to an upstream server; the hub uses only its own stored token for upstream connections.

OAuth metadata and callback URLs are restricted to HTTPS or loopback HTTP. Private, reserved, and link-local IP addresses are blocked, with DNS-rebinding checks across all resolved IPs.

## SuperLocalMemory

Run the SLM daemon as its own process, then enable the direct hub plugins:

```json
{
  "plugins_enabled": ["slm", "mesh"]
}
```

```bash
export SLM_DAEMON_URL=http://127.0.0.1:8765
export SLM_API_KEY='your-daemon-api-key'
slm-hub start
```

`SLM_API_KEY` is sent as `X-SLM-API-Key` by both the SLM and mesh plugins. Authentication failures disable the affected plugin and remain visible in logs — the key is never logged. Restart the hub after rotating the daemon key.

Do not add the SLM daemon under `mcpServers` when using these plugins. That creates a nested topology that is not the supported integration path.

## Remote access security

The hub refuses a non-loopback bind unless `SLM_HUB_API_KEY` is set:

```bash
export SLM_HUB_HOST=0.0.0.0
export SLM_HUB_API_KEY='generate-a-long-random-value'
slm-hub start
```

Clients send the key in `X-SLM-Hub-API-Key` or `Authorization: Bearer <key>`. Authentication covers `/mcp`, transparent proxy routes, and all management APIs. `/api/health` remains available without a key for infrastructure probes. Use TLS at the network boundary whenever traffic leaves the host.

## Protocol conformance

The hub targets MCP `2026-07-28`. Its own interface is the three meta-tools (`search_tools`, `call_tool`, `list_servers`); upstream tool names are not re-listed at `tools/list`. Upstream capabilities are exercised through `call_tool` across the full transport matrix: stdio, Streamable HTTP, SSE, and OAuth-protected HTTP backends.

## Development and verification

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest --cov=slm_mcp_hub
npm test
```

The release gate requires more than 97% Python line coverage, clean linting, wheel and sdist package inspection, isolated install tests, dependency audits, and supported-Python CI.

Architecture, configuration, migration, and getting-started details are in the [docs directory](docs/).

## Contributing

Bug reports are most useful with a reproduction test. Pull requests must keep both distribution channels version-aligned and pass all release gates. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

AGPL-3.0-or-later for open-source use. Commercial licenses are available — see [LICENSE](LICENSE) or contact the Qualixar team.
