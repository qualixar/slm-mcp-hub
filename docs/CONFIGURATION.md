# Configuration

The hub reads `~/.slm-mcp-hub/config.json`. Set `SLM_HUB_CONFIG_DIR` to move
the complete runtime directory, including configuration, database, PID, log,
and snapshots.

## Example

```json
{
  "host": "127.0.0.1",
  "port": 52414,
  "session_timeout_seconds": 3600,
  "max_sessions": 50,
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
      "enabled": true
    },
    "remote": {
      "type": "http",
      "url": "${REMOTE_MCP_URL}",
      "headers": {"Authorization": "Bearer ${REMOTE_MCP_TOKEN}"},
      "enabled": true
    }
  }
}
```

JSON comments are not supported.

## Server fields

| Field | Meaning |
|---|---|
| `command` | Executable for a stdio server. |
| `args` | Argument array for a stdio server. |
| `env` | Environment passed to a stdio server. |
| `type` | `stdio`, `http`, or `sse`. Inferred when omitted. |
| `url` | Endpoint for an HTTP or SSE server. |
| `headers` | Request headers for an HTTP or SSE server. |
| `enabled` | Whether the server may connect. |
| `always_on` | Prevents idle shutdown. |
| `no_cache` | Disables hub caching for the server. |
| `cost_per_call_cents` | Optional accounting value. |

## Secret placeholders

`${VAR}` and `${env:VAR}` are resolved only when a backend connection is
created. Saving, modifying, importing, or snapshotting the config preserves the
literal placeholder.

The CLI loads secrets from these files when present:

1. `~/.slm-mcp-hub/secrets.env`
2. `~/.claude-secrets.env`

An existing literal secret cannot be safely converted back into a placeholder
because its original variable name is unknown. Rotate secrets exposed by older
versions and remove contaminated config and snapshot copies manually.

## SuperLocalMemory plugin settings

SuperLocalMemory is a direct sibling service. Do not list it as a federated
backend when using the built-in plugins.

| Variable | Purpose |
|---|---|
| `SLM_DAEMON_URL` | Daemon base URL; defaults to `http://127.0.0.1:8765`. |
| `SLM_API_KEY` | Sent as `X-SLM-API-Key` by both SLM plugins. |

Restart the hub after changing either value.

## Hub environment variables

| Variable | Purpose |
|---|---|
| `SLM_HUB_CONFIG_DIR` | Runtime/config directory. |
| `SLM_HUB_HOST` | HTTP bind host. |
| `SLM_HUB_PORT` | HTTP bind port. |
| `SLM_HUB_LOG_LEVEL` | Logging level. |
| `SLM_HUB_API_KEY` | Required for non-loopback binds; authenticates MCP and management routes. |
| `SLM_HUB_STATELESS` | Set to `1` for legacy clients that cannot retain session IDs. |
| `SLM_HUB_SESSION_RECOVERY` | Set to `1` to re-adopt a legacy session ID after restart. |

Modern MCP `2026-07-28` requests are sessionless independently of
`SLM_HUB_STATELESS`.

## Remote binding

The hub refuses a non-loopback bind unless `SLM_HUB_API_KEY` is set. Clients
send the value in `X-SLM-Hub-API-Key` or `Authorization: Bearer <key>`. Use TLS
outside the host and restrict ingress at the network boundary.

## Safe changes and recovery

```bash
slm-hub server add example --command npx --arg -y --arg package-name
slm-hub server modify example --env TOKEN='${EXAMPLE_TOKEN}'
slm-hub server reload
slm-hub config snapshots
slm-hub config restore <snapshot-name>
```

Writes are atomic. Existing non-trivial configs are snapshotted, and a
large unexpected server-count drop is refused unless explicitly forced.
