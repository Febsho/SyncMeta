import unittest

from src.config import SyncPair
from src.providers import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    PmdbAdapter,
    TraktAdapter,
)


class FakePmdbClient:
    """Enough of PublicMetaDBClient for the list-creation path."""

    def __init__(self, existing: dict | None = None):
        self._config = type("C", (), {"api_key": "key"})()
        self.existing = existing
        self.created: list[tuple] = []
        self.batched: list[tuple] = []

    def find_list_by_type(self, list_type):
        return self.existing

    def find_list_by_name(self, name):
        return self.existing

    def get_or_create_list(self, name, description="", is_public=False, list_type="custom"):
        self.created.append((name, is_public, list_type))
        return {"id": "new-list"}

    def add_items_to_list_batch(self, list_id, payload):
        self.batched.append((list_id, payload))
        return {"success": True}


class FakeTraktClient:
    def __init__(self):
        self._config = type("C", (), {"client_id": "c", "access_token": "t"})()
        self.created: list[tuple] = []

    def get_personal_lists_metadata(self):
        return []

    def get_or_create_personal_list(self, name, description="", privacy="private"):
        self.created.append((name, privacy))
        return {"user": "me", "slug": "syncmeta-simkl-watching"}

    def add_to_custom_list(self, user, slug, items):
        return {"added": len(items), "not_found": 0, "batches": 1}


class PairVisibilityConfigTests(unittest.TestCase):
    def _pair(self, **kwargs) -> SyncPair:
        raw = {"source": "trakt", "target": "pmdb", "categories": ["watchlist"]}
        raw.update(kwargs)
        return SyncPair.from_dict(raw)

    def test_a_pair_is_private_by_default(self) -> None:
        self.assertEqual(SyncPair().visibility, VISIBILITY_PRIVATE)
        self.assertEqual(self._pair().visibility, VISIBILITY_PRIVATE)

    def test_public_is_accepted(self) -> None:
        self.assertEqual(self._pair(visibility="public").visibility, VISIBILITY_PUBLIC)
        self.assertEqual(self._pair(visibility=" PUBLIC ").visibility, VISIBILITY_PUBLIC)

    def test_an_unknown_value_falls_back_to_private(self) -> None:
        # Publishing someone's watchlist because a value was misspelt is the one
        # failure mode this must not have.
        for raw in ("unlisted", "", None, "true", 1):
            self.assertEqual(self._pair(visibility=raw).visibility, VISIBILITY_PRIVATE, raw)

    def test_it_survives_a_round_trip(self) -> None:
        pair = self._pair(visibility="public")
        self.assertEqual(SyncPair.from_dict(pair.to_dict()).visibility, VISIBILITY_PUBLIC)


class PmdbVisibilityTests(unittest.TestCase):
    def test_a_public_pair_creates_a_public_watchlist(self) -> None:
        client = FakePmdbClient()
        PmdbAdapter(client).add(
            "watchlist", [{"tmdb_id": "5", "media_type": "movie"}],
            visibility=VISIBILITY_PUBLIC,
        )
        self.assertEqual(client.created, [("Watchlist", True, "watchlist")])

    def test_the_default_stays_private(self) -> None:
        client = FakePmdbClient()
        PmdbAdapter(client).add("watchlist", [{"tmdb_id": "5", "media_type": "movie"}])
        self.assertEqual(client.created, [("Watchlist", False, "watchlist")])

    def test_a_public_pair_creates_a_public_collection_list(self) -> None:
        client = FakePmdbClient()
        PmdbAdapter(client).add(
            "collection", [{"tmdb_id": "5", "media_type": "movie"}],
            visibility=VISIBILITY_PUBLIC,
        )
        self.assertEqual([c[1] for c in client.created], [True])

    def test_an_existing_list_is_never_re_flagged(self) -> None:
        # The setting describes what SyncMeta creates. Flipping a list the user
        # already had would publish something they chose to keep private.
        client = FakePmdbClient(existing={"id": "old-list"})
        PmdbAdapter(client).add(
            "watchlist", [{"tmdb_id": "5", "media_type": "movie"}],
            visibility=VISIBILITY_PUBLIC,
        )
        self.assertEqual(client.created, [])
        self.assertEqual(client.batched, [("old-list", [{"tmdb_id": 5, "media_type": "movie"}])])


class TraktVisibilityTests(unittest.TestCase):
    def test_a_created_trakt_list_takes_the_pair_visibility(self) -> None:
        client = FakeTraktClient()
        TraktAdapter(client).add(
            "watchlist",
            [{"tmdb_id": "5", "media_type": "movie", "_syncmeta_source_status": "watching"}],
            "auto:simkl:watching",
            visibility=VISIBILITY_PUBLIC,
        )
        self.assertEqual([c[1] for c in client.created], ["public"])

    def test_it_defaults_to_private(self) -> None:
        client = FakeTraktClient()
        TraktAdapter(client).add(
            "watchlist",
            [{"tmdb_id": "5", "media_type": "movie", "_syncmeta_source_status": "watching"}],
            "auto:simkl:watching",
        )
        self.assertEqual([c[1] for c in client.created], ["private"])


class CapabilityTests(unittest.TestCase):
    def test_only_providers_that_can_act_on_it_advertise_it(self) -> None:
        self.assertTrue(PmdbAdapter(FakePmdbClient()).supports_visibility)
        self.assertTrue(TraktAdapter(FakeTraktClient()).supports_visibility)

        from src.providers import AniListAdapter, MdbListAdapter, SimklAdapter
        for cls in (SimklAdapter, MdbListAdapter, AniListAdapter):
            self.assertFalse(cls.supports_visibility, cls.__name__)

    def test_describe_reports_it(self) -> None:
        self.assertTrue(PmdbAdapter(FakePmdbClient()).describe()["has_visibility"])

    def test_an_adapter_that_cannot_use_it_is_never_sent_it(self) -> None:
        from src.cross_sync import _add_kwargs

        class Plain:
            supports_visibility = False

        self.assertEqual(_add_kwargs(Plain(), SyncPair(visibility="public")), {})
        self.assertEqual(
            _add_kwargs(PmdbAdapter(FakePmdbClient()), SyncPair(visibility="public")),
            {"visibility": "public"},
        )

    def test_a_pair_stored_before_this_field_existed_reads_as_private(self) -> None:
        from src.cross_sync import _pair_visibility

        class Legacy:
            pass

        self.assertEqual(_pair_visibility(Legacy()), VISIBILITY_PRIVATE)


if __name__ == "__main__":
    unittest.main()
