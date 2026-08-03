# SLM MCP Hub v0.2.6 Credibility Reset

Status: approved for implementation on 2026-08-03.

## Verified baseline

- GitHub has three open issues and nine open pull requests. None has a review or check run.
- Issue #11 is reproducible on `main`: loading and saving a configuration replaces
  `${VAR}` placeholders with resolved values in `env`, `headers`, `url`, and `args`.
  Snapshots can therefore copy resolved secrets.
- The source suite passes 665 tests with three warnings, but the configured coverage
  gate fails at 86.55% against `fail_under = 100`.
- `npm test` is an echo-only false green.
- PyPI and npm publish 0.2.5 while GitHub releases stop at v0.2.3. The installed
  package's CLI reports 0.2.5 while `slm_mcp_hub.__version__` reports 0.1.2.
- The repository has no CI workflow or branch protection. Secret and code scanning
  are disabled or unavailable.
- The published documentation URLs return 404. Public numerical, comparative, and
  "world's first" claims lack an adjacent reproducible evidence bundle.
- PR #13 contains PR #12. PRs #13, #14, and #9 are the focused compatibility
  candidates. PRs #1, #5, #6, #7, and #15 require replacement, redesign, or deferral.

## Release objective

Ship one verifiable v0.2.6 release in which configuration values remain secret,
supported MCP clients can initialize and call tools safely, the SLM plugin works in
both trusted-loopback and authenticated deployments, and every public artifact has
the same version and provenance.

"Best for everyone" is not used as a gate because it cannot be falsified. The release
gate is instead expressed as executable compatibility, security, packaging, and
runtime contracts.

## Architecture decisions

### AD-1: Canonical unresolved configuration

The persisted representation remains unresolved and is the single source of truth.
Environment expansion produces a derived runtime view only. Save and snapshot paths
must never receive the derived view. This is the **materialize-at-boundary pattern**:
durable configuration describes intent; process launch and HTTP connection boundaries
materialize secrets immediately before use.

Acceptance tests cover placeholders in environment maps, headers, URLs, arguments,
CLI mutations, hot reload, snapshots, missing variables, and programmatic configs.
Tests use sentinels, never real credentials.

### AD-2: Explicit authentication contract

The SLM plugin accepts an optional API key through environment-backed configuration,
adds the supported authentication header without logging its value, and is tested
against a server that rejects unauthenticated writes. Loopback-trusted deployments
remain supported. SLM stays a direct sibling of the Hub and is never nested inside
the Hub's federation config.

### AD-3: Defensive protocol normalization

Malformed or flattened client inputs are normalized only where the repair is
unambiguous. Invalid shapes return JSON-RPC client errors rather than internal errors.
Repairs log paths and client identity, never values. PR #13 supersedes #12; PR #14
adds initialize validation; PR #9 gates optional discovery by advertised capability.

Unknown session IDs are not silently authorized. Stateless support follows the
official MCP stateless work as a separate explicit mode with its own conformance and
security tests.

### AD-4: Risk-based coverage

The release will not game a global 100% line metric. Security-critical configuration
paths and changed code require complete behavioral coverage. The repository-wide gate
must be truthful, enforced in CI, and ratcheted upward from an achieved minimum of 90%.

### AD-5: Single release identity

Package metadata is the authoritative version source. Runtime and CLI versions derive
from installed metadata with a source-tree fallback. Python, npm, Git tags, GitHub
releases, changelog, and runtime output must agree.

## Workstreams

1. Write failing regression tests for issue #11, then implement canonical unresolved
   persistence and boundary materialization.
2. Write an authenticated-daemon test for issue #10, then add secret-safe SLM headers.
3. Port and independently verify PRs #13, #14, and #9 in dependency order.
4. Eliminate test warnings and add behavioral tests until the honest coverage floor
   is met.
5. Add CI for supported Python versions and operating systems, dependency audit,
   package-content checks, npm wrapper checks, and clean-install smoke tests.
6. Remove generated/backup artifacts, pin npm-to-Python package versions, fail loudly
   on install errors, and remove unsafe system-package fallbacks.
7. Replace broken URLs and unsupported public claims; correct contact metadata.
8. Reconstruct missing v0.2.4/v0.2.5 GitHub provenance, then release v0.2.6.
9. Triage every open issue and PR with an evidence-based comment and disposition.
10. Require independent Terra, Python, and general code reviews before release.

## Release gate

- All source tests pass with no runtime warnings.
- The enforced coverage threshold passes, with security-critical changed paths fully
  exercised.
- Clean wheel, sdist, and npm installations pass in isolated environments.
- The built artifacts contain no bytecode, backup files, databases, caches, or nested
  package tarballs.
- CLI, HTTP initialize, tool routing, capability discovery, session handling, SLM
  authentication, and placeholder persistence smoke tests pass.
- Dependency audit has no unacknowledged high or critical finding.
- CI is green and required on protected `main`.
- Public URLs resolve and public claims have evidence or are removed.
- Version, tag, release, registry, changelog, and runtime identity match exactly.
- Terra, Python, and general code review have no unresolved critical or high finding.

No release is complete until installed artifacts and the live registries are tested;
passing source tests alone is insufficient.
