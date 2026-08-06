"""Rate-limit handling shared by the provider clients.

Reads and writes need different retry policies, and urllib3's ``Retry`` cannot
express the difference — it takes one status list for all methods.

* A **read** is safe to retry on anything. That stays on the session-level
  ``Retry``, which already honours ``Retry-After`` for 429/503/413.
* A **write** is only safe to retry when the request provably did *not* take
  effect. A 429 is exactly that: the server rejected it before doing any work.
  A 5xx or a read timeout is not — the write may well have landed, and
  ``/sync/history`` is not idempotent, so retrying turns one play into two.

So the session retry covers GET only, and writes come through
``retry_on_rate_limit`` which retries a 429 (and nothing else), waiting for
whatever the service asked for.
"""

from __future__ import annotations

import logging
import random
import threading
import time

import requests

logger = logging.getLogger(__name__)

#: Never sleep longer than this for one attempt, however large Retry-After is.
MAX_RATE_LIMIT_WAIT_SECONDS = 120.0
#: Used when a 429 arrives with no usable hint about when to come back.
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 2.0


def retry_after_seconds(response: requests.Response | None) -> float:
    """How long a 429 asked us to wait, clamped to something sane.

    Providers disagree about which header carries it: ``Retry-After`` is the
    standard, Trakt and AniList also send ``X-RateLimit-Reset-After``, and
    MDBList sends an absolute epoch in ``X-RateLimit-Reset``.
    """
    if response is None:
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS

    headers = response.headers or {}
    for name in ("Retry-After", "X-RateLimit-Reset-After"):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return _clamp(float(str(raw).strip()))
        except (TypeError, ValueError):
            continue

    # An absolute reset timestamp: convert to a delay, ignoring anything in the
    # past or implausibly far out (a clock skew must not stall a sync).
    raw_reset = headers.get("X-RateLimit-Reset")
    if raw_reset is not None:
        try:
            delay = float(str(raw_reset).strip()) - time.time()
            if 0 < delay <= MAX_RATE_LIMIT_WAIT_SECONDS:
                return _clamp(delay)
        except (TypeError, ValueError):
            pass

    return DEFAULT_RATE_LIMIT_WAIT_SECONDS


def _clamp(seconds: float) -> float:
    if seconds != seconds or seconds < 0:  # NaN or negative
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS
    return max(0.0, min(seconds, MAX_RATE_LIMIT_WAIT_SECONDS))


def retry_on_rate_limit(
    send,
    *,
    provider: str,
    attempts: int = 3,
    sleep=time.sleep,
    check_cancelled=None,
):
    """Call ``send()``, retrying **only** when the provider answers 429.

    Any other error — including a 5xx or a timeout on a write that may already
    have been applied — is raised to the caller untouched, because retrying it
    could duplicate the write.
    """
    last_error: requests.HTTPError | None = None
    for attempt in range(max(1, attempts)):
        if check_cancelled is not None:
            check_cancelled()
        try:
            return send()
        except requests.HTTPError as exc:
            response = exc.response
            if response is None or response.status_code != 429:
                raise
            last_error = exc
            if attempt >= attempts - 1:
                break
            wait = retry_after_seconds(response)
            # A little jitter so several workers throttled at once do not all
            # come back in the same instant and trip the limit again.
            wait += random.uniform(0, min(1.0, wait or 1.0))
            logger.warning(
                "%s rate limited (429); waiting %.1fs before retry %d/%d",
                provider, wait, attempt + 2, attempts,
            )
            sleep(wait)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{provider}: rate limit retry ended without a result")


#: How long to wait between cancel checks while parked on a limiter.
_CANCEL_POLL_INTERVAL = 0.25


class RateLimiter:
    """Sliding-window rate limiter.

    Proactive pacing, as opposed to the reactive 429 handling above: it is
    better to stay under a documented limit than to be told off for exceeding
    it. Shared by every provider client — PublicMetaDB was the only one that
    paced itself, so the others could burst straight into a 429.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self._max = max(1, int(max_requests))
        self._window = float(window_seconds)
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def wait(self, cancel_requested_callback=None) -> None:
        while True:
            with self._lock:
                now = time.time()
                self._timestamps = [t for t in self._timestamps if now - t < self._window]
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                sleep_for = self._timestamps[0] + self._window - now + 0.1
            if sleep_for > 0:
                logger.debug("Rate limit: sleeping %.2fs", sleep_for)
                deadline = time.monotonic() + sleep_for
                while True:
                    # Parked on a limiter is exactly where a Stop must still be
                    # able to interrupt, so poll rather than sleeping through.
                    if cancel_requested_callback:
                        try:
                            if cancel_requested_callback():
                                from .sync_service import SyncCancelled
                                raise SyncCancelled("Sync stopped by user")
                        except SyncCancelled:
                            raise
                        except Exception:
                            logger.debug("Cancel callback failed", exc_info=True)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(_CANCEL_POLL_INTERVAL, remaining))
