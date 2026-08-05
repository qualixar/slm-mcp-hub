"""W8-P3: Transport-mode resolver — stateless default, stateful opt-in.

Single responsibility: decide whether the hub should run stateful sessions.

Default is stateless (modern MCP 2026-07-28, no resumable replay). Stateful
activates resumable replay via the SDK's event store path (stateless=False in
StreamableHTTPSessionManager). The env var SLM_HUB_STATEFUL, when set and
non-empty, always wins over the config-file value.
"""

from __future__ import annotations

import os

_TRUTHY_VALS: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def resolve_stateful(hub_config: object) -> bool:
    """Resolve effective transport mode.

    Resolution order (first match wins):
    1. ``SLM_HUB_STATEFUL`` env var — when set and non-blank (after strip):
       truthy ``{1, true, yes, on}`` (case-insensitive) → ``True``,
       anything else → ``False``. Empty/whitespace-only falls through to config.
    2. ``hub_config.transport_stateful`` — when hub_config is not None.
    3. ``False`` — safe stateless default when hub_config is None.

    Args:
        hub_config: A ``HubConfig`` instance or ``None``.  Typed as ``object``
            to avoid a circular import; only ``transport_stateful`` is accessed
            via ``getattr`` with a ``False`` default.

    Returns:
        ``True`` when the hub should run stateful sessions (resumable replay
        active). ``False`` when stateless (default, no session tracking).
    """
    env_val: str | None = os.environ.get("SLM_HUB_STATEFUL")
    if env_val is not None and env_val.strip() != "":
        return env_val.strip().lower() in _TRUTHY_VALS
    if hub_config is None:
        return False
    return bool(getattr(hub_config, "transport_stateful", False))
