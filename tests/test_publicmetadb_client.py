import unittest

import requests

from src.config import PublicMetaDBConfig
from src.publicmetadb_client import PublicMetaDBClient


class PublicMetaDBClientTests(unittest.TestCase):
    def test_get_watched_history_paginates_all_pages(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))
        calls: list[tuple[str, dict | None]] = []

        def fake_get(path: str, params: dict | None = None):
            calls.append((path, params))
            page = int((params or {}).get("page", 1))
            if page == 1:
                return {
                    "items": [{"id": "w1"}, {"id": "w2"}],
                    "totalPages": 2,
                }
            if page == 2:
                return {
                    "items": [{"id": "w3"}],
                    "totalPages": 2,
                }
            return {"items": [], "totalPages": 2}

        client._get = fake_get  # type: ignore[method-assign]

        items = client.get_watched_history()

        self.assertEqual([item["id"] for item in items], ["w1", "w2", "w3"])
        self.assertEqual(calls, [
            ("/api/external/watched", {"page": 1, "perPage": 100}),
            ("/api/external/watched", {"page": 2, "perPage": 100}),
        ])

    def test_clear_watched_history_deletes_all_entries_with_ids(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))
        calls: list[tuple[str, dict | None]] = []
        deleted_ids: list[str] = []

        responses = iter([
            {"items": [{"id": "w1"}, {"id": "w2"}, {"tmdb_id": 5}, {"id": "w3"}]},
            {"items": []},
        ])

        def fake_get(path: str, params: dict | None = None):
            calls.append((path, params))
            return next(responses)

        client._get = fake_get  # type: ignore[method-assign]

        def fake_delete(watched_id: str) -> bool:
            deleted_ids.append(watched_id)
            return True

        client.delete_watched_entry = fake_delete  # type: ignore[method-assign]

        deleted_count = client.clear_watched_history()

        self.assertEqual(deleted_count, 3)
        self.assertEqual(deleted_ids, ["w1", "w2", "w3"])
        self.assertEqual(calls, [
            ("/api/external/watched", {"page": 1, "perPage": 100}),
            ("/api/external/watched", {"page": 1, "perPage": 100}),
        ])

    def test_clear_watched_history_reloads_first_page_until_empty(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))
        deleted_ids: list[str] = []
        pages = [
            {"items": [{"id": "w1"}, {"id": "w2"}]},
            {"items": [{"id": "w3"}, {"id": "w4"}]},
            {"items": [{"id": "w5"}]},
            {"items": []},
        ]
        call_count = {"value": 0}

        def fake_get(path: str, params: dict | None = None):
            self.assertEqual(path, "/api/external/watched")
            self.assertEqual(params, {"page": 1, "perPage": 100})
            index = call_count["value"]
            call_count["value"] += 1
            return pages[index]

        def fake_delete(watched_id: str) -> bool:
            deleted_ids.append(watched_id)
            return True

        client._get = fake_get  # type: ignore[method-assign]
        client.delete_watched_entry = fake_delete  # type: ignore[method-assign]

        deleted_count = client.clear_watched_history()

        self.assertEqual(deleted_count, 5)
        self.assertEqual(deleted_ids, ["w1", "w2", "w3", "w4", "w5"])
        self.assertEqual(call_count["value"], 4)

    def test_lookup_prefers_highest_voted_result_over_first(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))

        def fake_get(path: str, params: dict | None = None):
            return {
                "results": [
                    {"tmdb_id": 46260, "votes": 1},   # Naruto – wrong, low votes
                    {"tmdb_id": 65930, "votes": 5},   # Boruto – correct, high votes
                ]
            }

        client._get = fake_get  # type: ignore[method-assign]

        result = client.lookup_by_external_id_detailed("anilist", "97938", "tv")

        self.assertEqual(result["tmdb_id"], 65930)
        self.assertEqual(result["status"], "hit")

    def test_lookup_falls_back_to_first_result_when_no_votes(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))

        def fake_get(path: str, params: dict | None = None):
            return {"results": [{"tmdb_id": 65930}, {"tmdb_id": 46260}]}

        client._get = fake_get  # type: ignore[method-assign]

        result = client.lookup_by_external_id_detailed("anilist", "97938", "tv")

        self.assertEqual(result["tmdb_id"], 65930)

    def test_lookup_normalizes_media_typed_tmdb_id_payload(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))

        def fake_get(path: str, params: dict | None = None):
            return {"results": [{"tmdb_id": {"tv": 63926, "movie": 123}, "votes": 1}]}

        client._get = fake_get  # type: ignore[method-assign]

        result = client.lookup_by_external_id_detailed("anilist", "12345", "tv")

        self.assertEqual(result["tmdb_id"], 63926)

    def test_lookup_by_external_id_treats_401_as_unavailable(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))

        class _Response:
            status_code = 401

        def fake_get(path: str, params: dict | None = None):
            response = _Response()
            raise requests.HTTPError("401 unauthorized", response=response)

        client._get = fake_get  # type: ignore[method-assign]

        result = client.lookup_by_external_id("imdb", "tt8336214", "tv")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
