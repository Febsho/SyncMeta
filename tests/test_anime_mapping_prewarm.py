import unittest
from unittest.mock import patch

from src.anime_mapping_store import AnimeMappingStore


class PrewarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AnimeMappingStore()

    def test_it_loads_both_sources(self) -> None:
        calls = []
        with patch.object(self.store, "_ensure_fribb_loaded", lambda: calls.append("fribb")), \
             patch.object(self.store, "_ensure_xml_loaded", lambda: calls.append("xml")):
            results = self.store.prewarm()

        self.assertEqual(calls, ["fribb", "xml"])
        self.assertTrue(results["fribb"]["ok"])
        self.assertTrue(results["anime_lists_xml"]["ok"])

    def test_a_failure_never_escapes(self) -> None:
        # A mapping that cannot be fetched must degrade anime matching, not stop
        # the app from starting — this runs on a boot thread.
        def boom():
            raise RuntimeError("Fribb anime map is unavailable")

        with patch.object(self.store, "_ensure_fribb_loaded", boom), \
             patch.object(self.store, "_ensure_xml_loaded", lambda: None):
            results = self.store.prewarm()

        self.assertFalse(results["fribb"]["ok"])
        self.assertIn("unavailable", results["fribb"]["error"])
        self.assertTrue(results["anime_lists_xml"]["ok"], "one failure must not skip the other")

    def test_the_reason_reaches_the_admin_panel(self) -> None:
        # "Not loaded" alone is indistinguishable from "nothing asked for it
        # yet", which is what made a silently-failing mapping look like anime
        # that simply could not be matched.
        with patch.object(self.store, "_ensure_fribb_loaded", side_effect=RuntimeError("no network")), \
             patch.object(self.store, "_ensure_xml_loaded", lambda: None):
            self.store.prewarm()

        meta = self.store.cache_metadata()
        self.assertEqual(meta["fribb_error"], "no network")
        self.assertEqual(meta["xml_error"], "")
        self.assertTrue(meta["prewarm_started"])

    def test_a_later_success_clears_the_error(self) -> None:
        with patch.object(self.store, "_ensure_fribb_loaded", side_effect=RuntimeError("no network")), \
             patch.object(self.store, "_ensure_xml_loaded", lambda: None):
            self.store.prewarm()
        with patch.object(self.store, "_ensure_fribb_loaded", lambda: None), \
             patch.object(self.store, "_ensure_xml_loaded", lambda: None):
            self.store.prewarm()

        self.assertEqual(self.store.cache_metadata()["fribb_error"], "")

    def test_metadata_is_readable_before_anything_is_loaded(self) -> None:
        meta = AnimeMappingStore().cache_metadata()

        self.assertFalse(meta["fribb"]["loaded"])
        self.assertFalse(meta["prewarm_started"])
        self.assertEqual(meta["fribb_error"], "")


if __name__ == "__main__":
    unittest.main()
