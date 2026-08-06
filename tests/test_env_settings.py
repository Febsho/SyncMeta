import json
import os
import tempfile
import unittest
from pathlib import Path

from src.env_settings import (
    CLEAR_SENTINEL,
    LOCKED_KEYS,
    SETTINGS_BY_KEY,
    SettingsError,
    SettingsStore,
    apply_overrides,
    describe,
)


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "settings.json"
        self.store = SettingsStore(self.path)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(self.store.overrides(), {})

    def test_values_round_trip_through_the_file(self) -> None:
        self.store.set_many({"SYNCMETA_MAX_CONCURRENT_SYNCS": "3"})
        self.assertEqual(SettingsStore(self.path).overrides(), {"SYNCMETA_MAX_CONCURRENT_SYNCS": "3"})

    def test_the_file_is_written_atomically_and_privately(self) -> None:
        self.store.set_many({"SITE_ACCESS_PASSWORD": "hunter2"})
        self.assertEqual(oct(self.path.stat().st_mode)[-3:], "600")
        self.assertEqual(json.loads(self.path.read_text())["SITE_ACCESS_PASSWORD"], "hunter2")

    def test_only_registered_keys_are_writable(self) -> None:
        for key in ("SYNCMETA_MASTER_KEY", "PROFILE_STORE_FILE", "PATH", "LD_PRELOAD"):
            with self.assertRaises(SettingsError, msg=key):
                self.store.set_many({key: "x"})

    def test_out_of_range_values_are_refused(self) -> None:
        with self.assertRaises(SettingsError):
            self.store.set_many({"SYNCMETA_MAX_CONCURRENT_SYNCS": "999"})
        with self.assertRaises(SettingsError):
            self.store.set_many({"SYNCMETA_TRAKT_READ_TIMEOUT": "0"})
        with self.assertRaises(SettingsError):
            self.store.set_many({"SYNCMETA_TRAKT_READ_TIMEOUT": "twenty"})

    def test_one_bad_field_does_not_half_apply_the_batch(self) -> None:
        with self.assertRaises(SettingsError):
            self.store.set_many({
                "SYNCMETA_MAX_CONCURRENT_SYNCS": "2",
                "SYNCMETA_TRAKT_READ_TIMEOUT": "nope",
            })
        self.assertEqual(self.store.overrides(), {})
        self.assertFalse(self.path.exists())

    def test_a_blank_secret_means_unchanged_not_cleared(self) -> None:
        self.store.set_many({"SITE_ACCESS_PASSWORD": "keepme"})
        self.store.set_many({"SITE_ACCESS_PASSWORD": ""})
        self.assertEqual(self.store.overrides()["SITE_ACCESS_PASSWORD"], "keepme")

    def test_a_secret_can_be_cleared_with_the_sentinel(self) -> None:
        self.store.set_many({"SITE_ACCESS_PASSWORD": "keepme"})
        self.store.set_many({"SITE_ACCESS_PASSWORD": CLEAR_SENTINEL})
        self.assertNotIn("SITE_ACCESS_PASSWORD", self.store.overrides())

    def test_the_admin_password_cannot_be_cleared(self) -> None:
        # Clearing it disables /admin entirely, so the panel would be locking
        # itself out — that is a mistake, not a setting.
        with self.assertRaises(SettingsError):
            self.store.set_many({"ADMIN_PASSWORD": CLEAR_SENTINEL})

    def test_booleans_accept_the_usual_spellings(self) -> None:
        for raw, expected in (("yes", "1"), ("true", "1"), ("1", "1"), ("no", "0"), ("", "0")):
            self.store.set_many({"DISABLE_PROFILE_SCHEDULER": raw})
            self.assertEqual(self.store.overrides()["DISABLE_PROFILE_SCHEDULER"], expected)

    def test_reset_drops_the_override(self) -> None:
        self.store.set_many({"SYNCMETA_MAX_CONCURRENT_SYNCS": "3"})
        self.assertTrue(self.store.reset("SYNCMETA_MAX_CONCURRENT_SYNCS"))
        self.assertFalse(self.store.reset("SYNCMETA_MAX_CONCURRENT_SYNCS"))
        self.assertEqual(self.store.overrides(), {})

    def test_a_corrupt_file_is_ignored_rather_than_fatal(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(SettingsStore(self.path).overrides(), {})

    def test_stored_keys_that_no_longer_exist_are_dropped(self) -> None:
        self.path.write_text(json.dumps({"SYNCMETA_GONE": "1", "SYNCMETA_MAX_CONCURRENT_SYNCS": "2"}))
        self.assertEqual(SettingsStore(self.path).overrides(), {"SYNCMETA_MAX_CONCURRENT_SYNCS": "2"})

    def test_a_stored_value_that_is_now_out_of_range_is_ignored(self) -> None:
        self.path.write_text(json.dumps({"SYNCMETA_MAX_CONCURRENT_SYNCS": "9999"}))
        self.assertEqual(SettingsStore(self.path).overrides(), {})


class ApplyAndDescribeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = SettingsStore(Path(self._dir.name) / "settings.json")

    def tearDown(self) -> None:
        os.environ.pop("SYNCMETA_MAX_CONCURRENT_SYNCS", None)
        self._dir.cleanup()

    def test_apply_overrides_wins_over_the_process_environment(self) -> None:
        os.environ["SYNCMETA_MAX_CONCURRENT_SYNCS"] = "1"
        self.store.set_many({"SYNCMETA_MAX_CONCURRENT_SYNCS": "4"})
        apply_overrides(self.store)
        self.assertEqual(os.environ["SYNCMETA_MAX_CONCURRENT_SYNCS"], "4")

    def test_describe_reports_where_each_value_came_from(self) -> None:
        self.store.set_many({"SYNCMETA_MAX_CONCURRENT_SYNCS": "4"})
        payload = describe(self.store, {"SYNCMETA_TRAKT_READ_TIMEOUT": "30"})
        by_key = {row["key"]: row for row in payload["settings"]}
        self.assertEqual(by_key["SYNCMETA_MAX_CONCURRENT_SYNCS"]["source"], "override")
        self.assertEqual(by_key["SYNCMETA_MAX_CONCURRENT_SYNCS"]["value"], "4")
        self.assertEqual(by_key["SYNCMETA_TRAKT_READ_TIMEOUT"]["source"], "environment")
        self.assertEqual(by_key["SYNCMETA_TRAKT_READ_TIMEOUT"]["value"], "30")
        self.assertEqual(by_key["SYNCMETA_SIMKL_READ_TIMEOUT"]["source"], "default")

    def test_secrets_are_never_sent_to_the_browser(self) -> None:
        self.store.set_many({"ADMIN_PASSWORD": "s3cret", "SITE_ACCESS_PASSWORD": "gate"})
        payload = describe(self.store, {})
        for row in payload["settings"]:
            if row["secret"]:
                self.assertEqual(row["value"], "")
                self.assertTrue(row["is_set"])
            self.assertNotIn("s3cret", json.dumps(row))

    def test_locked_keys_are_shown_but_not_editable(self) -> None:
        payload = describe(self.store, {"SYNCMETA_MASTER_KEY": "abc"})
        locked = {row["key"] for row in payload["locked"]}
        self.assertEqual(locked, {k for k, _ in LOCKED_KEYS})
        self.assertTrue(locked.isdisjoint(SETTINGS_BY_KEY))
        self.assertNotIn("abc", json.dumps(payload))

    def test_a_misspelled_variable_is_called_out(self) -> None:
        # A self-hoster who set ENCRYPTION_KEY expecting it to encrypt the store
        # has no encryption key set at all and otherwise no way to find out.
        payload = describe(self.store, {"ENCRYPTION_KEY": "x", "SYNCMETA_NOT_A_THING": "1"})
        self.assertIn("ENCRYPTION_KEY", payload["unrecognised"])
        self.assertIn("SYNCMETA_NOT_A_THING", payload["unrecognised"])

    def test_variables_we_really_do_read_are_not_called_out(self) -> None:
        payload = describe(self.store, {
            "SYNCMETA_GUNICORN_THREADS": "6",
            "SYNCMETA_MASTER_KEY": "k",
            "SYNCMETA_TRAKT_READ_TIMEOUT": "20",
            "PROFILE_STORE_FILE": "/app/data/profiles.json",
        })
        self.assertEqual(payload["unrecognised"], [])


class RegistryTests(unittest.TestCase):
    def test_every_setting_declares_a_group_that_exists(self) -> None:
        payload = describe(SettingsStore(Path(tempfile.mkdtemp()) / "s.json"), {})
        groups = {g["id"] for g in payload["groups"]}
        for row in payload["settings"]:
            self.assertIn(row["group"], groups, row["key"])

    def test_numeric_settings_have_a_default_inside_their_own_bounds(self) -> None:
        for setting in SETTINGS_BY_KEY.values():
            if setting.kind != "int":
                continue
            value = int(setting.default)
            self.assertGreaterEqual(value, setting.minimum, setting.key)
            self.assertLessEqual(value, setting.maximum, setting.key)

    def test_no_locked_key_is_also_editable(self) -> None:
        for key, _reason in LOCKED_KEYS:
            self.assertNotIn(key, SETTINGS_BY_KEY)


if __name__ == "__main__":
    unittest.main()
