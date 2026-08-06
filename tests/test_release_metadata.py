"""Release metadata must remain coherent across both distribution channels."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import slm_mcp_hub
from slm_mcp_hub.core.constants import VERSION
from slm_mcp_hub.plugins.mesh_plugin import MeshPlugin
from slm_mcp_hub.plugins.slm_plugin import SLMPlugin

ROOT = Path(__file__).resolve().parents[1]


def test_all_runtime_versions_match_npm_manifest() -> None:
    npm_version = json.loads((ROOT / "package.json").read_text())["version"]

    assert npm_version == VERSION
    assert slm_mcp_hub.__version__ == VERSION
    assert SLMPlugin().version == VERSION
    assert MeshPlugin().version == VERSION


def test_public_metadata_uses_live_repository_urls_and_contact() -> None:
    package = json.loads((ROOT / "package.json").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert package["homepage"] == "https://github.com/qualixar/slm-mcp-hub"
    assert "varun@qualixar.com" not in json.dumps(package)
    assert "qualixar.com/docs/slm-mcp-hub" not in pyproject
    assert "varun@qualixar.com" not in pyproject


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_pyproject_declares_v030_runtime_dependencies() -> None:
    """P01 dependency policy: the MCP SDK, keychain, and cross-process lock are
    production dependencies, not leaf/dev extras."""
    normalized = {dep.replace(" ", "") for dep in _pyproject()["project"]["dependencies"]}
    # Exact pins, including upper caps, so a loosened bound cannot pass silently.
    assert "mcp==2.0.0" in normalized, normalized
    assert "keyring>=25.7,<26" in normalized, normalized
    assert "filelock>=3.32,<4" in normalized, normalized
    # httpx2 is imported by production code (auth/broker.py, protocol/outbound.py),
    # so it must be a runtime dependency, NOT a dev-only extra — otherwise a plain
    # `pip install slm-mcp-hub` breaks with ModuleNotFoundError at OAuth/federation time.
    assert "httpx2>=2.9,<3" in normalized, normalized


def test_no_extra_depends_on_superlocalmemory() -> None:
    """The SLM and mesh plugins reach the daemon over HTTP (SLM_DAEMON_URL) and
    import no Python package — `grep -r 'import superlocalmemory' src/` is empty.

    Up to v0.3.1 the `slm`, `mesh`, and `full` extras still declared
    `superlocalmemory>=3.4.0`, a leftover of the v0.1.0 plugin that called
    `superlocalmemory.get_engine()` before being rewritten to HTTP. The cost was
    real: `pip install slm-mcp-hub[full]` resolved an entire transformer stack
    (sentence-transformers, transformers, onnxruntime) and could shadow a user's
    own SuperLocalMemory install with an older one.

    This is a mechanical guard, not a style check. Re-adding the dependency to
    any extra fails here rather than shipping.
    """
    extras = _pyproject()["project"].get("optional-dependencies", {})
    offenders = {
        name: [dep for dep in deps if "superlocalmemory" in dep.lower()]
        for name, deps in extras.items()
    }
    assert not any(offenders.values()), (
        "No extra may depend on superlocalmemory — the plugins are HTTP clients. "
        f"Offending extras: { {k: v for k, v in offenders.items() if v} }"
    )


def test_full_extra_is_the_union_of_real_extras() -> None:
    """`full` is the name users type. It must stay installable and must resolve
    to exactly the extras the hub actually uses — no more, no less."""
    extras = _pyproject()["project"]["optional-dependencies"]
    expected = {dep.replace(" ", "") for dep in extras["network"] + extras["observability"]}
    assert {dep.replace(" ", "") for dep in extras["full"]} == expected


def test_pyproject_targets_python_314() -> None:
    classifiers = _pyproject()["project"]["classifiers"]
    assert "Programming Language :: Python :: 3.14" in classifiers


def test_coverage_floor_at_least_97() -> None:
    """P01 raises the global floor 96 -> 97 so CI and the M4 gate agree. The
    final shipped release enforces a higher bar; this is the minimum."""
    fail_under = _pyproject()["tool"]["coverage"]["report"]["fail_under"]
    assert fail_under >= 97
