"""Lightweight in-memory ring buffer for tracking outbound API requests."""

from __future__ import annotations

import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from collections import deque

_lock = threading.Lock()
_log: deque[dict] = deque(maxlen=600)
_counters: dict[str, int] = {}   # source -> total requests
_error_counters: dict[str, int] = {}  # source -> total errors

_SECRET_QUERY_KEYS = {
    "apikey", "api_key", "access_token", "refresh_token", "client_secret", "token",
}


def _safe_url(url: str) -> str:
    """Remove query-string credentials before storing request diagnostics."""
    try:
        parts = urlsplit(str(url or ""))
        query = urlencode([
            (key, "[redacted]" if key.lower() in _SECRET_QUERY_KEYS else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except Exception:
        return str(url or "")


def record(source: str, method: str, url: str, status: int, duration_ms: float) -> None:
    """Append one outbound request record to the ring buffer."""
    now = time.time()
    is_err = status >= 400 or status == 0
    with _lock:
        _log.appendleft({
            "ts": now,
            "source": source,
            "method": method.upper(),
            "url": _safe_url(url),
            "status": status,
            "duration_ms": round(duration_ms, 1),
            "error": is_err,
        })
        _counters[source] = _counters.get(source, 0) + 1
        if is_err:
            _error_counters[source] = _error_counters.get(source, 0) + 1


def snapshot(limit: int = 300) -> list[dict]:
    """Return up to *limit* most-recent records (newest first)."""
    with _lock:
        return [
            {**entry, "url": _safe_url(entry.get("url", ""))}
            for entry in list(_log)[:limit]
        ]


def counters() -> dict:
    """Return total request and error counts per source."""
    with _lock:
        return {
            "requests": dict(_counters),
            "errors": dict(_error_counters),
        }


def make_requests_hook(source: str):
    """Return a `requests` response hook that logs the completed request."""
    def _hook(response, *args, **kwargs):
        try:
            elapsed_ms = response.elapsed.total_seconds() * 1000
            record(source, response.request.method, response.url, response.status_code, elapsed_ms)
        except Exception:
            pass
        return response
    return _hook
