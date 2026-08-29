"""Finding plays that were recorded twice.

The engine will never remove these on its own: history is a union, so a
duplicate looks exactly like a rewatch it is supposed to preserve. The only
thing separating them is the same tolerance window the planner uses, applied
backwards over what is already stored — which is why scanning is the default and
deleting has to be asked for.
"""

import unittest

from src.sync.duplicates import redundant_plays, scan


def _play(watched_at, *, episode=1, record_id=None, title="Breaking Bad"):
    return {
        "title": title, "media_type": "tv", "tmdb_id": "1396",
        "ids": {"tmdb": "1396"}, "season": 1, "episode": episode,
        "watched_at": watched_at, "pmdb_item_id": record_id,
    }


class ScanTests(unittest.TestCase):
    def test_one_play_is_not_a_duplicate(self) -> None:
        report = scan([_play("2024-01-01T20:00:00Z")])
        self.assertEqual(report.clusters, [])
        self.assertEqual(report.redundant_plays, 0)

    def test_the_same_viewing_recorded_three_times_is_one_cluster(self) -> None:
        report = scan([
            _play("2024-01-01T20:00:00Z"),
            _play("2024-01-01T20:00:37Z"),
            _play("2024-01-01T20:01:12Z"),
        ])
        self.assertEqual(len(report.clusters), 1)
        self.assertEqual(report.redundant_plays, 2)

    def test_a_genuine_rewatch_is_not_a_duplicate(self) -> None:
        report = scan([
            _play("2024-01-01T20:00:00Z"), _play("2024-08-20T20:00:00Z"),
        ])
        self.assertEqual(report.clusters, [])

    def test_a_rewatch_with_its_own_duplicates_clusters_separately(self) -> None:
        report = scan([
            _play("2024-01-01T20:00:00Z"), _play("2024-01-01T20:00:30Z"),
            _play("2024-08-20T20:00:00Z"), _play("2024-08-20T20:00:30Z"),
        ])
        self.assertEqual(len(report.clusters), 2)
        self.assertEqual(report.redundant_plays, 2)

    def test_a_chain_of_close_plays_is_one_viewing(self) -> None:
        # Each is within the window of the previous, though the first and last
        # are not. Three services relaying one watch produces exactly this.
        report = scan([
            _play("2024-01-01T20:00:00Z"),
            _play("2024-01-01T20:12:00Z"),
            _play("2024-01-01T20:24:00Z"),
        ])
        self.assertEqual(len(report.clusters), 1)
        self.assertEqual(report.redundant_plays, 2)

    def test_different_episodes_never_cluster(self) -> None:
        report = scan([
            _play("2024-01-01T20:00:00Z", episode=1),
            _play("2024-01-01T20:00:20Z", episode=2),
        ])
        self.assertEqual(report.clusters, [])

    def test_rows_without_a_timestamp_are_ignored(self) -> None:
        # Watched state, not a play: nothing to compare and nothing to delete.
        report = scan([_play(None), _play(None), _play("2024-01-01T20:00:00Z")])
        self.assertEqual(report.clusters, [])
        self.assertEqual(report.rows_scanned, 3)

    def test_the_earliest_play_is_the_one_kept(self) -> None:
        report = scan([
            _play("2024-01-01T20:01:00Z", record_id="b"),
            _play("2024-01-01T20:00:00Z", record_id="a"),
        ])
        cluster = report.clusters[0]
        self.assertEqual(cluster.keep["pmdb_item_id"], "a")
        self.assertEqual([p["pmdb_item_id"] for p in cluster.redundant], ["b"])

    def test_the_window_is_configurable(self) -> None:
        rows = [_play("2024-01-01T20:00:00Z"), _play("2024-01-01T20:30:00Z")]
        self.assertEqual(scan(rows).clusters, [])
        self.assertEqual(len(scan(rows, window=3600).clusters), 1)


class RepairSetTests(unittest.TestCase):
    def test_only_the_redundant_plays_are_offered_for_deletion(self) -> None:
        report = scan([
            _play("2024-01-01T20:00:00Z", record_id="a"),
            _play("2024-01-01T20:00:30Z", record_id="b"),
            _play("2024-01-01T20:01:00Z", record_id="c"),
            _play("2024-08-20T20:00:00Z", record_id="d"),
        ])
        doomed = [play["pmdb_item_id"] for play in redundant_plays(report)]
        self.assertEqual(sorted(doomed), ["b", "c"])

    def test_nothing_to_repair_is_an_empty_list(self) -> None:
        self.assertEqual(redundant_plays(scan([_play("2024-01-01T20:00:00Z")])), [])


class ReportShapeTests(unittest.TestCase):
    def test_the_payload_reports_what_it_looked_at(self) -> None:
        report = scan([
            _play("2024-01-01T20:00:00Z"), _play("2024-01-01T20:00:30Z"),
        ])
        payload = report.to_dict()
        self.assertEqual(payload["rows_scanned"], 2)
        self.assertEqual(payload["episodes_scanned"], 1)
        self.assertEqual(payload["affected_episodes"], 1)
        self.assertEqual(payload["redundant_plays"], 1)
        self.assertEqual(payload["clusters"][0]["count"], 2)

    def test_a_huge_report_is_truncated_for_the_ui(self) -> None:
        rows = []
        for index in range(300):
            rows.append(_play("2024-01-01T20:00:00Z", episode=index))
            rows.append(_play("2024-01-01T20:00:30Z", episode=index))
        payload = scan(rows).to_dict(limit=50)
        self.assertEqual(len(payload["clusters"]), 50)
        self.assertEqual(payload["truncated"], 250)
        self.assertEqual(payload["redundant_plays"], 300)
