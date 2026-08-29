"""Per-route synchronization baselines.

The baseline is what lets the engine tell a user's edit apart from a provider
hiccup. "Missing on the destination" says nothing on its own — the item may be
newly added on the source, or deliberately deleted on the destination — and only
the last state the two sides agreed on can separate those.

Everything here defends one rule: **only a run that actually succeeded may
advance the agreement.** A failed read must never be able to teach the engine
that the items it could not see are gone.
"""

import tempfile
import unittest
from pathlib import Path

from src.sync.models import (
    PHASE_ESTABLISHED,
    PHASE_INITIALIZING,
    STATE_ABSENT,
    STATE_PRESENT,
    FetchOutcome,
    FetchStatus,
    ItemState,
    RouteBaseline,
    RouteObservation,
)
from src.sync.state_store import SyncStateStore


class FetchStatusTests(unittest.TestCase):
    """A read that failed is not an empty list."""

    def test_only_a_real_success_may_justify_a_removal(self) -> None:
        for status in (FetchStatus.SUCCESS_WITH_ITEMS, FetchStatus.SUCCESS_EMPTY):
            self.assertTrue(status.trustworthy_for_removals, status)
        for status in (FetchStatus.PARTIAL, FetchStatus.FAILED,
                       FetchStatus.RATE_LIMITED, FetchStatus.UNAUTHORIZED,
                       FetchStatus.TIMEOUT):
            self.assertFalse(status.trustworthy_for_removals, status)

    def test_an_incomplete_success_is_not_trustworthy_either(self) -> None:
        # A page that never arrived looks exactly like a page that was emptied.
        outcome = FetchOutcome(
            "trakt", "watchlist", FetchStatus.SUCCESS_WITH_ITEMS,
            item_count=120, complete=False,
        )
        self.assertFalse(outcome.trustworthy_for_removals)
        self.assertIn("partial", outcome.describe())

    def test_an_unknown_status_degrades_to_failed(self) -> None:
        self.assertEqual(
            FetchOutcome.from_dict({"status": "who knows"}).status, FetchStatus.FAILED,
        )


class ItemStatePackingTests(unittest.TestCase):
    """A real profile holds tens of thousands of these, so they pack tight."""

    def test_a_full_state_round_trips(self) -> None:
        state = ItemState(
            source=STATE_PRESENT, destination=STATE_PRESENT, synced=STATE_PRESENT,
            managed=True, changed_source_at=3, changed_destination_at=4, action="added",
        )
        self.assertEqual(ItemState.from_list(state.to_list()), state)

    def test_trailing_defaults_are_trimmed(self) -> None:
        self.assertEqual(ItemState().to_list(), [STATE_ABSENT])

    def test_a_trimmed_state_still_round_trips(self) -> None:
        state = ItemState(source=STATE_PRESENT)
        self.assertEqual(ItemState.from_list(state.to_list()), state)

    def test_garbage_does_not_raise(self) -> None:
        self.assertEqual(ItemState.from_list("nonsense"), ItemState())
        self.assertEqual(ItemState.from_list(None), ItemState())


class BaselineLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "state.json"
        self.store = SyncStateStore(self.path)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _items(self, *keys) -> dict:
        return {
            key: ItemState(
                source=STATE_PRESENT, destination=STATE_PRESENT,
                synced=STATE_PRESENT, managed=True,
            )
            for key in keys
        }

    def test_a_route_that_never_ran_may_not_remove(self) -> None:
        baseline = self.store.baseline("r1", "watchlist")
        self.assertEqual(baseline.phase, PHASE_INITIALIZING)
        self.assertFalse(baseline.allows_removals)

    def test_one_success_establishes_the_baseline(self) -> None:
        self.store.commit("r1", "watchlist", items=self._items("m:1"))
        baseline = self.store.baseline("r1", "watchlist")
        self.assertEqual(baseline.phase, PHASE_ESTABLISHED)
        self.assertTrue(baseline.allows_removals)
        self.assertEqual(baseline.sync_version, 1)
        self.assertTrue(baseline.last_successful_sync)

    def test_a_failure_leaves_the_agreement_untouched(self) -> None:
        self.store.commit("r1", "watchlist", items=self._items("m:1"))
        before = self.store.baseline("r1", "watchlist")
        version, synced_at, items = before.sync_version, before.last_successful_sync, dict(before.items)

        self.store.record_failure("r1", "watchlist", "Trakt read timed out")

        after = self.store.baseline("r1", "watchlist")
        self.assertEqual(after.sync_version, version)
        self.assertEqual(after.last_successful_sync, synced_at)
        self.assertEqual(after.items, items)
        self.assertIn("timed out", after.last_error)
        self.assertTrue(after.last_attempt)

    def test_a_failure_on_a_new_route_does_not_establish_it(self) -> None:
        # Otherwise the *next* run would think it had an agreement to diff
        # against and could conclude a removal from it.
        self.store.record_failure("r1", "watchlist", "401 unauthorized")
        self.assertFalse(self.store.baseline("r1", "watchlist").allows_removals)

    def test_a_partial_run_keeps_what_landed_without_claiming_success(self) -> None:
        self.store.commit("r1", "watchlist", items=self._items("m:1"))
        before = self.store.baseline("r1", "watchlist")

        self.store.record_partial(
            "r1", "watchlist", applied=self._items("m:2"), error="3 of 100 writes failed",
        )

        after = self.store.baseline("r1", "watchlist")
        self.assertEqual(after.sync_version, before.sync_version)
        self.assertEqual(after.last_successful_sync, before.last_successful_sync)
        self.assertEqual(sorted(after.items), ["m:1", "m:2"])
        self.assertIn("writes failed", after.last_error)

    def test_a_commit_replaces_rather_than_merges(self) -> None:
        # The agreement is the whole observed state, not an accumulation.
        self.store.commit("r1", "watchlist", items=self._items("m:1", "m:2"))
        self.store.commit("r1", "watchlist", items=self._items("m:1"))
        self.assertEqual(sorted(self.store.baseline("r1", "watchlist").items), ["m:1"])

    def test_categories_are_independent(self) -> None:
        self.store.commit("r1", "watchlist", items=self._items("m:1"))
        self.assertTrue(self.store.baseline("r1", "watchlist").allows_removals)
        self.assertFalse(self.store.baseline("r1", "history").allows_removals)

    def test_state_survives_a_reload(self) -> None:
        self.store.commit("r1", "watchlist", items=self._items("m:1"))
        reopened = SyncStateStore(self.path)
        baseline = reopened.baseline("r1", "watchlist")
        self.assertEqual(baseline.phase, PHASE_ESTABLISHED)
        self.assertEqual(baseline.managed_keys(), {"m:1"})

    def test_a_corrupt_file_falls_back_to_no_baseline(self) -> None:
        # Safe direction: no baseline means the next run may add but not delete.
        self.path.write_text("{not json", encoding="utf-8")
        store = SyncStateStore(self.path)
        self.assertFalse(store.baseline("r1", "watchlist").allows_removals)

    def test_forgetting_a_route_drops_every_category(self) -> None:
        self.store.commit("r1", "watchlist", items=self._items("m:1"))
        self.store.commit("r1", "history", items=self._items("m:2"))
        self.store.commit("r2", "watchlist", items=self._items("m:3"))
        self.assertEqual(self.store.forget_route("r1"), 2)
        self.assertEqual(self.store.route_ids(), {"r2"})

    def test_pruning_keeps_only_live_routes(self) -> None:
        self.store.commit("r1", "watchlist", items=self._items("m:1"))
        self.store.commit("r2", "watchlist", items=self._items("m:2"))
        self.store.prune_to(["r2"])
        self.assertEqual(self.store.route_ids(), {"r2"})


class ManagedKeyMigrationTests(unittest.TestCase):
    """Upgraded profiles keep their ownership record but not a false agreement."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = SyncStateStore(Path(self._dir.name) / "state.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_ownership_is_adopted(self) -> None:
        adopted = self.store.adopt_managed_keys({
            "p1": {"watchlist": ["movie:tmdb:1", "movie:tmdb:2"]},
        })
        self.assertEqual(adopted, 2)
        self.assertEqual(
            self.store.baseline("p1", "watchlist").managed_keys(),
            {"movie:tmdb:1", "movie:tmdb:2"},
        )

    def test_migration_does_not_authorise_removals(self) -> None:
        # The old store never recorded what the source looked like, so it cannot
        # answer "did this disappear since we agreed". The first run after an
        # upgrade must not delete on the strength of a comparison never made.
        self.store.adopt_managed_keys({"p1": {"watchlist": ["movie:tmdb:1"]}})
        baseline = self.store.baseline("p1", "watchlist")
        self.assertEqual(baseline.phase, PHASE_INITIALIZING)
        self.assertFalse(baseline.allows_removals)

    def test_a_real_baseline_is_never_overwritten_by_migration(self) -> None:
        self.store.commit("p1", "watchlist", items={"movie:tmdb:9": ItemState(managed=True)})
        self.store.adopt_managed_keys({"p1": {"watchlist": ["movie:tmdb:1"]}})
        baseline = self.store.baseline("p1", "watchlist")
        self.assertEqual(baseline.phase, PHASE_ESTABLISHED)
        self.assertEqual(baseline.managed_keys(), {"movie:tmdb:9"})

    def test_malformed_input_is_ignored(self) -> None:
        self.assertEqual(self.store.adopt_managed_keys({"p1": "nonsense"}), 0)
        self.assertEqual(self.store.adopt_managed_keys(None), 0)


class RouteObservationTests(unittest.TestCase):
    """Only a clean run may be accepted as a new agreement."""

    def _fetch(self, status=FetchStatus.SUCCESS_WITH_ITEMS, complete=True):
        return FetchOutcome("trakt", "watchlist", status, item_count=5, complete=complete)

    def test_a_clean_run_is_trustworthy(self) -> None:
        observed = RouteObservation(
            "r1", "watchlist",
            source_fetch=self._fetch(), destination_fetch=self._fetch(),
        )
        self.assertTrue(observed.trustworthy)

    def test_a_failed_write_is_not(self) -> None:
        observed = RouteObservation(
            "r1", "watchlist", complete=False,
            source_fetch=self._fetch(), destination_fetch=self._fetch(),
        )
        self.assertFalse(observed.trustworthy)

    def test_a_timed_out_read_is_not(self) -> None:
        observed = RouteObservation(
            "r1", "watchlist",
            source_fetch=self._fetch(FetchStatus.TIMEOUT),
            destination_fetch=self._fetch(),
        )
        self.assertFalse(observed.trustworthy)

    def test_an_incomplete_read_is_not(self) -> None:
        observed = RouteObservation(
            "r1", "watchlist",
            source_fetch=self._fetch(complete=False),
            destination_fetch=self._fetch(),
        )
        self.assertFalse(observed.trustworthy)

    def test_a_missing_read_is_not(self) -> None:
        self.assertFalse(RouteObservation("r1", "watchlist").trustworthy)


class BaselinesFromRealRunsTests(unittest.TestCase):
    """The observations a real pair run produces, fed through the store.

    This is the seam that matters: the planner landing next reads these
    baselines, so what a run records now has to be the truth about what it saw.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = SyncStateStore(Path(self._dir.name) / "state.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    @staticmethod
    def _movie(tmdb_id: str) -> dict:
        return {
            "title": f"Film {tmdb_id}", "media_type": "movie",
            "tmdb_id": tmdb_id, "ids": {"tmdb": tmdb_id},
        }

    def _run(self, source_items, target_items, *, source_error="", write_error=""):
        from src.config import SyncPair
        from src.cross_sync import CrossSyncService
        from src.providers import CATEGORY_WATCHLIST, ProviderAdapter, item_key

        class Fake(ProviderAdapter):
            def __init__(self, key, contents, *, reads, writes,
                         fetch_error="", add_error=""):
                self.key, self.label = key, key.title()
                self.reads, self.writes = reads, writes
                self._contents = {CATEGORY_WATCHLIST: list(contents)}
                self._fetch_error, self._add_error = fetch_error, add_error

            def fetch(self, category, source_lists=None):
                if self._fetch_error:
                    raise RuntimeError(self._fetch_error)
                return list(self._contents.get(category, []))

            def add(self, category, items, target_list="", **kwargs):
                if self._add_error:
                    raise RuntimeError(self._add_error)
                self._contents.setdefault(category, []).extend(items)
                return {"added": len(items)}

            def remove(self, category, items, target_list=""):
                keys = {item_key(i) for i in items}
                self._contents[category] = [
                    i for i in self._contents.get(category, [])
                    if item_key(i) not in keys
                ]
                return {"deleted": len(items)}

        source = Fake("trakt", source_items, reads=(CATEGORY_WATCHLIST,), writes=(),
                      fetch_error=source_error)
        target = Fake("pmdb", target_items, reads=(CATEGORY_WATCHLIST,),
                      writes=(CATEGORY_WATCHLIST,), add_error=write_error)
        pair = SyncPair.from_dict({
            "pair_id": "r1", "source": "trakt", "target": "pmdb",
            "categories": [CATEGORY_WATCHLIST], "removal_mode": "additive",
        })
        service = CrossSyncService({"trakt": source, "pmdb": target})
        service.run_pair(pair)
        return service.route_states

    def _apply(self, observations) -> None:
        for (route_id, category), observed in observations.items():
            if observed.trustworthy:
                self.store.commit(
                    route_id, category, items=observed.items,
                    source_fetch=observed.source_fetch,
                    destination_fetch=observed.destination_fetch,
                )
            elif observed.items:
                self.store.record_partial(
                    route_id, category, applied=observed.items, error=observed.error,
                )
            else:
                self.store.record_failure(route_id, category, observed.error)

    def test_a_clean_run_records_what_both_sides_held(self) -> None:
        self._apply(self._run([self._movie("1"), self._movie("2")], [self._movie("1")]))
        baseline = self.store.baseline("r1", "watchlist")
        self.assertTrue(baseline.allows_removals)
        self.assertEqual(len(baseline.items), 2)
        # Film 2 was written this run, so the agreement records it as present on
        # the destination — not as still missing, which would rewrite it forever.
        written = baseline.state(next(k for k in baseline.items if k.endswith(":2")))
        self.assertEqual(written.destination, STATE_PRESENT)
        self.assertEqual(written.action, "added")

    def test_a_failed_source_read_records_no_agreement_at_all(self) -> None:
        # The critical one: a timeout must not be written down as "the source
        # is empty", or the next run would delete everything on the target.
        self._apply(self._run([], [self._movie("1")], source_error="read timed out"))
        baseline = self.store.baseline("r1", "watchlist")
        self.assertFalse(baseline.allows_removals)
        self.assertEqual(baseline.items, {})
        self.assertIn("timed out", baseline.last_error)

    def test_a_failed_read_never_replaces_a_good_baseline(self) -> None:
        self._apply(self._run([self._movie("1")], [self._movie("1")]))
        good = self.store.baseline("r1", "watchlist")
        self._apply(self._run([], [self._movie("1")], source_error="502 upstream"))
        after = self.store.baseline("r1", "watchlist")
        self.assertEqual(after.sync_version, good.sync_version)
        self.assertEqual(set(after.items), set(good.items))

    def test_a_failed_write_does_not_establish_the_baseline(self) -> None:
        self._apply(self._run([self._movie("1")], [], write_error="503 from PMDB"))
        self.assertFalse(self.store.baseline("r1", "watchlist").allows_removals)

    def test_the_source_read_outcome_is_recorded(self) -> None:
        self._apply(self._run([self._movie("1")], [self._movie("1")]))
        outcome = self.store.baseline("r1", "watchlist").source_fetch
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, FetchStatus.SUCCESS_WITH_ITEMS)
        self.assertTrue(outcome.trustworthy_for_removals)

    def test_an_empty_but_successful_read_is_recorded_as_such(self) -> None:
        # Genuinely empty differs from failed, and the baseline says which.
        self._apply(self._run([], []))
        outcome = self.store.baseline("r1", "watchlist").source_fetch
        self.assertEqual(outcome.status, FetchStatus.SUCCESS_EMPTY)
        self.assertTrue(outcome.trustworthy_for_removals)

    def test_a_timeout_is_classified_as_a_timeout(self) -> None:
        observations = self._run([], [self._movie("1")], source_error="read timed out")
        observed = observations[("r1", "watchlist")]
        self.assertEqual(observed.source_fetch.status, FetchStatus.TIMEOUT)
        self.assertFalse(observed.trustworthy)

    def test_an_auth_failure_is_classified_as_unauthorized(self) -> None:
        observations = self._run([], [self._movie("1")], source_error="401 invalid_grant")
        self.assertEqual(
            observations[("r1", "watchlist")].source_fetch.status,
            FetchStatus.UNAUTHORIZED,
        )

    def test_a_second_identical_run_agrees_on_the_same_state(self) -> None:
        items = [self._movie("1"), self._movie("2")]
        self._apply(self._run(items, list(items)))
        first = dict(self.store.baseline("r1", "watchlist").items)
        self._apply(self._run(items, list(items)))
        second = self.store.baseline("r1", "watchlist")
        self.assertEqual(set(second.items), set(first))
        self.assertEqual(second.sync_version, 2)
