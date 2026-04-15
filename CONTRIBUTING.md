# Contributing to SLM MCP Hub

Thank you for your interest in contributing to SLM MCP Hub.

## Development Setup

```bash
git clone https://github.com/qualixar/slm-mcp-hub
cd slm-mcp-hub
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
python -m pytest tests/ -q --cov=slm_mcp_hub
```

Tests must pass with **100% coverage** before any PR is merged.

## Code Standards

- **Python 3.11+** required
- **Frozen dataclasses** for all value objects (`@dataclass(frozen=True)`)
- **Immutability** — create new objects, never mutate existing ones
- **800-line file cap** — split files that approach this limit
- **Type annotations** on all public methods
- **No unused imports** — enforced by linting
- **TDD workflow** — write tests first (RED), implement (GREEN), refactor

## PR Guidelines

1. Create a feature branch from `main`
2. Write tests first
3. Implement the feature
4. Run the full test suite (`python -m pytest tests/ -q --cov=slm_mcp_hub`)
5. Verify 100% coverage
6. Open a PR with a clear description

## Architecture

- `src/slm_mcp_hub/core/` — Hub orchestrator, config, registry, constants
- `src/slm_mcp_hub/federation/` — MCP server connections and routing
- `src/slm_mcp_hub/session/` — Session management and coordination
- `src/slm_mcp_hub/intelligence/` — Cache, cost, filtering, lifecycle, learning
- `src/slm_mcp_hub/security/` — Permissions and audit
- `src/slm_mcp_hub/resilience/` — Auto-restart and health checks
- `src/slm_mcp_hub/observability/` — Tracing and metrics
- `src/slm_mcp_hub/discovery/` — Client detection and network discovery
- `src/slm_mcp_hub/plugins/` — Plugin system (SLM + Mesh)
- `src/slm_mcp_hub/server/` — HTTP transport (FastAPI)
- `src/slm_mcp_hub/storage/` — SQLite persistence
- `src/slm_mcp_hub/cli/` — CLI commands (Click)

## License

By contributing, you agree that your contributions will be licensed under the AGPL-3.0-or-later license.
