"""Planning watch history, where the unit is an *event* and not an item.

Membership asks "is this on the list". History asks "did this happen, and have
we already carried it across" — and getting that wrong is uniquely expensive,
because the failure mode is not a missing row but a growing pile of plays the
user never watched.

Three rules shape everything here.

**History is a union.** An episode absent from the source is not evidence it
should be deleted from the destination; providers expose different windows of
the same history, and one having forgotten a play says nothing about whether it
happened. Only an explicit mirror route may remove, and then only whole episodes.

**The same event, synced twice, is one play.** Deduplication runs in three
layers, strongest first: the source's own stable event id where it has one, then
this route's record of what it already carried, then the destination's current
plays matched within a tolerance window — because services timestamp the same
viewing minutes apart and an exact match makes every hop look like a rewatch.

**Watched *state* is not a play.** A provider that reports only "episode watched
= true" has no event to carry. Its row can confirm what is already recorded and
can create the first play of an episode nobody has, but it must never add a
second — otherwise every sync invents another viewing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..providers import PlaySet, item_key, normalize_watched_at
from .models import STATE_PRESENT, ItemState, RouteBaseline
from .planner import (
    ACTION_ADD,
    ACTION_REMOVE,
    CONFIDENCE_CERTAIN,
    CONFIDENCE_LIKELY,
    POLICY_MIRROR,
    PlannedAction,
    SyncPlan,
    normalize_policy,
)

logger = logging.getLogger(__name__)

REASON_NEW_EVENT = "New play, not yet on the destination"
REASON_REWATCH = "Watched again since the last sync"
REASON_ALREADY_SYNCED = "This route already carried this play across"
REASON_SAME_EVENT = "The destination already has this play"
REASON_PRESENCE_ONLY = "Source reports watched state, not plays; nothing new to add"
REASON_STATE_TARGET = "The destination records watched state, not individual plays"
REASON_ECHO = "This came back from a previous write by this route"
REASON_NO_EPISODE = "Names a show but no episode"
REASON_EPISODE_GONE = "The source no longer has any play of this episode"


def event_id(item: dict) -> str:
    """The source's own identifier for this play, when it has one.

    The strongest dedup key there is: if a provider hands back a stable id per
    history record, an id already carried can never be imported twice however
    its timestamp is later reformatted or rounded.
    """
    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    for candidate in (
        item.get("_syncmeta_event_id"), item.get("event_id"),
        item.get("history_id"), item.get("play_id"),
        ids.get("history"), ids.get("play"),
        # PMDB names its watch records `id` and hands the same value to DELETE.
        item.get("pmdb_item_id"),
    ):
        text = str(candidate or "").strip()
        if text and text.lower() not in {"none", "null"}:
            return text
    return ""


def _is_presence_only(item: dict) -> bool:
    """A row carrying no play time of its own, or a derived one.

    A watched-state read, an AniList progress count and a SIMKL aggregate all
    describe a *state*. Their timestamp, where they have one, belongs to the
    entry rather than to a viewing and moves whenever the entry is touched.
    """
    if item.get("cursor_exempt") or item.get("anilist_derived"):
        return True
    return not normalize_watched_at(item.get("watched_at"))


def _episode_scoped(item: dict) -> bool:
    media_type = str(item.get("media_type") or "").strip().lower()
    if media_type == "movie":
        return True
    try:
        return int(item.get("season")) >= 0 and int(item.get("episode")) >= 0
    except (TypeError, ValueError):
        return False


@dataclass
class HistoryPlanCounts:
    """What the planner concluded, for the run detail and the preview."""

    new_events: int = 0
    rewatches: int = 0
    already_synced: int = 0
    duplicates: int = 0
    presence_only: int = 0
    unwritable: int = 0
    removals: int = 0

    def to_dict(self) -> dict:
        return {
            "new_events": self.new_events,
            "rewatches": self.rewatches,
            "already_synced": self.already_synced,
            "probable_duplicates": self.duplicates,
            "presence_only": self.presence_only,
            "unwritable": self.unwritable,
            "removals": self.removals,
        }


@dataclass
class HistoryPlan:
    plan: SyncPlan
    counts: HistoryPlanCounts = field(default_factory=HistoryPlanCounts)
    #: Episode key -> the state to record once the writes land.
    projected: dict = field(default_factory=dict)


def plan_history(
    *,
    route_id: str,
    source_rows: list[dict],
    destination_rows: list[dict],
    baseline: RouteBaseline,
    target_records_plays: bool,
    policy: str = "",
    source_provider: str = "",
    destination_provider: str = "",
    category: str = "history",
) -> HistoryPlan:
    """Decide which source plays are genuinely missing from the destination."""
    policy = normalize_policy(policy)
    counts = HistoryPlanCounts()
    additions: list[PlannedAction] = []
    removals: list[PlannedAction] = []
    skipped: list[PlannedAction] = []

    # What the destination currently holds, per episode.
    destination_plays: dict[str, list] = {}
    destination_events: dict[str, set] = {}
    for row in destination_rows or []:
        key = item_key(row)
        destination_plays.setdefault(key, []).append(row.get("watched_at"))
        found = event_id(row)
        if found:
            destination_events.setdefault(key, set()).add(found)

    # One ledger per episode, seeded from the destination's plays *and* from what
    # this route already carried. The second half is what makes a repeated run
    # idempotent even when the destination reports its own timestamps back
    # differently, and what stops a two-way route re-importing its own write.
    ledgers: dict[str, PlaySet] = {}
    synced_events: dict[str, set] = {}
    projected: dict[str, ItemState] = {}

    def ledger_for(key: str) -> PlaySet:
        found = ledgers.get(key)
        if found is None:
            previous = baseline.state(key)
            found = PlaySet(list(destination_plays.get(key, ())) + list(previous.plays))
            ledgers[key] = found
            synced_events[key] = set(previous.event_ids) | destination_events.get(key, set())
            projected[key] = ItemState(
                source=STATE_PRESENT,
                destination=STATE_PRESENT if key in destination_plays else "",
                synced=STATE_PRESENT if key in destination_plays else "",
                managed=previous.managed,
                plays=tuple(previous.plays),
                event_ids=tuple(previous.event_ids),
            )
        return found

    def _action(kind, item, key, *, reason, destructive=False, confidence=CONFIDENCE_CERTAIN):
        return PlannedAction(
            key=key, kind=kind, category=category,
            source_provider=source_provider, destination_provider=destination_provider,
            title=str(item.get("title") or ""), media_type=str(item.get("media_type") or ""),
            year=item.get("year"),
            reason=reason, confidence=confidence, destructive=destructive, item=item,
        )

    source_episodes: set[str] = set()
    for row in source_rows or []:
        key = item_key(row)
        source_episodes.add(key)

        if not _episode_scoped(row):
            # A bare show entry means the whole show to every history API.
            counts.unwritable += 1
            skipped.append(_action("skip", row, key, reason=REASON_NO_EPISODE))
            continue

        ledger = ledger_for(key)
        state = projected[key]
        source_event = event_id(row)

        # Layer 1: an event id this route already carried can never come again.
        if source_event and source_event in synced_events[key]:
            counts.already_synced += 1
            skipped.append(_action("skip", row, key, reason=REASON_ALREADY_SYNCED))
            continue

        # Layer 2: a row that only reports watched state carries no event. It may
        # create the first play of an episode nobody has, never a second.
        if _is_presence_only(row):
            if ledger.stamped or key in destination_plays:
                counts.presence_only += 1
                skipped.append(_action("skip", row, key, reason=REASON_PRESENCE_ONLY))
                continue
            counts.new_events += 1
            additions.append(_action(ACTION_ADD, row, key, reason=REASON_NEW_EVENT))
            ledger.add(row.get("watched_at"))
            state.plays = tuple(state.plays) + (str(row.get("watched_at") or ""),)
            continue

        # From the ledger, not from the initial read: once this run has decided
        # to write a play for the episode, a second play of it must not also go
        # to a destination that cannot hold one — otherwise two rows in the same
        # batch slip past a check that only looked at what was there before.
        known_episode = ledger.stamped or key in destination_plays
        if known_episode and not target_records_plays:
            # The destination reports watched state: it would hand back one row
            # however many plays it was sent, so the extra would look missing
            # forever and be re-sent on every run.
            counts.duplicates += 1
            skipped.append(_action("skip", row, key, reason=REASON_STATE_TARGET))
            continue

        # Layer 3: timestamps, matched with tolerance.
        if ledger.matches(row.get("watched_at")):
            counts.already_synced += 1
            skipped.append(_action(
                "skip", row, key,
                reason=REASON_ECHO if not destination_plays.get(key) else REASON_SAME_EVENT,
            ))
            continue

        rewatch = bool(ledger.stamped)
        if rewatch:
            counts.rewatches += 1
        else:
            counts.new_events += 1
        additions.append(_action(
            ACTION_ADD, row, key,
            reason=REASON_REWATCH if rewatch else REASON_NEW_EVENT,
        ))
        ledger.add(row.get("watched_at"))
        state.plays = tuple(state.plays) + (str(row.get("watched_at") or ""),)
        if source_event:
            state.event_ids = tuple(state.event_ids) + (source_event,)
            synced_events[key].add(source_event)

    # Removals: only ever whole episodes, only under mirror, only once a
    # baseline exists. History is a union everywhere else.
    if policy == POLICY_MIRROR and baseline.allows_removals:
        for key, rows in destination_plays.items():
            if key in source_episodes:
                continue
            previous = baseline.state(key)
            if previous.source != STATE_PRESENT:
                continue  # the source never had it; not this route's to delete
            sample = next(
                (row for row in destination_rows if item_key(row) == key), {},
            )
            counts.removals += 1
            removals.append(_action(
                ACTION_REMOVE, sample, key, reason=REASON_EPISODE_GONE,
                destructive=True, confidence=CONFIDENCE_LIKELY,
            ))

    plan = SyncPlan(
        route_id=str(route_id), category=str(category),
        source_provider=source_provider, destination_provider=destination_provider,
        baseline_version=baseline.sync_version, phase=baseline.phase, policy=policy,
        additions=tuple(additions), removals=tuple(removals), skipped=tuple(skipped),
    )
    return HistoryPlan(plan=plan, counts=counts, projected=projected)
