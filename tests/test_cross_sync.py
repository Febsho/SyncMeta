"""Tests for one-way cross-service sync pairs.

The dangerous behaviours are removal (irreversible) and cross-provider identity
(if keys from two services don't line up, a pair silently re-adds its whole
source list on every run).  Both are covered here.
"""

import unittest
from unittest.mock import patch

from src.config import SyncPair
from src.cross_sync import CrossSyncService
from src.providers import (
    CATEGORY_COLLECTION,
    CATEGORY_HISTORY,
    CATEGORY_WATCHLIST,
    REMOVAL_ADDITIVE,
    REMOVAL_MANAGED,
    REMOVAL_MIRROR,
    ProviderAdapter,
    enrich_identity,
    has_portable_identity,
    item_key,
)


class FakeAdapter(ProviderAdapter):
    """In-memory adapter recording everything written to it."""

    def __init__(
        self,
        key: str,
        contents: dict | None = None,
        *,
        reads=(CATEGORY_WATCHLIST,),
        writes=(CATEGORY_WATCHLIST,),
        writable: bool = True,
        blocked_reason: str = "no token",
        fetch_error: str = "",
    ):
        self.key = key
        self.label = key.title()
        self.reads = tuple(reads)
        self.writes = tuple(writes)
        self._contents = {k: list(v) for k, v in (contents or {}).items()}
        self._writable = writable
        self._blocked_reason = blocked_reason
        self._fetch_error = fetch_error
        self.added: list[tuple[str, list[dict]]] = []
        self.removed: list[tuple[str, list[dict]]] = []

    def can_write(self) -> bool:
        return self._writable

    def write_blocked_reason(self) -> str:
        return self._blocked_reason

    def fetch(self, category: str) -> list[dict]:
        if self._fetch_error:
            raise RuntimeError(self._fetch_error)
        return list(self._contents.get(category, []))

    def add(self, category: str, items: list[dict]) -> dict:
        self.added.append((category, list(items)))
        self._contents.setdefault(category, []).extend(items)
        return {"added": len(items), "not_found": 0}

    def remove(self, category: str, items: list[dict]) -> dict:
        self.removed.append((category, list(items)))
        keys = {item_key(i) for i in items}
        self._contents[category] = [
            i for i in self._contents.get(category, []) if item_key(i) not in keys
        ]
        return {"deleted": len(items), "not_found": 0}


def _movie(tmdb_id: str, title: str = "Film") -> dict:
    return {"title": title, "media_type": "movie", "tmdb_id": tmdb_id, "ids": {"tmdb": tmdb_id}}


def _pair(**overrides) -> SyncPair:
    raw = {
        "pair_id": "p1",
        "name": "Test pair",
        "source": "trakt",
        "target": "simkl",
        "categories": [CATEGORY_WATCHLIST],
        "removal_mode": REMOVAL_ADDITIVE,
    }
    raw.update(overrides)
    return SyncPair.from_dict(raw)


class PairAddTests(unittest.TestCase):
    def test_only_missing_items_are_written(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1")]})
        service = CrossSyncService({"trakt": source, "simkl": target})

        stats = service.run_pair(_pair())

        self.assertEqual(stats.added, 1)
        self.assertEqual([i["tmdb_id"] for i in target.added[0][1]], ["2"])
        self.assertEqual(stats.categories[0].skipped_existing, 1)

    def test_a_second_run_writes_nothing(self) -> None:
        # The regression that matters: identity must line up run to run.
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": target})

        first = service.run_pair(_pair())
        second = service.run_pair(_pair())

        self.assertEqual(first.added, 2)
        self.assertEqual(second.added, 0, "a pair must not re-add what it already wrote")

    def test_items_without_a_portable_id_are_counted_not_written(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [
            {"title": "Mystery", "media_type": "movie"},
        ]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": target})

        stats = service.run_pair(_pair())

        self.assertEqual(stats.added, 0)
        self.assertEqual(stats.unmapped, 1)
        self.assertEqual(target.added, [])

    def test_dry_run_reports_without_writing(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": target}, dry_run=True)

        stats = service.run_pair(_pair())

        self.assertEqual(stats.added, 1)
        self.assertEqual(target.added, [], "dry run must not write")
        self.assertEqual(service.managed_keys, {}, "dry run must not record ownership")

    def test_multiple_categories_run_independently(self) -> None:
        source = FakeAdapter(
            "trakt",
            {CATEGORY_WATCHLIST: [_movie("1")], CATEGORY_COLLECTION: [_movie("2")]},
            reads=(CATEGORY_WATCHLIST, CATEGORY_COLLECTION),
            writes=(CATEGORY_WATCHLIST, CATEGORY_COLLECTION),
        )
        target = FakeAdapter(
            "simkl", {},
            reads=(CATEGORY_WATCHLIST, CATEGORY_COLLECTION),
            writes=(CATEGORY_WATCHLIST, CATEGORY_COLLECTION),
        )
        service = CrossSyncService({"trakt": source, "simkl": target})

        stats = service.run_pair(_pair(categories=[CATEGORY_WATCHLIST, CATEGORY_COLLECTION]))

        self.assertEqual(len(stats.categories), 2)
        self.assertEqual(stats.added, 2)


class RemovalModeTests(unittest.TestCase):
    def _setup(self, mode: str, managed=None):
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1"), _movie("99")]})
        service = CrossSyncService(
            {"trakt": source, "simkl": target}, managed_keys=managed or {},
        )
        return source, target, service, _pair(removal_mode=mode)

    def test_additive_never_removes(self) -> None:
        _source, target, service, pair = self._setup(REMOVAL_ADDITIVE)
        stats = service.run_pair(pair)
        self.assertEqual(stats.removed, 0)
        self.assertEqual(target.removed, [])

    def test_mirror_removes_anything_missing_from_the_source(self) -> None:
        _source, target, service, pair = self._setup(REMOVAL_MIRROR)
        stats = service.run_pair(pair)
        self.assertEqual(stats.removed, 1)
        self.assertEqual([i["tmdb_id"] for i in target.removed[0][1]], ["99"])

    def test_managed_mode_leaves_items_this_pair_never_added(self) -> None:
        # 99 was added on the target by hand, so a managed pair must not touch it.
        _source, target, service, pair = self._setup(REMOVAL_MANAGED)
        stats = service.run_pair(pair)
        self.assertEqual(stats.removed, 0)
        self.assertEqual(target.removed, [])

    def test_managed_mode_removes_what_this_pair_previously_added(self) -> None:
        managed = {"p1": {CATEGORY_WATCHLIST: ["movie:tmdb:1", "movie:tmdb:99"]}}
        _source, target, service, pair = self._setup(REMOVAL_MANAGED, managed)
        stats = service.run_pair(pair)
        self.assertEqual(stats.removed, 1)
        self.assertEqual([i["tmdb_id"] for i in target.removed[0][1]], ["99"])

    def test_managed_ownership_is_recorded_for_the_next_run(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": target})

        service.run_pair(_pair(removal_mode=REMOVAL_MANAGED))

        self.assertEqual(
            service.managed_keys["p1"][CATEGORY_WATCHLIST],
            ["movie:tmdb:1", "movie:tmdb:2"],
        )

    def test_managed_keys_are_scoped_per_pair(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1"), _movie("42")]})
        # Another pair owns 42; this pair must not remove it.
        managed = {"other": {CATEGORY_WATCHLIST: ["movie:tmdb:42"]}}
        service = CrossSyncService({"trakt": source, "simkl": target}, managed_keys=managed)

        stats = service.run_pair(_pair(removal_mode=REMOVAL_MANAGED))

        self.assertEqual(stats.removed, 0)

    def test_unknown_removal_mode_refuses_to_delete(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1"), _movie("99")]})
        service = CrossSyncService({"trakt": source, "simkl": target})
        pair = _pair()
        pair.removal_mode = "something-new"  # bypass validation deliberately

        stats = service.run_pair(pair)

        self.assertEqual(stats.removed, 0)
        self.assertEqual(target.removed, [])


class PairValidationTests(unittest.TestCase):
    def test_unwritable_target_is_reported_with_its_reason(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter(
            "anilist", {}, writable=False,
            blocked_reason="AniList needs an access token to be written to.",
        )
        service = CrossSyncService({"trakt": source, "anilist": target})

        stats = service.run_pair(_pair(target="anilist"))

        self.assertIn("access token", stats.errors[0])
        self.assertEqual(target.added, [])

    def test_missing_provider_is_reported(self) -> None:
        service = CrossSyncService({"trakt": FakeAdapter("trakt")})
        stats = service.run_pair(_pair(target="simkl"))
        self.assertIn("not configured", stats.errors[0])

    def test_category_unsupported_by_either_end_is_reported(self) -> None:
        # AniList has no honest notion of per-episode watch history.
        source = FakeAdapter(
            "trakt", {CATEGORY_HISTORY: []},
            reads=(CATEGORY_HISTORY,), writes=(CATEGORY_HISTORY,),
        )
        target = FakeAdapter(
            "anilist", {}, reads=(CATEGORY_WATCHLIST,), writes=(CATEGORY_WATCHLIST,),
        )
        service = CrossSyncService({"trakt": source, "anilist": target})

        stats = service.run_pair(_pair(target="anilist", categories=[CATEGORY_HISTORY]))

        self.assertTrue(stats.errors)
        self.assertIn("cannot receive", stats.errors[0])

    def test_a_supported_category_still_runs_beside_an_unsupported_one(self) -> None:
        source = FakeAdapter(
            "trakt",
            {CATEGORY_WATCHLIST: [_movie("1")], CATEGORY_HISTORY: [_movie("2")]},
            reads=(CATEGORY_WATCHLIST, CATEGORY_HISTORY),
            writes=(CATEGORY_WATCHLIST, CATEGORY_HISTORY),
        )
        target = FakeAdapter("anilist", {}, reads=(CATEGORY_WATCHLIST,), writes=(CATEGORY_WATCHLIST,))
        service = CrossSyncService({"trakt": source, "anilist": target})

        stats = service.run_pair(
            _pair(target="anilist", categories=[CATEGORY_WATCHLIST, CATEGORY_HISTORY]),
        )

        self.assertEqual(stats.added, 1)
        self.assertEqual([c.category for c in stats.categories], [CATEGORY_WATCHLIST])

    def test_disabled_pairs_are_skipped(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {})
        service = CrossSyncService({"trakt": source, "simkl": target})

        results = service.run_pairs([_pair(enabled=False)])

        self.assertEqual(results, [])
        self.assertEqual(target.added, [])


class FetchFailureTests(unittest.TestCase):
    def test_source_failure_writes_nothing(self) -> None:
        source = FakeAdapter("trakt", fetch_error="upstream 500")
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": target})

        stats = service.run_pair(_pair())

        self.assertIn("Could not read", stats.categories[0].errors[0])
        self.assertEqual(target.added, [])

    def test_target_read_failure_aborts_the_category(self) -> None:
        # Without the target's contents everything looks new, so a pair must not
        # proceed to write — it would duplicate the entire source list.
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", fetch_error="upstream 500")
        service = CrossSyncService({"trakt": source, "simkl": target})

        stats = service.run_pair(_pair())

        self.assertIn("Could not read", stats.categories[0].errors[0])
        self.assertEqual(target.added, [])


class IdentityTests(unittest.TestCase):
    def test_episode_keys_include_season_and_episode(self) -> None:
        key = item_key({"media_type": "tv", "tmdb_id": "1429", "season": 1, "episode": 3})
        self.assertEqual(key, "tv:tmdb:1429:s1e3")

    def test_title_only_items_are_not_portable(self) -> None:
        self.assertFalse(has_portable_identity({"media_type": "movie", "title": "X"}))
        self.assertTrue(has_portable_identity({"media_type": "movie", "tmdb_id": "1"}))

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anilist_items_gain_a_tmdb_id_so_keys_line_up(self, lookup_by_anilist) -> None:
        lookup_by_anilist.return_value = {"anilist_id": 164, "themoviedb_id": {"movie": [128]}}
        enriched = enrich_identity({"media_type": "movie", "anilist_id": "164"})
        self.assertEqual(enriched["tmdb_id"], "128")
        self.assertEqual(item_key(enriched), "movie:tmdb:128")

    @patch("src.fribb_client.lookup_by_anilist")
    def test_enrichment_never_overwrites_a_supplied_tmdb_id(self, lookup_by_anilist) -> None:
        lookup_by_anilist.return_value = {"themoviedb_id": {"movie": [999]}}
        enriched = enrich_identity({"media_type": "movie", "tmdb_id": "128", "anilist_id": "164"})
        self.assertEqual(enriched["tmdb_id"], "128")
        lookup_by_anilist.assert_not_called()

    @patch("src.fribb_client.lookup_by_anilist")
    def test_enrichment_refuses_a_cross_namespace_mapping(self, lookup_by_anilist) -> None:
        # A tv-namespace mapping must not be adopted by a movie item.
        lookup_by_anilist.return_value = {"themoviedb_id": {"tv": 12634}}
        enriched = enrich_identity({"media_type": "movie", "anilist_id": "999"})
        self.assertNotIn("tmdb_id", enriched)

    @patch("src.fribb_client.lookup_by_anilist")
    def test_an_anilist_target_matches_a_tmdb_source(self, lookup_by_anilist) -> None:
        # End to end: Trakt (TMDB) -> AniList (AniList ids). The item is already
        # on AniList, so nothing should be written.
        lookup_by_anilist.return_value = {"anilist_id": 164, "themoviedb_id": {"movie": [128]}}
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("128")]})
        target = FakeAdapter("anilist", {CATEGORY_WATCHLIST: [
            {"title": "Mononoke Hime", "media_type": "movie", "anilist_id": "164"},
        ]})
        service = CrossSyncService({"trakt": source, "anilist": target})

        stats = service.run_pair(_pair(target="anilist"))

        self.assertEqual(stats.added, 0, "item already present on the target")
        self.assertEqual(target.added, [])


class MdbListSourceTests(unittest.TestCase):
    """MDBList feeds other services but can never receive."""

    class FakeMdbClient:
        def __init__(self, items_by_list=None, failing=()):
            self._items = items_by_list or {}
            self._failing = set(failing)
            self.calls: list[int] = []

        def get_list_items(self, list_id: int):
            self.calls.append(list_id)
            if list_id in self._failing:
                raise RuntimeError("mdblist 500")
            return list(self._items.get(list_id, []))

    def _adapter(self, **kwargs):
        from src.providers import MdbListAdapter
        return MdbListAdapter(**kwargs)

    def test_never_writable_and_says_why(self) -> None:
        adapter = self._adapter(client=self.FakeMdbClient(), selected_lists=[])
        self.assertFalse(adapter.can_write())
        self.assertEqual(adapter.writable_categories(), ())
        self.assertIn("only be read from", adapter.write_blocked_reason())

    def test_writing_is_refused_outright(self) -> None:
        adapter = self._adapter(client=self.FakeMdbClient(), selected_lists=[])
        with self.assertRaises(ValueError):
            adapter.add(CATEGORY_WATCHLIST, [_movie("1")])
        with self.assertRaises(ValueError):
            adapter.remove(CATEGORY_WATCHLIST, [_movie("1")])

    def test_selected_lists_are_combined_and_deduplicated(self) -> None:
        client = self.FakeMdbClient({
            10: [_movie("1"), _movie("2")],
            11: [_movie("2"), _movie("3")],
        })
        adapter = self._adapter(
            client=client, selected_lists=[{"id": 10, "name": "A"}, {"id": 11, "name": "B"}],
        )
        items = adapter.fetch(CATEGORY_WATCHLIST)
        self.assertEqual(sorted(i["tmdb_id"] for i in items), ["1", "2", "3"])

    def test_items_are_fetched_once_and_reused_across_categories(self) -> None:
        client = self.FakeMdbClient({10: [_movie("1")]})
        adapter = self._adapter(client=client, selected_lists=[{"id": 10, "name": "A"}])
        adapter.fetch(CATEGORY_WATCHLIST)
        adapter.fetch(CATEGORY_COLLECTION)
        self.assertEqual(client.calls, [10], "the same items answer both categories")

    def test_one_failing_list_does_not_lose_the_others(self) -> None:
        client = self.FakeMdbClient({10: [_movie("1")], 11: [_movie("2")]}, failing=(10,))
        adapter = self._adapter(
            client=client, selected_lists=[{"id": 10, "name": "A"}, {"id": 11, "name": "B"}],
        )
        items = adapter.fetch(CATEGORY_WATCHLIST)
        self.assertEqual([i["tmdb_id"] for i in items], ["2"])

    def test_mdblist_can_feed_another_service(self) -> None:
        client = self.FakeMdbClient({10: [_movie("1"), _movie("2")]})
        source = self._adapter(client=client, selected_lists=[{"id": 10, "name": "A"}])
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1")]})
        service = CrossSyncService({"mdblist": source, "simkl": target})

        stats = service.run_pair(_pair(source="mdblist", target="simkl"))

        self.assertEqual(stats.added, 1)
        self.assertEqual([i["tmdb_id"] for i in target.added[0][1]], ["2"])

    def test_mdblist_as_a_target_is_rejected_before_anything_runs(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = self._adapter(client=self.FakeMdbClient(), selected_lists=[])
        service = CrossSyncService({"trakt": source, "mdblist": target})

        stats = service.run_pair(_pair(target="mdblist"))

        self.assertIn("only be read from", stats.errors[0])
        self.assertEqual(stats.added, 0)


if __name__ == "__main__":
    unittest.main()
