# Security Policy

## SLM MCP Hub Security

### Supported Versions

| Version | Supported |
|:--------|:---------:|
| 0.2.x | Yes |
| 0.1.x | No |

### Reporting Vulnerabilities

**Do NOT open public issues for security vulnerabilities.**

Email: varun.pratap.bhardwaj@gmail.com

Include:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Suggested fix (if any)

Please include a safe contact method for coordinated disclosure. Response time
depends on severity and maintainer availability; no fixed SLA is promised.

### Security Architecture

#### Network Security
- Default bind: `127.0.0.1` (localhost only)
- CORS restricted to `http://127.0.0.1` and `http://localhost` by default
- Non-loopback binds require `SLM_HUB_API_KEY`
- MCP and management routes accept `X-SLM-Hub-API-Key` or a Bearer token
- No credentials transmitted in CORS responses
- Session IDs via `Mcp-Session-Id` header

#### Config Security
- Environment variable resolution for secrets (`${VAR}` placeholders) occurs
  only at the connection boundary; persisted config and snapshots retain the
  placeholder
- Versioned config snapshots are created before non-trivial modifications
- SQL injection prevention via table name allowlist and column validation
- Internal error messages never leaked to clients

If a release before v0.2.6 persisted a resolved secret, rotate the credential
and remove affected configuration and snapshot copies. Automatic cleanup would
risk deleting legitimate literal values and cannot reconstruct the original
environment-variable name.

#### Process Security
- Plugin error isolation (plugin crash never crashes hub)
- PID file management for single-instance enforcement
- Graceful shutdown with pending request cleanup

#### Data Security
- All data stored locally at `~/.slm-mcp-hub/`
- SQLite with WAL mode for concurrent access safety
- No telemetry, no analytics, no phone-home
- Zero cloud dependency in standalone mode
