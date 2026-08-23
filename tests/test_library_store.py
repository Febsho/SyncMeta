import tempfile
import unittest
from pathlib import Path

from src.library_store import LibraryStore, series_key
from src.media_kind import (
    KIND_ANIME,
    KIND_ANIME_MOVIE,
    KIND_MOVIE,
    KIND_SHOW,
    classify,
    matches_filter,
)
from src.providers import (
    CATEGORY_COLLECTION,
    CATEGORY_HISTORY,
    CATEGORY_RESUME,
    CATEGORY_WATCHLIST,
    LibraryAdapter,
)


class MediaKindTests(unittest.TestCase):
    def test_the_four_kinds(self) -> None:
        self.assertEqual(classify({"media_type": "movie"}), KIND_MOVIE)
        self.assertEqual(classify({"media_type": "tv"}), KIND_SHOW)
        self.assertEqual(classify({"media_type": "tv", "anilist_id": 1}), KIND_ANIME)
        self.assertEqual(classify({"media_type": "movie", "mal_id": 2}), KIND_ANIME_MOVIE)

    def test_an_anime_film_is_not_stored_as_tv(self) -> None:
        # SIMKL calls it "anime", which is true about the anime-ness and wrong
        # about the namespace — taking it at face value is what turned anime
        # films into fake one-episode shows.
        item = {"media_type": "anime", "simkl_type": "anime", "anilist_id": 21519}
        self.assertEqual(classify(item, namespace="movie"), KIND_ANIME_MOVIE)

    def test_anime_is_detected_from_nested_ids(self) -> None:
        self.assertEqual(classify({"media_type": "tv", "ids": {"mal": 5}}), KIND_ANIME)

    def test_an_untouched_filter_hides_nothing(self) -> None:
        for selection in ([], None, "", ["all"]):
            self.assertTrue(matches_filter(KIND_ANIME, selection), selection)

    def test_a_filter_selects_exactly_its_kinds(self) -> None:
        self.assertTrue(matches_filter(KIND_ANIME_MOVIE, ["anime_movie", "movie"]))
        self.assertFalse(matches_filter(KIND_ANIME, ["anime_movie", "movie"]))


class SeriesKeyTests(unittest.TestCase):
    def test_tmdb_is_preferred(self) -> None:
        self.assertEqual(series_key({"media_type": "tv", "tmdb_id": "1429"}), "tmdb:tv:1429")

    def test_the_same_series_from_two_providers_keys_the_same(self) -> None:
        # This is the whole point of the series shape: SIMKL lists an anime per
        # season and AniList per cour, and keyed naively that is three rows.
        simkl = {"media_type": "tv", "tmdb_id": "1429", "season": 1, "title": "Attack on Titan"}
        anilist_s2 = {"media_type": "tv", "tmdb_id": "1429", "season": 2, "title": "Shingeki no Kyojin S2"}
        self.assertEqual(series_key(simkl), series_key(anilist_s2))

    def test_an_unmapped_anime_still_has_a_key(self) -> None:
        self.assertEqual(series_key({"media_type": "tv", "anilist_id": 16498}), "anilist:16498")

    def test_movies_and_shows_do_not_collide_on_the_same_tmdb_id(self) -> None:
        self.assertNotEqual(
            series_key({"media_type": "movie", "tmdb_id": "5"}),
            series_key({"media_type": "tv", "tmdb_id": "5"}),
        )

    def test_an_item_with_nothing_usable_has_no_key(self) -> None:
        self.assertEqual(series_key({}), "")


class LibraryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = LibraryStore(Path(self._dir.name) / "library.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _aot(self, **extra) -> dict:
        return {"media_type": "tv", "tmdb_id": "1429", "title": "Attack on Titan", **extra}

    def test_items_round_trip_through_the_file(self) -> None:
        self.store.add(CATEGORY_WATCHLIST, [self._aot()])
        reopened = LibraryStore(self.store.path)
        self.assertEqual(len(reopened.fetch(CATEGORY_WATCHLIST)), 1)

    def test_provider_status_is_persisted_for_library_inspector(self) -> None:
        self.store.add(CATEGORY_COLLECTION, [self._aot(
            simkl_id=39687,
            _syncmeta_source_provider="simkl",
            _syncmeta_source_status="completed",
        )], source="pair")
        state = LibraryStore(self.store.path).entries()[0]["provider_states"]["simkl"]
        self.assertEqual(state["status"], "Completed")
        self.assertEqual(state["provider_id"], "39687")

    def test_two_seasons_of_one_anime_become_one_entry(self) -> None:
        self.store.add(CATEGORY_WATCHLIST, [
            self._aot(season=1, anilist_id=16498),
            self._aot(season=2, anilist_id=25777, title="Shingeki no Kyojin Season 2"),
        ])
        entries = self.store.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(sorted(entries[0]["seasons"]), ["1", "2"])

    def test_a_later_source_fills_gaps_but_never_overwrites(self) -> None:
        # Otherwise the last service to sync decides the title, and the entry
        # flips between romaji and English on every run.
        self.store.add(CATEGORY_WATCHLIST, [self._aot()])
        self.store.add(CATEGORY_WATCHLIST, [self._aot(title="Shingeki no Kyojin", imdb_id="tt2560140")])
        entry = self.store.entries()[0]
        self.assertEqual(entry["title"], "Attack on Titan")
        self.assertEqual(entry["imdb_id"], "tt2560140")

    def test_anime_ness_is_sticky(self) -> None:
        # A source that does not model anime must not downgrade an entry another
        # source already identified as anime.
        self.store.add(CATEGORY_WATCHLIST, [self._aot(anilist_id=16498)])
        self.store.add(CATEGORY_COLLECTION, [self._aot()])
        self.assertEqual(self.store.entries()[0]["kind"], KIND_ANIME)

    def test_sections_are_independent(self) -> None:
        self.store.add(CATEGORY_WATCHLIST, [self._aot()])
        self.store.add(CATEGORY_COLLECTION, [self._aot()])
        self.store.remove(CATEGORY_WATCHLIST, [self._aot()])
        self.assertEqual(self.store.fetch(CATEGORY_WATCHLIST), [])
        self.assertEqual(len(self.store.fetch(CATEGORY_COLLECTION)), 1)

    def test_an_entry_with_nothing_left_disappears(self) -> None:
        self.store.add(CATEGORY_WATCHLIST, [self._aot()])
        self.store.remove(CATEGORY_WATCHLIST, [self._aot()])
        self.assertEqual(self.store.entries(), [])

    def test_watch_history_is_per_episode(self) -> None:
        self.store.mark_watched([
            self._aot(season=1, episode=1, watched_at="2024-01-01T00:00:00Z"),
            self._aot(season=1, episode=2, watched_at="2024-01-02T00:00:00Z"),
        ])
        entry = self.store.entries()[0]
        self.assertEqual(sorted(entry["watched"]), ["1x1", "1x2"])

    def test_a_show_play_with_no_episode_is_dropped_not_guessed(self) -> None:
        # Inventing an episode number would claim episodes nobody watched.
        result = self.store.mark_watched([self._aot()])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["not_found"], 1)

    def test_a_movie_needs_no_episode(self) -> None:
        self.store.mark_watched([{"media_type": "movie", "tmdb_id": "128", "title": "Mononoke"}])
        self.assertEqual(list(self.store.entries()[0]["watched"]), ["0x0"])

    def test_replaying_an_episode_does_not_double_count(self) -> None:
        item = self._aot(season=1, episode=1)
        self.store.mark_watched([item])
        second = self.store.mark_watched([item])
        self.assertEqual(second["added"], 0)

    def test_history_reads_back_one_row_per_episode(self) -> None:
        self.store.mark_watched([
            self._aot(season=1, episode=1),
            self._aot(season=1, episode=2),
        ])
        history = self.store.fetch("history")
        self.assertEqual(len(history), 2)
        self.assertEqual({row["episode"] for row in history}, {1, 2})

    def test_specials_are_kept(self) -> None:
        self.store.mark_watched([self._aot(season=0, episode=1)])
        self.assertEqual(list(self.store.entries()[0]["watched"]), ["0x1"])

    def test_counts_are_grouped_by_kind(self) -> None:
        self.store.add(CATEGORY_WATCHLIST, [
            self._aot(anilist_id=16498),
            {"media_type": "movie", "tmdb_id": "128", "title": "Mononoke", "mal_id": 164},
            {"media_type": "movie", "tmdb_id": "27205", "title": "Inception"},
        ])
        counts = self.store.counts()
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["by_kind"][KIND_ANIME], 1)
        self.assertEqual(counts["by_kind"][KIND_ANIME_MOVIE], 1)
        self.assertEqual(counts["by_kind"][KIND_MOVIE], 1)

    def test_resume_progress_round_trips_and_updates(self) -> None:
        first = self._aot(season=1, episode=3, position_ms=12_000, runtime_ms=24_000)
        later = {**first, "position_ms": 18_000}
        self.assertEqual(self.store.save_resume([first])["added"], 1)
        self.assertEqual(self.store.save_resume([first])["added"], 0)
        self.assertEqual(self.store.save_resume([later])["added"], 1)
        rows = LibraryStore(self.store.path).fetch("resume")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position_ms"], 18_000)

    def test_resume_keeps_an_otherwise_empty_entry_until_removed(self) -> None:
        item = self._aot(season=1, episode=3, position_ms=12_000, runtime_ms=24_000)
        self.store.save_resume([item])
        self.assertEqual(len(self.store.fetch("all")), 1)
        self.store.remove_resume([item])
        self.assertEqual(self.store.entries(), [])

    def test_a_corrupt_file_starts_empty_rather_than_crashing(self) -> None:
        self.store.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(LibraryStore(self.store.path).entries(), [])


class LibraryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = LibraryStore(Path(self._dir.name) / "library.json")
        self.adapter = LibraryAdapter(self.store)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_it_reads_and_writes_every_category(self) -> None:
        self.assertEqual(
            set(self.adapter.writable_categories()),
            {CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION, CATEGORY_RESUME},
        )

    def test_it_is_always_writable(self) -> None:
        # No credential, no rate limit — that is what makes it usable as the hub.
        self.assertTrue(self.adapter.can_write())
        self.assertEqual(self.adapter.write_blocked_reason(), "")

    def test_a_write_then_read_round_trips(self) -> None:
        self.adapter.add(CATEGORY_WATCHLIST, [{"media_type": "tv", "tmdb_id": "1429", "title": "AoT"}])
        items = self.adapter.fetch(CATEGORY_WATCHLIST)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tmdb_id"], "1429")

    def test_a_selection_naming_another_category_reads_nothing(self) -> None:
        # Falling back to the whole category is how a pair silently syncs far
        # more than it was asked to.
        self.adapter.add(CATEGORY_WATCHLIST, [{"media_type": "tv", "tmdb_id": "1", "title": "x"}])
        self.assertEqual(self.adapter.fetch(CATEGORY_WATCHLIST, ["section:collection"]), [])
        self.assertEqual(len(self.adapter.fetch(CATEGORY_WATCHLIST, ["section:watchlist"])), 1)

    def test_an_empty_selection_means_the_default(self) -> None:
        self.adapter.add(CATEGORY_WATCHLIST, [{"media_type": "tv", "tmdb_id": "1", "title": "x"}])
        self.assertEqual(len(self.adapter.fetch(CATEGORY_WATCHLIST, [])), 1)

    def test_all_titles_source_includes_every_library_section(self) -> None:
        self.adapter.add(CATEGORY_COLLECTION, [{"media_type": "movie", "tmdb_id": "2", "title": "x"}])
        self.assertEqual(len(self.adapter.fetch(CATEGORY_WATCHLIST, ["section:all"])), 1)

    def test_resume_writes_go_through_resume_state(self) -> None:
        item = {"media_type": "tv", "tmdb_id": "1429", "season": 1, "episode": 3,
                "title": "AoT", "position_ms": 12_000, "runtime_ms": 24_000}
        self.adapter.add(CATEGORY_RESUME, [item])
        self.assertEqual(self.adapter.fetch(CATEGORY_RESUME)[0]["position_ms"], 12_000)

    def test_history_writes_go_through_watched_state(self) -> None:
        self.adapter.add(CATEGORY_HISTORY, [
            {"media_type": "tv", "tmdb_id": "1429", "season": 1, "episode": 3, "title": "AoT"},
        ])
        self.assertEqual(len(self.adapter.fetch(CATEGORY_HISTORY)), 1)

    def test_corrected_anime_episode_removes_legacy_season_one_overflow(self) -> None:
        stale = {
            "title": "Mapped Anime", "media_type": "tv", "tmdb_id": "9000",
            "season": 1, "episode": 29,
        }
        corrected = {
            "title": "Mapped Anime", "media_type": "tv", "tmdb_id": "9000",
            "season": 2, "episode": 1, "_syncmeta_replaces_episode": "1x29",
        }
        self.adapter.add(CATEGORY_HISTORY, [stale])

        self.adapter.add(CATEGORY_HISTORY, [corrected])
        rows = self.adapter.fetch(CATEGORY_HISTORY)

        self.assertEqual([(row["season"], row["episode"]) for row in rows], [(2, 1)])

    def test_removing_history_unmarks_the_episode(self) -> None:
        item = {"media_type": "tv", "tmdb_id": "1429", "season": 1, "episode": 3, "title": "AoT"}
        self.adapter.add(CATEGORY_HISTORY, [item])
        self.adapter.remove(CATEGORY_HISTORY, [item])
        self.assertEqual(self.adapter.fetch(CATEGORY_HISTORY), [])

    def test_it_offers_no_target_lists(self) -> None:
        self.assertFalse(self.adapter.supports_target_lists)
        self.assertEqual(self.adapter.describe()["target_list_categories"], [])


if __name__ == "__main__":
    unittest.main()
