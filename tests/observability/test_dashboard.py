"""W5-P3 TDD — Dashboard HTML renderer tests.

TDD: written BEFORE implementation. Tests MUST FAIL until
observability/dashboard.py is created.

Test plan (per LLD §12 W5-P3):
1. render_dashboard_html() contains all backend names from status_entries.
2. _fmt_uptime(3661) == '1h 1m'; _fmt_uptime(0) == '—'.
3. _fmt_ram(12_582_912) == '12.0 MB'; _fmt_ram(None) == '—'.
4. needs_attention=True renders with CSS class 'attention', not 'connected'.
5. Backends sorted alphabetically by name in rendered HTML.
6. [SECURITY] '<script>' in backend name is HTML-escaped — unescaped '<script>'
   MUST NOT appear in output. html.escape('<script>') == '&lt;script&gt;'.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_entry(
    name: str = "srv-a",
    lifecycle: str = "connected",
    uptime_seconds: float = 300.0,
    restart_count: int = 0,
    p95_latency_ms: float = 12.5,
    ram_bytes: int | None = None,
    needs_attention: bool = False,
) -> dict[str, Any]:
    """Build a minimal enriched status entry for testing."""
    return {
        "name": name,
        "lifecycle": lifecycle,
        "uptime_seconds": uptime_seconds,
        "restart_count": restart_count,
        "p95_latency_ms": p95_latency_ms,
        "ram_bytes": ram_bytes,
        "needs_attention": needs_attention,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRenderDashboardHtml:
    def test_render_dashboard_html_contains_backend_names(self) -> None:
        """render_dashboard_html() output contains all backend names from status_entries."""
        from slm_mcp_hub.observability.dashboard import render_dashboard_html

        entries = [
            _make_entry(name="backend-alpha"),
            _make_entry(name="backend-beta"),
        ]
        html_output = render_dashboard_html(entries)

        assert "backend-alpha" in html_output
        assert "backend-beta" in html_output

    def test_render_dashboard_html_fmt_uptime(self) -> None:
        """_fmt_uptime(3661) == '1h 1m'. _fmt_uptime(0) == '—'."""
        from slm_mcp_hub.observability.dashboard import _fmt_uptime

        assert _fmt_uptime(3661) == "1h 1m"
        assert _fmt_uptime(0) == "—"

    def test_render_dashboard_html_fmt_ram(self) -> None:
        """_fmt_ram(12_582_912) == '12.0 MB'. _fmt_ram(None) == '—'."""
        from slm_mcp_hub.observability.dashboard import _fmt_ram

        assert _fmt_ram(12_582_912) == "12.0 MB"
        assert _fmt_ram(None) == "—"

    def test_render_dashboard_html_needs_attention_class(self) -> None:
        """An entry with needs_attention=True renders with CSS class 'attention', not 'connected'."""
        from slm_mcp_hub.observability.dashboard import render_dashboard_html

        entries = [
            _make_entry(name="healthy-srv", needs_attention=False),
            _make_entry(name="broken-srv", needs_attention=True),
        ]
        html_output = render_dashboard_html(entries)

        # Broken server row must use class 'attention'
        assert 'class="attention"' in html_output, (
            "Expected class='attention' for needs_attention=True entry"
        )
        # Healthy server row must use class 'connected'
        assert 'class="connected"' in html_output, (
            "Expected class='connected' for needs_attention=False entry"
        )

    def test_render_dashboard_html_sorted_by_name(self) -> None:
        """Backends are sorted alphabetically by name in the rendered HTML."""
        from slm_mcp_hub.observability.dashboard import render_dashboard_html

        entries = [
            _make_entry(name="zebra-srv"),
            _make_entry(name="alpha-srv"),
            _make_entry(name="mango-srv"),
        ]
        html_output = render_dashboard_html(entries)

        alpha_pos = html_output.find("alpha-srv")
        mango_pos = html_output.find("mango-srv")
        zebra_pos = html_output.find("zebra-srv")

        assert alpha_pos != -1, "alpha-srv not found in HTML"
        assert mango_pos != -1, "mango-srv not found in HTML"
        assert zebra_pos != -1, "zebra-srv not found in HTML"
        assert alpha_pos < mango_pos < zebra_pos, (
            f"Expected alpha({alpha_pos}) < mango({mango_pos}) < zebra({zebra_pos})"
        )

    def test_render_dashboard_html_xss_name_escaped(self) -> None:
        """[SECURITY] A backend name containing '<script>' is HTML-escaped.
        Assert '<script>' does NOT appear unescaped in rendered HTML.
        NOTE: Python f-strings do NOT HTML-escape — html.escape() MUST be used.
        [CITATION-VERIFIED]: html.escape('<script>') == '&lt;script&gt;' in Python 3.x
        """
        from slm_mcp_hub.observability.dashboard import render_dashboard_html

        xss_name = "<script>alert('xss')</script>"
        entries = [_make_entry(name=xss_name)]
        html_output = render_dashboard_html(entries)

        # The unescaped '<script>' tag MUST NOT appear in the output
        assert "<script>" not in html_output, (
            "XSS VULNERABILITY: unescaped '<script>' found in dashboard HTML output. "
            "All backend-derived strings MUST be HTML-escaped via html.escape(). "
            "Python f-strings / str.format() do NOT escape HTML."
        )
        # The escaped form MUST appear — proves the name is in the output, just safe
        assert "&lt;script&gt;" in html_output, (
            "Expected '&lt;script&gt;' (html.escape result) in HTML but not found. "
            "The backend name must appear in the page as escaped text."
        )
