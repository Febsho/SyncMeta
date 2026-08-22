import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import requests

from src.anilist_client import (
    AniListClient,
    _load_persistent_root_cache,
    _reset_persistent_root_cache_state,
    _save_persistent_root_cache,
)
from src.config import AniListConfig


class AniListClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        self._cache_path = Path(self._tmpdir.name) / "anilist_root_cache.json"
        self._env = mock.patch.dict("os.environ", {"ANILIST_ROOT_CACHE_FILE": str(self._cache_path)})
        self._env.start()
        _reset_persistent_root_cache_state()

    def tearDown(self) -> None:
        _reset_persistent_root_cache_state()
        self._env.stop()
        self._tmpdir.cleanup()

    def test_normalize_returns_direct_ids_without_root_walk(self) -> None:
        # Root IDs are no longer pre-populated during normalization; the matcher
        # resolves them lazily via anime_root_resolver when direct lookup fails.
        client = AniListClient(AniListConfig(username="tester"))

        normalized = client._normalize({
            "id": 177937,
            "idMal": 59027,
            "title": {"english": "SPY x FAMILY Season 3"},
            "seasonYear": 2025,
        })

        self.assertEqual(normalized["anilist_id"], "177937")
        self.assertEqual(normalized["mal_id"], "59027")
        self.assertIsNone(normalized["root_anilist_id"])
        self.assertIsNone(normalized["root_mal_id"])

    def test_normalize_single_episode_ona_as_movie(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        normalized = client._normalize({
            "id": 21783,
            "idMal": 31387,
            "title": {"english": "Star Fox Zero: The Battle Begins"},
            "seasonYear": 2016,
            "format": "ONA",
            "episodes": 1,
        })

        self.assertEqual(normalized["media_type"], "movie")

    def test_normalize_multi_episode_ona_stays_tv(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        normalized = client._normalize({
            "id": 99999,
            "idMal": 99999,
            "title": {"english": "Example Episodic ONA"},
            "seasonYear": 2026,
            "format": "ONA",
            "episodes": 6,
        })

        self.assertEqual(normalized["media_type"], "tv")

    def test_root_context_computes_episode_offset_from_prequels(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        payloads = {
            177937: {
                "Media": {
                    "id": 177937,
                    "idMal": 59027,
                    "episodes": 12,
                    "format": "TV",
                    "seasonYear": 2025,
                    "startDate": {"year": 2025, "month": 1, "day": 1},
                    "title": {"english": "SPY x FAMILY Season 3"},
                    "relations": {
                        "edges": [
                            {
                                "relationType": "PREQUEL",
                                "node": {
                                    "id": 140960,
                                    "idMal": 48675,
                                    "episodes": 25,
                                    "format": "TV",
                                    "seasonYear": 2022,
                                    "startDate": {"year": 2022, "month": 4, "day": 1},
                                    "title": {"english": "SPY x FAMILY"},
                                },
                            }
                        ]
                    },
                }
            },
            140960: {
                "Media": {
                    "id": 140960,
                    "idMal": 48675,
                    "episodes": 25,
                    "format": "TV",
                    "seasonYear": 2022,
                    "startDate": {"year": 2022, "month": 4, "day": 1},
                    "title": {"english": "SPY x FAMILY"},
                    "relations": {"edges": []},
                }
            },
        }

        client._query = lambda query, variables: payloads.get(variables["id"])

        context = client._get_root_context(177937)

        self.assertEqual(context["root"]["id"], 140960)
        self.assertEqual(context["episode_offset"], 25)

    def test_get_statuses_reuses_completed_fetch_for_filtered_statuses(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))
        calls: list[str] = []

        def fake_query(query, variables):
            calls.append(str(variables["status"]))
            return {
                "MediaListCollection": {
                    "lists": [
                        {
                            "entries": [
                                {
                                    "media": {
                                        "id": 1,
                                        "idMal": 11,
                                        "title": {"english": "Movie OVA"},
                                        "seasonYear": 2024,
                                        "format": "OVA",
                                        "episodes": 1,
                                    }
                                },
                                {
                                    "media": {
                                        "id": 2,
                                        "idMal": 22,
                                        "title": {"english": "Movie ONA"},
                                        "seasonYear": 2024,
                                        "format": "ONA",
                                        "episodes": 1,
                                    }
                                },
                            ]
                        }
                    ]
                }
            }

        client._query = fake_query

        results = client.get_statuses(["COMPLETED_OVA", "COMPLETED_ONA", "COMPLETED"])

        self.assertEqual(calls, ["COMPLETED"])
        # All items are OVA or ONA so they're excluded from the plain COMPLETED bucket
        # (which only holds formats not claimed by a synthetic sibling).
        self.assertEqual(len(results["COMPLETED"]), 0)
        self.assertEqual(len(results["COMPLETED_OVA"]), 1)
        self.assertEqual(results["COMPLETED_OVA"][0]["anilist_format"], "OVA")
        self.assertEqual(len(results["COMPLETED_ONA"]), 1)
        self.assertEqual(results["COMPLETED_ONA"][0]["anilist_format"], "ONA")

    def _progress_client(self, entries: list[dict]) -> AniListClient:
        """A client whose list query returns `entries` for COMPLETED only."""
        client = AniListClient(AniListConfig(username="tester"))

        def fake_query(query, variables):
            if str(variables.get("status")) != "COMPLETED":
                return {"MediaListCollection": {"lists": []}}
            return {"MediaListCollection": {"lists": [{"entries": entries}]}}

        client._query = fake_query
        return client

    def test_progress_becomes_one_row_per_episode(self) -> None:
        """AniList stores no plays — `progress: 3` is the only watch data there
        is, so it is derived into three episode rows sharing the entry date."""
        client = self._progress_client([{
            "id": 5,
            "progress": 3,
            "completedAt": {"year": 2025, "month": 7, "day": 14},
            "media": {"id": 1, "idMal": 11, "title": {"english": "Some Anime"},
                      "seasonYear": 2024, "format": "TV", "episodes": 12},
        }])

        rows = client.get_watched_history()

        self.assertEqual([(row["season"], row["episode"]) for row in rows], [(1, 1), (1, 2), (1, 3)])
        self.assertTrue(all(row["watched_at"] == "2025-07-14T00:00:00Z" for row in rows))
        # Both flags must ride along: the shared date is not a play time.
        self.assertTrue(all(row["cursor_exempt"] for row in rows))
        self.assertTrue(all(row["anilist_derived"] for row in rows))

    def test_progress_is_clamped_to_the_entrys_own_episode_count(self) -> None:
        client = self._progress_client([{
            "id": 5,
            "progress": 99,
            "updatedAt": 1767225600,
            "media": {"id": 1, "idMal": 11, "title": {"english": "Short Anime"},
                      "seasonYear": 2024, "format": "TV", "episodes": 4},
        }])

        rows = client.get_watched_history()

        self.assertEqual(len(rows), 4)
        # updatedAt is the fallback when the entry was never marked completed.
        self.assertEqual(rows[0]["watched_at"], "2026-01-01T00:00:00Z")

    def test_zero_progress_writes_nothing(self) -> None:
        client = self._progress_client([{
            "id": 5,
            "progress": 0,
            "completedAt": {"year": 2025, "month": 1, "day": 1},
            "media": {"id": 1, "idMal": 11, "title": {"english": "Untouched"},
                      "seasonYear": 2024, "format": "TV", "episodes": 12},
        }])

        self.assertEqual(client.get_watched_history(), [])

    def test_an_entry_with_no_date_at_all_is_skipped(self) -> None:
        """Writing it would stamp today onto a watch from years ago."""
        client = self._progress_client([{
            "id": 5,
            "progress": 2,
            "media": {"id": 1, "idMal": 11, "title": {"english": "Undated"},
                      "seasonYear": 2024, "format": "TV", "episodes": 12},
        }])

        self.assertEqual(client.get_watched_history(), [])

    def test_a_film_becomes_a_single_row_with_no_episode(self) -> None:
        client = self._progress_client([{
            "id": 5,
            "progress": 1,
            "completedAt": {"year": 2025, "month": 3, "day": 2},
            "media": {"id": 1, "idMal": 11, "title": {"english": "Anime Film"},
                      "seasonYear": 2024, "format": "MOVIE", "episodes": 1},
        }])

        rows = client.get_watched_history()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["media_type"], "movie")
        self.assertNotIn("episode", rows[0])

    def test_a_partial_anilist_date_is_kept_rather_than_dropped(self) -> None:
        client = self._progress_client([{
            "id": 5,
            "progress": 1,
            "completedAt": {"year": 2023, "month": None, "day": None},
            "media": {"id": 1, "idMal": 11, "title": {"english": "Year Only"},
                      "seasonYear": 2023, "format": "TV", "episodes": 12},
        }])

        rows = client.get_watched_history()

        self.assertEqual(rows[0]["watched_at"], "2023-01-01T00:00:00Z")

    def test_query_returns_none_on_http_error(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        class _Resp:
            status_code = 404
            headers: dict[str, str] = {}

            def raise_for_status(self) -> None:
                raise requests.HTTPError("404 Client Error: Not Found for url: https://graphql.anilist.co/")

        client._session.post = lambda *args, **kwargs: _Resp()

        result = client._query("query {}", {"id": 1})

        self.assertIsNone(result)

    def test_root_context_is_persisted_and_reloaded_after_reset(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        payloads = {
            177937: {
                "Media": {
                    "id": 177937,
                    "idMal": 59027,
                    "episodes": 12,
                    "format": "TV",
                    "seasonYear": 2025,
                    "startDate": {"year": 2025, "month": 1, "day": 1},
                    "title": {"english": "SPY x FAMILY Season 3"},
                    "relations": {
                        "edges": [
                            {
                                "relationType": "PREQUEL",
                                "node": {
                                    "id": 140960,
                                    "idMal": 48675,
                                    "episodes": 25,
                                    "format": "TV",
                                    "seasonYear": 2022,
                                    "startDate": {"year": 2022, "month": 4, "day": 1},
                                    "title": {"english": "SPY x FAMILY"},
                                },
                            }
                        ]
                    },
                }
            },
            140960: {
                "Media": {
                    "id": 140960,
                    "idMal": 48675,
                    "episodes": 25,
                    "format": "TV",
                    "seasonYear": 2022,
                    "startDate": {"year": 2022, "month": 4, "day": 1},
                    "title": {"english": "SPY x FAMILY"},
                    "relations": {"edges": []},
                }
            },
        }

        client._query = lambda query, variables: payloads.get(variables["id"])

        context = client._get_root_context(177937)
        self.assertEqual(context["root"]["id"], 140960)
        self.assertTrue(self._cache_path.exists())

        _reset_persistent_root_cache_state()
        _load_persistent_root_cache()

        reloaded_client = AniListClient(AniListConfig(username="tester"))
        reloaded_client._query = lambda query, variables: self.fail("persisted root cache should avoid refetch")
        reloaded = reloaded_client._get_root_context(177937)
        self.assertEqual(reloaded["root"]["id"], 140960)
        self.assertEqual(reloaded["episode_offset"], 25)

    def test_persistent_cache_drops_expired_entries(self) -> None:
        now = 1_800_000_000
        self._cache_path.write_text(
            """
{
  "version": 1,
  "saved_at": 1800000000,
  "ttl_seconds": 2592000,
  "entries": {
    "177937": {
      "cached_at": 1700000000,
      "context": {
        "root": {
          "id": 140960,
          "idMal": 48675,
          "title": {"english": "SPY x FAMILY"}
        },
        "episode_offset": 25
      }
    }
  }
}
            """.strip(),
            encoding="utf-8",
        )

        with mock.patch("src.anilist_client.time.time", return_value=now):
            _load_persistent_root_cache()
            client = AniListClient(AniListConfig(username="tester"))
            client._query = lambda query, variables: self.fail("expired cache entry should not survive load")
            self.assertIsNone(client._root_context_cache.get(177937))
            _save_persistent_root_cache(force=True)

        payload = self._cache_path.read_text(encoding="utf-8")
        self.assertNotIn('"177937"', payload)


if __name__ == "__main__":
    unittest.main()
