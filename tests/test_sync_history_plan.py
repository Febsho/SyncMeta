"""Planning history and progress.

History's failure mode is not a missing row but a growing pile of plays nobody
watched, so most of this file is about the difference between *the same event
synced twice* and *the same episode genuinely watched twice*.
"""

import unittest

from src.sync.history import (
    REASON_ALREADY_SYNCED,
    REASON_ECHO,
    REASON_NEW_EVENT,
    REASON_NO_EPISODE,
    REASON_PRESENCE_ONLY,
    REASON_REWATCH,
    REASON_SAME_EVENT,
    REASON_STATE_TARGET,
    event_id,
    plan_history,
)
from src.sync.models import (
    PHASE_ESTABLISHED,
    STATE_PRESENT,
    ItemState,
    RouteBaseline,
)
from src.sync.planner import POLICY_MIRROR
from src.sync.progress import (
    COMPLETED_PERCENT,
    REASON_ALREADY_THERE,
    REASON_COMPLETED,
    REASON_DESTINATION_FURTHER,
    REASON_FURTHER,
    REASON_NEW_POSITION,
    REASON_TOO_EARLY,
    plan_progress,
)


def _ep(watched_at="2024-01-01T20:00:00Z", *, season=1, episode=3, **extra):
    return {
        "title": "Breaking Bad", "media_type": "tv", "tmdb_id": "1396",
        "ids": {"tmdb": "1396"}, "season": season, "episode": episode,
        "watched_at": watched_at, **extra,
    }


KEY = "tv:tmdb:1396:s1e3"


def _baseline(established=True, **kwargs) -> RouteBaseline:
    baseline = RouteBaseline("r1", "history")
    if established:
        baseline.phase = PHASE_ESTABLISHED
        baseline.sync_version = 1
    if kwargs:
        baseline.items[KEY] = ItemState(
            source=STATE_PRESENT, destination=STATE_PRESENT, **kwargs
        )
    return baseline


def _plan(source, destination, baseline=None, *, records_plays=True, policy=""):
    return plan_history(
        route_id="r1", source_rows=source, destination_rows=destination,
        baseline=baseline or _baseline(), target_records_plays=records_plays,
        policy=policy, source_provider="trakt", destination_provider="pmdb",
    )


class EventIdentityTests(unittest.TestCase):
    def test_a_stable_event_id_is_found_wherever_a_provider_puts_it(self) -> None:
        self.assertEqual(event_id({"_syncmeta_event_id": "a"}), "a")
        self.assertEqual(event_id({"history_id": "b"}), "b")
        self.assertEqual(event_id({"pmdb_item_id": "c"}), "c")
        self.assertEqual(event_id({"ids": {"history": "d"}}), "d")

    def test_no_id_is_empty_not_a_guess(self) -> None:
        self.assertEqual(event_id({}), "")
        self.assertEqual(event_id({"history_id": None}), "")


class SameEventSyncedTwiceTests(unittest.TestCase):
    """The headline requirement: one watch stays one play."""

    def test_the_first_sync_writes_the_play(self) -> None:
        result = _plan([_ep()], [])
        self.assertEqual(len(result.plan.additions), 1)
        self.assertEqual(result.counts.new_events, 1)
        self.assertEqual(result.plan.additions[0].reason, REASON_NEW_EVENT)

    def test_the_second_sync_writes_nothing(self) -> None:
        result = _plan([_ep()], [_ep()])
        self.assertTrue(result.plan.is_noop)
        self.assertEqual(result.counts.already_synced, 1)

    def test_a_third_sync_still_writes_nothing(self) -> None:
        for _ in range(3):
            result = _plan([_ep()], [_ep()])
            self.assertTrue(result.plan.is_noop)

    def test_a_stable_event_id_already_carried_is_never_reimported(self) -> None:
        # Strongest layer: survives any later reformatting of the timestamp.
        baseline = _baseline(event_ids=("trakt:99",), plays=("2024-01-01T20:00:00Z",))
        result = _plan([_ep("2024-03-03T03:03:03Z", history_id="trakt:99")], [], baseline)
        self.assertTrue(result.plan.is_noop)
        self.assertEqual(result.plan.skipped[0].reason, REASON_ALREADY_SYNCED)

    def test_differing_timestamp_precision_is_not_a_second_play(self) -> None:
        result = _plan([_ep("2024-01-01T20:00:00.000Z")], [_ep("2024-01-01T20:00:00Z")])
        self.assertTrue(result.plan.is_noop)

    def test_provider_drift_of_a_minute_is_not_a_second_play(self) -> None:
        result = _plan([_ep("2024-01-01T20:01:00Z")], [_ep("2024-01-01T20:00:00Z")])
        self.assertTrue(result.plan.is_noop)
        self.assertEqual(result.plan.skipped[0].reason, REASON_SAME_EVENT)


class GenuineRewatchTests(unittest.TestCase):
    def test_the_same_episode_watched_months_later_is_a_second_play(self) -> None:
        result = _plan([_ep("2024-08-20T20:00:00Z")], [_ep("2024-01-01T20:00:00Z")])
        self.assertEqual(len(result.plan.additions), 1)
        self.assertEqual(result.counts.rewatches, 1)
        self.assertEqual(result.plan.additions[0].reason, REASON_REWATCH)

    def test_both_plays_reach_an_empty_destination(self) -> None:
        result = _plan([_ep("2024-01-01T20:00:00Z"), _ep("2024-08-20T20:00:00Z")], [])
        self.assertEqual(len(result.plan.additions), 2)

    def test_two_drifting_source_rows_are_one_play(self) -> None:
        result = _plan([_ep("2024-01-01T20:00:00Z"), _ep("2024-01-01T20:00:40Z")], [])
        self.assertEqual(len(result.plan.additions), 1)


class EchoProtectionTests(unittest.TestCase):
    """A two-way route must not re-import its own write."""

    def test_a_play_this_route_wrote_is_not_carried_back(self) -> None:
        # The destination now reports the event SyncMeta put there. The route's
        # own record of having written it is what recognises the echo.
        baseline = _baseline(plays=("2024-01-01T20:00:00Z",))
        result = _plan([], [], baseline)
        self.assertTrue(result.plan.is_noop)

    def test_reading_our_own_write_back_from_the_destination_is_not_new(self) -> None:
        baseline = _baseline(plays=("2024-01-01T20:00:00Z",))
        result = _plan([_ep("2024-01-01T20:00:10Z")], [], baseline)
        self.assertTrue(result.plan.is_noop)
        self.assertEqual(result.plan.skipped[0].reason, REASON_ECHO)


class WatchedStateTests(unittest.TestCase):
    """A provider reporting only "watched = true" has no event to carry."""

    def test_a_state_row_creates_the_first_play_of_an_unknown_episode(self) -> None:
        result = _plan([_ep(None, cursor_exempt=True)], [])
        self.assertEqual(len(result.plan.additions), 1)

    def test_a_state_row_never_adds_a_second_play(self) -> None:
        result = _plan([_ep(None, cursor_exempt=True)], [_ep()])
        self.assertTrue(result.plan.is_noop)
        self.assertEqual(result.plan.skipped[0].reason, REASON_PRESENCE_ONLY)

    def test_repeated_state_syncs_create_nothing(self) -> None:
        baseline = _baseline(plays=("2024-01-01T20:00:00Z",))
        for _ in range(3):
            result = _plan([_ep(None, anilist_derived=True)], [_ep()], baseline)
            self.assertEqual(result.plan.additions, ())

    def test_a_derived_row_is_never_a_rewatch(self) -> None:
        # Its date is the entry's and moves whenever the entry is touched.
        result = _plan([_ep("2025-05-05T00:00:00Z", anilist_derived=True)], [_ep()])
        self.assertTrue(result.plan.is_noop)


class StateOnlyTargetTests(unittest.TestCase):
    def test_a_rewatch_is_not_sent_to_a_state_only_destination(self) -> None:
        # It would report one row back however many it was sent, so the extra
        # would look missing on every later run and be re-sent forever.
        result = _plan(
            [_ep("2024-08-20T20:00:00Z")], [_ep("2024-01-01T20:00:00Z")],
            records_plays=False,
        )
        self.assertTrue(result.plan.is_noop)
        self.assertEqual(result.plan.skipped[0].reason, REASON_STATE_TARGET)

    def test_an_unknown_episode_still_reaches_it(self) -> None:
        result = _plan([_ep(season=2, episode=1)], [_ep()], records_plays=False)
        self.assertEqual(len(result.plan.additions), 1)


class HistoryUnionTests(unittest.TestCase):
    """History is never deleted merely because the source lacks it."""

    def test_an_episode_missing_from_the_source_is_kept(self) -> None:
        result = _plan([], [_ep()], _baseline(plays=("2024-01-01T20:00:00Z",)))
        self.assertEqual(result.plan.removals, ())

    def test_mirror_may_remove_a_whole_episode(self) -> None:
        baseline = _baseline(plays=("2024-01-01T20:00:00Z",))
        result = _plan([], [_ep()], baseline, policy=POLICY_MIRROR)
        self.assertEqual(len(result.plan.removals), 1)
        self.assertTrue(result.plan.removals[0].destructive)

    def test_mirror_will_not_remove_without_a_baseline(self) -> None:
        result = _plan([], [_ep()], _baseline(established=False), policy=POLICY_MIRROR)
        self.assertEqual(result.plan.removals, ())

    def test_mirror_will_not_remove_what_the_source_never_had(self) -> None:
        baseline = RouteBaseline("r1", "history", phase=PHASE_ESTABLISHED, sync_version=1)
        baseline.items[KEY] = ItemState(destination=STATE_PRESENT)
        result = _plan([], [_ep()], baseline, policy=POLICY_MIRROR)
        self.assertEqual(result.plan.removals, ())


class EpisodeScopeTests(unittest.TestCase):
    def test_a_show_row_with_no_episode_is_refused(self) -> None:
        row = {"title": "Breaking Bad", "media_type": "tv", "tmdb_id": "1396",
               "watched_at": "2024-01-01T20:00:00Z"}
        result = _plan([row], [])
        self.assertEqual(result.plan.additions, ())
        self.assertEqual(result.counts.unwritable, 1)
        self.assertEqual(result.plan.skipped[0].reason, REASON_NO_EPISODE)

    def test_a_movie_needs_no_episode(self) -> None:
        movie = {"title": "Parasite", "media_type": "movie", "tmdb_id": "496243",
                 "watched_at": "2024-01-01T20:00:00Z"}
        result = _plan([movie], [])
        self.assertEqual(len(result.plan.additions), 1)


class ProjectedStateTests(unittest.TestCase):
    def test_written_plays_are_projected_for_the_baseline(self) -> None:
        result = _plan([_ep()], [])
        self.assertIn("2024-01-01T20:00:00Z", result.projected[KEY].plays)

    def test_a_carried_event_id_is_recorded(self) -> None:
        result = _plan([_ep(history_id="trakt:7")], [])
        self.assertIn("trakt:7", result.projected[KEY].event_ids)


class ProgressPlanTests(unittest.TestCase):
    """Resume: the risk is rewinding somebody's playback."""

    @staticmethod
    def _resume(position_ms, runtime_ms=100_000):
        return {
            "title": "Dune", "media_type": "movie", "tmdb_id": "438631",
            "ids": {"tmdb": "438631"},
            "position_ms": position_ms, "runtime_ms": runtime_ms,
        }

    def _plan(self, source, destination):
        return plan_progress(
            route_id="r1",
            source_by_key={"movie:tmdb:438631": source} if source else {},
            destination_by_key={"movie:tmdb:438631": destination} if destination else {},
            baseline=RouteBaseline("r1", "resume"),
        ).plan

    def test_a_new_position_is_written(self) -> None:
        plan = self._plan(self._resume(40_000), None)
        self.assertEqual(len(plan.additions), 1)
        self.assertEqual(plan.additions[0].reason, REASON_NEW_POSITION)

    def test_a_barely_started_item_is_not_a_resume_point(self) -> None:
        plan = self._plan(self._resume(1_000), None)
        self.assertEqual(plan.additions, ())
        self.assertEqual(plan.skipped[0].reason, REASON_TOO_EARLY)

    def test_a_finished_item_is_not_pushed_on(self) -> None:
        # Carrying it would make a watched title look abandoned near the end.
        plan = self._plan(self._resume(95_000), None)
        self.assertEqual(plan.additions, ())
        self.assertEqual(plan.skipped[0].reason, REASON_COMPLETED)

    def test_a_further_position_updates(self) -> None:
        plan = self._plan(self._resume(60_000), self._resume(20_000))
        self.assertEqual(len(plan.updates), 1)
        self.assertEqual(plan.updates[0].reason, REASON_FURTHER)

    def test_a_shorter_position_never_rewinds_the_destination(self) -> None:
        plan = self._plan(self._resume(20_000), self._resume(60_000))
        self.assertEqual(plan.updates, ())
        self.assertEqual(plan.skipped[0].reason, REASON_DESTINATION_FURTHER)

    def test_a_completed_destination_is_never_rewound(self) -> None:
        # The 95% -> 2% case: a stale record must not clobber a finished one.
        plan = self._plan(self._resume(5_000), self._resume(95_000))
        self.assertEqual(plan.updates, ())

    def test_the_same_position_is_not_rewritten(self) -> None:
        plan = self._plan(self._resume(40_000), self._resume(40_500))
        self.assertEqual(plan.updates, ())
        self.assertEqual(plan.skipped[0].reason, REASON_ALREADY_THERE)

    def test_a_settled_route_writes_nothing(self) -> None:
        plan = self._plan(self._resume(40_000), self._resume(40_000))
        self.assertTrue(plan.is_noop)


class StateOnlyTargetBatchTests(unittest.TestCase):
    """Two plays of one episode in a single run, to a state-only destination.

    The check has to read the ledger rather than the destination's initial
    contents: the first row makes the episode known, and the second must then be
    refused. Looking only at what was there before the run let both through.
    """

    def test_two_plays_in_one_batch_send_only_one(self) -> None:
        result = plan_history(
            route_id="r1",
            source_rows=[_ep("2024-01-01T20:00:00Z"), _ep("2024-08-20T20:00:00Z")],
            destination_rows=[], baseline=_baseline(),
            target_records_plays=False, source_provider="trakt",
            destination_provider="simkl",
        )
        self.assertEqual(len(result.plan.additions), 1)
        self.assertEqual(result.counts.duplicates, 1)

    def test_a_play_recording_destination_takes_both(self) -> None:
        result = plan_history(
            route_id="r1",
            source_rows=[_ep("2024-01-01T20:00:00Z"), _ep("2024-08-20T20:00:00Z")],
            destination_rows=[], baseline=_baseline(),
            target_records_plays=True, source_provider="trakt",
            destination_provider="pmdb",
        )
        self.assertEqual(len(result.plan.additions), 2)
