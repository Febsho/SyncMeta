"""Regression contracts for cross-service pair routing and scheduling.

These tests intentionally describe the user-facing pair model rather than the
dashboard implementation.  The same normalized pair is persisted by the API,
read by the scheduler, and executed by ``CrossSyncService``.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import SyncPair
from src.cross_sync import CrossSyncService
from src.providers import CATEGORY_COLLECTION, CATEGORY_WATCHLIST, SimklAdapter, TraktAdapter
from src.profile_store import ProfileStore


TWELVE_HOURS_SECONDS = 12 * 60 * 60


def _movie(tmdb_id: str = "128") -> dict:
    return {
        "title": "Princess Mononoke",
        "year": 1997,
        "media_type": "movie",
        "tmdb_id": tmdb_id,
        "ids": {"tmdb": tmdb_id},
    }


class _SimklClient:
    def __init__(self, planned: list[dict]) -> None:
        self.planned = list(planned)
        self.status_calls: list[tuple[str, tuple[str, ...]]] = []

    def get_status(self, status: str, media_types: list[str]) -> dict:
        self.status_calls.append((status, tuple(media_types)))
        media_type = media_types[0]
        return {
            media_type: list(self.planned)
            if status in {"plantowatch", "completed"}
            else []
        }


class _TraktClient:
    def __init__(self) -> None:
        self.watchlist: list[dict] = []
        self.native_adds: list[list[dict]] = []
        self.custom_adds: list[tuple[str, str, list[dict]]] = []
        self.created_privacy: list[str] = []

    def get_watchlist(self) -> list[dict]:
        return list(self.watchlist)

    def add_to_watchlist(self, items: list[dict]) -> dict:
        self.native_adds.append(list(items))
        return {"added": len(items)}

    def add_to_custom_list(self, user: str, slug: str, items: list[dict]) -> dict:
        self.custom_adds.append((user, slug, list(items)))
        return {"added": len(items)}

    def get_personal_lists_metadata(self) -> list[dict]:
        return []

    def get_or_create_personal_list(
        self, name: str, description: str = "", privacy: str = "private",
    ) -> dict:
        slug = name.lower().replace("syncmeta · simkl ", "simkl-").replace(" ", "-")
        self.created_privacy.append(privacy)
        return {"user": "me", "slug": slug, "name": name}


class PlanToWatchRoutingTests(unittest.TestCase):
    def _run(self, *, target_list: str = "") -> _TraktClient:
        simkl_client = _SimklClient([_movie()])
        trakt_client = _TraktClient()
        service = CrossSyncService({
            "simkl": SimklAdapter(simkl_client, media_types=["movies"]),
            "trakt": TraktAdapter(trakt_client),
        })
        pair = SyncPair.from_dict({
            "pair_id": "simkl-plan-to-trakt",
            "source": "simkl",
            "target": "trakt",
            "categories": ["watchlist"],
            "source_lists": ["status:plantowatch:movies"],
            "target_list": target_list,
        })

        result = service.run_pair(pair)

        self.assertEqual(result.error_count, 0)
        self.assertEqual(result.added, 1)
        self.assertIn(("plantowatch", ("movies",)), simkl_client.status_calls)
        return trakt_client

    def test_plan_to_watch_uses_trakt_native_watchlist_by_default(self) -> None:
        trakt = self._run()

        self.assertEqual(len(trakt.native_adds), 1)
        self.assertEqual(trakt.native_adds[0][0]["tmdb_id"], "128")
        self.assertEqual(trakt.custom_adds, [])

    def test_non_plan_simkl_status_uses_a_trakt_custom_list(self) -> None:
        simkl_client = _SimklClient([_movie()])
        trakt_client = _TraktClient()
        service = CrossSyncService({
            "simkl": SimklAdapter(simkl_client, media_types=["movies"]),
            "trakt": TraktAdapter(trakt_client),
        })
        pair = SyncPair.from_dict({
            "source": "simkl",
            "target": "trakt",
            "categories": ["collection"],
            "source_lists": ["status:completed:movies"],
        })

        result = service.run_pair(pair)

        self.assertEqual(result.error_count, 0)
        self.assertEqual(result.added, 1)
        self.assertEqual(trakt_client.native_adds, [])
        self.assertEqual(len(trakt_client.custom_adds), 1)
        self.assertEqual(trakt_client.custom_adds[0][:2], ("me", "simkl-completed"))
        # A pair defaults to private, and the list it had to create follows it.
        self.assertEqual(trakt_client.created_privacy, ["private"])

    def test_irrelevant_simkl_status_selection_does_not_fall_back_to_plan_to_watch(self) -> None:
        """A collection-only selection must not unexpectedly sync the default watchlist."""
        client = _SimklClient([_movie()])
        adapter = SimklAdapter(client, media_types=["movies"])

        items = adapter.fetch("watchlist", ["status:completed:movies"])

        self.assertEqual(items, [])
        self.assertNotIn(("plantowatch", ("movies",)), client.status_calls)

    def test_status_transition_moves_item_between_managed_trakt_lists(self) -> None:
        movie = _movie()

        class _StatusClient(_SimklClient):
            def get_status(self, status, media_types):
                self.status_calls.append((status, tuple(media_types)))
                return {media_types[0]: [movie] if status == "completed" else []}

        class _ListsClient(_TraktClient):
            def __init__(self):
                super().__init__()
                self.removed = []

            def get_personal_lists_metadata(self):
                return [
                    {"name": "SyncMeta · SIMKL Watching", "user": "me", "slug": "simkl-watching"},
                    {"name": "SyncMeta · SIMKL Completed", "user": "me", "slug": "simkl-completed"},
                ]

            def get_list_items(self, user, slug):
                return [movie] if slug == "simkl-watching" else []

            def remove_from_custom_list(self, user, slug, items):
                self.removed.append((user, slug, list(items)))
                return {"deleted": len(items)}

        trakt_client = _ListsClient()
        pair = SyncPair.from_dict({
            "pair_id": "status-transition",
            "source": "simkl", "target": "trakt",
            "categories": [CATEGORY_COLLECTION],
            "source_lists": ["status:watching:movies", "status:completed:movies"],
            "removal_mode": "managed",
        })
        service = CrossSyncService({
            "simkl": SimklAdapter(_StatusClient([]), media_types=["movies"]),
            "trakt": TraktAdapter(trakt_client),
        }, managed_keys={
            "status-transition": {
                CATEGORY_COLLECTION: ["watching:movie:tmdb:128"],
            },
        })

        result = service.run_pair(pair)

        self.assertEqual(result.added, 1)
        self.assertEqual(result.removed, 1)
        self.assertEqual(trakt_client.custom_adds[0][:2], ("me", "simkl-completed"))
        self.assertEqual(trakt_client.removed[0][:2], ("me", "simkl-watching"))


class PairCapabilityValidationTests(unittest.TestCase):
    def test_named_target_list_is_rejected_when_provider_cannot_support_it(self) -> None:
        """A stale/malicious payload must not silently write to a default list."""

        class _Adapter:
            key = "source"
            label = "Source"
            supports_target_lists = False

            def can_write(self): return True
            def readable_categories(self): return (CATEGORY_WATCHLIST,)
            def writable_categories(self): return (CATEGORY_WATCHLIST,)

        source = _Adapter()
        target = _Adapter()
        target.key = "simkl"
        target.label = "SIMKL"
        pair = SyncPair.from_dict({
            "source": "source",
            "target": "simkl",
            "categories": ["watchlist"],
            "target_list": "list:should-not-be-accepted",
        })

        problem = CrossSyncService({"source": source, "simkl": target}).validate_pair(pair)

        self.assertIn("custom list", problem.lower())
        self.assertIn("SIMKL", problem)


class PairAutoSyncContractTests(unittest.TestCase):
    def test_pair_auto_sync_toggle_and_interval_round_trip(self) -> None:
        pair = SyncPair.from_dict({
            "source": "simkl",
            "target": "trakt",
            "categories": ["watchlist"],
            "auto_sync": True,
            "interval_seconds": 24 * 60 * 60,
        })

        saved = pair.to_dict()
        self.assertTrue(saved["auto_sync"])
        self.assertEqual(saved["interval_seconds"], 24 * 60 * 60)

    def test_pair_auto_sync_interval_is_clamped_to_twelve_hours(self) -> None:
        pair = SyncPair.from_dict({
            "source": "simkl",
            "target": "trakt",
            "categories": ["watchlist"],
            "auto_sync": True,
            "interval_seconds": 60,
        })

        self.assertEqual(pair.to_dict()["interval_seconds"], TWELVE_HOURS_SECONDS)

    def test_pair_auto_sync_defaults_off_for_existing_profiles(self) -> None:
        pair = SyncPair.from_dict({
            "source": "simkl",
            "target": "trakt",
            "categories": ["watchlist"],
        })

        self.assertFalse(pair.to_dict()["auto_sync"])
        self.assertEqual(pair.to_dict()["interval_seconds"], TWELVE_HOURS_SECONDS)


class PairSchedulerContractTests(unittest.TestCase):
    def test_changing_pair_interval_reschedules_the_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            created = store.create_profile("secret", {
                "simkl": {"client_id": "c", "access_token": "s"},
                "trakt": {"client_id": "c", "access_token": "t"},
                "pmdb": {"api_key": "p"},
            }, {
                "auto_sync": False,
                "media_types": ["movies"],
                "sync_pairs": [{
                    "pair_id": "simkl-trakt-auto",
                    "source": "simkl",
                    "target": "trakt",
                    "categories": ["watchlist"],
                    "auto_sync": True,
                    "interval_seconds": TWELVE_HOURS_SECONDS,
                }],
            })
            profile_id = created["profile_id"]
            before = store.get_private_profile_by_id(profile_id)["pair_sync_schedule"]["simkl-trakt-auto"]["next_sync_at"]

            store.update_sync_pairs(profile_id, [{
                "pair_id": "simkl-trakt-auto",
                "source": "simkl",
                "target": "trakt",
                "categories": ["watchlist"],
                "auto_sync": True,
                "interval_seconds": 24 * 60 * 60,
            }])

            after = store.get_private_profile_by_id(profile_id)["pair_sync_schedule"]["simkl-trakt-auto"]["next_sync_at"]
            self.assertNotEqual(after, before)
            self.assertGreaterEqual(
                datetime.fromisoformat(after),
                datetime.now(timezone.utc) + timedelta(hours=23),
            )

    def test_auto_pair_is_claimed_with_global_sync_off_then_rescheduled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            created = store.create_profile("secret", {
                "simkl": {"client_id": "c", "access_token": "s"},
                "trakt": {"client_id": "c", "access_token": "t"},
                "pmdb": {"api_key": "p"},
            }, {
                "auto_sync": False,
                "auto_history_sync": False,
                "auto_resume_sync": False,
                "media_types": ["movies"],
                "sync_pairs": [{
                    "pair_id": "simkl-trakt-auto",
                    "source": "simkl",
                    "target": "trakt",
                    "categories": ["watchlist"],
                    "auto_sync": True,
                    "interval_seconds": 60,
                }],
            })
            profile_id = created["profile_id"]
            # Creation schedules the first run in the future. Move only this
            # pair's due time into the past to model the scheduler waking up.
            store._profiles[profile_id]["pair_sync_schedule"]["simkl-trakt-auto"][
                "next_sync_at"
            ] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

            claimed = store.claim_due_profiles()

            self.assertEqual(len(claimed), 1)
            modes = claimed[0]["pending_sync_modes"]
            self.assertFalse(modes["lists"], "global list auto-sync was off")
            self.assertTrue(modes["pairs"])
            self.assertEqual(modes["pair_ids"], ["simkl-trakt-auto"])

            # Completing the scheduled job advances only that pair. It must not
            # be claimable again until at least the configured 12-hour floor.
            completed_at = datetime.now(timezone.utc)
            store.record_sync_success(profile_id, [], sync_modes=modes)
            stored = store.get_private_profile_by_id(profile_id)
            next_at = datetime.fromisoformat(
                stored["pair_sync_schedule"]["simkl-trakt-auto"]["next_sync_at"]
            )
            self.assertGreaterEqual(
                next_at,
                completed_at + timedelta(seconds=TWELVE_HOURS_SECONDS),
            )
            self.assertEqual(store.claim_due_profiles(), [])


if __name__ == "__main__":
    unittest.main()
