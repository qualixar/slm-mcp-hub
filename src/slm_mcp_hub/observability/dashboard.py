"""W5-P3 — Dashboard HTML renderer for SLM MCP Hub admin interface.

Renders a static HTML page showing the 6-signal status table for all backends.
Auto-refreshes every 10 seconds via <meta http-equiv="refresh" content="10">.

SECURITY NOTE — XSS prevention:
  ALL backend-derived string values (names, lifecycle states, etc.) are
  HTML-escaped via html.escape() BEFORE insertion into the page.
  Python f-strings and str.format() do NOT HTML-escape — callers of this
  module MUST NOT assume escaping happens at template-expansion time.

  [CITATION-VERIFIED] html.escape() behaviour for attribute context (Python 3.x):
    - html.escape(s, quote=True)  escapes &, <, >, ", '  — safe for both
      text content and double-quoted HTML attribute values.
    - html.escape(s, quote=False) escapes &, <, >           — text content only.
    - We use quote=True throughout as a belt-and-suspenders measure even
      when inserting into text content, in case a template is later modified
      to use attribute context.

Design:
  - render_dashboard_html() is a pure function — no I/O, no side effects.
  - _fmt_uptime() and _fmt_ram() are module-level helpers (importable for TDD).
  - The page template is split into _PAGE_HEADER / _PAGE_FOOTER to avoid
    Python format-string conflicts with CSS curly braces.
"""

from __future__ import annotations

import html
from typing import Any

# ---------------------------------------------------------------------------
# Em dash constant (U+2014) — used for "not available" display values
# ---------------------------------------------------------------------------
_EMDASH = "—"

# ---------------------------------------------------------------------------
# Page template — split to avoid CSS curly-brace format conflicts
# ---------------------------------------------------------------------------

_PAGE_HEADER = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="10">
    <title>SLM MCP Hub — Dashboard</title>
    <style>
        body {
            font-family: monospace;
            padding: 1rem;
            background: #fff;
            color: #222;
        }
        h1 { font-size: 1.2rem; margin-bottom: 0.4rem; }
        p.note { margin: 0.2rem 0 1rem; color: #666; font-size: 0.9rem; }
        table { border-collapse: collapse; width: 100%; }
        th, td {
            border: 1px solid #ccc;
            padding: 0.4rem 0.8rem;
            text-align: left;
        }
        th { background: #f0f0f0; font-weight: bold; }
        .connected { color: #007700; }
        .attention { color: #cc0000; font-weight: bold; }
    </style>
</head>
<body>
    <h1>SLM MCP Hub — Dashboard</h1>
    <p class="note">Auto-refreshes every 10 seconds.</p>
    <table>
        <thead>
            <tr>
                <th>Name</th>
                <th>State</th>
                <th>Uptime</th>
                <th>Restarts</th>
                <th>P95 ms</th>
                <th>RAM</th>
            </tr>
        </thead>
        <tbody>
"""

_PAGE_FOOTER = """\
        </tbody>
    </table>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public helpers (importable for direct unit testing per LLD §12 W5-P3)
# ---------------------------------------------------------------------------


def _fmt_uptime(seconds: float) -> str:
    """Format uptime seconds as human-readable string.

    Examples:
        _fmt_uptime(3661)  → '1h 1m'
        _fmt_uptime(90)    → '1m 30s'
        _fmt_uptime(45)    → '45s'
        _fmt_uptime(0)     → '—'
        _fmt_uptime(-1)    → '—'

    Args:
        seconds: Uptime in seconds. Non-positive values return the em dash.

    Returns:
        Human-readable uptime string.
    """
    if seconds <= 0:
        return _EMDASH
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _fmt_ram(ram_bytes: int | None) -> str:
    """Format RAM bytes to a compact MB string.

    Examples:
        _fmt_ram(12_582_912) → '12.0 MB'
        _fmt_ram(1_048_576)  → '1.0 MB'
        _fmt_ram(None)       → '—'

    Args:
        ram_bytes: RSS bytes, or None when psutil is absent or the backend
            is an HTTP server (no subprocess to measure).

    Returns:
        '12.0 MB' style string, or '—' when None.
    """
    if ram_bytes is None:
        return _EMDASH
    return f"{ram_bytes / 1_048_576:.1f} MB"


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------


def render_dashboard_html(status_entries: list[dict[str, Any]]) -> str:
    """Render the admin dashboard as a complete HTML page.

    SECURITY: Every backend-derived value is HTML-escaped via html.escape()
    before inclusion in the page. This prevents XSS via a backend whose name
    or state contains HTML/script tags. Python f-strings do NOT auto-escape.

    Sorting: entries are displayed sorted alphabetically by name.

    Args:
        status_entries: List of enriched status dicts — typically the output
            of enrich_server_status(). Expected keys per entry:
            - name           (str)
            - lifecycle      (str)
            - uptime_seconds (float)
            - restart_count  (int)
            - p95_latency_ms (float)
            - ram_bytes      (int or None)
            - needs_attention (bool)
            Missing keys degrade to safe defaults.

    Returns:
        A complete HTML document as a plain Python string.
    """
    sorted_entries = sorted(status_entries, key=lambda e: str(e.get("name", "")))
    rows: list[str] = []

    for entry in sorted_entries:
        # --- Escape ALL backend-derived strings before HTML insertion ---
        # html.escape(s, quote=True): covers text content AND attribute context.
        # Applied to every value that originates from external backend data.
        name = html.escape(str(entry.get("name", "")), quote=True)
        lifecycle = html.escape(str(entry.get("lifecycle", _EMDASH)), quote=True)
        uptime = html.escape(
            _fmt_uptime(float(entry.get("uptime_seconds", 0.0))), quote=True
        )
        restarts = html.escape(str(entry.get("restart_count", 0)), quote=True)
        p95 = html.escape(
            f"{float(entry.get('p95_latency_ms', 0.0)):.1f}", quote=True
        )
        ram = html.escape(_fmt_ram(entry.get("ram_bytes")), quote=True)

        # css_class is derived from our own bool logic — no user data, no escape needed
        needs_attention: bool = bool(entry.get("needs_attention", False))
        css_class = "attention" if needs_attention else "connected"

        # Build the row using f-string interpolation of already-escaped values.
        # All variables below have been through html.escape() above.
        row = (
            "            <tr>\n"
            f"                <td class=\"{css_class}\">{name}</td>\n"
            f"                <td>{lifecycle}</td>\n"
            f"                <td>{uptime}</td>\n"
            f"                <td>{restarts}</td>\n"
            f"                <td>{p95}</td>\n"
            f"                <td>{ram}</td>\n"
            "            </tr>\n"
        )
        rows.append(row)

    return _PAGE_HEADER + "".join(rows) + _PAGE_FOOTER
