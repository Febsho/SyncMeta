"""Perform a plan, and report honestly about what actually happened.

The reason this is separate from the planner is §17: a run where 95 writes land
and 5 fail must not be recorded as "synchronised". If it is, the next run
compares against a baseline claiming all 100 arrived and never retries the five.
Record it as an outright failure instead and the next run re-sends all 100,
which for history means duplicate plays.

So execution tracks each action independently and hands back two things: what
succeeded (which may safely become the new agreement) and whether anything did
not (which decides between ``commit`` and ``record_partial`` on the store).

Writes go out in batches because that is what the provider APIs take and what
their rate limits expect. Batching costs per-item certainty: an adapter reports
"added: 8" for ten items without saying which two it dropped. That is recorded
as it is — eight successes and two ``not_found`` — rather than pretended
otherwise, and the ones it could not confirm simply stay outstanding for the
next run. Per-item confirmation would need adapter support that does not exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import STATE_ABSENT, STATE_PRESENT, ItemState
from .planner import ACTION_ADD, ACTION_REMOVE, PlannedAction, SyncPlan

logger = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_BLOCKED = "blocked"
#: The provider positively said it could not match this item — a title with no
#: id it understands. Retrying will not help, so it does not hold the route back
#: from establishing a baseline; one permanently unmappable entry must not stop
#: a route ever agreeing on anything.
STATUS_NOT_FOUND = "not_found"
#: The write may or may not have landed: the adapter confirmed fewer items than
#: were sent without saying which. Treated as still outstanding, because the
#: alternative is recording work as done that may not be.
STATUS_UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class ActionOutcome:
    action: PlannedAction
    status: str
    error: str = ""

    @property
    def applied(self) -> bool:
        return self.status == STATUS_SUCCESS


@dataclass
class ExecutionResult:
    """What a plan actually did.

    ``complete`` is the flag the state store keys off: True means every planned
    write either landed or was deliberately skipped, and the run may become the
    new agreement. False means something is still outstanding.
    """

    route_id: str = ""
    category: str = ""
    outcomes: list[ActionOutcome] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    not_found: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    unconfirmed: int = 0

    @property
    def complete(self) -> bool:
        """Whether this run may become the new agreement.

        Anything still outstanding — a failed write, one the provider never
        confirmed, an action the guard blocked — means it may not. An item the
        provider explicitly could not match is not outstanding: retrying will
        not change its answer.
        """
        return not self.errors and not self.outstanding()

    def applied_actions(self) -> list[PlannedAction]:
        return [o.action for o in self.outcomes if o.applied]

    def outstanding(self) -> list[PlannedAction]:
        """Actions the next run still has to make good."""
        return [
            o.action for o in self.outcomes
            if o.status in (STATUS_FAILED, STATUS_UNCONFIRMED, STATUS_BLOCKED)
        ]

    def item_states(self, previous: dict[str, ItemState] | None = None) -> dict[str, ItemState]:
        """The agreement this execution establishes, item by item.

        Only confirmed writes move an item's destination state. An action that
        failed leaves the item exactly as the previous agreement had it, so the
        next run still sees the work as outstanding.
        """
        states = dict(previous or {})
        for outcome in self.outcomes:
            action = outcome.action
            if not outcome.applied:
                continue
            existing = states.get(action.key) or ItemState()
            if action.kind == ACTION_ADD:
                states[action.key] = ItemState(
                    source=STATE_PRESENT, destination=STATE_PRESENT,
                    synced=STATE_PRESENT, managed=True, action="added",
                )
            elif action.kind == ACTION_REMOVE:
                states[action.key] = ItemState(
                    source=STATE_ABSENT, destination=STATE_ABSENT,
                    synced=STATE_ABSENT, managed=False, action="removed",
                )
            else:
                states[action.key] = existing
        return states

    def to_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "category": self.category,
            "added": self.added,
            "removed": self.removed,
            "not_found": self.not_found,
            "unconfirmed": self.unconfirmed,
            "failed": self.failed,
            "complete": self.complete,
            "dry_run": self.dry_run,
            "errors": list(self.errors),
        }


def _batch(
    actions: list[PlannedAction],
    writer,
    *,
    count_key: str,
    result: ExecutionResult,
    dry_run: bool,
) -> list[PlannedAction]:
    """Send one batch and record an outcome for every action in it."""
    if not actions:
        return []
    if dry_run:
        for action in actions:
            result.outcomes.append(ActionOutcome(action, STATUS_SKIPPED))
        return []

    items = [action.item for action in actions]
    try:
        totals = writer(items) or {}
    except Exception as exc:
        message = str(exc)
        for action in actions:
            result.outcomes.append(ActionOutcome(action, STATUS_FAILED, message))
        result.failed += len(actions)
        result.errors.append(message)
        logger.warning("Sync write failed for %d action(s)", len(actions), exc_info=True)
        return []

    # The adapter says how many landed but not which. Treat the batch in order:
    # the confirmed ones succeeded, the remainder stays outstanding rather than
    # being claimed either way.
    confirmed = max(0, min(_count(totals, count_key), len(actions)))
    applied = actions[:confirmed]
    rest = actions[confirmed:]
    # The provider may say how many it could not match. Those are permanent;
    # anything left over is simply unaccounted for and stays outstanding.
    reported_missing = max(0, min(_count(totals, "not_found"), len(rest)))
    unmatched, unconfirmed = rest[:reported_missing], rest[reported_missing:]

    for action in applied:
        result.outcomes.append(ActionOutcome(action, STATUS_SUCCESS))
    for action in unmatched:
        result.outcomes.append(ActionOutcome(
            action, STATUS_NOT_FOUND, "the provider could not match this item",
        ))
    for action in unconfirmed:
        result.outcomes.append(ActionOutcome(
            action, STATUS_UNCONFIRMED, "the provider did not confirm this item",
        ))
    result.not_found += len(unmatched)
    result.unconfirmed += len(unconfirmed)
    if count_key == "added":
        result.added += len(applied)
    else:
        result.removed += len(applied)
    return applied


def _count(totals: dict, key: str) -> int:
    """Read one count from an adapter result, degrading rather than raising."""
    def walk(value) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return sum(walk(inner) for inner in value.values())
        return 0

    return walk((totals or {}).get(key))


def execute_plan(
    plan: SyncPlan,
    *,
    add_writer=None,
    remove_writer=None,
    dry_run: bool = False,
) -> ExecutionResult:
    """Carry out ``plan``, one batch per kind, recording every action's fate.

    Removals go first. A route that both adds and removes is reshaping a list,
    and doing the destructive half while the read that justified it is freshest
    means a failure part-way through leaves the smaller, recoverable mess.
    """
    result = ExecutionResult(
        route_id=plan.route_id, category=plan.category, dry_run=dry_run,
    )

    for action in plan.unresolved:
        result.outcomes.append(ActionOutcome(action, STATUS_SKIPPED, action.reason))
    for action in plan.skipped:
        result.outcomes.append(ActionOutcome(action, STATUS_SKIPPED, action.reason))
    for action in plan.conflicts:
        result.outcomes.append(ActionOutcome(action, STATUS_BLOCKED, action.reason))

    if plan.removals and remove_writer is None and not dry_run:
        for action in plan.removals:
            result.outcomes.append(ActionOutcome(
                action, STATUS_BLOCKED, "no remove writer supplied",
            ))
    else:
        _batch(
            list(plan.removals), remove_writer,
            count_key="deleted", result=result, dry_run=dry_run,
        )

    if plan.additions and add_writer is None and not dry_run:
        for action in plan.additions:
            result.outcomes.append(ActionOutcome(
                action, STATUS_BLOCKED, "no add writer supplied",
            ))
    else:
        _batch(
            list(plan.additions), add_writer,
            count_key="added", result=result, dry_run=dry_run,
        )

    if dry_run:
        # Nothing was written, so report the plan's intent rather than zeroes.
        result.added = len(plan.additions)
        result.removed = len(plan.removals)
    return result
