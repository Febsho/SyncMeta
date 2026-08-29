"""The vocabulary the sync engine reasons in.

Two ideas carry most of the weight here.

**A read that failed is not an empty list.** Every provider fetch reports a
``FetchStatus``, and only an outright success may be used to conclude that
something is *gone*. A timeout, a 401, a rate limit or a half-read page all look
identical to "the user deleted everything" if you only count the items that came
back — and acting on that difference is irreversible.

**State is compared against the last agreement, not against the other side.**
"Missing on the destination" says nothing on its own: the item may be new on the
source, or it may have been deleted on the destination. Only the previous
successful baseline can tell those apart, so an ``ItemState`` records what each
side looked like the last time the two agreed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FetchStatus(str, Enum):
    """How a provider read actually went.

    The distinction that matters is ``trustworthy_for_removals``: a status may
    be perfectly fine to *add* from and still be far too weak to delete from.
    """

    SUCCESS_WITH_ITEMS = "success_with_items"
    SUCCESS_EMPTY = "success_empty"
    PARTIAL = "partial"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    TIMEOUT = "timeout"

    @property
    def succeeded(self) -> bool:
        return self in (self.SUCCESS_WITH_ITEMS, self.SUCCESS_EMPTY)

    @property
    def trustworthy_for_removals(self) -> bool:
        """Whether absence in this read may be read as "the user deleted it".

        ``PARTIAL`` is the subtle one: the call returned 200 and real items, so
        it is fine to add from, but a page that never arrived is indistinguishable
        from a page whose contents were deleted.
        """
        return self.succeeded


@dataclass(frozen=True)
class FetchOutcome:
    """One provider read, with enough context to explain a blocked removal."""

    provider: str
    category: str
    status: FetchStatus
    item_count: int = 0
    error: str = ""
    #: Set when the provider said, or the client detected, that it did not hand
    #: back everything — a page that failed mid-pagination, a truncated response.
    complete: bool = True

    @property
    def trustworthy_for_removals(self) -> bool:
        return self.status.trustworthy_for_removals and self.complete

    def describe(self) -> str:
        if self.trustworthy_for_removals:
            return f"{self.provider} {self.category}: {self.item_count} item(s)"
        reason = self.error or self.status.value.replace("_", " ")
        if self.status.succeeded and not self.complete:
            reason = "provider returned partial data"
        return f"{self.provider} {self.category}: {reason}"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "category": self.category,
            "status": self.status.value,
            "item_count": self.item_count,
            "error": self.error,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "FetchOutcome":
        data = raw if isinstance(raw, dict) else {}
        try:
            status = FetchStatus(str(data.get("status") or FetchStatus.FAILED.value))
        except ValueError:
            status = FetchStatus.FAILED
        return cls(
            provider=str(data.get("provider") or ""),
            category=str(data.get("category") or ""),
            status=status,
            item_count=int(data.get("item_count") or 0),
            error=str(data.get("error") or ""),
            complete=bool(data.get("complete", True)),
        )


#: A route that has never completed a comparison. Its first run may add, but it
#: must not delete: with no baseline, "absent on the source" cannot be
#: distinguished from "the source never had it".
PHASE_INITIALIZING = "baseline_initializing"
PHASE_ESTABLISHED = "established"

#: Item state tokens. Deliberately short strings rather than booleans so a
#: category with richer states (a list status, a progress bucket) can use the
#: same slot without a schema change.
STATE_ABSENT = ""
STATE_PRESENT = "1"


@dataclass
class ItemState:
    """What one canonical item looked like the last time the two sides agreed.

    ``changed_*_at`` hold the *sync version* at which that side last moved, not a
    wall-clock time: providers disagree about modification timestamps and several
    do not expose one at all, so the engine's own run counter is the only
    ordering every route can rely on.
    """

    source: str = STATE_ABSENT
    destination: str = STATE_ABSENT
    synced: str = STATE_ABSENT
    managed: bool = False
    changed_source_at: int = 0
    changed_destination_at: int = 0
    action: str = ""
    #: History only: the play timestamps this route has already carried across
    #: for this episode, and the source event ids behind them. This is what makes
    #: a repeated sync idempotent and, in a two-way route, what stops an event
    #: coming back as if the destination had invented it.
    plays: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """Whether the baseline has ever seen this item on either side."""
        return bool(self.source or self.destination or self.synced)

    def to_list(self) -> list:
        """Pack compactly — a real profile holds tens of thousands of these."""
        packed = [
            self.source, self.destination, self.synced,
            1 if self.managed else 0,
            self.changed_source_at, self.changed_destination_at,
            self.action,
            list(self.plays), list(self.event_ids),
        ]
        while len(packed) > 1 and packed[-1] in ("", 0, []):
            packed.pop()
        return packed

    @classmethod
    def from_list(cls, raw: object) -> "ItemState":
        values = list(raw) if isinstance(raw, (list, tuple)) else []
        values += [None] * (9 - len(values))

        def _tuple(value) -> tuple[str, ...]:
            if not isinstance(value, (list, tuple)):
                return ()
            return tuple(str(entry) for entry in value if str(entry or "").strip())

        return cls(
            source=str(values[0] or ""),
            destination=str(values[1] or ""),
            synced=str(values[2] or ""),
            managed=bool(values[3]),
            changed_source_at=int(values[4] or 0),
            changed_destination_at=int(values[5] or 0),
            action=str(values[6] or ""),
            plays=_tuple(values[7]),
            event_ids=_tuple(values[8]),
        )


@dataclass
class RouteBaseline:
    """The last state a route successfully agreed on, for one category.

    Only ``commit`` advances this. An attempt that failed, was blocked, or only
    partly applied updates the attempt bookkeeping and leaves the agreement
    alone — otherwise a broken run would teach the engine that the user had
    deleted everything it could not read.
    """

    route_id: str
    category: str
    phase: str = PHASE_INITIALIZING
    sync_version: int = 0
    last_successful_sync: str = ""
    last_attempt: str = ""
    last_error: str = ""
    items: dict[str, ItemState] = field(default_factory=dict)
    source_fetch: FetchOutcome | None = None
    destination_fetch: FetchOutcome | None = None

    @property
    def is_initializing(self) -> bool:
        return self.phase != PHASE_ESTABLISHED

    @property
    def allows_removals(self) -> bool:
        """Whether a removal may be concluded from this baseline at all.

        False until one run has completed, so a fresh route — or one migrated
        from the old managed-key store — cannot delete on the strength of a
        comparison it has never actually made.
        """
        return self.phase == PHASE_ESTABLISHED

    def state(self, key: str) -> ItemState:
        return self.items.get(key) or ItemState()

    def managed_keys(self) -> set[str]:
        return {key for key, state in self.items.items() if state.managed}

    def to_dict(self) -> dict:
        out: dict = {
            "route_id": self.route_id,
            "category": self.category,
            "phase": self.phase,
            "sync_version": self.sync_version,
            "last_successful_sync": self.last_successful_sync,
            "last_attempt": self.last_attempt,
            "last_error": self.last_error,
            "items": {key: state.to_list() for key, state in self.items.items()},
        }
        if self.source_fetch is not None:
            out["source_fetch"] = self.source_fetch.to_dict()
        if self.destination_fetch is not None:
            out["destination_fetch"] = self.destination_fetch.to_dict()
        return out

    @classmethod
    def from_dict(cls, raw: object) -> "RouteBaseline":
        data = raw if isinstance(raw, dict) else {}
        raw_items = data.get("items")
        items: dict[str, ItemState] = {}
        if isinstance(raw_items, dict):
            for key, packed in raw_items.items():
                items[str(key)] = ItemState.from_list(packed)
        phase = str(data.get("phase") or PHASE_INITIALIZING)
        return cls(
            route_id=str(data.get("route_id") or ""),
            category=str(data.get("category") or ""),
            phase=phase if phase in (PHASE_INITIALIZING, PHASE_ESTABLISHED) else PHASE_INITIALIZING,
            sync_version=int(data.get("sync_version") or 0),
            last_successful_sync=str(data.get("last_successful_sync") or ""),
            last_attempt=str(data.get("last_attempt") or ""),
            last_error=str(data.get("last_error") or ""),
            items=items,
            source_fetch=(
                FetchOutcome.from_dict(data["source_fetch"])
                if isinstance(data.get("source_fetch"), dict) else None
            ),
            destination_fetch=(
                FetchOutcome.from_dict(data["destination_fetch"])
                if isinstance(data.get("destination_fetch"), dict) else None
            ),
        )


@dataclass
class RouteObservation:
    """What one route actually saw and did for one category, in one run.

    Kept apart from ``PairCategoryStats`` on purpose. The stats object is
    serialized into ``last_pair_results`` and rides the status poll; these key
    sets run to tens of thousands of entries and must never go near it. This is
    consumed server-side by the state store and nowhere else.
    """

    route_id: str
    category: str
    items: dict[str, ItemState] = field(default_factory=dict)
    source_fetch: FetchOutcome | None = None
    destination_fetch: FetchOutcome | None = None
    #: Every planned write either landed or was deliberately skipped. False when
    #: a write raised, which is what separates `commit` from `record_partial`.
    complete: bool = True
    error: str = ""

    @property
    def trustworthy(self) -> bool:
        """Whether this run may be accepted as a new agreement at all."""
        if not self.complete or self.error:
            return False
        for outcome in (self.source_fetch, self.destination_fetch):
            if outcome is None or not outcome.trustworthy_for_removals:
                return False
        return True
