# Migration Guide

## v0.2.x → v0.3.0

v0.3.0 changes the transport default and adds several new configuration fields.
Most existing setups work without changes — the breaking items are listed first.

### Breaking: transport default is now stateless

In v0.2.x, the hub defaulted to stateful sessions. `SLM_HUB_STATELESS=1` was
used to switch to stateless.

In v0.3.0, **stateless is the default**. `SLM_HUB_STATELESS` is no longer the
relevant variable. If your setup relied on stateful sessions (for resumable
streaming or session-keyed state), add:

```bash
export SLM_HUB_STATEFUL=1
```

Or in config:

```json
{
  "transport_stateful": true
}
```

If your setup had `SLM_HUB_STATELESS=1` set, remove it — the hub is already
stateless by default in v0.3.0.

### New config fields

| Field | Default | What it does |
|---|---|---|
| `transport_stateful` | `false` | Enables stateful sessions (resumable streaming). |
| `timeout_class` (per server) | `"default"` | `fast`/`default`/`extended`/`unbounded` — replaces the flat 120 s ceiling. |
| `max_live_backends` | unlimited | LRU cap on live backend subprocesses. |

### New behavior

**Unified call pipeline.** All tool calls now flow through a single dispatch
path with a per-backend concurrency gate (default 10 concurrent per backend)
and timeout class resolution. The per-backend gate prevents one slow server from
blocking calls to the others — this was the primary cause of head-of-line
blocking in v0.2.x.

**Progress forwarding.** Backend `notifications/progress` events are now
forwarded to the hub's client in real time. No config change required.

**Resumable streaming.** Available in stateful mode only. Client↔hub resumption
is handled by the SDK automatically. The hub→backend one-shot retry fires only
when a resumption token was captured — the call does not retry blindly. In the
default stateless mode there is no resumption.

**Observability.** New endpoints and CLI commands: `GET /api/servers/enriched`,
`GET /api/events` (SSE), admin dashboard at `/admin`, and CLI `slm-hub servers`,
`slm-hub health`, `slm-hub warm`, `slm-hub stop`. All require the hub API key.

### Dependency change

`mcp==2.0.0`, `keyring>=25.7,<26`, and `filelock>=3.32,<4` are now required.
`pip install slm-mcp-hub` pulls them automatically.

---

## Direct MCPs → SLM MCP Hub

This section covers migrating from per-session direct MCP connections to the hub.
The process is reversible and takes about 5 minutes.

### Before you start

#### What you have now

```json
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp"]
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
    }
  }
}
```

Each session spawns all these as separate processes. Five sessions = 5× the subprocesses.

#### What you will have after

```json
{
  "mcpServers": {
    "context7": {
      "type": "http",
      "url": "http://127.0.0.1:52414/mcp/context7"
    },
    "github": {
      "type": "http",
      "url": "http://127.0.0.1:52414/mcp/github"
    },
    "gemini": {
      "type": "http",
      "url": "http://127.0.0.1:52414/mcp/gemini"
    }
  }
}
```

Same keys. Same tool names. One hub manages everything.

### Step-by-step migration

#### 1. Install the hub

```bash
pip install slm-mcp-hub
```

#### 2. Import your MCPs

```bash
slm-hub setup import ~/.claude.json
```

This reads your MCP definitions and copies them into `~/.slm-mcp-hub/config.json`.
Your `claude.json` is not modified.

#### 3. Start the hub

```bash
slm-hub start
```

Watch the output — you should see all your backends connecting:

```
SLM MCP Hub v0.3.0 running on http://127.0.0.1:52414/mcp
  MCP servers: 38/38 connected
  Tools: 462
```

If some fail:
- Are the executables installed? (`npx`, `uvx`, `node` must be in `PATH`)
- Are environment variables set? The hub loads `~/.claude-secrets.env`.
- Run `slm-hub start --log-level DEBUG` to see connection details.

#### 4. Verify before migrating

```bash
curl http://127.0.0.1:52414/api/health
curl http://127.0.0.1:52414/api/servers/enriched
```

#### 5. Migrate your client

```bash
# Preview (no files modified)
slm-hub setup register --client claude_code --mode transparent --dry-run

# Apply (creates backup automatically at ~/.claude.json.pre-hub-backup)
slm-hub setup register --client claude_code --mode transparent
```

#### 6. Restart your client

Close and reopen your Claude Code session. All tools work identically.

#### 7. Migrate other clients (optional)

```bash
slm-hub setup register --all --mode transparent
```

This covers Claude Code, VS Code Copilot, Cursor, Windsurf, and Codex CLI.

### Rollback

```bash
cp ~/.claude.json.pre-hub-backup ~/.claude.json
```

Restart your client. You are back to direct connections.

### Special cases

#### MCPs with browser-based OAuth sessions

Some MCPs maintain OAuth sessions tied to the specific process. Keep them as
direct connections while routing everything else through the hub:

```json
{
  "mcpServers": {
    "google-workspace": {
      "command": "uvx",
      "args": ["google-workspace-mcp"],
      "env": { }
    },
    "everything-else": {
      "type": "http",
      "url": "http://127.0.0.1:52414/mcp/everything-else"
    }
  }
}
```

#### Adding new backends after migration

Add new backends to the hub config, not to `claude.json`:

```bash
slm-hub server add new-backend --command npx --arg -y --arg new-mcp-server
```

Then add the proxy entry to `claude.json` (transparent mode) or just use
`call_tool("new-backend__tool_name", {...})` in federated mode.

#### Running the hub as a service

**macOS (launchd)**

```bash
slm-hub setup --launchd
```

**Linux (systemd)**

```ini
[Unit]
Description=SLM MCP Hub
After=network.target

[Service]
ExecStart=/usr/local/bin/slm-hub start
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable slm-mcp-hub
systemctl --user start slm-mcp-hub
```

### Verifying the migration

1. **Tool names unchanged:** All MCP tools should show the same names as before
   (e.g. `mcp__context7__query-docs`).
2. **Tools respond:** Call any tool — responses should be identical.
3. **Hub is routing:** Check the hub log: `tail -f ~/.slm-mcp-hub/hub.log`
4. **RAM reduced:** `ps aux | grep -c "mcp"` should be ~39 regardless of how
   many client sessions are open.
