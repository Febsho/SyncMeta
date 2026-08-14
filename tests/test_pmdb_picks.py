"""PublicMetaDB's native Picks list as a pair source and target.

Picks is a typed singleton like the watchlist — PMDB owns it by `type`, not by
name — so it is resolved through `find_list_by_type` and offered under its own
key rather than appearing in the generic custom-list loop.
"""

import unittest

from src.providers import VISIBILITY_PRIVATE, VISIBILITY_PUBLIC, PmdbAdapter
from src.publicmetadb_client import NATIVE_LIST_TYPES


class FakePmdbClient:
    """Enough of PublicMetaDBClient for the list read/write paths."""

    def __init__(self, lists: list[dict] | None = None):
        self._config = type("C", (), {"api_key": "key"})()
        self.lists = lists or []
        self.created: list[tuple] = []
        self.batched: list[tuple] = []
        self.removed: list[tuple] = []
        self.items_by_list: dict[str, list[dict]] = {}

    def get_lists(self):
        return list(self.lists)

    def find_list_by_type(self, list_type):
        wanted = str(list_type or "").strip().lower()
        for entry in self.lists:
            if str(entry.get("type") or entry.get("list_type") or "").strip().lower() == wanted:
                return entry
        return None

    def find_list_by_name(self, name):
        for entry in self.lists:
            if str(entry.get("name") or "").strip() == str(name).strip():
                return entry
        return None

    def get_or_create_list(self, name, description="", is_public=False, list_type="custom"):
        self.created.append((name, is_public, list_type))
        entry = {"id": f"new-{list_type}", "name": name, "type": list_type, "list_type": list_type}
        self.lists.append(entry)
        return entry

    def get_list_items(self, list_id):
        return self.items_by_list.get(str(list_id), [])

    def add_items_to_list_batch(self, list_id, payload):
        self.batched.append((list_id, payload))
        return {"success": True}

    def remove_item_from_list(self, list_id, item_id):
        self.removed.append((list_id, item_id))
        return True


def _picks(list_id: str = "picks-1") -> dict:
    return {"id": list_id, "name": "My Picks", "type": "picks", "list_type": "picks"}


def _watchlist(list_id: str = "wl-1") -> dict:
    return {"id": list_id, "name": "Watchlist", "type": "watchlist", "list_type": "watchlist"}


class PmdbPicksSelectionTests(unittest.TestCase):
    def test_picks_is_offered_as_its_own_source(self) -> None:
        adapter = PmdbAdapter(FakePmdbClient([_watchlist(), _picks()]))
        keys = [entry["key"] for entry in adapter.list_sources()]
        self.assertIn("picks", keys)

    def test_picks_is_offered_as_a_destination(self) -> None:
        adapter = PmdbAdapter(FakePmdbClient([_watchlist(), _picks()]))
        self.assertIn("picks", [entry["key"] for entry in adapter.target_lists()])

    def test_a_renamed_picks_list_is_not_offered_twice(self) -> None:
        # It is matched by type, so its current name must not also surface it as
        # a generic custom list — picking the wrong one would write to a second
        # list that only looks right.
        client = FakePmdbClient([_watchlist(), _picks()])
        sources = PmdbAdapter(client).list_sources()
        self.assertEqual([e["key"] for e in sources].count("picks"), 1)
        self.assertNotIn("list:picks-1", [e["key"] for e in sources])
        self.assertNotIn("list:picks-1", [e["key"] for e in PmdbAdapter(client).target_lists()])

    def test_picks_is_only_read_when_it_was_asked_for(self) -> None:
        # Folding it into the default would make every plain watchlist pair sync
        # a second list the user never selected.
        client = FakePmdbClient([_watchlist(), _picks()])
        client.items_by_list["wl-1"] = [{"tmdb_id": 1, "media_type": "movie", "title": "W"}]
        client.items_by_list["picks-1"] = [{"tmdb_id": 2, "media_type": "movie", "title": "P"}]
        adapter = PmdbAdapter(client)

        default = adapter.fetch("watchlist")
        self.assertEqual([i["tmdb_id"] for i in default], ["1"])

        chosen = adapter.fetch("watchlist", ["picks"])
        self.assertEqual([i["tmdb_id"] for i in chosen], ["2"])

    def test_reading_picks_never_creates_it(self) -> None:
        client = FakePmdbClient([_watchlist()])
        self.assertEqual(PmdbAdapter(client).fetch("watchlist", ["picks"]), [])
        self.assertEqual(client.created, [])


class PmdbPicksWriteTests(unittest.TestCase):
    def test_writing_to_picks_targets_the_existing_typed_list(self) -> None:
        client = FakePmdbClient([_watchlist(), _picks()])
        totals = PmdbAdapter(client).add(
            "watchlist", [{"tmdb_id": "603", "media_type": "movie"}], "picks",
        )
        self.assertEqual(totals["added"], 1)
        self.assertEqual(client.batched, [("picks-1", [{"tmdb_id": 603, "media_type": "movie"}])])
        self.assertEqual(client.created, [])

    def test_writing_creates_picks_with_its_own_type(self) -> None:
        client = FakePmdbClient([_watchlist()])
        PmdbAdapter(client).add("watchlist", [{"tmdb_id": "603", "media_type": "movie"}], "picks")
        self.assertEqual(client.created, [("Picks", False, "picks")])

    def test_a_public_pair_creates_a_public_picks_list(self) -> None:
        client = FakePmdbClient([])
        PmdbAdapter(client).add(
            "watchlist", [{"tmdb_id": "1", "media_type": "movie"}], "picks",
            visibility=VISIBILITY_PUBLIC,
        )
        self.assertEqual(client.created, [("Picks", True, "picks")])

    def test_a_private_pair_creates_a_private_picks_list(self) -> None:
        client = FakePmdbClient([])
        PmdbAdapter(client).add(
            "watchlist", [{"tmdb_id": "1", "media_type": "movie"}], "picks",
            visibility=VISIBILITY_PRIVATE,
        )
        self.assertEqual(client.created, [("Picks", False, "picks")])

    def test_removing_from_picks_uses_the_typed_list(self) -> None:
        client = FakePmdbClient([_picks()])
        totals = PmdbAdapter(client).remove(
            "watchlist", [{"tmdb_id": "603", "media_type": "movie", "pmdb_item_id": "i1"}], "picks",
        )
        self.assertEqual(totals["deleted"], 1)
        self.assertEqual(client.removed, [("picks-1", "i1")])

    def test_removing_never_creates_the_list(self) -> None:
        client = FakePmdbClient([])
        totals = PmdbAdapter(client).remove(
            "watchlist", [{"tmdb_id": "1", "media_type": "movie", "pmdb_item_id": "i1"}], "picks",
        )
        self.assertEqual(client.created, [])
        self.assertEqual(totals["not_found"], 1)
        self.assertEqual(client.removed, [])


class PmdbNativeListTypeTests(unittest.TestCase):
    def test_picks_and_watchlist_are_both_native_types(self) -> None:
        # get_or_create_list matches these by type rather than by name, so a
        # renamed one is found instead of being duplicated alongside ours.
        self.assertIn("picks", NATIVE_LIST_TYPES)
        self.assertIn("watchlist", NATIVE_LIST_TYPES)


if __name__ == "__main__":
    unittest.main()
