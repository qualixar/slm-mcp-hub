"""Pytest conftest — global safety guards.

Prevents the April 26, 2026 incident where tests nuked the user's real
~/.slm-mcp-hub/config.json by calling save_config / generate_default_config
without a path argument.

The autouse fixture below redirects the dynamic SLM_HUB_CONFIG_DIR lookup to
a per-test tmp path for ALL tests automatically, so even tests that forget to
pass an explicit path won't write to the real user config.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the dynamic config directory to tmp for every test.

    The save_config() function also has a runtime guard, but this fixture
    prevents the guard from ever firing in well-behaved tests.
    """
    safe_config = tmp_path / "_isolated" / "config.json"
    safe_config.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SLM_HUB_CONFIG_DIR", str(safe_config.parent))

    return safe_config
