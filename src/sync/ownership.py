"""Who is keeping an item on a destination, and whether anyone still is.

``managed`` is recorded per route, which is right for deciding whether *this*
route may delete something — but wrong for deciding whether it *should*. Two
routes commonly feed one destination:

    SIMKL   → Trakt
    MDBList → Trakt

Both want Movie X there. If it leaves SIMKL, the first route sees a managed item
its source no longer has and removes it — and the second route, whose source
still lists it, silently re-adds it on the next run. The item flickers, and each
run reports work it should not be doing.

The fix is to ask the question at the destination rather than at the route: an
item may go only when *no* active route still requires it. That is a reference
count, and this module computes it from the routes' own current source sets, so
it is always derived from what the run actually read rather than from a stored
counter that could drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def destination_scope(route) -> tuple[str, str, str]:
    """What a route writes into, as a comparable handle.

    Two routes only contend when they write to the same list on the same
    provider; a route writing to a named list has no bearing on the native one.
    """
    return (
        str(getattr(route, "target", "") or ""),
        str(getattr(route, "target_list", "") or ""),
        "",
    )


@dataclass
class OwnershipIndex:
    """Which routes still require each item, per destination scope."""

    #: (scope, category, key) -> set of route ids whose source still has it.
    _required: dict = field(default_factory=dict)
    #: (scope, category, key) -> set of route ids that manage it.
    _managed: dict = field(default_factory=dict)

    def record(self, route_id: str, scope, category: str, *, required_keys, managed_keys=()) -> None:
        """Register one route's view of a destination for this run."""
        for key in required_keys or ():
            self._required.setdefault((scope, str(category), str(key)), set()).add(str(route_id))
        for key in managed_keys or ():
            self._managed.setdefault((scope, str(category), str(key)), set()).add(str(route_id))

    def required_by(self, scope, category: str, key: str) -> set:
        return set(self._required.get((scope, str(category), str(key)), ()))

    def other_route_requires(self, route_id: str, scope, category: str, key: str) -> bool:
        """Whether some *other* route's source still lists this item."""
        return bool(self.required_by(scope, category, key) - {str(route_id)})

    def holders(self, scope, category: str, key: str) -> set:
        return set(self._managed.get((scope, str(category), str(key)), ()))

    def blocked_removals(self, route_id: str, scope, category: str, keys) -> set:
        """Of ``keys``, the ones another active route still needs."""
        return {
            str(key) for key in keys or ()
            if self.other_route_requires(route_id, scope, category, key)
        }
