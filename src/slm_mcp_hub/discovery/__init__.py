"""Discovery and multi-client auto-setup for SLM MCP Hub."""

from slm_mcp_hub.discovery.auto_register import (
    AutoRegister,
    ImportResult,
    RegistrationPlan,
    RegistrationResult,
)
from slm_mcp_hub.discovery.client_detector import (
    ClientConfig,
    ClientDetector,
    DetectedClient,
)
from slm_mcp_hub.discovery.network import DiscoveredHub, NetworkDiscovery

__all__ = [
    "AutoRegister",
    "ClientConfig",
    "ClientDetector",
    "DetectedClient",
    "DiscoveredHub",
    "ImportResult",
    "NetworkDiscovery",
    "RegistrationPlan",
    "RegistrationResult",
]
