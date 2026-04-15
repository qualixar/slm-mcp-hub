"""Network discovery for SLM MCP Hub via Zeroconf/mDNS."""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

from slm_mcp_hub.core.constants import VERSION

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_slm-mcp-hub._tcp.local."

# Check if zeroconf is available
_zeroconf_available = False
try:
    from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

    _zeroconf_available = True  # pragma: no cover
except ImportError:
    pass


def is_zeroconf_available() -> bool:
    """Check if zeroconf package is installed."""
    return _zeroconf_available


@dataclass(frozen=True)
class DiscoveredHub:
    """A hub instance found on the network."""

    host: str
    port: int
    version: str
    mcp_count: int
    hostname: str
    address: str


class _DiscoveryListener:
    """Internal listener for Zeroconf service browser."""

    def __init__(self) -> None:
        self.discovered: list[dict[str, Any]] = []

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is not None:
            self.discovered.append(self._extract_info(info))

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        pass

    @staticmethod
    def _extract_info(info: Any) -> dict[str, Any]:
        props = {}
        if info.properties:
            for key, val in info.properties.items():
                k = key.decode("utf-8") if isinstance(key, bytes) else key
                v = val.decode("utf-8") if isinstance(val, bytes) else str(val)
                props[k] = v

        addresses = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
        address = addresses[0] if addresses else "unknown"

        return {
            "host": info.server or "unknown",
            "port": info.port or 0,
            "version": props.get("version", "unknown"),
            "mcp_count": int(props.get("mcp_count", 0)),
            "hostname": props.get("hostname", "unknown"),
            "address": address,
        }


class NetworkDiscovery:
    """Publish and discover hub instances on LAN via Zeroconf/mDNS.

    Gracefully degrades when zeroconf is not installed.
    """

    def __init__(self) -> None:
        self._zeroconf: Any = None
        self._service_info: Any = None
        self._published = False

    @property
    def is_published(self) -> bool:
        return self._published

    def publish(self, port: int, mcp_count: int = 0) -> bool:
        """Publish this hub on the local network. Returns True if published."""
        if not _zeroconf_available:
            logger.info("Zeroconf not installed — network discovery disabled")
            return False

        hostname = socket.gethostname()

        try:
            self._zeroconf = Zeroconf()
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                f"slm-mcp-hub-{hostname}.{SERVICE_TYPE}",
                port=port,
                properties={
                    "version": VERSION,
                    "mcp_count": str(mcp_count),
                    "hostname": hostname,
                },
                server=f"{hostname}.local.",
            )
            self._zeroconf.register_service(self._service_info)
            self._published = True
            logger.info("Published hub on mDNS: port=%d, hostname=%s", port, hostname)
            return True
        except Exception as exc:
            logger.warning("Failed to publish on mDNS: %s", exc)
            self._cleanup()
            return False

    def discover(self, timeout_seconds: float = 3.0) -> tuple[DiscoveredHub, ...]:
        """Discover hub instances on the local network."""
        if not _zeroconf_available:
            logger.info("Zeroconf not installed — cannot discover hubs")
            return ()

        try:
            zc = Zeroconf()
            listener = _DiscoveryListener()
            ServiceBrowser(zc, SERVICE_TYPE, listener)

            time.sleep(timeout_seconds)

            results = tuple(
                DiscoveredHub(**info) for info in listener.discovered
            )
            zc.close()
            return results
        except Exception as exc:
            logger.warning("Network discovery failed: %s", exc)
            return ()

    def stop(self) -> None:
        """Stop publishing and clean up."""
        self._cleanup()

    def _cleanup(self) -> None:
        if self._zeroconf and self._service_info and self._published:
            try:
                self._zeroconf.unregister_service(self._service_info)
            except Exception:
                pass
        if self._zeroconf:
            try:
                self._zeroconf.close()
            except Exception:
                pass
        self._zeroconf = None
        self._service_info = None
        self._published = False
