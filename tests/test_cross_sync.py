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
        self.fetched: list[tuple[str, list[str]]] = []
        self.target_lists_used: list[str] = []

    def can_write(self) -> bool:
        return self._writable

    def write_blocked_reason(self) -> str:
        return self._blocked_reason

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        self.fetched.append((category, list(source_lists or [])))
        if self._fetch_error:
            raise RuntimeError(self._fetch_error)
        return list(self._contents.get(category, []))

    def add(self, category: str, items: list[dict], target_list: str = "") -> dict:
        self.added.append((category, list(items)))
        self.target_lists_used.append(target_list)
        self._contents.setdefault(category, []).extend(items)
        return {"added": len(items), "not_found": 0}

    def remove(self, category: str, items: list[dict], target_list: str = "") -> dict:
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
    def test_enrichment_also_attaches_the_fribb_imdb_id(self, lookup_by_anilist) -> None:
        lookup_by_anilist.return_value = {
            "anilist_id": 164,
            "themoviedb_id": {"movie": [128]},
            "imdb_id": ["tt0119698"],
        }
        enriched = enrich_identity({"media_type": "movie", "anilist_id": "164"})
        self.assertEqual(enriched["tmdb_id"], "128")
        self.assertEqual(enriched["imdb_id"], "tt0119698")
        self.assertEqual(enriched["ids"]["imdb"], "tt0119698")

    @patch("src.fribb_client.lookup_by_anilist")
    def test_enrichment_keeps_an_existing_imdb_id(self, lookup_by_anilist) -> None:
        lookup_by_anilist.return_value = {
            "themoviedb_id": {"movie": [128]},
            "imdb_id": ["tt9999999"],
        }
        enriched = enrich_identity({"media_type": "movie", "anilist_id": "164", "imdb_id": "tt0119698"})
        self.assertEqual(enriched["imdb_id"], "tt0119698")

    def test_describe_error_translates_trakt_account_limit(self) -> None:
        class _Response:
            status_code = 420

        class _HttpError(Exception):
            response = _Response()

        message = CrossSyncService._describe_error(_HttpError("420 Client Error: <none> for url: x"))
        self.assertIn("account limit reached", message)
        self.assertIn("VIP", message)
        # Anything else passes through untouched.
        self.assertEqual(CrossSyncService._describe_error(ValueError("boom")), "boom")

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


class SourceListSelectionTests(unittest.TestCase):
    def test_selected_lists_are_passed_to_the_source(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {})
        service = CrossSyncService({"trakt": source, "simkl": target})

        service.run_pair(_pair(source_lists=["watchlist", "list:me/faves"]))

        self.assertEqual(source.fetched[0][1], ["watchlist", "list:me/faves"])

    def test_no_selection_means_the_provider_default(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {})
        service = CrossSyncService({"trakt": source, "simkl": target})

        service.run_pair(_pair())

        self.assertEqual(source.fetched[0][1], [])

    def test_the_target_is_read_without_a_source_list_filter(self) -> None:
        # source_lists narrows what is read *from the source*; the target's
        # current contents must always be read in full or the diff is wrong.
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1")]})
        service = CrossSyncService({"trakt": source, "simkl": target})

        service.run_pair(_pair(source_lists=["list:me/faves"]))

        self.assertEqual(target.fetched[0][1], [])


class TwoWayPairTests(unittest.TestCase):
    """Two-way keeps both sides holding the union.

    The dangerous part is telling "new on one side" from "deleted on the other".
    Both look identical in a single snapshot, so the pair's managed-key set is
    used as the last agreed state. These tests pin that distinction down.
    """

    def _two_way(self, **overrides):
        raw = {"mode": "two_way", "removal_mode": REMOVAL_MANAGED}
        raw.update(overrides)
        return _pair(**raw)

    def test_each_side_gains_what_the_other_had(self) -> None:
        a = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("2"), _movie("3")]})
        service = CrossSyncService({"trakt": a, "simkl": b})

        stats = service.run_pair(self._two_way())

        self.assertEqual([i["tmdb_id"] for i in b.added[0][1]], ["1"])
        self.assertEqual([i["tmdb_id"] for i in a.added[0][1]], ["3"])
        self.assertEqual(stats.added, 2)
        self.assertEqual(stats.categories[0].added_back, 1)
        self.assertEqual(stats.categories[0].skipped_existing, 1)

    def test_a_second_run_writes_nothing(self) -> None:
        a = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("2")]})
        service = CrossSyncService({"trakt": a, "simkl": b})

        first = service.run_pair(self._two_way())
        second = service.run_pair(self._two_way())

        self.assertEqual(first.added, 2)
        self.assertEqual(second.added, 0, "a settled two-way pair kept writing")
        self.assertEqual(second.removed, 0)

    def test_a_deletion_propagates_instead_of_being_re_added(self) -> None:
        # The case two independent one-way pairs cannot get right: after both
        # sides agreed on an item, removing it from one must remove it from the
        # other rather than being copied straight back.
        a = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        service = CrossSyncService({"trakt": a, "simkl": b})
        service.run_pair(self._two_way())          # agree on 1 and 2

        b.remove(CATEGORY_WATCHLIST, [_movie("2")])   # user deletes on SIMKL
        b.removed.clear()
        stats = service.run_pair(self._two_way())

        self.assertEqual(stats.removed, 1)
        self.assertEqual([i["tmdb_id"] for i in a.removed[-1][1]], ["2"])
        self.assertEqual(a.added, [], "the deleted item was copied back instead")

    def test_a_deletion_is_re_added_when_the_pair_is_additive(self) -> None:
        # Additive never deletes, so the item comes back from the other side.
        # That is the documented trade-off, not a bug — pin it so it stays a
        # deliberate choice.
        a = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1")]})
        service = CrossSyncService({"trakt": a, "simkl": b})
        service.run_pair(self._two_way(removal_mode=REMOVAL_ADDITIVE))

        b.remove(CATEGORY_WATCHLIST, [_movie("1")])
        stats = service.run_pair(self._two_way(removal_mode=REMOVAL_ADDITIVE))

        self.assertEqual(stats.removed, 0)
        self.assertEqual(stats.added, 1)

    def test_a_new_item_is_not_mistaken_for_a_deletion(self) -> None:
        a = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1")]})
        service = CrossSyncService({"trakt": a, "simkl": b})
        service.run_pair(self._two_way())

        a.add(CATEGORY_WATCHLIST, [_movie("9")])      # brand new, never synced
        a.added.clear()
        stats = service.run_pair(self._two_way())

        self.assertEqual(stats.removed, 0, "a new item was deleted as if it were stale")
        self.assertEqual([i["tmdb_id"] for i in b.added[-1][1]], ["9"])

    def test_direction_of_the_pair_does_not_change_the_outcome(self) -> None:
        # Naming the services the other way round must give the same result;
        # that is the whole reason this is one pass rather than two one-way runs.
        def build(swap):
            a = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
            b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
            svc = CrossSyncService({"trakt": a, "simkl": b})
            first = self._two_way() if not swap else self._two_way(source="simkl", target="trakt")
            svc.run_pair(first)
            a.remove(CATEGORY_WATCHLIST, [_movie("1")])   # deleted on Trakt
            a.removed.clear(); b.removed.clear()
            return svc.run_pair(first), a, b

        forward, fa, fb = build(False)
        reverse, ra, rb = build(True)

        self.assertEqual(forward.removed, 1)
        self.assertEqual(reverse.removed, 1)
        self.assertEqual([i["tmdb_id"] for i in fb.removed[-1][1]], ["1"])
        self.assertEqual([i["tmdb_id"] for i in rb.removed[-1][1]], ["1"])

    def test_mirror_is_downgraded_because_it_has_no_two_way_meaning(self) -> None:
        pair = SyncPair.from_dict({
            "source": "trakt", "target": "simkl", "categories": [CATEGORY_WATCHLIST],
            "mode": "two_way", "removal_mode": REMOVAL_MIRROR,
        })

        self.assertEqual(pair.removal_mode, REMOVAL_MANAGED)
        self.assertTrue(pair.is_two_way())

    def test_a_read_only_service_cannot_be_one_end(self) -> None:
        a = FakeAdapter("mdblist", {CATEGORY_WATCHLIST: [_movie("1")]}, writable=False,
                        blocked_reason="MDBList can only be read from.")
        b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"mdblist": a, "simkl": b})

        stats = service.run_pair(self._two_way(source="mdblist", target="simkl"))

        self.assertEqual(stats.added, 0)
        self.assertEqual(b.added, [])
        self.assertIn("MDBList can only be read from", " ".join(stats.errors))

    def test_a_failed_read_of_either_side_writes_nothing(self) -> None:
        a = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("2")]}, fetch_error="upstream 500")
        service = CrossSyncService({"trakt": a, "simkl": b})

        stats = service.run_pair(self._two_way())

        self.assertEqual(a.added, [])
        self.assertEqual(b.added, [])
        self.assertTrue(stats.categories[0].errors)

    def test_a_dry_run_reports_both_directions_and_writes_nothing(self) -> None:
        a = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        b = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("2")]})
        service = CrossSyncService({"trakt": a, "simkl": b}, dry_run=True)

        stats = service.run_pair(self._two_way())

        self.assertEqual(stats.added, 2)
        self.assertEqual(stats.categories[0].added_back, 1)
        self.assertEqual(a.added, [])
        self.assertEqual(b.added, [])
        self.assertEqual(service.managed_keys, {}, "a dry run recorded ownership")

    def test_display_name_shows_the_direction(self) -> None:
        one = SyncPair.from_dict({"source": "trakt", "target": "simkl",
                                  "categories": [CATEGORY_WATCHLIST]})
        two = SyncPair.from_dict({"source": "trakt", "target": "simkl",
                                  "categories": [CATEGORY_WATCHLIST], "mode": "two_way"})

        self.assertIn("→", one.display_name())
        self.assertIn("↔", two.display_name())


class PairIdTests(unittest.TestCase):
    """The id is echoed into HTML attributes and keys stored state, so it is
    restricted to an inert alphabet rather than trusted to every call site."""

    def test_quotes_and_markup_are_stripped_from_a_supplied_id(self) -> None:
        pair = SyncPair.from_dict({
            "pair_id": "a'\"><img src=x onerror=alert(1)>b",
            "source": "trakt",
            "target": "simkl",
            "categories": [CATEGORY_WATCHLIST],
        })

        self.assertNotIn("'", pair.pair_id)
        self.assertNotIn('"', pair.pair_id)
        self.assertNotIn("<", pair.pair_id)
        self.assertNotIn(">", pair.pair_id)
        self.assertTrue(pair.pair_id)

    def test_a_normal_id_is_left_alone(self) -> None:
        pair = SyncPair.from_dict({
            "pair_id": "trakt-simkl-1",
            "source": "trakt",
            "target": "simkl",
            "categories": [CATEGORY_WATCHLIST],
        })

        self.assertEqual(pair.pair_id, "trakt-simkl-1")

    def test_an_id_of_only_illegal_characters_becomes_empty_not_partial(self) -> None:
        # Empty is fine — callers assign a generated id when the field is blank.
        pair = SyncPair.from_dict({
            "pair_id": "<>\"'",
            "source": "trakt",
            "target": "simkl",
            "categories": [CATEGORY_WATCHLIST],
        })

        self.assertEqual(pair.pair_id, "")


class BatchReadCacheTests(unittest.TestCase):
    """One batch must not fetch the same provider data twice.

    The saving is only safe if a write is folded back into the cache, so the
    correctness cases matter more than the counting ones.
    """

    def test_a_shared_source_is_fetched_once_for_the_whole_batch(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        simkl = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        pmdb = FakeAdapter("pmdb", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": simkl, "pmdb": pmdb})

        service.run_pairs([
            _pair(pair_id="a", target="simkl"),
            _pair(pair_id="b", target="pmdb"),
        ])

        self.assertEqual(len(source.fetched), 1, "Trakt was read once per pair")
        self.assertEqual(service.last_run_cache_hits, 1)

    def test_a_shared_target_sees_what_an_earlier_pair_wrote(self) -> None:
        # The dangerous case: two sources feeding one target. Without the write
        # being folded back in, the second pair reads a stale target and re-adds
        # the item the first pair just wrote.
        trakt = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        pmdb = FakeAdapter("pmdb", {CATEGORY_WATCHLIST: [_movie("1")]})
        simkl = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": trakt, "pmdb": pmdb, "simkl": simkl})

        results = service.run_pairs([
            _pair(pair_id="a", source="trakt", target="simkl"),
            _pair(pair_id="b", source="pmdb", target="simkl"),
        ])

        self.assertEqual(results[0].added, 1)
        self.assertEqual(results[1].added, 0, "the second pair re-added an existing item")
        self.assertEqual(len(simkl.added), 1)

    def test_a_removal_is_folded_back_into_the_cached_target(self) -> None:
        trakt = FakeAdapter("trakt", {CATEGORY_WATCHLIST: []})
        pmdb = FakeAdapter("pmdb", {CATEGORY_WATCHLIST: []})
        simkl = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("9")]})
        service = CrossSyncService({"trakt": trakt, "pmdb": pmdb, "simkl": simkl})

        results = service.run_pairs([
            _pair(pair_id="a", source="trakt", target="simkl", removal_mode=REMOVAL_MIRROR),
            _pair(pair_id="b", source="pmdb", target="simkl", removal_mode=REMOVAL_MIRROR),
        ])

        self.assertEqual(results[0].removed, 1)
        self.assertEqual(results[1].removed, 0, "the second pair tried to delete it again")
        self.assertEqual(len(simkl.removed), 1)

    def test_a_failed_write_drops_the_cached_target(self) -> None:
        # A write that raised may have partly landed, so the next pair has to go
        # back to the network rather than trust a cached view.
        class ExplodingTarget(FakeAdapter):
            def add(self, category, items, target_list=""):
                raise RuntimeError("upstream 500")

        trakt = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        pmdb = FakeAdapter("pmdb", {CATEGORY_WATCHLIST: [_movie("2")]})
        simkl = ExplodingTarget("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": trakt, "pmdb": pmdb, "simkl": simkl})

        service.run_pairs([
            _pair(pair_id="a", source="trakt", target="simkl"),
            _pair(pair_id="b", source="pmdb", target="simkl"),
        ])

        self.assertEqual(len(simkl.fetched), 2, "the target was not re-read after a failed write")

    def test_separate_runs_do_not_share_a_cache(self) -> None:
        # State can change between runs, so a cache must never outlive its batch.
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": target})

        service.run_pairs([_pair()])
        service.run_pairs([_pair()])

        self.assertEqual(len(source.fetched), 2)

    def test_different_source_lists_are_cached_separately(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": target})

        service.run_pairs([
            _pair(pair_id="a", source_lists=["watchlist"]),
            _pair(pair_id="b", source_lists=["list:me/faves"]),
        ])

        self.assertEqual(
            [lists for _cat, lists in source.fetched],
            [["watchlist"], ["list:me/faves"]],
        )

    def test_cached_reads_are_reported_on_the_result(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        simkl = FakeAdapter("simkl", {CATEGORY_WATCHLIST: []})
        pmdb = FakeAdapter("pmdb", {CATEGORY_WATCHLIST: []})
        service = CrossSyncService({"trakt": source, "simkl": simkl, "pmdb": pmdb})

        results = service.run_pairs([
            _pair(pair_id="a", target="simkl"),
            _pair(pair_id="b", target="pmdb"),
        ])

        self.assertEqual(results[0].cached_reads, 0)
        self.assertEqual(results[1].cached_reads, 1)
        self.assertEqual(results[1].to_dict()["cached_reads"], 1)


class TargetListTests(unittest.TestCase):
    def test_target_list_is_passed_to_the_target(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("pmdb", {})
        service = CrossSyncService({"trakt": source, "pmdb": target})

        service.run_pair(_pair(target="pmdb", target_list="list:42"))

        self.assertEqual(target.target_lists_used, ["list:42"])

    def test_no_target_list_means_the_providers_default(self) -> None:
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1")]})
        target = FakeAdapter("simkl", {})
        service = CrossSyncService({"trakt": source, "simkl": target})

        service.run_pair(_pair())

        self.assertEqual(target.target_lists_used, [""])

    def test_only_providers_that_support_it_advertise_target_lists(self) -> None:
        from src.providers import (
            AniListAdapter, MdbListAdapter, PmdbAdapter, SimklAdapter, TraktAdapter,
        )
        # Writing into a named list is a real capability, not a UI nicety: SIMKL
        # and AniList have no writable custom lists and MDBList has no writes at
        # all, so offering a destination list for them would be a lie.
        self.assertTrue(TraktAdapter.supports_target_lists)
        self.assertTrue(PmdbAdapter.supports_target_lists)
        self.assertFalse(SimklAdapter.supports_target_lists)
        self.assertFalse(AniListAdapter.supports_target_lists)
        self.assertFalse(MdbListAdapter.supports_target_lists)

    def test_only_providers_with_a_public_search_advertise_one(self) -> None:
        from src.providers import (
            AniListAdapter, MdbListAdapter, PmdbAdapter, SimklAdapter, TraktAdapter,
        )
        # Offering a search box where search_lists can only return an empty list
        # reads as a broken search rather than an absent feature.
        self.assertTrue(TraktAdapter.supports_list_search)
        self.assertTrue(MdbListAdapter.supports_list_search)
        self.assertFalse(SimklAdapter.supports_list_search)
        self.assertFalse(AniListAdapter.supports_list_search)
        self.assertFalse(PmdbAdapter.supports_list_search)

    def test_unwritable_provider_reports_no_target_lists(self) -> None:
        from src.providers import AniListAdapter

        class _Client:
            def can_write(self): return False
            def write_blocked_reason(self): return "needs a token"

        self.assertEqual(AniListAdapter(_Client()).safe_target_lists(), [])


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
