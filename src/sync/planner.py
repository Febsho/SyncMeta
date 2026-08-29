"""Turn two current states plus a baseline into an immutable plan.

This is the module that answers the question the old diff could not. Comparing
a source list with a destination list tells you only that they *differ*; it
cannot tell you which side moved. "Missing on the destination" is either an item
the user just added on the source or an item the user just deleted on the
destination, and those call for opposite actions.

The baseline is what separates them. Every decision here is made by comparing
*both* current states against the last state the two sides agreed on, never by
comparing them against each other.

Nothing in this module performs I/O or mutates anything. It takes states in and
returns a plan, which is what lets the same code produce a dry-run preview and
drive a real execution — the two cannot drift apart if there is only one of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .models import STATE_ABSENT, STATE_PRESENT, RouteBaseline

# ── what a plan can ask for ────────────────────────────────────────────────
ACTION_ADD = "add"
ACTION_REMOVE = "remove"
ACTION_UPDATE = "update"
ACTION_SKIP = "skip"
ACTION_CONFLICT = "conflict"

# ── removal policies ───────────────────────────────────────────────────────
#: Only additions and updates propagate. Nothing is ever deleted.
POLICY_NEVER_REMOVE = "never_remove"
#: Delete only what this route put there, and only once it has left the source.
#: The recommended destructive policy: destination content that predates the
#: route, or that a person added by hand, is never touched.
POLICY_REMOVE_MANAGED = "remove_managed"
#: The destination is made to match the source exactly, including deleting
#: entries this route never created. Dangerous by construction.
POLICY_MIRROR = "mirror"

#: The historical names, still what `SyncPair.removal_mode` stores.
_POLICY_ALIASES = {
    "additive": POLICY_NEVER_REMOVE,
    "managed": POLICY_REMOVE_MANAGED,
    "mirror": POLICY_MIRROR,
    POLICY_NEVER_REMOVE: POLICY_NEVER_REMOVE,
    POLICY_REMOVE_MANAGED: POLICY_REMOVE_MANAGED,
    POLICY_MIRROR: POLICY_MIRROR,
}


def normalize_policy(value: object) -> str:
    """Map a stored removal mode onto a policy, refusing to guess.

    An unrecognised value resolves to ``never_remove``: a mode nobody
    understands must not fall through to a destructive default.
    """
    return _POLICY_ALIASES.get(str(value or "").strip().lower(), POLICY_NEVER_REMOVE)


# ── conflict policies ──────────────────────────────────────────────────────
#: Surface it and act on neither side. The default, because for membership the
#: alternative is letting run order decide which of the user's two edits wins.
CONFLICT_MANUAL = "manual_review"
CONFLICT_SOURCE_WINS = "source_wins"
CONFLICT_DESTINATION_WINS = "destination_wins"

# `latest_change` is deliberately not offered for membership. It needs a
# trustworthy per-item modification time on both sides, and several providers
# expose none at all — a policy that silently degrades to "whichever we happened
# to read second" is worse than reporting the conflict.

# ── reasons, written to be shown to a person ───────────────────────────────
REASON_NEW_ON_SOURCE = "Added on the source since the last sync"
REASON_FIRST_SYNC = "Not yet on the destination"
REASON_RESTORED = "Deleted on the destination; the source is authoritative for this route"
REASON_REMOVED_ON_SOURCE = "Removed from the source since the last sync"
REASON_REMOVED_ON_DESTINATION = "Removed from the destination since the last sync"
REASON_IN_SYNC = "Already in sync"
REASON_UNMANAGED = "Destination item is unmanaged; keeping it"
REASON_NO_BASELINE = "No baseline yet, so a removal cannot be justified"
REASON_POLICY_NEVER_REMOVE = "This route never removes"
REASON_SOURCE_UNTRUSTWORTHY = "Source read was incomplete, so removals are unsafe"
REASON_UNRESOLVED = "Could not be resolved to a known title"
REASON_BOTH_CHANGED = "Changed on both sides since the last sync"

#: Confidence attached to an action. Anything below `certain` is derived from a
#: baseline that could not fully explain the item.
CONFIDENCE_CERTAIN = "certain"
CONFIDENCE_LIKELY = "likely"


@dataclass(frozen=True)
class PlannedAction:
    """One thing the plan intends to do, and why."""

    key: str
    kind: str
    category: str
    source_provider: str = ""
    destination_provider: str = ""
    title: str = ""
    media_type: str = ""
    old_state: str = STATE_ABSENT
    new_state: str = STATE_ABSENT
    reason: str = ""
    confidence: str = CONFIDENCE_CERTAIN
    destructive: bool = False
    managed: bool = False
    #: The provider payload to hand to the adapter. Carried, never inspected.
    item: dict = field(default_factory=dict, repr=False, compare=False)
    #: Two-way only: which side this action writes to.
    direction: str = "forward"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "category": self.category,
            "source_provider": self.source_provider,
            "destination_provider": self.destination_provider,
            "title": self.title,
            "media_type": self.media_type,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "reason": self.reason,
            "confidence": self.confidence,
            "destructive": self.destructive,
            "managed": self.managed,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class SyncPlan:
    """Everything one route intends to do to one category, decided up front.

    Immutable on purpose: the plan a preview shows and the plan an executor runs
    have to be the same object, and an executor that could edit it mid-run would
    make the preview a lie.
    """

    route_id: str
    category: str
    source_provider: str = ""
    destination_provider: str = ""
    baseline_version: int = 0
    phase: str = ""
    policy: str = POLICY_NEVER_REMOVE
    additions: tuple[PlannedAction, ...] = ()
    updates: tuple[PlannedAction, ...] = ()
    removals: tuple[PlannedAction, ...] = ()
    conflicts: tuple[PlannedAction, ...] = ()
    unresolved: tuple[PlannedAction, ...] = ()
    skipped: tuple[PlannedAction, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def destructive_count(self) -> int:
        return len(self.removals)

    @property
    def write_count(self) -> int:
        return len(self.additions) + len(self.updates) + len(self.removals)

    @property
    def is_noop(self) -> bool:
        """A run with nothing to do. Idempotency is exactly this being True."""
        return self.write_count == 0

    def destructive_percent(self, destination_size: int) -> float:
        if destination_size <= 0:
            return 0.0
        return round(100.0 * self.destructive_count / destination_size, 1)

    def with_warning(self, message: str) -> "SyncPlan":
        return replace(self, warnings=self.warnings + (str(message),))

    def without_removals(self, reason: str) -> "SyncPlan":
        """Return the same plan with every removal demoted to a skip."""
        demoted = tuple(
            replace(action, kind=ACTION_SKIP, reason=reason, destructive=False)
            for action in self.removals
        )
        return replace(
            self, removals=(), skipped=self.skipped + demoted,
            warnings=self.warnings + (reason,),
        )

    def to_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "category": self.category,
            "source_provider": self.source_provider,
            "destination_provider": self.destination_provider,
            "baseline_version": self.baseline_version,
            "phase": self.phase,
            "policy": self.policy,
            "counts": {
                "additions": len(self.additions),
                "updates": len(self.updates),
                "removals": len(self.removals),
                "conflicts": len(self.conflicts),
                "unresolved": len(self.unresolved),
                "skipped": len(self.skipped),
            },
            "additions": [a.to_dict() for a in self.additions],
            "updates": [a.to_dict() for a in self.updates],
            "removals": [a.to_dict() for a in self.removals],
            "conflicts": [a.to_dict() for a in self.conflicts],
            "unresolved": [a.to_dict() for a in self.unresolved],
            "warnings": list(self.warnings),
        }


def _describe(item: dict) -> tuple[str, str]:
    return (
        str((item or {}).get("title") or ""),
        str((item or {}).get("media_type") or ""),
    )


def plan_membership(
    *,
    route_id: str,
    category: str,
    source_by_key: dict,
    destination_by_key: dict,
    baseline: RouteBaseline,
    policy: str = POLICY_NEVER_REMOVE,
    source_provider: str = "",
    destination_provider: str = "",
    unresolved_keys=(),
    source_trustworthy: bool = True,
) -> SyncPlan:
    """Plan one one-way category: the source is authoritative, deletions are not.

    Membership means "is this item on this list" — a watchlist, a collection, a
    status list. History and progress have different semantics and are planned
    elsewhere.

    The four cases that matter, and why presence alone cannot decide them:

    * on source, not on destination — an addition, unless the baseline says the
      destination had it and this route put it there, in which case somebody
      deleted it there and the route is overruling them. Both are additions;
      they are given different reasons so a preview can say which.
    * not on source, on destination — only a removal if the baseline says the
      source *used to* have it. If the source never had it, the item predates
      this route or belongs to another one, and only ``mirror`` may touch it.
    * on both — already in sync.
    * on neither — nothing to do, and it drops out of the next baseline.
    """
    policy = normalize_policy(policy)
    unresolved = set(unresolved_keys or ())

    additions: list[PlannedAction] = []
    removals: list[PlannedAction] = []
    skipped: list[PlannedAction] = []
    unresolved_actions: list[PlannedAction] = []
    warnings: list[str] = []

    def _action(key, kind, item, *, reason, destructive=False,
                old_state=STATE_ABSENT, new_state=STATE_ABSENT,
                confidence=CONFIDENCE_CERTAIN, managed=False) -> PlannedAction:
        title, media_type = _describe(item)
        return PlannedAction(
            key=key, kind=kind, category=category,
            source_provider=source_provider, destination_provider=destination_provider,
            title=title, media_type=media_type,
            old_state=old_state, new_state=new_state, reason=reason,
            confidence=confidence, destructive=destructive, managed=managed,
            item=item or {},
        )

    keys = set(source_by_key) | set(destination_by_key)
    # Baseline-only keys matter: an item on neither side now, but present in the
    # agreement, is simply gone and should not linger in the next baseline.
    for key in sorted(keys):
        previous = baseline.state(key)
        on_source = key in source_by_key
        on_destination = key in destination_by_key
        item = source_by_key.get(key) or destination_by_key.get(key) or {}

        if key in unresolved:
            unresolved_actions.append(
                _action(key, ACTION_SKIP, item, reason=REASON_UNRESOLVED)
            )
            continue

        if on_source and on_destination:
            skipped.append(_action(
                key, ACTION_SKIP, item, reason=REASON_IN_SYNC,
                old_state=STATE_PRESENT, new_state=STATE_PRESENT,
            ))
            continue

        if on_source and not on_destination:
            deleted_there = (
                baseline.allows_removals
                and previous.destination == STATE_PRESENT
                and previous.managed
            )
            additions.append(_action(
                key, ACTION_ADD, item,
                reason=REASON_RESTORED if deleted_there else (
                    REASON_NEW_ON_SOURCE if previous.known else REASON_FIRST_SYNC
                ),
                old_state=STATE_ABSENT, new_state=STATE_PRESENT,
                confidence=CONFIDENCE_LIKELY if deleted_there else CONFIDENCE_CERTAIN,
                managed=previous.managed,
            ))
            continue

        # Not on the source, present on the destination: the only case that can
        # possibly justify deleting something.
        destination_item = destination_by_key.get(key) or {}
        if policy == POLICY_NEVER_REMOVE:
            skipped.append(_action(
                key, ACTION_SKIP, destination_item, reason=REASON_POLICY_NEVER_REMOVE,
                old_state=STATE_PRESENT, new_state=STATE_PRESENT,
            ))
            continue
        if not baseline.allows_removals:
            skipped.append(_action(
                key, ACTION_SKIP, destination_item, reason=REASON_NO_BASELINE,
                old_state=STATE_PRESENT, new_state=STATE_PRESENT,
            ))
            continue
        if previous.source != STATE_PRESENT:
            # The source never had it at the last agreement, so its absence now
            # is not evidence of anything. Only mirror may act on that.
            if policy != POLICY_MIRROR:
                skipped.append(_action(
                    key, ACTION_SKIP, destination_item, reason=REASON_UNMANAGED,
                    old_state=STATE_PRESENT, new_state=STATE_PRESENT,
                ))
                continue
            removals.append(_action(
                key, ACTION_REMOVE, destination_item, reason=REASON_UNMANAGED,
                destructive=True, old_state=STATE_PRESENT, new_state=STATE_ABSENT,
                confidence=CONFIDENCE_LIKELY, managed=previous.managed,
            ))
            continue
        if policy == POLICY_REMOVE_MANAGED and not previous.managed:
            skipped.append(_action(
                key, ACTION_SKIP, destination_item, reason=REASON_UNMANAGED,
                old_state=STATE_PRESENT, new_state=STATE_PRESENT,
            ))
            continue
        removals.append(_action(
            key, ACTION_REMOVE, destination_item, reason=REASON_REMOVED_ON_SOURCE,
            destructive=True, old_state=STATE_PRESENT, new_state=STATE_ABSENT,
            managed=previous.managed,
        ))

    plan = SyncPlan(
        route_id=str(route_id), category=str(category),
        source_provider=source_provider, destination_provider=destination_provider,
        baseline_version=baseline.sync_version, phase=baseline.phase, policy=policy,
        additions=tuple(additions), removals=tuple(removals),
        unresolved=tuple(unresolved_actions), skipped=tuple(skipped),
        warnings=tuple(warnings),
    )
    if not source_trustworthy and plan.removals:
        # The source could not be read completely, so "absent from the source"
        # carries no information at all. Additions are still fine.
        plan = plan.without_removals(REASON_SOURCE_UNTRUSTWORTHY)
    return plan


@dataclass(frozen=True)
class TwoWayPlan:
    """One reconciliation of two sides, expressed as two directed plans.

    Built in a single pass, deliberately not as two one-way plans run back to
    back: running A→B and then B→A means whichever direction goes first re-adds
    an item the user just deleted on the other side, so the *order* decides the
    outcome. Here both sides are read once and compared against the same
    baseline, so the result is the same whichever service is named first.
    """

    route_id: str
    category: str
    forward: SyncPlan
    backward: SyncPlan
    conflicts: tuple[PlannedAction, ...] = ()

    @property
    def write_count(self) -> int:
        return self.forward.write_count + self.backward.write_count

    @property
    def is_noop(self) -> bool:
        return self.write_count == 0

    def to_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "category": self.category,
            "forward": self.forward.to_dict(),
            "backward": self.backward.to_dict(),
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


def plan_two_way(
    *,
    route_id: str,
    category: str,
    first_by_key: dict,
    second_by_key: dict,
    baseline: RouteBaseline,
    policy: str = POLICY_NEVER_REMOVE,
    first_provider: str = "",
    second_provider: str = "",
    unresolved_keys=(),
    first_trustworthy: bool = True,
    second_trustworthy: bool = True,
    conflict_policy: str = CONFLICT_MANUAL,
) -> TwoWayPlan:
    """Reconcile two sides against what they last agreed on.

    The whole decision rests on *which side moved*, which is a question only the
    baseline can answer:

    * one side changed, the other did not — the side that changed wins, and its
      change propagates. This covers both directions of both an add and a delete.
    * both changed, and they now agree — nothing to do; they converged.
    * both changed, and they still disagree — a genuine conflict. Letting run
      order pick a winner here would silently discard one of the user's two
      edits, so by default neither side is touched and it is reported.
    * neither changed but they disagree — there is no baseline for this item
      (a first run, or an item added while the last run was failing), so
      whichever side has it is treated as the newer truth and it propagates.
    """
    policy = normalize_policy(policy)
    unresolved = set(unresolved_keys or ())

    forward_adds: list[PlannedAction] = []
    forward_removes: list[PlannedAction] = []
    forward_skips: list[PlannedAction] = []
    backward_adds: list[PlannedAction] = []
    backward_removes: list[PlannedAction] = []
    backward_skips: list[PlannedAction] = []
    conflicts: list[PlannedAction] = []
    unresolved_actions: list[PlannedAction] = []

    def _make(key, kind, item, *, reason, direction, destructive=False,
              old_state=STATE_ABSENT, new_state=STATE_ABSENT,
              confidence=CONFIDENCE_CERTAIN, managed=False) -> PlannedAction:
        title, media_type = _describe(item)
        forward = direction == "forward"
        return PlannedAction(
            key=key, kind=kind, category=category,
            source_provider=first_provider if forward else second_provider,
            destination_provider=second_provider if forward else first_provider,
            title=title, media_type=media_type, old_state=old_state,
            new_state=new_state, reason=reason, confidence=confidence,
            destructive=destructive, managed=managed, item=item or {},
            direction=direction,
        )

    def _may_remove(previous, side_trustworthy: bool) -> tuple[bool, str]:
        if policy == POLICY_NEVER_REMOVE:
            return False, REASON_POLICY_NEVER_REMOVE
        if not baseline.allows_removals:
            return False, REASON_NO_BASELINE
        if not side_trustworthy:
            return False, REASON_SOURCE_UNTRUSTWORTHY
        if policy == POLICY_REMOVE_MANAGED and not previous.managed:
            return False, REASON_UNMANAGED
        return True, ""

    for key in sorted(set(first_by_key) | set(second_by_key) | set(baseline.items)):
        previous = baseline.state(key)
        on_first = key in first_by_key
        on_second = key in second_by_key
        item = first_by_key.get(key) or second_by_key.get(key) or {}

        if key in unresolved:
            unresolved_actions.append(
                _make(key, ACTION_SKIP, item, reason=REASON_UNRESOLVED, direction="forward")
            )
            continue
        if on_first == on_second:
            continue  # both have it, or neither does

        had_first = previous.source == STATE_PRESENT
        had_second = previous.destination == STATE_PRESENT
        first_changed = baseline.allows_removals and on_first != had_first
        second_changed = baseline.allows_removals and on_second != had_second

        if first_changed and second_changed:
            conflicts.append(_make(
                key, ACTION_CONFLICT, item, reason=REASON_BOTH_CHANGED,
                direction="forward", confidence=CONFIDENCE_LIKELY,
                old_state=STATE_PRESENT if had_first else STATE_ABSENT,
                new_state=STATE_PRESENT if on_first else STATE_ABSENT,
                managed=previous.managed,
            ))
            if conflict_policy == CONFLICT_MANUAL:
                continue
            # An explicit policy may still pick a side.
            first_changed = conflict_policy == CONFLICT_SOURCE_WINS
            second_changed = not first_changed

        # Work out which side is the authority for this item, then apply it.
        if on_first and not on_second:
            if second_changed and not first_changed:
                # It was on both and has gone from the second side: a deletion
                # there, which propagates back to the first.
                allowed, why = _may_remove(previous, second_trustworthy)
                target = backward_removes if allowed else backward_skips
                target.append(_make(
                    key, ACTION_REMOVE if allowed else ACTION_SKIP,
                    first_by_key[key],
                    reason=REASON_REMOVED_ON_DESTINATION if allowed else why,
                    direction="backward", destructive=allowed,
                    old_state=STATE_PRESENT,
                    new_state=STATE_ABSENT if allowed else STATE_PRESENT,
                    managed=previous.managed,
                ))
            else:
                forward_adds.append(_make(
                    key, ACTION_ADD, first_by_key[key],
                    reason=REASON_NEW_ON_SOURCE if previous.known else REASON_FIRST_SYNC,
                    direction="forward", new_state=STATE_PRESENT,
                    managed=previous.managed,
                ))
        else:
            if first_changed and not second_changed:
                allowed, why = _may_remove(previous, first_trustworthy)
                target = forward_removes if allowed else forward_skips
                target.append(_make(
                    key, ACTION_REMOVE if allowed else ACTION_SKIP,
                    second_by_key[key],
                    reason=REASON_REMOVED_ON_SOURCE if allowed else why,
                    direction="forward", destructive=allowed,
                    old_state=STATE_PRESENT,
                    new_state=STATE_ABSENT if allowed else STATE_PRESENT,
                    managed=previous.managed,
                ))
            else:
                backward_adds.append(_make(
                    key, ACTION_ADD, second_by_key[key],
                    reason=REASON_NEW_ON_SOURCE if previous.known else REASON_FIRST_SYNC,
                    direction="backward", new_state=STATE_PRESENT,
                    managed=previous.managed,
                ))

    common = dict(
        route_id=str(route_id), category=str(category),
        baseline_version=baseline.sync_version, phase=baseline.phase, policy=policy,
    )
    forward = SyncPlan(
        source_provider=first_provider, destination_provider=second_provider,
        additions=tuple(forward_adds), removals=tuple(forward_removes),
        skipped=tuple(forward_skips), unresolved=tuple(unresolved_actions), **common,
    )
    backward = SyncPlan(
        source_provider=second_provider, destination_provider=first_provider,
        additions=tuple(backward_adds), removals=tuple(backward_removes),
        skipped=tuple(backward_skips), **common,
    )
    return TwoWayPlan(
        route_id=str(route_id), category=str(category),
        forward=forward, backward=backward, conflicts=tuple(conflicts),
    )
