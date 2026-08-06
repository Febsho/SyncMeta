"""Env-tunable HTTP read timeouts.

Every provider client hard-codes a ``(connect, read)`` pair. The read halves
were tuned for a fast link, and on a slow or congested host a too-short read
timeout is worse than useless: the request is abandoned just before the answer
arrives, the session retries it, and the retries cost more than waiting would
have. Making them tunable lets a self-hoster on a bad link raise them — or lower
them if they would rather fail fast than wait.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Bounds. Below ~2s almost everything times out; above ~180s a stuck request
#: would hold a sync worker long enough to look like a hang.
MIN_READ_TIMEOUT_SECONDS = 2
MAX_READ_TIMEOUT_SECONDS = 180


def _env_timeout(name: str, default: int) -> int:
    """Read a timeout from the environment, falling back to ``default``."""
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %ss", name, raw, default)
        return default
    clamped = max(MIN_READ_TIMEOUT_SECONDS, min(value, MAX_READ_TIMEOUT_SECONDS))
    if clamped != value:
        logger.warning("%s=%s clamped to %ss", name, value, clamped)
    return clamped
