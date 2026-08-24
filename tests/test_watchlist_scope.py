"""The PublicMetaDB watchlist is a plan-to-watch list, and only that.

Several unrelated things map onto ``CATEGORY_WATCHLIST`` because it is the only
category that fits them — a curated MDBList list, a Trakt personal list, PMDB's
own Picks — but none of them means "plan to watch". Treating the category as if
it did is what filled a real user's PublicMetaDB watchlist with 4,560 entries
that mirrored their collection almost exactly.

These tests pin both halves of the fix: every adapter says whether the list an
item came from actually means plan-to-watch, and PublicMetaDB refuses anything
that positively is not.
"""

import tempfile
import unittest
from pathlib import Path

from src.config import SyncPair
from src.cross_sync import CrossSyncService
from src.library_store import LibraryStore
from src.providers import (
    CATEGORY_COLLECTION,
    CATEGORY_WATCHLIST,
    PLANNED_FLAG,
    MdbListAdapter,
    PmdbAdapter,
    ProviderAdapter,
    SimklAdapter,
    TraktAdapter,
    VISIBILITY_PRIVATE,
    is_planned,
    item_key,
)

from tests.test_pmdb_picks import FakePmdbClient, _watchlist


def _movie(tmdb_id: str = "496243", **extra) -> dict:
    return {
        "title": "Parasite", "media_type": "movie", "tmdb_id": tmdb_id,
        "ids": {"tmdb": tmdb_id}, **extra,
    }


class PlannedFlagTests(unittest.TestCase):
    def test_an_unflagged_item_is_unknown_not_refused(self) -> None:
        # Items stored before the flag existed must not be silently dropped.
        self.assertIsNone(is_planned(_movie()))

    def test_the_flag_is_read_back_exactly(self) -> None:
        self.assertIs(is_planned(_movie(**{PLANNED_FLAG: True})), True)
        self.assertIs(is_planned(_movie(**{PLANNED_FLAG: False})), False)


class SourceMarksPlannedTests(unittest.TestCase):
    """Each provider knows which of its lists means plan-to-watch."""

    def test_trakt_watchlist_is_planned_and_a_personal_list_is_not(self) -> None:
        class FakeTrakt:
            def get_watchlist(self):
                return [_movie("1")]

            def get_list_items(self, user, slug):
                return [_movie("2")]

            def get_personal_lists_metadata(self):
                return []

            def get_liked_lists_metadata(self):
                return []

        adapter = TraktAdapter(FakeTrakt())
        self.assertIs(is_planned(adapter.fetch(CATEGORY_WATCHLIST)[0]), True)
        picked = adapter.fetch(CATEGORY_WATCHLIST, ["list:me/faves"])
        self.assertIs(is_planned(picked[0]), False)

    def test_simkl_plantowatch_is_planned_and_completed_is_not(self) -> None:
        class FakeSimkl:
            def get_status(self, status, media_types):
                return {media_types[0]: [_movie("1")]}

        adapter = SimklAdapter(FakeSimkl(), media_types=["movies"])
        planned = adapter.fetch(CATEGORY_WATCHLIST, ["status:plantowatch:movies"])
        self.assertIs(is_planned(planned[0]), True)
        completed = adapter.fetch(CATEGORY_COLLECTION, ["status:completed:movies"])
        self.assertIs(is_planned(completed[0]), False)

    def test_an_mdblist_curated_list_is_not_planned(self) -> None:
        # A curated list answers both watchlist and collection because it has no
        # watched/unwatched semantics — which is exactly why it cannot mean
        # "plan to watch" either.
        class FakeMdb:
            def get_sync_items(self, category):
                return []

            def get_list_items(self, list_id):
                return [_movie("2")]

            def get_lists(self):
                return [{"id": "42", "name": "Curated"}]

        adapter = MdbListAdapter(FakeMdb(), [{"id": "42", "name": "Curated"}])
        self.assertIs(is_planned(adapter.fetch(CATEGORY_WATCHLIST, ["list:42"])[0]), False)

    def test_mdblists_own_watchlist_is_planned(self) -> None:
        class FakeMdb:
            def get_sync_items(self, category):
                return [_movie("1")] if category == CATEGORY_WATCHLIST else []

            def get_list_items(self, list_id):
                return []

            def get_lists(self):
                return []

        adapter = MdbListAdapter(FakeMdb(), [])
        self.assertIs(is_planned(adapter.fetch(CATEGORY_WATCHLIST, ["watchlist"])[0]), True)

    def test_pmdb_picks_is_not_planned(self) -> None:
        client = FakePmdbClient([
            _watchlist(),
            {"id": "picks-1", "name": "Picks", "type": "picks", "list_type": "picks"},
        ])
        client.items_by_list["wl-1"] = [{"tmdb_id": "1", "media_type": "movie", "id": "a"}]
        client.items_by_list["picks-1"] = [{"tmdb_id": "2", "media_type": "movie", "id": "b"}]
        adapter = PmdbAdapter(client)
        by_id = {i["tmdb_id"]: i for i in adapter.fetch(CATEGORY_WATCHLIST, ["watchlist", "picks"])}
        self.assertIs(is_planned(by_id["1"]), True)
        self.assertIs(is_planned(by_id["2"]), False)


class PmdbWatchlistAcceptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakePmdbClient([_watchlist()])
        self.adapter = PmdbAdapter(self.client)

    def test_a_non_planned_item_is_refused_by_the_native_watchlist(self) -> None:
        self.assertFalse(
            self.adapter.accepts(CATEGORY_WATCHLIST, _movie(**{PLANNED_FLAG: False}))
        )

    def test_a_planned_item_is_accepted(self) -> None:
        self.assertTrue(
            self.adapter.accepts(CATEGORY_WATCHLIST, _movie(**{PLANNED_FLAG: True}))
        )

    def test_an_unknown_item_is_accepted(self) -> None:
        self.assertTrue(self.adapter.accepts(CATEGORY_WATCHLIST, _movie()))

    def test_a_named_destination_list_takes_anything(self) -> None:
        # There the user picked the list, so the plan-to-watch rule does not apply.
        self.assertTrue(
            self.adapter.accepts(
                CATEGORY_WATCHLIST, _movie(**{PLANNED_FLAG: False}), "list:99",
            )
        )
        self.assertTrue(
            self.adapter.accepts(
                CATEGORY_WATCHLIST, _movie(**{PLANNED_FLAG: False}), "picks",
            )
        )

    def test_the_write_itself_also_refuses_it(self) -> None:
        # Defence in depth: the diff filters first, but a direct adapter call
        # must not be able to put a non-planned item in the watchlist either.
        totals = self.adapter.add(
            CATEGORY_WATCHLIST, [_movie(**{PLANNED_FLAG: False})], "", VISIBILITY_PRIVATE,
        )
        self.assertEqual(totals["added"], 0)
        self.assertEqual(totals["not_found"], 1)
        self.assertEqual(self.client.batched, [])

    def test_a_planned_item_still_reaches_the_watchlist(self) -> None:
        totals = self.adapter.add(
            CATEGORY_WATCHLIST, [_movie(**{PLANNED_FLAG: True})], "", VISIBILITY_PRIVATE,
        )
        self.assertEqual(totals["added"], 1)
        self.assertEqual(self.client.batched[0][0], "wl-1")


class LibraryCarriesPlannedTests(unittest.TestCase):
    """The Library is the hub, so it must not launder a curated list.

    It used to record any watchlist-section item as "Planning" whether or not
    the source said so, which is how MDBList lists arrived at PublicMetaDB
    looking like plan-to-watch.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = LibraryStore(Path(self._dir.name) / "library.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _add(self, planned) -> dict:
        item = _movie(_syncmeta_source_provider="mdblist")
        if planned is not None:
            item[PLANNED_FLAG] = planned
        self.store.add("watchlist", [item], source="pair")
        return self.store.entries()[0]

    def test_a_curated_item_is_not_recorded_as_planning(self) -> None:
        entry = self._add(False)
        self.assertIs(entry["planned"], False)
        self.assertEqual(entry["provider_states"]["mdblist"]["status"], "Synced")

    def test_a_planned_item_is_recorded_as_planning(self) -> None:
        entry = self._add(True)
        self.assertIs(entry["planned"], True)
        self.assertEqual(entry["provider_states"]["mdblist"]["status"], "Planning")

    def test_the_flag_survives_a_read(self) -> None:
        self._add(False)
        self.assertIs(is_planned(self.store.fetch("watchlist")[0]), False)

    def test_a_legacy_entry_stays_unknown(self) -> None:
        self._add(None)
        self.assertIsNone(is_planned(self.store.fetch("watchlist")[0]))

    def test_one_planned_source_wins_over_a_curated_one(self) -> None:
        # The same title can sit in a curated list *and* be genuinely planned
        # on another service. Being planned anywhere makes it planned.
        self._add(False)
        self._add(True)
        self.assertIs(self.store.entries()[0]["planned"], True)

    def test_leaving_the_watchlist_clears_the_claim(self) -> None:
        self._add(True)
        self.store.remove("watchlist", [_movie()])
        self.store.add("collection", [_movie()], source="pair")
        self.assertIsNone(self.store.entries()[0].get("planned"))


class _Fake(ProviderAdapter):
    def __init__(self, key, contents=None, reads=(CATEGORY_WATCHLIST,), writes=()):
        self.key = key
        self.label = key.title()
        self.reads = tuple(reads)
        self.writes = tuple(writes)
        self._contents = {k: list(v) for k, v in (contents or {}).items()}
        self.added: list[list[dict]] = []
        self.removed: list[list[dict]] = []

    def fetch(self, category, source_lists=None):
        return list(self._contents.get(category, []))

    def add(self, category, items, target_list="", **kwargs):
        self.added.append(list(items))
        self._contents.setdefault(category, []).extend(items)
        return {"added": len(items)}

    def remove(self, category, items, target_list=""):
        self.removed.append(list(items))
        keys = {item_key(i) for i in items}
        self._contents[category] = [
            i for i in self._contents.get(category, []) if item_key(i) not in keys
        ]
        return {"deleted": len(items)}


class _FakePmdbTarget(_Fake):
    accepts = PmdbAdapter.accepts


class PairWatchlistScopeTests(unittest.TestCase):
    """The refusal has to shape the diff, not just the write.

    An item the target will not store must leave the source set too. Left in, it
    looks present, so a stale copy already sitting on the target could never be
    recognised as stale — and the entries the user wants gone could never be
    removed.
    """

    def _run(self, removal_mode: str, managed_keys=None):
        source = _Fake("library", {CATEGORY_WATCHLIST: [
            _movie("1", **{PLANNED_FLAG: True}),
            _movie("2", **{PLANNED_FLAG: False}),
        ]})
        target = _FakePmdbTarget(
            "pmdb",
            {CATEGORY_WATCHLIST: [
                _movie("1", **{PLANNED_FLAG: True}),
                _movie("2", **{PLANNED_FLAG: False}),
            ]},
            reads=(CATEGORY_WATCHLIST,), writes=(CATEGORY_WATCHLIST,),
        )
        pair = SyncPair.from_dict({
            "pair_id": "p1", "source": "library", "target": "pmdb",
            "categories": [CATEGORY_WATCHLIST], "removal_mode": removal_mode,
        })
        service = CrossSyncService(
            {"library": source, "pmdb": target}, managed_keys=managed_keys,
        )
        return service.run_pair(pair), source, target

    def test_a_refused_item_is_counted_not_written(self) -> None:
        result, _source, target = self._run("additive")
        self.assertEqual(result.categories[0].skipped_unsupported, 1)
        self.assertEqual(result.added, 0)
        self.assertEqual(target.added, [])

    def test_additive_still_never_deletes(self) -> None:
        result, _source, target = self._run("additive")
        self.assertEqual(result.removed, 0)
        self.assertEqual(target.removed, [])

    def test_managed_removal_prunes_what_the_watchlist_should_not_hold(self) -> None:
        managed = {"p1": {CATEGORY_WATCHLIST: [
            item_key(_movie("1")), item_key(_movie("2")),
        ]}}
        result, _source, target = self._run("managed", managed)
        self.assertEqual(result.removed, 1)
        self.assertEqual([i["tmdb_id"] for b in target.removed for i in b], ["2"])
        self.assertEqual(
            [i["tmdb_id"] for i in target._contents[CATEGORY_WATCHLIST]], ["1"],
        )

    def test_a_genuinely_planned_item_is_never_touched(self) -> None:
        managed = {"p1": {CATEGORY_WATCHLIST: [item_key(_movie("1"))]}}
        _result, _source, target = self._run("managed", managed)
        self.assertIn("1", [i["tmdb_id"] for i in target._contents[CATEGORY_WATCHLIST]])
