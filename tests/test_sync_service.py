import time
import unittest
from unittest.mock import patch

from src.config import AniListConfig, AppConfig, PublicMetaDBConfig, SimklConfig, SyncConfig
from src.matcher import MatchResult
from src.sync_service import SyncCancelled, SyncService, SyncStats
from src.trakt_client import TraktAuthenticationError


class StubSimklClient:
    def __init__(self) -> None:
        self.last_history_since = None

    def get_status(self, status_key: str, media_types: list[str]) -> dict[str, list[dict]]:
        if status_key == "watching" and media_types == ["shows"]:
            return {
                "shows": [{
                    "title": "Demo Show",
                    "media_type": "tv",
                }],
            }
        return {media_type: [] for media_type in media_types}

    def get_watched_history(self, since: str | None = None) -> list[dict]:
        self.last_history_since = since
        return [
            {"tmdb_id": 801, "media_type": "movie", "simkl_type": "movies", "watched_at": "2026-04-01T12:00:00Z", "title": "SIMKL Movie"},
            {"tmdb_id": 802, "media_type": "tv", "simkl_type": "anime", "season": 1, "episode": 3, "watched_at": "2026-04-01T13:00:00Z", "title": "SIMKL Episode"},
        ]

    def get_playback_progress(self) -> list[dict]:
        return [
            {"tmdb_id": 803, "media_type": "movie", "position_ms": 1_200_000, "runtime_ms": 3_600_000, "progress": 33.3, "title": "SIMKL Resume Movie"},
        ]

    def expand_aggregate_history_item(self, item: dict) -> list[dict]:
        return []

    def _get_tmdb_season_plan_cached(self, tmdb_id: int) -> list[tuple[int, int]]:
        if tmdb_id == 9501:
            return [(1, 25), (2, 0)]
        return []


class StubMatcher:
    def resolve_tmdb_id(self, item: dict) -> int | None:
        return 101


class StubPMDBClient:
    def __init__(self) -> None:
        self.deleted_lists: list[str] = []
        self.deleted_watched: list[str] = []
        self.removed_list_items: list[tuple[str, str]] = []
        self.created_lists: list[dict] = []
        self.added_items: list[dict] = []
        self.created_mappings: list[dict] = []
        self.anime_seasons_by_tmdb: dict[int, list[dict]] = {}
        self.anime_seasons_calls: list[int] = []
        self.watched: list[dict] = []
        self.resume_points: list[dict] = []
        self.resume_batches: list[list[dict]] = []
        self.list_item_reads = 0
        self.list_items_by_id: dict[str, list[dict]] = {}

    def get_or_create_list(self, name: str, description: str, is_public: bool = False, list_type: str = "custom") -> dict:
        self.created_lists.append({
            "name": name,
            "is_public": is_public,
            "list_type": list_type,
        })
        return {"id": "pmdb-active", "name": name}

    def get_list_items(self, list_id: str) -> list[dict]:
        self.list_item_reads += 1
        return list(self.list_items_by_id.get(str(list_id), []))

    def add_item_to_list(self, list_id: str, tmdb_id: int, media_type: str) -> None:
        self.added_items.append({
            "list_id": list_id,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        })
        return None

    def create_id_mapping(self, tmdb_id: int, media_type: str, id_type: str, id_value: str) -> bool:
        self.created_mappings.append({
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "id_type": id_type,
            "id_value": id_value,
        })
        return True

    def get_anime_seasons(self, tmdb_id: int) -> list[dict]:
        self.anime_seasons_calls.append(int(tmdb_id))
        return list(self.anime_seasons_by_tmdb.get(int(tmdb_id), []))

    def remove_item_from_list(self, list_id: str, item_id: str) -> None:
        self.removed_list_items.append((str(list_id), str(item_id)))
        self.list_items_by_id[str(list_id)] = [
            item for item in self.list_items_by_id.get(str(list_id), [])
            if str(item.get("id", "")) != str(item_id)
        ]

    def delete_list(self, list_id: str) -> bool:
        self.deleted_lists.append(list_id)
        return True

    def find_list_by_name(self, name: str) -> dict | None:
        return None

    def get_watched_history(self) -> list[dict]:
        return list(self.watched)

    def mark_watched(
        self,
        tmdb_id: int,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
        watched_at: str | None = None,
        dedupe: bool = False,
    ) -> dict:
        item = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "watched_at": watched_at,
        }
        if season is not None:
            item["season"] = season
        if episode is not None:
            item["episode"] = episode
        self.watched.append(item)
        return {"success": True, "item": item}

    def get_resume_points(self) -> list[dict]:
        return list(self.resume_points)

    def save_resume_points_batch(self, items: list[dict]) -> dict:
        self.resume_batches.append(list(items))
        return {
            "results": [
                {**item, "action": "completed" if item["position_ms"] >= int(item["runtime_ms"] * 0.8) else "saved"}
                for item in items
            ]
        }

    def delete_watched_entry(self, watched_id: str) -> bool:
        self.deleted_watched.append(watched_id)
        self.watched = [item for item in self.watched if str(item.get("id", "")) != watched_id]
        return True

    def stats_snapshot(self) -> dict[str, int]:
        return {
            "mapping_lookup_hits": 0,
            "mapping_lookup_misses": 0,
            "mapping_lookup_auth_soft_misses": 0,
            "mapping_lookup_errors": 0,
            "list_write_successes": len(self.created_lists) + len(self.added_items),
            "list_write_failures": 0,
        }


class StubTraktClient:
    def __init__(self, playback_progress: list[dict] | None = None) -> None:
        self.last_history_since = None
        self.playback_progress = playback_progress or [
            {"tmdb_id": 903, "media_type": "movie", "position_ms": 1_800_000, "runtime_ms": 3_600_000, "progress": 50, "title": "Resume Movie"},
            {"tmdb_id": 904, "media_type": "tv", "season": 2, "episode": 5, "position_ms": 3_000_000, "runtime_ms": 3_600_000, "progress": 83.3, "title": "Completed Episode"},
        ]

    def get_watchlist(self) -> list[dict]:
        return [
            {"title": "Watchlist Movie", "media_type": "movie"},
            {"title": "Watchlist Show", "media_type": "tv"},
        ]

    def get_default_catalog(self, catalog_key: str) -> list[dict]:
        return [{"title": f"Default {catalog_key}", "media_type": "movie"}]

    def get_list_items(self, user: str, slug: str) -> list[dict]:
        return [{"title": f"{user}-{slug}", "media_type": "movie"}]

    def get_liked_lists(self) -> list[dict]:
        return []

    def get_watched_history(self, since: str | None = None, status_callback=None) -> list[dict]:
        self.last_history_since = since
        return [
            {"tmdb_id": 901, "media_type": "movie", "watched_at": "2026-04-01T12:00:00Z", "title": "Watched Movie"},
            {"tmdb_id": 902, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-04-01T13:00:00Z", "title": "Watched Episode"},
        ]

    def get_watched_state(self, status_callback=None) -> list[dict]:
        return []

    def get_playback_progress(self) -> list[dict]:
        return list(self.playback_progress)


class StubWatchedStateTraktClient(StubTraktClient):
    """Play log reports one episode; the watched state knows about three seasons."""

    def __init__(self, watched_state: list[dict] | None = None) -> None:
        super().__init__()
        self.watched_state_calls = 0
        self._watched_state = watched_state if watched_state is not None else [
            {"tmdb_id": 910, "media_type": "tv", "season": 1, "episode": 1,
             "watched_at": "2025-01-01T00:00:00Z", "title": "Long Anime",
             "plays": 1, "watched_state": True, "cursor_exempt": True},
            {"tmdb_id": 910, "media_type": "tv", "season": 2, "episode": 1,
             "watched_at": "2025-06-01T00:00:00Z", "title": "Long Anime",
             "plays": 1, "watched_state": True, "cursor_exempt": True},
            {"tmdb_id": 910, "media_type": "tv", "season": 3, "episode": 1,
             "watched_at": "2026-01-01T00:00:00Z", "title": "Long Anime",
             "plays": 1, "watched_state": True, "cursor_exempt": True},
        ]

    def get_watched_history(self, since: str | None = None, status_callback=None) -> list[dict]:
        self.last_history_since = since
        return [{
            "tmdb_id": 910, "media_type": "tv", "season": 3, "episode": 1,
            "watched_at": "2026-01-01T00:00:00Z", "title": "Long Anime",
        }]

    def get_watched_state(self, status_callback=None) -> list[dict]:
        self.watched_state_calls += 1
        return [dict(row) for row in self._watched_state]


class StubRepeatedWatchTraktClient(StubTraktClient):
    def get_watched_history(self, since: str | None = None, status_callback=None) -> list[dict]:
        self.last_history_since = since
        return [
            {"tmdb_id": 901, "media_type": "movie", "watched_at": "2026-04-01T12:00:00Z", "title": "Watched Movie"},
            {"tmdb_id": 901, "media_type": "movie", "watched_at": "2026-04-02T12:00:00Z", "title": "Watched Movie"},
        ]


class StubUnauthorizedTraktClient(StubTraktClient):
    def get_playback_progress(self) -> list[dict]:
        raise TraktAuthenticationError("Trakt token expired, reconnect Trakt.")


class StubMdbListClient:
    def get_list_items(self, list_id: int) -> list[dict]:
        return [{"title": f"MDB-{list_id}", "media_type": "movie"}]


class StubActivityMatcher:
    def resolve_tmdb_id(self, item: dict) -> int | None:
        title = item.get("title")
        if title == "Fallback Show Episode":
            return 811
        if title == "Fallback Anime Episode":
            return 812
        if title == "Aggregate Anime":
            return 814
        if title == "Fallback Resume Show":
            return 813
        return item.get("tmdb_id")


class SyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        SyncService._shared_anime_seasons_cache.clear()
        SyncService._shared_fribb_lookup_cache.clear()
        SyncService._shared_anime_history_remap_cache.clear()

    def build_service(self, delete_disabled_lists: bool) -> tuple[SyncService, StubPMDBClient]:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={
                    "shows": ["watching"],
                    "movies": [],
                    "anime": [],
                },
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=delete_disabled_lists,
                dry_run=False,
                media_types=["shows"],
            ),
        )
        service = SyncService(config, managed_lists=[
            {
                "list_name": "SIMKL - Series - Watching",
                "list_id": "pmdb-active",
                "display_name": "Watching - Series",
                "source_name": "SIMKL",
            },
            {
                "list_name": "Trakt List - demo - old-list",
                "list_id": "pmdb-disabled",
                "display_name": "old-list",
                "source_name": "Trakt by demo",
            },
        ])
        pmdb = StubPMDBClient()
        service._simkl = StubSimklClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb
        return service, pmdb

    def test_pmdb_watchlist_merge_reports_error_instead_of_crashing_on_trakt_auth_failure(self) -> None:
        config = AppConfig(
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                dry_run=False,
                media_types=["shows"],
                trakt_sync_to_pmdb_watchlist=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        class ForbiddenTraktClient(StubTraktClient):
            def get_watchlist(self) -> list[dict]:
                raise TraktAuthenticationError("Trakt returned 403 Forbidden — reconnect Trakt.")

        service._trakt = ForbiddenTraktClient()

        stats = service._sync_pmdb_watchlist()

        self.assertIsNotNone(stats)
        self.assertEqual(stats.display_name, "PMDB Watchlist")
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("403 Forbidden", stats.errors[0])
        self.assertIn("left untouched", stats.errors[0])
        # The merge must not run on a partial source set: nothing written, nothing removed.
        self.assertEqual(pmdb.created_lists, [])
        self.assertEqual(pmdb.added_items, [])
        self.assertEqual(pmdb.removed_list_items, [])

    def test_live_progress_publishes_pending_row_before_list_finishes(self) -> None:
        progress_events: list[list[dict]] = []
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={
                    "shows": ["watching"],
                    "movies": [],
                    "anime": [],
                },
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows"],
            ),
        )
        service = SyncService(config, progress_callback=lambda rows: progress_events.append(rows))
        service._simkl = StubSimklClient()
        service._matcher = StubMatcher()
        service._pmdb = StubPMDBClient()

        service.run()

        self.assertGreaterEqual(len(progress_events), 2)
        self.assertTrue(any(
            any(row.get("display_name") == "Watching - Series" and row.get("items_fetched") == 0 for row in event)
            for event in progress_events
        ))
        self.assertTrue(any(
            any(row.get("display_name") == "Watching - Series" and row.get("items_fetched") == 1 for row in event)
            for event in progress_events
        ))

    def test_pmdb_list_items_are_cached_per_run(self) -> None:
        service, pmdb = self.build_service(delete_disabled_lists=False)

        items_first = service._get_cached_list_items("pmdb-active")
        items_second = service._get_cached_list_items("pmdb-active")

        self.assertEqual(items_first, [])
        self.assertEqual(items_second, [])
        self.assertEqual(pmdb.list_item_reads, 1)

    def test_deletes_disabled_managed_lists_when_enabled(self) -> None:
        service, pmdb = self.build_service(delete_disabled_lists=True)

        results = service.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(pmdb.deleted_lists, ["pmdb-active", "pmdb-disabled"])
        self.assertEqual([item["name"] for item in pmdb.created_lists], ["Watching - Series"])
        self.assertEqual(
            [item["list_name"] for item in service.managed_lists],
            ["Watching - Series"],
        )

    def test_keeps_disabled_managed_lists_when_option_off(self) -> None:
        service, pmdb = self.build_service(delete_disabled_lists=False)

        service.run()

        self.assertEqual(pmdb.deleted_lists, [])
        self.assertEqual([item["name"] for item in pmdb.created_lists], ["Watching - Series"])
        self.assertEqual(
            [item["list_name"] for item in service.managed_lists],
            ["SIMKL - Series - Watching", "Trakt List - demo - old-list", "Watching - Series"],
        )

    def test_anime_sync_does_not_contribute_external_ids_to_pmdb(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={
                    "shows": [],
                    "movies": [],
                    "anime": ["watching"],
                },
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
            ),
        )
        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        class AnimeMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 1, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=209867, resolution_kind="direct_tmdb")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return 209867

        service._matcher = AnimeMatcher()

        service._sync_list([{
            "title": "Frieren: Beyond Journey's End",
            "year": 2023,
            "media_type": "tv",
            "simkl_type": "anime",
            "tmdb_id": "999999",
            "imdb_id": "tt22248376",
            "mal_id": "59978",
            "anilist_id": "154587",
            "root_mal_id": "59978",
            "root_anilist_id": "154587",
            "trakt_id": 12345,
            "ids": {
                "imdb": "tt22248376",
                "mal": "59978",
                "anilist": "154587",
                "root_mal": "59978",
                "root_anilist": "154587",
                "trakt": 12345,
            },
        }], "Watching - Anime", "Auto-synced anime")

        self.assertEqual(pmdb.created_mappings, [])

    def test_anime_root_series_resolution_does_not_contribute_child_ids_to_pmdb(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"anime": ["watching"], "shows": [], "movies": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["anime"]),
            )
        )
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        class RootSeriesMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 1, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=20, resolution_kind="root_series")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return 20

        service._matcher = RootSeriesMatcher()

        service._sync_list([{
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "9999811",
            "mal_id": "9999812",
            "root_anilist_id": "9999813",
            "root_mal_id": "9999814",
            "ids": {
                "anilist": "9999811",
                "mal": "9999812",
                "root_anilist": "9999813",
                "root_mal": "9999814",
            },
        }], "Watching - Anime", "Auto-synced anime")

        contributed = {(item["id_type"], item["id_value"]) for item in pmdb.created_mappings}
        self.assertNotIn(("anilist", "9999811"), contributed)
        self.assertNotIn(("mal", "9999812"), contributed)
        self.assertIn(("anilist", "9999813"), contributed)
        self.assertIn(("mal", "9999814"), contributed)

    def test_anilist_anime_lists_remove_stale_items_even_when_remove_missing_is_off(self) -> None:
        config = AppConfig(
            anilist=AniListConfig(
                enabled=True,
                username="demo",
                selected_statuses=["completed"],
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
            ),
        )
        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.list_items_by_id["pmdb-active"] = [
            {"id": "stale-ice3", "tmdb_id": 154634, "media_type": "movie"},
            {"id": "keep-naruto", "tmdb_id": 46260, "media_type": "tv"},
        ]
        service._pmdb = pmdb

        class StubAniListClient:
            _FORMAT_FILTER_MAP: dict = {}

            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def get_statuses(self, statuses: list[str]) -> dict[str, list[dict]]:
                self.statuses = statuses
                return {
                    "completed": [
                        {
                            "title": "Naruto",
                            "year": 2002,
                            "media_type": "tv",
                            "simkl_type": "anime",
                            "anilist_id": "20",
                            "ids": {"anilist": "20"},
                            "anime_resolve_mode": "list_identity",
                        }
                    ]
                }

        class DirectAnimeMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 1, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=46260, resolution_kind="fribb_exact", match_confidence="exact")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return 46260

        service._matcher = DirectAnimeMatcher()

        service._simkl = StubSimklClient()
        from unittest.mock import patch
        with patch("src.anilist_client.AniListClient", StubAniListClient):
            results = service.run()

        anime_stats = next(item for item in results if item.source_name == "AniList")
        self.assertEqual(anime_stats.items_removed, 1)
        self.assertEqual(pmdb.removed_list_items, [("pmdb-active", "stale-ice3")])

    def test_anilist_anime_lists_do_not_preserve_manual_additions(self) -> None:
        config = AppConfig(
            anilist=AniListConfig(
                enabled=True,
                username="demo",
                selected_statuses=["completed"],
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
            ),
        )
        service = SyncService(
            config,
            manual_list_additions={
                "Completed - Anime": [
                    {"tmdb_id": 154634, "media_type": "movie"},
                ]
            },
        )
        pmdb = StubPMDBClient()
        pmdb.list_items_by_id["pmdb-active"] = [
            {"id": "stale-ice3", "tmdb_id": 154634, "media_type": "movie"},
            {"id": "keep-naruto", "tmdb_id": 46260, "media_type": "tv"},
        ]
        service._pmdb = pmdb

        class StubAniListClient:
            _FORMAT_FILTER_MAP: dict = {}

            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def get_statuses(self, statuses: list[str]) -> dict[str, list[dict]]:
                return {
                    "completed": [
                        {
                            "title": "Naruto",
                            "year": 2002,
                            "media_type": "tv",
                            "simkl_type": "anime",
                            "anilist_id": "20",
                            "ids": {"anilist": "20"},
                            "anime_resolve_mode": "list_identity",
                        }
                    ]
                }

        class DirectAnimeMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 1, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=46260, resolution_kind="fribb_exact", match_confidence="exact")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return 46260

        service._matcher = DirectAnimeMatcher()

        service._simkl = StubSimklClient()
        from unittest.mock import patch
        with patch("src.anilist_client.AniListClient", StubAniListClient):
            results = service.run()

        anime_stats = next(item for item in results if item.source_name == "AniList")
        self.assertEqual(anime_stats.items_removed, 1)
        self.assertEqual(pmdb.removed_list_items, [("pmdb-active", "stale-ice3")])

    def test_anilist_status_sync_filters_out_movies_from_anime_lists(self) -> None:
        config = AppConfig(
            anilist=AniListConfig(
                enabled=True,
                username="demo",
                selected_statuses=["completed"],
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
            ),
        )
        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        class StubAniListClient:
            _FORMAT_FILTER_MAP: dict = {}

            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def get_statuses(self, statuses: list[str]) -> dict[str, list[dict]]:
                return {
                    "completed": [
                        {
                            "title": "Naruto",
                            "year": 2002,
                            "media_type": "tv",
                            "simkl_type": "anime",
                            "anilist_id": "20",
                            "ids": {"anilist": "20"},
                            "anime_resolve_mode": "list_identity",
                        },
                        {
                            "title": "Your Name.",
                            "year": 2016,
                            "media_type": "movie",
                            "simkl_type": "anime",
                            "anilist_id": "21519",
                            "ids": {"anilist": "21519"},
                            "anime_resolve_mode": "list_identity",
                        },
                    ]
                }

        class TypeAwareMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 1, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                if item["title"] == "Naruto":
                    return MatchResult(tmdb_id=46260, resolution_kind="fribb_exact", match_confidence="exact")
                raise AssertionError(f"Movie item should not be synced into anime list: {item['title']}")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                if item["title"] == "Naruto":
                    return 46260
                raise AssertionError(f"Movie item should not be synced into anime list: {item['title']}")

        service._matcher = TypeAwareMatcher()

        service._simkl = StubSimklClient()
        from unittest.mock import patch
        with patch("src.anilist_client.AniListClient", StubAniListClient):
            results = service.run()

        anime_stats = next(item for item in results if item.source_name == "AniList")
        self.assertEqual(anime_stats.items_fetched, 1)
        self.assertEqual(anime_stats.items_added, 1)
        self.assertEqual(len(pmdb.added_items), 1)
        self.assertEqual(pmdb.added_items[0]["tmdb_id"], 46260)

    def test_anime_list_tmdb_collision_is_reported_as_unresolved_not_duplicate(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"anime": ["completed"], "shows": [], "movies": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["anime"]),
            )
        )
        service._pmdb = StubPMDBClient()

        class CollisionMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 2, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=46260, resolution_kind="fribb_exact", match_confidence="exact")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return 46260

        service._matcher = CollisionMatcher()

        stats = service._sync_list(
            [
                {
                    "title": "Naruto",
                    "year": 2002,
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "anilist_id": "20",
                    "mal_id": "20",
                    "anime_resolve_mode": "list_identity",
                },
                {
                    "title": "Naruto Shippuden",
                    "year": 2007,
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "anilist_id": "1735",
                    "mal_id": "1735",
                    "anime_resolve_mode": "list_identity",
                },
            ],
            "Completed - Anime",
            "Auto-synced 'Completed' anime from AniList",
            display_name="Completed - Anime",
            source_name="AniList",
            selection={"source": "anilist", "status": "completed"},
            force_remove_missing=True,
        )

        self.assertEqual(stats.items_skipped_duplicate, 0)
        self.assertEqual(stats.items_skipped_unresolved, 1)
        self.assertTrue(any(item.get("unresolved_reason") == "tmdb_collision" for item in stats.unresolved_items))

    def test_anime_list_resolution_uses_direct_fribb_entry_not_root_fallback(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"anime": ["watching"], "shows": [], "movies": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["anime"]),
            )
        )

        class WrongRootMatcher:
            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=20, resolution_kind="external_mapping")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return 20

        lookups: list[bool] = []

        def fake_lookup_fribb(item: dict, allow_root_fallback: bool = True) -> dict | None:
            lookups.append(allow_root_fallback)
            if allow_root_fallback:
                return {"themoviedb": "20"}
            return {"themoviedb": "65930"}

        service._matcher = WrongRootMatcher()
        service._lookup_fribb_entry = fake_lookup_fribb  # type: ignore[method-assign]

        result = service._resolve_match({
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "97938",
            "root_anilist_id": "20",
            "mal_id": "34566",
            "root_mal_id": "1735",
            "ids": {
                "anilist": "97938",
                "root_anilist": "20",
                "mal": "34566",
                "root_mal": "1735",
            },
        })

        self.assertEqual(result.tmdb_id, 65930)
        self.assertEqual(result.resolution_kind, "fribb_exact")
        self.assertEqual(result.match_confidence, "exact")
        self.assertEqual(result.anime_mapping_source, "fribb_exact")
        self.assertEqual(lookups, [False])

    def test_anilist_list_resolution_uses_direct_fribb_entry_not_root_fallback(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"anime": [], "shows": [], "movies": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                anilist=AniListConfig(username="demo", selected_statuses=["COMPLETED"]),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["anime"]),
            )
        )

        class WrongRootMatcher:
            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=20, resolution_kind="external_mapping")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return 20

        lookups: list[bool] = []

        def fake_lookup_fribb(item: dict, allow_root_fallback: bool = True) -> dict | None:
            lookups.append(allow_root_fallback)
            if allow_root_fallback:
                return {"themoviedb": "20"}
            return {"themoviedb": "65930"}

        service._matcher = WrongRootMatcher()
        service._lookup_fribb_entry = fake_lookup_fribb  # type: ignore[method-assign]

        result = service._resolve_match({
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "97938",
            "root_anilist_id": "20",
            "mal_id": "34566",
            "root_mal_id": "1735",
            "ids": {
                "anilist": "97938",
                "root_anilist": "20",
                "mal": "34566",
                "root_mal": "1735",
            },
        })

        self.assertEqual(result.tmdb_id, 65930)
        self.assertEqual(result.resolution_kind, "fribb_exact")
        self.assertEqual(result.match_confidence, "exact")
        self.assertEqual(result.anime_mapping_source, "fribb_exact")
        self.assertEqual(lookups, [False])

    def test_sync_list_tracks_unresolved_reasons_and_phase_metrics(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"shows": ["watching"], "movies": [], "anime": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["shows"]),
            )
        )
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        class DiagnosticMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 2, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                if item["title"] == "Resolvable":
                    return MatchResult(tmdb_id=111, resolution_kind="external_mapping")
                return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="missing_ids")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return self.resolve_match(item).tmdb_id

        service._matcher = DiagnosticMatcher()

        stats = service._sync_list(
            [
                {"title": "Resolvable", "media_type": "tv"},
                {"title": "Missing", "media_type": "tv"},
            ],
            "Watching - Series",
            "Auto-synced testing list",
        )

        self.assertEqual(stats.match_breakdown["external_mapping"], 1)
        self.assertEqual(stats.unresolved_reason_counts["missing_ids"], 1)
        self.assertEqual(stats.unresolved_items[0]["unresolved_reason"], "missing_ids")
        self.assertIn("resolve_seconds", stats.phase_timings)
        self.assertIn("list_write_successes", stats.pmdb_metrics)

    def test_unresolved_anime_items_capture_resolution_diagnostics(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"anime": ["watching"], "shows": [], "movies": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["anime"]),
            )
        )
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        class DiagnosticAnimeMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 1, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="lookup_unavailable")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return None

        service._matcher = DiagnosticAnimeMatcher()

        stats = service._sync_list(
            [{
                "title": "Unresolved Anime",
                "year": 2026,
                "media_type": "tv",
                "simkl_type": "anime",
                "anilist_id": "123",
                "root_anilist_id": "99",
                "root_episode_offset": 12,
                "ids": {"anilist": "123", "root_anilist": "99"},
            }],
            "Watching - Anime",
            "Auto-synced testing anime list",
        )

        self.assertEqual(stats.unresolved_reason_counts["lookup_unavailable"], 1)
        self.assertEqual(len(stats.unresolved_items), 1)
        unresolved = stats.unresolved_items[0]
        self.assertEqual(unresolved["unresolved_reason"], "lookup_unavailable")
        self.assertEqual(unresolved["root_episode_offset"], 12)
        self.assertTrue(unresolved["has_root_ids"])
        self.assertTrue(unresolved["has_anime_ids"])

    def test_sync_list_uses_row_fingerprint_to_skip_unchanged_rows(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"shows": ["watching"], "movies": [], "anime": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["shows"]),
            )
        )
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        class FingerprintMatcher:
            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=222, resolution_kind="external_mapping")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return 222

            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 1, "cache_hits": 0, "failed_cache_hits": 0}

        service._matcher = FingerprintMatcher()
        items = [{"title": "Fingerprint Show", "media_type": "tv", "ids": {"simkl": 1}}]

        first = service._sync_list(items, "Watching - Series", "Demo", source_name="SIMKL", selection={"source": "simkl", "media_type": "shows", "status": "watching"})
        self.assertEqual(first.items_skipped_fingerprint, 0)
        self.assertEqual(pmdb.list_item_reads, 1)

        second = service._sync_list(items, "Watching - Series", "Demo", source_name="SIMKL", selection={"source": "simkl", "media_type": "shows", "status": "watching"})
        self.assertEqual(second.items_skipped_fingerprint, 1)
        self.assertEqual(second.match_breakdown["skipped:fingerprint"], 1)
        self.assertEqual(pmdb.list_item_reads, 1)
        self.assertEqual(first.row_key, second.row_key)
        self.assertEqual(second.row_type, "status_list")

    def test_can_cancel_before_sync_work_starts(self) -> None:
        service, _ = self.build_service(delete_disabled_lists=False)
        service._cancel_requested_callback = lambda: True

        with self.assertRaises(SyncCancelled):
            service.run()

    def test_uses_expected_public_private_visibility_per_source_group(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={
                    "shows": ["watching"],
                    "movies": [],
                    "anime": [],
                },
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                simkl_visibility="private",
                trakt_personal_visibility="private",
                trakt_public_visibility="public",
                mdblist_visibility="public",
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True
        config.trakt.sync_watchlist = True
        config.trakt.sync_liked_lists = False
        config.trakt.selected_lists = [
            {"name": "Recommended Movies", "user": "me", "slug": "recommended-movies", "source": "default", "catalog_key": "recommended-movies"},
            {"name": "My TV Picks", "user": "me", "slug": "my-tv-picks", "source": "personal"},
            {"name": "Public Liked", "user": "demo", "slug": "public-liked", "source": "liked"},
            {"name": "Discover Picks", "user": "demo", "slug": "discover-picks", "source": "discover"},
        ]
        config.mdblist.api_key = "mdbl-key"
        config.mdblist.enabled = True
        config.mdblist.selected_lists = [
            {"id": 7, "name": "Popular Netflix Movies", "mediatype": "movie"},
        ]

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = StubSimklClient()
        service._trakt = StubTraktClient()
        service._mdblist = StubMdbListClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        service.run()

        visibility_by_name = {item["name"]: item["is_public"] for item in pmdb.created_lists}
        self.assertFalse(visibility_by_name["Watching - Series"])
        self.assertFalse(visibility_by_name["Watchlist - Movies"])
        self.assertFalse(visibility_by_name["Watchlist - Series"])
        self.assertFalse(visibility_by_name["Recommended Movies"])
        self.assertFalse(visibility_by_name["My TV Picks"])
        self.assertTrue(visibility_by_name["Public Liked"])
        self.assertTrue(visibility_by_name["Discover Picks"])
        self.assertTrue(visibility_by_name["Popular Netflix Movies"])

    def test_same_display_name_from_different_sources_gets_separate_pmdb_lists(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["movies"],
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True
        config.trakt.selected_lists = [{
            "name": "Trending Movies",
            "user": "default",
            "slug": "trending-movies",
            "source": "default",
            "catalog_key": "trending-movies",
        }]
        config.mdblist.api_key = "mdbl-key"
        config.mdblist.enabled = True
        config.mdblist.selected_lists = [{
            "id": 22,
            "name": "Trending Movies",
            "mediatype": "movie",
        }]

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._trakt = StubTraktClient()
        service._mdblist = StubMdbListClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        self.assertEqual(len([row for row in results if row.display_name == "Trending Movies"]), 2)
        created_names = sorted(item["name"] for item in pmdb.created_lists)
        self.assertIn("Watchlist - Movies", created_names)
        trending_names = [n for n in created_names if n.startswith("Trending Movies")]
        self.assertEqual(len(trending_names), 2)
        self.assertTrue(any("(" in n for n in trending_names), f"Expected one disambiguated name, got {trending_names}")

    def test_duplicate_source_rows_only_add_one_pmdb_item(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={
                    "shows": ["watching"],
                    "movies": [],
                    "anime": [],
                },
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows"],
            ),
        )

        class DuplicateSimklClient(StubSimklClient):
            def get_status(self, status_key: str, media_types: list[str]) -> dict[str, list[dict]]:
                if status_key == "watching" and media_types == ["shows"]:
                    return {
                        "shows": [
                            {"title": "Duplicate Show", "media_type": "tv"},
                            {"title": "Duplicate Show", "media_type": "tv"},
                            {"title": "Duplicate Show", "media_type": "tv"},
                        ],
                    }
                return {}

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = DuplicateSimklClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        self.assertEqual(len(pmdb.added_items), 1)
        self.assertEqual(results[0].items_added, 1)
        self.assertEqual(results[0].items_skipped_duplicate, 2)

    def test_syncs_trakt_watched_history_and_resume_when_enabled(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
                trakt_sync_resume_progress=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config, sync_modes={"lists": False, "history": True, "resume": True})
        pmdb = StubPMDBClient()
        service._trakt = StubTraktClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        resume_stats = next(item for item in results if item.display_name == "Resume Progress")

        self.assertEqual(watched_stats.items_fetched, 2)
        self.assertEqual(watched_stats.items_added, 2)
        # 904 at 83.3% is submitted as watched (not as a resume point).
        self.assertEqual(len(pmdb.watched), 3)
        self.assertEqual(resume_stats.items_fetched, 2)
        # 904 at 83.3% (≥80%) → watched; 903 at 50% → resume batch.
        self.assertEqual(resume_stats.items_added, 2)
        self.assertEqual(resume_stats.items_removed, 0)
        self.assertEqual(len(pmdb.resume_batches), 1)
        self.assertEqual(pmdb.resume_batches[0], [{
            "tmdb_id": 903,
            "media_type": "movie",
            "position_ms": 1_800_000,
            "runtime_ms": 3_600_000,
        }])
        self.assertEqual(watched_stats.history_cursor, "2026-04-01T13:00:00Z")

    def test_trakt_resume_progress_marks_ge80_percent_as_watched(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_resume_progress=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config, sync_modes={"lists": False, "resume": True})
        pmdb = StubPMDBClient()
        service._trakt = StubTraktClient(playback_progress=[
            {"tmdb_id": 904, "media_type": "tv", "season": 2, "episode": 5, "position_ms": 3_000_000, "runtime_ms": 3_600_000, "progress": 83.3, "title": "Nearly Done Episode"},
            {"tmdb_id": 905, "media_type": "tv", "season": 1, "episode": 1, "position_ms": 3_600_000, "runtime_ms": 3_600_000, "progress": 100, "title": "Finished Episode"},
        ])
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        resume_stats = next(item for item in results if item.display_name == "Resume Progress")

        self.assertEqual(resume_stats.items_fetched, 2)
        self.assertEqual(resume_stats.items_added, 2)
        self.assertEqual(resume_stats.items_removed, 0)
        self.assertEqual(resume_stats.items_skipped_duplicate, 0)
        # Both items are ≥80% — neither goes to the resume endpoint.
        self.assertEqual(pmdb.resume_batches, [])
        self.assertEqual(pmdb.watched, [
            {"tmdb_id": 904, "media_type": "tv", "season": 2, "episode": 5, "watched_at": None},
            {"tmdb_id": 905, "media_type": "tv", "season": 1, "episode": 1, "watched_at": None},
        ])

    def test_trakt_resume_progress_skips_existing_resume_and_completed_duplicates(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_resume_progress=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config, sync_modes={"lists": False, "resume": True})
        pmdb = StubPMDBClient()
        pmdb.resume_points = [{
            "tmdb_id": 903,
            "media_type": "movie",
            "position_ms": 1_800_000,
            "runtime_ms": 3_600_000,
        }]
        pmdb.watched = [{
            "tmdb_id": 904,
            "media_type": "tv",
            "season": 2,
            "episode": 5,
            "watched_at": "2026-04-01T13:00:00Z",
        }]
        service._trakt = StubTraktClient(playback_progress=[
            {"tmdb_id": 903, "media_type": "movie", "position_ms": 1_800_000, "runtime_ms": 3_600_000, "progress": 50, "title": "Resume Movie"},
            {"tmdb_id": 904, "media_type": "tv", "season": 2, "episode": 5, "position_ms": 3_600_000, "runtime_ms": 3_600_000, "progress": 100, "title": "Finished Episode"},
        ])
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        resume_stats = next(item for item in results if item.display_name == "Resume Progress")

        self.assertEqual(resume_stats.items_fetched, 2)
        self.assertEqual(resume_stats.items_added, 0)
        self.assertEqual(resume_stats.items_removed, 0)
        self.assertEqual(resume_stats.items_skipped_duplicate, 2)
        self.assertEqual(pmdb.resume_batches, [])
        self.assertEqual(pmdb.watched, [{
            "tmdb_id": 904,
            "media_type": "tv",
            "season": 2,
            "episode": 5,
            "watched_at": "2026-04-01T13:00:00Z",
        }])

    def test_syncs_simkl_watched_history_when_enabled(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                simkl_sync_watched_history=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = StubSimklClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_fetched, 2)
        self.assertEqual(watched_stats.items_added, 2)
        self.assertEqual(watched_stats.source_name, "SIMKL")
        self.assertEqual(watched_stats.history_cursor, "2026-04-01T13:00:00Z")

    def test_simkl_history_anime_only_filters_non_anime_entries(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies", "anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = StubSimklClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_fetched, 1)
        self.assertEqual(watched_stats.items_added, 1)
        self.assertEqual(len(pmdb.watched), 1)
        self.assertEqual(pmdb.watched[0]["tmdb_id"], 802)

    def test_simkl_history_expands_aggregate_anime_after_match_resolution(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        class AggregateAnimeSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [{
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "title": "Aggregate Anime",
                    "anilist_id": "9999821",
                    "ids": {"anilist": 9999821},
                    "watched_at": "2026-04-01T13:00:00Z",
                    "aggregate_watched_count": 3,
                }]

            def expand_aggregate_history_item(self, item: dict) -> list[dict]:
                if item.get("tmdb_id") != 814:
                    return []
                return [
                    {**item, "season": 1, "episode": 1},
                    {**item, "season": 1, "episode": 2},
                    {**item, "season": 2, "episode": 1},
                ]

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = AggregateAnimeSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_fetched, 3)
        self.assertEqual(watched_stats.items_added, 3)
        self.assertEqual(
            [(item["season"], item["episode"]) for item in pmdb.watched],
            [(1, 1), (1, 2), (2, 1)],
        )

    def test_simkl_history_skips_unsafe_aggregate_anime_mapping(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        class UnsafeAggregateAnimeSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [{
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "title": "Unsafe Aggregate Anime",
                    "anilist_id": "555",
                    "ids": {"anilist": 555},
                    "watched_at": "2026-04-01T13:00:00Z",
                    "aggregate_watched_count": 38,
                }]

            def expand_aggregate_history_item(self, item: dict) -> list[dict]:
                return []

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = UnsafeAggregateAnimeSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_fetched, 0)
        self.assertEqual(watched_stats.items_added, 0)
        self.assertEqual(pmdb.watched, [])

    def test_simkl_completed_anime_fallback_accepts_media_typed_tmdb_id(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        class CompletedAnimeSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return []

            def get_status(self, status_key: str, media_types: list[str]) -> dict[str, list[dict]]:
                if status_key == "completed":
                    return {"anime": [{
                        "media_type": "tv",
                        "simkl_type": "anime",
                        "title": "Completed Anime",
                        "tmdb_id": {"tv": 63926},
                        "status": "completed",
                        "last_watched_at": "2026-04-01T13:00:00Z",
                    }]}
                return super().get_status(status_key, media_types)

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = CompletedAnimeSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(watched_stats.items_added, 1)
        self.assertEqual(pmdb.watched[0]["tmdb_id"], 63926)
        self.assertEqual(pmdb.watched[0]["season"], 1)

    def test_simkl_completed_anime_fallback_skips_without_completed_evidence(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        class AmbiguousAnimeSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return []

            def get_status(self, status_key: str, media_types: list[str]) -> dict[str, list[dict]]:
                if status_key == "completed":
                    return {"anime": [{
                        "media_type": "tv",
                        "simkl_type": "anime",
                        "title": "Ambiguous Anime",
                        "tmdb_id": 63926,
                    }]}
                return super().get_status(status_key, media_types)

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = AmbiguousAnimeSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(watched_stats.items_added, 0)
        self.assertEqual(pmdb.watched, [])

    def test_list_write_preserves_source_order_after_parallel_resolution(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token"),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["movies"]),
        )

        class SlowFirstMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 0, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                if item["title"] == "First":
                    time.sleep(0.02)
                    return MatchResult(tmdb_id=111, resolution_kind="direct_tmdb")
                return MatchResult(tmdb_id=222, resolution_kind="direct_tmdb")

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._pmdb = pmdb
        service._matcher = SlowFirstMatcher()

        service._sync_list(
            [
                {"title": "First", "media_type": "movie"},
                {"title": "Second", "media_type": "movie"},
            ],
            "Order Test",
            "Order Test",
            source_name="SIMKL",
        )

        self.assertEqual([item["tmdb_id"] for item in pmdb.added_items], [111, 222])

    def test_simkl_history_remaps_sequel_anime_into_single_root_season(self) -> None:
        class OffsetAnimeSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [
                    {
                        "tmdb_id": 9501,
                        "media_type": "tv",
                        "simkl_type": "anime",
                        "season": 1,
                        "episode": 1,
                        "watched_at": "2026-04-01T13:00:00Z",
                        "title": "Hell's Paradise 2",
                        "root_episode_offset": 13,
                    },
                    {
                        "tmdb_id": 9501,
                        "media_type": "tv",
                        "simkl_type": "anime",
                        "season": 1,
                        "episode": 12,
                        "watched_at": "2026-04-01T13:00:00Z",
                        "title": "Hell's Paradise 2",
                        "root_episode_offset": 13,
                    },
                ]

        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = OffsetAnimeSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        service.run()

        self.assertEqual(
            [(item["season"], item["episode"]) for item in pmdb.watched],
            [(1, 14), (1, 25)],
        )

    def test_simkl_history_does_not_collapse_sequel_into_season_one_when_future_pmdb_season_is_missing(self) -> None:
        class MissingFutureSeasonAnimeSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [{
                    "tmdb_id": 9601,
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "season": 2,
                    "episode": 3,
                    "watched_at": "2026-04-01T13:00:00Z",
                    "title": "Future Season Anime 2",
                    "root_episode_offset": 12,
                    "anilist_id": "9999801",
                    "root_anilist_id": "9999802",
                    "ids": {"anilist": "9999801", "root_anilist": "9999802"},
                }]

        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.anime_seasons_by_tmdb[9601] = [
            {"season_number": 1, "episode_count": 12, "tmdb_season": 1, "tmdb_episode_start": 1},
            {"season_number": 2, "episode_count": 12, "tmdb_season": 2, "tmdb_episode_start": 1},
        ]
        service._simkl = MissingFutureSeasonAnimeSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        # With no Fribb data for these fake IDs, the remap chain uses PMDB
        # anime-seasons. The item must not be collapsed into season 1.
        if pmdb.watched:
            for entry in pmdb.watched:
                self.assertNotEqual(
                    (entry.get("season"), entry.get("episode")),
                    (1, 3),
                    "Sequel episode was incorrectly collapsed into season 1",
                )

    def test_simkl_history_allows_single_season_overflow_when_no_multi_season_evidence_exists(self) -> None:
        class SingleSeasonOverflowSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [{
                    "tmdb_id": 9701,
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "season": 1,
                    "episode": 15,
                    "watched_at": "2026-04-01T13:00:00Z",
                    "title": "Single Season Overflow Anime",
                }]

        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.anime_seasons_by_tmdb[9701] = [
            {"season_number": 1, "episode_count": 12, "tmdb_season": 1, "tmdb_episode_start": 1},
        ]
        service._simkl = SingleSeasonOverflowSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(watched_stats.items_added, 1)
        self.assertEqual([(item["season"], item["episode"]) for item in pmdb.watched], [(1, 15)])

    def test_history_sync_uses_saved_cursors_for_incremental_fetches(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                simkl_sync_watched_history=True,
                trakt_sync_watched_history=True,
                simkl_history_cursor="2026-04-01T00:00:00Z",
                trakt_history_cursor="2026-04-02T00:00:00Z",
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config)
        pmdb = StubPMDBClient()
        simkl = StubSimklClient()
        trakt = StubTraktClient()
        service._simkl = simkl
        service._trakt = trakt
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        service.run()

        self.assertEqual(simkl.last_history_since, "2026-04-01T00:00:00Z")
        self.assertEqual(trakt.last_history_since, "2026-04-02T00:00:00Z")

    def test_simkl_activity_resolves_show_and_anime_without_direct_tmdb_ids(self) -> None:
        class NoTmdbSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [
                    {"media_type": "tv", "season": 1, "episode": 2, "title": "Fallback Show Episode", "tvdb_id": "991"},
                    {"media_type": "tv", "season": 1, "episode": 3, "title": "Fallback Anime Episode", "anilist_id": "992"},
                ]

        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "anime"],
                simkl_sync_watched_history=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = NoTmdbSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_added, 2)
        self.assertEqual(
            {(item["tmdb_id"], item["season"], item["episode"]) for item in pmdb.watched},
            {(811, 1, 2), (812, 1, 3)},
        )

    def test_simkl_anime_history_re_resolves_wrong_direct_tmdb_id(self) -> None:
        class WrongAnimeTmdbSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [{
                    "tmdb_id": 111111,
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "season": 1,
                    "episode": 1,
                    "title": "Oshi no Ko",
                    "anilist_id": "150672",
                    "mal_id": "52034",
                    "ids": {"anilist": "150672", "mal": "52034"},
                }]

        class CorrectingMatcher:
            def resolve_tmdb_id(self, item: dict) -> int | None:
                if item.get("title") == "Oshi no Ko":
                    return 203737
                return item.get("tmdb_id")

        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = WrongAnimeTmdbSimklClient()
        service._matcher = CorrectingMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(watched_stats.items_added, 1)
        self.assertEqual(pmdb.watched[0]["tmdb_id"], 203737)

    def test_simkl_aggregate_anime_uses_root_offset_mapping_for_later_seasons(self) -> None:
        class OffsetAggregateAnimeSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [{
                    "tmdb_id": 9501,
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "title": "Seasonal Anime Part 2",
                    "aggregate_watched_count": 3,
                    "root_episode_offset": 12,
                    "anilist_id": "9999",
                    "ids": {"anilist": "9999"},
                    "watched_at": "2026-04-01T13:00:00Z",
                }]

            def expand_aggregate_history_item(self, item: dict) -> list[dict]:
                return []

        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.anime_seasons_by_tmdb[9501] = [
            {"season_number": 1, "episode_count": 12, "tmdb_season": 1, "tmdb_episode_start": 1},
            {"season_number": 2, "episode_count": 12, "tmdb_season": 2, "tmdb_episode_start": 1},
        ]
        service._simkl = OffsetAggregateAnimeSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(watched_stats.items_added, 3)
        self.assertEqual(
            [(item["season"], item["episode"]) for item in pmdb.watched],
            [(2, 1), (2, 2), (2, 3)],
        )
        self.assertEqual(pmdb.anime_seasons_calls, [9501])

    def test_anime_seasons_cache_is_reused_across_sync_service_instances(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
            ),
        )

        pmdb = StubPMDBClient()
        pmdb.anime_seasons_by_tmdb[9501] = [
            {"season_number": 1, "episode_count": 12, "tmdb_season": 1, "tmdb_episode_start": 1},
        ]

        service_one = SyncService(config)
        service_one._pmdb = pmdb
        service_two = SyncService(config)
        service_two._pmdb = pmdb

        self.assertEqual(service_one._get_cached_anime_seasons(9501), pmdb.anime_seasons_by_tmdb[9501])
        self.assertEqual(service_two._get_cached_anime_seasons(9501), pmdb.anime_seasons_by_tmdb[9501])
        self.assertEqual(pmdb.anime_seasons_calls, [9501])

    def test_simkl_history_skips_zero_episode_placeholder_target_seasons(self) -> None:
        class FrierenStyleSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [{
                    "tmdb_id": 209867,
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "season": 1,
                    "episode": 30,
                    "watched_at": "2026-04-01T13:00:00Z",
                    "title": "Frieren: Beyond Journey's End",
                    "root_episode_offset": 0,
                    "anilist_id": "154587",
                    "ids": {"anilist": "154587"},
                }]

        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
                simkl_history_anime_only=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.anime_seasons_by_tmdb[209867] = [
            {"season_number": 1, "episode_count": 28, "tmdb_season": 1, "tmdb_episode_start": 1},
            {"season_number": 2, "episode_count": 0, "tmdb_season": 2, "tmdb_episode_start": 1},
        ]
        service._simkl = FrierenStyleSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(watched_stats.items_added, 0)
        self.assertEqual(pmdb.watched, [])

    def test_write_watched_history_items_dedupes_duplicate_cross_season_payloads(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"anime": [], "shows": [], "movies": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["anime"]),
            )
        )
        pmdb = StubPMDBClient()
        service._pmdb = pmdb
        stats = SyncStats(display_name="Watch History", source_name="SIMKL")
        existing_counts = {}

        service._write_watched_history_items(
            [
                {"tmdb_id": 20, "media_type": "tv", "season": 1, "episode": 1, "title": "Naruto", "watched_at": "2026-04-01T00:00:00Z"},
                {"tmdb_id": 20, "media_type": "tv", "season": 1, "episode": 1, "title": "Naruto", "watched_at": "2026-04-01T00:00:00Z"},
            ],
            existing_counts,
            stats,
            "Writing test history",
        )

        self.assertEqual(len(pmdb.watched), 1)
        self.assertEqual(stats.items_added, 1)
        self.assertEqual(stats.items_skipped_duplicate, 1)

    def test_write_watched_history_items_recounts_server_side_deduped_writes(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"anime": [], "shows": [], "movies": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["anime", "shows"]),
            )
        )

        class ServerDedupePMDBClient(StubPMDBClient):
            def mark_watched(
                self,
                tmdb_id: int,
                media_type: str,
                season: int | None = None,
                episode: int | None = None,
                watched_at: str | None = None,
                dedupe: bool = False,
            ) -> dict:
                existing_key = (
                    int(tmdb_id),
                    str(media_type),
                    int(season) if season is not None else None,
                    None,
                )
                for item in self.watched:
                    item_key = (
                        int(item.get("tmdb_id") or 0),
                        str(item.get("media_type") or ""),
                        int(item.get("season")) if item.get("season") is not None else None,
                        None,
                    )
                    if item_key == existing_key:
                        return {"success": True, "deduped": True}
                item = {
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "watched_at": watched_at,
                }
                if season is not None:
                    item["season"] = season
                if episode is not None:
                    item["episode"] = episode
                self.watched.append(item)
                return {"success": True, "item": item}

        pmdb = ServerDedupePMDBClient()
        service._pmdb = pmdb
        stats = SyncStats(display_name="Watch History", source_name="Trakt")
        existing_counts = {}

        service._write_watched_history_items(
            [
                {"tmdb_id": 50, "media_type": "tv", "season": 1, "episode": 1, "title": "Demo Episode 1", "watched_at": "2026-04-01T00:00:00Z"},
                {"tmdb_id": 50, "media_type": "tv", "season": 1, "episode": 2, "title": "Demo Episode 2", "watched_at": "2026-04-02T00:00:00Z"},
            ],
            existing_counts,
            stats,
            "Writing test history",
        )

        self.assertEqual(stats.items_added, 1)
        self.assertEqual(stats.items_skipped_duplicate, 1)
        self.assertEqual(len(pmdb.watched), 1)

    def test_write_watched_history_items_retries_pmdb_visibility_before_downgrading_added_count(self) -> None:
        service = SyncService(
            AppConfig(
                simkl=SimklConfig(client_id="simkl-client", access_token="simkl-token", selected_statuses={"anime": [], "shows": [], "movies": []}),
                pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
                sync=SyncConfig(remove_missing=False, delete_disabled_lists=False, dry_run=False, media_types=["movies"]),
            )
        )

        class DelayedVisibilityPMDBClient(StubPMDBClient):
            def __init__(self) -> None:
                super().__init__()
                self._history_reads = 0
                self._pending_visible: list[dict] = []

            def mark_watched(
                self,
                tmdb_id: int,
                media_type: str,
                season: int | None = None,
                episode: int | None = None,
                watched_at: str | None = None,
                dedupe: bool = False,
            ) -> dict:
                item = {
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "watched_at": watched_at,
                }
                self._pending_visible.append(item)
                return {"success": True, "item": item}

            def get_watched_history(self) -> list[dict]:
                self._history_reads += 1
                if self._history_reads >= 2 and self._pending_visible:
                    self.watched.extend(self._pending_visible)
                    self._pending_visible = []
                return list(self.watched)

        pmdb = DelayedVisibilityPMDBClient()
        service._pmdb = pmdb
        stats = SyncStats(display_name="Watch History", source_name="Trakt")
        existing_counts = {}

        with patch("src.sync_service.time.sleep", return_value=None) as mock_sleep:
            service._write_watched_history_items(
                [
                    {"tmdb_id": 5001, "media_type": "movie", "title": "Weapons", "watched_at": "2026-05-03T12:00:00Z"},
                    {"tmdb_id": 5002, "media_type": "movie", "title": "Send Help", "watched_at": "2026-05-03T12:05:00Z"},
                ],
                existing_counts,
                stats,
                "Writing test history",
            )

        self.assertEqual(stats.items_added, 2)
        self.assertEqual(stats.items_skipped_duplicate, 0)
        self.assertEqual(len(pmdb.watched), 2)
        self.assertGreaterEqual(pmdb._history_reads, 2)
        mock_sleep.assert_called()

    def test_trakt_watched_history_keeps_distinct_repeat_watches_with_different_timestamps(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._trakt = StubRepeatedWatchTraktClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_fetched, 2)
        self.assertEqual(watched_stats.items_added, 2)
        self.assertEqual(watched_stats.items_skipped_duplicate, 0)
        self.assertEqual(len(pmdb.watched), 2)

    @staticmethod
    def _trakt_history_config(**sync_kwargs) -> AppConfig:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
                **sync_kwargs,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True
        return config

    def test_watched_state_reconciliation_backfills_seasons_the_play_log_never_reported(self) -> None:
        """The whole point: /sync/history walks forward from a cursor, so the
        earlier seasons of a multi-season show are invisible to it forever."""
        config = self._trakt_history_config(trakt_reconcile_watched_history=True)
        service = SyncService(config)
        pmdb = StubPMDBClient()
        trakt = StubWatchedStateTraktClient()
        service._trakt = trakt
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()
        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(trakt.watched_state_calls, 1)
        self.assertEqual(
            sorted((row["season"], row["episode"]) for row in pmdb.watched),
            [(1, 1), (2, 1), (3, 1)],
        )
        # S3E1 came from the play log; S1E1 and S2E1 are the backfill.
        self.assertEqual(watched_stats.items_reconciled, 2)
        self.assertEqual(watched_stats.items_added, 3)

    def test_watched_state_reconciliation_writes_nothing_on_a_second_run(self) -> None:
        """Reconciliation is a floor, not plays — a fully imported show must
        not re-add every episode each time the pass runs."""
        config = self._trakt_history_config(trakt_reconcile_watched_history=True)
        pmdb = StubPMDBClient()

        for _ in range(2):
            service = SyncService(config)
            service._trakt = StubWatchedStateTraktClient()
            service._matcher = StubMatcher()
            service._pmdb = pmdb
            results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(len(pmdb.watched), 3)
        self.assertEqual(watched_stats.items_added, 0)
        self.assertEqual(watched_stats.items_reconciled, 0)

    def test_watched_state_is_not_read_unless_reconciliation_is_enabled(self) -> None:
        """It is a second whole-account read, so it stays off by default."""
        config = self._trakt_history_config()
        service = SyncService(config)
        trakt = StubWatchedStateTraktClient()
        service._trakt = trakt
        service._matcher = StubMatcher()
        service._pmdb = StubPMDBClient()

        service.run()

        self.assertEqual(trakt.watched_state_calls, 0)

    def test_full_history_sync_reconciles_without_the_flag(self) -> None:
        config = self._trakt_history_config(full_history_sync=True)
        service = SyncService(config)
        trakt = StubWatchedStateTraktClient()
        service._trakt = trakt
        service._matcher = StubMatcher()
        service._pmdb = StubPMDBClient()

        service.run()

        self.assertEqual(trakt.watched_state_calls, 1)

    def test_watched_state_rows_never_advance_the_history_cursor(self) -> None:
        """last_watched_at is not a play timestamp. Letting it move the cursor
        would skip every real play recorded between the two."""
        config = self._trakt_history_config(
            trakt_reconcile_watched_history=True,
            trakt_history_cursor="2026-01-01T00:00:00Z",
        )
        service = SyncService(config)
        trakt = StubWatchedStateTraktClient(watched_state=[{
            "tmdb_id": 910, "media_type": "tv", "season": 4, "episode": 1,
            "watched_at": "2099-01-01T00:00:00Z", "title": "Long Anime",
            "plays": 1, "watched_state": True, "cursor_exempt": True,
        }])
        service._trakt = trakt
        service._matcher = StubMatcher()
        service._pmdb = StubPMDBClient()

        results = service.run()
        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.history_cursor, "2026-01-01T00:00:00Z")

    def test_reconciliation_runs_even_when_the_play_log_has_nothing_new(self) -> None:
        """The cursor early-return used to skip the PMDB read entirely, which
        would make the backfill unreachable on an up-to-date profile."""
        class QuietTraktClient(StubWatchedStateTraktClient):
            def get_watched_history(self, since: str | None = None, status_callback=None) -> list[dict]:
                self.last_history_since = since
                return []

        config = self._trakt_history_config(
            trakt_reconcile_watched_history=True,
            trakt_history_cursor="2026-05-05T00:00:00Z",
        )
        service = SyncService(config)
        service._trakt = QuietTraktClient()
        service._matcher = StubMatcher()
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        results = service.run()
        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(len(pmdb.watched), 3)
        self.assertEqual(watched_stats.items_reconciled, 3)
        self.assertEqual(watched_stats.history_cursor, "2026-05-05T00:00:00Z")

    def test_a_failed_watched_state_read_does_not_lose_the_incremental_import(self) -> None:
        class BrokenWatchedStateClient(StubWatchedStateTraktClient):
            def get_watched_state(self, status_callback=None) -> list[dict]:
                self.watched_state_calls += 1
                raise RuntimeError("Trakt is down")

        config = self._trakt_history_config(trakt_reconcile_watched_history=True)
        service = SyncService(config)
        service._trakt = BrokenWatchedStateClient()
        service._matcher = StubMatcher()
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        results = service.run()
        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(len(pmdb.watched), 1)
        self.assertEqual(watched_stats.items_reconciled, 0)
        self.assertTrue(any("reconciliation failed" in error for error in watched_stats.errors))

    def test_simkl_anime_history_does_not_backfill_pmdb_external_ids(self) -> None:
        class DirectAnimeSimklClient(StubSimklClient):
            def get_watched_history(self, since: str | None = None) -> list[dict]:
                self.last_history_since = since
                return [{
                    "tmdb_id": 209867,
                    "media_type": "tv",
                    "simkl_type": "anime",
                    "season": 1,
                    "episode": 1,
                    "title": "Frieren: Beyond Journey's End",
                    "anilist_id": "154587",
                    "mal_id": "59978",
                    "imdb_id": "tt22248376",
                    "root_anilist_id": "154587",
                    "root_mal_id": "59978",
                    "ids": {
                        "anilist": "154587",
                        "mal": "59978",
                        "imdb": "tt22248376",
                        "root_anilist": "154587",
                        "root_mal": "59978",
                    },
                }]

        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["anime"],
                simkl_sync_watched_history=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = DirectAnimeSimklClient()
        service._matcher = StubActivityMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_added, 1)
        self.assertEqual(pmdb.created_mappings, [])

    def test_history_only_mode_does_not_run_list_syncs(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={
                    "shows": ["watching"],
                    "movies": [],
                    "anime": [],
                },
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=True,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True
        config.trakt.sync_watchlist = True

        service = SyncService(config, sync_modes={"lists": False, "history": True, "resume": False})
        pmdb = StubPMDBClient()
        service._simkl = StubSimklClient()
        service._trakt = StubTraktClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        self.assertEqual([item.display_name for item in results], ["Watch History"])
        self.assertEqual(results[0].source_name, "Trakt")
        self.assertEqual(pmdb.created_lists, [])
        self.assertEqual(pmdb.deleted_lists, [])
        self.assertEqual(len(pmdb.watched), 2)

    def test_pmdb_native_watchlist_reconciles_removed_and_added_items(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["movies"],
            ),
        )
        config.simkl.access_token = "simkl-token"
        config.sync.simkl_sync_to_pmdb_watchlist = True

        service = SyncService(config, sync_modes={"lists": True, "history": False, "resume": False})
        pmdb = StubPMDBClient()
        pmdb.list_items_by_id["pmdb-active"] = [
            {"id": "keep-item", "tmdb_id": 101, "media_type": "movie"},
            {"id": "remove-item", "tmdb_id": 999, "media_type": "movie"},
        ]
        service._pmdb = pmdb

        class WatchlistSimklClient(StubSimklClient):
            def get_status(self, status_key: str, media_types: list[str]) -> dict[str, list[dict]]:
                if status_key == "plantowatch":
                    return {
                        "movies": [
                            {"title": "Keep", "media_type": "movie", "tmdb_id": 101},
                            {"title": "Add", "media_type": "movie", "tmdb_id": 202},
                        ]
                    }
                return {media_type: [] for media_type in media_types}

        class DirectMatcher:
            def stats_snapshot(self) -> dict[str, int]:
                return {"lookups": 2, "cache_hits": 0, "failed_cache_hits": 0}

            def resolve_match(self, item: dict) -> MatchResult:
                return MatchResult(tmdb_id=int(item["tmdb_id"]), resolution_kind="direct_tmdb")

            def resolve_tmdb_id(self, item: dict) -> int | None:
                return int(item["tmdb_id"])

        service._simkl = WatchlistSimklClient()
        service._matcher = DirectMatcher()

        results = service.run()

        watchlist_stats = next(item for item in results if item.display_name == "PMDB Watchlist")
        self.assertEqual(watchlist_stats.items_added, 1)
        self.assertEqual(watchlist_stats.items_removed, 1)
        self.assertEqual(pmdb.added_items[-1]["tmdb_id"], 202)
        self.assertEqual(pmdb.removed_list_items, [("pmdb-active", "remove-item")])

    def test_pmdb_native_watchlist_clears_when_sources_return_no_items(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["movies"],
            ),
        )
        config.simkl.access_token = "simkl-token"
        config.sync.simkl_sync_to_pmdb_watchlist = True

        service = SyncService(config, sync_modes={"lists": True, "history": False, "resume": False})
        pmdb = StubPMDBClient()
        pmdb.list_items_by_id["pmdb-active"] = [
            {"id": "stale-1", "tmdb_id": 777, "media_type": "movie"},
            {"id": "stale-2", "tmdb_id": 778, "media_type": "movie"},
        ]
        service._pmdb = pmdb

        class EmptyWatchlistSimklClient(StubSimklClient):
            def get_status(self, status_key: str, media_types: list[str]) -> dict[str, list[dict]]:
                return {media_type: [] for media_type in media_types}

        service._simkl = EmptyWatchlistSimklClient()
        service._matcher = StubMatcher()

        results = service.run()

        watchlist_stats = next(item for item in results if item.display_name == "PMDB Watchlist")
        self.assertEqual(watchlist_stats.items_fetched, 0)
        self.assertEqual(watchlist_stats.items_added, 0)
        self.assertEqual(watchlist_stats.items_removed, 2)
        self.assertEqual(
            pmdb.removed_list_items,
            [("pmdb-active", "stale-1"), ("pmdb-active", "stale-2")],
        )

    def test_trakt_watched_history_skips_already_watched_title_even_with_new_timestamp(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.watched = [
            {"tmdb_id": 901, "media_type": "movie", "watched_at": "2026-03-31T12:00:00Z"},
            {"tmdb_id": 902, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-03-31T13:00:00Z"},
        ]
        service._trakt = StubTraktClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_fetched, 2)
        self.assertEqual(watched_stats.items_resolved, 2)
        self.assertEqual(watched_stats.items_added, 0)
        self.assertEqual(watched_stats.items_skipped_duplicate, 2)

    def test_trakt_watched_history_fast_skips_existing_items_before_resolution(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.watched = [
            {"tmdb_id": 901, "media_type": "movie", "watched_at": "2026-04-01T12:00:00Z"},
            {"tmdb_id": 902, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-04-01T13:00:00Z"},
        ]
        service._trakt = StubTraktClient()
        service._pmdb = pmdb

        resolve_calls = {"count": 0}
        original_resolve_activity = service._resolve_activity_item

        def counting_resolve(item: dict) -> dict:
            resolve_calls["count"] += 1
            return original_resolve_activity(item)

        service._resolve_activity_item = counting_resolve

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(watched_stats.items_added, 0)
        self.assertEqual(watched_stats.items_skipped_duplicate, 2)
        self.assertEqual(resolve_calls["count"], 0)

    def test_trakt_watched_history_does_not_add_repeat_watches_for_same_title(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._trakt = StubTraktClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")

        self.assertEqual(watched_stats.items_added, 2)
        self.assertEqual(watched_stats.items_removed, 0)
        self.assertEqual(pmdb.deleted_watched, [])

    def test_simkl_watched_history_fast_skips_existing_non_anime_items(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies", "anime"],
                simkl_sync_watched_history=True,
            ),
        )

        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.watched = [
            {"tmdb_id": 801, "media_type": "movie", "watched_at": "2026-04-01T12:00:00Z"},
            {"tmdb_id": 802, "media_type": "tv", "season": 1, "episode": 3, "watched_at": "2026-04-01T13:00:00Z"},
        ]
        service._simkl = StubSimklClient()
        service._pmdb = pmdb

        resolve_calls = {"count": 0}
        original_resolve_activity = service._resolve_activity_item

        def counting_resolve(item: dict) -> dict:
            resolve_calls["count"] += 1
            return original_resolve_activity(item)

        service._resolve_activity_item = counting_resolve

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Watch History")
        self.assertEqual(watched_stats.items_added, 0)
        self.assertEqual(watched_stats.items_skipped_duplicate, 2)
        self.assertEqual(resolve_calls["count"], 1)

    def test_trakt_resume_progress_skips_unchanged_existing_points(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_resume_progress=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config, sync_modes={"lists": False, "resume": True})
        pmdb = StubPMDBClient()
        pmdb.resume_points = [
            {"tmdb_id": 903, "media_type": "movie", "position_ms": 1_800_000, "runtime_ms": 3_600_000},
            {"tmdb_id": 904, "media_type": "tv", "season": 2, "episode": 5, "position_ms": 3_000_000, "runtime_ms": 3_600_000},
        ]
        service._trakt = StubTraktClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        resume_stats = next(item for item in results if item.display_name == "Resume Progress")

        # Item at 50%  → resume point, matches existing PMDB entry → skipped duplicate.
        # Item at 83.3% (≥80%) → submitted as watched (no progress sent to PMDB).
        self.assertEqual(resume_stats.items_fetched, 2)
        self.assertEqual(resume_stats.items_resolved, 1)
        self.assertEqual(resume_stats.items_added, 1)
        self.assertEqual(resume_stats.items_removed, 0)
        self.assertEqual(resume_stats.items_skipped_duplicate, 1)
        self.assertEqual(len(pmdb.watched), 1)
        self.assertEqual(pmdb.watched[0]["tmdb_id"], 904)

    def test_trakt_resume_auth_error_is_reported_as_result_error(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=[],
                trakt_sync_resume_progress=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.access_token = "expired-token"
        config.trakt.enabled = True

        service = SyncService(config, sync_modes={"lists": False, "history": False, "resume": True})
        service._trakt = StubUnauthorizedTraktClient()
        pmdb = StubPMDBClient()
        service._pmdb = pmdb

        results = service.run()

        resume_stats = next(item for item in results if item.display_name == "Resume Progress")
        self.assertEqual(resume_stats.items_fetched, 0)
        self.assertEqual(resume_stats.errors, ["Trakt token expired, reconnect Trakt."])
        self.assertEqual(pmdb.resume_batches, [])


class RemapViaFribbTests(unittest.TestCase):
    """Fribb season/offset remapping.

    The upstream feed stores these as namespace-keyed dicts — ``season`` is
    ``{"tvdb": N, "tmdb": N}`` and ``episode_offset`` is ``{"tvdb": N, "tmdb":
    N}``. The old code read ``thetvdb_season`` / ``thetvdb_epoffset``, neither of
    which exists upstream, so this returned None for every single item and the
    whole tier was dead.
    """

    def _service(self, anime_seasons=None) -> SyncService:
        config = AppConfig(pmdb=PublicMetaDBConfig(api_key="pmdb-key"))
        service = SyncService(config)
        service._get_cached_anime_seasons = lambda tmdb_id: anime_seasons or []  # type: ignore[method-assign]
        return service

    def test_tmdb_season_is_used_directly_without_a_pmdb_lookup(self) -> None:
        calls = []
        service = self._service()

        def fail_if_called(tmdb_id):
            calls.append(tmdb_id)
            return []

        service._get_cached_anime_seasons = fail_if_called  # type: ignore[method-assign]

        result = service._remap_via_fribb(
            {"season": {"tvdb": 2, "tmdb": 3}, "episode_offset": {"tvdb": 12, "tmdb": 5}},
            item_tmdb_id=1429,
            episode=4,
        )

        self.assertEqual(result, {"season": 3, "episode": 9})  # 4 + tmdb offset 5
        self.assertEqual(calls, [], "TMDB coordinates should not need a PMDB round trip")

    def test_tvdb_season_falls_back_when_no_tmdb_pair_exists(self) -> None:
        service = self._service()
        result = service._remap_via_fribb(
            {"season": {"tvdb": 2}, "episode_offset": {"tvdb": 12}},
            item_tmdb_id=1429,
            episode=4,
        )
        self.assertEqual(result, {"season": 2, "episode": 16})  # 4 + tvdb offset 12

    def test_missing_offsets_default_to_zero(self) -> None:
        service = self._service()
        result = service._remap_via_fribb({"season": {"tmdb": 2}}, item_tmdb_id=1429, episode=7)
        self.assertEqual(result, {"season": 2, "episode": 7})

    def test_season_zero_specials_are_left_untouched(self) -> None:
        service = self._service()
        self.assertIsNone(
            service._remap_via_fribb(
                {"season": {"tvdb": 0, "tmdb": 0}, "episode_offset": {"tvdb": 2}},
                item_tmdb_id=1429,
                episode=1,
            )
        )

    def test_entry_without_season_data_is_skipped(self) -> None:
        service = self._service()
        self.assertIsNone(service._remap_via_fribb({}, item_tmdb_id=1429, episode=1))

    def test_fribb_tmdb_id_is_adopted_when_item_has_none(self) -> None:
        service = self._service()
        result = service._remap_via_fribb(
            {"season": {"tmdb": 1}, "themoviedb_id": {"tv": 46260}},
            item_tmdb_id=0,
            episode=3,
        )
        self.assertEqual(result, {"season": 1, "episode": 3, "tmdb_id": 46260})

    def test_tvdb_season_is_translated_through_pmdb_anime_seasons(self) -> None:
        service = self._service(anime_seasons=[
            {"season_number": 2, "tmdb_season": 4, "tmdb_episode_start": 10},
        ])
        result = service._remap_via_fribb(
            {"season": {"tvdb": 2}, "episode_offset": {"tvdb": 0}},
            item_tmdb_id=1429,
            episode=3,
        )
        self.assertEqual(result, {"season": 4, "episode": 12})  # start 10 + (3 - 1)


if __name__ == "__main__":
    unittest.main()
