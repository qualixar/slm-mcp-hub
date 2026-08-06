# SLM MCP Hub

[![PyPI](https://img.shields.io/pypi/v/slm-mcp-hub)](https://pypi.org/project/slm-mcp-hub/)
[![npm](https://img.shields.io/npm/v/slm-mcp-hub)](https://www.npmjs.com/package/slm-mcp-hub)
[![CI](https://github.com/qualixar/slm-mcp-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/qualixar/slm-mcp-hub/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/slm-mcp-hub/)
[![MCP](https://img.shields.io/badge/MCP-2026--07--28-6f42c1)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange)](https://github.com/qualixar/slm-mcp-hub/issues)

**Run every MCP server once. Reach them from every client through one local endpoint.**

SLM MCP Hub sits between your AI clients and your MCP servers: clients connect
to the hub, the hub connects to each backend once and shares it across every
session — adding governance, observability, and reliability on top. Part of
Qualixar's work on AI Reliability Engineering.

**Who it's for:** anyone running more than one MCP client (Claude Code, Cursor,
Windsurf, Claude Desktop, custom agents) against a shared set of servers — or
anyone whose machine is buckling under duplicate MCP subprocesses.

> **Alpha software.** The interfaces work and are tested — 2,306 tests at 98.54%
> line coverage, with the full transport matrix exercised against real
> processes — but they can still change between releases. Please file
> reproducible failures through
> [GitHub Issues](https://github.com/qualixar/slm-mcp-hub/issues).

## Why

Every AI client that speaks MCP spawns its own copy of every server it uses.
Run a few sessions and the math turns ugly fast:

```
      Without a hub                              With SLM MCP Hub

  client 1 ─► 38 subprocesses           client 1 ─┐
  client 2 ─► 38 subprocesses           client 2 ─┤
  client 3 ─► 38 subprocesses           client 3 ─┼─► hub ─► 38 shared backends
  client 4 ─► 38 subprocesses           client 4 ─┤        one endpoint,
  client 5 ─► 38 subprocesses           client 5 ─┘        one process pool

  = 190 processes, ~10 GB RAM           = 38 processes, shared by everyone
```

The hub runs each backend once, multiplexes every client through a single
endpoint, and keeps memory bounded while your tools stay one call away.

### Hub vs configuring servers in each client

| | Per-client config | With SLM MCP Hub |
|---|---|---|
| **Processes** | every client spawns every server | each server runs once, shared |
| **RAM** | grows with every session | bounded by spawn policy + LRU cap |
| **Server config** | duplicated in every client | one file, one place |
| **Adding a server** | edit every client by hand | `slm-hub server add`, hot-reloaded |
| **Health & metrics** | none | live state, p95 latency, and RAM per backend |
| **OAuth backends** | re-authorize in every client | authorize once, token in the OS keychain |

## What you get

| | |
|---|---|
| **One connection, every backend** | Point a client at the hub once and reach every configured server through three meta-tools — no per-client server list to maintain. |
| **Shared process pool** | Backends start once and are shared across all client sessions instead of being re-spawned per session. |
| **RAM governance** | Lazy spawn, idle eviction, and an LRU cap on live backends keep memory bounded. Evicted backends stay discoverable and reconnect on the next call. |
| **Unified call pipeline** | Every call takes one path: a per-backend concurrency gate, per-server timeout classes, live progress forwarding, and p95 metrics on every dispatch. |
| **Every transport** | stdio, Streamable HTTP, SSE, and OAuth 2.0-protected HTTP backends, all behind one endpoint. |
| **Observability** | Live per-backend state, uptime, restarts, p95 latency, and RAM over REST, an SSE event stream, a localhost dashboard, and the CLI. |
| **Secure by default** | Loopback-only unless you set an API key; OAuth tokens in the OS keychain; secrets never land in logs or config. |

## Install

Python 3.11 or newer.

```bash
pip install slm-mcp-hub
```

Or via npm — the shim installs the matching Python release into an isolated
environment it owns:

```bash
npm install -g slm-mcp-hub
```

The two packages are release-locked. Installation fails loudly rather than
falling back to a mismatched version or modifying an externally managed Python.

Two optional extras, both off by default:

```bash
pip install 'slm-mcp-hub[network]'        # zeroconf: discover servers on the LAN
pip install 'slm-mcp-hub[observability]'  # psutil: per-backend RAM in the metrics
pip install 'slm-mcp-hub[full]'           # both of the above
```

Nothing else is pulled in. The hub talks to SuperLocalMemory over HTTP, so
`[full]` does not install a memory engine, a model runtime, or anything else you
did not ask for — see [SuperLocalMemory](#superlocalmemory) below.

## Quick start

```bash
slm-hub config init                     # write a default config
slm-hub setup detect                    # find MCP servers already on this machine
slm-hub setup import ~/.claude.json     # import them into the hub
slm-hub start                           # run the hub
```

The hub is now serving every imported backend at one endpoint:

- **HTTP:** `http://127.0.0.1:52414/mcp`
- **Health:** `http://127.0.0.1:52414/api/health`
- **Dashboard:** `http://127.0.0.1:52414/`

Point a client at it over native stdio:

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

Confirm what's connected:

```bash
slm-hub servers        # live table: state, uptime, restarts, p95, RAM, tools
slm-hub tools          # every tool reachable through the hub
```

## Routing modes

**Federated** — one hub entry in your client config exposes three meta-tools
(`search_tools`, `call_tool`, `list_servers`) that reach everything. Best when
context size matters:

```bash
slm-hub setup register --client claude_code --mode federated
```

**Transparent** — each backend keeps its own route at `/mcp/{server-name}` with
its original tool names and zero behavior change. Best for migration testing or
clients that need a backend's native tool surface:

```bash
slm-hub setup register --client claude_code --mode transparent
```

## How it works

### Unified call pipeline

Every tool call flows through one dispatch path. A per-backend concurrency gate
(default 10 concurrent calls per backend) stops one slow server from blocking
calls to the others. Per-server timeout classes — `fast` (30 s), `default`
(120 s), `extended` (600 s), `unbounded` — let a long-running server finish
instead of being cut off at a flat ceiling. Backend `notifications/progress`
are forwarded to the hub's client in real time on both transport modes, and
per-server p95 latency and call metrics are recorded on every dispatch.

### RAM governance

Lazy spawn harvests a backend's tools at startup and starts its subprocess only
when the first call arrives. Idle eviction shuts a backend down once it has been
idle past `idle_ttl_seconds`, freeing the process while its tools stay
discoverable and callable — the next routed call reconnects it transparently.
An LRU cap evicts the least-recently-used non-pinned backend when the live
process count hits `max_live_backends`. Mark a server `always_on` (or
`spawn: pinned`) to keep it hot.

### Transport completeness

Backends connect over stdio, Streamable HTTP, SSE, or OAuth 2.0-protected HTTP
(authorize once with `slm-hub auth login`; tokens stored in the OS keychain).
Downstream clients connect over Streamable HTTP or native stdio. The
combination of an SSE backend and OAuth is rejected at configuration time.

### Federation

Three meta-tools — `search_tools`, `call_tool`, `list_servers` — let any client
discover and invoke any tool across all connected backends through a single hub
entry. Tools are namespaced as `server__tool`. Backward-compatible `hub__`
prefix aliases are accepted.

### Observability

`GET /api/servers/enriched` reports each backend's live state, uptime, restart
count, p95 latency, RAM, and tool count. `GET /api/events` streams lifecycle
events over SSE without a slow reader ever stalling the hub. A localhost admin
dashboard renders the same data in a browser. Runtime CLI: `slm-hub servers`,
`slm-hub health`, `slm-hub warm <server>`, `slm-hub stop <server>`. All
management routes require the hub API key when one is set.

## Transport mode: stateless by default

The default run mode is modern stateless MCP `2026-07-28` — no session IDs, no
server-side event store, no resumable streaming, and so no session state to
manage or recover. This is the right default for most deployments: the hub
restarts cleanly with nothing to rebuild.

Enable stateful sessions only when you need resumable streaming:

```bash
export SLM_HUB_STATEFUL=1
slm-hub start
```

Or in `config.json`:

```json
{ "transport_stateful": true }
```

**Resumable streaming is a stateful-mode feature.** On the client↔hub leg, the
SDK handles `Last-Event-ID` reconnection through `InMemoryEventStore`
automatically. On the hub→backend leg, a one-shot retry fires **if and only if**
the backend issued a resumption token before the connection dropped — so the
call continues from that point rather than restarting. A drop without a token
fails cleanly; the hub never blindly re-executes a tool whose idempotency is
unknown. In the default stateless mode there is no resumption, by design.

## Configuration

Default file: `~/.slm-mcp-hub/config.json`. Set `SLM_HUB_CONFIG_DIR` to move the
whole runtime directory (config, database, PID, log, and snapshots) at once.

```json
{
  "host": "127.0.0.1",
  "port": 52414,
  "transport_stateful": false,
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}" },
      "timeout_class": "default"
    },
    "deep-research": {
      "type": "http",
      "url": "${RESEARCH_MCP_URL}",
      "headers": { "Authorization": "Bearer ${RESEARCH_MCP_TOKEN}" },
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
| `SLM_HUB_API_KEY` | Required for non-loopback binds; authenticates MCP, transparent proxy, and all management routes. The CLI reads it from the environment and sends it automatically. |
| `SLM_HUB_STATEFUL` | Set to `1` to enable stateful sessions and resumable streaming. Default is stateless. |

Secret values go in `~/.slm-mcp-hub/secrets.env` or `~/.claude-secrets.env`.
`${VAR}` placeholders in config resolve only when a backend connection starts;
the hub persists the placeholder, never the resolved value. If an older release
wrote a literal secret into config, rotate that credential and remove the
contaminated file manually.

### Safe changes and recovery

```bash
slm-hub server add example --command npx --arg -y --arg package-name
slm-hub server modify example --env TOKEN='${EXAMPLE_TOKEN}'
slm-hub server reload
slm-hub config snapshots
slm-hub config restore <snapshot-name>
```

Config writes are atomic. Existing non-trivial configs are snapshotted before
any write. A large, unexpected drop in server count is refused unless explicitly
forced.

## Authentication (upstream OAuth)

OAuth 2.0-protected upstream servers authorize once per server:

```bash
slm-hub auth login SERVER      # opens a browser once to authorize
slm-hub auth status [SERVER]   # metadata only — never prints a token
slm-hub auth status --json
slm-hub auth logout SERVER
```

Tokens are stored in the OS keychain via `keyring` — a working keychain backend
is required. `login` is the only command that opens a browser. No command ever
prints an access token, refresh token, client secret, or authorization code. A
downstream client's `Authorization` header is never forwarded upstream; the hub
uses only its own stored token for upstream connections. OAuth metadata and
callback URLs are restricted to HTTPS or loopback HTTP, with private, reserved,
and link-local addresses blocked and DNS-rebinding checks across all resolved
IPs.

## Remote access security

The hub refuses a non-loopback bind unless `SLM_HUB_API_KEY` is set:

```bash
export SLM_HUB_HOST=0.0.0.0
export SLM_HUB_API_KEY='generate-a-long-random-value'
slm-hub start
```

Clients send the key in `X-SLM-Hub-API-Key` or `Authorization: Bearer <key>`.
Authentication covers `/mcp`, the transparent proxy routes, and all management
APIs; the CLI attaches the key from the environment on your behalf. `/api/health`
stays open without a key for infrastructure probes. Use TLS at the network
boundary whenever traffic leaves the host.

## SuperLocalMemory

The hub integrates with [SuperLocalMemory](https://github.com/qualixar/superlocalmemory)
over HTTP, not through a Python import. There is no extra to install and no
version to match — the hub works with whatever SLM release you are running,
because the daemon's HTTP API is the only contract between them.

Run the SLM daemon as its own process, then enable the direct hub plugins:

```json
{ "plugins_enabled": ["slm", "mesh"] }
```

```bash
export SLM_DAEMON_URL=http://127.0.0.1:8765
export SLM_API_KEY='your-daemon-api-key'
slm-hub start
```

`SLM_API_KEY` is sent as `X-SLM-API-Key` by both the SLM and mesh plugins. An
authentication failure disables the affected plugin and stays visible in logs —
the key itself is never logged. Restart the hub after rotating the daemon key.
Do not also add the SLM daemon under `mcpServers`; that creates a nested
topology that is not the supported integration path.

## Protocol conformance

The hub targets MCP `2026-07-28`. Its own interface is the three meta-tools
(`search_tools`, `call_tool`, `list_servers`); upstream tool names are not
re-listed at `tools/list`. Upstream capabilities are exercised through
`call_tool` across the full transport matrix: stdio, Streamable HTTP, SSE, and
OAuth-protected HTTP backends.

## Development and verification

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest --cov=slm_mcp_hub
npm test
```

v0.3.2 ships at **2,306 tests and 98.54% line coverage**. The transport matrix
is exercised with real processes, not mocks: stdio, Streamable HTTP, SSE, and
OAuth-protected HTTP upstreams, across both downstream transports, plus the full
lazy-spawn, idle-eviction, and on-demand-reconnect cycle.

The release gate requires more than 97% Python line coverage, clean linting,
wheel and sdist package inspection, isolated install tests, dependency audits,
and CI across every supported Python version. Architecture, configuration,
migration, and getting-started guides live in the
[docs directory](docs/).

## Contributing

Bug reports are most useful with a reproduction test. Pull requests must keep
both distribution channels version-aligned and pass every release gate. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

AGPL-3.0-or-later for open-source use. Commercial licenses are available — see
[LICENSE](LICENSE) or contact the Qualixar team.
