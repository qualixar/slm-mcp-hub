"""SLM MCP Hub — Observability package.

Re-exports for easy top-level import:

    from slm_mcp_hub.observability import enrich_server_status, MetricsCollector
    from slm_mcp_hub.observability import EventStreamBridge
    from slm_mcp_hub.observability import render_dashboard_html
"""

from slm_mcp_hub.observability.dashboard import render_dashboard_html
from slm_mcp_hub.observability.event_stream import EventStreamBridge
from slm_mcp_hub.observability.metrics import MetricsCollector, ServerMetrics
from slm_mcp_hub.observability.status_enriched import enrich_server_status

__all__ = [
    "EventStreamBridge",
    "MetricsCollector",
    "ServerMetrics",
    "enrich_server_status",
    "render_dashboard_html",
]
