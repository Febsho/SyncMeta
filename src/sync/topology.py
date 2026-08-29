"""Reading a set of routes as a graph, and warning about shapes that fight.

Nothing here forbids a configuration. A user may have a good reason for any of
these, and refusing to run would be worse than the problem. But some shapes have
consequences that are invisible until data starts moving, so they are named.

Three shapes matter:

**A pair of opposing one-way routes** (A → B and B → A) is a two-way route the
engine cannot see. Expressed as one two-way route it reconciles from a single
baseline and knows which side moved; expressed as two, each run decides
independently and a deletion on one side is simply re-added by the other.

**A cycle** (A → B → C → A) gives an item several competing authorities. It
converges for additions — everything ends up everywhere — but a deletion can
travel the loop and come back as an addition.

**Two routes into one destination category** is entirely legitimate and common
(SIMKL and MDBList both feeding Trakt); it is reported only because a removal
there depends on the ownership rules, which is worth knowing before turning one
of them destructive.
"""

from __future__ import annotations

from dataclasses import dataclass

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class TopologyNote:
    """One observation about how the routes fit together."""

    kind: str
    severity: str
    message: str
    routes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "routes": list(self.routes),
        }


def _label(route) -> str:
    name = str(getattr(route, "name", "") or "").strip()
    if name:
        return name
    return f"{getattr(route, 'source', '?')} → {getattr(route, 'target', '?')}"


def _is_two_way(route) -> bool:
    checker = getattr(route, "is_two_way", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return str(getattr(route, "mode", "") or "") == "two_way"


def analyze(routes) -> list[TopologyNote]:
    """Describe the shapes in a set of routes, worst first."""
    active = [
        route for route in routes or []
        if getattr(route, "enabled", True)
    ]
    notes: list[TopologyNote] = []

    one_way = [route for route in active if not _is_two_way(route)]
    seen_pairs: set[frozenset] = set()
    for route in one_way:
        source = str(getattr(route, "source", "") or "")
        target = str(getattr(route, "target", "") or "")
        if not source or not target:
            continue
        for other in one_way:
            if other is route:
                continue
            if (
                str(getattr(other, "source", "")) == target
                and str(getattr(other, "target", "")) == source
            ):
                handle = frozenset({source, target})
                if handle in seen_pairs:
                    continue
                seen_pairs.add(handle)
                notes.append(TopologyNote(
                    kind="opposing_routes", severity=SEVERITY_WARNING,
                    message=(
                        f"{source} and {target} each have a one-way route to the "
                        f"other. One two-way route between them would reconcile "
                        f"from a single baseline; as two, a deletion on one side "
                        f"is re-added by the other."
                    ),
                    routes=(_label(route), _label(other)),
                ))

    # Cycles, over the directed graph of one-way routes.
    edges: dict[str, set] = {}
    route_by_edge: dict[tuple, str] = {}
    for route in one_way:
        source = str(getattr(route, "source", "") or "")
        target = str(getattr(route, "target", "") or "")
        if source and target and source != target:
            edges.setdefault(source, set()).add(target)
            route_by_edge[(source, target)] = _label(route)

    for cycle in _find_cycles(edges):
        if len(cycle) < 3:
            continue  # a two-node cycle is the opposing-routes case above
        path = " → ".join(cycle + [cycle[0]])
        notes.append(TopologyNote(
            kind="cycle", severity=SEVERITY_WARNING,
            message=(
                f"These routes form a loop ({path}). Additions still settle, but "
                f"a deletion can travel the loop and return as an addition, so no "
                f"one service is the authority."
            ),
            routes=tuple(
                route_by_edge.get((cycle[i], cycle[(i + 1) % len(cycle)]), "")
                for i in range(len(cycle))
            ),
        ))

    # Several routes writing the same destination category.
    destinations: dict[tuple, list] = {}
    for route in active:
        target = str(getattr(route, "target", "") or "")
        target_list = str(getattr(route, "target_list", "") or "")
        for category in getattr(route, "categories", ()) or ():
            destinations.setdefault((target, target_list, category), []).append(route)
    for (target, _list, category), group in sorted(destinations.items()):
        if len(group) < 2:
            continue
        notes.append(TopologyNote(
            kind="shared_destination", severity=SEVERITY_INFO,
            message=(
                f"{len(group)} routes write {category} to {target}. That is fine, "
                f"but an item is only removed once no route still requires it — "
                f"so a removal may not happen when one of them expects it to."
            ),
            routes=tuple(_label(route) for route in group),
        ))

    order = {SEVERITY_WARNING: 0, SEVERITY_INFO: 1}
    return sorted(notes, key=lambda note: (order.get(note.severity, 9), note.kind))


def _find_cycles(edges: dict[str, set]) -> list[list[str]]:
    """Every simple cycle in a small directed graph.

    Depth-first with the current path as the stack. Route graphs are tiny — a
    handful of providers — so this does not need to be clever, only correct and
    terminating.
    """
    found: list[list[str]] = []
    seen: set[frozenset] = set()

    def walk(node: str, path: list[str]) -> None:
        for neighbour in sorted(edges.get(node, ())):
            if neighbour in path:
                cycle = path[path.index(neighbour):]
                handle = frozenset(cycle)
                if len(cycle) > 2 and handle not in seen:
                    seen.add(handle)
                    found.append(cycle)
                continue
            walk(neighbour, path + [neighbour])

    for start in sorted(edges):
        walk(start, [start])
    return found
