# SLM MCP Hub

[![PyPI](https://img.shields.io/pypi/v/slm-mcp-hub)](https://pypi.org/project/slm-mcp-hub/)
[![npm](https://img.shields.io/npm/v/slm-mcp-hub)](https://www.npmjs.com/package/slm-mcp-hub)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

SLM MCP Hub is a local-first gateway for Model Context Protocol servers. It
connects stdio and HTTP MCP servers once, exposes them through one endpoint,
and offers either direct transparent routing or a compact set of discovery and
call tools.

It is part of Qualixar's work on AI Reliability Engineering. The project is
alpha software: test it with your own MCP clients and report reproducible
failures through [GitHub Issues](https://github.com/qualixar/slm-mcp-hub/issues).

## What it does

- Federates stdio, Streamable HTTP, and SSE backends.
- Serves clients over Streamable HTTP or stdio.
- Supports transparent per-server proxy routes and compact federated routing.
- Hot-adds, removes, modifies, reconnects, and reloads backend servers.
- Preserves `${VAR}` secret placeholders when configuration is saved or
  snapshotted; values are materialized only when a backend connection starts.
- Connects directly to SuperLocalMemory and SLM Mesh when their plugins are
  enabled. The SLM daemon remains a sibling service, not a server nested inside
  the hub's federation graph.
- Supports legacy stateful MCP clients, optional legacy stateless mode, and the
  stateless core of MCP `2026-07-28` including `server/discover` and per-request
  client metadata validation.

## Install

Python 3.11 or newer is required.

```bash
pip install slm-mcp-hub
```

The npm package installs the exact matching Python release into an isolated
environment owned by the npm package:

```bash
npm install -g slm-mcp-hub
```

The npm and Python versions are release-locked. Installation fails instead of
silently falling back to a different version or modifying an externally managed
Python installation.

## Quick start

```bash
slm-hub config init
slm-hub setup detect
slm-hub setup import ~/.claude.json
slm-hub start
```

The default HTTP endpoint is `http://127.0.0.1:52414/mcp`.

For a native stdio connection:

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

Federated mode exposes three compact tools:

- `search_tools` finds tools across connected servers.
- `call_tool` invokes a namespaced tool returned by the search.
- `list_servers` reports connected backends.

Transparent mode gives each backend a direct route:

```text
http://127.0.0.1:52414/mcp/{server-name}
```

Register either mode with a supported client:

```bash
slm-hub setup register --client claude_code --mode federated
slm-hub setup register --client claude_code --mode transparent
```

Use federated mode when context size matters. Use transparent mode when a
client needs the backend's original tool surface.

## Configuration

The default file is `~/.slm-mcp-hub/config.json`. Override its directory with
`SLM_HUB_CONFIG_DIR`.

```json
{
  "host": "127.0.0.1",
  "port": 52414,
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      }
    },
    "remote": {
      "type": "http",
      "url": "${REMOTE_MCP_URL}",
      "headers": {
        "Authorization": "Bearer ${REMOTE_MCP_TOKEN}"
      }
    }
  },
  "plugins_enabled": ["slm", "mesh"]
}
```

Place secret values in the process environment or in
`~/.slm-mcp-hub/secrets.env`. The hub persists the placeholders shown above,
not their resolved values. If an older release already wrote a literal secret
into `config.json` or `snapshots/`, rotate that credential and remove the
contaminated copies manually; the hub cannot reliably reconstruct a lost
environment-variable name.

## SuperLocalMemory

Run SuperLocalMemory as its own daemon, then enable the direct hub plugins:

```bash
export SLM_DAEMON_URL=http://127.0.0.1:8765
export SLM_API_KEY='your-daemon-api-key'
```

`SLM_API_KEY` is sent as `X-SLM-API-Key` by both the memory and mesh plugins.
Authentication failures disable the affected plugin and remain visible in
logs without printing the key. Restart the hub after rotating the daemon key.

Do not add the SLM daemon to `mcpServers` when using these plugins. That creates
a misleading nested topology and is not the supported integration path.

## Stateless clients

Modern MCP `2026-07-28` HTTP requests are handled without a protocol session.
They must send matching protocol versions in the `MCP-Protocol-Version` header
and `params._meta`, plus per-request client information and capabilities.

For older clients that cannot retain `Mcp-Session-Id`, enable compatibility
mode:

```bash
export SLM_HUB_STATELESS=1
slm-hub start
```

Legacy stateful mode remains the default for older protocol versions. Optional
restart recovery can re-adopt a client-supplied session identifier:

```bash
export SLM_HUB_SESSION_RECOVERY=1
```

Recovery is off by default. At capacity, the hub refuses recovery rather than
evicting an unrelated live session.

## Remote access security

The default loopback bind is the safest deployment. A non-loopback host is
refused unless `SLM_HUB_API_KEY` is set:

```bash
export SLM_HUB_HOST=0.0.0.0
export SLM_HUB_API_KEY='generate-a-long-random-value'
slm-hub start
```

Clients must send either `X-SLM-Hub-API-Key` or
`Authorization: Bearer <key>`. Authentication covers `/mcp`, transparent MCP
routes, and management APIs; `/api/health` remains available for health checks.
Use TLS at the network boundary whenever traffic leaves the host.

## Development and verification

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest --cov=slm_mcp_hub
npm test
```

The release gate requires more than 95% Python line coverage, clean linting,
wheel/sdist/npm package inspection, isolated install tests, dependency audits,
and supported-Python CI.

Architecture, configuration, migration, and getting-started details are in
the [docs directory](docs/).

## Contributing

Reproduction tests are strongly preferred with bug reports. Pull requests must
keep both distribution channels version-aligned and pass all release gates.
See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
