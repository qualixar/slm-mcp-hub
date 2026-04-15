# Getting Started with SLM MCP Hub

## What This Does

SLM MCP Hub runs all your MCP servers in **one process** and shares them across every AI client session. Instead of each Claude Code window spawning its own 38 MCP processes, they all connect to one hub.

**Before hub:** 5 sessions x 38 MCPs = 190 processes, ~9GB RAM, 30s startup per session
**After hub:** 38 MCPs + 1 hub = 39 processes, ~2GB RAM, instant new sessions

## Install

```bash
pip install slm-mcp-hub
```

Or via npm:

```bash
npm install -g slm-mcp-hub
```

## Step 1: Initialize

```bash
slm-hub config init
```

Creates `~/.slm-mcp-hub/config.json` with default settings.

## Step 2: Import Your MCPs

```bash
# From Claude Code
slm-hub setup import ~/.claude.json

# From VS Code
slm-hub setup import ~/Library/Application\ Support/Code/User/settings.json --format vscode

# From Cursor
slm-hub setup import ~/Library/Application\ Support/Cursor/User/settings.json --format vscode
```

The hub reads your MCP definitions and stores them in its own config. Your original files are not modified.

## Step 3: Start the Hub

```bash
slm-hub start
```

You'll see:
```
SLM MCP Hub v0.1.0 running on http://127.0.0.1:52414/mcp
  MCP servers: 36/36 connected
  Tools: 313
  Plugins: 2 (slm, mesh)
```

## Step 4: Connect Claude Code

### Federated Mode (Recommended)

One entry. 3 meta-tools. Maximum token savings.

```json
{
  "mcpServers": {
    "hub": {
      "type": "http",
      "url": "http://127.0.0.1:52414/mcp"
    }
  }
}
```

### Automatic Setup

```bash
slm-hub setup detect                                          # See what's installed
slm-hub setup register --client claude_code --mode federated  # Apply
```

A backup is created automatically at `~/.claude.json.pre-hub-backup`.

## Step 5: Restart Claude Code

All tools available through `hub__search_tools` and `hub__call_tool`.

## Step 6: Verify

```bash
slm-hub status
curl http://127.0.0.1:52414/api/health
```

## Rollback

```bash
cp ~/.claude.json.pre-hub-backup ~/.claude.json
```

One command. Original config restored. Restart Claude Code.

---

## With SuperLocalMemory (Recommended)

If you have [SuperLocalMemory](https://superlocalmemory.com) installed, the hub automatically connects to it:

```bash
# Install SLM if you don't have it
npm install -g superlocalmemory
slm start   # Start the daemon
```

The hub discovers the SLM daemon at `localhost:8765` on startup. No configuration needed:

```
SLM MCP Hub v0.1.0 running on http://127.0.0.1:52414/mcp
  MCP servers: 37/37 connected
  Tools: 345
  Plugins: 2 (slm, mesh)
  SLM: connected (mode=b, 4461 facts)
```

### What You Get

**SLM Plugin (automatic):**
- Every tool call logged to SLM's learning pipeline
- Past context recalled on session start
- Session summaries persisted for future sessions
- Predictive warm-up — hub knows which MCPs you'll need

**Mesh Plugin (automatic):**
- Hub registered as a mesh peer
- Tool usage broadcast to other sessions
- Distributed locking for conflict prevention

### SLM as a Hub Backend

You can run SLM itself through the hub (it's just another MCP):

```bash
slm-hub setup import ~/.claude.json   # Imports SLM along with everything else
```

SLM's 32 tools become available via `hub__call_tool("superlocalmemory__recall", {...})`. The SLM plugin handles session lifecycle automatically — no separate hooks needed.

---

## FAQ

### Hooks and Skills?
Unaffected. Hooks are shell commands, skills are prompt templates. They don't interact with MCP transport.

### Agent Teams?
When Claude spawns agents, they inherit `~/.claude.json`. Each agent connects to the hub. Transparent.

### My API Keys?
The hub loads `~/.claude-secrets.env` on startup — the same file Claude Code uses. All `${VAR}` placeholders resolve correctly.

### Multiple Claude Sessions?
This is the main benefit. All sessions share the same hub. No process duplication.

---

## Next Steps

- [Architecture Guide](ARCHITECTURE.md) — Two modes, plugin system, tool call flow
- [Migration Guide](MIGRATION-GUIDE.md) — Detailed migration from direct MCPs
- [Configuration Reference](CONFIGURATION.md) — All settings explained
