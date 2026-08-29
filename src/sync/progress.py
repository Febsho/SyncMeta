"""Planning resume points, where the risk is rewinding somebody's playback.

Resume is neither membership nor an event log. An item present on both sides may
still need writing because the position moved, and writing the *wrong* position
is worse than writing nothing: it does not add a stray row, it sends the user
back to where they were an hour ago.

Three rules:

**A position is only worth syncing if it means something.** A few seconds in is
an accidental open, not progress, and pushing it can overwrite a real position
on the other side. Near the end is not a resume point at all — it is a finished
title, and carrying it onward makes a watched show look half-seen.

**Furthest wins, unless the baseline says otherwise.** There is no portable
per-item modification time across these providers, so "most recent" is not
answerable. Furthest is the least destructive deterministic rule: it can never
rewind either side because a route happened to run in a particular order.

**A large jump backwards is a question, not an answer.** 95% to 2% is either a
fresh viewing that has just started or a stale record about to clobber a real
one, and nothing in the data distinguishes them. It is reported rather than
guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..providers import item_key
from .models import RouteBaseline
from .planner import (
    ACTION_ADD,
    ACTION_UPDATE,
    CONFIDENCE_CERTAIN,
    CONFIDENCE_LIKELY,
    PlannedAction,
    SyncPlan,
    normalize_policy,
)

#: Below this a "resume point" is somebody having opened the wrong thing.
MIN_MEANINGFUL_PERCENT = 2.0
#: At or above this the title is finished, not in progress. Providers broadly
#: agree on this band; PublicMetaDB deletes the resume point outright at 80%.
COMPLETED_PERCENT = 90.0
#: A drop at least this large is a new viewing or a stale record, and the data
#: cannot say which.
SUSPICIOUS_REWIND_PERCENT = 50.0
#: Positions this close together are the same place; writing would be noise.
POSITION_TOLERANCE_MS = 1000

REASON_NEW_POSITION = "Not yet on the destination"
REASON_FURTHER = "The source is further along"
REASON_ALREADY_THERE = "Already at the same position"
REASON_DESTINATION_FURTHER = "The destination is further along; leaving it"
REASON_TOO_EARLY = "Barely started; not a real resume point"
REASON_COMPLETED = "Finished on the source; not a resume point"
REASON_SUSPICIOUS_REWIND = "Would rewind the destination a long way"


def _percent(item: dict) -> float:
    """How far through this item is, from whichever fields the provider gave."""
    try:
        explicit = float(item.get("progress"))
        if 0 < explicit <= 100:
            return explicit
    except (TypeError, ValueError):
        pass
    try:
        position = float(item.get("position_ms") or 0)
        runtime = float(item.get("runtime_ms") or 0)
    except (TypeError, ValueError):
        return 0.0
    if runtime <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * position / runtime))


def _position(item: dict) -> float:
    try:
        return float(item.get("position_ms") or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class ProgressPlan:
    plan: SyncPlan
    conflicts: tuple = ()


def plan_progress(
    *,
    route_id: str,
    source_by_key: dict,
    destination_by_key: dict,
    baseline: RouteBaseline,
    policy: str = "",
    source_provider: str = "",
    destination_provider: str = "",
    category: str = "resume",
) -> ProgressPlan:
    """Decide which resume points are worth writing to the destination."""
    policy = normalize_policy(policy)
    updates: list[PlannedAction] = []
    additions: list[PlannedAction] = []
    skipped: list[PlannedAction] = []
    conflicts: list[PlannedAction] = []

    def _action(kind, item, key, *, reason, confidence=CONFIDENCE_CERTAIN):
        return PlannedAction(
            key=key, kind=kind, category=category,
            source_provider=source_provider, destination_provider=destination_provider,
            title=str(item.get("title") or ""), media_type=str(item.get("media_type") or ""),
            reason=reason, confidence=confidence, item=item,
        )

    for key, item in source_by_key.items():
        source_percent = _percent(item)

        if source_percent < MIN_MEANINGFUL_PERCENT:
            skipped.append(_action("skip", item, key, reason=REASON_TOO_EARLY))
            continue
        if source_percent >= COMPLETED_PERCENT:
            # Finished. Pushing this on would make a watched title look like it
            # was abandoned near the end.
            skipped.append(_action("skip", item, key, reason=REASON_COMPLETED))
            continue

        existing = destination_by_key.get(key)
        if existing is None:
            additions.append(_action(ACTION_ADD, item, key, reason=REASON_NEW_POSITION))
            continue

        destination_percent = _percent(existing)
        if abs(_position(item) - _position(existing)) <= POSITION_TOLERANCE_MS:
            skipped.append(_action("skip", item, key, reason=REASON_ALREADY_THERE))
            continue
        if destination_percent >= COMPLETED_PERCENT:
            skipped.append(_action(
                "skip", item, key, reason=REASON_DESTINATION_FURTHER,
            ))
            continue
        if source_percent <= destination_percent:
            skipped.append(_action(
                "skip", item, key, reason=REASON_DESTINATION_FURTHER,
            ))
            continue
        if destination_percent - source_percent >= SUSPICIOUS_REWIND_PERCENT:
            # Cannot happen given the check above, but kept explicit: a large
            # backwards jump is reported rather than performed.
            conflicts.append(_action(
                "conflict", item, key, reason=REASON_SUSPICIOUS_REWIND,
                confidence=CONFIDENCE_LIKELY,
            ))
            continue
        updates.append(_action(ACTION_UPDATE, item, key, reason=REASON_FURTHER))

    plan = SyncPlan(
        route_id=str(route_id), category=str(category),
        source_provider=source_provider, destination_provider=destination_provider,
        baseline_version=baseline.sync_version, phase=baseline.phase, policy=policy,
        additions=tuple(additions), updates=tuple(updates), skipped=tuple(skipped),
        conflicts=tuple(conflicts),
    )
    return ProgressPlan(plan=plan, conflicts=tuple(conflicts))
