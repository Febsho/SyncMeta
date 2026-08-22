import unittest

import requests

from src.config import TraktConfig
from src.trakt_client import TraktAuthenticationError, TraktClient


class FakeResponse:
    def __init__(self, status_code: int, text: str = "", payload=None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError("401 responses should be converted before raise_for_status")

    def json(self):
        return self._payload if self._payload is not None else {}


class FakeSession:
    def request(self, method: str, url: str, timeout: int = 30, **kwargs) -> FakeResponse:
        return FakeResponse(401, "unauthorized")


class RefreshingSession:
    def __init__(self) -> None:
        self.headers = {"Authorization": "Bearer old"}
        self.requests = 0
        self.refreshes = 0

    def post(self, url: str, json=None, timeout=None) -> FakeResponse:
        self.refreshes += 1
        return FakeResponse(200, "{}", {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
            "created_at": 1893456000,
        })

    def request(self, method: str, url: str, timeout: int = 30, **kwargs) -> FakeResponse:
        self.requests += 1
        if self.requests == 1:
            return FakeResponse(401, "unauthorized")
        return FakeResponse(200, "[]", [])


class ForbiddenSession:
    def request(self, method: str, url: str, timeout: int = 30, **kwargs) -> FakeResponse:
        return FakeResponse(403, "forbidden")


class ForbiddenThenOkSession(RefreshingSession):
    def request(self, method: str, url: str, timeout: int = 30, **kwargs) -> FakeResponse:
        self.requests += 1
        if self.requests == 1:
            return FakeResponse(403, "forbidden")
        return FakeResponse(200, "[]", [])


class AccountLimitResponse(FakeResponse):
    def raise_for_status(self) -> None:
        raise requests.HTTPError(
            "420 Client Error for url: https://api.trakt.tv/sync/watchlist",
            response=self,
        )


class AccountLimitSession:
    def request(self, method: str, url: str, timeout: int = 30, **kwargs) -> FakeResponse:
        return AccountLimitResponse(420, "account limit exceeded")


class TraktClientTests(unittest.TestCase):
    def test_420_is_reported_as_an_actionable_account_limit(self) -> None:
        """A bare HTTP 420 looks like throttling, but Trakt means item limit."""
        client = TraktClient(TraktConfig(base_url="https://api.trakt.tv"))
        client._session = AccountLimitSession()

        with self.assertRaisesRegex(Exception, "(?i)account limit.*VIP"):
            client._get("/sync/watchlist")

    def test_401_raises_reconnect_error(self) -> None:
        client = TraktClient(TraktConfig(base_url="https://api.trakt.tv"))
        client._session = FakeSession()

        with self.assertRaisesRegex(TraktAuthenticationError, "Trakt token expired, reconnect Trakt"):
            client._get("/sync/playback/movies")

    def test_401_refreshes_token_and_retries_request(self) -> None:
        captured = {}
        client = TraktClient(
            TraktConfig(
                base_url="https://api.trakt.tv",
                client_id="client",
                client_secret="secret",
                access_token="old",
                refresh_token="refresh",
            ),
            token_refreshed_callback=lambda at, rt, exp="": captured.update({
                "access": at,
                "refresh": rt,
                "expires_at": exp,
            }),
        )
        session = RefreshingSession()
        client._session = session

        result = client._get("/sync/playback/movies")

        self.assertEqual(result, [])
        self.assertEqual(session.refreshes, 1)
        self.assertEqual(session.requests, 2)
        self.assertEqual(session.headers["Authorization"], "Bearer new-access")
        self.assertEqual(client._config.refresh_token, "new-refresh")
        self.assertEqual(captured["access"], "new-access")
        self.assertEqual(captured["refresh"], "new-refresh")
        self.assertTrue(captured["expires_at"])

    def test_403_raises_auth_error_instead_of_http_error(self) -> None:
        client = TraktClient(TraktConfig(base_url="https://api.trakt.tv"))
        client._session = ForbiddenSession()

        with self.assertRaisesRegex(TraktAuthenticationError, "403 Forbidden"):
            client._get("/sync/watchlist")

    def test_403_refreshes_token_and_retries_request(self) -> None:
        client = TraktClient(TraktConfig(
            base_url="https://api.trakt.tv",
            client_id="client",
            client_secret="secret",
            access_token="old",
            refresh_token="refresh",
        ))
        session = ForbiddenThenOkSession()
        client._session = session

        result = client._get("/sync/watchlist")

        self.assertEqual(result, [])
        self.assertEqual(session.refreshes, 1)
        self.assertEqual(session.requests, 2)

    def test_normalize_movie_watchlist_entry(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_watchlist_entry({
            "type": "movie",
            "listed_at": "2026-01-01T00:00:00.000Z",
            "movie": {
                "title": "Movie",
                "year": 2024,
                "ids": {"tmdb": 123, "imdb": "tt123"},
            },
        })

        self.assertEqual(item["media_type"], "movie")
        self.assertEqual(item["tmdb_id"], "123")
        self.assertEqual(item["imdb_id"], "tt123")

    def test_normalize_show_watchlist_entry(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_watchlist_entry({
            "type": "show",
            "listed_at": "2026-01-01T00:00:00.000Z",
            "show": {
                "title": "Show",
                "year": 2024,
                "ids": {"tmdb": 456, "tvdb": 789},
            },
        })

        self.assertEqual(item["media_type"], "tv")
        self.assertEqual(item["tmdb_id"], "456")
        self.assertEqual(item["tvdb_id"], "789")

    def test_normalize_liked_list_metadata(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_list_metadata({
            "type": "list",
            "list": {
                "name": "Anime",
                "description": "Favorites",
                "item_count": 22,
                "likes": 300,
                "share_link": "https://trakt.tv/users/demo/lists/anime",
                "ids": {"trakt": 12, "slug": "anime"},
                "user": {"username": "demo", "ids": {"slug": "demo"}},
            },
        }, source="liked")

        self.assertEqual(item["name"], "Anime")
        self.assertEqual(item["user"], "demo")
        self.assertEqual(item["slug"], "anime")
        self.assertEqual(item["source"], "liked")
        self.assertEqual(item["catalog_key"], "")

    def test_normalize_personal_list_metadata(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_list_metadata({
            "name": "My Custom List",
            "description": "Mine",
            "item_count": 12,
            "likes": 0,
            "ids": {"trakt": 99, "slug": "my-custom-list"},
            "user": {"username": "demo", "ids": {"slug": "demo"}},
        }, source="personal")

        self.assertEqual(item["name"], "My Custom List")
        self.assertEqual(item["user"], "demo")
        self.assertEqual(item["slug"], "my-custom-list")
        self.assertEqual(item["source"], "personal")

    def test_normalize_default_catalog_movie_entry(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_catalog_entry({
            "watchers": 12,
            "movie": {
                "title": "Trending Movie",
                "year": 2025,
                "ids": {"tmdb": 321, "imdb": "tt321"},
            },
        }, "movie")

        self.assertEqual(item["media_type"], "movie")
        self.assertEqual(item["tmdb_id"], "321")

    def test_normalize_movie_history_entry(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_movie_history_entry({
            "watched_at": "2026-04-01T12:00:00.000Z",
            "movie": {
                "title": "History Movie",
                "ids": {"tmdb": 777},
            },
        })

        self.assertEqual(item["tmdb_id"], 777)
        self.assertEqual(item["media_type"], "movie")
        self.assertEqual(item["watched_at"], "2026-04-01T12:00:00.000Z")

    def test_get_watched_history_since_filters_older_entries(self) -> None:
        class RecordingTraktClient(TraktClient):
            def __init__(self) -> None:
                super().__init__(TraktConfig())

            def _get_paginated_history(self, path: str, normalizer, since=None, status_callback=None, label="") -> list[dict]:
                if "movies" in path:
                    return [
                        {"tmdb_id": 1, "media_type": "movie", "watched_at": "2026-04-01T12:00:00.000Z", "title": "Old Movie"},
                        {"tmdb_id": 2, "media_type": "movie", "watched_at": "2026-04-03T12:00:00.000Z", "title": "New Movie"},
                    ]
                return [
                    {"tmdb_id": 3, "media_type": "tv", "season": 1, "episode": 1, "watched_at": "2026-04-04T12:00:00.000Z", "title": "New Episode"},
                ]

        client = RecordingTraktClient()

        history = client.get_watched_history(since="2026-04-02T00:00:00.000Z")

        self.assertEqual(len(history), 2)
        self.assertEqual({item["tmdb_id"] for item in history}, {2, 3})

    def test_normalize_episode_playback_entry(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_episode_playback_entry({
            "progress": 50,
            "paused_at": "2026-04-01T13:00:00.000Z",
            "show": {
                "title": "Playback Show",
                "ids": {"tmdb": 888},
            },
            "episode": {
                "season": 2,
                "number": 4,
                "runtime": 48,
            },
        })

        self.assertEqual(item["tmdb_id"], 888)
        self.assertEqual(item["media_type"], "tv")
        self.assertEqual(item["season"], 2)
        self.assertEqual(item["episode"], 4)
        self.assertEqual(item["runtime_ms"], 48 * 60_000)
        self.assertEqual(item["position_ms"], 24 * 60_000)


if __name__ == "__main__":
    unittest.main()


class WatchedStateSession:
    """Serves /sync/watched responses; records the paths that were requested."""

    def __init__(self, movies=None, shows=None) -> None:
        self.paths: list[str] = []
        self._movies = movies if movies is not None else []
        self._shows = shows if shows is not None else []

    def request(self, method: str, url: str, timeout: int = 30, **kwargs) -> FakeResponse:
        self.paths.append(url)
        payload = self._movies if url.endswith("/sync/watched/movies") else self._shows
        return FakeResponse(200, "[]", payload)


class TraktWatchedStateTests(unittest.TestCase):
    """`/sync/watched` is the only complete view of a multi-season show.

    The play log at /sync/history is read forward from a cursor, so these
    tests pin the shape that lets a partially-imported show be backfilled.
    """

    @staticmethod
    def _client(session) -> TraktClient:
        client = TraktClient(TraktConfig(base_url="https://api.trakt.tv"))
        client._session = session
        return client

    def test_every_season_of_a_show_is_returned_as_episode_rows(self) -> None:
        session = WatchedStateSession(shows=[{
            "plays": 38,
            "last_watched_at": "2026-01-05T20:00:00.000Z",
            "show": {"title": "Multi Season Anime", "ids": {"tmdb": 4242, "trakt": 9}},
            "seasons": [
                {"number": 1, "episodes": [
                    {"number": 1, "plays": 1, "last_watched_at": "2025-11-01T20:00:00.000Z"},
                    {"number": 2, "plays": 2, "last_watched_at": "2025-11-02T20:00:00.000Z"},
                ]},
                {"number": 3, "episodes": [
                    {"number": 7, "plays": 1, "last_watched_at": "2026-01-05T20:00:00.000Z"},
                ]},
            ],
        }])

        rows = self._client(session).get_watched_state()

        self.assertEqual(
            [(row["tmdb_id"], row["season"], row["episode"]) for row in rows],
            [(4242, 1, 1), (4242, 1, 2), (4242, 3, 7)],
        )
        self.assertTrue(all(row["media_type"] == "tv" for row in rows))
        self.assertTrue(all(row["watched_state"] for row in rows))
        # These rows must never move the history cursor: last_watched_at is not
        # a play timestamp, so treating it as one would skip real plays.
        self.assertTrue(all(row["cursor_exempt"] for row in rows))
        self.assertEqual(rows[1]["plays"], 2)
        self.assertEqual(rows[2]["watched_at"], "2026-01-05T20:00:00.000Z")

    def test_specials_and_episode_zero_are_skipped(self) -> None:
        session = WatchedStateSession(shows=[{
            "show": {"title": "Specials Show", "ids": {"tmdb": 55}},
            "seasons": [
                {"number": 0, "episodes": [{"number": 1, "plays": 1}]},
                {"number": 1, "episodes": [
                    {"number": 0, "plays": 1},
                    {"number": 1, "plays": 1, "last_watched_at": "2026-02-01T10:00:00.000Z"},
                ]},
            ],
        }])

        rows = self._client(session).get_watched_state()

        self.assertEqual([(row["season"], row["episode"]) for row in rows], [(1, 1)])

    def test_episode_without_its_own_timestamp_falls_back_to_the_show(self) -> None:
        session = WatchedStateSession(shows=[{
            "last_watched_at": "2026-03-03T09:00:00.000Z",
            "show": {"title": "No Episode Stamps", "ids": {"tmdb": 77}},
            "seasons": [{"number": 2, "episodes": [{"number": 4, "plays": 1}]}],
        }])

        rows = self._client(session).get_watched_state()

        self.assertEqual(rows[0]["watched_at"], "2026-03-03T09:00:00.000Z")

    def test_movies_are_returned_and_entries_without_a_tmdb_id_are_dropped(self) -> None:
        session = WatchedStateSession(
            movies=[
                {"plays": 3, "last_watched_at": "2026-01-01T00:00:00.000Z",
                 "movie": {"title": "Watched Movie", "ids": {"tmdb": 601}}},
                {"plays": 1, "movie": {"title": "No TMDB", "ids": {"trakt": 4}}},
            ],
            shows=[{"show": {"title": "No TMDB Show", "ids": {"trakt": 5}},
                    "seasons": [{"number": 1, "episodes": [{"number": 1}]}]}],
        )

        rows = self._client(session).get_watched_state()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tmdb_id"], 601)
        self.assertEqual(rows[0]["media_type"], "movie")
        self.assertEqual(rows[0]["plays"], 3)

    def test_malformed_payloads_do_not_raise(self) -> None:
        session = WatchedStateSession(
            movies=["nonsense", {"movie": None}],
            shows=[{"show": {"ids": {"tmdb": 1}}, "seasons": "not-a-list"},
                   {"show": {"ids": {"tmdb": 2}}, "seasons": [{"number": "x"}]}],
        )

        self.assertEqual(self._client(session).get_watched_state(), [])
