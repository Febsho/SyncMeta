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


class AniListHistoryPairTests(unittest.TestCase):
    """AniList is a history *source* only.

    Its rows are derived from progress counts rather than observed as plays, so
    they may leave AniList but must never be written back into it: AniList
    stores one absolute progress number per cour, and a wrong answer rewrites
    the user's real progress instead of adding a stray row.
    """

    @staticmethod
    def _episode(tmdb_id: str, season: int, episode: int) -> dict:
        return {
            "tmdb_id": tmdb_id,
            "media_type": "tv",
            "season": season,
            "episode": episode,
            "watched_at": "2025-05-05T00:00:00Z",
            "cursor_exempt": True,
            "anilist_derived": True,
        }

    def test_derived_history_flows_out_of_anilist(self) -> None:
        source = FakeAdapter(
            "anilist",
            {CATEGORY_HISTORY: [self._episode("910", 1, 1), self._episode("910", 1, 2)]},
            reads=(CATEGORY_HISTORY,), writes=(),
        )
        target = FakeAdapter(
            "library", {CATEGORY_HISTORY: []},
            reads=(CATEGORY_HISTORY,), writes=(CATEGORY_HISTORY,),
        )
        service = CrossSyncService({"anilist": source, "library": target})

        stats = service.run_pair(_pair(source="anilist", target="library", categories=[CATEGORY_HISTORY]))

        self.assertEqual(stats.added, 2)
        self.assertEqual(
            sorted((i["season"], i["episode"]) for i in target.added[0][1]),
            [(1, 1), (1, 2)],
        )

    def test_a_second_run_of_a_history_pair_writes_nothing(self) -> None:
        """Derived rows carry one shared date, so identity has to line up on
        season/episode alone or the pair re-adds the whole history each run."""
        rows = [self._episode("910", 1, 1), self._episode("910", 1, 2)]
        source = FakeAdapter("anilist", {CATEGORY_HISTORY: rows}, reads=(CATEGORY_HISTORY,), writes=())
        target = FakeAdapter(
            "library", {CATEGORY_HISTORY: []},
            reads=(CATEGORY_HISTORY,), writes=(CATEGORY_HISTORY,),
        )
        service = CrossSyncService({"anilist": source, "library": target})
        pair = _pair(source="anilist", target="library", categories=[CATEGORY_HISTORY])

        first = service.run_pair(pair)
        second = service.run_pair(pair)

        self.assertEqual(first.added, 2)
        self.assertEqual(second.added, 0)

    def test_a_target_that_cannot_write_the_category_is_reported(self) -> None:
        """Generic guard, kept because AniList's history write is narrow: it
        only ever advances progress on entries the user already has, so a pair
        can still find nothing it is allowed to do."""
        source = FakeAdapter(
            "trakt", {CATEGORY_HISTORY: [self._episode("910", 1, 1)]},
            reads=(CATEGORY_HISTORY,), writes=(CATEGORY_HISTORY,),
        )
        target = FakeAdapter(
            "anilist", {CATEGORY_HISTORY: []},
            reads=(CATEGORY_HISTORY,), writes=(CATEGORY_WATCHLIST,),
        )
        service = CrossSyncService({"trakt": source, "anilist": target})

        stats = service.run_pair(_pair(source="trakt", target="anilist", categories=[CATEGORY_HISTORY]))

        self.assertEqual(stats.added, 0)
        self.assertEqual(target.added, [])
        self.assertTrue(stats.errors or any(c.errors for c in stats.categories))


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
    def test_exact_anime_mapping_replaces_a_season_title_tmdb_id(self, lookup_by_anilist) -> None:
        lookup_by_anilist.return_value = {"themoviedb_id": {"movie": [999]}}
        enriched = enrich_identity({"media_type": "movie", "tmdb_id": "128", "anilist_id": "164"})
        self.assertEqual(enriched["tmdb_id"], "999")

    @patch("src.anime_mapping_store.resolve_tvdb_episode_from_anidb_episode", return_value=None)
    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_episode_is_mapped_to_tmdb_series_season(
        self, lookup_by_anilist, _resolve_episode,
    ) -> None:
        lookup_by_anilist.return_value = {
            "anilist_id": 200,
            "anidb_id": 300,
            "themoviedb_id": {"tv": 9000},
            "season": {"tvdb": 2, "tmdb": 3},
            "episode_offset": {"tvdb": 12, "tmdb": 0},
        }

        enriched = enrich_identity({
            "title": "Season title", "media_type": "tv", "simkl_type": "anime",
            "tmdb_id": "8123", "anilist_id": "200", "anidb_id": "300",
            "season": 1, "episode": 7,
        })

        self.assertEqual(enriched["tmdb_id"], "9000")
        self.assertEqual((enriched["season"], enriched["episode"]), (3, 7))
        self.assertEqual(item_key(enriched), "tv:tmdb:9000:s3e7")

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anilist_season_entries_share_the_mapped_tmdb_series_key(
        self, lookup_by_anilist,
    ) -> None:
        mappings = {
            100: {"themoviedb_id": {"tv": 9000}, "season": {"tmdb": 1}},
            200: {"themoviedb_id": {"tv": 9000}, "season": {"tmdb": 2}},
        }
        lookup_by_anilist.side_effect = lambda value: mappings[int(value)]

        first = enrich_identity({
            "media_type": "tv", "anilist_id": "100", "tmdb_id": "8100",
        })
        second = enrich_identity({
            "media_type": "tv", "anilist_id": "200", "tmdb_id": "8200",
        })

        self.assertEqual(item_key(first), "tv:tmdb:9000")
        self.assertEqual(item_key(second), "tv:tmdb:9000")
        self.assertEqual((first["season"], second["season"]), (1, 2))

    @patch("src.anime_mapping_store.resolve_tvdb_episode_from_anidb_episode")
    @patch("src.fribb_client.lookup_by_anidb")
    def test_anime_lists_xml_direct_tmdb_coordinates_take_priority(
        self, lookup_by_anidb, resolve_episode,
    ) -> None:
        lookup_by_anidb.return_value = {
            "anidb_id": 300, "themoviedb_id": {"tv": 9000},
            "season": {"tmdb": 2}, "episode_offset": {"tmdb": 0},
        }
        resolve_episode.return_value = {
            "tvdb_id": 10, "tvdb_season": 4, "tvdb_episode": 19,
            "tmdb_id": 9000, "tmdb_season": 5, "tmdb_episode": 2,
        }

        enriched = enrich_identity({
            "media_type": "tv", "simkl_type": "anime", "anidb_id": "300",
            "season": 1, "episode": 2,
        })

        self.assertEqual((enriched["season"], enriched["episode"]), (5, 2))

    @patch("src.fribb_client.lookup_by_anilist")
    def test_live_tmdb_plan_coordinates_are_not_remapped_through_root_entry(
        self, lookup_by_anilist,
    ) -> None:
        lookup_by_anilist.return_value = {
            "anilist_id": 154587, "themoviedb_id": {"tv": 209867},
            "season": {"tmdb": 1},
        }

        enriched = enrich_identity({
            "media_type": "tv", "simkl_type": "anime", "tmdb_id": "209867",
            "anilist_id": "154587", "season": 2, "episode": 1,
            "_syncmeta_tmdb_coordinates": True,
        })

        self.assertEqual((enriched["season"], enriched["episode"]), (2, 1))

    @patch("src.anime_mapping_store.resolve_tvdb_episode_from_anidb_episode", return_value=None)
    @patch("src.fribb_client.lookup_by_anilist")
    def test_pair_writes_tmdb_episode_coordinates_to_library_target(
        self, lookup_by_anilist, _resolve_episode,
    ) -> None:
        lookup_by_anilist.return_value = {
            "anilist_id": 200, "themoviedb_id": {"tv": 9000},
            "season": {"tmdb": 2}, "episode_offset": {"tmdb": 0},
        }
        source = FakeAdapter("simkl", {CATEGORY_HISTORY: [{
            "media_type": "tv", "simkl_type": "anime", "tmdb_id": "8123",
            "anilist_id": "200", "season": 1, "episode": 4,
        }]}, reads=(CATEGORY_HISTORY,), writes=(CATEGORY_HISTORY,))
        target = FakeAdapter(
            "library", {CATEGORY_HISTORY: []},
            reads=(CATEGORY_HISTORY,), writes=(CATEGORY_HISTORY,),
        )

        stats = CrossSyncService({"simkl": source, "library": target}).run_pair(
            _pair(source="simkl", target="library", categories=[CATEGORY_HISTORY]),
        )

        self.assertEqual(stats.added, 1)
        written = target.added[0][1][0]
        self.assertEqual((written["tmdb_id"], written["season"], written["episode"]), ("9000", 2, 4))

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


class TraktListDiscoveryTests(unittest.TestCase):
    class Client:
        def get_personal_lists_metadata(self):
            return [{"user": "me", "slug": "mine", "name": "Mine"}]

        def get_liked_lists_metadata(self):
            return [{"user": "other", "slug": "liked", "name": "Liked"}]

    def test_personal_and_liked_lists_are_shown(self) -> None:
        from src.providers import TraktAdapter
        sources = TraktAdapter(self.Client()).list_sources()
        keys = {entry["key"] for entry in sources}

        self.assertIn("list:me/mine", keys)
        self.assertIn("list:other/liked", keys)

    def test_one_failed_catalog_does_not_hide_the_other_or_native_feeds(self) -> None:
        from src.providers import TraktAdapter

        class PartiallyFailingClient(self.Client):
            def get_personal_lists_metadata(self):
                raise RuntimeError("Trakt personal lists unavailable")

        sources = TraktAdapter(PartiallyFailingClient()).list_sources()
        keys = {entry["key"] for entry in sources}

        self.assertIn("watchlist", keys)
        self.assertIn("collection", keys)
        self.assertIn("history", keys)
        self.assertIn("list:other/liked", keys)


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


class NativeHistorySourceTests(unittest.TestCase):
    def test_simkl_status_selections_do_not_suppress_history(self) -> None:
        from src.providers import SimklAdapter

        class Client:
            def __init__(self):
                self.history_calls = 0

            def get_watched_history(self):
                self.history_calls += 1
                return [_movie("7")]

        client = Client()
        adapter = SimklAdapter(client)

        items = adapter.fetch(CATEGORY_HISTORY, [
            "status:plantowatch:movies", "status:completed:shows",
        ])

        self.assertEqual([item["tmdb_id"] for item in items], ["7"])
        self.assertEqual(client.history_calls, 1)


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

    def test_two_way_pair_reads_and_writes_the_selected_target_list(self) -> None:
        source = FakeAdapter(
            "simkl", {CATEGORY_COLLECTION: [_movie("1")]},
            reads=(CATEGORY_COLLECTION,), writes=(CATEGORY_COLLECTION,),
        )
        target = FakeAdapter(
            "pmdb", {CATEGORY_COLLECTION: []},
            reads=(CATEGORY_COLLECTION,), writes=(CATEGORY_COLLECTION,),
        )
        service = CrossSyncService({"simkl": source, "pmdb": target})

        result = service.run_pair(_pair(
            source="simkl", target="pmdb", mode="two_way",
            categories=[CATEGORY_COLLECTION], target_list="list:42",
        ))

        self.assertEqual(result.added, 1)
        self.assertIn((CATEGORY_COLLECTION, ["list:42"]), target.fetched)
        self.assertEqual(target.target_lists_used, ["list:42"])

    def test_only_providers_that_support_it_advertise_target_lists(self) -> None:
        from src.providers import (
            AniListAdapter, MdbListAdapter, PmdbAdapter, SimklAdapter, TraktAdapter,
        )
        # Writing into a named list is a real capability, not a UI nicety: SIMKL
        # and AniList have no writable custom lists, so offering a destination
        # list for them would be a lie. MDBList's static lists are writable.
        self.assertTrue(TraktAdapter.supports_target_lists)
        self.assertTrue(PmdbAdapter.supports_target_lists)
        self.assertTrue(MdbListAdapter.supports_target_lists)
        self.assertFalse(SimklAdapter.supports_target_lists)
        self.assertFalse(AniListAdapter.supports_target_lists)
        # Watch history has no named destination anywhere.
        self.assertNotIn(CATEGORY_HISTORY, MdbListAdapter.target_list_categories)
        self.assertNotIn(CATEGORY_HISTORY, TraktAdapter.target_list_categories)

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


class PmdbCollectionTargetTests(unittest.TestCase):
    class FakePmdbClient:
        def __init__(self, existing=None):
            from types import SimpleNamespace
            self._config = SimpleNamespace(api_key="pm-key")
            self.existing = existing
            self.created = []
            self.added = []
            self.removed = []
            self.items = {}

        def find_list_by_name(self, name):
            return self.existing if self.existing and self.existing.get("name") == name else None

        def get_or_create_list(self, name, description="", is_public=False, list_type="custom"):
            self.created.append((name, description, is_public, list_type))
            self.existing = {"id": "collection-id", "name": name, "type": list_type}
            return self.existing

        def get_lists(self):
            return [self.existing] if self.existing else []

        def get_list_items(self, list_id):
            return list(self.items.get(str(list_id), []))

        def add_items_to_list_batch(self, list_id, payload):
            self.added.append((str(list_id), list(payload)))

        def remove_item_from_list(self, list_id, item_id):
            self.removed.append((str(list_id), str(item_id)))

        def find_list_by_type(self, _list_type):
            return None

    def test_collection_is_a_declared_read_write_capability(self) -> None:
        from src.providers import PmdbAdapter
        self.assertIn(CATEGORY_COLLECTION, PmdbAdapter.reads)
        self.assertIn(CATEGORY_COLLECTION, PmdbAdapter.writes)
        self.assertIn(CATEGORY_COLLECTION, PmdbAdapter.target_list_categories)

    def test_default_collection_read_never_creates_a_list(self) -> None:
        from src.providers import PmdbAdapter
        client = self.FakePmdbClient()
        adapter = PmdbAdapter(client)

        self.assertEqual(adapter.fetch(CATEGORY_COLLECTION), [])
        self.assertEqual(client.created, [])

    def test_default_collection_write_creates_and_reuses_managed_custom_list(self) -> None:
        from src.providers import PmdbAdapter
        client = self.FakePmdbClient()
        adapter = PmdbAdapter(client)

        result = adapter.add(CATEGORY_COLLECTION, [_movie("42")])

        self.assertEqual(result["added"], 1)
        self.assertEqual(client.created[0][0], "SyncMeta · Collection")
        self.assertEqual(client.created[0][3], "custom")
        self.assertEqual(client.added, [("collection-id", [{"tmdb_id": 42, "media_type": "movie"}])])

    def test_named_custom_list_accepts_collection_without_creating_default(self) -> None:
        from src.providers import PmdbAdapter
        client = self.FakePmdbClient()
        adapter = PmdbAdapter(client)

        result = adapter.add(CATEGORY_COLLECTION, [_movie("7")], "list:user-list")

        self.assertEqual(result["added"], 1)
        self.assertEqual(client.created, [])
        self.assertEqual(client.added[0][0], "user-list")

    def test_two_way_simkl_pmdb_collection_passes_runtime_validation(self) -> None:
        from src.providers import PmdbAdapter
        simkl = FakeAdapter(
            "simkl", {}, reads=(CATEGORY_COLLECTION,), writes=(CATEGORY_COLLECTION,),
        )
        pmdb = PmdbAdapter(self.FakePmdbClient())
        pair = _pair(
            source="simkl", target="pmdb", mode="two_way",
            categories=[CATEGORY_COLLECTION],
        )

        self.assertEqual(CrossSyncService({"simkl": simkl, "pmdb": pmdb}).validate_pair(pair), "")

    def test_collection_dry_run_does_not_create_the_default_list(self) -> None:
        from src.providers import PmdbAdapter
        source = FakeAdapter(
            "simkl", {CATEGORY_COLLECTION: [_movie("9")]},
            reads=(CATEGORY_COLLECTION,), writes=(CATEGORY_COLLECTION,),
        )
        client = self.FakePmdbClient()
        result = CrossSyncService(
            {"simkl": source, "pmdb": PmdbAdapter(client)}, dry_run=True,
        ).run_pair(_pair(source="simkl", target="pmdb", categories=[CATEGORY_COLLECTION]))

        self.assertEqual(result.added, 1)
        self.assertEqual(client.created, [])
        self.assertEqual(client.added, [])


class MdbListProviderTests(unittest.TestCase):
    """MDBList reads and writes: its own sync API plus the user's static lists."""

    class FakeMdbClient:
        def __init__(
            self, items_by_list=None, failing=(), sync_items=None, write_auth=True,
            user_lists=None,
        ):
            self._items = items_by_list or {}
            self._failing = set(failing)
            self._sync = sync_items or {}
            self._write_auth = write_auth
            self.calls: list[int] = []
            self.sync_reads: list[str] = []
            self.added: list[tuple[str, list]] = []
            self.removed: list[tuple[str, list]] = []
            self.list_changes: list[tuple] = []
            self.user_lists = list(user_lists or [])

        def has_write_auth(self):
            return self._write_auth

        def get_list_items(self, list_id: int):
            self.calls.append(list_id)
            if list_id in self._failing:
                raise RuntimeError("mdblist 500")
            return list(self._items.get(list_id, []))

        def get_user_lists(self):
            return list(self.user_lists)

        def get_sync_items(self, category):
            self.sync_reads.append(category)
            return list(self._sync.get(category, []))

        def add_sync_items(self, category, items):
            self.added.append((category, list(items)))
            return {"added": len(items)}

        def remove_sync_items(self, category, items):
            self.removed.append((category, list(items)))
            return {"removed": len(items)}

        def change_list_items(self, list_id, items, action):
            self.list_changes.append((list_id, list(items), action))
            return {"changed": len(items)}

    def _adapter(self, **kwargs):
        from src.providers import MdbListAdapter
        return MdbListAdapter(**kwargs)

    def test_writable_when_a_credential_exists(self) -> None:
        adapter = self._adapter(client=self.FakeMdbClient(), selected_lists=[])
        self.assertTrue(adapter.can_write())
        self.assertEqual(
            sorted(adapter.writable_categories()),
            sorted([CATEGORY_WATCHLIST, CATEGORY_COLLECTION, CATEGORY_HISTORY]),
        )

    def test_not_writable_without_any_credential(self) -> None:
        adapter = self._adapter(
            client=self.FakeMdbClient(write_auth=False), selected_lists=[],
        )
        self.assertFalse(adapter.can_write())
        self.assertEqual(adapter.writable_categories(), ())
        self.assertIn("API key", adapter.write_blocked_reason())
        with self.assertRaises(ValueError):
            adapter.add(CATEGORY_WATCHLIST, [_movie("1")])

    def test_account_categories_come_from_the_sync_api(self) -> None:
        client = self.FakeMdbClient(sync_items={CATEGORY_HISTORY: [_movie("7")]})
        adapter = self._adapter(client=client, selected_lists=[])
        items = adapter.fetch(CATEGORY_HISTORY)
        self.assertEqual([i["tmdb_id"] for i in items], ["7"])
        self.assertEqual(client.sync_reads, [CATEGORY_HISTORY])

    def test_account_lists_are_discovered_without_legacy_selections(self) -> None:
        client = self.FakeMdbClient(user_lists=[{"id": 42, "name": "Mine"}])
        adapter = self._adapter(client=client, selected_lists=[])

        sources = adapter.list_sources()

        self.assertIn(
            {"key": "list:42", "label": "Mine", "category": CATEGORY_WATCHLIST, "kind": "list"},
            sources,
        )
        self.assertEqual(adapter.target_lists(), [{"key": "list:42", "label": "Mine"}])

    def test_newly_discovered_list_can_be_read_without_legacy_selection(self) -> None:
        client = self.FakeMdbClient(
            {42: [_movie("7")]}, user_lists=[{"id": 42, "name": "Mine"}],
        )
        adapter = self._adapter(client=client, selected_lists=[])

        items = adapter.fetch(CATEGORY_WATCHLIST, ["list:42"])

        self.assertEqual([item["tmdb_id"] for item in items], ["7"])

    def test_history_never_folds_in_a_curated_list(self) -> None:
        # A curated list carries no watch dates, so mixing one into history
        # would invent history that never happened.
        client = self.FakeMdbClient({10: [_movie("1")]}, sync_items={CATEGORY_HISTORY: []})
        adapter = self._adapter(client=client, selected_lists=[{"id": 10, "name": "A"}])
        self.assertEqual(adapter.fetch(CATEGORY_HISTORY), [])
        self.assertEqual(client.calls, [])

    def test_history_is_not_suppressed_by_curated_list_selections(self) -> None:
        client = self.FakeMdbClient(
            {10: [_movie("1")]}, sync_items={CATEGORY_HISTORY: [_movie("7")]},
        )
        adapter = self._adapter(client=client, selected_lists=[{"id": 10, "name": "A"}])

        items = adapter.fetch(CATEGORY_HISTORY, ["list:10"])

        self.assertEqual([i["tmdb_id"] for i in items], ["7"])
        self.assertEqual(client.calls, [])

    def test_a_list_selection_does_not_also_read_the_whole_account(self) -> None:
        client = self.FakeMdbClient(
            {10: [_movie("1")]}, sync_items={CATEGORY_WATCHLIST: [_movie("99")]},
        )
        adapter = self._adapter(client=client, selected_lists=[{"id": 10, "name": "A"}])
        items = adapter.fetch(CATEGORY_WATCHLIST, ["list:10"])
        self.assertEqual([i["tmdb_id"] for i in items], ["1"])
        self.assertEqual(client.sync_reads, [], "a named selection must not widen to the account")

    def test_selected_lists_are_combined_and_deduplicated(self) -> None:
        client = self.FakeMdbClient({
            10: [_movie("1"), _movie("2")],
            11: [_movie("2"), _movie("3")],
        })
        adapter = self._adapter(
            client=client, selected_lists=[{"id": 10, "name": "A"}, {"id": 11, "name": "B"}],
        )
        items = adapter.fetch(CATEGORY_WATCHLIST, ["list:10", "list:11"])
        self.assertEqual(sorted(i["tmdb_id"] for i in items), ["1", "2", "3"])

    def test_items_are_fetched_once_and_reused_across_categories(self) -> None:
        client = self.FakeMdbClient({10: [_movie("1")]})
        adapter = self._adapter(client=client, selected_lists=[{"id": 10, "name": "A"}])
        adapter.fetch(CATEGORY_WATCHLIST, ["list:10"])
        adapter.fetch(CATEGORY_COLLECTION, ["list:10"])
        self.assertEqual(client.calls, [10], "the same items answer both categories")

    def test_one_failing_list_does_not_lose_the_others(self) -> None:
        client = self.FakeMdbClient({10: [_movie("1")], 11: [_movie("2")]}, failing=(10,))
        adapter = self._adapter(
            client=client, selected_lists=[{"id": 10, "name": "A"}, {"id": 11, "name": "B"}],
        )
        items = adapter.fetch(CATEGORY_WATCHLIST, ["list:10", "list:11"])
        self.assertEqual([i["tmdb_id"] for i in items], ["2"])

    def test_mdblist_can_feed_another_service(self) -> None:
        client = self.FakeMdbClient({10: [_movie("1"), _movie("2")]})
        source = self._adapter(client=client, selected_lists=[{"id": 10, "name": "A"}])
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: [_movie("1")]})
        service = CrossSyncService({"mdblist": source, "simkl": target})

        stats = service.run_pair(_pair(
            source="mdblist", target="simkl", source_lists=["list:10"],
        ))

        self.assertEqual(stats.added, 1)
        self.assertEqual([i["tmdb_id"] for i in target.added[0][1]], ["2"])

    def test_mdblist_receives_a_watchlist_from_another_service(self) -> None:
        client = self.FakeMdbClient(sync_items={CATEGORY_WATCHLIST: [_movie("1")]})
        target = self._adapter(client=client, selected_lists=[])
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        service = CrossSyncService({"trakt": source, "mdblist": target})

        stats = service.run_pair(_pair(target="mdblist"))

        self.assertEqual(stats.errors, [])
        self.assertEqual(stats.added, 1, "only the item MDBList was missing")
        self.assertEqual(client.added[0][0], CATEGORY_WATCHLIST)
        self.assertEqual([i["tmdb_id"] for i in client.added[0][1]], ["2"])

    def test_a_second_run_writes_nothing(self) -> None:
        # The idempotence guard: identity must normalise so an already-synced
        # item is not re-added on every run.
        client = self.FakeMdbClient(sync_items={CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        target = self._adapter(client=client, selected_lists=[])
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("1"), _movie("2")]})
        service = CrossSyncService({"trakt": source, "mdblist": target})

        stats = service.run_pair(_pair(target="mdblist"))

        self.assertEqual(stats.added, 0)
        self.assertEqual(client.added, [])

    def test_writing_into_a_named_list_uses_the_list_endpoint(self) -> None:
        client = self.FakeMdbClient({10: []})
        target = self._adapter(client=client, selected_lists=[{"id": 10, "name": "A"}])
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: [_movie("5")]})
        service = CrossSyncService({"trakt": source, "mdblist": target})

        stats = service.run_pair(_pair(target="mdblist", target_list="list:10"))

        self.assertEqual(stats.errors, [])
        self.assertEqual(client.list_changes[0][0], "10")
        self.assertEqual(client.list_changes[0][2], "add")
        self.assertEqual(client.added, [], "a named list must not go through the sync API")

    def test_history_has_no_named_destination(self) -> None:
        adapter = self._adapter(
            client=self.FakeMdbClient(), selected_lists=[{"id": 10, "name": "A"}],
        )
        self.assertNotIn(CATEGORY_HISTORY, adapter.target_list_categories)
        with self.assertRaises(ValueError):
            adapter.add(CATEGORY_HISTORY, [_movie("1")], target_list="list:10")


if __name__ == "__main__":
    unittest.main()



class RemovalGuardTests(unittest.TestCase):
    """A run that would empty a list is paused rather than performed.

    A source that answers with far less than it holds — a half-read page, a
    token that just expired, a provider outage — is indistinguishable from the
    user having deleted everything, except by how much it would destroy.
    """

    @staticmethod
    def _movies(count, offset=0):
        return [_movie(str(1000 + i), f"Film {i}") for i in range(offset, offset + count)]

    def _run(self, source_items, target_items, **service_kwargs):
        source = FakeAdapter("trakt", {CATEGORY_WATCHLIST: source_items})
        target = FakeAdapter("simkl", {CATEGORY_WATCHLIST: target_items})
        service = CrossSyncService({"trakt": source, "simkl": target}, **service_kwargs)
        result = service.run_pair(_pair(removal_mode=REMOVAL_MIRROR))
        return result, target

    def test_large_removal_is_blocked_and_reported(self):
        result, target = self._run(self._movies(1), self._movies(40))
        category = result.categories[0]
        self.assertEqual(category.removed, 0)
        self.assertEqual(len(category.blocked_removals), 1)
        blocked = category.blocked_removals[0]
        self.assertEqual(blocked["removals"], 39)
        self.assertEqual(blocked["target_size"], 40)
        self.assertEqual(target.removed, [])
        # And it rides out on the payload the dashboard reads.
        self.assertEqual(len(result.to_dict()["blocked_removals"]), 1)

    def test_ordinary_removal_still_happens(self):
        result, target = self._run(self._movies(38), self._movies(40))
        category = result.categories[0]
        self.assertEqual(category.removed, 2)
        self.assertEqual(category.blocked_removals, [])
        self.assertTrue(target.removed)

    def test_small_lists_are_exempt(self):
        # Removing 2 of 3 is 67% and entirely ordinary; the guard must not speak.
        result, _ = self._run(self._movies(1), self._movies(3))
        self.assertEqual(result.categories[0].removed, 2)
        self.assertEqual(result.categories[0].blocked_removals, [])

    def test_guard_can_be_turned_off(self):
        result, _ = self._run(self._movies(1), self._movies(40), guard_large_removals=False)
        self.assertEqual(result.categories[0].removed, 39)
        self.assertEqual(result.categories[0].blocked_removals, [])

    def test_threshold_is_configurable(self):
        result, _ = self._run(self._movies(30), self._movies(40), guard_removal_percent=10)
        self.assertEqual(result.categories[0].removed, 0)
        self.assertTrue(result.categories[0].blocked_removals)

    def test_blocked_two_way_side_is_not_re_added_to_the_other(self):
        """Two-way: a paused removal leaves those keys alone on both sides."""
        first_items = self._movies(40)
        first = FakeAdapter("trakt", {CATEGORY_WATCHLIST: first_items})
        second = FakeAdapter("simkl", {CATEGORY_WATCHLIST: self._movies(1)})
        managed = {"p1": {CATEGORY_WATCHLIST: [
            item_key(enrich_identity(item)) for item in first_items
        ]}}
        service = CrossSyncService({"trakt": first, "simkl": second}, managed_keys=managed)
        result = service.run_pair(_pair(removal_mode=REMOVAL_MANAGED, mode="two_way"))
        category = result.categories[0]
        self.assertEqual(category.removed, 0)
        self.assertTrue(category.blocked_removals)
        self.assertEqual(second.added, [])
        # The blocked keys stay in the agreed state, so the next run sees the
        # same situation rather than treating them as never-synced.
        self.assertEqual(len(category.managed_keys), 40)
