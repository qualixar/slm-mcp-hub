"""W3-P3 — LRU victim selection for the global live-backend cap.

Pure, side-effect-free helper used by ConnectionManager._apply_lru_cap
to pick which non-pinned backend to evict when max_live_backends would
be exceeded.  Kept in its own module so manager.py stays under the
800-line hard cap and the logic is independently unit-testable.
"""

from __future__ import annotations

from collections.abc import Mapping


def select_lru_victim(
    candidates: list[str],
    last_activity: Mapping[str, float],
) -> str | None:
    """Return the least-recently-used candidate, or None when empty.

    Parameters
    ----------
    candidates:
        Names of eligible (non-pinned, live, not-being-connected) backends.
    last_activity:
        Maps backend name → monotonic timestamp of last recorded activity.
        Backends absent from ``last_activity`` are treated as the oldest
        possible entry (``float("-inf")``), making them the preferred victim.

    Returns
    -------
    str | None
        The candidate with the smallest ``last_activity`` timestamp, or
        ``None`` if ``candidates`` is empty.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda n: last_activity.get(n, float("-inf")))
