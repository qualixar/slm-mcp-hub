# Configuration Reference

## Hub Config File

Location: `~/.slm-mcp-hub/config.json`

Override with: `SLM_HUB_CONFIG_DIR` environment variable

### Full Example

```json
{
  "host": "127.0.0.1",
  "port": 52414,
  "log_level": "INFO",
  "session_timeout_seconds": 3600,
  "max_sessions": 50,
  "cache_ttl_seconds": 300,
  "cache_max_entries": 1000,
  "idle_shutdown_seconds": 1800,
  "cors_origins": ["http://127.0.0.1", "http://localhost"],
  "plugins_enabled": [],  // v0.2.0: SLM + Mesh plugins (not yet available)
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp"],
      "enabled": true
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"
      }
    },
    "gemini": {
      "type": "http",
      "url": "http://localhost:3001/mcp",
      "headers": {
        "X-Api-Key": "${GEMINI_API_KEY}"
      }
    },
    "tavily": {
      "type": "http",
      "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=${TAVILY_API_KEY}"
    }
  }
}
```

### Settings

| Setting | Default | Description |
|:--------|:--------|:------------|
| `host` | `127.0.0.1` | Listen address. Use `0.0.0.0` for network access. |
| `port` | `52414` | Listen port |
| `log_level` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `session_timeout_seconds` | `3600` | Session auto-expiry (1 hour) |
| `max_sessions` | `50` | Maximum concurrent client sessions |
| `cache_ttl_seconds` | `300` | Default cache TTL (5 minutes) |
| `cache_max_entries` | `1000` | Maximum cached results |
| `idle_shutdown_seconds` | `1800` | Idle MCP shutdown timeout (30 minutes) |
| `cors_origins` | `["http://127.0.0.1", "http://localhost"]` | Allowed CORS origins |
| `plugins_enabled` | `[]` | Plugin filter. Empty = all discovered plugins. |

### Environment Variable Overrides

| Variable | Overrides |
|:---------|:----------|
| `SLM_HUB_PORT` | `port` |
| `SLM_HUB_HOST` | `host` |
| `SLM_HUB_LOG_LEVEL` | `log_level` |
| `SLM_HUB_CONFIG_DIR` | Config directory path |

### MCP Server Entry Format

#### Stdio Transport (command-based)

```json
{
  "server-name": {
    "command": "npx",
    "args": ["-y", "package-name"],
    "env": {
      "API_KEY": "${API_KEY}"
    },
    "enabled": true,
    "always_on": false,
    "no_cache": false,
    "cost_per_call_cents": 0.0
  }
}
```

#### HTTP Transport (URL-based)

```json
{
  "server-name": {
    "type": "http",
    "url": "https://example.com/mcp",
    "headers": {
      "Authorization": "Bearer ${TOKEN}"
    },
    "enabled": true
  }
}
```

### Secret Resolution

MCP configs support `${VAR}` and `${env:VAR}` placeholders. The hub resolves these from:

1. `~/.claude-secrets.env` (shared with Claude Code)
2. `~/.slm-mcp-hub/secrets.env` (hub-specific)
3. OS environment variables

Example `.claude-secrets.env`:
```
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxxxxxxxxx
GEMINI_API_KEY=AIzaSyxxxxxxxxxx
TAVILY_API_KEY=tvly-xxxxxxxxxx
```

## API Endpoints

### Hub Management

| Endpoint | Method | Description |
|:---------|:-------|:------------|
| `/api/health` | GET | Health check (version, state, uptime) |
| `/api/status` | GET | Detailed hub + session status |
| `/api/sessions` | GET | List active sessions |
| `/api/sessions/{id}` | DELETE | Destroy a session |
| `/api/servers` | GET | List all backend MCP servers |

### MCP Protocol

| Endpoint | Method | Description |
|:---------|:-------|:------------|
| `/mcp` | POST | Federated MCP endpoint (all tools) |
| `/mcp/{server}` | POST | Transparent proxy for specific server |

## CLI Reference

```bash
# Hub lifecycle
slm-hub start [--port PORT] [--config PATH] [--log-level LEVEL]
slm-hub status

# Configuration
slm-hub config init
slm-hub config show
slm-hub config import <file> [--format auto|claude|vscode]

# Client setup
slm-hub setup detect [--json-output]
slm-hub setup register --all|--client NAME [--mode transparent|federated] [--dry-run]
slm-hub setup unregister --all|--client NAME
slm-hub setup import <file> [--format auto|claude|vscode]

# Network
slm-hub network discover [--timeout SECONDS] [--json-output]
slm-hub network info
```
