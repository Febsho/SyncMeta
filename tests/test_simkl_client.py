import unittest

from src.config import SimklConfig
from src.simkl_client import SimklClient


class RecordingSimklClient(SimklClient):
    def __init__(self) -> None:
        super().__init__(SimklConfig(client_id="client", access_token="token"))
        self.paths: list[str] = []
        self.requests: list[tuple[str, dict | None]] = []

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        self.paths.append(path)
        self.requests.append((path, params))
        if path == "/sync/all-items/tv/watching":
            return {
                "shows": [
                    {
                        "show": {
                            "title": "Demo Show",
                            "year": 2024,
                            "ids": {"tmdb": 1234},
                        },
                        "status": "watching",
                        "seasons": [
                            {
                                "number": 1,
                                "episodes": [
                                    {"number": 1, "watched_at": "2026-04-04T12:00:00Z"},
                                ],
                            }
                        ],
                    },
                    {
                        "show": {
                            "title": "Completed Leak",
                            "year": 2022,
                            "ids": {"tmdb": 9999},
                        },
                        "status": "completed",
                    },
                ]
            }
        if path == "/sync/all-items/anime/plan%20to%20watch":
            return {
                "anime": [
                    {
                        "show": {
                            "title": "Demo Anime",
                            "year": 2025,
                            "anime_type": "tv",
                            "ids": {"tmdb": 5678, "anilist": 44},
                        },
                        "status": "plantowatch",
                    }
                ]
            }
        if path == "/sync/all-items/movie/completed":
            return {
                "movies": [
                    {
                        "movie": {
                            "title": "Completed Movie",
                            "ids": {"tmdb": 7001},
                        },
                        "last_watched_at": "2026-04-01T12:00:00Z",
                    }
                ]
            }
        if path == "/sync/all-items/tv/watching":
            return {
                "shows": [
                    {
                        "show": {
                            "title": "Watching Show",
                            "ids": {"tmdb": 7004},
                        },
                        "seasons": [
                            {
                                "number": 1,
                                "episodes": [
                                    {"number": 1, "watched_at": "2026-04-04T12:00:00Z"},
                                ],
                            }
                        ],
                    }
                ]
            }
        if path == "/sync/all-items/tv/completed":
            return {
                "shows": [
                    {
                        "title": "Completed Show",
                        "ids": {"tmdb": 7002},
                        "seasons": [
                            {
                                "number": 1,
                                "episodes": [
                                    {"number": 2, "watched_at": "2026-04-02T12:00:00Z"},
                                ],
                            }
                        ],
                    }
                ]
            }
        if path == "/sync/all-items/tv/on%20hold":
            return {
                "shows": []
            }
        if path == "/sync/all-items/tv/dropped":
            return {
                "shows": []
            }
        if path == "/sync/all-items/tv/plan%20to%20watch":
            return {
                "shows": []
            }
        if path == "/sync/all-items/anime/watching":
            return {
                "anime": []
            }
        if path == "/sync/all-items/anime/completed":
            return {
                "anime": [
                    {
                        "show": {
                            "title": "Completed Anime",
                            "ids": {"tmdb": 7003},
                        },
                        "last_watched_episode": {
                            "season": 1,
                            "number": 3,
                            "watched_at": "2026-04-03T12:00:00Z",
                        },
                    },
                    {
                        "show": {
                            "title": "Count Only Anime",
                            "ids": {"tmdb": 7005},
                        },
                        "watched_episodes_count": 3,
                        "total_episodes_count": 3,
                        "last_watched_at": "2026-04-03T13:00:00Z",
                    }
                ]
            }
        if path == "/sync/all-items/anime/on%20hold":
            return {
                "anime": []
            }
        if path == "/sync/all-items/anime/dropped":
            return {
                "anime": []
            }
        if path == "/sync/all-items/anime/plan%20to%20watch":
            return {
                "anime": []
            }
        if path == "/sync/playback":
            return {
                "movies": [
                    {
                        "movie": {
                            "title": "Paused Movie",
                            "runtime": 120,
                            "ids": {"tmdb": 8001},
                        },
                        "progress": 50,
                    }
                ],
                "shows": [
                    {
                        "show": {
                            "title": "Paused Show",
                            "ids": {"tmdb": 8002},
                        },
                        "episode": {
                            "season": 2,
                            "number": 4,
                            "runtime": 45,
                        },
                        "progress": 40,
                    }
                ],
            }
        return None

    def _get_anime_root_media(self, anilist_id: int) -> dict | None:
        context = self._get_anime_root_context(anilist_id)
        if isinstance(context, dict):
            return context.get("root")
        return None

    def _get_anime_root_context(self, anilist_id: int) -> dict | None:
        if anilist_id == 177937:
            return {
                "root": {
                    "id": 140960,
                    "idMal": 48675,
                    "title": {
                        "romaji": "SPY x FAMILY",
                        "english": "SPY x FAMILY",
                    },
                },
                "episode_offset": 25,
            }
        return None

    @staticmethod
    def _fetch_tmdb_season_plan(tmdb_id: int) -> list[tuple[int, int]]:
        if tmdb_id == 7005:
            return [(1, 2), (2, 1)]
        if tmdb_id == 7006:
            return [(1, 28), (2, 0)]
        if tmdb_id == 7007:
            return [(1, 12), (2, 12)]
        return []


class SimklClientTests(unittest.TestCase):
    def test_fetch_list_uses_type_specific_endpoint(self) -> None:
        client = RecordingSimklClient()

        grouped = client.get_status("watching", ["shows"])

        self.assertEqual(client.paths, ["/sync/all-items/tv/watching"])
        self.assertEqual(len(grouped["shows"]), 1)
        self.assertEqual(grouped["shows"][0]["title"], "Demo Show")
        self.assertEqual(grouped["shows"][0]["media_type"], "tv")

    def test_fetch_list_maps_plan_to_watch_status_for_api(self) -> None:
        client = RecordingSimklClient()

        grouped = client.get_status("plantowatch", ["anime"])

        self.assertEqual(client.paths, ["/sync/all-items/anime/plan%20to%20watch"])
        self.assertEqual(grouped["anime"][0]["title"], "Demo Anime")
        self.assertEqual(grouped["anime"][0]["anilist_id"], "44")

    def test_normalize_anime_item_returns_direct_ids_without_root_walk(self) -> None:
        # Plain root-season anime keeps its direct ids; sequel/part titles get
        # separate root ids through the prefer_root_series path.
        client = RecordingSimklClient()

        normalized = client._normalize_item(
            {
                "show": {
                    "title": "Naruto",
                    "year": 2025,
                    "anime_type": "tv",
                    "ids": {"tmdb": 46260, "anilist": 20, "mal": 20},
                },
                "status": "watching",
            },
            "anime",
        )

        self.assertEqual(normalized["anilist_id"], "20")
        self.assertEqual(normalized["mal_id"], "20")
        self.assertIsNone(normalized["root_anilist_id"])
        self.assertIsNone(normalized["root_mal_id"])

    def test_normalize_seasoned_anime_item_resolves_root_even_with_tmdb(self) -> None:
        client = RecordingSimklClient()

        normalized = client._normalize_item(
            {
                "show": {
                    "title": "SPY x FAMILY Season 3",
                    "year": 2025,
                    "ids": {"tmdb": 999999, "anilist": 177937, "mal": 59027},
                },
                "status": "watching",
            },
            "anime",
        )

        self.assertEqual(normalized["tmdb_id"], "999999")
        self.assertEqual(normalized["root_anilist_id"], "140960")
        self.assertEqual(normalized["root_mal_id"], "48675")
        self.assertFalse(normalized["prefer_root_series"])

    def test_normalize_anime_movie_item_stays_movie_without_root_preference(self) -> None:
        client = RecordingSimklClient()

        normalized = client._normalize_item(
            {
                "show": {
                    "title": "Cosmic Princess Kaguya!",
                    "year": 2026,
                    "ids": {"tmdb": 1575337, "anilist": 999},
                },
                "anime_type": "movie",
                "status": "completed",
            },
            "anime",
        )

        self.assertEqual(normalized["media_type"], "movie")
        self.assertEqual(normalized["tmdb_id"], "1575337")
        self.assertFalse(normalized["prefer_root_series"])

    def test_normalize_anime_item_skips_entries_without_anime_ids(self) -> None:
        client = RecordingSimklClient()

        normalized = client._normalize_item(
            {
                "show": {
                    "title": "Soccer Aid",
                    "year": 2006,
                    "ids": {"tmdb": 872058},
                },
                "status": "completed",
            },
            "anime",
        )

        self.assertIsNone(normalized)

    def test_get_watched_history_parses_movies_shows_and_anime(self) -> None:
        client = RecordingSimklClient()

        history = client.get_watched_history()

        self.assertEqual(len(history), 7)
        self.assertTrue(any(
            item["tmdb_id"] == 7001 and item["media_type"] == "movie" and item["watched_at"] == "2026-04-01T12:00:00Z" and item["title"] == "Completed Movie"
            for item in history
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 1234 and item["media_type"] == "tv" and item["season"] == 1 and item["episode"] == 1 and item["watched_at"] == "2026-04-04T12:00:00Z" and item["title"] == "Demo Show"
            for item in history
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 7002 and item["media_type"] == "tv" and item["season"] == 1 and item["episode"] == 2 and item["watched_at"] == "2026-04-02T12:00:00Z" and item["title"] == "Completed Show"
            for item in history
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 7003 and item["media_type"] == "tv" and item["season"] == 1 and item["episode"] == 3 and item["watched_at"] == "2026-04-03T12:00:00Z" and item["title"] == "Completed Anime"
            for item in history
        ))
        self.assertEqual(
            sorted(
                (item["season"], item["episode"])
                for item in history
                if item["tmdb_id"] == 7005
            ),
            [(1, 1), (1, 2), (2, 1)],
        )

    def test_get_watched_history_since_filters_older_entries_and_passes_date_from(self) -> None:
        client = RecordingSimklClient()

        history = client.get_watched_history(since="2026-04-02T12:30:00Z")

        self.assertEqual(len(history), 5)
        self.assertTrue(all(item["watched_at"] > "2026-04-02T12:30:00Z" for item in history))
        self.assertTrue(any(
            params and params.get("date_from") == "2026-04-02T12:30:00Z"
            for _, params in client.requests
        ))
        self.assertTrue(any(
            item.get("tmdb_id") == 7005
            for item in history
        ))

    def test_get_playback_progress_parses_movie_and_episode(self) -> None:
        client = RecordingSimklClient()

        progress = client.get_playback_progress()

        self.assertEqual(len(progress), 2)
        self.assertTrue(any(
            item["tmdb_id"] == 8001 and item["media_type"] == "movie" and item["position_ms"] == 3_600_000 and item["runtime_ms"] == 7_200_000 and item["progress"] == 50.0 and item["paused_at"] is None and item["title"] == "Paused Movie"
            for item in progress
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 8002 and item["media_type"] == "tv" and item["season"] == 2 and item["episode"] == 4 and item["position_ms"] == 1_080_000 and item["runtime_ms"] == 2_700_000 and item["progress"] == 40.0 and item["paused_at"] is None and item["title"] == "Paused Show"
            for item in progress
        ))

    def test_get_playback_progress_can_fallback_to_next_up_for_tv_and_anime(self) -> None:
        class NextUpFallbackSimklClient(RecordingSimklClient):
            def _get(self, path: str, params: dict | None = None) -> dict | list | None:
                self.paths.append(path)
                self.requests.append((path, params))
                if path == "/sync/playback":
                    return []
                if path == "/sync/all-items/tv/watching":
                    return {
                        "shows": [{
                            "show": {
                                "title": "Next Up Show",
                                "year": 2026,
                                "runtime": 45,
                                "ids": {"tmdb": 9001},
                            },
                            "next_to_watch": {"season": 2, "number": 5},
                            "last_watched_at": "2026-04-05T10:00:00Z",
                        }]
                    }
                if path == "/sync/all-items/tv/on%20hold":
                    return {"shows": []}
                if path == "/sync/all-items/anime/watching":
                    return {
                        "anime": [{
                            "show": {
                                "title": "Next Up Anime",
                                "year": 2026,
                                "runtime": 24,
                                "ids": {"tmdb": 9002, "anilist": 777},
                            },
                            "next_to_watch": {"season": 1, "number": 7},
                            "last_watched_at": "2026-04-05T11:00:00Z",
                        }]
                    }
                if path == "/sync/all-items/anime/on%20hold":
                    return {"anime": []}
                return None

        client = NextUpFallbackSimklClient()

        progress = client.get_playback_progress(include_next_up_fallback=True)

        self.assertEqual(len(progress), 2)
        self.assertTrue(any(
            item["tmdb_id"] == 9001 and item["season"] == 2 and item["episode"] == 5 and item["position_ms"] == 135000 and item["runtime_ms"] == 2700000
            for item in progress
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 9002 and item["season"] == 1 and item["episode"] == 7 and item["position_ms"] == 72000 and item["runtime_ms"] == 1440000 and item["resume_fallback"] == "next_up"
            for item in progress
        ))

    def test_expand_aggregate_history_item_uses_tmdb_season_plan(self) -> None:
        client = RecordingSimklClient()

        expanded = client.expand_aggregate_history_item({
            "tmdb_id": 7005,
            "media_type": "tv",
            "simkl_type": "anime",
            "title": "Count Only Anime",
            "aggregate_watched_count": 3,
            "watched_at": "2026-04-03T13:00:00Z",
        })

        self.assertEqual(
            [(item["season"], item["episode"]) for item in expanded],
            [(1, 1), (1, 2), (2, 1)],
        )

    def test_partial_explicit_anime_history_is_supplemented_from_watched_count(self) -> None:
        client = RecordingSimklClient()

        history = client._extract_episode_history(
            {
                "show": {
                    "title": "Partial Anime",
                    "year": 2025,
                    "ids": {"tmdb": 7005, "anilist": 77},
                },
                "status": "watching",
                "seasons": [
                    {
                        "number": 1,
                        "episodes": [
                            {"number": 1, "watched_at": "2026-04-03T13:00:00Z"},
                        ],
                    }
                ],
                "watched_episodes_count": 3,
                "total_episodes_count": 3,
                "last_watched_at": "2026-04-03T13:00:00Z",
            },
            {
                "title": "Partial Anime",
                "year": 2025,
                "ids": {"tmdb": 7005, "anilist": 77},
            },
            "anime",
        )

        self.assertEqual(
            [(item["season"], item["episode"]) for item in history],
            [(1, 1), (1, 2), (2, 1)],
        )

    def test_anime_movie_history_is_normalized_as_movie(self) -> None:
        client = RecordingSimklClient()

        history = client._extract_episode_history(
            {
                "show": {
                    "title": "Cosmic Princess Kaguya!",
                    "year": 2026,
                    "ids": {"tmdb": 1575337, "anilist": 999},
                },
                "anime_type": "movie",
                "total_episodes_count": 1,
                "last_watched_at": "2026-04-03T13:00:00Z",
            },
            {
                "title": "Cosmic Princess Kaguya!",
                "year": 2026,
                "ids": {"tmdb": 1575337, "anilist": 999},
            },
            "anime",
        )

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["media_type"], "movie")
        self.assertEqual(history[0]["tmdb_id"], 1575337)
        self.assertEqual(history[0]["watched_at"], "2026-04-03T13:00:00Z")

    def test_expand_aggregate_history_item_uses_pending_later_tmdb_season(self) -> None:
        client = RecordingSimklClient()

        expanded = client.expand_aggregate_history_item({
            "tmdb_id": 7006,
            "media_type": "tv",
            "simkl_type": "anime",
            "title": "Future Season Anime",
            "aggregate_watched_count": 38,
            "watched_at": "2026-04-03T13:00:00Z",
        })

        self.assertEqual(len(expanded), 38)
        self.assertEqual(
            [(item["season"], item["episode"]) for item in expanded[27:]],
            [(1, 28), *[(2, episode) for episode in range(1, 11)]],
        )
        self.assertEqual(expanded[28]["_syncmeta_replaces_episode"], "1x29")
        self.assertEqual(expanded[-1]["_syncmeta_replaces_episode"], "1x38")

    def test_expand_aggregate_history_item_still_skips_unsafe_multi_season_gap(self) -> None:
        client = RecordingSimklClient()

        expanded = client.expand_aggregate_history_item({
            "tmdb_id": 7007,
            "media_type": "tv",
            "simkl_type": "anime",
            "title": "Unsafe Multi Season Anime",
            "aggregate_watched_count": 30,
            "watched_at": "2026-04-03T13:00:00Z",
        })

        self.assertEqual(expanded, [])

    def test_cursor_exempt_aggregate_history_survives_since_filter(self) -> None:
        client = RecordingSimklClient()

        self.assertTrue(client._is_history_after({
            "watched_at": "2026-04-01T00:00:00Z",
            "cursor_exempt": True,
        }, "2026-04-02T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
