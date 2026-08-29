"""Per-route synchronization baselines, persisted per profile.

Why this is its own file and not another key in ``profiles.json``: a baseline
holds one entry per item per category per route, so a real profile's baselines
run to megabytes. ``profiles.json`` is rewritten under a lock on every profile
mutation and deep-copied on the status poll, and the existing
``pair_managed_keys`` — a fraction of this size — already had to be excluded
from ``/status`` for exactly that reason. So baselines live beside the Library,
one file per profile, written atomically.

The invariant this file exists to enforce:

    **Only a run that actually succeeded may advance the agreement.**

``begin_attempt`` records that a run started. ``commit`` — and only ``commit`` —
moves ``last_successful_sync``, bumps ``sync_version`` and replaces the item
states. ``record_failure`` writes the error and leaves the agreement untouched.
A partial run therefore leaves the next run comparing against the last state
that was genuinely verified, rather than against whatever a broken read
happened to return.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from .models import (
    PHASE_ESTABLISHED,
    PHASE_INITIALIZING,
    FetchOutcome,
    ItemState,
    RouteBaseline,
)

logger = logging.getLogger(__name__)

#: Bumped when the on-disk shape changes in a way older readers cannot handle.
SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SyncStateStore:
    """The per-profile baseline file.

    Deliberately the same storage idiom as ``LibraryStore``: one JSON file on
    the mounted volume, written atomically, guarded by one re-entrant lock.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._baselines: dict[tuple[str, str], RouteBaseline] = {}
        self._dirty = False
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            # A corrupt baseline is not a reason to refuse to sync. Starting
            # empty re-enters `baseline_initializing`, which is the safe state:
            # the next run may add but must not delete.
            logger.warning(
                "Could not read sync state %s (%s); starting with no baseline",
                self._path, exc,
            )
            return
        routes = raw.get("routes") if isinstance(raw, dict) else None
        if not isinstance(routes, dict):
            return
        for key, entry in routes.items():
            route_id, _, category = str(key).partition("\t")
            if not route_id or not category:
                continue
            baseline = RouteBaseline.from_dict(entry)
            baseline.route_id, baseline.category = route_id, category
            self._baselines[(route_id, category)] = baseline

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "routes": {
                f"{route_id}\t{category}": baseline.to_dict()
                for (route_id, category), baseline in self._baselines.items()
            },
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".syncstate-", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp_name, self._path)
            self._dirty = False
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    # ── reading ────────────────────────────────────────────────────────────

    def baseline(self, route_id: str, category: str) -> RouteBaseline:
        """The agreed state for one route and category.

        A route that has never run comes back in ``baseline_initializing``,
        which is what stops its first comparison concluding a removal.
        """
        route_id, category = str(route_id), str(category)
        with self._lock:
            found = self._baselines.get((route_id, category))
            if found is not None:
                return found
            return RouteBaseline(route_id=route_id, category=category)

    def route_ids(self) -> set[str]:
        with self._lock:
            return {route_id for route_id, _ in self._baselines}

    def categories(self, route_id: str) -> set[str]:
        with self._lock:
            return {
                category for route, category in self._baselines
                if route == str(route_id)
            }

    def _mutable(self, route_id: str, category: str) -> RouteBaseline:
        key = (str(route_id), str(category))
        baseline = self._baselines.get(key)
        if baseline is None:
            baseline = RouteBaseline(route_id=key[0], category=key[1])
            self._baselines[key] = baseline
        return baseline

    # ── writing ────────────────────────────────────────────────────────────

    def begin_attempt(self, route_id: str, category: str) -> RouteBaseline:
        """Record that a run started, without disturbing the agreement."""
        with self._lock:
            baseline = self._mutable(route_id, category)
            baseline.last_attempt = _utc_now_iso()
            self._dirty = True
            return baseline

    def commit(
        self,
        route_id: str,
        category: str,
        *,
        items: dict[str, ItemState],
        source_fetch: FetchOutcome | None = None,
        destination_fetch: FetchOutcome | None = None,
        save: bool = True,
    ) -> RouteBaseline:
        """Accept a completed run's result as the new agreement.

        Call this only when the run genuinely finished: every planned write
        either succeeded or was deliberately skipped. A run with failed writes
        must use ``record_partial`` instead, so the next run still sees the work
        as outstanding rather than believing it done.
        """
        with self._lock:
            baseline = self._mutable(route_id, category)
            baseline.sync_version += 1
            baseline.phase = PHASE_ESTABLISHED
            baseline.last_successful_sync = _utc_now_iso()
            baseline.last_attempt = baseline.last_successful_sync
            baseline.last_error = ""
            baseline.items = dict(items or {})
            baseline.source_fetch = source_fetch
            baseline.destination_fetch = destination_fetch
            self._dirty = True
            if save:
                self._save_locked()
            return baseline

    def record_failure(
        self,
        route_id: str,
        category: str,
        error: str,
        *,
        source_fetch: FetchOutcome | None = None,
        destination_fetch: FetchOutcome | None = None,
        save: bool = True,
    ) -> RouteBaseline:
        """Record that a run did not complete. The agreement is left alone.

        This is the whole point of the split: a failed read must never be able
        to teach the engine that the items it could not see are gone.
        """
        with self._lock:
            baseline = self._mutable(route_id, category)
            baseline.last_attempt = _utc_now_iso()
            baseline.last_error = str(error or "")[:500]
            # Deliberately *not* stored on the baseline: these describe the run
            # that failed, not the state that is agreed. They ride the run
            # record instead, so nothing later mistakes them for the agreement.
            del source_fetch, destination_fetch
            self._dirty = True
            if save:
                self._save_locked()
            return baseline

    def record_partial(
        self,
        route_id: str,
        category: str,
        *,
        applied: dict[str, ItemState] | None = None,
        error: str = "",
        save: bool = True,
    ) -> RouteBaseline:
        """Fold in only what was actually written, without declaring success.

        A run where 95 of 100 writes landed must not be recorded as agreed —
        the next run has to retry the five. But the 95 must not be re-applied
        either, so their states are folded into the existing agreement while
        the phase and ``last_successful_sync`` stay where they were.
        """
        with self._lock:
            baseline = self._mutable(route_id, category)
            baseline.last_attempt = _utc_now_iso()
            baseline.last_error = str(error or "")[:500]
            for key, state in (applied or {}).items():
                baseline.items[str(key)] = state
            self._dirty = True
            if save:
                self._save_locked()
            return baseline

    def forget_route(self, route_id: str, save: bool = True) -> int:
        """Drop every baseline for a route the user deleted."""
        with self._lock:
            doomed = [key for key in self._baselines if key[0] == str(route_id)]
            for key in doomed:
                self._baselines.pop(key, None)
            if doomed:
                self._dirty = True
                if save:
                    self._save_locked()
            return len(doomed)

    def prune_to(self, route_ids, save: bool = True) -> int:
        """Forget every route not in ``route_ids``."""
        keep = {str(value) for value in route_ids or ()}
        with self._lock:
            doomed = [key for key in self._baselines if key[0] not in keep]
            for key in doomed:
                self._baselines.pop(key, None)
            if doomed:
                self._dirty = True
                if save:
                    self._save_locked()
            return len(doomed)

    # ── migration ──────────────────────────────────────────────────────────

    def adopt_managed_keys(self, managed_keys: dict, save: bool = True) -> int:
        """Seed ownership from the existing ``pair_managed_keys`` store.

        Those keys are the one piece of the new model that already exists: they
        record what a pair previously wrote to its target, which is exactly
        ``ItemState.managed``. Adopting them means an upgraded profile keeps
        knowing which destination entries are its own.

        What it deliberately does *not* do is mark the route established. The
        old store recorded ownership but never what the *source* looked like, so
        it cannot answer "did this disappear from the source since we agreed" —
        and guessing would let the first run after an upgrade delete on the
        strength of a comparison it never made. The route stays in
        ``baseline_initializing`` until one real run completes.
        """
        adopted = 0
        with self._lock:
            for route_id, categories in (managed_keys or {}).items():
                if not isinstance(categories, dict):
                    continue
                for category, keys in categories.items():
                    if not isinstance(keys, (list, tuple, set)):
                        continue
                    baseline = self._mutable(str(route_id), str(category))
                    if baseline.phase == PHASE_ESTABLISHED or baseline.items:
                        continue  # a real baseline already exists; leave it alone
                    for key in keys:
                        text = str(key or "").strip()
                        if not text:
                            continue
                        baseline.items[text] = ItemState(managed=True)
                        adopted += 1
                    baseline.phase = PHASE_INITIALIZING
            if adopted:
                self._dirty = True
                if save:
                    self._save_locked()
        return adopted

    # ── diagnostics ────────────────────────────────────────────────────────

    def describe(self) -> list[dict]:
        """A compact summary per route/category, for the run-details view."""
        with self._lock:
            return [
                {
                    "route_id": baseline.route_id,
                    "category": baseline.category,
                    "phase": baseline.phase,
                    "sync_version": baseline.sync_version,
                    "last_successful_sync": baseline.last_successful_sync,
                    "last_attempt": baseline.last_attempt,
                    "last_error": baseline.last_error,
                    "tracked_items": len(baseline.items),
                    "managed_items": sum(1 for s in baseline.items.values() if s.managed),
                    "allows_removals": baseline.allows_removals,
                }
                for baseline in sorted(
                    self._baselines.values(),
                    key=lambda b: (b.route_id, b.category),
                )
            ]
