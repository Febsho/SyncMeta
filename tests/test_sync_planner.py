"""The planner: deciding what changed, not merely what differs.

Comparing two lists tells you they differ; it cannot tell you which side moved.
"Missing on the destination" is either an item the user just added on the source
or one they just deleted on the destination, and those call for opposite
actions. Every test here is really about that distinction.
"""

import unittest

from src.sync.models import (
    STATE_ABSENT,
    STATE_PRESENT,
    ItemState,
    RouteBaseline,
    PHASE_ESTABLISHED,
)
from src.sync.planner import (
    CONFLICT_DESTINATION_WINS,
    CONFLICT_MANUAL,
    CONFLICT_SOURCE_WINS,
    POLICY_MIRROR,
    POLICY_NEVER_REMOVE,
    POLICY_REMOVE_MANAGED,
    REASON_BOTH_CHANGED,
    REASON_NO_BASELINE,
    REASON_REMOVED_ON_SOURCE,
    REASON_RESTORED,
    REASON_SOURCE_UNTRUSTWORTHY,
    REASON_UNMANAGED,
    normalize_policy,
    plan_membership,
    plan_two_way,
)


def _item(key: str) -> dict:
    return {"title": f"Title {key}", "media_type": "movie", "tmdb_id": key}


def _side(*keys) -> dict:
    return {key: _item(key) for key in keys}


def _baseline(established=True, **items) -> RouteBaseline:
    """A baseline from a compact spec: key="sd" / "s" / "d" / "", "!" = managed."""
    baseline = RouteBaseline(route_id="r1", category="watchlist")
    if established:
        baseline.phase = PHASE_ESTABLISHED
        baseline.sync_version = 1
    for key, spec in items.items():
        baseline.items[key] = ItemState(
            source=STATE_PRESENT if "s" in spec else STATE_ABSENT,
            destination=STATE_PRESENT if "d" in spec else STATE_ABSENT,
            synced=STATE_PRESENT if "sd" in spec.replace("!", "") else STATE_ABSENT,
            managed="!" in spec,
        )
    return baseline


def _plan(source, destination, baseline, **kwargs):
    return plan_membership(
        route_id="r1", category="watchlist",
        source_by_key=source, destination_by_key=destination,
        baseline=baseline, source_provider="trakt", destination_provider="pmdb",
        **kwargs,
    )


class PolicyNormalizationTests(unittest.TestCase):
    def test_stored_modes_map_onto_policies(self) -> None:
        self.assertEqual(normalize_policy("additive"), POLICY_NEVER_REMOVE)
        self.assertEqual(normalize_policy("managed"), POLICY_REMOVE_MANAGED)
        self.assertEqual(normalize_policy("mirror"), POLICY_MIRROR)

    def test_an_unknown_mode_refuses_to_delete(self) -> None:
        # A mode nobody understands must not fall through to a destructive one.
        self.assertEqual(normalize_policy("obliterate"), POLICY_NEVER_REMOVE)
        self.assertEqual(normalize_policy(None), POLICY_NEVER_REMOVE)


class OneWayAdditionTests(unittest.TestCase):
    def test_a_new_source_item_is_added(self) -> None:
        plan = _plan(_side("a"), {}, _baseline())
        self.assertEqual([a.key for a in plan.additions], ["a"])
        self.assertFalse(plan.additions[0].destructive)

    def test_an_item_on_both_sides_is_left_alone(self) -> None:
        plan = _plan(_side("a"), _side("a"), _baseline(a="sd"))
        self.assertTrue(plan.is_noop)
        self.assertEqual([s.key for s in plan.skipped], ["a"])

    def test_an_item_deleted_on_the_destination_is_restored_with_a_reason(self) -> None:
        # One-way means the source is authoritative, so this is an addition —
        # but the preview must say it is overruling a deletion, not adding
        # something new.
        plan = _plan(_side("a"), {}, _baseline(a="sd!"))
        self.assertEqual(plan.additions[0].reason, REASON_RESTORED)


class OneWayRemovalTests(unittest.TestCase):
    """Absence on the source only justifies a deletion if the baseline agrees."""

    def test_never_remove_never_removes(self) -> None:
        plan = _plan({}, _side("a"), _baseline(a="sd!"), policy=POLICY_NEVER_REMOVE)
        self.assertEqual(plan.removals, ())
        self.assertEqual(plan.destructive_count, 0)

    def test_a_managed_item_gone_from_the_source_is_removed(self) -> None:
        plan = _plan({}, _side("a"), _baseline(a="sd!"), policy=POLICY_REMOVE_MANAGED)
        self.assertEqual([r.key for r in plan.removals], ["a"])
        self.assertEqual(plan.removals[0].reason, REASON_REMOVED_ON_SOURCE)
        self.assertTrue(plan.removals[0].destructive)

    def test_an_unmanaged_item_is_kept(self) -> None:
        # Destination content this route never created is not its to delete.
        plan = _plan({}, _side("a"), _baseline(a="sd"), policy=POLICY_REMOVE_MANAGED)
        self.assertEqual(plan.removals, ())
        self.assertEqual(plan.skipped[0].reason, REASON_UNMANAGED)

    def test_an_item_the_source_never_had_is_kept(self) -> None:
        # It predates the route or belongs to another one. Its absence from the
        # source now is not evidence of anything.
        plan = _plan({}, _side("a"), _baseline(a="d!"), policy=POLICY_REMOVE_MANAGED)
        self.assertEqual(plan.removals, ())
        self.assertEqual(plan.skipped[0].reason, REASON_UNMANAGED)

    def test_mirror_removes_an_unmanaged_item(self) -> None:
        plan = _plan({}, _side("a"), _baseline(a="d"), policy=POLICY_MIRROR)
        self.assertEqual([r.key for r in plan.removals], ["a"])
        self.assertTrue(plan.removals[0].destructive)

    def test_no_baseline_means_no_removal_at_any_policy(self) -> None:
        for policy in (POLICY_REMOVE_MANAGED, POLICY_MIRROR):
            plan = _plan({}, _side("a"), _baseline(established=False), policy=policy)
            self.assertEqual(plan.removals, (), policy)
            self.assertEqual(plan.skipped[0].reason, REASON_NO_BASELINE)

    def test_a_first_run_may_still_add(self) -> None:
        plan = _plan(_side("a"), {}, _baseline(established=False), policy=POLICY_MIRROR)
        self.assertEqual([a.key for a in plan.additions], ["a"])


class UntrustworthySourceTests(unittest.TestCase):
    """An incomplete read must never be able to empty a destination."""

    def test_removals_are_dropped_when_the_source_could_not_be_read_fully(self) -> None:
        plan = _plan(
            {}, _side("a", "b", "c"), _baseline(a="sd!", b="sd!", c="sd!"),
            policy=POLICY_MIRROR, source_trustworthy=False,
        )
        self.assertEqual(plan.removals, ())
        self.assertEqual(len(plan.skipped), 3)
        self.assertIn(REASON_SOURCE_UNTRUSTWORTHY, plan.warnings)

    def test_additions_still_happen(self) -> None:
        # A partial read is fine to add from; it is only deletion it cannot justify.
        plan = _plan(_side("a"), {}, _baseline(), source_trustworthy=False)
        self.assertEqual(len(plan.additions), 1)


class UnresolvedTests(unittest.TestCase):
    def test_an_unresolved_item_is_never_acted_on(self) -> None:
        plan = _plan(
            {}, _side("a"), _baseline(a="sd!"),
            policy=POLICY_MIRROR, unresolved_keys={"a"},
        )
        self.assertEqual(plan.removals, ())
        self.assertEqual(plan.additions, ())
        self.assertEqual([u.key for u in plan.unresolved], ["a"])


class IdempotencyTests(unittest.TestCase):
    def test_a_settled_route_plans_nothing(self) -> None:
        plan = _plan(_side("a", "b"), _side("a", "b"), _baseline(a="sd!", b="sd!"),
                     policy=POLICY_MIRROR)
        self.assertTrue(plan.is_noop)
        self.assertEqual(plan.write_count, 0)

    def test_replanning_the_applied_state_is_a_noop(self) -> None:
        # Run one: an addition. Run two, against the state that produced, must
        # want nothing — this is what stops a route rewriting forever.
        first = _plan(_side("a"), {}, _baseline())
        self.assertEqual(len(first.additions), 1)
        second = _plan(_side("a"), _side("a"), _baseline(a="sd!"))
        self.assertTrue(second.is_noop)


class TwoWayTests(unittest.TestCase):
    """One pass over both sides, so the answer cannot depend on run order."""

    def _two_way(self, first, second, baseline, **kwargs):
        return plan_two_way(
            route_id="r1", category="watchlist",
            first_by_key=first, second_by_key=second, baseline=baseline,
            first_provider="trakt", second_provider="simkl", **kwargs,
        )

    def test_an_addition_on_the_first_side_propagates(self) -> None:
        plan = self._two_way(_side("a", "b"), _side("a"), _baseline(a="sd!"))
        self.assertEqual([x.key for x in plan.forward.additions], ["b"])
        self.assertEqual(plan.backward.additions, ())

    def test_an_addition_on_the_second_side_propagates(self) -> None:
        plan = self._two_way(_side("a"), _side("a", "b"), _baseline(a="sd!"))
        self.assertEqual([x.key for x in plan.backward.additions], ["b"])
        self.assertEqual(plan.forward.additions, ())

    def test_a_deletion_on_the_first_side_propagates(self) -> None:
        plan = self._two_way(
            {}, _side("a"), _baseline(a="sd!"), policy=POLICY_REMOVE_MANAGED,
        )
        self.assertEqual([x.key for x in plan.forward.removals], ["a"])
        self.assertEqual(plan.backward.additions, ())

    def test_a_deletion_on_the_second_side_propagates(self) -> None:
        plan = self._two_way(
            _side("a"), {}, _baseline(a="sd!"), policy=POLICY_REMOVE_MANAGED,
        )
        self.assertEqual([x.key for x in plan.backward.removals], ["a"])
        self.assertEqual(plan.forward.additions, ())

    def test_a_deletion_is_not_resurrected_by_the_other_side(self) -> None:
        # The failure two sequential one-way runs produce: the direction that
        # runs first re-adds what the user just deleted.
        plan = self._two_way(
            {}, _side("a"), _baseline(a="sd!"), policy=POLICY_REMOVE_MANAGED,
        )
        self.assertEqual(plan.backward.additions, ())

    def test_naming_the_services_the_other_way_round_gives_the_same_answer(self) -> None:
        baseline = _baseline(a="sd!")
        forward = self._two_way({}, _side("a"), baseline, policy=POLICY_REMOVE_MANAGED)
        # Swap the sides and the baseline's notion of which side is which.
        swapped_baseline = _baseline(a="sd!")
        swapped = self._two_way(_side("a"), {}, swapped_baseline, policy=POLICY_REMOVE_MANAGED)
        self.assertEqual(
            [x.key for x in forward.forward.removals],
            [x.key for x in swapped.backward.removals],
        )
        self.assertEqual(forward.write_count, swapped.write_count)

    def test_both_sides_adding_different_items_is_not_a_conflict(self) -> None:
        plan = self._two_way(_side("a", "b"), _side("a", "c"), _baseline(a="sd!"))
        self.assertEqual(plan.conflicts, ())
        self.assertEqual([x.key for x in plan.forward.additions], ["b"])
        self.assertEqual([x.key for x in plan.backward.additions], ["c"])

    def test_an_add_on_one_side_against_a_delete_on_the_other_is_a_conflict(self) -> None:
        # Baseline: only the second side had it. Now the first has it and the
        # second does not — both moved, in opposite directions.
        plan = self._two_way(
            _side("a"), {}, _baseline(a="d!"), policy=POLICY_REMOVE_MANAGED,
        )
        self.assertEqual([c.key for c in plan.conflicts], ["a"])
        self.assertEqual(plan.conflicts[0].reason, REASON_BOTH_CHANGED)

    def test_a_conflict_touches_neither_side_by_default(self) -> None:
        plan = self._two_way(
            _side("a"), {}, _baseline(a="d!"), policy=POLICY_REMOVE_MANAGED,
        )
        self.assertTrue(plan.is_noop)

    def test_a_conflict_policy_can_pick_a_side(self) -> None:
        plan = self._two_way(
            _side("a"), {}, _baseline(a="d!"), policy=POLICY_REMOVE_MANAGED,
            conflict_policy=CONFLICT_SOURCE_WINS,
        )
        self.assertEqual([x.key for x in plan.forward.additions], ["a"])
        self.assertEqual([c.key for c in plan.conflicts], ["a"])

    def test_the_other_conflict_policy_picks_the_other_side(self) -> None:
        plan = self._two_way(
            _side("a"), {}, _baseline(a="d!"), policy=POLICY_REMOVE_MANAGED,
            conflict_policy=CONFLICT_DESTINATION_WINS,
        )
        self.assertEqual([x.key for x in plan.backward.removals], ["a"])

    def test_no_baseline_means_union_and_no_removals(self) -> None:
        plan = self._two_way(
            _side("a"), _side("b"), _baseline(established=False),
            policy=POLICY_REMOVE_MANAGED,
        )
        self.assertEqual([x.key for x in plan.forward.additions], ["a"])
        self.assertEqual([x.key for x in plan.backward.additions], ["b"])
        self.assertEqual(plan.forward.removals, ())
        self.assertEqual(plan.backward.removals, ())

    def test_a_settled_two_way_route_plans_nothing(self) -> None:
        plan = self._two_way(_side("a"), _side("a"), _baseline(a="sd!"),
                             policy=POLICY_REMOVE_MANAGED)
        self.assertTrue(plan.is_noop)

    def test_an_untrustworthy_side_cannot_cause_a_removal(self) -> None:
        plan = self._two_way(
            {}, _side("a"), _baseline(a="sd!"),
            policy=POLICY_REMOVE_MANAGED, first_trustworthy=False,
        )
        self.assertEqual(plan.forward.removals, ())
        self.assertEqual(
            plan.forward.skipped[0].reason, REASON_SOURCE_UNTRUSTWORTHY,
        )


class PlanShapeTests(unittest.TestCase):
    def test_a_plan_is_immutable(self) -> None:
        plan = _plan(_side("a"), {}, _baseline())
        with self.assertRaises(Exception):
            plan.additions = ()
        with self.assertRaises(Exception):
            plan.additions[0].kind = "remove"

    def test_destructive_percent_is_reported_for_the_guard(self) -> None:
        plan = _plan({}, _side("a", "b"), _baseline(a="sd!", b="sd!"),
                     policy=POLICY_REMOVE_MANAGED)
        self.assertEqual(plan.destructive_count, 2)
        self.assertEqual(plan.destructive_percent(10), 20.0)
        self.assertEqual(plan.destructive_percent(0), 0.0)

    def test_a_plan_serializes_with_reasons_for_the_preview(self) -> None:
        plan = _plan({}, _side("a"), _baseline(a="sd!"), policy=POLICY_REMOVE_MANAGED)
        payload = plan.to_dict()
        self.assertEqual(payload["counts"]["removals"], 1)
        self.assertEqual(payload["removals"][0]["reason"], REASON_REMOVED_ON_SOURCE)
        self.assertTrue(payload["removals"][0]["destructive"])

    def test_the_payload_carries_no_provider_item_blobs(self) -> None:
        # Plans are shown in the UI; the raw provider dicts must not ride along.
        plan = _plan(_side("a"), {}, _baseline())
        self.assertNotIn("item", plan.to_dict()["additions"][0])
