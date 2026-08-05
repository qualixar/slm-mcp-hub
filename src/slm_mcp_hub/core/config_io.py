"""On-disk I/O and env-override machinery for SLM MCP Hub configuration.

Extracted from core/config.py (W8-P6) to keep both modules under the 800-line cap.
All public names remain importable from ``slm_mcp_hub.core.config`` via re-export.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # HubConfig is imported lazily at runtime (inside function bodies) to avoid
    # circular imports.  The TYPE_CHECKING import is only for static analysers.
    from slm_mcp_hub.core.config import HubConfig  # noqa: TCH004

from slm_mcp_hub.core.constants import (
    CACHE_DEFAULT_TTL_SECONDS,
    CACHE_MAX_ENTRIES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    IDLE_SHUTDOWN_SECONDS,
    MAX_SESSIONS,
    SESSION_TIMEOUT_SECONDS,
    get_config_dir,
    get_config_file,
    get_snapshots_dir,
)

logger = logging.getLogger(__name__)

# MAX_SNAPSHOTS + DROP_GUARD_THRESHOLD are single-sourced in core.config and read
# lazily at each use site below, so test monkeypatches on core.config take effect.

# ---------------------------------------------------------------------------
# Robust boolean coercion (config-bool robustness sweep)
# ---------------------------------------------------------------------------

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def as_bool(value: object, default: bool = False) -> bool:
    """Coerce a config value to bool without the ``bool("false") is True`` trap.

    JSON booleans (``True``/``False``) pass through unchanged — no behavior
    change for correct configs.  Only string spellings of *false*
    (``"false"``, ``"0"``, ``"no"``, ``"off"`` etc.) now parse as ``False``.

    Args:
        value:   Raw value from the config dict.
        default: Returned when *value* is ``None``.

    Returns:
        Coerced boolean.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    if value is None:
        return default
    return bool(value)


# ---------------------------------------------------------------------------
# File-permission helpers
# ---------------------------------------------------------------------------


def _secure_config_permissions(path: Path) -> None:
    """Restrict live config and retained snapshots without following symlinks."""
    if path.exists() and not path.is_symlink():
        path.chmod(0o600)

    snapshots_dir = get_snapshots_dir(path.parent)
    if not snapshots_dir.exists() or snapshots_dir.is_symlink():
        return
    snapshots_dir.chmod(0o700)
    # Snapshot retention is capped at MAX_SNAPSHOTS, read lazily from core.config.
    import slm_mcp_hub.core.config as _cfg_mod  # noqa: PLC0415
    for snapshot in sorted(
        snapshots_dir.glob("config-*.json"), reverse=True
    )[: _cfg_mod.MAX_SNAPSHOTS]:
        if snapshot.is_file() and not snapshot.is_symlink():
            snapshot.chmod(0o600)


def _snapshot_existing(path: Path) -> Path | None:
    """Snapshot existing config to versioned file before overwriting.

    Skips snapshot if existing config is empty/trivial (< 3 MCPs) — no point
    in keeping useless snapshots that bloat the snapshot dir.

    Returns snapshot path if created, None otherwise.

    Reads ``get_snapshots_dir`` and ``MAX_SNAPSHOTS`` lazily from
    ``slm_mcp_hub.core.config`` so that test patches on that module take effect.
    """
    # Lazy import so monkeypatch.setattr(config_module, "get_snapshots_dir", …)
    # and monkeypatch.setattr(config_module, "MAX_SNAPSHOTS", …) are visible here.
    import slm_mcp_hub.core.config as _cfg_mod  # noqa: PLC0415
    _get_snapshots_dir = _cfg_mod.get_snapshots_dir
    _max_snapshots = _cfg_mod.MAX_SNAPSHOTS

    if not path.exists():
        return None

    # Don't snapshot trivial configs
    try:
        with open(path) as f:
            existing = json.load(f)
        existing_count = len(existing.get("mcpServers", existing.get("servers", {})))
        if existing_count < 3:
            return None  # not worth snapshotting
    except (json.JSONDecodeError, OSError):
        return None  # corrupt file, can't snapshot meaningfully

    snapshots_dir = _get_snapshots_dir(path.parent)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.chmod(0o700)
    ts = time.strftime("%Y%m%d-%H%M%S")
    snap_path = snapshots_dir / f"config-{ts}-{existing_count}mcps.json"

    import shutil  # noqa: PLC0415
    shutil.copy2(path, snap_path)
    snap_path.chmod(0o600)

    # Prune old snapshots — keep _max_snapshots newest
    snaps = sorted(snapshots_dir.glob("config-*.json"))
    while len(snaps) > _max_snapshots:
        snaps[0].unlink(missing_ok=True)
        snaps = snaps[1:]

    return snap_path


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + rename.

    Validates JSON parses before rename. If anything fails, original file
    is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)

        # Verify the file we just wrote parses back identically
        with open(tmp_path) as f:
            verify = json.load(f)
        if verify.get("mcpServers", {}) != data.get("mcpServers", {}):
            raise RuntimeError("Atomic write verification failed: mcpServers mismatch")

        os.replace(tmp_path, path)
        path.chmod(0o600)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def load_config(config_path: Path | None = None) -> HubConfig:
    """Load hub configuration from file, with env var overrides."""
    # Lazy import avoids circular dependency (config_io ↔ config).
    from slm_mcp_hub.core.config import HubConfig, parse_mcp_server  # noqa: PLC0415

    path = config_path or get_config_file()

    if not path.exists():
        logger.info("No config file found at %s, using defaults", path)
        return _apply_env_overrides(HubConfig())

    _secure_config_permissions(path)

    with open(path) as f:
        raw = json.load(f)

    servers_raw = raw.get("mcpServers", raw.get("servers", {}))
    servers = tuple(
        parse_mcp_server(name, cfg) for name, cfg in servers_raw.items()
    )

    config = HubConfig(
        host=raw.get("host", DEFAULT_HOST),
        port=raw.get("port", DEFAULT_PORT),
        config_dir=Path(raw.get("config_dir", str(get_config_dir()))),
        mcp_servers=servers,
        session_timeout_seconds=raw.get("session_timeout_seconds", SESSION_TIMEOUT_SECONDS),
        max_sessions=raw.get("max_sessions", MAX_SESSIONS),
        cache_ttl_seconds=raw.get("cache_ttl_seconds", CACHE_DEFAULT_TTL_SECONDS),
        cache_max_entries=raw.get("cache_max_entries", CACHE_MAX_ENTRIES),
        idle_shutdown_seconds=raw.get("idle_shutdown_seconds", IDLE_SHUTDOWN_SECONDS),
        log_level=raw.get("log_level", "INFO"),
        cors_origins=tuple(
            raw.get("cors_origins", ["http://127.0.0.1", "http://localhost"])
        ),
        plugins_enabled=tuple(raw.get("plugins_enabled", [])),
        webhooks=tuple(raw.get("webhooks", [])),
        # W2-P1: startup concurrency cap (default 8).
        startup_max_concurrency=int(raw.get("startup_max_concurrency", 8)),
        # W3-P1: idle eviction TTL and live-backend cap.
        idle_ttl_seconds=int(raw.get("idle_ttl_seconds", 300)),
        max_live_backends=int(raw.get("max_live_backends", 0)),
        # W4-P3: server-leg EventStore config.
        # as_bool() prevents bool("false") is True for string-valued JSON fields.
        event_store_enabled=as_bool(raw.get("event_store_enabled"), default=True),
        event_store_max_events_per_stream=int(
            raw.get("event_store_max_events_per_stream", 500)
        ),
        event_store_max_streams=int(raw.get("event_store_max_streams", 200)),
        event_store_stream_ttl_s=float(raw.get("event_store_stream_ttl_s", 7200.0)),
        transport_stateful=as_bool(raw.get("transport_stateful"), default=False),
        # W5-P1: dashboard + SSE event queue config.
        dashboard_enabled=as_bool(raw.get("dashboard_enabled"), default=True),
        dashboard_bind=str(raw.get("dashboard_bind", "127.0.0.1")),
        event_queue_maxsize=int(raw.get("event_queue_maxsize", 256)),
    )

    return _apply_env_overrides(config)


# ---------------------------------------------------------------------------
# _apply_env_overrides
# ---------------------------------------------------------------------------


def _apply_env_overrides(config: HubConfig) -> HubConfig:
    """Apply environment variable overrides to config. Returns new config."""
    from slm_mcp_hub.core.config import HubConfig  # noqa: PLC0415

    port = int(os.environ.get("SLM_HUB_PORT", config.port))
    host = os.environ.get("SLM_HUB_HOST", config.host)
    log_level = os.environ.get("SLM_HUB_LOG_LEVEL", config.log_level)
    config_dir = Path(os.environ.get("SLM_HUB_CONFIG_DIR", str(config.config_dir)))
    # W2-P1: startup concurrency cap — validated by HubConfig.__post_init__.
    startup_max_concurrency = int(
        os.environ.get("SLM_HUB_STARTUP_MAX_CONCURRENCY", config.startup_max_concurrency)
    )
    # W3-P1: idle eviction TTL and live-backend cap env overrides.
    idle_ttl_seconds = int(
        os.environ.get("SLM_HUB_IDLE_TTL_SECONDS", config.idle_ttl_seconds)
    )
    max_live_backends = int(
        os.environ.get("SLM_HUB_MAX_LIVE_BACKENDS", config.max_live_backends)
    )

    if (
        port == config.port
        and host == config.host
        and log_level == config.log_level
        and config_dir == config.config_dir
        and startup_max_concurrency == config.startup_max_concurrency
        and idle_ttl_seconds == config.idle_ttl_seconds
        and max_live_backends == config.max_live_backends
    ):
        return config

    return HubConfig(
        host=host,
        port=port,
        config_dir=config_dir,
        mcp_servers=config.mcp_servers,
        session_timeout_seconds=config.session_timeout_seconds,
        max_sessions=config.max_sessions,
        cache_ttl_seconds=config.cache_ttl_seconds,
        cache_max_entries=config.cache_max_entries,
        idle_shutdown_seconds=config.idle_shutdown_seconds,
        log_level=log_level,
        cors_origins=config.cors_origins,
        plugins_enabled=config.plugins_enabled,
        webhooks=config.webhooks,
        startup_max_concurrency=startup_max_concurrency,
        idle_ttl_seconds=idle_ttl_seconds,
        max_live_backends=max_live_backends,
        # W4-P3: pass through event_store fields so they survive env-override rebuild.
        event_store_enabled=config.event_store_enabled,
        event_store_max_events_per_stream=config.event_store_max_events_per_stream,
        event_store_max_streams=config.event_store_max_streams,
        # W5-P1: pass through dashboard fields so they survive env-override rebuild.
        dashboard_enabled=config.dashboard_enabled,
        dashboard_bind=config.dashboard_bind,
        event_queue_maxsize=config.event_queue_maxsize,
        event_store_stream_ttl_s=config.event_store_stream_ttl_s,
        transport_stateful=config.transport_stateful,
    )


# ---------------------------------------------------------------------------
# save_config
# ---------------------------------------------------------------------------


def save_config(
    config: HubConfig,
    config_path: Path | None = None,
    force: bool = False,
) -> None:
    """Save hub configuration to JSON file.

    Defenses (in order):
    1. PYTEST guard — refuses to write real user config during pytest.
    2. COUNT-DROP guard — refuses if new MCP count < 70% of existing (unless force=True).
    3. SNAPSHOT — versioned backup written to ~/.slm-mcp-hub/snapshots/ before overwrite.
    4. ATOMIC WRITE — write to .tmp, validate, rename.
    """
    from slm_mcp_hub.auth.models import AuthNoneConfig  # noqa: PLC0415
    from slm_mcp_hub.core.config import (  # noqa: PLC0415
        _serialize_auth,
        validate_server_config,
    )

    path = config_path or get_config_file(config.config_dir)
    _secure_config_permissions(path)

    if "PYTEST_CURRENT_TEST" in os.environ:
        real_user_config = (Path.home() / ".slm-mcp-hub" / "config.json").resolve()
        if path.resolve() == real_user_config:
            raise RuntimeError(
                f"REFUSING to overwrite real user config {path} during pytest. "
                "Tests must pass an explicit config_path. "
                "This guard prevents the April 26 incident where tests "
                "nuked 39 MCP server configurations."
            )

    # COUNT-DROP GUARD — refuse catastrophic shrinkage unless forced.
    # Threshold read lazily from core.config so test monkeypatches take effect.
    import slm_mcp_hub.core.config as _cfg_mod  # noqa: PLC0415
    _drop_threshold = _cfg_mod.DROP_GUARD_THRESHOLD
    new_count = len(config.mcp_servers)
    if path.exists() and not force:
        try:
            with open(path) as f:
                existing = json.load(f)
            existing_count = len(existing.get("mcpServers", existing.get("servers", {})))
            if existing_count > 5 and new_count < int(existing_count * _drop_threshold):
                raise RuntimeError(
                    f"REFUSING to save: MCP count would drop from {existing_count} to {new_count} "
                    f"(>{int((1-_drop_threshold)*100)}% loss). "
                    f"Pass force=True or use 'slm-hub config restore' if this is unintended. "
                    f"Snapshots: {get_snapshots_dir(path.parent)}"
                )
        except (json.JSONDecodeError, OSError):
            pass  # corrupt existing file — let save proceed

    # SNAPSHOT existing before overwriting
    snap = _snapshot_existing(path)
    if snap:
        logger.info("Snapshot saved: %s", snap)

    path.parent.mkdir(parents=True, exist_ok=True)

    servers_dict: dict[str, Any] = {}
    for srv in config.mcp_servers:
        validate_server_config(srv)
        entry: dict[str, Any] = {"enabled": srv.enabled}
        if srv.transport == "stdio":
            entry["command"] = srv.command
            entry["args"] = list(srv.args)
            if srv.env:
                entry["env"] = srv.env
        else:
            entry["type"] = srv.transport
            entry["url"] = srv.url
            if srv.headers:
                entry["headers"] = srv.headers
        if srv.always_on:
            entry["always_on"] = True
        if srv.no_cache:
            entry["no_cache"] = True
        # W3-P1: only persist spawn when it's non-default (lazy or pinned);
        # eager (the default) is omitted for a clean config file.
        if srv.spawn != "eager":
            entry["spawn"] = srv.spawn
        if srv.cost_per_call_cents > 0:
            entry["cost_per_call_cents"] = srv.cost_per_call_cents
        # W4-P2: only persist timeout_class when non-default; omit "default"
        # so configs without the field round-trip cleanly to the default.
        from slm_mcp_hub.core.config import DEFAULT_MAX_CONCURRENCY  # noqa: PLC0415
        from slm_mcp_hub.core.constants import TIMEOUT_CLASS_DEFAULT  # noqa: PLC0415
        if srv.timeout_class != TIMEOUT_CLASS_DEFAULT:
            entry["timeout_class"] = srv.timeout_class
        # W4-P2: only persist max_concurrency when non-default.
        if srv.max_concurrency != DEFAULT_MAX_CONCURRENCY:
            entry["max_concurrency"] = srv.max_concurrency
        # Serialize auth policy — NEVER tokens/secrets.  Omit for none-mode (default).
        if not isinstance(srv.auth, AuthNoneConfig):
            entry["auth"] = _serialize_auth(srv.auth)
        servers_dict[srv.name] = entry

    data: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "mcpServers": servers_dict,
        "session_timeout_seconds": config.session_timeout_seconds,
        "max_sessions": config.max_sessions,
        "cache_ttl_seconds": config.cache_ttl_seconds,
        "cache_max_entries": config.cache_max_entries,
        "idle_shutdown_seconds": config.idle_shutdown_seconds,
        "log_level": config.log_level,
        "cors_origins": list(config.cors_origins),
        "plugins_enabled": list(config.plugins_enabled),
        "webhooks": list(config.webhooks),
        # W2-P1: startup concurrency cap.
        "startup_max_concurrency": config.startup_max_concurrency,
        # W3-P1: idle eviction TTL and live-backend cap.
        "idle_ttl_seconds": config.idle_ttl_seconds,
        "max_live_backends": config.max_live_backends,
        # W4-P3: event store config — always persisted for explicit round-trip.
        "event_store_enabled": config.event_store_enabled,
        "event_store_max_events_per_stream": config.event_store_max_events_per_stream,
        "event_store_max_streams": config.event_store_max_streams,
        "event_store_stream_ttl_s": config.event_store_stream_ttl_s,
        "transport_stateful": config.transport_stateful,
        # W5-P1: dashboard + SSE event queue config.
        "dashboard_enabled": config.dashboard_enabled,
        "dashboard_bind": config.dashboard_bind,
        "event_queue_maxsize": config.event_queue_maxsize,
    }

    _atomic_write(path, data)
    _secure_config_permissions(path)
    logger.info("Config saved to %s (%d MCP servers)", path, len(config.mcp_servers))


# ---------------------------------------------------------------------------
# Snapshot management
# ---------------------------------------------------------------------------


def list_snapshots() -> list[dict[str, Any]]:
    """List all config snapshots, newest first.

    Reads ``get_snapshots_dir`` lazily from ``slm_mcp_hub.core.config`` so
    that test patches on that module take effect.
    """
    import slm_mcp_hub.core.config as _cfg_mod  # noqa: PLC0415
    snapshots_dir = _cfg_mod.get_snapshots_dir()
    if not snapshots_dir.exists():
        return []
    out = []
    for snap in sorted(snapshots_dir.glob("config-*.json"), reverse=True):
        try:
            with open(snap) as f:
                d = json.load(f)
            mcp_count = len(d.get("mcpServers", d.get("servers", {})))
        except (json.JSONDecodeError, OSError):
            mcp_count = -1
        out.append({
            "path": snap,
            "name": snap.name,
            "mcp_count": mcp_count,
            "size": snap.stat().st_size,
        })
    return out


def restore_snapshot(snapshot_name: str, target: Path | None = None) -> Path:
    """Restore a snapshot to the live config path. Returns the restored path.

    Reads ``get_snapshots_dir`` lazily from ``slm_mcp_hub.core.config`` so
    that test patches on that module take effect.
    """
    import slm_mcp_hub.core.config as _cfg_mod  # noqa: PLC0415
    snap = _cfg_mod.get_snapshots_dir() / snapshot_name
    if not snap.exists():
        raise FileNotFoundError(f"Snapshot not found: {snap}")

    target = target or get_config_file()

    # Snapshot the current state before restoring (so restore is reversible)
    _snapshot_existing(target)

    import shutil  # noqa: PLC0415
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snap, target)
    target.chmod(0o600)
    return target


# ---------------------------------------------------------------------------
# Default config generation
# ---------------------------------------------------------------------------


def generate_default_config(config_path: Path | None = None) -> HubConfig:
    """Generate and save a default configuration file."""
    from slm_mcp_hub.core.config import HubConfig  # noqa: PLC0415

    config = HubConfig()
    save_config(config, config_path)
    return config
