"""Tests for the Fribb offline mapping data layer.

The shapes exercised here are the ones the upstream feed actually uses, verified
against ``Fribb/anime-lists`` ``anime-list-full.json``:

* ``themoviedb_id`` is always a dict, keyed by media type, and the two keys carry
  different value shapes — ``{"tv": int}`` and ``{"movie": [int, ...]}``.
* ``imdb_id`` is always a list, and a minority of entries carry more than one.

Both used to be read as if they were scalars, which silently discarded every
anime-movie mapping and every IMDB mapping.
"""

import unittest

from src.anime_mapping_store import AnimeMappingStore, _iter_imdb_ids, _safe_int, extract_tmdb


class ExtractTmdbTests(unittest.TestCase):
    def test_tv_entries_are_scalars(self) -> None:
        self.assertEqual(extract_tmdb({"tv": 26209}), (26209, "tv"))

    def test_movie_entries_are_single_element_lists(self) -> None:
        # Princess Mononoke: the shape that used to raise TypeError and be
        # swallowed, making every anime movie unresolvable.
        self.assertEqual(extract_tmdb({"movie": [128]}), (128, "movie"))

    def test_ambiguous_movie_lists_are_refused(self) -> None:
        # 22 entries upstream carry more than one movie id. Guessing would risk
        # writing the wrong film, so the id is dropped and the media type kept.
        self.assertEqual(extract_tmdb({"movie": [1, 2]}), (None, "movie"))

    def test_missing_and_empty_values(self) -> None:
        self.assertEqual(extract_tmdb(None), (None, None))
        self.assertEqual(extract_tmdb(""), (None, None))
        self.assertEqual(extract_tmdb({}), (None, None))

    def test_bare_scalar_is_tolerated_with_unknown_media_type(self) -> None:
        self.assertEqual(extract_tmdb(1234), (1234, None))
        self.assertEqual(extract_tmdb("1234"), (1234, None))

    def test_safe_int_unwraps_single_element_lists(self) -> None:
        self.assertEqual(_safe_int({"movie": [128]}), 128)
        self.assertIsNone(_safe_int({"movie": [1, 2]}))
        self.assertEqual(_safe_int([7]), 7)
        self.assertIsNone(_safe_int([7, 8]))


class IterImdbIdsTests(unittest.TestCase):
    def test_list_is_flattened_to_strings(self) -> None:
        self.assertEqual(_iter_imdb_ids(["tt0119698"]), ["tt0119698"])

    def test_multiple_ids_are_all_kept(self) -> None:
        self.assertEqual(_iter_imdb_ids(["tt1", "tt2"]), ["tt1", "tt2"])

    def test_blanks_and_none_are_dropped(self) -> None:
        self.assertEqual(_iter_imdb_ids(["tt1", "", None, "  "]), ["tt1"])
        self.assertEqual(_iter_imdb_ids(None), [])

    def test_bare_string_is_tolerated(self) -> None:
        self.assertEqual(_iter_imdb_ids("tt0119698"), ["tt0119698"])


class FribbIndexTests(unittest.TestCase):
    """Index construction over a hand-built feed using real upstream shapes."""

    FEED = [
        {
            "type": "MOVIE",
            "anilist_id": 164,
            "mal_id": 164,
            "anidb_id": 7,
            "simkl_id": 36228,
            "imdb_id": ["tt0119698"],
            "themoviedb_id": {"movie": [128]},
        },
        {
            "type": "TV",
            "anilist_id": 20,
            "mal_id": 20,
            "imdb_id": ["tt0409591"],
            "tvdb_id": 78857,
            "themoviedb_id": {"tv": 46260},
        },
        {
            # Multi-IMDB entry: both ids must resolve back to this entry.
            "type": "OVA",
            "anilist_id": 555,
            "imdb_id": ["tt1000001", "tt1000002"],
            "themoviedb_id": {"movie": [999]},
        },
    ]

    def _loaded_store(self) -> AnimeMappingStore:
        store = AnimeMappingStore()

        def fake_load(url, cache_path, meta_path):
            return True, self.FEED

        store._load_or_download_json_with_change = fake_load  # type: ignore[method-assign]
        store._ensure_fribb_loaded()
        return store

    def test_movie_entries_are_indexed_by_tmdb(self) -> None:
        store = self._loaded_store()
        entry = store.lookup_fribb(tmdb_id=128)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["anilist_id"], 164)

    def test_tv_entries_are_indexed_by_tmdb(self) -> None:
        store = self._loaded_store()
        entry = store.lookup_fribb(tmdb_id=46260)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["anilist_id"], 20)

    def test_imdb_lookup_resolves_list_valued_ids(self) -> None:
        store = self._loaded_store()
        entry = store.lookup_fribb(imdb_id="tt0119698")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["anilist_id"], 164)

    def test_every_id_of_a_multi_imdb_entry_resolves(self) -> None:
        store = self._loaded_store()
        for imdb_id in ("tt1000001", "tt1000002"):
            entry = store.lookup_fribb(imdb_id=imdb_id)
            self.assertIsNotNone(entry, imdb_id)
            self.assertEqual(entry["anilist_id"], 555)

    def test_tvdb_lookup(self) -> None:
        store = self._loaded_store()
        entry = store.lookup_fribb(tvdb_id=78857)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["anilist_id"], 20)

    def test_validate_tmdb_accepts_movie_mappings(self) -> None:
        store = self._loaded_store()
        entry = store.lookup_fribb(anilist_id=164)
        self.assertTrue(store.validate_tmdb(entry, 128))
        self.assertFalse(store.validate_tmdb(entry, 129))


class XmlTmdbCoordinateTests(unittest.TestCase):
    """The XML carries TMDB coordinates directly; they used to be ignored.

    ``tmdbtv`` and ``tmdbseason`` co-occur on every entry that has either, so a
    TVDB season no longer has to be translated through PMDB anime-seasons.
    """

    XML = """<?xml version="1.0" encoding="UTF-8"?>
    <anime-list>
      <anime anidbid="100" tvdbid="70000" defaulttvdbseason="2"
             episodeoffset="12" tmdbtv="55555" tmdbseason="3" tmdboffset="5" />
      <anime anidbid="200" tvdbid="70001" defaulttvdbseason="1" />
      <anime anidbid="300" tvdbid="70002" defaulttvdbseason="0"
             episodeoffset="2" tmdbtv="55557" tmdbseason="0" tmdboffset="2" />
      <anime anidbid="400" tmdbid="128" tvdbid="70003" defaulttvdbseason="1" />
    </anime-list>
    """

    def _loaded_store(self) -> AnimeMappingStore:
        store = AnimeMappingStore()

        def fake_load(url, cache_path, meta_path):
            return True, self.XML

        store._load_or_download_text_with_change = fake_load  # type: ignore[method-assign]
        store._ensure_xml_loaded()
        return store

    def test_tmdb_coordinates_are_returned_when_present(self) -> None:
        store = self._loaded_store()
        result = store.resolve_tvdb_episode_from_anidb_episode(100, 3)
        self.assertIsNotNone(result)
        # TVDB coordinates stay available for the fallback path...
        self.assertEqual(result["tvdb_season"], 2)
        self.assertEqual(result["tvdb_episode"], 15)  # 3 + episodeoffset 12
        # ...alongside the direct TMDB answer.
        self.assertEqual(result["tmdb_season"], 3)
        self.assertEqual(result["tmdb_episode"], 8)  # 3 + tmdboffset 5
        self.assertEqual(result["tmdb_id"], 55555)

    def test_entries_without_tmdb_attributes_are_unchanged(self) -> None:
        store = self._loaded_store()
        result = store.resolve_tvdb_episode_from_anidb_episode(200, 4)
        self.assertIsNotNone(result)
        self.assertEqual(result["tvdb_season"], 1)
        self.assertNotIn("tmdb_season", result)

    def test_specials_are_left_untouched(self) -> None:
        store = self._loaded_store()
        self.assertIsNone(store.resolve_tvdb_episode_from_anidb_episode(300, 1))

    def test_movie_entries_are_indexed_by_tmdbid(self) -> None:
        store = self._loaded_store()
        entries = store.get_xml_entries_by_tmdb(128)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].attrib["anidbid"], "400")


if __name__ == "__main__":
    unittest.main()
