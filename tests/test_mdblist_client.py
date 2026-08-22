import unittest
from unittest.mock import Mock, patch

import requests

from src.config import MdbListConfig
from src.mdblist_client import MdbListClient


class MdbListClientTests(unittest.TestCase):
    def test_get_user_lists_normalizes_response(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        response = Mock()
        response.json.return_value = [{
            "id": 12,
            "name": "Best Movies",
            "slug": "best-movies",
            "user_name": "demo",
            "description": "desc",
            "mediatype": "movie",
            "items": 25,
            "likes": 7,
            "type": "user",
            "private": False,
        }]

        with patch.object(client, "_get", return_value=response):
            items = client.get_user_lists()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 12)
        self.assertEqual(items[0]["mediatype"], "movie")

    def test_get_list_items_paginates_and_normalizes(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        page_one = Mock()
        page_one.json.return_value = [{
            "title": "Movie One",
            "release_year": 2023,
            "mediatype": "movie",
            "ids": {"tmdb": 101, "imdb": "tt0101"},
        }]
        page_one.headers = {"X-Has-More": "true"}

        page_two = Mock()
        page_two.json.return_value = [{
            "title": "Show One",
            "release_year": 2024,
            "mediatype": "show",
            "ids": {"tmdb": 202, "tvdb": 303},
        }]
        page_two.headers = {"X-Has-More": "false"}

        with patch.object(client, "_get", side_effect=[page_one, page_two]):
            items = client.get_list_items(12)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["media_type"], "movie")
        self.assertEqual(items[1]["media_type"], "tv")

    def test_get_list_items_supports_wrapped_payload(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        response = Mock()
        response.json.return_value = {
            "items": [{
                "title": "Wrapped Movie",
                "release_year": 2022,
                "mediatype": "movie",
                "ids": {"tmdb": 404, "imdb": "tt0404"},
            }]
        }
        response.headers = {}

        with patch.object(client, "_get", return_value=response):
            items = client.get_list_items(44)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Wrapped Movie")
        self.assertEqual(items[0]["media_type"], "movie")


if __name__ == "__main__":
    unittest.main()


class MdbListAuthTests(unittest.TestCase):
    """Two auth modes: an OAuth bearer token, else the apikey query param."""

    def test_apikey_is_used_when_there_is_no_token(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        self.assertEqual(client._auth_params(), {"apikey": "key"})
        self.assertEqual(client._auth_headers(), {})

    def test_bearer_token_is_preferred_and_replaces_the_apikey(self) -> None:
        # The token is the credential carrying the write scope, and sending
        # both would leave which one authorised the write ambiguous.
        client = MdbListClient(MdbListConfig(api_key="key", access_token="tok"))
        self.assertEqual(client._auth_headers(), {"Authorization": "Bearer tok"})
        self.assertEqual(client._auth_params(), {})

    def test_write_auth_needs_one_credential_or_the_other(self) -> None:
        self.assertTrue(MdbListClient(MdbListConfig(api_key="key")).has_write_auth())
        self.assertTrue(MdbListClient(MdbListConfig(access_token="tok")).has_write_auth())
        self.assertFalse(MdbListClient(MdbListConfig()).has_write_auth())

    def test_authorize_url_carries_pkce_and_the_write_scope(self) -> None:
        client = MdbListClient(MdbListConfig(client_id="cid"))
        verifier, challenge = MdbListClient.generate_pkce_pair()
        self.assertNotEqual(verifier, challenge)
        url = client.build_authorize_url("https://sync.example/", challenge)
        # Authorization lives on the site host, not the API host.
        self.assertTrue(url.startswith("https://mdblist.com/oauth/authorize/?"))
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("scope=read+write", url)
        self.assertIn("client_id=cid", url)

    def test_token_exchange_posts_the_verifier_to_the_trailing_slash_path(self) -> None:
        client = MdbListClient(MdbListConfig(
            client_id="cid", client_secret="sec", base_url="https://api.mdblist.com",
        ))
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "at", "refresh_token": "rt", "expires_in": 60}
        with patch.object(client._session, "post", return_value=response) as post:
            payload = client.exchange_code_for_token("code-1", "verifier-1", "https://sync.example/")

        url = post.call_args.args[0]
        # MDBList rejects OAuth paths without the trailing slash.
        self.assertEqual(url, "https://api.mdblist.com/oauth/token/")
        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code_verifier"], "verifier-1")
        self.assertEqual(sent["client_secret"], "sec")
        self.assertEqual(payload["access_token"], "at")

    def test_refresh_adopts_the_new_token(self) -> None:
        config = MdbListConfig(client_id="cid", client_secret="sec", refresh_token="old")
        client = MdbListClient(config)
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "new-at", "refresh_token": "new-rt"}
        with patch.object(client._session, "post", return_value=response):
            client.refresh_access_token()
        self.assertEqual(config.access_token, "new-at")
        self.assertEqual(config.refresh_token, "new-rt")

    def test_refresh_without_a_stored_token_fails_loudly(self) -> None:
        client = MdbListClient(MdbListConfig(client_id="cid", client_secret="sec"))
        with self.assertRaises(RuntimeError):
            client.refresh_access_token()

    def test_token_error_surfaces_the_server_description(self) -> None:
        client = MdbListClient(MdbListConfig(client_id="cid", client_secret="sec"))
        response = Mock(status_code=400)
        response.json.return_value = {"error_description": "code expired"}
        with patch.object(client._session, "post", return_value=response):
            with self.assertRaises(RuntimeError) as caught:
                client.exchange_code_for_token("c", "v", "https://sync.example/")
        self.assertIn("code expired", str(caught.exception))


class MdbListSyncWriteTests(unittest.TestCase):
    def test_writes_are_not_retried(self) -> None:
        # A write that timed out may already have landed, so retrying it
        # duplicates list items or watch records.
        client = MdbListClient(MdbListConfig(api_key="key"))
        adapter = client._session.get_adapter("https://api.mdblist.com")
        self.assertNotIn("POST", adapter.max_retries.allowed_methods)
        self.assertIn("GET", adapter.max_retries.allowed_methods)

    def test_payload_splits_movies_from_shows_and_drops_unidentifiable_items(self) -> None:
        payload = MdbListClient._to_sync_payload([
            {"media_type": "movie", "tmdb_id": "603", "imdb_id": "tt0133093", "title": "The Matrix"},
            {"media_type": "tv", "tmdb_id": "1396"},
            {"media_type": "movie", "title": "No ids at all"},
        ])
        self.assertEqual(payload["movies"], [
            {"ids": {"imdb": "tt0133093", "tmdb": 603}, "title": "The Matrix"},
        ])
        self.assertEqual(payload["shows"], [{"ids": {"tmdb": 1396}}])

    def test_watched_response_is_flattened_out_of_its_wrapper(self) -> None:
        items = MdbListClient._from_sync_payload({
            "movies": [{
                "last_watched_at": "2025-10-21T12:00:00Z",
                "movie": {"title": "Batman Begins", "year": 2005,
                          "ids": {"imdb": "tt0372784", "tmdb": 272}},
            }],
        }, "history")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tmdb_id"], "272")
        self.assertEqual(items[0]["media_type"], "movie")
        self.assertEqual(items[0]["watched_at"], "2025-10-21T12:00:00Z")

    def test_watchlist_uses_the_dedicated_items_endpoint_and_cursor(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        first = Mock()
        first.json.return_value = {
            "movies": [{"id": 278, "title": "The Shawshank Redemption", "imdb_id": "tt0111161"}],
            "pagination": {"next_cursor": "next-page"},
        }
        first.headers = {}
        second = Mock()
        second.json.return_value = {
            "shows": [{"id": 1396, "title": "Breaking Bad", "tvdb_id": 81189}],
            "pagination": {"next_cursor": None},
        }
        second.headers = {}

        with patch.object(client, "_get", side_effect=[first, second]) as get:
            items = client.get_sync_items("watchlist")

        self.assertEqual([item["tmdb_id"] for item in items], ["278", "1396"])
        self.assertEqual(get.call_args_list[0].args[0], "/watchlist/items")
        self.assertEqual(get.call_args_list[1].args[1]["cursor"], "next-page")

    def test_each_category_maps_to_its_own_endpoint(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        with patch.object(client, "_post", return_value={}) as post:
            client.add_sync_items("history", [{"media_type": "movie", "tmdb_id": "1"}])
            client.remove_sync_items("watchlist", [{"media_type": "movie", "tmdb_id": "1"}])
        self.assertEqual(post.call_args_list[0].args[0], "/sync/watched")
        self.assertEqual(post.call_args_list[1].args[0], "/watchlist/items/remove")

    def test_watchlist_add_uses_the_dedicated_write_endpoint(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        with patch.object(client, "_post", return_value={}) as post:
            client.add_sync_items("watchlist", [{"media_type": "movie", "tmdb_id": "1"}])
        self.assertEqual(post.call_args.args[0], "/watchlist/items/add")

    def test_trakt_shaped_counts_are_flattened_into_flat_totals(self) -> None:
        # MDBList's sync API is Trakt-shaped: the buckets are dicts per media
        # type, not the scalars their names suggest. Returning them raw made
        # cross_sync do int({"movies": 2}) *after* the write had landed, so a
        # successful sync was reported as an error and its managed keys were
        # never recorded — every run then re-added the whole list.
        client = MdbListClient(MdbListConfig(api_key="key"))
        response = {
            "added": {"movies": 2, "shows": 1},
            "existing": {"movies": 1, "shows": 0},
            "not_found": {"movies": ["tt999"], "shows": []},
        }
        with patch.object(client, "_post", return_value=response):
            totals = client.add_sync_items("watchlist", [
                {"media_type": "movie", "tmdb_id": "1"},
                {"media_type": "tv", "tmdb_id": "2"},
            ])
        self.assertEqual(totals["added"], 4)
        self.assertEqual(totals["not_found"], 1)
        self.assertIsInstance(totals["added"], int)

    def test_removals_report_a_flat_deleted_count(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        with patch.object(client, "_post", return_value={"deleted": {"movies": 3, "shows": 2}}):
            totals = client.remove_sync_items("watchlist", [{"media_type": "movie", "tmdb_id": "1"}])
        self.assertEqual(totals["deleted"], 5)

    def test_an_unrecognised_success_body_counts_what_was_sent(self) -> None:
        # A 200 that carries no bucket we know still means the items went, so
        # reporting 0 added would understate a write that actually happened.
        client = MdbListClient(MdbListConfig(api_key="key"))
        with patch.object(client, "_post", return_value={"success": True}):
            totals = client.add_sync_items("watchlist", [
                {"media_type": "movie", "tmdb_id": "1"},
                {"media_type": "tv", "tmdb_id": "2"},
            ])
        self.assertEqual(totals["added"], 2)

    def test_named_list_writes_are_flattened_too(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        with patch.object(client, "_post", return_value={"added": {"movies": 1}}):
            totals = client.change_list_items(10, [{"media_type": "movie", "tmdb_id": "630"}], "add")
        self.assertEqual(totals["added"], 1)

    def test_http_errors_do_not_echo_the_api_key(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="top-secret"))
        response = Mock(status_code=405)
        response.raise_for_status.side_effect = requests.HTTPError(
            "405 for https://api.mdblist.com/watchlist?apikey=top-secret",
            response=response,
        )
        with patch.object(client._session, "get", return_value=response):
            with self.assertRaisesRegex(Exception, r"apikey=\[redacted\]") as caught:
                client._get("/watchlist")
        self.assertNotIn("top-secret", str(caught.exception))

    def test_an_unknown_category_is_refused_rather_than_guessed(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        with self.assertRaises(ValueError):
            client.add_sync_items("ratings", [{"media_type": "movie", "tmdb_id": "1"}])

    def test_nothing_identifiable_means_no_request_at_all(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        with patch.object(client, "_post") as post:
            client.add_sync_items("watchlist", [{"media_type": "movie", "title": "no ids"}])
        post.assert_not_called()

    def test_list_items_use_bare_ids_not_the_sync_shape(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        with patch.object(client, "_post", return_value={}) as post:
            client.change_list_items(10, [{"media_type": "movie", "tmdb_id": "630"}], "add")
        self.assertEqual(post.call_args.args[0], "/lists/10/items/add")
        self.assertEqual(post.call_args.args[1], {"movies": [{"tmdb": 630}], "shows": []})

    def test_list_action_must_be_add_or_remove(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        with self.assertRaises(ValueError):
            client.change_list_items(10, [{"media_type": "movie", "tmdb_id": "1"}], "delete")


class _Resp:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.headers = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload


class MdbListTokenRefreshTests(unittest.TestCase):
    """MDBList access tokens last 30 days.

    Without refresh the connection dies silently a month after it was made and
    the only cure is redoing the whole OAuth flow, which is what made a working
    MDBList login look broken.
    """

    @staticmethod
    def _oauth_config(**overrides) -> MdbListConfig:
        values = {
            "client_id": "cid",
            "client_secret": "csecret",
            "access_token": "old-token",
            "refresh_token": "old-refresh",
        }
        values.update(overrides)
        return MdbListConfig(**values)

    def test_a_401_refreshes_the_token_and_retries_once(self) -> None:
        saved: list[tuple] = []
        client = MdbListClient(
            self._oauth_config(),
            token_refreshed_callback=lambda at, rt, exp="": saved.append((at, rt, exp)),
        )
        client.refresh_access_token = lambda: {
            "access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 2592000,
        }
        responses = [_Resp(401), _Resp(200, [{"id": 1, "name": "L", "slug": "l"}])]
        sent: list[dict] = []

        def fake_get(url, params=None, headers=None, timeout=None):
            sent.append(dict(headers or {}))
            return responses.pop(0)

        client._session.get = fake_get
        result = client._get("/lists/user/")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(sent), 2)
        # The retry must carry the new bearer, not the stale one.
        self.assertEqual(sent[0]["Authorization"], "Bearer old-token")
        self.assertEqual(sent[1]["Authorization"], "Bearer new-token")
        # And the refreshed pair has to be persisted, or the next process
        # starts from the expired token all over again.
        self.assertEqual(saved[0][0], "new-token")
        self.assertEqual(saved[0][1], "new-refresh")
        self.assertTrue(saved[0][2])

    def test_an_api_key_401_is_not_retried(self) -> None:
        """No bearer means a bad key, and no refresh can fix that."""
        client = MdbListClient(MdbListConfig(api_key="bad-key"))
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(url)
            return _Resp(401)

        client._session.get = fake_get
        with self.assertRaises(requests.HTTPError):
            client._get("/lists/user/")
        self.assertEqual(len(calls), 1)

    def test_refresh_is_attempted_once_per_client(self) -> None:
        """The refresh token rotates, so a second concurrent exchange would be
        replaying an already-spent token."""
        client = MdbListClient(self._oauth_config())
        refreshes = []
        client.refresh_access_token = lambda: (
            refreshes.append(1) or {"access_token": "new-token", "expires_in": 60}
        )
        client._session.get = lambda url, params=None, headers=None, timeout=None: _Resp(401)

        for _ in range(3):
            with self.assertRaises(requests.HTTPError):
                client._get("/lists/user/")

        self.assertEqual(len(refreshes), 1)

    def test_a_token_expiring_soon_is_refreshed_before_the_request(self) -> None:
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        client = MdbListClient(self._oauth_config(access_token_expires_at=soon))
        refreshes = []
        client.refresh_access_token = lambda: (
            refreshes.append(1) or {"access_token": "new-token", "expires_in": 2592000}
        )
        sent: list[dict] = []

        def fake_get(url, params=None, headers=None, timeout=None):
            sent.append(dict(headers or {}))
            return _Resp(200, [])

        client._session.get = fake_get
        client._get("/lists/user/")

        self.assertEqual(len(refreshes), 1)
        self.assertEqual(sent[0]["Authorization"], "Bearer new-token")

    def test_no_refresh_token_means_no_refresh_attempt(self) -> None:
        client = MdbListClient(self._oauth_config(refresh_token=""))
        client.refresh_access_token = lambda: self.fail("must not refresh without a refresh token")
        client._session.get = lambda url, params=None, headers=None, timeout=None: _Resp(401)

        with self.assertRaises(requests.HTTPError):
            client._get("/lists/user/")

    def test_a_write_401_also_refreshes_and_retries(self) -> None:
        """Safe for a write too: a 401 is rejected before any work, so the
        retry cannot duplicate a record."""
        client = MdbListClient(self._oauth_config())
        client.refresh_access_token = lambda: {"access_token": "new-token", "expires_in": 60}
        responses = [_Resp(401), _Resp(200, {"added": 1})]
        sent: list[dict] = []

        def fake_post(url, params=None, json=None, headers=None, timeout=None):
            sent.append(dict(headers or {}))
            return responses.pop(0)

        client._session.post = fake_post
        payload = client._post("/watchlist/items/add", {"movies": []})

        self.assertEqual(payload, {"added": 1})
        self.assertEqual(sent[1]["Authorization"], "Bearer new-token")
