# Getting started

SLM MCP Hub requires Python 3.11 or newer.

## 1. Install

```bash
pip install slm-mcp-hub
slm-hub --version
```

Or install the npm shim, which creates an isolated Python environment and pins
the same release version:

```bash
npm install -g slm-mcp-hub
slm-hub --version
```

## 2. Create or import configuration

```bash
slm-hub config init
slm-hub setup detect
slm-hub setup import ~/.claude.json
slm-hub config show
```

Review imported commands, URLs, headers, and secret placeholders before
starting the process.

## 3. Start the HTTP hub

```bash
slm-hub start
```

The default endpoint is `http://127.0.0.1:52414/mcp` and the health endpoint is
`http://127.0.0.1:52414/api/health`.

Register compact federated routing:

```bash
slm-hub setup register --client claude_code --mode federated
```

Or register direct transparent backend routes:

```bash
slm-hub setup register --client claude_code --mode transparent
```

## 4. Use stdio instead

For clients that launch MCP servers as subprocesses:

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

## 5. Connect SuperLocalMemory

Run the SLM daemon separately and enable the direct plugins in hub config:

```json
{
  "plugins_enabled": ["slm", "mesh"]
}
```

For an authenticated daemon:

```bash
export SLM_DAEMON_URL=http://127.0.0.1:8765
export SLM_API_KEY='your-daemon-api-key'
slm-hub start
```

Do not add the SLM daemon under `mcpServers` when the direct plugins are enabled.

## 6. Transport mode

The default is stateless MCP `2026-07-28`: no session tracking, no resumable
replay. This is the right choice for most deployments.

Enable stateful sessions if you need resumable streaming — for example, when
running long-running backend tools and you want the client↔hub stream to survive
a network blip:

```bash
export SLM_HUB_STATEFUL=1
slm-hub start
```

Or set `transport_stateful: true` in `config.json`.

**What stateful mode adds:** The SDK's `InMemoryEventStore` handles client↔hub
`Last-Event-ID` reconnection automatically. On the hub→backend leg, a one-shot
retry fires when a backend drops mid-stream and had issued a resumption token.
Without a token, the call fails cleanly.

**What stateful mode costs:** Per-client session state in memory; the hub must
be running for clients to resume (unlike stateless, which has no state to recover).

## 7. Verify

```bash
slm-hub status --verbose
slm-hub server list --show-tools
curl http://127.0.0.1:52414/api/health
```

For non-loopback access, configure `SLM_HUB_API_KEY` before changing the host.
See [CONFIGURATION.md](CONFIGURATION.md) and [SECURITY.md](../SECURITY.md).
