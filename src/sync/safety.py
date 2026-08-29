"""Refuse a plan that is more likely to be a bug than an intention.

A route that would delete most of a list is nearly always a provider hiccup — a
half-read source, a token that just expired, an outage answering 200 with
nothing — and only rarely a person who genuinely emptied their watchlist. The
two are indistinguishable from the data; they differ only in how much they cost
if you are wrong. Pausing costs one run. Being wrong costs the list.

So this module never asks "is this correct". It asks "how much would this
destroy, and how confident is the evidence" — and when the answer is bad it
blocks and explains, rather than continuing quietly.

Two rules the design turns on:

* an automatic run may never bypass a block. The override exists for a person
  looking at a preview who has decided the deletion is right.
* blocking is *demotion*, not failure. The additions in the plan are still
  valid and still run; only the destructive part is held back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .planner import SyncPlan

#: Past these, a removal stops looking like an edit and starts looking like an
#: outage. Either alone is enough to block.
DEFAULT_MAX_REMOVALS = 25
DEFAULT_MAX_REMOVAL_PERCENT = 20

#: Below these a mass deletion is not surprising: emptying a three-item list is
#: 100% and entirely ordinary, and pausing it would be noise, not safety.
MIN_DESTINATION_SIZE = 10
MIN_REMOVALS = 5

#: A run where this share of the source could not be resolved to a known title
#: is a mapping failure, not a library that changed.
UNRESOLVED_SPIKE_PERCENT = 30


@dataclass(frozen=True)
class SafetyVerdict:
    """Whether a plan's destructive half may proceed, and why not."""

    allowed: bool
    blocks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    removals: int = 0
    destination_size: int = 0
    percent: float = 0.0
    overridden: bool = False

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def explain(self) -> str:
        if self.allowed:
            return ""
        return "; ".join(self.blocks)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "blocks": list(self.blocks),
            "warnings": list(self.warnings),
            "removals": self.removals,
            "destination_size": self.destination_size,
            "percent": self.percent,
            "overridden": self.overridden,
        }


@dataclass
class SafetyPolicy:
    """The thresholds a profile syncs under."""

    enabled: bool = True
    max_removals: int = DEFAULT_MAX_REMOVALS
    max_removal_percent: int = DEFAULT_MAX_REMOVAL_PERCENT
    min_destination_size: int = MIN_DESTINATION_SIZE
    min_removals: int = MIN_REMOVALS
    unresolved_spike_percent: int = UNRESOLVED_SPIKE_PERCENT
    #: Set only by a person acting on a preview. An automatic run must always
    #: leave this False — that is the entire point of the guard.
    allow_destructive_override: bool = False
    extra_blocks: tuple[str, ...] = field(default_factory=tuple)


def evaluate(
    plan: SyncPlan,
    *,
    policy: SafetyPolicy | None = None,
    destination_size: int = 0,
    source_size: int = 0,
    source_trustworthy: bool = True,
    destination_trustworthy: bool = True,
    baseline_established: bool = True,
) -> SafetyVerdict:
    """Decide whether ``plan``'s removals may be performed."""
    policy = policy or SafetyPolicy()
    removals = plan.destructive_count
    percent = plan.destructive_percent(destination_size)
    blocks: list[str] = []
    warnings: list[str] = []

    # Hard blocks: conditions where the *evidence* is bad. No override touches
    # these — a person can decide a large deletion is right, but nobody can
    # decide that an unread source really was empty.
    hard: list[str] = []
    if not source_trustworthy:
        hard.append(
            "The source could not be read completely, so items missing from it "
            "may simply not have been returned."
        )
    if not destination_trustworthy:
        hard.append(
            "The destination could not be read completely, so what is actually "
            "there is unknown."
        )
    if not baseline_established:
        hard.append(
            "This route has no confirmed previous sync yet, so a removal cannot "
            "be told apart from an item it has simply never seen."
        )

    # A source that answers with nothing, for a destination that holds plenty,
    # is the shape every mass-deletion incident has.
    if removals and source_size == 0 and destination_size >= policy.min_destination_size:
        hard.append(
            f"The source returned no items at all while the destination holds "
            f"{destination_size}. That is far more often an outage than a "
            f"library someone emptied."
        )

    unresolved = len(plan.unresolved)
    considered = unresolved + source_size
    if considered and (100.0 * unresolved / considered) >= policy.unresolved_spike_percent:
        warnings.append(
            f"{unresolved} of {considered} source items could not be resolved to "
            f"a known title; mappings may be failing."
        )

    if policy.enabled and removals:
        big_enough = (
            destination_size >= policy.min_destination_size
            and removals >= policy.min_removals
        )
        if big_enough:
            if removals > policy.max_removals:
                blocks.append(
                    f"{removals} removals is over the limit of "
                    f"{policy.max_removals} for one run."
                )
            if percent > policy.max_removal_percent:
                blocks.append(
                    f"{removals} removals is {percent}% of the {destination_size} "
                    f"items on the destination, over the {policy.max_removal_percent}% limit."
                )

    blocks.extend(policy.extra_blocks)

    if blocks and not hard and policy.allow_destructive_override:
        # A person looked at this and said do it anyway. Recorded as an override
        # rather than quietly dropped, so the run detail still shows what the
        # guard thought — and only the size thresholds are waived, never a hard
        # block.
        return SafetyVerdict(
            allowed=True, blocks=(), warnings=tuple(warnings + blocks),
            removals=removals, destination_size=destination_size, percent=percent,
            overridden=True,
        )

    everything = hard + blocks
    return SafetyVerdict(
        allowed=not everything, blocks=tuple(everything), warnings=tuple(warnings),
        removals=removals, destination_size=destination_size, percent=percent,
    )


def enforce(plan: SyncPlan, verdict: SafetyVerdict) -> SyncPlan:
    """Return the plan the run may actually perform.

    A blocked plan keeps its additions: whatever made the removals unsafe says
    nothing about items the source positively reported.
    """
    if verdict.allowed or not plan.removals:
        return plan
    return plan.without_removals(
        "Paused by the safety guard: " + verdict.explain()
    )
