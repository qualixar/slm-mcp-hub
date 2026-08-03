"""Release metadata must remain coherent across both distribution channels."""

from __future__ import annotations

import json
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
