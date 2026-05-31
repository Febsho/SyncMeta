"""In-memory ring buffer that captures Python logging output for the UI.

Log entries are tagged with a profile_id when sync threads use
``set_profile_context()`` so the frontend can filter to the current user.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

_MAX_ENTRIES = 2000

_lock = threading.Lock()
_entries: deque[dict] = deque(maxlen=_MAX_ENTRIES)
_seq: int = 0

_thread_ctx = threading.local()


def set_profile_context(profile_id: str | None) -> None:
    _thread_ctx.profile_id = (profile_id or "")[:16]


def get_profile_context() -> str:
    return getattr(_thread_ctx, "profile_id", "")


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        try:
            entry = {
                "seq": 0,
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
                "profile_id": get_profile_context(),
            }
            msg_lower = entry["message"].lower()
            name = record.name.lower()
            if "sync" in name or "sync" in msg_lower:
                entry["category"] = "sync"
            elif "matcher" in name or "[resolve" in msg_lower:
                entry["category"] = "resolve"
            elif "simkl" in name or "anilist" in name or "trakt" in name or "mdblist" in name:
                entry["category"] = "api"
            elif "pmdb" in name or "publicmetadb" in name:
                entry["category"] = "pmdb"
            elif "fribb" in name or "anime_mapping" in name:
                entry["category"] = "mapping"
            else:
                entry["category"] = "other"
            with _lock:
                _seq += 1
                entry["seq"] = _seq
                _entries.append(entry)
        except Exception:
            pass


def install(level: int = logging.INFO) -> None:
    handler = BufferHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)


def snapshot(after_seq: int = 0, limit: int = 300, profile_id: str = "") -> list[dict]:
    with _lock:
        if after_seq <= 0:
            items = list(_entries)
        else:
            items = [e for e in _entries if e["seq"] > after_seq]
    if profile_id:
        pid_prefix = profile_id[:16]
        items = [
            e for e in items
            if not e.get("profile_id") or e["profile_id"] == pid_prefix
        ]
    return items[-limit:]


def clear() -> None:
    global _seq
    with _lock:
        _entries.clear()
        _seq = 0
