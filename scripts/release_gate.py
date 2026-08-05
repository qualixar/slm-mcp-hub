#!/usr/bin/env python3
"""Black-box release gate for SLM MCP Hub.

Independent, re-runnable checks over the built artifacts rather than the source
tree — the kind of thing a human would otherwise do by hand before pressing the
publish button:

  1. VERSION CONSISTENCY — the single canonical version (``src/.../core/
     constants.py::VERSION``) must match ``package.json``, ``package-lock.json``,
     ``CITATION.cff``, the built wheel's ``METADATA`` + filename, and the npm
     tarball's ``package.json``. Catches a half-finished version bump.
  2. RUNTIME-DEPENDENCY COMPLETENESS — a fresh virtual-environment install of the
     wheel (declared runtime dependencies only, NO dev extras) must import every
     production module. Catches a module that is imported by shipped code but
     declared only as a dev/test extra (e.g. ``httpx2``), which passes the test
     suite yet breaks a real ``pip install``.

Transport / federation conformance across the full stdio/HTTP/OAuth matrix is
proven separately, with real processes, by ``tests/e2e/``.

Usage:
    python scripts/release_gate.py --mode source
    python scripts/release_gate.py --mode wheel [--artifact dist/<name>.whl]
    python scripts/release_gate.py --mode npm

Exit code 0 = all selected checks passed; non-zero = at least one failed.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Production modules that must import in a clean install. broker/outbound pull in
# httpx2; auth_commands/main are the CLI surface; inbound is the SDK server path.
PROD_IMPORTS = (
    "import slm_mcp_hub",
    "from slm_mcp_hub.auth import broker, provider, callback, token_store",
    "from slm_mcp_hub.protocol import outbound, inbound",
    "from slm_mcp_hub.cli import auth_commands, main",
)


def _canonical_version() -> str:
    text = (ROOT / "src" / "slm_mcp_hub" / "core" / "constants.py").read_text()
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("release_gate: cannot find VERSION in core/constants.py")
    return match.group(1)


def _newest(glob: str) -> Path | None:
    dist = ROOT / "dist"
    if not dist.exists():
        return None
    matches = sorted(dist.glob(glob), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def check_version_consistency() -> list[str]:
    version = _canonical_version()
    errors: list[str] = []

    pkg = json.loads((ROOT / "package.json").read_text())
    if pkg.get("version") != version:
        errors.append(f"package.json version {pkg.get('version')!r} != {version!r}")

    lock = json.loads((ROOT / "package-lock.json").read_text())
    if lock.get("version") != version:
        errors.append(f"package-lock.json version {lock.get('version')!r} != {version!r}")
    root_pkg = lock.get("packages", {}).get("", {})
    if root_pkg.get("version") != version:
        errors.append(f"package-lock.json packages[''] {root_pkg.get('version')!r} != {version!r}")

    cff = (ROOT / "CITATION.cff").read_text()
    cff_match = re.search(r"^version:\s*(\S+)", cff, re.MULTILINE)
    cff_version = cff_match.group(1) if cff_match else None
    if cff_version != version:
        errors.append(f"CITATION.cff version {cff_version!r} != {version!r}")

    wheel = _newest("*.whl")
    if wheel:
        with zipfile.ZipFile(wheel) as archive:
            meta_name = next(n for n in archive.namelist() if n.endswith("METADATA"))
            meta = archive.read(meta_name).decode()
        meta_match = re.search(r"^Version:\s*(\S+)", meta, re.MULTILINE)
        meta_version = meta_match.group(1) if meta_match else None
        if meta_version != version:
            errors.append(f"wheel METADATA version {meta_version!r} != {version!r}")
        if version not in wheel.name:
            errors.append(f"wheel filename {wheel.name!r} lacks {version!r}")

    tgz = _newest("*.tgz")
    if tgz:
        with tarfile.open(tgz) as archive:
            member = archive.extractfile("package/package.json")
            npm_pkg = json.load(member) if member else {}
        if npm_pkg.get("version") != version:
            errors.append(f"npm tarball version {npm_pkg.get('version')!r} != {version!r}")

    checked = ["package.json", "package-lock.json", "CITATION.cff"]
    if wheel:
        checked.append(f"wheel({wheel.name})")
    if tgz:
        checked.append(f"npm({tgz.name})")
    print(f"[version] canonical={version}; matched: {', '.join(checked)}")
    return errors


def check_runtime_deps(wheel: Path) -> list[str]:
    uv = shutil.which("uv")
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        try:
            if uv:
                subprocess.run(
                    [uv, "venv", str(venv), "--python", "3.14"],
                    check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    [uv, "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)],
                    check=True, capture_output=True, text=True,
                )
            else:
                subprocess.run(
                    [sys.executable, "-m", "venv", str(venv)],
                    check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    [str(venv / "bin" / "pip"), "install", str(wheel)],
                    check=True, capture_output=True, text=True,
                )
        except subprocess.CalledProcessError as exc:
            return [f"fresh-venv install failed: {exc.stderr or exc}"]

        code = "; ".join(PROD_IMPORTS) + "; import slm_mcp_hub; print(slm_mcp_hub.__version__)"
        result = subprocess.run(
            [str(venv / "bin" / "python"), "-c", code],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return [
                "fresh-venv install imports a module NOT covered by declared runtime "
                f"dependencies:\n{result.stderr.strip()}"
            ]
        print(f"[deps] fresh-venv install imported all production modules "
              f"(reported version {result.stdout.strip()})")
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Black-box release gate for SLM MCP Hub")
    parser.add_argument("--mode", choices=["source", "wheel", "npm"], default="source")
    parser.add_argument(
        "--artifact", type=Path,
        help="wheel path (wheel mode); defaults to the newest dist/*.whl",
    )
    args = parser.parse_args()

    errors = check_version_consistency()

    if args.mode == "wheel":
        wheel = args.artifact or _newest("*.whl")
        if not wheel or not wheel.exists():
            errors.append("no wheel found: run `python -m build` or pass --artifact")
        else:
            errors += check_runtime_deps(wheel)

    if errors:
        print("\nRELEASE GATE: FAIL")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("\nRELEASE GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
