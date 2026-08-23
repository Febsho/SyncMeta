import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.profile_store import (
    MIN_RESUME_SYNC_INTERVAL_SECONDS,
    MIN_SYNC_INTERVAL_SECONDS,
    MIN_WATCHED_HISTORY_INTERVAL_SECONDS,
    ProfileStore,
)


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        self.credentials = {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {
                    "shows": ["watching", "completed"],
                    "movies": ["plantowatch"],
                    "anime": [],
                },
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": ["CURRENT", "COMPLETED"],
            },
            "mdblist": {
                "api_key": "mdbl-key",
                "selected_lists": [{
                    "id": 11,
                    "name": "Top Movies",
                    "slug": "top-movies",
                    "user_name": "demo",
                    "description": "desc",
                    "mediatype": "movie",
                    "items": 10,
                    "likes": 5,
                    "type": "user",
                    "private": False,
                }],
            },
            "pmdb": {
                "api_key": "pm-key",
            },
        }
        self.options = {
            "auto_sync": True,
            "interval_seconds": MIN_SYNC_INTERVAL_SECONDS,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "simkl_visibility": "private",
            "anilist_visibility": "private",
            "trakt_personal_visibility": "private",
            "trakt_public_visibility": "public",
            "mdblist_visibility": "public",
            "media_types": ["shows", "movies"],
        }

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_create_profile_persists_and_authenticates(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        private_loaded = self.store.get_private_profile_by_id(created["profile_id"])
        payload = json.loads((Path(self.tmpdir.name) / "profiles.json").read_text(encoding="utf-8"))

        self.assertEqual(loaded["profile_id"], created["profile_id"])
        self.assertTrue(loaded["credentials"]["pmdb"]["api_key_saved"])
        self.assertNotIn("credentials", payload["profiles"][created["profile_id"]])
        self.assertIn("credentials_encrypted", payload["profiles"][created["profile_id"]])
        self.assertEqual(private_loaded["credentials"]["pmdb"]["api_key"], "pm-key")
        self.assertEqual(loaded["options"]["interval_seconds"], MIN_SYNC_INTERVAL_SECONDS)
        self.assertEqual(loaded["options"]["trakt_watched_history_interval_seconds"], 86400)
        self.assertEqual(loaded["options"]["trakt_resume_progress_interval_seconds"], 86400)
        self.assertFalse(loaded["options"]["auto_resume_sync"])
        self.assertTrue(loaded["options"]["onboarding_completed"])
        self.assertFalse(loaded["options"]["delete_disabled_lists"])
        self.assertEqual(loaded["options"]["simkl_visibility"], "private")
        self.assertEqual(loaded["options"]["trakt_public_visibility"], "public")
        self.assertEqual(loaded["credentials"]["simkl"]["selected_statuses"]["shows"], ["watching", "completed"])
        self.assertEqual(loaded["credentials"]["anilist"]["selected_statuses"], ["CURRENT", "COMPLETED"])
        self.assertEqual(loaded["credentials"]["mdblist"]["selected_lists"][0]["id"], 11)
        self.assertIsNotNone(loaded["next_sync_at"])
        self.assertGreater(datetime.fromisoformat(loaded["next_sync_at"]), datetime.now(timezone.utc))

    def test_notification_channels_are_encrypted_and_masked(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        profile_id = created["profile_id"]
        public = self.store.update_notification_channel(profile_id, "discord", {
            "enabled": True, "events": ["sync_failed"],
            "url": "https://discord.example/secret-token",
        })
        raw = (Path(self.tmpdir.name) / "profiles.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-token", raw)
        self.assertTrue(public["notification_channels"]["discord"]["configured"])
        self.assertNotIn("url", public["notification_channels"]["discord"])

    def test_pair_changes_create_activity_and_non_rollback_snapshot(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        profile_id = created["profile_id"]
        self.store.update_pair_last_results(profile_id, [{
            "pair_id": "route-1", "source": "simkl", "target": "pmdb",
            "categories": [{"changes": [{
                "change_type": "added", "category": "watchlist", "title": "Dune",
                "media_type": "movie", "ids": {"tmdb": "693134"},
                "previous": "absent", "new": "present",
            }]}],
        }])
        private = self.store.get_private_profile_by_id(profile_id)
        self.assertEqual(self.store.get_item_activity(profile_id)[0]["title"], "Dune")
        self.assertFalse(private["change_snapshots"][0]["rollback_available"])

    def test_onboarding_completion_flag_round_trips(self) -> None:
        created = self.store.create_profile(
            "secret", self.credentials, {**self.options, "onboarding_completed": False},
        )
        self.assertFalse(created["options"]["onboarding_completed"])

        updated = self.store.update_profile_by_id(
            created["profile_id"], {}, {**created["options"], "onboarding_completed": True},
        )
        self.assertTrue(updated["options"]["onboarding_completed"])

    def test_legacy_pmdb_pipeline_is_migrated_to_normal_pairs_once(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_history_source": "simkl",
            "auto_history_sync": True,
        })
        path = Path(self.tmpdir.name) / "profiles.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        stored = payload["profiles"][created["profile_id"]]
        stored.pop("pair_model_version", None)
        # Emulate a profile written before pairs replaced the built-in pipeline.
        stored["options"]["sync_pairs"] = []
        path.write_text(json.dumps(payload), encoding="utf-8")

        migrated_store = ProfileStore(path)
        migrated = migrated_store.get_private_profile_by_id(created["profile_id"])
        pairs = {pair["source"]: pair for pair in migrated["options"]["sync_pairs"]}

        self.assertEqual(pairs["simkl"]["target"], "pmdb")
        self.assertEqual(pairs["simkl"]["categories"], ["watchlist", "history", "collection"])
        self.assertIn("status:completed:shows", pairs["simkl"]["source_lists"])
        self.assertEqual(pairs["mdblist"]["source_lists"], ["list:11"])
        self.assertFalse(migrated["options"]["auto_sync"])
        self.assertEqual(migrated["options"]["activity_history_source"], "off")

        persisted = json.loads(path.read_text(encoding="utf-8"))["profiles"][created["profile_id"]]
        self.assertEqual(persisted["pair_model_version"], 1)
        self.assertEqual(len(ProfileStore(path).get_private_profile_by_id(created["profile_id"])["options"]["sync_pairs"]), len(pairs))

    def test_create_profile_does_not_become_due_immediately(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        due = self.store.claim_due_profiles()

        self.assertEqual(due, [])
        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        self.assertFalse(loaded["sync_running"])

    def test_interval_below_minimum_is_clamped(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "interval_seconds": MIN_SYNC_INTERVAL_SECONDS - 1,
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        self.assertEqual(loaded["options"]["interval_seconds"], MIN_SYNC_INTERVAL_SECONDS)

    def test_watched_history_interval_below_minimum_is_clamped(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "trakt_watched_history_interval_seconds": MIN_WATCHED_HISTORY_INTERVAL_SECONDS - 1,
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        self.assertEqual(
            loaded["options"]["trakt_watched_history_interval_seconds"],
            MIN_WATCHED_HISTORY_INTERVAL_SECONDS,
        )

    def test_resume_interval_below_minimum_is_clamped(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "trakt_resume_progress_interval_seconds": MIN_RESUME_SYNC_INTERVAL_SECONDS - 1,
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        self.assertEqual(
            loaded["options"]["trakt_resume_progress_interval_seconds"],
            MIN_RESUME_SYNC_INTERVAL_SECONDS,
        )

    def test_schedule_jitter_is_deterministic_and_mode_specific(self) -> None:
        profile_id = "00000000-0000-4000-8000-000000000001"

        list_jitter = ProfileStore._schedule_jitter_seconds(profile_id, "lists", 900)
        history_jitter = ProfileStore._schedule_jitter_seconds(profile_id, "history", 900)
        resume_jitter = ProfileStore._schedule_jitter_seconds(profile_id, "resume", 900)

        self.assertEqual(list_jitter, ProfileStore._schedule_jitter_seconds(profile_id, "lists", 900))
        self.assertTrue(0 <= list_jitter <= 900)
        self.assertTrue(0 <= history_jitter <= 900)
        self.assertTrue(0 <= resume_jitter <= 900)
        self.assertGreater(len({list_jitter, history_jitter, resume_jitter}), 1)

    def test_existing_profiles_migrate_resume_to_manual_and_disable_auto_activity(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_history_source": "trakt",
            "auto_history_sync": True,
            "activity_resume_source": "trakt",
            "auto_resume_sync": True,
            "trakt_resume_progress_interval_seconds": 86400,
        })

        store_path = Path(self.tmpdir.name) / "profiles.json"
        payload = json.loads(store_path.read_text(encoding="utf-8"))
        raw_profile = payload["profiles"][created["profile_id"]]
        raw_profile["options_version"] = 1
        store_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

        reloaded = ProfileStore(store_path)
        loaded = reloaded.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertFalse(loaded["options"]["auto_history_sync"])
        self.assertFalse(loaded["options"]["auto_resume_sync"])
        self.assertEqual(loaded["options"]["activity_resume_source"], "off")
        self.assertIsNone(loaded["next_history_sync_at"])
        self.assertIsNone(loaded["next_resume_sync_at"])

    def test_manual_dry_run_does_not_advance_schedule(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        claimed = self.store.claim_profile_for_sync(created["profile_id"], "secret")
        next_sync_before = claimed["next_sync_at"]

        self.store.record_sync_success(created["profile_id"], [{"list_name": "demo"}], dry_run=True)
        loaded = self.store.get_profile(created["profile_id"], "secret")

        self.assertEqual(loaded["next_sync_at"], next_sync_before)
        self.assertEqual(len(loaded["history"]), 1)
        self.assertTrue(loaded["history"][0]["dry_run"])

    def test_request_sync_cancel_and_record_cancelled(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.claim_profile_for_sync(created["profile_id"], "secret")

        stopping = self.store.request_sync_cancel(created["profile_id"])
        self.assertTrue(stopping["sync_cancel_requested"])
        self.assertEqual(stopping["sync_status"], "Stopping...")
        self.assertTrue(self.store.is_sync_cancel_requested(created["profile_id"]))

        stopped = self.store.record_sync_cancelled(created["profile_id"])
        self.assertFalse(stopped["sync_running"])
        self.assertFalse(stopped["sync_cancel_requested"])
        self.assertEqual(stopped["sync_status"], "Stopped")
        self.assertEqual(stopped["history"][0]["status"], "stopped")

    def test_sync_error_is_added_to_history(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.claim_profile_for_sync(created["profile_id"], "secret")

        failed = self.store.record_sync_error(created["profile_id"], "Boom")

        self.assertEqual(failed["sync_status"], "Failed: Boom")
        self.assertEqual(failed["history"][0]["status"], "failed")
        self.assertEqual(failed["history"][0]["error_message"], "Boom")

    def test_update_sync_progress_exposes_live_results(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.claim_profile_for_sync(created["profile_id"], "secret")

        self.store.update_sync_progress(created["profile_id"], [{
            "list_name": "Watching - Series",
            "display_name": "Watching - Series",
            "source_name": "SIMKL",
            "items_fetched": 20,
            "items_resolved": 12,
            "items_added": 4,
            "items_removed": 0,
            "items_skipped_duplicate": 3,
            "items_skipped_unresolved": 1,
            "error_count": 0,
        }])

        # update_sync_progress is fire-and-forget; the live results are read
        # back off the profile, which is what /status does.
        updated = self.store.get_profile_by_id(created["profile_id"], include_credentials=False)
        self.assertEqual(len(updated["sync_live_results"]), 1)
        self.assertEqual(updated["sync_live_results"][0]["items_fetched"], 20)

    def test_enabling_auto_sync_schedules_next_run_in_future(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "auto_sync": False,
        })

        updated = self.store.update_profile(created["profile_id"], "secret", {}, self.options)

        self.assertIsNotNone(updated["next_sync_at"])
        self.assertGreater(
            datetime.fromisoformat(updated["next_sync_at"]),
            datetime.now(timezone.utc),
        )
        self.assertEqual(self.store.claim_due_profiles(), [])

    def test_enabling_auto_history_sync_schedules_next_history_run_in_future(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_history_source": "trakt",
            "auto_history_sync": False,
        })

        updated = self.store.update_profile(created["profile_id"], "secret", {}, {
            **self.options,
            "activity_history_source": "trakt",
            "auto_history_sync": True,
            "trakt_watched_history_interval_seconds": 86400,
        })

        self.assertIsNotNone(updated["next_history_sync_at"])
        self.assertGreater(
            datetime.fromisoformat(updated["next_history_sync_at"]),
            datetime.now(timezone.utc),
        )

    def test_claim_due_profiles_can_schedule_history_without_list_auto_sync(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "auto_sync": False,
            "activity_history_source": "trakt",
            "auto_history_sync": True,
            "trakt_watched_history_interval_seconds": 86400,
        })
        profile = self.store._profiles[created["profile_id"]]
        profile["next_history_sync_at"] = "2000-01-01T00:00:00+00:00"
        self.store._save_locked()

        due = self.store.claim_due_profiles()

        self.assertEqual(len(due), 1)
        self.assertFalse(due[0]["pending_sync_modes"]["lists"])
        self.assertTrue(due[0]["pending_sync_modes"]["history"])

    def test_a_killed_process_does_not_wedge_a_profile_as_running(self) -> None:
        # sync_running is deliberately not persisted (_hydrate_profile forces it
        # False), so a process killed mid-sync cannot leave a profile stuck with
        # claim_due_profiles skipping it forever. Pinned by a test because
        # persisting the flag "for accuracy" would silently reintroduce that.
        created = self.store.create_profile("secret", self.credentials, self.options)
        profile_id = created["profile_id"]
        self.store._profiles[profile_id]["next_sync_at"] = "2000-01-01T00:00:00+00:00"
        self.store._save_locked()

        self.assertEqual(len(self.store.claim_due_profiles()), 1)
        self.assertTrue(self.store._profiles[profile_id]["sync_running"])

        on_disk = json.loads((Path(self.tmpdir.name) / "profiles.json").read_text())
        self.assertNotIn("sync_running", on_disk["profiles"][profile_id])

        restarted = ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        self.assertFalse(restarted._profiles[profile_id]["sync_running"])
        restarted._profiles[profile_id]["next_sync_at"] = "2000-01-01T00:00:00+00:00"
        self.assertEqual(len(restarted.claim_due_profiles()), 1)

    def test_claim_due_profiles_honours_a_batch_limit(self) -> None:
        ids = []
        for _ in range(3):
            created = self.store.create_profile("secret", self.credentials, self.options)
            ids.append(created["profile_id"])
            self.store._profiles[created["profile_id"]]["next_sync_at"] = "2000-01-01T00:00:00+00:00"
        self.store._save_locked()

        first = self.store.claim_due_profiles(limit=2)
        self.assertEqual(len(first), 2)
        # The unclaimed one is still due and is picked up by the next poll.
        second = self.store.claim_due_profiles(limit=2)
        self.assertEqual(len(second), 1)
        self.assertEqual(
            sorted(p["profile_id"] for p in first + second), sorted(ids)
        )
        # No limit keeps the old behaviour.
        self.assertEqual(self.store.claim_due_profiles(), [])

    def test_sync_success_persists_managed_lists(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Watching - Series",
        }], managed_lists=[{
            "list_name": "SIMKL - Series - Watching",
            "list_id": "pmdb-1",
            "display_name": "Watching - Series",
            "source_name": "SIMKL",
            "selection": {
                "source": "simkl",
                "media_type": "shows",
                "status": "watching",
            },
        }])

        reloaded_store = ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        loaded = reloaded_store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertEqual(loaded["last_results"][0]["list_name"], "Watching - Series")
        self.assertFalse(loaded["options"]["delete_disabled_lists"])
        self.assertEqual(reloaded_store._profiles[created["profile_id"]]["managed_lists"][0]["list_id"], "pmdb-1")
        self.assertEqual(
            reloaded_store._profiles[created["profile_id"]]["managed_lists"][0]["selection"]["status"],
            "watching",
        )

    def test_sync_success_persists_detailed_runs_and_list_state(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        claimed = self.store.claim_profile_for_sync(created["profile_id"], "secret")
        run_id = claimed["sync_job_id"]

        updated = self.store.record_sync_success(
            created["profile_id"],
            [{
                "list_name": "Watching - Series",
                "display_name": "Watching - Series",
                "source_name": "SIMKL",
                "row_key": "simkl|watching-series|status_list",
                "row_type": "status_list",
                "has_details": True,
                "run_id": run_id,
            }],
            detailed_results=[{
                "list_name": "Watching - Series",
                "display_name": "Watching - Series",
                "source_name": "SIMKL",
                "row_key": "simkl|watching-series|status_list",
                "row_type": "status_list",
                "errors": ["SIMKL list fetch failed"],
                "sample_failed_titles": ["Demo Show"],
                "row_state": {
                    "fingerprint": "fp-1",
                    "activities_ts": "2026-05-14T12:00:00Z",
                    "updated_at": "2026-05-14T12:05:00Z",
                    "item_count": 4,
                    "last_resolved_count": 3,
                    "write_keys": ["tmdb:123:tv"],
                },
            }],
        )

        self.assertEqual(updated["latest_run_id"], run_id)
        runs = self.store.get_sync_runs(created["profile_id"])
        self.assertEqual(runs["items"][0]["run_id"], run_id)
        detail = self.store.get_sync_run_detail(created["profile_id"], run_id)
        self.assertEqual(detail["rows"][0]["errors"], ["SIMKL list fetch failed"])
        self.assertEqual(
            self.store._profiles[created["profile_id"]]["list_state"]["simkl|watching-series|status_list"]["fingerprint"],
            "fp-1",
        )

    def test_detailed_runs_are_capped_at_25(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        for _ in range(27):
            claimed = self.store.claim_profile_for_sync(created["profile_id"], "secret")
            self.store.record_sync_success(
                created["profile_id"],
                [{"list_name": "Watching - Series", "display_name": "Watching - Series"}],
                detailed_results=[{"list_name": "Watching - Series", "display_name": "Watching - Series"}],
            )

        runs = self.store.get_sync_runs(created["profile_id"], page=1, page_size=25)
        stored = self.store._profiles[created["profile_id"]]["sync_runs_detailed"]

        self.assertEqual(len(stored), 25)
        self.assertEqual(runs["total"], 25)
        self.assertEqual(runs["items"][0]["run_id"], stored[0]["run_id"])
        self.assertEqual(runs["items"][-1]["run_id"], stored[-1]["run_id"])

    def test_delete_managed_list_by_id_removes_selection_and_results(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Top Movies",
        }], managed_lists=[{
            "list_name": "Top Movies",
            "list_id": "pmdb-2",
            "display_name": "Top Movies",
            "source_name": "MDBList",
            "selection": {
                "source": "mdblist",
                "id": 11,
                "mediatype": "movie",
            },
        }])

        updated_credentials = dict(self.credentials)
        updated_credentials["mdblist"] = {
            **self.credentials["mdblist"],
            "selected_lists": [],
        }

        updated = self.store.delete_managed_list_by_id(created["profile_id"], "Top Movies", updated_credentials)

        self.assertEqual(updated["sync_status"], "Managed list deleted")
        self.assertEqual(updated["last_results"], [])
        self.assertEqual(self.store._profiles[created["profile_id"]]["managed_lists"], [])
        self.assertEqual(self.store._profiles[created["profile_id"]]["credentials"]["mdblist"]["selected_lists"], [])

    def test_delete_profile_by_id_removes_records(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        self.store.delete_profile_by_id(created["profile_id"])

        with self.assertRaises(KeyError):
            self.store.get_private_profile_by_id(created["profile_id"])

    def test_reset_history_import_state_by_id_clears_cursors(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        profile = self.store.get_private_profile_by_id(created["profile_id"])
        profile["activity_state"] = {
            "simkl_history_cursor": "2026-04-01T12:00:00Z",
            "trakt_history_cursor": "2026-04-02T12:00:00Z",
        }
        profile["activity_results"] = {
            "watch_history": {
                "timestamp": "2026-04-03T12:00:00Z",
                "row": {"display_name": "Watch History"},
            }
        }
        profile["last_history_sync"] = "2026-04-03T12:00:00Z"
        self.store._profiles[created["profile_id"]] = profile

        updated = self.store.reset_history_import_state_by_id(created["profile_id"])

        self.assertEqual(updated["activity_state"]["simkl_history_cursor"], "")
        self.assertEqual(updated["activity_state"]["trakt_history_cursor"], "")
        self.assertNotIn("watch_history", updated["activity_results"])
        self.assertIsNone(updated["last_history_sync"])

    def test_update_by_id_keeps_saved_secrets_when_fields_are_blank(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        updated = self.store.update_profile_by_id(created["profile_id"], {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "",
            },
        }, self.options)

        private_loaded = self.store.get_private_profile_by_id(created["profile_id"])

        self.assertTrue(updated["credentials"]["pmdb"]["api_key_saved"])
        self.assertEqual(private_loaded["credentials"]["simkl"]["access_token"], "simkl-token")
        self.assertEqual(private_loaded["credentials"]["mdblist"]["api_key"], "mdbl-key")
        self.assertEqual(private_loaded["credentials"]["pmdb"]["api_key"], "pm-key")

    def test_empty_source_statuses_stay_empty_until_user_selects_them(self) -> None:
        created = self.store.create_profile("secret", {
            **self.credentials,
            "simkl": {
                **self.credentials["simkl"],
                "selected_statuses": {},
            },
            "anilist": {
                **self.credentials["anilist"],
                "selected_statuses": [],
            },
        }, self.options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertEqual(loaded["credentials"]["simkl"]["selected_statuses"], {
            "shows": [],
            "movies": [],
            "anime": [],
        })
        self.assertEqual(loaded["credentials"]["anilist"]["selected_statuses"], [])

    def test_legacy_default_trakt_catalogs_are_cleared_until_user_opts_in(self) -> None:
        created = self.store.create_profile("secret", {
            **self.credentials,
            "trakt": {
                "client_id": "trakt-client",
                "client_secret": "",
                "access_token": "trakt-token",
                "refresh_token": "",
                "username": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [{
                    "name": "Trending Movies",
                    "user": "default",
                    "slug": "trending-movies",
                    "source": "default",
                    "catalog_key": "trending-movies",
                }],
            },
        }, self.options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertFalse(loaded["credentials"]["trakt"]["default_catalogs_initialized"])
        self.assertEqual(loaded["credentials"]["trakt"]["selected_lists"], [])

    def test_history_is_manual_but_resume_is_auto_scheduled_when_enabled(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "trakt_sync_watched_history": True,
            "activity_resume_source": "trakt",
            "auto_resume_sync": True,
            "trakt_resume_progress_interval_seconds": 86400,
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        self.assertIsNone(loaded["next_history_sync_at"])
        self.assertIsNotNone(loaded["next_resume_sync_at"])
        self.assertEqual(self.store.claim_due_profiles(), [])

    def _trakt_connected_credentials(self) -> dict:
        return {
            **self.credentials,
            "trakt": {
                "client_id": "trakt-client",
                "client_secret": "",
                "access_token": "trakt-token",
                "refresh_token": "",
                "username": "",
                "sync_watchlist": True,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
        }

    def test_turning_resume_off_beats_the_legacy_trakt_flag(self) -> None:
        # The fallback exists to migrate the old trakt_sync_resume_progress
        # boolean onto the newer activity_resume_source. But it used to catch
        # *every* non-"trakt" value, including an explicit "off" — and because
        # the normalised options carry trakt_sync_resume_progress back out
        # again, the override re-armed itself on every save. Turning resume off
        # was therefore impossible: it silently kept syncing.
        created = self.store.create_profile("secret", self._trakt_connected_credentials(), {
            **self.options,
            "trakt_sync_resume_progress": True,
            "activity_resume_source": "off",
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        self.assertEqual(loaded["options"]["activity_resume_source"], "off")

        # And it stays off across a re-save rather than re-arming.
        self.store.update_profile_by_id(created["profile_id"], {}, {
            **self.options,
            "activity_resume_source": "off",
        })
        reloaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        self.assertEqual(reloaded["options"]["activity_resume_source"], "off")

    def test_legacy_trakt_resume_flag_still_migrates_when_source_is_unset(self) -> None:
        # The migration itself must survive: an older profile that only has the
        # boolean still ends up pointed at Trakt.
        options = {**self.options, "trakt_sync_resume_progress": True}
        options.pop("activity_resume_source", None)
        created = self.store.create_profile("secret", self._trakt_connected_credentials(), options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        self.assertEqual(loaded["options"]["activity_resume_source"], "trakt")

    def test_simkl_resume_source_round_trips(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_resume_source": "simkl",
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertEqual(loaded["options"]["activity_resume_source"], "simkl")
        self.assertTrue(loaded["options"]["simkl_sync_resume_progress"])

    def test_simkl_watched_state_reconciliation_round_trips(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "simkl_reconcile_watched_history": True,
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertTrue(loaded["options"]["simkl_reconcile_watched_history"])
        # Defaults off — it is a whole-account read per history run.
        self.assertFalse(
            self.store.get_profile(
                self.store.create_profile("secret2", self.credentials, self.options)["profile_id"],
                "secret2", include_credentials=True,
            )["options"]["simkl_reconcile_watched_history"]
        )

    def test_mdblist_oauth_only_profile_counts_as_a_configured_source(self) -> None:
        """An OAuth connection is a complete MDBList credential. Requiring an
        api key here left OAuth-only profiles out of the source list, and out
        of the config validation that goes with it."""
        from src.profile_store import _configured_sources_for_profile

        profile = {
            "credentials": {
                "mdblist": {
                    "client_id": "cid",
                    "access_token": "mdb-token",
                    "refresh_token": "mdb-refresh",
                    "selected_lists": [{"id": 4, "name": "Picks", "mediatype": "movie"}],
                },
            },
            "options": self.options,
        }

        self.assertIn("mdblist", _configured_sources_for_profile(profile))

    def test_mdblist_without_any_credential_is_not_a_source(self) -> None:
        from src.profile_store import _configured_sources_for_profile

        profile = {
            "credentials": {"mdblist": {
                "selected_lists": [{"id": 4, "name": "Picks", "mediatype": "movie"}],
            }},
            "options": self.options,
        }

        self.assertNotIn("mdblist", _configured_sources_for_profile(profile))

    def test_trakt_watched_state_reconciliation_round_trips(self) -> None:
        """It was declared in SyncConfig but pinned to False everywhere, so the
        toggle had no way to reach the pipeline."""
        created = self.store.create_profile("secret", self._trakt_connected_credentials(), {
            **self.options,
            "trakt_reconcile_watched_history": True,
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertTrue(loaded["options"]["trakt_reconcile_watched_history"])

    def test_trakt_watched_state_reconciliation_defaults_off(self) -> None:
        # It is a second whole-account read per history run, so it opts in.
        created = self.store.create_profile("secret", self.credentials, self.options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertFalse(loaded["options"]["trakt_reconcile_watched_history"])

    def _pair(self) -> dict:
        return {
            "pair_id": "trakt-simkl-1",
            "name": "",
            "source": "trakt",
            "target": "simkl",
            "categories": ["watchlist"],
            "source_lists": ["list:someone/top-100"],
            "target_list": "",
            "removal_mode": "additive",
            "enabled": True,
        }

    def test_saving_settings_does_not_delete_sync_pairs(self) -> None:
        # The settings form never submits sync_pairs — they live on their own
        # screen — so a save from it must leave them alone.
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.update_sync_pairs(created["profile_id"], [self._pair()])

        updated = self.store.update_profile_by_id(created["profile_id"], self.credentials, self.options)

        self.assertEqual(len(updated["options"]["sync_pairs"]), 1)
        self.assertEqual(updated["options"]["sync_pairs"][0]["source_lists"], ["list:someone/top-100"])

    def test_update_profile_with_password_does_not_delete_sync_pairs(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.update_sync_pairs(created["profile_id"], [self._pair()])

        updated = self.store.update_profile(created["profile_id"], "secret", self.credentials, self.options)

        self.assertEqual(len(updated["options"]["sync_pairs"]), 1)

    def test_explicit_empty_sync_pairs_still_clears_them(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.update_sync_pairs(created["profile_id"], [self._pair()])

        updated = self.store.update_profile_by_id(
            created["profile_id"], self.credentials, {**self.options, "sync_pairs": []},
        )

        self.assertEqual(updated["options"]["sync_pairs"], [])

    def test_update_profile_by_id_preserves_existing_next_list_sync_time(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.claim_profile_for_sync(created["profile_id"], "secret")
        after_sync = self.store.record_sync_success(created["profile_id"], [{"list_name": "demo"}], dry_run=False)
        next_sync_before = after_sync["next_sync_at"]

        updated = self.store.update_profile_by_id(created["profile_id"], self.credentials, self.options)

        due_profiles = self.store.claim_due_profiles()

        self.assertEqual(updated["next_sync_at"], next_sync_before)
        self.assertEqual(due_profiles, [])

    def test_update_profile_by_id_preserves_existing_next_resume_sync_time(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_resume_source": "trakt",
            "auto_resume_sync": True,
        })
        initial = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        next_resume_before = initial["next_resume_sync_at"]

        updated = self.store.update_profile_by_id(created["profile_id"], self.credentials, {
            **self.options,
            "activity_resume_source": "trakt",
            "auto_resume_sync": True,
        })

        self.assertEqual(updated["next_resume_sync_at"], next_resume_before)

    def test_resume_sync_success_uses_configured_resume_interval_for_next_run(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_resume_source": "trakt",
            "auto_resume_sync": True,
            "trakt_resume_progress_interval_seconds": 86400,
        })
        self.store.claim_profile_for_sync(created["profile_id"], "secret", {"lists": False, "resume": True})

        updated = self.store.record_sync_success(created["profile_id"], [{
            "display_name": "Resume Progress",
            "list_name": "Resume Progress",
        }], dry_run=False, sync_modes={"lists": False, "resume": True})

        next_resume = datetime.fromisoformat(updated["next_resume_sync_at"])
        delta_seconds = (next_resume - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(delta_seconds, 86000)
        self.assertLess(delta_seconds, 86400 + 901)

    def test_activity_only_sync_keeps_existing_last_list_results(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_resume_source": "trakt",
        })
        self.store.claim_profile_for_sync(created["profile_id"], "secret")
        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Watching - Series",
            "display_name": "Watching - Series",
            "source_name": "SIMKL",
        }], dry_run=False, sync_modes={"lists": True, "history": False, "resume": False})

        self.store.claim_profile_for_sync(created["profile_id"], "secret", sync_modes={"lists": False, "history": False, "resume": True})
        updated = self.store.record_sync_success(created["profile_id"], [{
            "list_name": "",
            "display_name": "Trakt Resume Progress",
            "source_name": "Trakt",
            "items_fetched": 6,
        }], dry_run=False, sync_modes={"lists": False, "history": False, "resume": True})

        self.assertEqual(len(updated["last_results"]), 1)
        self.assertEqual(updated["last_results"][0]["display_name"], "Watching - Series")
        self.assertIn("resume_progress", updated["activity_results"])
        self.assertEqual(
            updated["activity_results"]["resume_progress"]["row"]["display_name"],
            "Trakt Resume Progress",
        )

    def test_list_sync_replaces_stale_unresolved_snapshot(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Watching - Anime",
            "display_name": "Watching - Anime",
            "source_name": "SIMKL",
            "items_skipped_unresolved": 1,
            "unresolved_items": [{
                "cache_key": "stale-item",
                "title": "Old Missing Show",
                "list_name": "Watching - Anime",
            }],
        }], dry_run=False, sync_modes={"lists": True, "history": False, "resume": False})

        updated = self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Watching - Anime",
            "display_name": "Watching - Anime",
            "source_name": "SIMKL",
            "items_skipped_unresolved": 0,
            "unresolved_items": [],
        }], dry_run=False, sync_modes={"lists": True, "history": False, "resume": False})

        self.assertEqual(self.store.get_unresolved_items(created["profile_id"]), [])
        self.assertEqual(updated["last_results"][0]["items_skipped_unresolved"], 0)

    def test_activity_only_sync_keeps_existing_unresolved_snapshot(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "trakt_sync_resume_progress": True,
        })

        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Watching - Anime",
            "display_name": "Watching - Anime",
            "source_name": "SIMKL",
            "items_skipped_unresolved": 1,
            "unresolved_items": [{
                "cache_key": "still-open",
                "title": "Current Missing Show",
                "list_name": "Watching - Anime",
            }],
        }], dry_run=False, sync_modes={"lists": True, "history": False, "resume": False})

        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "",
            "display_name": "Trakt Resume Progress",
            "source_name": "Trakt",
            "items_fetched": 3,
        }], dry_run=False, sync_modes={"lists": False, "history": False, "resume": True})

        self.assertEqual(len(self.store.get_unresolved_items(created["profile_id"])), 1)
        self.assertEqual(self.store.get_unresolved_items(created["profile_id"])[0]["cache_key"], "still-open")

    def test_public_profile_uses_active_unresolved_snapshot_for_last_results(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Watching - Anime",
            "display_name": "Watching - Anime",
            "source_name": "SIMKL",
            "items_skipped_unresolved": 1,
            "unresolved_items": [{
                "cache_key": "dismiss-me",
                "title": "Dismissed Missing Show",
                "list_name": "Watching - Anime",
            }],
        }], dry_run=False, sync_modes={"lists": True, "history": False, "resume": False})

        self.store.dismiss_unresolved_item(created["profile_id"], "dismiss-me")
        updated = self.store.get_profile_by_id(created["profile_id"], include_credentials=False)

        self.assertEqual(updated["last_results"][0]["items_skipped_unresolved"], 0)

    def test_history_sync_persists_latest_source_cursor(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_history_source": "simkl",
        })

        self.store.claim_profile_for_sync(created["profile_id"], "secret", sync_modes={"lists": False, "history": True, "resume": False})
        updated = self.store.record_sync_success(created["profile_id"], [{
            "list_name": "",
            "display_name": "SIMKL Watch History",
            "source_name": "SIMKL",
            "items_fetched": 5,
            "history_cursor": "2026-04-05T12:00:00Z",
        }], dry_run=False, sync_modes={"lists": False, "history": True, "resume": False})

        private_loaded = self.store.get_private_profile_by_id(created["profile_id"])

        self.assertEqual(updated["activity_results"]["watch_history"]["row"]["history_cursor"], "2026-04-05T12:00:00Z")
        self.assertEqual(private_loaded["activity_state"]["simkl_history_cursor"], "2026-04-05T12:00:00Z")

    def test_update_pair_last_results_merges_per_pair(self) -> None:
        created = self.store.create_profile("pw", self.credentials, self.options)
        profile_id = created["profile_id"]

        self.store.update_pair_last_results(profile_id, [
            {"pair_id": "a-b-1", "name": "anilist → trakt", "added": 19, "removed": 0},
            {"pair_id": "c-d-2", "name": "trakt → simkl", "added": 3, "removed": 1},
        ])
        # Running only one pair later must not discard the other's outcome.
        self.store.update_pair_last_results(profile_id, [
            {"pair_id": "a-b-1", "name": "anilist → trakt", "added": 2, "removed": 0},
        ])

        loaded = self.store.get_private_profile_by_id(profile_id)
        results = loaded["last_pair_results"]
        self.assertEqual(results["a-b-1"]["added"], 2)
        self.assertEqual(results["c-d-2"]["added"], 3)
        self.assertTrue(results["a-b-1"]["timestamp"])
        self.assertFalse(results["a-b-1"]["dry_run"])

    def test_pair_result_provider_urls_never_expose_persisted_api_keys(self) -> None:
        created = self.store.create_profile("pw", self.credentials, self.options)
        profile_id = created["profile_id"]
        self.store.update_pair_last_results(profile_id, [{
            "pair_id": "mdb-target",
            "categories": [{
                "category": "watchlist",
                "errors": ["405 for https://api.mdblist.com/watchlist?apikey=secret-key"],
            }],
        }])

        public = self.store.get_profile_by_id(profile_id, include_credentials=False)
        reloaded = ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        private = reloaded.get_private_profile_by_id(profile_id)

        self.assertNotIn("secret-key", str(public["last_pair_results"]))
        self.assertNotIn("secret-key", str(private["last_pair_results"]))
        self.assertIn("apikey=[redacted]", str(private["last_pair_results"]))

    def test_connection_health_is_sanitized_and_persists(self) -> None:
        created = self.store.create_profile("pw", self.credentials, self.options)
        profile_id = created["profile_id"]

        self.store.record_connection_health(profile_id, [{
            "provider": "pmdb",
            "status": "healthy",
            "code": "ok",
            "message": "PublicMetaDB is reachable.",
            "checked_at": "2026-08-03T12:00:00+00:00",
            "capabilities": {"readable": True, "writable": True},
            "identity": "",
            "recovery_action": "none",
            "api_key": "must-not-persist",
            "upstream_payload": {"secret": True},
        }])

        reloaded = ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        health = reloaded.get_profile_by_id(profile_id)["connection_health"]["pmdb"]
        self.assertEqual(health["status"], "healthy")
        self.assertNotIn("api_key", health)
        self.assertNotIn("upstream_payload", health)

    def test_disconnect_provider_clears_auth_but_keeps_sync_selections(self) -> None:
        credentials = {
            **self.credentials,
            "trakt": {
                "client_id": "trakt-client",
                "client_secret": "trakt-secret",
                "access_token": "trakt-token",
                "refresh_token": "trakt-refresh",
                "access_token_expires_at": "2026-09-01T00:00:00+00:00",
                "username": "viewer",
                "sync_watchlist": True,
                "sync_watchlist_movies": True,
                "sync_watchlist_shows": False,
                "selected_lists": [{"user": "viewer", "slug": "favorites", "name": "Favorites"}],
            },
        }
        created = self.store.create_profile("pw", credentials, self.options)
        self.store.record_connection_health(created["profile_id"], [{
            "provider": "trakt", "status": "healthy", "code": "ok", "message": "ok",
            "checked_at": "2026-08-23T10:00:00+00:00",
            "capabilities": {"readable": True, "writable": True},
        }])

        public = self.store.disconnect_provider(created["profile_id"], "trakt")
        private = self.store.get_private_profile_by_id(created["profile_id"])

        self.assertFalse(public["credentials"]["trakt"]["access_token_saved"])
        self.assertEqual(private["credentials"]["trakt"]["client_id"], "")
        self.assertEqual(private["credentials"]["trakt"]["access_token"], "")
        self.assertTrue(private["credentials"]["trakt"]["sync_watchlist_movies"])
        self.assertEqual(private["credentials"]["trakt"]["selected_lists"][0]["slug"], "favorites")
        self.assertNotIn("trakt", private["connection_health"])


if __name__ == "__main__":
    unittest.main()
