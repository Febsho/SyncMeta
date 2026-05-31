import unittest
from unittest.mock import patch

from src.matcher import ItemMatcher


class StubPMDBClient:
    def __init__(self):
        self.calls = []

    def lookup_by_external_id(self, id_type: str, id_value: str, media_type: str) -> int | None:
        self.calls.append((id_type, id_value, media_type))
        if (id_type, id_value, media_type) == ("mal", "48675", "tv"):
            return 68028
        return None


class DetailedStubPMDBClient(StubPMDBClient):
    def lookup_by_external_id_detailed(self, id_type: str, id_value: str, media_type: str) -> dict:
        self.calls.append((id_type, id_value, media_type))
        return {"tmdb_id": None, "status": "lookup_unavailable"}


class ItemMatcherTests(unittest.TestCase):
    def test_initial_cache_normalizes_media_typed_tmdb_id(self) -> None:
        item = {
            "title": "Demo",
            "year": 2024,
            "media_type": "tv",
        }
        matcher = ItemMatcher(
            StubPMDBClient(),
            initial_cache={ItemMatcher._cache_key(item): {"tv": 63926}},
        )

        result = matcher.resolve_match(item)

        self.assertEqual(result.tmdb_id, 63926)
        self.assertEqual(result.resolution_kind, "cache")

    def test_non_anime_falls_back_to_root_series_ids(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "Example Franchise Sequel",
            "year": 2025,
            "media_type": "tv",
            "root_mal_id": "48675",
            "root_anilist_id": "140960",
            "root_title": "Example Root",
            "ids": {
                "root_mal": 48675,
                "root_anilist": 140960,
            },
        })

        self.assertEqual(tmdb_id, 68028)

    def test_anime_does_not_fall_back_to_root_series_ids(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        result = matcher.resolve_match({
            "title": "SPY x FAMILY Season 3",
            "year": 2025,
            "media_type": "tv",
            "simkl_type": "anime",
            "mal_id": "59027",
            "anilist_id": "177937",
            "root_mal_id": "48675",
            "root_anilist_id": "140960",
            "root_title": "SPY x FAMILY",
            "ids": {
                "mal": 59027,
                "anilist": 177937,
                "root_mal": 48675,
                "root_anilist": 140960,
            },
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.unresolved_reason, "not_found")

    def test_anime_prefers_anilist_before_imdb(self) -> None:
        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            if (id_type, id_value, media_type) == ("anilist", "12345", "tv"):
                return 777
            if (id_type, id_value, media_type) == ("imdb", "tt123", "tv"):
                return 888
            return None

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "Example Anime",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "imdb_id": "tt123",
            "anilist_id": "12345",
        })

        self.assertEqual(tmdb_id, 777)
        self.assertEqual(client.calls[0], ("anilist", "12345", "tv"))

    def test_resolve_match_reports_missing_ids(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        result = matcher.resolve_match({
            "title": "No IDs Anime",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "ids": {},
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.unresolved_reason, "missing_ids")

    def test_resolve_match_reports_lookup_unavailable(self) -> None:
        matcher = ItemMatcher(DetailedStubPMDBClient())

        result = matcher.resolve_match({
            "title": "Unavailable Mapping Anime",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "12345",
            "ids": {"anilist": "12345"},
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.unresolved_reason, "lookup_unavailable")

    def test_simkl_anime_prefers_external_mapping_before_direct_tmdb(self) -> None:
        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            if (id_type, id_value, media_type) == ("anilist", "12345", "tv"):
                return 777
            return None

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(
            client,
            anime_root_resolver=lambda anilist_id, mal_id: {
                "root": {"id": anilist_id or 999, "idMal": mal_id},
                "episode_offset": 0,
            },
        )

        result = matcher.resolve_match({
            "title": "Verified Anime",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "tmdb_id": "999999",
            "anilist_id": "12345",
            "ids": {"anilist": "12345"},
        })

        self.assertEqual(result.tmdb_id, 777)
        self.assertEqual(result.resolution_kind, "external_mapping")

    def test_simkl_anime_rejects_unverified_direct_tmdb(self) -> None:
        matcher = ItemMatcher(
            StubPMDBClient(),
            anime_root_resolver=lambda anilist_id, mal_id: None,
        )

        result = matcher.resolve_match({
            "title": "Suspicious Anime Entry",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "tmdb_id": "1575337",
            "anilist_id": "12345",
            "ids": {"anilist": "12345"},
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.unresolved_reason, "not_found")

    def test_simkl_anime_allows_verified_direct_tmdb_after_mapping_miss(self) -> None:
        matcher = ItemMatcher(
            StubPMDBClient(),
            anime_root_resolver=lambda anilist_id, mal_id: {
                "root": {"id": anilist_id or 999, "idMal": mal_id},
                "episode_offset": 0,
            },
        )

        result = matcher.resolve_match({
            "title": "Verified Direct TMDB Anime",
            "year": 2026,
            "media_type": "movie",
            "simkl_type": "anime",
            "tmdb_id": "1575337",
            "anilist_id": "999",
            "ids": {"anilist": "999"},
        })

        self.assertEqual(result.tmdb_id, 1575337)
        self.assertEqual(result.resolution_kind, "direct_tmdb")

    def test_anime_sequel_keeps_exact_mapping_instead_of_root_series(self) -> None:
        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            if (id_type, id_value, media_type) == ("anilist", "1735", "tv"):
                return 31910
            if (id_type, id_value, media_type) == ("anilist", "20", "tv"):
                return 46260
            return None

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(
            client,
            anime_root_resolver=lambda anilist_id, mal_id: {
                "root": {"id": 20, "idMal": 20},
                "episode_offset": 0,
            },
        )

        result = matcher.resolve_match({
            "title": "Naruto Shippuden",
            "year": 2007,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "1735",
            "ids": {"anilist": "1735"},
        })

        self.assertEqual(result.tmdb_id, 31910)
        self.assertEqual(result.resolution_kind, "external_mapping")
        self.assertEqual(client.calls[0], ("anilist", "1735", "tv"))

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_prefers_exact_fribb_mapping_over_bad_external_match(self, lookup_by_anilist) -> None:
        lookup_by_anilist.return_value = {
            "anilist_id": 1735,
            "themoviedb": "31910",
            "type": "TV",
        }

        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            return 154634

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Naruto Shippuden",
            "year": 2007,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "1735",
            "ids": {"anilist": "1735"},
        })

        self.assertEqual(result.tmdb_id, 31910)
        self.assertEqual(result.resolution_kind, "fribb_exact")
        self.assertEqual(client.calls, [])

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_list_identity_blocks_unverified_external_mapping(self, lookup_by_anilist) -> None:
        lookup_by_anilist.return_value = None

        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            return 154634

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "ICE 3",
            "year": 2022,
            "media_type": "movie",
            "simkl_type": "anime",
            "anilist_id": "999001",
            "ids": {"anilist": "999001"},
            "anime_resolve_mode": "list_identity",
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.resolution_kind, "unresolved")
        self.assertEqual(result.unresolved_reason, "missing_anime_mapping")
        self.assertEqual(client.calls, [("anilist", "999001", "movie")])

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_list_identity_rejects_incompatible_verified_external_title(self, lookup_by_anilist) -> None:
        lookup_by_anilist.return_value = None

        client = StubPMDBClient()

        def fake_lookup_detailed(id_type: str, id_value: str, media_type: str) -> dict:
            client.calls.append((id_type, id_value, media_type))
            if (id_type, id_value, media_type) == ("anilist", "38691", "tv"):
                return {
                    "tmdb_id": 68028,
                    "status": "hit",
                    "votes": 12,
                    "title": "Vazquez vs Marquez I",
                }
            return {"tmdb_id": None, "status": "miss"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Dr. Stone",
            "year": 2019,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "38691",
            "ids": {"anilist": "38691"},
            "anime_resolve_mode": "list_identity",
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.resolution_kind, "unresolved")
        self.assertEqual(result.unresolved_reason, "missing_anime_mapping")
        self.assertEqual(client.calls, [("anilist", "38691", "tv")])


    def test_cache_key_includes_anilist_id_so_stale_entries_are_invalidated(self) -> None:
        # Two items that differ only in anilist_id must produce different cache keys
        # so that a stale wrong resolution for one (e.g. Boruto cached as Naruto)
        # cannot collide with or mask the other.
        boruto = {
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "97938",
            "mal_id": "34566",
            "ids": {"anilist": 97938, "mal": 34566},
        }
        naruto = {
            "title": "Naruto",
            "year": 2002,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "20",
            "mal_id": "1735",
            "ids": {"anilist": 20, "mal": 1735},
        }
        # Also verify that an item with the same MAL/title/year but different
        # anilist_id gets a different key (guards against franchise-root collision).
        fake_boruto_no_anilist = {
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "99999",
            "mal_id": "34566",
            "ids": {"anilist": 99999, "mal": 34566},
        }
        key_boruto = ItemMatcher._cache_key(boruto)
        key_naruto = ItemMatcher._cache_key(naruto)
        key_fake = ItemMatcher._cache_key(fake_boruto_no_anilist)

        self.assertNotEqual(key_boruto, key_naruto)
        self.assertNotEqual(key_boruto, key_fake)
        self.assertIn("97938", key_boruto)

    def test_highest_voted_pmdb_result_resolves_sequel_correctly(self) -> None:
        # Simulate PMDB returning two results for Boruto's AniList ID: the
        # wrong franchise-root mapping (Naruto, low votes) first, and the
        # correct per-show mapping (Boruto, high votes) second.
        client = StubPMDBClient()

        def fake_lookup_detailed(id_type: str, id_value: str, media_type: str) -> dict:
            client.calls.append((id_type, id_value, media_type))
            if (id_type, id_value, media_type) == ("anilist", "97938", "tv"):
                from src.publicmetadb_client import PublicMetaDBClient, PublicMetaDBConfig
                pmdb = PublicMetaDBClient(PublicMetaDBConfig(api_key="x"))
                import types

                def fake_get(path, params=None):
                    return {
                        "results": [
                            {"tmdb_id": 46260, "votes": 1},   # Naruto
                            {"tmdb_id": 65930, "votes": 10},  # Boruto
                        ]
                    }

                pmdb._get = fake_get  # type: ignore[method-assign]
                return pmdb.lookup_by_external_id_detailed(id_type, id_value, media_type)
            return {"tmdb_id": None, "status": "miss"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "97938",
            "mal_id": "34566",
            "ids": {"anilist": 97938, "mal": 34566},
        })

        self.assertEqual(result.tmdb_id, 65930)
        self.assertEqual(result.resolution_kind, "external_mapping")

    def test_prefer_root_series_uses_root_before_direct_tmdb(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "SPY x FAMILY Season 3",
            "year": 2025,
            "media_type": "tv",
            "tmdb_id": "999999",
            "mal_id": "59027",
            "anilist_id": "177937",
            "root_mal_id": "48675",
            "root_anilist_id": "140960",
            "root_title": "SPY x FAMILY",
            "prefer_root_series": True,
            "ids": {
                "tmdb": 999999,
                "mal": 59027,
                "anilist": 177937,
                "root_mal": 48675,
                "root_anilist": 140960,
            },
        })

        self.assertEqual(tmdb_id, 68028)

    def test_anime_sequel_can_keep_direct_mapping_even_with_root_ids(self) -> None:
        client = StubPMDBClient()
        matcher = ItemMatcher(client)

        def fake_lookup_detailed(id_type: str, ext_id: str, media_type: str):
            if id_type == "anilist" and ext_id == "177937" and media_type == "tv":
                return {"tmdb_id": 999999, "status": "hit"}
            if id_type == "mal" and ext_id == "48675" and media_type == "tv":
                return {"tmdb_id": 68028, "status": "hit"}
            if id_type == "anilist" and ext_id == "140960" and media_type == "tv":
                return {"tmdb_id": 68028, "status": "hit"}
            return {"tmdb_id": None, "status": "miss"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]

        result = matcher.resolve_match({
            "title": "SPY x FAMILY Season 3",
            "year": 2025,
            "media_type": "tv",
            "simkl_type": "anime",
            "tmdb_id": "999999",
            "anilist_id": "177937",
            "mal_id": "59027",
            "root_mal_id": "48675",
            "root_anilist_id": "140960",
            "root_title": "SPY x FAMILY",
            "prefer_root_series": False,
            "ids": {
                "tmdb": 999999,
                "anilist": 177937,
                "mal": 59027,
                "root_mal": 48675,
                "root_anilist": 140960,
            },
        })

        self.assertEqual(result.tmdb_id, 999999)
        self.assertEqual(result.resolution_kind, "external_mapping")

    def test_direct_tmdb_still_wins_without_root_preference(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "Anime Movie",
            "media_type": "movie",
            "tmdb_id": "1575337",
            "root_mal_id": "48675",
            "ids": {"tmdb": 1575337, "root_mal": 48675},
        })

        self.assertEqual(tmdb_id, 1575337)

    def test_initial_cache_takes_priority_over_pmdb_and_fribb(self) -> None:
        # Oshi no Ko S2: manual TMDB override stored in initial_cache must be
        # returned immediately without any PMDB or Fribb calls.
        client = StubPMDBClient()
        oshi_item = {
            "title": "Oshi no Ko Season 2",
            "year": 2024,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "166531",
            "mal_id": "55791",
            "ids": {"anilist": 166531, "mal": 55791},
            "anime_resolve_mode": "list_identity",
        }
        cache_key = ItemMatcher._cache_key(oshi_item)
        matcher = ItemMatcher(client, initial_cache={cache_key: 237438})

        result = matcher.resolve_match(oshi_item)

        self.assertEqual(result.tmdb_id, 237438)
        self.assertEqual(result.resolution_kind, "cache")
        self.assertEqual(client.calls, [])

    def test_manual_override_takes_priority_over_conflicting_fribb(self) -> None:
        # If a manual override is in the cache, Fribb is never consulted even
        # if it would return a different (potentially correct) mapping.
        client = StubPMDBClient()
        item = {
            "title": "Some Anime",
            "year": 2023,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "12345",
            "ids": {"anilist": 12345},
            "anime_resolve_mode": "list_identity",
        }
        cache_key = ItemMatcher._cache_key(item)
        matcher = ItemMatcher(client, initial_cache={cache_key: 99999})

        with patch("src.fribb_client.lookup_by_anilist") as mock_fribb:
            mock_fribb.return_value = {"anilist_id": 12345, "themoviedb": "11111", "type": "TV"}
            result = matcher.resolve_match(item)

        self.assertEqual(result.tmdb_id, 99999)
        self.assertEqual(result.resolution_kind, "cache")
        mock_fribb.assert_not_called()

    def test_failed_manual_override_cache_key_is_stable_across_sync_runs(self) -> None:
        # The cache key must be identical whether computed from the full item
        # dict or a reconstructed unresolved-item summary (which uses top-level
        # ID fields instead of the nested ids dict).
        full_item = {
            "title": "Oshi no Ko Season 2",
            "year": 2024,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "166531",
            "mal_id": "55791",
            "ids": {"anilist": 166531, "mal": 55791},
            "anime_resolve_mode": "list_identity",
        }
        unresolved_summary = {
            "title": "Oshi no Ko Season 2",
            "year": 2024,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "166531",
            "mal_id": "55791",
            "ids": {},
            "anime_resolve_mode": "list_identity",
        }
        self.assertEqual(ItemMatcher._cache_key(full_item), ItemMatcher._cache_key(unresolved_summary))

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_generic_mode_rejects_blocked_tmdb_id_in_fallback(self, lookup_by_anilist) -> None:
        # TMDB 298754 is a known bad community mapping; generic fallback path
        # must reject it even when anime_resolve_mode is not list_identity.
        lookup_by_anilist.return_value = None
        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            return 298754

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Some Anime",
            "year": 2022,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "99002",
            "ids": {"anilist": "99002"},
        })

        self.assertIsNone(result.tmdb_id)

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_history_mode_rejects_blocked_tmdb_id_in_fallback(self, lookup_by_anilist) -> None:
        # TMDB 277700 is blocked; history_identity mode must also reject it.
        lookup_by_anilist.return_value = None
        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            return 277700

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Another Anime",
            "year": 2021,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "99003",
            "ids": {"anilist": "99003"},
            "anime_resolve_mode": "history_identity",
        })

        self.assertIsNone(result.tmdb_id)

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_generic_mode_rejects_incompatible_title_in_fallback(self, lookup_by_anilist) -> None:
        # PMDB returns a TMDB entry titled "Vazquez vs Marquez I" for an anime
        # item — title is incompatible, must be rejected in generic fallback.
        lookup_by_anilist.return_value = None
        client = StubPMDBClient()

        def fake_lookup_detailed(id_type: str, id_value: str, media_type: str) -> dict:
            client.calls.append((id_type, id_value, media_type))
            return {"tmdb_id": 68028, "status": "hit", "votes": 5, "title": "Vazquez vs Marquez I"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Oshi no Ko",
            "year": 2023,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "150672",
            "ids": {"anilist": "150672"},
        })

        self.assertIsNone(result.tmdb_id)

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_history_mode_rejects_incompatible_title_in_fallback(self, lookup_by_anilist) -> None:
        # "Don't Play the Fool" title is incompatible with "Jujutsu Kaisen";
        # history_identity mode must reject it.
        lookup_by_anilist.return_value = None
        client = StubPMDBClient()

        def fake_lookup_detailed(id_type: str, id_value: str, media_type: str) -> dict:
            client.calls.append((id_type, id_value, media_type))
            return {"tmdb_id": 95479, "status": "hit", "votes": 3, "title": "Don't Play the Fool"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Jujutsu Kaisen",
            "year": 2020,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "113415",
            "ids": {"anilist": "113415"},
            "anime_resolve_mode": "history_identity",
        })

        self.assertIsNone(result.tmdb_id)

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_generic_mode_accepts_compatible_title_in_fallback(self, lookup_by_anilist) -> None:
        # A title-compatible PMDB mapping must still be accepted in generic mode.
        lookup_by_anilist.return_value = None
        client = StubPMDBClient()

        def fake_lookup_detailed(id_type: str, id_value: str, media_type: str) -> dict:
            client.calls.append((id_type, id_value, media_type))
            if id_type == "anilist" and id_value == "113415":
                return {"tmdb_id": 95479, "status": "hit", "votes": 8, "title": "Jujutsu Kaisen"}
            return {"tmdb_id": None, "status": "miss"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Jujutsu Kaisen",
            "year": 2020,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "113415",
            "ids": {"anilist": "113415"},
        })

        self.assertEqual(result.tmdb_id, 95479)
        self.assertEqual(result.resolution_kind, "external_mapping")

    @patch("src.fribb_client.lookup_by_anilist")
    def test_anime_list_identity_surfaces_blocked_id_as_candidate_hint(self, lookup_by_anilist) -> None:
        # When PMDB returns a blocked TMDB ID (298754) and Fribb has no entry,
        # the blocked ID must be surfaced as candidate_tmdb_id in the unresolved
        # result so the unresolved panel shows what was rejected.
        lookup_by_anilist.return_value = None
        client = StubPMDBClient()

        def fake_lookup_detailed(id_type: str, id_value: str, media_type: str) -> dict:
            client.calls.append((id_type, id_value, media_type))
            if id_type == "anilist":
                return {"tmdb_id": 298754, "status": "hit", "votes": 2, "title": "Some Bad Mapping"}
            return {"tmdb_id": None, "status": "miss"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Oshi no Ko",
            "year": 2023,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "150672",
            "ids": {"anilist": "150672"},
            "anime_resolve_mode": "list_identity",
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.resolution_kind, "unresolved")
        self.assertEqual(result.candidate_tmdb_id, 298754)


if __name__ == "__main__":
    unittest.main()
