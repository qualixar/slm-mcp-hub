# Security Policy

## SLM MCP Hub Security

### Supported Versions

| Version | Supported |
|:--------|:---------:|
| 0.1.x | Yes |

### Reporting Vulnerabilities

**Do NOT open public issues for security vulnerabilities.**

Email: admin@qualixar.com

Include:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Suggested fix (if any)

We will respond within 48 hours and provide a fix timeline within 7 days.

### Security Architecture

#### Network Security
- Default bind: `127.0.0.1` (localhost only)
- CORS restricted to `http://127.0.0.1` and `http://localhost` by default
- No credentials transmitted in CORS responses
- Session IDs via `Mcp-Session-Id` header

#### Config Security
- Environment variable resolution for secrets (`${VAR}` placeholders)
- Config backups created before any modification (`.pre-hub-backup`)
- SQL injection prevention via table name allowlist and column validation
- Internal error messages never leaked to clients

#### Process Security
- Plugin error isolation (plugin crash never crashes hub)
- PID file management for single-instance enforcement
- Graceful shutdown with pending request cleanup

#### Data Security
- All data stored locally at `~/.slm-mcp-hub/`
- SQLite with WAL mode for concurrent access safety
- No telemetry, no analytics, no phone-home
- Zero cloud dependency in standalone mode
