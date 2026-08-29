"""Reading a set of routes as a graph.

Nothing here forbids a configuration — a user may have a reason for any of them,
and refusing to run would be worse than the problem. These shapes are named
because their consequences are invisible until data starts moving.
"""

import unittest

from src.config import SyncPair
from src.sync.topology import SEVERITY_INFO, SEVERITY_WARNING, analyze


def _route(source, target, **overrides):
    raw = {
        "source": source, "target": target,
        "categories": ["watchlist"], "removal_mode": "additive",
    }
    raw.update(overrides)
    return SyncPair.from_dict(raw)


def _kinds(notes):
    return [note.kind for note in notes]


class OpposingRouteTests(unittest.TestCase):
    def test_two_one_way_routes_facing_each_other_are_flagged(self) -> None:
        notes = analyze([_route("trakt", "simkl"), _route("simkl", "trakt")])
        self.assertIn("opposing_routes", _kinds(notes))
        note = next(n for n in notes if n.kind == "opposing_routes")
        self.assertEqual(note.severity, SEVERITY_WARNING)
        self.assertIn("two-way", note.message)

    def test_the_pair_is_reported_once_not_twice(self) -> None:
        notes = analyze([_route("trakt", "simkl"), _route("simkl", "trakt")])
        self.assertEqual(_kinds(notes).count("opposing_routes"), 1)

    def test_a_real_two_way_route_is_not_flagged(self) -> None:
        notes = analyze([_route("trakt", "simkl", mode="two_way")])
        self.assertNotIn("opposing_routes", _kinds(notes))

    def test_one_direction_alone_is_fine(self) -> None:
        self.assertEqual(analyze([_route("trakt", "simkl")]), [])


class CycleTests(unittest.TestCase):
    def test_a_three_node_loop_is_flagged(self) -> None:
        notes = analyze([
            _route("trakt", "simkl"), _route("simkl", "pmdb"), _route("pmdb", "trakt"),
        ])
        self.assertIn("cycle", _kinds(notes))
        self.assertIn("loop", next(n for n in notes if n.kind == "cycle").message)

    def test_a_chain_without_a_loop_is_fine(self) -> None:
        notes = analyze([
            _route("trakt", "library"), _route("library", "pmdb"),
        ])
        self.assertNotIn("cycle", _kinds(notes))

    def test_a_hub_is_not_a_cycle(self) -> None:
        # The recommended shape: everything into the Library and back out.
        notes = analyze([
            _route("trakt", "library"), _route("simkl", "library"),
            _route("library", "pmdb"),
        ])
        self.assertNotIn("cycle", _kinds(notes))

    def test_a_loop_is_reported_once(self) -> None:
        notes = analyze([
            _route("a", "b"), _route("b", "c"), _route("c", "a"),
        ])
        self.assertEqual(_kinds(notes).count("cycle"), 1)


class SharedDestinationTests(unittest.TestCase):
    def test_two_routes_into_one_destination_are_noted(self) -> None:
        notes = analyze([_route("simkl", "trakt"), _route("mdblist", "trakt")])
        note = next(n for n in notes if n.kind == "shared_destination")
        self.assertEqual(note.severity, SEVERITY_INFO)
        self.assertIn("only removed once no route still requires it", note.message)

    def test_different_categories_do_not_contend(self) -> None:
        notes = analyze([
            _route("simkl", "trakt", categories=["watchlist"]),
            _route("mdblist", "trakt", categories=["collection"]),
        ])
        self.assertNotIn("shared_destination", _kinds(notes))


class OrderingAndScopeTests(unittest.TestCase):
    def test_warnings_come_before_information(self) -> None:
        notes = analyze([
            _route("simkl", "trakt"), _route("mdblist", "trakt"),
            _route("trakt", "simkl"),
        ])
        severities = [note.severity for note in notes]
        self.assertEqual(severities, sorted(severities, key=lambda s: s != SEVERITY_WARNING))

    def test_a_disabled_route_is_ignored(self) -> None:
        notes = analyze([
            _route("trakt", "simkl"), _route("simkl", "trakt", enabled=False),
        ])
        self.assertEqual(notes, [])

    def test_no_routes_is_no_notes(self) -> None:
        self.assertEqual(analyze([]), [])
        self.assertEqual(analyze(None), [])

    def test_notes_serialize_for_the_ui(self) -> None:
        notes = analyze([_route("trakt", "simkl"), _route("simkl", "trakt")])
        payload = notes[0].to_dict()
        self.assertEqual(
            sorted(payload), ["kind", "message", "routes", "severity"],
        )
