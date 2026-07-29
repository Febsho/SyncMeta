import unittest
from unittest.mock import patch

from src import tmdb_client
from src.profile_store import merge_credentials, normalize_credentials, public_credentials
from src.tmdb_client import TmdbClient, TmdbError, normalize_media_type


class TmdbCredentialTests(unittest.TestCase):
    def test_normalize_credentials_carries_tmdb_key(self) -> None:
        creds = normalize_credentials({"tmdb": {"api_key": "  tmdb-key  "}})
        self.assertEqual(creds["tmdb"]["api_key"], "tmdb-key")

    def test_normalize_credentials_defaults_tmdb_to_empty(self) -> None:
        creds = normalize_credentials({})
        self.assertEqual(creds["tmdb"]["api_key"], "")

    def test_public_credentials_masks_tmdb_key(self) -> None:
        public = public_credentials({"tmdb": {"api_key": "tmdb-key"}})
        self.assertTrue(public["tmdb"]["api_key_saved"])
        self.assertNotIn("api_key", public["tmdb"])

    def test_merge_credentials_keeps_stored_tmdb_key_on_blank_submission(self) -> None:
        merged = merge_credentials({"tmdb": {"api_key": "stored-key"}}, {"tmdb": {"api_key": ""}})
        self.assertEqual(merged["tmdb"]["api_key"], "stored-key")

    def test_merge_credentials_replaces_tmdb_key_when_supplied(self) -> None:
        merged = merge_credentials({"tmdb": {"api_key": "stored-key"}}, {"tmdb": {"api_key": "new-key"}})
        self.assertEqual(merged["tmdb"]["api_key"], "new-key")


class NormalizeMediaTypeTests(unittest.TestCase):
    def test_show_like_values_map_to_tv(self) -> None:
        for value in ("tv", "show", "shows", "series", "anime", "TV", " Show "):
            self.assertEqual(normalize_media_type(value), "tv", value)

    def test_everything_else_maps_to_movie(self) -> None:
        for value in ("movie", "movies", "", None, "unknown"):
            self.assertEqual(normalize_media_type(value), "movie", repr(value))


class TmdbClientTests(unittest.TestCase):
    def setUp(self) -> None:
        tmdb_client.clear_cache()

    def tearDown(self) -> None:
        tmdb_client.clear_cache()

    def test_get_details_parses_movie_payload(self) -> None:
        client = TmdbClient("key")
        with patch.object(TmdbClient, "_request", return_value={
            "title": "Inception",
            "release_date": "2010-07-16",
            "poster_path": "/poster.jpg",
            "overview": "A mind heist.",
        }) as mock_request:
            details = client.get_details(27205, "movie")
        mock_request.assert_called_once_with("/movie/27205")
        self.assertEqual(details["title"], "Inception")
        self.assertEqual(details["year"], "2010")
        self.assertEqual(details["poster_url"], f"{tmdb_client.POSTER_BASE_URL}/poster.jpg")

    def test_get_details_uses_tv_name_and_first_air_date(self) -> None:
        client = TmdbClient("key")
        with patch.object(TmdbClient, "_request", return_value={
            "name": "Frieren",
            "first_air_date": "2023-09-29",
            "poster_path": "",
        }):
            details = client.get_details(209867, "anime")
        self.assertEqual(details["media_type"], "tv")
        self.assertEqual(details["title"], "Frieren")
        self.assertEqual(details["year"], "2023")
        self.assertEqual(details["poster_url"], "")

    def test_get_details_caches_across_client_instances(self) -> None:
        with patch.object(TmdbClient, "_request", return_value={"title": "Cached", "release_date": "2020-01-01"}) as mock_request:
            TmdbClient("key").get_details(1, "movie")
            details = TmdbClient("other-key").get_details(1, "movie")
        self.assertEqual(mock_request.call_count, 1)
        self.assertEqual(details["title"], "Cached")

    def test_get_details_caches_negative_404_results(self) -> None:
        with patch.object(TmdbClient, "_request", side_effect=TmdbError("gone", status_code=404)) as mock_request:
            client = TmdbClient("key")
            self.assertIsNone(client.get_details(999, "movie"))
            self.assertIsNone(client.get_details(999, "movie"))
        self.assertEqual(mock_request.call_count, 1)

    def test_get_details_does_not_cache_auth_failures(self) -> None:
        client = TmdbClient("bad-key")
        with patch.object(TmdbClient, "_request", side_effect=TmdbError("denied", status_code=401)):
            with self.assertRaises(TmdbError):
                client.get_details(1, "movie")
        with patch.object(TmdbClient, "_request", return_value={"title": "Now works"}) as mock_request:
            details = client.get_details(1, "movie")
        self.assertEqual(mock_request.call_count, 1)
        self.assertEqual(details["title"], "Now works")

    def test_get_details_rejects_invalid_ids(self) -> None:
        client = TmdbClient("key")
        self.assertIsNone(client.get_details(None, "movie"))
        self.assertIsNone(client.get_details("abc", "movie"))
        self.assertIsNone(client.get_details(0, "movie"))

    def test_batch_dedupes_and_keys_on_normalized_media_type(self) -> None:
        client = TmdbClient("key")
        with patch.object(TmdbClient, "_request", return_value={"title": "Once"}) as mock_request:
            results = client.get_details_batch([(5, "shows"), (5, "tv"), (5, "anime")])
        self.assertEqual(mock_request.call_count, 1)
        self.assertIn(("tv", 5), results)

    def test_get_season_episodes_parses_and_caches(self) -> None:
        with patch.object(TmdbClient, "_request", return_value={
            "episodes": [
                {"episode_number": 1, "name": "Pilot", "still_path": "/e1.jpg", "air_date": "2022-02-20"},
                {"episode_number": "not-a-number", "name": "Dropped"},
            ],
        }) as mock_request:
            first = TmdbClient("key").get_season_episodes(1, 1)
            second = TmdbClient("other-key").get_season_episodes(1, 1)
        mock_request.assert_called_once_with("/tv/1/season/1")
        self.assertEqual(first[1]["name"], "Pilot")
        self.assertEqual(first[1]["still_url"], f"{tmdb_client.STILL_BASE_URL}/e1.jpg")
        self.assertNotIn("not-a-number", first)
        self.assertEqual(second, first)

    def test_get_season_episodes_caches_missing_season_as_empty(self) -> None:
        with patch.object(TmdbClient, "_request", side_effect=TmdbError("gone", status_code=404)) as mock_request:
            client = TmdbClient("key")
            self.assertEqual(client.get_season_episodes(1, 99), {})
            self.assertEqual(client.get_season_episodes(1, 99), {})
        self.assertEqual(mock_request.call_count, 1)

    def test_batch_raises_auth_error_when_nothing_resolves(self) -> None:
        client = TmdbClient("bad-key")
        with patch.object(TmdbClient, "_request", side_effect=TmdbError("denied", status_code=401)):
            with self.assertRaises(TmdbError):
                client.get_details_batch([(1, "movie"), (2, "tv")])


if __name__ == "__main__":
    unittest.main()
