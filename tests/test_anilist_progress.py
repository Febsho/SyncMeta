"""Tests for turning TMDB-keyed history back into AniList progress counts.

Every rule here exists because AniList's progress number is written
*absolutely*: a wrong answer does not add a row that can be deleted, it
overwrites what the user actually watched. These tests pin the four refusals
that make the write non-destructive.
"""

import unittest
from unittest.mock import patch

from src.anilist_progress import (
    build_reverse_index,
    plan_progress_updates,
)


def _entry(anilist_id: int, title: str, episodes: int, progress: int = 0,
           status: str = "CURRENT", media_type: str = "tv") -> dict:
    return {
        "anilist_id": str(anilist_id),
        "title": title,
        "media_type": media_type,
        "simkl_type": "anime",
        "anilist_episode_count": episodes,
        "anilist_progress": progress,
        "anilist_status": status,
    }


def _watched(tmdb_id: str, season: int | None, episode: int | None) -> dict:
    row = {"tmdb_id": tmdb_id, "media_type": "tv" if season is not None else "movie"}
    if season is not None:
        row["season"] = season
        row["episode"] = episode
    return row


#: Forward mapping used by the fake enrich_identity below.
#: Entry 1 is the first cour (TMDB season 1); entry 2 is the sequel cour, which
#: TMDB stores as season 2 of the same series — the case the whole offset
#: machinery exists for.
_FORWARD = {
    ("1", 1): (1, 1), ("1", 2): (1, 2), ("1", 3): (1, 3), ("1", 4): (1, 4),
    ("2", 1): (2, 1), ("2", 2): (2, 2), ("2", 3): (2, 3),
}


def _fake_enrich(item: dict) -> dict:
    """Stand-in for the real Fribb/offset forward mapper."""
    anilist_id = str(item.get("anilist_id") or "")
    if str(item.get("media_type") or "").lower() == "movie":
        return {**item, "tmdb_id": f"movie-{anilist_id}", "media_type": "movie"}
    episode = item.get("episode")
    mapped = _FORWARD.get((anilist_id, episode))
    if mapped is None:
        return {**item, "tmdb_id": None}
    season, number = mapped
    return {**item, "tmdb_id": "900", "media_type": "tv", "season": season, "episode": number}


class ReverseIndexTests(unittest.TestCase):
    def test_index_inverts_the_forward_mapper(self) -> None:
        with patch("src.providers.enrich_identity", _fake_enrich):
            index, ambiguous = build_reverse_index([
                _entry(1, "Cour One", episodes=4),
                _entry(2, "Cour Two", episodes=3),
            ])

        self.assertEqual(ambiguous, set())
        # A sequel cour's local episode 1 is TMDB S2E1 — and must map back to
        # the sequel entry, not to the root series.
        # Coordinates are normalized to int ids so a "900" from one
        # service and a 900 from another are the same key.
        self.assertEqual(index[(900, 2, 1)], (2, 1))
        self.assertEqual(index[(900, 1, 3)], (1, 3))

    def test_a_coordinate_two_entries_claim_is_refused(self) -> None:
        """Overlapping mappings happen; picking one writes the wrong cour."""
        def overlapping(item: dict) -> dict:
            return {**item, "tmdb_id": "900", "media_type": "tv", "season": 1, "episode": 1}

        with patch("src.providers.enrich_identity", overlapping):
            index, ambiguous = build_reverse_index([
                _entry(1, "One", episodes=1),
                _entry(2, "Two", episodes=1),
            ])

        self.assertIn((900, 1, 1), ambiguous)
        self.assertNotIn((900, 1, 1), index)


class ProgressPlanTests(unittest.TestCase):
    def _plan(self, entries, items):
        with patch("src.providers.enrich_identity", _fake_enrich):
            return plan_progress_updates(entries, items)

    def test_a_full_contiguous_history_advances_progress(self) -> None:
        plan = self._plan(
            [_entry(1, "Cour One", episodes=4, progress=0)],
            [_watched("900", 1, 1), _watched("900", 1, 2), _watched("900", 1, 3)],
        )

        self.assertEqual([(u.media_id, u.new_progress) for u in plan.updates], [(1, 3)])

    def test_progress_stops_at_a_gap_rather_than_claiming_it(self) -> None:
        """Progress means "watched up to N" — 4 would claim episode 3, which
        this history does not contain."""
        plan = self._plan(
            [_entry(1, "Cour One", episodes=4, progress=0)],
            [_watched("900", 1, 1), _watched("900", 1, 2), _watched("900", 1, 4)],
        )

        self.assertEqual([u.new_progress for u in plan.updates], [2])

    def test_history_that_starts_mid_season_writes_nothing(self) -> None:
        plan = self._plan(
            [_entry(1, "Cour One", episodes=4, progress=0)],
            [_watched("900", 1, 3), _watched("900", 1, 4)],
        )

        self.assertEqual(plan.updates, [])

    def test_progress_is_never_rolled_back(self) -> None:
        """The guard that makes this non-destructive: a partial or stale
        history must not undo a count the user set themselves."""
        plan = self._plan(
            [_entry(1, "Cour One", episodes=4, progress=4)],
            [_watched("900", 1, 1), _watched("900", 1, 2)],
        )

        self.assertEqual(plan.updates, [])
        self.assertEqual(plan.would_regress, 1)

    def test_an_entry_already_at_the_right_count_is_left_alone(self) -> None:
        plan = self._plan(
            [_entry(1, "Cour One", episodes=4, progress=2)],
            [_watched("900", 1, 1), _watched("900", 1, 2)],
        )

        self.assertEqual(plan.updates, [])
        self.assertEqual(plan.already_current, 1)

    def test_progress_is_clamped_to_the_entrys_episode_count(self) -> None:
        with patch("src.providers.enrich_identity", _fake_enrich):
            plan = plan_progress_updates(
                [_entry(1, "Cour One", episodes=2, progress=0)],
                [_watched("900", 1, 1), _watched("900", 1, 2), _watched("900", 1, 3)],
            )

        self.assertEqual([u.new_progress for u in plan.updates], [2])

    def test_a_sequel_cour_gets_its_own_progress(self) -> None:
        """The multi-season case: TMDB season 2 belongs to the second AniList
        entry, and writing it onto the root entry would be wrong twice over."""
        plan = self._plan(
            [_entry(1, "Cour One", episodes=4, progress=4),
             _entry(2, "Cour Two", episodes=3, progress=0)],
            [_watched("900", 2, 1), _watched("900", 2, 2)],
        )

        self.assertEqual([(u.media_id, u.new_progress) for u in plan.updates], [(2, 2)])

    def test_history_for_an_anime_the_user_does_not_track_is_skipped(self) -> None:
        """Only entries already on the user's list are in the index, so a pair
        can never conjure a list entry out of a watch record."""
        plan = self._plan(
            [_entry(1, "Cour One", episodes=4)],
            [_watched("555", 1, 1)],
        )

        self.assertEqual(plan.updates, [])
        self.assertEqual(plan.unmatched, 1)

    def test_the_entrys_status_is_preserved_on_the_update(self) -> None:
        plan = self._plan(
            [_entry(1, "Cour One", episodes=4, progress=0, status="PAUSED")],
            [_watched("900", 1, 1)],
        )

        self.assertEqual(plan.updates[0].status, "PAUSED")

    def test_no_items_means_no_reads_and_no_updates(self) -> None:
        self.assertEqual(plan_progress_updates([_entry(1, "One", 4)], []).updates, [])
        self.assertEqual(plan_progress_updates([], [_watched("900", 1, 1)]).updates, [])


if __name__ == "__main__":
    unittest.main()
