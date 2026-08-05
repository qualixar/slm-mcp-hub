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


def test_pyproject_targets_python_314() -> None:
    classifiers = _pyproject()["project"]["classifiers"]
    assert "Programming Language :: Python :: 3.14" in classifiers


def test_coverage_floor_at_least_97() -> None:
    """P01 raises the global floor 96 -> 97 so CI and the M4 gate agree. The
    final shipped release enforces a higher bar; this is the minimum."""
    fail_under = _pyproject()["tool"]["coverage"]["report"]["fail_under"]
    assert fail_under >= 97
