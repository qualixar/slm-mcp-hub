"""Watchdog — auto-restart configuration and PID management.

Gap 9: Resilience — generate launchd/systemd configs, manage PID file.
"""

from __future__ import annotations

import logging
import os
import sys
import textwrap
from pathlib import Path

from slm_mcp_hub.core.constants import get_log_file, get_pid_file

logger = logging.getLogger(__name__)

LAUNCHD_LABEL = "com.qualixar.slm-mcp-hub"
SYSTEMD_UNIT = "slm-mcp-hub.service"


def generate_launchd_plist(port: int = 52414) -> str:
    """Generate a macOS launchd plist for auto-restart.

    Bulletproof: kills old process before starting (via start command),
    throttles restarts to 10s minimum interval, includes full PATH + HOME.
    """
    slm_hub_bin = _find_binary()
    home = str(Path.home())
    log_file = get_log_file()
    current_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin")
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{LAUNCHD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{slm_hub_bin}</string>
                <string>start</string>
                <string>--port</string>
                <string>{port}</string>
                <string>--log-level</string>
                <string>WARNING</string>
            </array>
            <key>KeepAlive</key>
            <true/>
            <key>RunAtLoad</key>
            <true/>
            <key>ThrottleInterval</key>
            <integer>10</integer>
            <key>StandardOutPath</key>
            <string>{log_file}</string>
            <key>StandardErrorPath</key>
            <string>{log_file}</string>
            <key>WorkingDirectory</key>
            <string>{home}</string>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>{current_path}</string>
                <key>HOME</key>
                <string>{home}</string>
            </dict>
        </dict>
        </plist>
    """)


def generate_systemd_unit(port: int = 52414) -> str:
    """Generate a Linux systemd unit file for auto-restart."""
    slm_hub_bin = _find_binary()
    return textwrap.dedent(f"""\
        [Unit]
        Description=SLM MCP Hub - Intelligent MCP Gateway
        After=network.target

        [Service]
        Type=simple
        ExecStart={slm_hub_bin} start --port {port}
        Restart=always
        RestartSec=5
        Environment=PATH=/usr/local/bin:/usr/bin:/bin

        [Install]
        WantedBy=default.target
    """)


def install_launchd(port: int = 52414) -> Path:
    """Write launchd plist and return the path."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"
    plist_path.write_text(generate_launchd_plist(port))
    logger.info("Launchd plist written to %s", plist_path)
    return plist_path


def write_pid_file() -> None:
    """Write current PID to file."""
    pid_file = get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))


def read_pid_file() -> int | None:
    """Read PID from file, or None if not found."""
    pid_file = get_pid_file()
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_pid_file() -> None:
    """Remove PID file if it exists."""
    pid_file = get_pid_file()
    if pid_file.exists():
        pid_file.unlink()


def is_running() -> bool:
    """Check if the hub is currently running (PID file + process alive)."""
    pid = read_pid_file()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # Signal 0 = check if alive
        return True
    except (OSError, ProcessLookupError):
        remove_pid_file()  # Stale PID file
        return False


def _find_binary() -> str:
    """Find the slm-hub binary path."""
    # Check if running from installed package
    for path_dir in os.environ.get("PATH", "").split(":"):
        candidate = Path(path_dir) / "slm-hub"
        if candidate.exists():
            return str(candidate)
    # Fallback: python -m slm_mcp_hub.cli.main
    return f"{sys.executable} -m slm_mcp_hub.cli.main"
