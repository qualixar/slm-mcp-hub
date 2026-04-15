# Migration Guide: Direct MCPs to SLM MCP Hub

This guide walks you through migrating from direct MCP connections to the hub. The process is safe, reversible, and takes about 5 minutes.

## Before You Start

### What You Have Now

Your `~/.claude.json` has MCP entries like this:

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

Each session spawns all these as separate processes. 5 sessions = 5x the processes.

### What You'll Have After

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

## Step-by-Step Migration

### 1. Install the Hub

```bash
pip install slm-mcp-hub
```

### 2. Import Your MCPs

```bash
slm-hub setup import ~/.claude.json
```

This reads your MCP definitions and copies them into `~/.slm-mcp-hub/config.json`. Your claude.json is NOT modified.

### 3. Start the Hub

```bash
slm-hub start
```

Watch the output. You should see all your MCPs connecting:

```
SLM MCP Hub v0.1.0 running on http://127.0.0.1:52414/mcp
  MCP servers: 38/38 connected
  Tools: 462
```

If some fail, check:
- Are the commands installed? (`npx`, `uvx`, `node` must be in PATH)
- Are environment variables set? The hub loads `~/.claude-secrets.env`
- Is the MCP server compatible? Check `slm-hub start --log-level DEBUG` for details

### 4. Verify Before Migrating

Test that tools work through the hub:

```bash
# Check health
curl http://127.0.0.1:52414/api/health

# List all servers and their tools
curl http://127.0.0.1:52414/api/servers
```

### 5. Migrate Claude Code

```bash
# Preview what will change (no files modified)
slm-hub setup register --client claude_code --mode transparent --dry-run

# Apply (creates backup automatically)
slm-hub setup register --client claude_code --mode transparent
```

A backup is created at `~/.claude.json.pre-hub-backup`.

### 6. Restart Claude Code

Close and reopen your Claude Code session. All tools work identically.

### 7. Migrate Other Clients (Optional)

```bash
slm-hub setup register --all --mode transparent
```

This migrates Claude Code, VS Code Copilot, Cursor, Windsurf, and Codex CLI — any that are installed.

## Rollback

At any point, restore your original config:

```bash
cp ~/.claude.json.pre-hub-backup ~/.claude.json
```

Restart Claude Code. You're back to direct connections. The hub can be stopped with Ctrl+C.

## Special Cases

### MCPs with OAuth Sessions (e.g., Google Workspace)

Some MCPs maintain OAuth sessions that are tied to the specific process. If an MCP uses browser-based OAuth login, keep it as a direct connection:

```json
{
  "mcpServers": {
    "google-workspace": {
      "command": "uvx",
      "args": ["google-workspace-mcp"],
      "env": { ... }
    },
    "everything-else": {
      "type": "http",
      "url": "http://127.0.0.1:52414/mcp/everything-else"
    }
  }
}
```

### Adding New MCPs After Migration

Add new MCPs directly to the hub config, not to claude.json:

```bash
# Edit hub config
slm-hub config show   # See current config

# Or add to ~/.slm-mcp-hub/config.json directly:
{
  "mcpServers": {
    "new-mcp": {
      "command": "npx",
      "args": ["-y", "new-mcp-server"]
    }
  }
}
```

Then add the proxy entry to claude.json:

```json
{
  "new-mcp": {
    "type": "http",
    "url": "http://127.0.0.1:52414/mcp/new-mcp"
  }
}
```

Restart the hub (`slm-hub start`) and Claude Code.

### Running Hub as a Service

#### macOS (launchd)

```bash
# Generate and install launchd plist
slm-hub setup --launchd
```

The hub will auto-start on login and restart on crash.

#### Linux (systemd)

Create `/etc/systemd/user/slm-mcp-hub.service`:

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

## Verifying the Migration

After migration, verify everything works:

1. **Tool names unchanged:** Run `/context` in Claude Code. All MCP tools should show the same names as before (e.g., `mcp__context7__query-docs`).

2. **Tools respond:** Call any tool. The response should be identical.

3. **Hub is proxying:** Check the hub log:
   ```bash
   tail -f ~/.slm-mcp-hub/hub.log
   ```
   You'll see tool calls flowing through the hub.

4. **RAM reduced:** Check process count:
   ```bash
   ps aux | grep -c "mcp"
   ```
   Should be ~39 (38 MCPs + 1 hub) regardless of how many Claude sessions are open.
