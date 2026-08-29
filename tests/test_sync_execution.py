"""Safety validation and plan execution.

Two independent jobs. The guard decides whether a plan's destructive half is
credible enough to perform. The executor performs what survives and reports
exactly what landed — which is what lets a run where some writes failed be
retried without either repeating the successes or forgetting the failures.
"""

import unittest

from src.sync.executor import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_NOT_FOUND,
    STATUS_UNCONFIRMED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    execute_plan,
)
from src.sync.models import PHASE_ESTABLISHED, STATE_PRESENT, ItemState, RouteBaseline
from src.sync.planner import (
    POLICY_MIRROR,
    POLICY_REMOVE_MANAGED,
    plan_membership,
)
from src.sync.safety import SafetyPolicy, SafetyVerdict, enforce, evaluate


def _item(key: str) -> dict:
    return {"title": f"Title {key}", "media_type": "movie", "tmdb_id": key}


def _side(*keys) -> dict:
    return {key: _item(key) for key in keys}


def _established(*keys) -> RouteBaseline:
    baseline = RouteBaseline("r1", "watchlist", phase=PHASE_ESTABLISHED, sync_version=1)
    for key in keys:
        baseline.items[key] = ItemState(
            source=STATE_PRESENT, destination=STATE_PRESENT,
            synced=STATE_PRESENT, managed=True,
        )
    return baseline


def _removal_plan(count: int, policy=POLICY_REMOVE_MANAGED):
    keys = [f"k{i}" for i in range(count)]
    return plan_membership(
        route_id="r1", category="watchlist",
        source_by_key={}, destination_by_key=_side(*keys),
        baseline=_established(*keys), policy=policy,
    )


class SafetyThresholdTests(unittest.TestCase):
    def test_a_small_removal_passes(self) -> None:
        plan = _removal_plan(2)
        verdict = evaluate(plan, destination_size=40, source_size=38)
        self.assertTrue(verdict.allowed)

    def test_emptying_a_tiny_list_is_not_suspicious(self) -> None:
        # Removing 2 of 3 is 67% and entirely ordinary. Pausing it is noise.
        plan = _removal_plan(2)
        verdict = evaluate(plan, destination_size=3, source_size=1)
        self.assertTrue(verdict.allowed)

    def test_too_many_removals_blocks(self) -> None:
        plan = _removal_plan(30)
        verdict = evaluate(plan, destination_size=1000, source_size=970)
        self.assertTrue(verdict.blocked)
        self.assertIn("over the limit", verdict.explain())

    def test_too_large_a_share_blocks(self) -> None:
        plan = _removal_plan(6)
        verdict = evaluate(plan, destination_size=20, source_size=14)
        self.assertTrue(verdict.blocked)
        self.assertIn("%", verdict.explain())

    def test_a_source_returning_nothing_blocks(self) -> None:
        # The shape every mass-deletion incident has.
        plan = _removal_plan(30)
        verdict = evaluate(plan, destination_size=30, source_size=0)
        self.assertTrue(verdict.blocked)
        self.assertIn("outage", verdict.explain())

    def test_an_untrustworthy_source_blocks(self) -> None:
        verdict = evaluate(
            _removal_plan(2), destination_size=100, source_size=98,
            source_trustworthy=False,
        )
        self.assertTrue(verdict.blocked)

    def test_a_missing_baseline_blocks(self) -> None:
        verdict = evaluate(
            _removal_plan(2), destination_size=100, source_size=98,
            baseline_established=False,
        )
        self.assertTrue(verdict.blocked)

    def test_a_mapping_failure_spike_warns_without_blocking(self) -> None:
        plan = plan_membership(
            route_id="r1", category="watchlist",
            source_by_key=_side("a"), destination_by_key={},
            baseline=_established(), unresolved_keys={"a"},
        )
        verdict = evaluate(plan, destination_size=10, source_size=1)
        self.assertTrue(verdict.allowed)
        self.assertTrue(any("resolved" in w for w in verdict.warnings))


class SafetyOverrideTests(unittest.TestCase):
    """The override is for a person acting on a preview, never for a schedule."""

    def test_an_override_allows_the_run_and_records_why_it_was_blocked(self) -> None:
        plan = _removal_plan(30)
        verdict = evaluate(
            plan, destination_size=1000, source_size=970,
            policy=SafetyPolicy(allow_destructive_override=True),
        )
        self.assertTrue(verdict.allowed)
        self.assertTrue(verdict.overridden)
        self.assertTrue(verdict.warnings)

    def test_the_default_policy_never_overrides(self) -> None:
        # An automatic run constructs SafetyPolicy() and so cannot bypass.
        self.assertFalse(SafetyPolicy().allow_destructive_override)

    def test_a_disabled_guard_still_blocks_an_unreadable_source(self) -> None:
        # Turning the thresholds off is not permission to delete on bad data.
        verdict = evaluate(
            _removal_plan(30), destination_size=1000, source_size=970,
            policy=SafetyPolicy(enabled=False), source_trustworthy=False,
        )
        self.assertTrue(verdict.blocked)


class SafetyEnforcementTests(unittest.TestCase):
    def test_blocking_keeps_the_additions(self) -> None:
        # Whatever made the removals unsafe says nothing about items the source
        # positively reported.
        plan = plan_membership(
            route_id="r1", category="watchlist",
            source_by_key=_side("new"), destination_by_key=_side(*[f"k{i}" for i in range(30)]),
            baseline=_established(*[f"k{i}" for i in range(30)]),
            policy=POLICY_MIRROR,
        )
        self.assertEqual(len(plan.removals), 30)
        verdict = evaluate(plan, destination_size=30, source_size=1)
        guarded = enforce(plan, verdict)
        self.assertEqual(guarded.removals, ())
        self.assertEqual(len(guarded.additions), 1)
        self.assertTrue(any("safety guard" in w for w in guarded.warnings))

    def test_an_allowed_plan_passes_through_untouched(self) -> None:
        plan = _removal_plan(2)
        verdict = evaluate(plan, destination_size=40, source_size=38)
        self.assertIs(enforce(plan, verdict), plan)


class ExecutionTests(unittest.TestCase):
    def _plan(self, adds=(), removes=()):
        return plan_membership(
            route_id="r1", category="watchlist",
            source_by_key=_side(*adds),
            destination_by_key=_side(*removes),
            baseline=_established(*removes), policy=POLICY_REMOVE_MANAGED,
        )

    def test_a_clean_run_is_complete(self) -> None:
        plan = self._plan(adds=("a", "b"))
        result = execute_plan(plan, add_writer=lambda items: {"added": len(items)})
        self.assertTrue(result.complete)
        self.assertEqual(result.added, 2)
        self.assertEqual(len(result.outstanding()), 0)

    def test_a_failed_write_is_not_complete_and_stays_outstanding(self) -> None:
        plan = self._plan(adds=("a", "b"))

        def boom(items):
            raise RuntimeError("503 from the provider")

        result = execute_plan(plan, add_writer=boom)
        self.assertFalse(result.complete)
        self.assertEqual(result.failed, 2)
        self.assertEqual(len(result.outstanding()), 2)
        self.assertTrue(all(o.status == STATUS_FAILED for o in result.outcomes
                            if o.action.kind == "add"))

    def test_a_partly_confirmed_batch_keeps_the_rest_outstanding(self) -> None:
        # The adapter says 1 of 2 landed but not which. The unconfirmed one is
        # left outstanding rather than claimed either way.
        plan = self._plan(adds=("a", "b"))
        result = execute_plan(plan, add_writer=lambda items: {"added": 1})
        self.assertEqual(result.added, 1)
        self.assertEqual(result.unconfirmed, 1)
        self.assertEqual(len(result.outstanding()), 1)
        self.assertFalse(result.complete)
        self.assertEqual(
            [o.status for o in result.outcomes if o.action.kind == "add"],
            [STATUS_SUCCESS, STATUS_UNCONFIRMED],
        )

    def test_an_item_the_provider_cannot_match_does_not_hold_the_route_back(self) -> None:
        # One permanently unmappable title must not stop the route ever
        # agreeing on anything.
        plan = self._plan(adds=("a", "b"))
        result = execute_plan(
            plan, add_writer=lambda items: {"added": 1, "not_found": 1},
        )
        self.assertEqual(result.not_found, 1)
        self.assertEqual(result.unconfirmed, 0)
        self.assertTrue(result.complete)
        self.assertEqual(result.outstanding(), [])

    def test_only_confirmed_writes_enter_the_agreement(self) -> None:
        plan = self._plan(adds=("a", "b"))
        result = execute_plan(plan, add_writer=lambda items: {"added": 1})
        states = result.item_states()
        applied = [key for key, state in states.items() if state.destination == STATE_PRESENT]
        self.assertEqual(len(applied), 1)

    def test_a_failed_run_records_no_agreement_at_all(self) -> None:
        plan = self._plan(adds=("a",))

        def boom(items):
            raise RuntimeError("nope")

        result = execute_plan(plan, add_writer=boom)
        self.assertEqual(result.item_states(), {})

    def test_removals_run_before_additions(self) -> None:
        order: list[str] = []
        plan = self._plan(adds=("a",), removes=("k0",))
        execute_plan(
            plan,
            add_writer=lambda items: (order.append("add"), {"added": len(items)})[1],
            remove_writer=lambda items: (order.append("remove"), {"deleted": len(items)})[1],
        )
        self.assertEqual(order, ["remove", "add"])

    def test_a_dry_run_writes_nothing_but_reports_the_intent(self) -> None:
        plan = self._plan(adds=("a", "b"))
        called = []
        result = execute_plan(
            plan, add_writer=lambda items: called.append(items), dry_run=True,
        )
        self.assertEqual(called, [])
        self.assertEqual(result.added, 2)
        self.assertTrue(all(
            o.status == STATUS_SKIPPED for o in result.outcomes if o.action.kind == "add"
        ))

    def test_skipped_and_unresolved_actions_are_recorded_not_dropped(self) -> None:
        plan = plan_membership(
            route_id="r1", category="watchlist",
            source_by_key=_side("a"), destination_by_key=_side("a", "b"),
            baseline=_established("a", "b"), unresolved_keys={"a"},
            policy=POLICY_REMOVE_MANAGED,
        )
        result = execute_plan(plan, remove_writer=lambda items: {"deleted": len(items)})
        statuses = {o.status for o in result.outcomes}
        self.assertIn(STATUS_SKIPPED, statuses)

    def test_a_missing_writer_blocks_rather_than_silently_succeeding(self) -> None:
        plan = self._plan(adds=("a",))
        result = execute_plan(plan)
        self.assertFalse(result.complete or result.outcomes[0].applied)
        self.assertEqual(result.outcomes[0].status, STATUS_BLOCKED)


class RetryTests(unittest.TestCase):
    """A partial run must retry only what is still outstanding."""

    def test_the_second_run_replans_only_the_failure(self) -> None:
        source = _side("a", "b")
        baseline = RouteBaseline("r1", "watchlist", phase=PHASE_ESTABLISHED, sync_version=1)

        first = plan_membership(
            route_id="r1", category="watchlist",
            source_by_key=source, destination_by_key={}, baseline=baseline,
        )
        self.assertEqual(len(first.additions), 2)
        result = execute_plan(first, add_writer=lambda items: {"added": 1})
        self.assertFalse(result.complete)

        # Only the confirmed one is on the destination now, and only it enters
        # the agreement. The next plan wants exactly the other one.
        applied = result.applied_actions()
        destination = {a.key: source[a.key] for a in applied}
        baseline.items = result.item_states()
        second = plan_membership(
            route_id="r1", category="watchlist",
            source_by_key=source, destination_by_key=destination, baseline=baseline,
        )
        self.assertEqual([a.key for a in second.additions], ["b"])

    def test_a_successful_retry_then_settles(self) -> None:
        source = _side("a")
        baseline = RouteBaseline("r1", "watchlist", phase=PHASE_ESTABLISHED, sync_version=1)
        plan = plan_membership(
            route_id="r1", category="watchlist",
            source_by_key=source, destination_by_key={}, baseline=baseline,
        )
        result = execute_plan(plan, add_writer=lambda items: {"added": len(items)})
        baseline.items = result.item_states()
        settled = plan_membership(
            route_id="r1", category="watchlist",
            source_by_key=source, destination_by_key=source, baseline=baseline,
        )
        self.assertTrue(settled.is_noop)


class OverrideScopeTests(unittest.TestCase):
    """An override waives thresholds, never the quality of the evidence.

    A person can look at a preview and decide a large deletion is right. Nobody
    can decide that a source which failed to read really was empty.
    """

    def _verdict(self, **kwargs):
        return evaluate(
            _removal_plan(30), destination_size=1000, source_size=970,
            policy=SafetyPolicy(allow_destructive_override=True), **kwargs,
        )

    def test_a_threshold_block_can_be_overridden(self) -> None:
        verdict = self._verdict()
        self.assertTrue(verdict.allowed)
        self.assertTrue(verdict.overridden)

    def test_an_unreadable_source_cannot_be_overridden(self) -> None:
        self.assertTrue(self._verdict(source_trustworthy=False).blocked)

    def test_an_unreadable_destination_cannot_be_overridden(self) -> None:
        self.assertTrue(self._verdict(destination_trustworthy=False).blocked)

    def test_a_missing_baseline_cannot_be_overridden(self) -> None:
        self.assertTrue(self._verdict(baseline_established=False).blocked)

    def test_a_source_returning_nothing_cannot_be_overridden(self) -> None:
        verdict = evaluate(
            _removal_plan(30), destination_size=30, source_size=0,
            policy=SafetyPolicy(allow_destructive_override=True),
        )
        self.assertTrue(verdict.blocked)
        self.assertIn("outage", verdict.explain())
