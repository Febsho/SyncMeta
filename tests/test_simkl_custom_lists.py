import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

import requests

from src.config import SimklConfig
from src.providers import CATEGORY_COLLECTION, CATEGORY_DROPPED, CATEGORY_WATCHLIST, PmdbAdapter, SimklAdapter
from src.profile_store import ProfileStore
from src.simkl_client import SimklApiError, SimklClient


class CustomListClient(SimklClient):
    def __init__(self):
        super().__init__(SimklConfig(client_id="client", access_token="token"))
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/users/settings":
            return {"account": {"id": 42}}
        if path == "/lists/user/42":
            return {"lists": [{"id": 123, "name": "Favorites"}], "pagination": {"page": 1, "total_pages": 1}}
        if path == "/lists/123":
            return {"items": [
                {"title": "Movie", "year": 2024, "type": "movie", "ids": {"simkl_id": 1, "tmdb": "10", "imdb": "tt10"}},
                {"title": "Show", "year": 2023, "type": "tv", "ids": {"simkl_id": 2, "tmdb": "20", "tvdb": "30"}},
                {"title": "Naruto", "year": 2002, "type": "anime", "anime_type": "tv", "ids": {"simkl_id": 3, "anilist": 20, "mal": 20}},
            ], "pagination": {"page": 1, "total_pages": 1}}
        return None


class AdapterClient:
    def __init__(self, *, list_error=False):
        self.list_error = list_error
        self.list_requests = 0
        self.status_calls = []
        self.custom_calls = []

    def get_custom_lists(self):
        self.list_requests += 1
        if self.list_error:
            raise RuntimeError("custom lists unavailable")
        return [{"id": 123, "name": "Favorites"}, {"id": "456", "name": "Horror"}]

    def get_custom_list_items(self, list_id):
        self.custom_calls.append(str(list_id))
        return {
            "123": [
                {"title": "Movie", "media_type": "movie", "tmdb_id": "10"},
                {"title": "Show", "media_type": "tv", "tvdb_id": "20"},
            ],
            "456": [
                {"title": "Movie duplicate", "media_type": "movie", "tmdb_id": "10"},
                {"title": "Anime", "media_type": "tv", "anilist_id": "40", "mal_id": "50"},
            ],
        }.get(str(list_id), [])

    def get_status(self, status, media_types):
        self.status_calls.append((status, tuple(media_types)))
        return {media_types[0]: [{"title": "Plan", "media_type": "movie", "tmdb_id": "99"}]}


class SimklCustomListTests(unittest.TestCase):
    def test_v2_completion_persists_movie_status_selection(self):
        with TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            profile = store.create_profile("password", {
                "simkl": {"client_id": "v1", "access_token": "legacy",
                          "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []}},
            }, {"auto_sync": False, "media_types": ["shows", "movies", "anime"]})
            store.update_simkl_auth(
                profile["profile_id"], access_token="access", refresh_token="refresh",
                access_token_expires_at="2030-01-01T00:00:00+00:00",
                selected_statuses={"shows": ["watching"], "movies": ["plantowatch", "completed"], "anime": []},
            )
            saved = store.get_private_profile_by_id(profile["profile_id"])["credentials"]["simkl"]
            self.assertEqual(saved["auth_version"], "v2")
            self.assertEqual(saved["selected_statuses"]["movies"], ["plantowatch", "completed"])

    def test_v1_token_keeps_status_sources_but_reports_v2_requirement(self):
        class V1Client(AdapterClient):
            def get_custom_lists(self):
                raise SimklApiError("oauth2_token_required", "AUTH V2 required", 403)

        adapter = SimklAdapter(V1Client(), media_types=["movies"])
        self.assertEqual(len(adapter.list_sources()), 5)
        self.assertEqual(adapter.custom_lists_error, "SIMKL Custom Lists require reconnecting SIMKL with AUTH V2.")

    def test_premium_only_response_is_not_treated_as_an_empty_list(self):
        class PremiumClient(AdapterClient):
            def get_custom_lists(self):
                raise SimklApiError("premium_only", "premium_only", 200)

        adapter = SimklAdapter(PremiumClient(), media_types=["movies"])
        self.assertEqual(len(adapter.list_sources()), 5)
        self.assertEqual(adapter.custom_lists_error, "Custom Lists require SIMKL PRO or VIP.")

    def test_v2_refreshes_expired_access_token_and_persists_it(self):
        saved = []
        client = SimklClient(SimklConfig(
            client_id="v2-client", access_token="old", refresh_token="refresh",
            access_token_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            auth_version="v2", token_refreshed_callback=lambda *args: saved.append(args),
        ))
        client._oauth2_post = lambda path, data: {
            "access_token": "new", "refresh_token": "refresh", "expires_in": 604800,
        }
        client._ensure_v2_access_token()
        self.assertEqual(client._config.access_token, "new")
        self.assertEqual(client._session.headers["Authorization"], "Bearer new")
        self.assertEqual(saved[0][:2], ("new", "refresh"))

    def test_v2_device_flow_uses_oauth2_endpoints_and_stores_both_tokens(self):
        client = SimklClient(SimklConfig(client_id="v2-client", client_secret="confidential-secret", auth_version="v2"))
        requests_seen = []
        def oauth(path, data):
            requests_seen.append((path, data))
            if path == "/oauth2/device":
                return {"device_code": "opaque", "user_code": "ABCD-1234", "expires_in": 900}
            return {"access_token": "access", "refresh_token": "refresh", "expires_in": 604800}
        client._oauth2_post = oauth
        self.assertEqual(client.request_device_authorization()["user_code"], "ABCD-1234")
        self.assertEqual(client.poll_device_authorization("opaque")["status"], "approved")
        self.assertEqual(client._config.refresh_token, "refresh")
        self.assertEqual(requests_seen[0][1], {"client_id": "v2-client", "scope": "media:read media:write"})
        self.assertEqual(requests_seen[1][1]["grant_type"], "urn:ietf:params:oauth:grant-type:device_code")
        self.assertNotIn("client_secret", requests_seen[1][1])

    def test_client_discovers_and_normalizes_custom_list_items(self):
        client = CustomListClient()

        self.assertEqual(client.get_custom_lists(), [{"id": 123, "name": "Favorites"}])
        items = client.get_custom_list_items(123)

        self.assertEqual([item["media_type"] for item in items], ["movie", "tv", "tv"])
        self.assertEqual(items[0]["simkl_id"], "1")
        self.assertEqual(items[0]["imdb_id"], "tt10")
        self.assertEqual(items[1]["tvdb_id"], "30")
        self.assertEqual(items[2]["anilist_id"], "20")
        self.assertEqual(items[2]["mal_id"], "20")
        self.assertIn(("/lists/user/42", {"limit": 500, "page": 1}), client.calls)
        self.assertIn(("/lists/123", {"limit": 500, "page": 1}), client.calls)

    def test_custom_sources_are_read_only_and_keep_status_sources(self):
        adapter = SimklAdapter(AdapterClient(), media_types=["movies"])

        sources = adapter.list_sources()

        self.assertIn("status:plantowatch:movies", {source["key"] for source in sources})
        custom = next(source for source in sources if source["key"] == "custom:123")
        self.assertEqual(custom["label"], "Favorites")
        self.assertEqual(custom["category"], CATEGORY_WATCHLIST)
        self.assertEqual(custom["kind"], "list")
        self.assertTrue(custom["read_only"])
        self.assertFalse(adapter.supports_target_lists)
        self.assertEqual(adapter.safe_target_lists(), [])
        adapter.list_sources()
        self.assertEqual(adapter._client.list_requests, 1)

    def test_custom_list_discovery_failure_keeps_status_sources(self):
        sources = SimklAdapter(AdapterClient(list_error=True), media_types=["movies"]).list_sources()

        self.assertEqual([source["key"] for source in sources], [
            "status:watching:movies", "status:plantowatch:movies",
            "status:completed:movies", "status:hold:movies", "status:dropped:movies",
        ])

    def test_selected_custom_lists_merge_and_deduplicate_without_status_fallback(self):
        client = AdapterClient()
        adapter = SimklAdapter(client, media_types=["movies"])

        items = adapter.fetch(CATEGORY_WATCHLIST, ["custom:123", "custom:456"])

        self.assertEqual(client.custom_calls, ["123", "456"])
        self.assertEqual(client.status_calls, [])
        self.assertEqual(len(items), 3)
        self.assertEqual({item["tmdb_id"] for item in items if item.get("tmdb_id")}, {"10"})
        self.assertTrue(any(item.get("anilist_id") == "40" for item in items))

    def test_custom_selection_never_falls_back_to_a_status(self):
        client = AdapterClient()
        adapter = SimklAdapter(client, media_types=["movies"])

        self.assertEqual(adapter.fetch(CATEGORY_WATCHLIST, ["custom:missing"]), [])
        self.assertEqual(client.status_calls, [])
        self.assertEqual(adapter.fetch(CATEGORY_COLLECTION, ["custom:123"]), [])
        self.assertEqual(client.status_calls, [])

    def test_malformed_or_empty_custom_list_content_is_safe(self):
        client = CustomListClient()

        self.assertIsNone(client._normalize_custom_list_item({"title": "No type"}))
        self.assertIsNone(client._normalize_custom_list_item(None))

    def test_dropped_default_and_explicit_status_sources_cover_every_media_type(self):
        client = AdapterClient()
        adapter = SimklAdapter(client, media_types=["shows", "anime", "movies"])

        adapter.fetch(CATEGORY_DROPPED)
        self.assertEqual(client.status_calls, [
            ("dropped", ("shows",)), ("dropped", ("anime",)), ("dropped", ("movies",)),
        ])

        client.status_calls.clear()
        for key, media_type in (
            ("status:dropped:shows", "shows"),
            ("status:dropped:anime", "anime"),
            ("status:dropped:movies", "movies"),
        ):
            adapter.fetch(CATEGORY_DROPPED, [key])
            self.assertEqual(client.status_calls.pop(), ("dropped", (media_type,)))


class PmdbTargetClient:
    def __init__(self, *, missing=False):
        self.missing = missing
        self.item_reads = 0
        self.list_reads = 0

    def get_list_items(self, list_id):
        self.item_reads += 1
        if self.missing:
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("missing", response=response)
        return [{"id": "entry", "tmdb_id": 42, "media_type": "movie", "title": "Existing"}]

    def get_lists(self):
        self.list_reads += 1
        return [] if self.missing else [{"id": "existing", "name": "Existing"}]


class PmdbDeletedTargetTests(unittest.TestCase):
    def test_existing_named_target_is_read_normally(self):
        client = PmdbTargetClient()
        items = PmdbAdapter(client).fetch_target(CATEGORY_COLLECTION, "list:existing")
        self.assertEqual(items[0]["tmdb_id"], "42")
        self.assertEqual(client.list_reads, 0)

    def test_deleted_named_target_is_not_empty_and_is_not_retried(self):
        client = PmdbTargetClient(missing=True)
        adapter = PmdbAdapter(client)

        with self.assertRaisesRegex(RuntimeError, "destination list no longer exists"):
            adapter.fetch_target(CATEGORY_COLLECTION, "list:deleted")
        with self.assertRaisesRegex(RuntimeError, "destination list no longer exists"):
            adapter.fetch_target(CATEGORY_COLLECTION, "list:deleted")

        self.assertEqual(client.item_reads, 1)
        self.assertEqual(client.list_reads, 1)

    def test_intentional_pmdb_deletion_marks_exact_pair_for_destination_selection(self):
        profile = {"options": {"sync_pairs": [
            {"target": "pmdb", "target_list": "list:deleted", "enabled": True},
            {"target": "pmdb", "target_list": "list:other", "enabled": True},
        ]}}

        affected = ProfileStore._mark_pmdb_target_list_deleted_locked(profile, "deleted")

        self.assertEqual(affected, 1)
        self.assertTrue(profile["options"]["sync_pairs"][0]["destination_needs_selection"])
        self.assertFalse(profile["options"]["sync_pairs"][0]["enabled"])
        self.assertTrue(profile["options"]["sync_pairs"][1]["enabled"])


if __name__ == "__main__":
    unittest.main()
