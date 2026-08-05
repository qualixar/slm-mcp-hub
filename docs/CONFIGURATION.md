# Configuration

The hub reads `~/.slm-mcp-hub/config.json`. Set `SLM_HUB_CONFIG_DIR` to move the
complete runtime directory, including config, database, PID, log, and snapshots.

## Example

```json
{
  "host": "127.0.0.1",
  "port": 52414,
  "transport_stateful": false,
  "session_timeout_seconds": 3600,
  "max_sessions": 50,
  "max_live_backends": 30,
  "cache_ttl_seconds": 300,
  "cache_max_entries": 1000,
  "idle_shutdown_seconds": 1800,
  "log_level": "INFO",
  "cors_origins": ["http://127.0.0.1", "http://localhost"],
  "plugins_enabled": ["slm", "mesh"],
  "mcpServers": {
    "local": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-example"],
      "env": {"TOKEN": "${LOCAL_MCP_TOKEN}"},
      "timeout_class": "default",
      "enabled": true
    },
    "remote": {
      "type": "http",
      "url": "${REMOTE_MCP_URL}",
      "headers": {"Authorization": "Bearer ${REMOTE_MCP_TOKEN}"},
      "timeout_class": "extended",
      "enabled": true
    }
  }
}
```

JSON comments are not supported.

## Top-level fields

| Field | Default | Purpose |
|---|---|---|
| `host` | `127.0.0.1` | HTTP bind address. Non-loopback requires `SLM_HUB_API_KEY`. |
| `port` | `52414` | HTTP bind port. |
| `transport_stateful` | `false` | Enable stateful sessions and resumable streaming. See Transport Mode. |
| `max_live_backends` | unlimited | LRU cap on simultaneously live backend subprocesses. |
| `idle_shutdown_seconds` | — | Global idle eviction threshold; overridable per server. |
| `log_level` | `INFO` | Logging level. |
| `cors_origins` | localhost only | Allowed CORS origins. |
| `plugins_enabled` | `[]` | Plugin names to activate: `slm`, `mesh`. |

## Server fields

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
| `always_on` | Prevents idle eviction. Equivalent to `spawn: pinned`. |
| `idle_ttl_seconds` | Per-server idle eviction threshold. |
| `no_cache` | Disables hub caching for this server. |
| `cost_per_call_cents` | Optional accounting value for cost tracking. |

## Transport Mode

The default (`transport_stateful: false`) is modern stateless MCP `2026-07-28`.
Requests carry no `Mcp-Session-Id`; the hub maintains no per-client session state.

Set `transport_stateful: true` to enable stateful sessions. Stateful mode
activates the SDK's `InMemoryEventStore` and allows resumable streaming on the
client↔hub leg. The hub→backend one-shot token-gated retry also requires stateful
mode. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full resumption model.

## Timeout Classes

Each backend's `timeout_class` sets the `read_timeout_seconds` for its tool calls:

| Class | Timeout | Use for |
|---|---|---|
| `fast` | 30 s | Quick search, lookup, or local tools |
| `default` | 120 s | Standard backends |
| `extended` | 600 s | Slow or batch-processing backends |
| `unbounded` | None (no limit) | Deep research, long generative tasks |

A per-call `timeout_s` override takes precedence over the class timeout.

## Secret placeholders

`${VAR}` and `${env:VAR}` are resolved only when a backend connection is created.
Saving, modifying, importing, or snapshotting the config preserves the literal
placeholder — the resolved value never touches the config file or snapshots.

The CLI loads secrets from these files when present, in order:

1. `~/.slm-mcp-hub/secrets.env`
2. `~/.claude-secrets.env`

If an older release wrote a literal secret into `config.json` or a snapshot,
rotate that credential and remove the contaminated copies manually. The hub
cannot reconstruct a lost variable name from a resolved value.

## SuperLocalMemory plugin settings

SuperLocalMemory is a sibling service. Do not list it as a federated backend
when using the built-in plugins.

| Variable | Purpose |
|---|---|
| `SLM_DAEMON_URL` | Daemon base URL. Defaults to `http://127.0.0.1:8765`. |
| `SLM_API_KEY` | Sent as `X-SLM-API-Key` by both SLM plugins. |

Restart the hub after changing either value.

## Hub environment variables

Environment variables take precedence over the config file for their respective
settings.

| Variable | Purpose |
|---|---|
| `SLM_HUB_CONFIG_DIR` | Runtime/config directory. |
| `SLM_HUB_HOST` | HTTP bind host. |
| `SLM_HUB_PORT` | HTTP bind port. |
| `SLM_HUB_LOG_LEVEL` | Logging level. |
| `SLM_HUB_API_KEY` | Required for non-loopback binds; authenticates MCP and management routes. |
| `SLM_HUB_STATEFUL` | Set to `1` (or `true`, `yes`, `on`) to enable stateful sessions. Default is stateless. |

`SLM_HUB_STATEFUL` takes precedence over `transport_stateful` in the config file
when it is set and non-blank.

## Remote binding

The hub refuses a non-loopback bind unless `SLM_HUB_API_KEY` is set. Clients
send the value in `X-SLM-Hub-API-Key` or `Authorization: Bearer <key>`. The
authenticated perimeter covers `/mcp`, all transparent proxy routes, and all
management APIs. `/api/health` is available unauthenticated for infrastructure
probes. Use TLS outside the host and restrict ingress at the network boundary.

## Safe changes and recovery

```bash
slm-hub server add example --command npx --arg -y --arg package-name
slm-hub server modify example --env TOKEN='${EXAMPLE_TOKEN}'
slm-hub server reload
slm-hub config snapshots
slm-hub config restore <snapshot-name>
```

Config writes are atomic. Existing non-trivial configs are snapshotted before any
write. A large unexpected server-count drop is refused unless explicitly forced.
