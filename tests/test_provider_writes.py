"""Tests for the provider write APIs used by cross-service sync pairs.

The payload builders are the risky part: each service groups items by media kind
and expresses episodes as a nested seasons/episodes tree, so a mistake here
writes the wrong thing to somebody's real library.
"""

import unittest
from unittest.mock import patch

from src.anilist_client import AniListClient
from src.config import AniListConfig, SimklConfig, TraktConfig
from src.mdblist_client import MdbListClient
from src.simkl_client import SimklClient
from src.trakt_client import TraktClient


def _movie(**overrides) -> dict:
    item = {
        "title": "Princess Mononoke",
        "year": 1997,
        "media_type": "movie",
        "tmdb_id": "128",
        "imdb_id": "tt0119698",
        "ids": {},
    }
    item.update(overrides)
    return item


def _episode(season: int, episode: int, **overrides) -> dict:
    item = {
        "title": "Attack on Titan",
        "media_type": "tv",
        "tmdb_id": "1429",
        "season": season,
        "episode": episode,
        "ids": {},
    }
    item.update(overrides)
    return item


class TraktWritePayloadTests(unittest.TestCase):
    def test_movie_carries_every_known_id(self) -> None:
        payload = TraktClient._build_sync_payload([_movie()])
        self.assertEqual(payload["movies"], [{"ids": {"imdb": "tt0119698", "tmdb": 128}}])
        self.assertNotIn("shows", payload)

    def test_numeric_ids_are_sent_as_integers(self) -> None:
        # Trakt rejects string ids for its numeric id fields.
        payload = TraktClient._build_sync_payload([_movie(tvdb_id="7899")])
        ids = payload["movies"][0]["ids"]
        self.assertIsInstance(ids["tmdb"], int)
        self.assertIsInstance(ids["tvdb"], int)
        self.assertIsInstance(ids["imdb"], str)

    def test_items_with_no_usable_ids_are_dropped(self) -> None:
        # An empty ids object would silently match nothing on Trakt's side.
        payload = TraktClient._build_sync_payload([{"title": "Nameless", "media_type": "movie"}])
        self.assertEqual(payload, {})

    def test_episodes_of_one_show_group_under_a_single_show_entry(self) -> None:
        payload = TraktClient._build_sync_payload([
            _episode(1, 1), _episode(1, 2), _episode(2, 1),
        ])
        self.assertEqual(len(payload["shows"]), 1)
        seasons = payload["shows"][0]["seasons"]
        self.assertEqual([s["number"] for s in seasons], [1, 2])
        self.assertEqual([e["number"] for e in seasons[0]["episodes"]], [1, 2])
        self.assertEqual([e["number"] for e in seasons[1]["episodes"]], [1])

    def test_distinct_shows_stay_separate(self) -> None:
        payload = TraktClient._build_sync_payload([
            _episode(1, 1),
            _episode(1, 1, tmdb_id="1399", title="Game of Thrones"),
        ])
        self.assertEqual(len(payload["shows"]), 2)

    def test_watched_at_is_only_included_for_history(self) -> None:
        item = _movie(watched_at="2026-01-02T03:04:05Z")
        without = TraktClient._build_sync_payload([item])
        self.assertNotIn("watched_at", without["movies"][0])
        with_stamp = TraktClient._build_sync_payload([item], with_watched_at=True)
        self.assertEqual(with_stamp["movies"][0]["watched_at"], "2026-01-02T03:04:05Z")

    def test_episode_watched_at_lands_on_the_episode(self) -> None:
        payload = TraktClient._build_sync_payload(
            [_episode(1, 3, watched_at="2026-01-02T03:04:05Z")], with_watched_at=True,
        )
        episode = payload["shows"][0]["seasons"][0]["episodes"][0]
        self.assertEqual(episode["watched_at"], "2026-01-02T03:04:05Z")

    def test_show_without_episode_numbers_is_sent_whole(self) -> None:
        # A watchlist entry is the show itself, with no seasons tree.
        payload = TraktClient._build_sync_payload([_episode(None, None)])
        self.assertEqual(payload["shows"], [{"ids": {"tmdb": 1429}}])

    def test_write_batches_and_totals(self) -> None:
        client = TraktClient(TraktConfig(client_id="c", access_token="t"))
        posted: list[tuple[str, dict]] = []

        def fake_post(path, data):
            posted.append((path, data))
            return {"added": {"movies": len(data.get("movies", []))}, "not_found": {"movies": []}}

        client._post = fake_post  # type: ignore[method-assign]
        with patch("src.trakt_client.TRAKT_SYNC_BATCH_SIZE", 2):
            totals = client.add_to_watchlist([
                _movie(tmdb_id="1"), _movie(tmdb_id="2"), _movie(tmdb_id="3"),
            ])
        self.assertEqual(len(posted), 2)
        self.assertEqual(posted[0][0], "/sync/watchlist")
        self.assertEqual(totals["added"], 3)
        self.assertEqual(totals["batches"], 2)

    def test_not_found_is_counted(self) -> None:
        client = TraktClient(TraktConfig(client_id="c", access_token="t"))
        client._post = lambda path, data: {  # type: ignore[method-assign]
            "added": {"movies": 0},
            "not_found": {"movies": [{"ids": {"tmdb": 1}}]},
        }
        totals = client.add_to_watchlist([_movie()])
        self.assertEqual(totals["not_found"], 1)

    def test_remove_endpoints_are_distinct(self) -> None:
        client = TraktClient(TraktConfig(client_id="c", access_token="t"))
        seen: list[str] = []
        client._post = lambda path, data: seen.append(path) or {}  # type: ignore[method-assign]
        client.remove_from_watchlist([_movie()])
        client.remove_from_history([_movie()])
        client.remove_from_collection([_movie()])
        self.assertEqual(seen, [
            "/sync/watchlist/remove",
            "/sync/history/remove",
            "/sync/collection/remove",
        ])


class SimklWritePayloadTests(unittest.TestCase):
    def test_target_list_is_named_per_item(self) -> None:
        payload = SimklClient._build_sync_payload([_movie()], to_list="plantowatch")
        self.assertEqual(payload["movies"][0]["to"], "plantowatch")

    def test_anime_ids_are_included(self) -> None:
        payload = SimklClient._build_sync_payload(
            [_movie(mal_id="164", anidb_id="7", ids={"simkl": "36228"})],
        )
        ids = payload["movies"][0]["ids"]
        self.assertEqual(ids["mal"], "164")
        self.assertEqual(ids["anidb"], "7")
        self.assertEqual(ids["simkl"], 36228)

    def test_episodes_group_under_one_show(self) -> None:
        payload = SimklClient._build_sync_payload([_episode(1, 1), _episode(1, 2)])
        self.assertEqual(len(payload["shows"]), 1)
        self.assertEqual(
            [e["number"] for e in payload["shows"][0]["seasons"][0]["episodes"]], [1, 2],
        )

    def test_category_maps_to_a_simkl_list_name(self) -> None:
        client = SimklClient(SimklConfig(client_id="c", access_token="t"))
        seen: list[tuple[str, dict]] = []
        client._post = lambda path, data: seen.append((path, data)) or {}  # type: ignore[method-assign]
        client.add_to_list([_movie()], "watchlist")
        client.add_to_list([_movie()], "collection")
        self.assertEqual(seen[0][0], "/sync/add-to-list")
        self.assertEqual(seen[0][1]["movies"][0]["to"], "plantowatch")
        self.assertEqual(seen[1][1]["movies"][0]["to"], "completed")

    def test_unknown_category_is_rejected_rather_than_guessed(self) -> None:
        client = SimklClient(SimklConfig(client_id="c", access_token="t"))
        with self.assertRaises(ValueError):
            client.add_to_list([_movie()], "ratings")

    def test_history_uses_the_history_endpoint(self) -> None:
        client = SimklClient(SimklConfig(client_id="c", access_token="t"))
        seen: list[str] = []
        client._post = lambda path, data: seen.append(path) or {}  # type: ignore[method-assign]
        client.add_to_history([_episode(1, 1, watched_at="2026-01-01T00:00:00Z")])
        client.remove_from_history([_episode(1, 1)])
        self.assertEqual(seen, ["/sync/history", "/sync/history/remove"])

    def test_not_found_reduces_the_added_count(self) -> None:
        client = SimklClient(SimklConfig(client_id="c", access_token="t"))
        client._post = lambda path, data: {"not_found": {"movies": [{"ids": {}}]}}  # type: ignore[method-assign]
        totals = client.add_to_list([_movie(tmdb_id="1"), _movie(tmdb_id="2")], "watchlist")
        self.assertEqual(totals["not_found"], 1)
        self.assertEqual(totals["added"], 1)


class AniListWriteTests(unittest.TestCase):
    def test_write_requires_a_token_and_says_so(self) -> None:
        # Reading a public list needs only a username, so existing profiles may
        # legitimately have no token. That must be reported, not crashed on.
        without = AniListClient(AniListConfig(username="someone"))
        self.assertFalse(without.can_write())
        self.assertIn("access token", without.write_blocked_reason())

        with_token = AniListClient(AniListConfig(username="someone", access_token="t"))
        self.assertTrue(with_token.can_write())
        self.assertEqual(with_token.write_blocked_reason(), "")

    def test_category_maps_to_a_list_status(self) -> None:
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        calls: list[tuple[int, str, int | None]] = []
        client.save_entry = lambda media_id, status, progress=None: (  # type: ignore[method-assign]
            calls.append((media_id, status, progress)) or {"id": 1}
        )
        client.add_to_list([{"title": "X", "anilist_id": "16498", "media_type": "tv"}], "watchlist")
        client.add_to_list([{"title": "X", "anilist_id": "16498", "media_type": "tv"}], "collection")
        self.assertEqual(calls[0][:2], (16498, "PLANNING"))
        self.assertEqual(calls[1][:2], (16498, "COMPLETED"))

    def test_completed_entries_get_full_progress(self) -> None:
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        calls: list[int | None] = []
        client.save_entry = lambda media_id, status, progress=None: (  # type: ignore[method-assign]
            calls.append(progress) or {"id": 1}
        )
        client.add_to_list([{
            "anilist_id": "16498", "media_type": "tv", "total_episodes_count": 25,
        }], "collection")
        self.assertEqual(calls, [25])

    @patch("src.fribb_client.lookup_by_tmdb")
    @patch("src.fribb_client.lookup_all_by_tmdb", return_value=[])
    def test_non_anilist_items_are_mapped_through_the_anime_data(self, _lookup_all, lookup_by_tmdb) -> None:
        # A Trakt or PMDB item has no AniList id; it has to be mapped from TMDB.
        lookup_by_tmdb.return_value = {"anilist_id": 164, "mal_id": 164}
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        calls: list[int] = []
        client.save_entry = lambda media_id, status, progress=None: (  # type: ignore[method-assign]
            calls.append(media_id) or {"id": 1}
        )
        totals = client.add_to_list([_movie()], "watchlist")
        self.assertEqual(calls, [164])
        self.assertEqual(totals["added"], 1)

    @patch("src.fribb_client.lookup_all_by_tmdb")
    def test_tmdb_season_selects_the_matching_anilist_entry(self, lookup_all) -> None:
        lookup_all.return_value = [
            {"anilist_id": 100, "season": {"tmdb": 1}},
            {"anilist_id": 200, "season": {"tmdb": 2}},
        ]
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        calls: list[int] = []
        client.save_entry = lambda media_id, status, progress=None: (  # type: ignore[method-assign]
            calls.append(media_id) or {"id": 1}
        )
        client.add_to_list([{"media_type": "tv", "tmdb_id": "9000", "season": 2}], "watchlist")
        self.assertEqual(calls, [200])

    @patch("src.fribb_client.lookup_all_by_tmdb")
    def test_tmdb_episode_selects_the_matching_split_cour(self, lookup_all) -> None:
        lookup_all.return_value = [
            {"anilist_id": 100, "season": {"tmdb": 1}, "episode_offset": {"tmdb": 0}},
            {"anilist_id": 101, "season": {"tmdb": 1}, "episode_offset": {"tmdb": 12}},
        ]
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        self.assertEqual(
            client._resolve_media_id({"media_type": "tv", "tmdb_id": "9000", "season": 1, "episode": 15}),
            101,
        )

    @patch("src.fribb_client.lookup_by_imdb")
    @patch("src.fribb_client.lookup_by_tmdb")
    @patch("src.fribb_client.lookup_all_by_tmdb", return_value=[])
    def test_unmappable_items_are_reported_not_written(self, _lookup_all, lookup_by_tmdb, lookup_by_imdb) -> None:
        lookup_by_tmdb.return_value = None
        lookup_by_imdb.return_value = None
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        client.save_entry = lambda *a, **k: self.fail("must not write without a media id")  # type: ignore[method-assign]
        totals = client.add_to_list([_movie()], "watchlist")
        self.assertEqual(totals["not_found"], 1)
        self.assertEqual(totals["added"], 0)

    def test_removal_uses_the_entry_id_carried_on_the_item(self) -> None:
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        client.get_status = lambda status: []  # type: ignore[method-assign]
        deleted: list[int] = []
        client.delete_entry = lambda entry_id: deleted.append(entry_id) or True  # type: ignore[method-assign]
        totals = client.remove_from_list(
            [{"anilist_id": "16498", "anilist_entry_id": 999}], "watchlist",
        )
        self.assertEqual(deleted, [999])
        self.assertEqual(totals["deleted"], 1)

    def test_removal_falls_back_to_looking_the_entry_up(self) -> None:
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        client.get_status = lambda status: [  # type: ignore[method-assign]
            {"anilist_id": "16498", "anilist_entry_id": 4242},
        ]
        deleted: list[int] = []
        client.delete_entry = lambda entry_id: deleted.append(entry_id) or True  # type: ignore[method-assign]
        totals = client.remove_from_list([{"anilist_id": "16498"}], "watchlist")
        self.assertEqual(deleted, [4242])
        self.assertEqual(totals["deleted"], 1)

    def test_removal_reports_items_it_cannot_locate(self) -> None:
        client = AniListClient(AniListConfig(username="u", access_token="t"))
        client.get_status = lambda status: []  # type: ignore[method-assign]
        client.delete_entry = lambda entry_id: self.fail("nothing to delete")  # type: ignore[method-assign]
        totals = client.remove_from_list([{"anilist_id": "999999"}], "watchlist")
        self.assertEqual(totals["not_found"], 1)


if __name__ == "__main__":
    unittest.main()


class EpisodeScopedHistoryTests(unittest.TestCase):
    """Watching one episode must never mark the whole show watched.

    Every one of these services reads a show entry with no seasons tree as *the
    entire show*. A history row that names only the series is therefore not a
    smaller write, it is a much larger one: it claims every season the user has
    never seen. Such a row is dropped and reported, never flattened and sent.
    """

    def test_trakt_history_drops_a_show_row_with_no_episode(self) -> None:
        payload = TraktClient._build_sync_payload(
            [_episode(None, None)], with_watched_at=True, episode_scoped=True,
        )
        self.assertNotIn("shows", payload)
        self.assertEqual(payload["_dropped"], 1)

    def test_trakt_history_keeps_the_episodes_beside_a_dropped_row(self) -> None:
        payload = TraktClient._build_sync_payload(
            [_episode(None, None), _episode(1, 3)],
            with_watched_at=True, episode_scoped=True,
        )
        seasons = payload["shows"][0]["seasons"]
        self.assertEqual([e["number"] for e in seasons[0]["episodes"]], [3])
        self.assertEqual(payload["_dropped"], 1)

    def test_trakt_watchlist_still_sends_the_whole_show(self) -> None:
        # Only history is episode-scoped; a watchlist entry *is* the series.
        payload = TraktClient._build_sync_payload([_episode(None, None)])
        self.assertEqual(payload["shows"], [{"ids": {"tmdb": 1429}}])

    def test_trakt_add_to_history_reports_the_dropped_row(self) -> None:
        client = TraktClient(TraktConfig(client_id="c", access_token="t"))
        with patch.object(TraktClient, "_post", return_value={"added": {"episodes": 1}}) as post:
            totals = client.add_to_history([_episode(None, None), _episode(1, 3)])
        self.assertEqual(totals["not_found"], 1)
        self.assertNotIn("_dropped", post.call_args[0][1])

    def test_simkl_history_drops_a_show_row_with_no_episode(self) -> None:
        payload = SimklClient._build_sync_payload(
            [_episode(None, None)], with_watched_at=True, episode_scoped=True,
        )
        self.assertNotIn("shows", payload)
        self.assertEqual(payload["_dropped"], 1)

    def test_simkl_stamps_each_episode_rather_than_the_show(self) -> None:
        # Episodes of one show share an entry, so a show-level watched_at would
        # put the first episode's date on every other episode in the batch.
        payload = SimklClient._build_sync_payload(
            [
                _episode(1, 1, watched_at="2024-01-01T00:00:00Z"),
                _episode(1, 2, watched_at="2024-02-02T00:00:00Z"),
            ],
            with_watched_at=True, episode_scoped=True,
        )
        show = payload["shows"][0]
        self.assertNotIn("watched_at", show)
        episodes = show["seasons"][0]["episodes"]
        self.assertEqual(
            [e["watched_at"] for e in episodes],
            ["2024-01-01T00:00:00Z", "2024-02-02T00:00:00Z"],
        )

    def test_simkl_watchlist_still_sends_the_whole_show(self) -> None:
        payload = SimklClient._build_sync_payload([_episode(None, None)], to_list="plantowatch")
        self.assertEqual(len(payload["shows"]), 1)
        self.assertNotIn("seasons", payload["shows"][0])

    def test_mdblist_history_sends_an_episode_tree(self) -> None:
        payload = MdbListClient._to_sync_payload(
            [_episode(1, 3, imdb_id="tt2560140", watched_at="2024-01-01T00:00:00Z")],
            episode_scoped=True,
        )
        seasons = payload["shows"][0]["seasons"]
        self.assertEqual(seasons[0]["number"], 1)
        self.assertEqual(seasons[0]["episodes"][0]["number"], 3)
        self.assertEqual(payload["_dropped"], 0)

    def test_mdblist_history_drops_a_show_row_with_no_episode(self) -> None:
        payload = MdbListClient._to_sync_payload(
            [_episode(None, None, imdb_id="tt2560140")], episode_scoped=True,
        )
        self.assertEqual(payload["shows"], [])
        self.assertEqual(payload["_dropped"], 1)

    def test_mdblist_reads_watched_episodes_back_one_row_each(self) -> None:
        # Flattening the tree to the series would make every episode look
        # unsynced and the pair would rewrite the whole show on every run.
        rows = MdbListClient._from_sync_payload({
            "shows": [{
                "show": {"title": "Show", "ids": {"tmdb": 1429}},
                "seasons": [{"number": 1, "episodes": [
                    {"number": 1, "watched_at": "2024-01-01T00:00:00Z"},
                    {"number": 2, "watched_at": "2024-01-02T00:00:00Z"},
                ]}],
            }],
        }, "history")
        self.assertEqual([(r["season"], r["episode"]) for r in rows], [(1, 1), (1, 2)])
        self.assertEqual(rows[1]["watched_at"], "2024-01-02T00:00:00Z")
