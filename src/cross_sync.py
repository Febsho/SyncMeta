"""Cross-service sync: run a one-way pair from one provider to another.

The existing pipeline only ever writes to PublicMetaDB.  A sync pair instead
reads a category from any provider and writes it to any other, so a user can
push Trakt into SIMKL, PublicMetaDB into AniList, and so on, chaining pairs to
build whatever topology they want.

Design notes:

* Pairs are **one-way**.  Both directions is two pairs, which keeps each run's
  intent unambiguous and avoids needing conflict resolution.
* Identity is keyed by ``providers.item_key`` — TMDB where available, because
  that is the id every provider can be mapped to.
* Removal is opt-in per pair.  ``additive`` (the default) never deletes.
  ``managed`` may delete only keys this pair previously wrote, reusing the same
  invariant that protects manually-added PublicMetaDB watchlist entries.
  ``mirror`` may delete anything the source no longer has.
* Reads are cached **for the duration of one batch run** (see ``ReadCache``).
  A realistic setup fans one service out to several others — Trakt → SIMKL,
  Trakt → AniList, Trakt → PMDB — and without this the same Trakt watchlist was
  fetched once per pair, tripling the API cost of the identical data.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from .sync.executor import execute_plan
from .sync.ownership import OwnershipIndex, destination_scope
from .sync.history import plan_history
from .sync.progress import plan_progress
from .sync.planner import (
    SyncPlan,
    normalize_policy,
    plan_membership,
    plan_two_way,
)
from .sync.safety import SafetyPolicy, enforce as enforce_safety, evaluate as evaluate_safety
from .sync.models import (
    ItemState,
    RouteBaseline,
    STATE_ABSENT,
    STATE_PRESENT,
    FetchOutcome,
    FetchStatus,
    ItemState,
    RouteObservation,
)
from .providers import (
    CATEGORY_COLLECTION,
    CATEGORY_HISTORY,
    CATEGORY_RESUME,
    CATEGORY_WATCHLIST,
    MODE_ONE_WAY,
    REMOVAL_ADDITIVE,
    REMOVAL_MANAGED,
    REMOVAL_MIRROR,
    ALL_VISIBILITIES,
    VISIBILITY_PRIVATE,
    enrich_identity,
    has_portable_identity,
    PlaySet,
    item_key,
)

logger = logging.getLogger(__name__)


def _total(totals: dict, key: str) -> int:
    """Read one count out of an adapter's write result.

    Adapters are contracted to return flat integers, but a provider's raw
    response nests counts per media type, and `int()` on that dict raises
    *after* the write has already landed — which reports a successful sync as
    an error and, worse, skips recording the managed keys for what was written,
    so the next run re-adds it all. A shape we did not expect must degrade to a
    count, never to an exception.
    """
    def count(value) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return sum(count(inner) for inner in value.values())
        return 0

    return count((totals or {}).get(key))


def _add_kwargs(adapter, pair) -> dict:
    """Visibility is only passed to adapters that declare they can use it.

    Every other provider ignores list privacy entirely, and sending it anyway
    would make the write signature something all of them — including test
    doubles — have to accept for no benefit.
    """
    if not getattr(adapter, "supports_visibility", False):
        return {}
    return {"visibility": _pair_visibility(pair)}


def _pair_visibility(pair) -> str:
    """The privacy a pair wants for lists it has to create on the target.

    Read defensively: pairs stored before this field existed have no attribute,
    and a missing value must mean private rather than publishing someone's
    watchlist.
    """
    value = str(getattr(pair, "visibility", "") or "").strip().lower()
    return value if value in ALL_VISIBILITIES else VISIBILITY_PRIVATE


class ReadCache:
    """Per-run memo of provider reads, so N pairs sharing a side cost one fetch.

    Scoped to a single batch, never to the service, because a later run must see
    what changed in between.  Correctness rests on the write-through below: once
    a pair writes to a target, any *other* pair in the same batch has to observe
    the new contents or it would re-add what was just written.  The exact list we
    read is updated in place; everything else belonging to that provider is
    dropped rather than guessed at, since a write to one list says nothing about
    the others.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, tuple[str, ...]], list[dict]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(provider: str, category: str, source_lists) -> tuple[str, str, tuple[str, ...]]:
        lists = tuple(sorted(str(k) for k in (source_lists or []) if str(k).strip()))
        return (str(provider), str(category), lists)

    def get_or_fetch(self, provider: str, category: str, source_lists, loader):
        key = self._key(provider, category, source_lists)
        if key in self._entries:
            self.hits += 1
            return list(self._entries[key])
        items = list(loader() or [])
        self.misses += 1
        self._entries[key] = list(items)
        return items

    def apply_write(self, provider: str, category: str, source_lists, *, added, removed) -> None:
        """Fold a completed write into the cache for the list it was read from."""
        key = self._key(provider, category, source_lists)
        current = self._entries.get(key)
        # Drop every other view of this provider — a write invalidates reads we
        # cannot reason about (other lists, other categories of the same item).
        for other in [k for k in self._entries if k[0] == key[0] and k != key]:
            self._entries.pop(other, None)
        if current is None:
            return
        automatic_simkl = any(
            str(value).startswith("auto:simkl:") for value in (source_lists or [])
        )

        def _key(item):
            base = item_key(item)
            if automatic_simkl:
                return f"{str(item.get('_syncmeta_source_status') or '')}:{base}"
            return base

        removed_keys = {_key(item) for item in (removed or [])}
        added_keys = {_key(item) for item in (added or [])}
        kept = [item for item in current if _key(item) not in removed_keys]
        if category != CATEGORY_HISTORY:
            # `add` is an upsert for mutable categories such as resume progress.
            # Replace the cached old value instead of retaining two rows with the
            # same identity until the next full provider read.
            #
            # History is the exception: its rows are plays, so two entries with
            # the same identity are two viewings, and dropping the older one
            # would make a rewatch this batch just wrote look like the only play.
            kept = [item for item in kept if _key(item) not in added_keys]
        kept.extend(added or [])
        self._entries[key] = kept

    def invalidate_provider(self, provider: str) -> None:
        for key in [k for k in self._entries if k[0] == str(provider)]:
            self._entries.pop(key, None)


def _history_adds(source_rows: list[tuple[str, dict]], target_by_key: dict, target_plays: dict) -> list[dict]:
    """Pick the history rows the target is genuinely missing.

    Identity alone cannot answer this. ``item_key`` says *which episode*, so
    watching an episode three times is three rows sharing one key — diffing on
    it keeps the first and silently discards every rewatch, which is why a
    second viewing never reached the other service.

    But nor can an exact timestamp. Services do not agree on *when* a play
    happened — Trakt stamps the scrobble, SIMKL stamps when its server recorded
    it — so the same viewing arrives seconds apart and, matched exactly, looked
    like a fresh rewatch at every hop. Plays are therefore matched within
    ``PLAY_MATCH_WINDOW_SECONDS`` (see ``providers.PlaySet``).

    The ledger starts from what the target already holds and is updated as rows
    are accepted, so two source rows that are themselves a few seconds apart
    cannot both be written either.

    A rewatch only goes to a target that can hold one: a service reporting
    watched *state* hands back a single row however many plays it was sent, so
    the extra would look missing on every later run and be re-sent forever.
    ``records_plays`` decides that, and ``target_plays`` is empty when it is
    false. An episode the target lacks entirely is always added.
    """
    out: list[dict] = []
    ledgers: dict[str, PlaySet] = {}
    for key, item in source_rows:
        present = key in target_by_key
        known = target_plays.get(key)
        if present and not known:
            # Already watched there, and the target keeps no per-play record to
            # attach a rewatch to.
            continue
        ledger = ledgers.get(key)
        if ledger is None:
            ledger = PlaySet(known or ())
            ledgers[key] = ledger
        stamp = item.get("watched_at")
        if present and not ledger.stamped:
            continue
        if ledger.matches(stamp):
            continue
        ledger.add(stamp)
        out.append(item)
    return out


#: A list has to be big enough, and lose enough, for a mass deletion to be
#: surprising. Below these the guard stays silent — removing 2 of 3 items is a
#: perfectly ordinary edit and pausing it would be noise.
#: Categories the baseline planner owns. Membership only — see _plan_category.
_PLANNED_CATEGORIES = frozenset({CATEGORY_WATCHLIST, CATEGORY_COLLECTION})

_GUARD_MIN_TARGET_SIZE = 10
_GUARD_MIN_REMOVALS = 5


@dataclass
class PairCategoryStats:
    """Outcome of syncing one category of one pair."""

    category: str
    source_items: int = 0
    target_items: int = 0
    added: int = 0
    removed: int = 0
    unmapped: int = 0
    skipped_existing: int = 0
    #: Source items the *target* declined to store in this category — not a
    #: failure and not an identity problem, so it is neither an error nor
    #: `unmapped`. PublicMetaDB's native watchlist declines anything that is not
    #: plan-to-watch, which is the case this exists for.
    skipped_unsupported: int = 0
    errors: list[str] = field(default_factory=list)
    managed_keys: list[str] = field(default_factory=list)
    #: Provider reads this category answered from the batch cache instead of the
    #: network. Surfaced so the saving is visible rather than merely claimed.
    cached_reads: int = 0
    #: Two-way only: what went back the other way. `added`/`removed` stay the
    #: totals so one-way callers and the dashboard need no special case.
    added_back: int = 0
    removed_back: int = 0
    #: Removals the safety guard refused to perform, one entry per side it
    #: stopped. Reported rather than raised: nothing was deleted, and the user
    #: decides whether the run was right.
    blocked_removals: list[dict] = field(default_factory=list)
    #: Two-way only: items both sides changed since the baseline, in ways that
    #: disagree. Reported rather than resolved — letting run order pick a winner
    #: silently discards one of the user's two edits.
    conflicts_detected: int = 0
    changes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "source_items": self.source_items,
            "target_items": self.target_items,
            "added": self.added,
            "removed": self.removed,
            "unmapped": self.unmapped,
            "skipped_existing": self.skipped_existing,
            "skipped_unsupported": self.skipped_unsupported,
            "cached_reads": self.cached_reads,
            "added_back": self.added_back,
            "removed_back": self.removed_back,
            "conflicts_detected": self.conflicts_detected,
            "blocked_removals": [dict(entry) for entry in self.blocked_removals],
            "changes": [dict(entry) for entry in self.changes],
            "errors": list(self.errors),
        }


@dataclass
class PairRunStats:
    pair_id: str
    name: str
    source: str
    target: str
    removal_mode: str
    dry_run: bool = False
    mode: str = "one_way"
    categories: list[PairCategoryStats] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(c.added for c in self.categories)

    @property
    def removed(self) -> int:
        return sum(c.removed for c in self.categories)

    @property
    def unmapped(self) -> int:
        return sum(c.unmapped for c in self.categories)

    @property
    def cached_reads(self) -> int:
        return sum(c.cached_reads for c in self.categories)

    @property
    def checked(self) -> int:
        """Source items this run actually inspected, across every category."""
        return sum(c.source_items for c in self.categories)

    @property
    def blocked_removals(self) -> list[dict]:
        return [entry for c in self.categories for entry in c.blocked_removals]

    @property
    def error_count(self) -> int:
        return len(self.errors) + sum(len(c.errors) for c in self.categories)

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "removal_mode": self.removal_mode,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "added": self.added,
            "removed": self.removed,
            "checked": self.checked,
            "unmapped": self.unmapped,
            "cached_reads": self.cached_reads,
            "blocked_removals": self.blocked_removals,
            "error_count": self.error_count,
            "categories": [c.to_dict() for c in self.categories],
            "errors": list(self.errors),
        }


class CrossSyncService:
    """Executes sync pairs against a set of provider adapters."""

    def __init__(
        self,
        adapters: dict,
        *,
        dry_run: bool = False,
        managed_keys: dict | None = None,
        cancel_requested_callback=None,
        status_callback=None,
        guard_large_removals: bool = True,
        guard_removal_percent: int = 20,
        state_store=None,
        allow_destructive_override: bool = False,
    ):
        self._adapters = dict(adapters or {})
        # Baselines, when the caller has them. Optional so every existing test
        # and call site keeps working; without one the planner simply reports
        # from an empty baseline, which is the safe reading.
        self._state_store = state_store
        self._dry_run = bool(dry_run)
        # A run that would empty a list is far more often a provider hiccup — a
        # half-read source, a revoked token that returned nothing — than a user
        # deleting everything. Refusing it costs one paused run; performing it
        # costs the list.
        self._guard_large_removals = bool(guard_large_removals)
        try:
            percent = int(guard_removal_percent)
        except (TypeError, ValueError):
            percent = 20
        self._guard_removal_percent = min(100, max(1, percent))
        # Only a person acting on a preview may waive the size thresholds. An
        # automatic run leaves this False and therefore cannot, and no override
        # ever waives a *hard* block — an unreadable source or a missing
        # baseline is bad evidence, not a decision anyone gets to make.
        self._allow_destructive_override = bool(allow_destructive_override)
        # {pair_id: {category: [key, ...]}} — keys this pair has written before.
        self._managed_keys = {
            str(pair_id): {
                str(category): list(keys or [])
                for category, keys in (categories or {}).items()
            }
            for pair_id, categories in (managed_keys or {}).items()
        }
        # What each route actually observed this run, for the baseline store.
        # Server-side only: these key sets are far too large to ride /status,
        # which is why they are not folded into PairCategoryStats.
        self._route_states: dict[tuple[str, str], RouteObservation] = {}
        # Plans built alongside the live decision. Reported, not yet obeyed —
        # switching execution onto them is the next step, and running them in
        # parallel first is what makes that switch reviewable against real data.
        self._plans: dict[tuple[str, str], SyncPlan] = {}
        self._plan_verdicts: dict = {}
        self._plan_blocked_items: dict = {}
        self._ownership = OwnershipIndex()
        self._cancel_requested_callback = cancel_requested_callback
        self._status_callback = status_callback
        # Set only while a batch is in flight; see _batch_cache().
        self._read_cache: ReadCache | None = None
        self.last_run_cache_hits = 0
        self.last_run_provider_reads = 0

    # ── helpers ────────────────────────────────────────────────────────────

    @contextmanager
    def _batch_cache(self):
        """Give the enclosed work one shared read cache.

        Re-entrant on purpose: ``run_pairs`` opens the batch so every pair in it
        shares reads, while a bare ``run_pair`` opens its own. Nesting must not
        reset the outer cache, or the batch-level saving would vanish.
        """
        if self._read_cache is not None:
            yield self._read_cache
            return
        cache = ReadCache()
        self._read_cache = cache
        try:
            yield cache
        finally:
            self._read_cache = None
            self.last_run_cache_hits = cache.hits
            self.last_run_provider_reads = cache.misses

    def _check_cancelled(self) -> None:
        if not self._cancel_requested_callback:
            return
        if self._cancel_requested_callback():
            from .sync_service import SyncCancelled
            raise SyncCancelled("Sync stopped by user")

    def _set_status(self, message: str) -> None:
        if self._status_callback:
            try:
                self._status_callback(message)
            except Exception:
                logger.debug("Cross-sync status callback failed", exc_info=True)

    @property
    def plans(self) -> dict:
        """The plan each route/category produced this run."""
        return dict(self._plans)

    @property
    def plan_verdicts(self) -> dict:
        """What the safety guard concluded about each plan."""
        return dict(self._plan_verdicts)

    @property
    def route_states(self) -> dict:
        """Per (route_id, category) observation of this run. Never serialized."""
        return dict(self._route_states)

    @property
    def managed_keys(self) -> dict:
        """Managed keys to persist after a run, keyed by pair then category."""
        return {
            pair_id: {category: list(keys) for category, keys in categories.items()}
            for pair_id, categories in self._managed_keys.items()
        }

    def validate_pair(self, pair) -> str:
        """Return an empty string when the pair can run, else why it cannot."""
        source = self._adapters.get(pair.source)
        target = self._adapters.get(pair.target)
        if source is None:
            return f"Source '{pair.source}' is not configured."
        if target is None:
            return f"Target '{pair.target}' is not configured."
        if not target.can_write():
            return target.write_blocked_reason() or f"{target.label} cannot be written to."
        two_way = getattr(pair, "is_two_way", lambda: False)()
        if two_way and not source.can_write():
            # Two-way writes both ends, so a read-only first service (MDBList, or
            # AniList without a token) cannot take part however it is ordered.
            return (
                source.write_blocked_reason()
                or f"{source.label} cannot be written to, so it cannot be one end of a two-way pair."
            )
        target_list = str(getattr(pair, "target_list", "") or "").strip()
        if target_list and getattr(target, "supports_target_lists", None) is False:
            return f"{target.label} does not support writable custom lists."
        if target_list and getattr(target, "target_list_categories", ()):
            unsupported_destinations = [
                category for category in pair.categories
                if category not in set(getattr(target, "target_list_categories", ()))
            ]
            if unsupported_destinations:
                return (
                    f"{target.label} custom lists cannot receive "
                    f"{', '.join(unsupported_destinations)}."
                )

        readable = set(source.readable_categories())
        writable = set(target.writable_categories())
        if two_way:
            # Both ends must handle the category in both directions.
            readable &= set(target.readable_categories())
            writable &= set(source.writable_categories())
        usable = [c for c in pair.categories if c in readable and c in writable]
        if not usable:
            unreadable = [c for c in pair.categories if c not in readable]
            unwritable = [c for c in pair.categories if c not in writable]
            parts = []
            if unreadable:
                parts.append(f"{source.label} cannot provide {', '.join(unreadable)}")
            if unwritable:
                parts.append(f"{target.label} cannot receive {', '.join(unwritable)}")
            return "; ".join(parts) or "No categories in common."
        return ""

    @staticmethod
    def _effective_target_list(pair, category: str) -> str:
        """Resolve provider-aware destinations without changing saved pairs.

        SIMKL Plan to Watch is a true Trakt watchlist.  Its other status lists
        are not a Trakt collection (and flattening them there causes the HTTP
        420 account-limit failure), so an existing broad SIMKL→Trakt pair is
        transparently routed to one managed personal list per SIMKL status.
        """
        requested = str(getattr(pair, "target_list", "") or "").strip()
        # A pair may combine a named-list category with account-level mutable
        # state. The named destination applies only where it makes sense;
        # history and resume always use the provider's native account endpoint.
        if category not in ("watchlist", "collection"):
            return ""
        if pair.source == "simkl" and pair.target == "trakt":
            if category in ("watchlist", "history"):
                return ""
            if category == "collection" and not requested:
                selected = []
                for key in getattr(pair, "source_lists", []) or []:
                    parts = str(key).split(":")
                    if len(parts) == 3 and parts[0] == "status" and parts[1] in {
                        "watching", "completed", "hold", "dropped",
                    } and parts[1] not in selected:
                        selected.append(parts[1])
                return "auto:simkl:" + ",".join(selected or ["completed"])
        return requested

    @staticmethod
    def _target_accepts(target, category: str, item: dict, target_list: str) -> bool:
        accepts = getattr(target, "accepts", None)
        if not callable(accepts):
            return True
        try:
            return bool(accepts(category, item, target_list))
        except Exception:
            # A provider that cannot answer must not block the sync.
            logger.warning(
                "%s: accepts() failed for %s; allowing the item",
                getattr(target, "label", "target"), category, exc_info=True,
            )
            return True

    @staticmethod
    def _comparison_key(item: dict, target_list: str) -> str:
        """Use status-aware identity only for automatic SIMKL status lists."""
        base = item_key(item)
        if str(target_list or "").startswith("auto:simkl:"):
            return f"{str(item.get('_syncmeta_source_status') or '')}:{base}"
        return base

    # ── execution ──────────────────────────────────────────────────────────

    def run_pair(self, pair) -> PairRunStats:
        with self._batch_cache():
            return self._run_pair(pair)

    def _run_pair(self, pair) -> PairRunStats:
        two_way = getattr(pair, "is_two_way", lambda: False)()
        stats = PairRunStats(
            pair_id=pair.pair_id,
            name=pair.display_name(),
            source=pair.source,
            target=pair.target,
            removal_mode=pair.removal_mode,
            mode=getattr(pair, "mode", MODE_ONE_WAY),
            dry_run=self._dry_run,
        )

        problem = self.validate_pair(pair)
        if problem:
            stats.errors.append(problem)
            logger.warning("Skipping pair %s: %s", pair.display_name(), problem)
            return stats

        source = self._adapters[pair.source]
        target = self._adapters[pair.target]
        readable = set(source.readable_categories())
        writable = set(target.writable_categories())
        if two_way:
            readable &= set(target.readable_categories())
            writable &= set(source.writable_categories())

        for category in pair.categories:
            self._check_cancelled()
            if category not in readable or category not in writable:
                # Reported once by validate_pair; skip silently per category so a
                # pair with one unsupported category still syncs the others.
                logger.info(
                    "Pair %s: skipping %s (unsupported by %s or %s)",
                    pair.display_name(), category, source.label, target.label,
                )
                continue
            runner = self._run_category_two_way if two_way else self._run_category
            stats.categories.append(runner(pair, source, target, category))

        return stats

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        """Turn provider HTTP failures into something a user can act on."""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 420:
            return (
                "account limit reached (HTTP 420) — the target account cannot hold "
                "more items; on Trakt this usually means the collection/list is over "
                "the free-tier size and needs Trakt VIP"
            )
        if status == 429:
            return "rate limited (HTTP 429) — the service asked us to slow down; try again later"
        return str(exc)

    def _run_category(self, pair, source, target, category: str) -> PairCategoryStats:
        result = PairCategoryStats(category=category)
        self._set_status(f"{pair.display_name()}: reading {category} from {source.label}")
        cache = self._read_cache
        source_lists = getattr(pair, "source_lists", None)

        def _read(adapter, lists):
            if cache is None:
                return adapter.fetch(category, lists) or []
            before = cache.hits
            items = cache.get_or_fetch(
                adapter.key, category, lists, lambda: adapter.fetch(category, lists),
            )
            if cache.hits > before:
                result.cached_reads += 1
            return items

        try:
            source_items = _read(source, source_lists)
        except Exception as exc:
            message = f"Could not read {category} from {source.label}: {self._describe_error(exc)}"
            result.errors.append(message)
            logger.warning("Pair %s: %s", pair.display_name(), message, exc_info=True)
            # A read that failed is not an empty list, and the baseline must
            # record that difference rather than the zero items it got back.
            self._observe_failure(
                pair, category, message,
                source_fetch=self._fetch_outcome(source, category, error=exc),
            )
            return result

        try:
            target_list = self._effective_target_list(pair, category)
            if cache is None:
                target_items = target.fetch_target(category, target_list) or []
            else:
                before = cache.hits
                target_items = cache.get_or_fetch(
                    target.key, category, [target_list] if target_list else None,
                    lambda: target.fetch_target(category, target_list),
                )
                if cache.hits > before:
                    result.cached_reads += 1
        except Exception as exc:
            # Without the target's current contents everything would look new and
            # be rewritten, so this is fatal for the category rather than ignored.
            message = f"Could not read {category} from {target.label}: {self._describe_error(exc)}"
            result.errors.append(message)
            logger.warning("Pair %s: %s", pair.display_name(), message, exc_info=True)
            self._observe_failure(
                pair, category, message,
                source_fetch=self._fetch_outcome(source, category, count=len(source_items)),
                destination_fetch=self._fetch_outcome(target, category, error=exc),
            )
            return result

        result.source_items = len(source_items)
        result.target_items = len(target_items)

        # Both sides are normalized to a common id namespace first. AniList
        # reports AniList ids and Trakt reports TMDB, so without this the same
        # show would key differently per service, nothing would ever match, and
        # the pair would re-add its entire source list on every run.
        is_history = category == CATEGORY_HISTORY

        source_by_key: dict[str, dict] = {}
        # History keeps every row, not one per episode: the extras are the
        # rewatches, and collapsing them here is what lost them.
        source_rows: list[tuple[str, dict]] = []
        for raw_item in source_items:
            item = enrich_identity(raw_item)
            if not has_portable_identity(item):
                # Nothing portable to match on; count it rather than guessing.
                result.unmapped += 1
                continue
            if not self._target_accepts(target, category, item, target_list):
                # The target will not store this. It has to leave the source set
                # too, not just be refused at the write: an item that stays here
                # looks present to the diff, so a stale copy already on the
                # target could never be recognised as stale and removed.
                result.skipped_unsupported += 1
                continue
            key = self._comparison_key(item, target_list)
            source_by_key.setdefault(key, item)
            if is_history:
                source_rows.append((key, item))

        target_by_key: dict[str, dict] = {}
        target_plays: dict[str, list] = {}
        keeps_plays = is_history and bool(getattr(target, "records_plays", False))
        for raw_item in target_items:
            item = enrich_identity(raw_item)
            key = self._comparison_key(item, target_list)
            target_by_key.setdefault(key, item)
            if keeps_plays:
                target_plays.setdefault(key, []).append(item.get("watched_at"))

        source_outcome = self._fetch_outcome(source, category, count=len(source_items))
        history_plan = None

        # Every category is now planned against the baseline, but each kind of
        # data gets its own planner: membership asks "is it on the list",
        # history asks "did this happen and have we already carried it", and
        # resume asks "is this further along". Sharing one would collapse the
        # distinctions each of them exists to keep.
        if is_history:
            history_plan = self._plan_history_category(
                pair, category, source_rows=[item for _key, item in source_rows],
                target_items=[enrich_identity(row) for row in target_items],
                source=source, target=target,
            )
            plan = history_plan.plan if history_plan is not None else None
            if plan is None:
                to_add = _history_adds(source_rows, target_by_key, target_plays)
                result.skipped_existing = max(0, len(source_rows) - len(to_add))
        elif category == CATEGORY_RESUME:
            plan = self._plan_progress_category(
                pair, category, source_by_key=source_by_key,
                target_by_key=target_by_key, source=source, target=target,
            )
            if plan is None:
                to_add = [
                    item for key, item in source_by_key.items()
                    if key not in target_by_key
                    or not self._resume_matches(item, target_by_key[key])
                ]
                result.skipped_existing = len(source_by_key) - len(to_add)
        else:
            to_add = [
                item for key, item in source_by_key.items()
                if key not in target_by_key
            ]
            result.skipped_existing = len(source_by_key) - len(to_add)
            plan = self._plan_category(
                pair, category,
                source_by_key=source_by_key, target_by_key=target_by_key,
                source=source, target=target,
                source_trustworthy=source_outcome.trustworthy_for_removals,
            )
        if plan is not None:
            # Updates ride with additions: every adapter's `add` is an upsert for
            # the categories that have them (a resume position, a progress count).
            to_add = [action.item for action in plan.additions + plan.updates]
            to_remove = [action.item for action in plan.removals]
            result.skipped_existing = len(plan.skipped)
            verdict = self._plan_verdicts.get((str(pair.pair_id), str(category)))
            if verdict is not None and verdict.blocked:
                self._note_blocked(
                    pair, category, result,
                    removals=verdict.removals, target_size=len(target_by_key),
                    percent=verdict.percent, where=target.label,
                    items=self._plan_blocked_items.get(
                        (str(pair.pair_id), str(category)), [],
                    ),
                    detail=verdict.explain(),
                )
        else:
            to_remove = self._items_to_remove(pair, category, source_by_key, target_by_key)
            blocked, percent = self._guard_blocks(len(to_remove), len(target_by_key))
            if blocked:
                self._note_blocked(
                    pair, category, result,
                    removals=len(to_remove), target_size=len(target_by_key),
                    percent=percent, where=target.label, items=to_remove,
                )
                to_remove = []

        self._set_status(
            f"{pair.display_name()}: {len(to_add)} to add, {len(to_remove)} to remove on {target.label}"
        )

        wrote_added: list[dict] = []
        wrote_removed: list[dict] = []

        if to_add:
            if self._dry_run:
                result.added = len(to_add)
            else:
                try:
                    write_items = [
                        {**item, "_syncmeta_source_provider": source.key}
                        for item in to_add
                    ] if target.key == "library" else to_add
                    totals = target.add(
                        category, write_items, target_list, **_add_kwargs(target, pair),
                    ) or {}
                    result.added = _total(totals, "added")
                    result.unmapped += _total(totals, "not_found")
                    wrote_added = to_add[:result.added]
                    result.changes.extend(self._change_rows(wrote_added, "added", category))
                except Exception as exc:
                    message = f"Could not write {category} to {target.label}: {self._describe_error(exc)}"
                    result.errors.append(message)
                    logger.warning("Pair %s: %s", pair.display_name(), message, exc_info=True)
                    # A partial write may have landed; the cached view of this
                    # target can no longer be trusted by later pairs.
                    if cache is not None:
                        cache.invalidate_provider(target.key)

        if to_remove:
            if self._dry_run:
                result.removed = len(to_remove)
            else:
                try:
                    totals = target.remove(category, to_remove, target_list) or {}
                    result.removed = _total(totals, "deleted")
                    wrote_removed = to_remove[:result.removed]
                    result.changes.extend(self._change_rows(wrote_removed, "removed", category))
                except Exception as exc:
                    message = f"Could not remove {category} from {target.label}: {self._describe_error(exc)}"
                    result.errors.append(message)
                    logger.warning("Pair %s: %s", pair.display_name(), message, exc_info=True)
                    if cache is not None:
                        cache.invalidate_provider(target.key)

        # Keep the batch cache honest: another pair writing to this same target
        # must see what this one just did, or it would re-add the same items.
        if cache is not None and not self._dry_run and (wrote_added or wrote_removed):
            cache.apply_write(
                target.key, category, [target_list] if target_list else None,
                added=wrote_added, removed=wrote_removed,
            )

        self._record_managed_keys(pair, category, source_by_key, to_remove, result)
        if is_history:
            self._commit_history_baseline(pair, category, result, history_plan, wrote_added)
        self._commit_baseline(
            pair, category, result,
            source_by_key=source_by_key, target_by_key=target_by_key,
            added=wrote_added, removed=wrote_removed, target_list=target_list,
        )
        self._observe_route(
            pair, category,
            source_by_key=source_by_key,
            target_by_key=target_by_key,
            added=wrote_added if not self._dry_run else [],
            removed=wrote_removed if not self._dry_run else [],
            result=result,
            source_fetch=self._fetch_outcome(source, category, count=len(source_items)),
            destination_fetch=self._fetch_outcome(target, category, count=len(target_items)),
            target_list=target_list,
        )
        return result

    def _run_category_two_way(self, pair, first, second, category: str) -> PairCategoryStats:
        """Bring both services to the union of the two, in one pass.

        Deliberately not two one-way runs back to back. That ordering decides the
        outcome: whichever direction runs first re-adds an item the user deleted
        on the other side, so a deletion either propagates or is resurrected
        depending on which service happens to be named first.

        Instead the pair's managed-key set is read as *the state both sides last
        agreed on*, which is what makes a one-sided item interpretable:

            on one side, previously synced  -> deleted on the other -> remove it
            on one side, never synced       -> genuinely new        -> add it

        With `additive` the removal branch is skipped entirely, so a deletion is
        simply re-added from the other side — the usual additive trade-off.
        """
        result = PairCategoryStats(category=category)
        self._set_status(f"{pair.display_name()}: reading {category} from both services")
        cache = self._read_cache
        source_lists = getattr(pair, "source_lists", None)
        target_list = self._effective_target_list(pair, category)

        def _read(adapter, lists, *, declared_target: bool = False):
            loader = (
                (lambda: adapter.fetch_target(category, target_list))
                if declared_target else
                (lambda: adapter.fetch(category, lists))
            )
            if cache is None:
                return loader() or []
            before = cache.hits
            items = cache.get_or_fetch(
                adapter.key, category, lists, loader,
            )
            if cache.hits > before:
                result.cached_reads += 1
            return items

        sides = {}
        second_lists = [target_list] if target_list else None
        for label, adapter, lists, declared_target in (
            ("first", first, source_lists, False),
            ("second", second, second_lists, True),
        ):
            try:
                sides[label] = _read(adapter, lists, declared_target=declared_target)
            except Exception as exc:
                # Either side failing is fatal here: without both, every item on
                # the side that did load looks one-sided and would be acted on.
                message = f"Could not read {category} from {adapter.label}: {self._describe_error(exc)}"
                result.errors.append(message)
                logger.warning("Pair %s: %s", pair.display_name(), message, exc_info=True)
                return result

        is_history = category == CATEGORY_HISTORY

        def _index(raw_items, count_unmapped):
            """Index by identity, and for history also keep every play row."""
            out: dict[str, dict] = {}
            rows: list[tuple[str, dict]] = []
            plays: dict[str, list] = {}
            for raw in raw_items:
                item = enrich_identity(raw)
                if count_unmapped and not has_portable_identity(item):
                    result.unmapped += 1
                    continue
                key = item_key(item)
                out.setdefault(key, item)
                if is_history:
                    rows.append((key, item))
                    plays.setdefault(key, []).append(item.get("watched_at"))
            return out, rows, plays

        first_by_key, first_rows, first_plays = _index(sides["first"], True)
        second_by_key, second_rows, second_plays = _index(sides["second"], True)
        result.source_items = len(first_by_key)
        result.target_items = len(second_by_key)

        # Membership reconciles from the baseline: it can tell which side moved,
        # which set arithmetic on the managed keys cannot. History and resume
        # keep the path below until their own two-way handling lands.
        if category in _PLANNED_CATEGORIES:
            planned = self._run_two_way_planned(
                pair, first, second, category, result,
                first_by_key=first_by_key, second_by_key=second_by_key,
            )
            if planned is not None:
                return planned

        first_keys, second_keys = set(first_by_key), set(second_by_key)
        known = set(self._managed_keys.get(pair.pair_id, {}).get(category, []))
        only_first = first_keys - second_keys
        only_second = second_keys - first_keys

        if pair.removal_mode == REMOVAL_MANAGED:
            drop_from_first = only_first & known
            drop_from_second = only_second & known
        elif pair.removal_mode == REMOVAL_ADDITIVE:
            drop_from_first = drop_from_second = set()
        else:
            # mirror is downgraded on load; anything else refuses to delete.
            logger.warning(
                "Pair %s has removal mode %r, which two-way does not support; not removing anything",
                pair.display_name(), pair.removal_mode,
            )
            drop_from_first = drop_from_second = set()

        add_to_second = [first_by_key[k] for k in only_first - drop_from_first]
        add_to_first = [second_by_key[k] for k in only_second - drop_from_second]
        if is_history:
            # Same reasoning as the one-way path: a rewatch is a second row
            # under one episode key, so set arithmetic on identity drops it.
            # Only a side that keeps a per-play record can receive the extras;
            # a watched-state service would report the single row back and the
            # pair would re-send the rewatch on every run.
            add_to_second = _history_adds(
                [row for row in first_rows if row[0] not in drop_from_first],
                second_by_key,
                second_plays if getattr(second, "records_plays", False) else {},
            )
            add_to_first = _history_adds(
                [row for row in second_rows if row[0] not in drop_from_second],
                first_by_key,
                first_plays if getattr(first, "records_plays", False) else {},
            )
        differing_shared: set[str] = set()
        if category == CATEGORY_RESUME:
            for key in first_keys & second_keys:
                first_item = first_by_key[key]
                second_item = second_by_key[key]
                if self._resume_matches(first_item, second_item):
                    continue
                differing_shared.add(key)
                # Resume has no portable cross-provider modification timestamp.
                # The furthest playback position is the least destructive
                # deterministic conflict rule: a sync must never rewind either
                # service because its route happened to run second.
                if self._resume_score(first_item) >= self._resume_score(second_item):
                    add_to_second.append(first_item)
                else:
                    add_to_first.append(second_item)
        # A blocked side keeps its items: they are neither deleted here nor
        # re-added to the other service, so the run is a no-op for them and the
        # next run can act on the same state once the user has decided.
        blocked_first: set = set()
        blocked_second: set = set()
        for keys, size, label, bucket in (
            (drop_from_first, len(first_keys), first.label, "first"),
            (drop_from_second, len(second_keys), second.label, "second"),
        ):
            blocked, percent = self._guard_blocks(len(keys), size)
            if not blocked:
                continue
            self._note_blocked(
                pair, category, result,
                removals=len(keys), target_size=size, percent=percent, where=label,
                items=[(first_by_key if bucket == "first" else second_by_key)[key] for key in keys],
            )
            if bucket == "first":
                blocked_first = set(keys)
            else:
                blocked_second = set(keys)
        remove_from_first = [first_by_key[k] for k in drop_from_first - blocked_first]
        remove_from_second = [second_by_key[k] for k in drop_from_second - blocked_second]
        result.skipped_existing = len((first_keys & second_keys) - differing_shared)

        self._set_status(
            f"{pair.display_name()}: {len(add_to_second)}→{second.label}, "
            f"{len(add_to_first)}→{first.label}"
        )

        # (adapter, items, is_reverse, verb)
        plan = [
            (second, add_to_second, False, "add"),
            (first, add_to_first, True, "add"),
            (second, remove_from_second, False, "remove"),
            (first, remove_from_first, True, "remove"),
        ]
        for adapter, items, reverse, verb in plan:
            if not items:
                continue
            if self._dry_run:
                count = len(items)
            else:
                try:
                    # target_list belongs to the declared target only; writing
                    # back to the first service uses its own default list.
                    dest = "" if reverse else target_list
                    source_adapter = second if reverse else first
                    write_items = [
                        {**item, "_syncmeta_source_provider": source_adapter.key}
                        for item in items
                    ] if verb == "add" and adapter.key == "library" else items
                    totals = (
                        adapter.add(category, write_items, dest, **_add_kwargs(adapter, pair))
                        if verb == "add"
                        else adapter.remove(category, items, dest)
                    ) or {}
                    count = _total(totals, "added" if verb == "add" else "deleted")
                    if verb == "add":
                        result.unmapped += _total(totals, "not_found")
                    if cache is not None:
                        cache.apply_write(
                            adapter.key, category,
                            None if reverse else ([target_list] if target_list else None),
                            added=items if verb == "add" else [],
                            removed=items if verb == "remove" else [],
                        )
                except Exception as exc:
                    action = "write" if verb == "add" else "remove"
                    message = (f"Could not {action} {category} "
                               f"{'to' if verb == 'add' else 'from'} {adapter.label}: "
                               f"{self._describe_error(exc)}")
                    result.errors.append(message)
                    logger.warning("Pair %s: %s", pair.display_name(), message, exc_info=True)
                    if cache is not None:
                        cache.invalidate_provider(adapter.key)
                    continue
            if verb == "add":
                if reverse:
                    result.added_back += count
                result.added += count
            else:
                if reverse:
                    result.removed_back += count
                result.removed += count

        # The new agreed state: everything either side holds, minus what this run
        # deleted. Recorded even when a write failed, since a partial result is
        # still closer to the truth than the previous run's snapshot.
        if not self._dry_run:
            agreed = ((first_keys | second_keys)
                      - (drop_from_first - blocked_first)
                      - (drop_from_second - blocked_second))
            ordered = sorted(agreed)
            self._managed_keys.setdefault(pair.pair_id, {})[category] = ordered
            result.managed_keys = ordered
        return result

    @staticmethod
    def _resume_score(item: dict) -> tuple[float, int]:
        """Comparable progress score, preferring percentage then position."""
        try:
            position = max(0, int(item.get("position_ms") or 0))
        except (TypeError, ValueError):
            position = 0
        try:
            runtime = max(0, int(item.get("runtime_ms") or 0))
        except (TypeError, ValueError):
            runtime = 0
        try:
            progress = float(item.get("progress")) / 100.0
        except (TypeError, ValueError):
            progress = 0.0
        ratio = position / runtime if runtime else progress
        return (ratio, position)

    @classmethod
    def _resume_matches(cls, first: dict, second: dict) -> bool:
        """Treat sub-second rounding differences as the same resume point."""
        def _number(item, field):
            try:
                return max(0, int(item.get(field) or 0))
            except (TypeError, ValueError):
                return 0

        first_position = _number(first, "position_ms")
        second_position = _number(second, "position_ms")
        first_runtime = _number(first, "runtime_ms")
        second_runtime = _number(second, "runtime_ms")
        return (
            abs(first_position - second_position) <= 1000
            and (
                not first_runtime or not second_runtime
                or abs(first_runtime - second_runtime) <= 1000
            )
        )

    def _guard_blocks(self, removals: int, target_size: int) -> tuple[bool, int]:
        """Would deleting `removals` of `target_size` items trip the guard?

        Returns (blocked, percent). A small list is exempt: removing 2 of 3
        items is 67% and entirely ordinary, so the guard only speaks for lists
        big enough that a mass deletion is surprising.
        """
        if not self._guard_large_removals or removals <= 0 or target_size <= 0:
            return False, 0
        percent = round(removals * 100 / target_size)
        if target_size < _GUARD_MIN_TARGET_SIZE or removals < _GUARD_MIN_REMOVALS:
            return False, percent
        return percent > self._guard_removal_percent, percent

    @staticmethod
    def _change_rows(items: list[dict], change_type: str, category: str) -> list[dict]:
        rows = []
        for item in items or []:
            ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
            rows.append({
                "change_type": change_type, "category": category,
                "title": str(item.get("title") or "Unknown"),
                "media_type": str(item.get("media_type") or ""),
                "ids": {key: value for key, value in {
                    "tmdb": item.get("tmdb_id") or ids.get("tmdb"),
                    "imdb": item.get("imdb_id") or ids.get("imdb"),
                    "anilist": item.get("anilist_id") or ids.get("anilist"),
                    "mal": item.get("mal_id") or ids.get("mal"),
                }.items() if value},
                "previous": "present" if change_type == "removed" else "absent",
                "new": "absent" if change_type == "removed" else "present",
            })
        return rows

    def _note_blocked(
        self, pair, category: str, result: PairCategoryStats,
        *, removals: int, target_size: int, percent: int, where: str,
        items: list[dict] | None = None, detail: str = "",
    ) -> None:
        review_items = []
        for item in items or []:
            ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
            review_items.append({
                "title": str(item.get("title") or "Unknown"),
                "year": item.get("year"),
                "media_type": str(item.get("media_type") or ""),
                "tmdb_id": item.get("tmdb_id") or ids.get("tmdb"),
                "imdb_id": item.get("imdb_id") or ids.get("imdb"),
            })
        result.blocked_removals.append({
            "category": category,
            "removals": removals,
            "target_size": target_size,
            "percent": percent,
            "target": where,
            "items": review_items,
            # The exact threshold or condition that stopped it, so the Issues
            # page can say why rather than only that.
            "detail": detail,
        })
        logger.warning(
            "Pair %s: refusing to remove %d of %d %s items (%s%%) from %s — %s; "
            "nothing was deleted",
            pair.display_name(), removals, target_size, category, percent, where,
            detail or f"over the {self._guard_removal_percent}% safety threshold",
        )
        self._set_status(
            f"{pair.display_name()}: paused an unusually large removal "
            f"({removals} of {target_size} {category} items on {where})"
        )

    def _items_to_remove(
        self, pair, category: str, source_by_key: dict, target_by_key: dict,
    ) -> list[dict]:
        """Decide what may be deleted from the target, honouring the pair's mode."""
        if pair.removal_mode == REMOVAL_ADDITIVE:
            return []

        stale_keys = [key for key in target_by_key if key not in source_by_key]

        if pair.removal_mode == REMOVAL_MANAGED:
            # Only touch what this pair put there, so anything the user added on
            # the target by hand survives. An empty managed set means this pair
            # has never written before, so there is nothing it may claim.
            managed = set(self._managed_keys.get(pair.pair_id, {}).get(category, []))
            if not managed:
                return []
            stale_keys = [key for key in stale_keys if key in managed]
        elif pair.removal_mode != REMOVAL_MIRROR:
            # Unknown mode: refuse to delete rather than guess.
            logger.warning(
                "Pair %s has unrecognised removal mode %r; not removing anything",
                pair.display_name(), pair.removal_mode,
            )
            return []

        return [target_by_key[key] for key in stale_keys]

    def _run_two_way_planned(
        self, pair, first, second, category: str, result,
        *, first_by_key, second_by_key,
    ):
        """Reconcile a two-way membership category from the baseline.

        Returns the finished stats, or None to fall back to the legacy pass if a
        plan could not be built — a planner failure must not take a run with it.
        """
        baseline = None
        if self._state_store is not None:
            try:
                baseline = self._state_store.baseline(pair.pair_id, category)
            except Exception:
                logger.debug("Could not read baseline for two-way plan", exc_info=True)
        if baseline is None:
            baseline = RouteBaseline(route_id=str(pair.pair_id), category=str(category))
        try:
            two_way = plan_two_way(
                route_id=str(pair.pair_id), category=str(category),
                first_by_key=first_by_key, second_by_key=second_by_key,
                baseline=baseline, policy=normalize_policy(pair.removal_mode),
                first_provider=first.key, second_provider=second.key,
                first_trustworthy=first.last_read_complete(),
                second_trustworthy=second.last_read_complete(),
            )
        except Exception:
            logger.warning("Could not build two-way sync plan", exc_info=True)
            return None

        self._plans[(str(pair.pair_id), str(category))] = two_way.forward
        result.skipped_existing = len(set(first_by_key) & set(second_by_key))

        target_list = self._effective_target_list(pair, category)
        performed: list = []
        for plan, writer_target, other, destination_list in (
            (two_way.forward, second, first, target_list),
            (two_way.backward, first, second, ""),
        ):
            verdict = evaluate_safety(
                plan,
                policy=SafetyPolicy(
                    enabled=self._guard_large_removals,
                    max_removal_percent=self._guard_removal_percent,
                    allow_destructive_override=self._allow_destructive_override,
                ),
                destination_size=len(second_by_key if writer_target is second else first_by_key),
                source_size=len(first_by_key if writer_target is second else second_by_key),
                source_trustworthy=other.last_read_complete(),
                baseline_established=baseline.allows_removals,
            )
            if verdict.blocked and plan.removals:
                self._note_blocked(
                    pair, category, result,
                    removals=verdict.removals,
                    target_size=verdict.destination_size,
                    percent=verdict.percent, where=writer_target.label,
                    items=[a.item for a in plan.removals], detail=verdict.explain(),
                )
            plan = enforce_safety(plan, verdict)
            performed.append(plan)
            self._apply_two_way_side(
                pair, category, plan, writer_target, result, destination_list,
            )

        result.conflicts_detected = len(two_way.conflicts)
        # Ownership is the union both sides now hold, minus what actually went.
        # Taken from the *enforced* plans, so a removal the guard paused stays in
        # the agreed set — the next run then sees the same situation rather than
        # treating those items as never synced.
        gone = {a.key for plan in performed for a in plan.removals}
        agreed = sorted((set(first_by_key) | set(second_by_key)) - gone)
        if not self._dry_run:
            self._managed_keys.setdefault(pair.pair_id, {})[category] = agreed
        result.managed_keys = agreed

        if self._state_store is not None and not self._dry_run and not result.errors:
            try:
                applied_forward = {a.key for a in two_way.forward.additions}
                applied_backward = {a.key for a in two_way.backward.additions}
                gone_forward = {a.key for a in two_way.forward.removals}
                gone_backward = {a.key for a in two_way.backward.removals}
                items = {}
                for key in set(first_by_key) | set(second_by_key):
                    on_first = (
                        key in first_by_key or key in applied_backward
                    ) and key not in gone_backward
                    on_second = (
                        key in second_by_key or key in applied_forward
                    ) and key not in gone_forward
                    items[key] = ItemState(
                        source=STATE_PRESENT if on_first else STATE_ABSENT,
                        destination=STATE_PRESENT if on_second else STATE_ABSENT,
                        synced=STATE_PRESENT if (on_first and on_second) else STATE_ABSENT,
                        managed=True,
                    )
                self._state_store.commit(pair.pair_id, category, items=items)
            except Exception:
                logger.warning("Could not record two-way baseline", exc_info=True)
        return result

    def _apply_two_way_side(
        self, pair, category, plan, target, result, target_list: str = "",
    ) -> None:
        """Write one direction of a reconciled two-way plan."""
        forward = target.key == pair.target
        if plan.removals and not self._dry_run:
            try:
                totals = target.remove(
                    category, [a.item for a in plan.removals], target_list,
                ) or {}
                count = _total(totals, "deleted")
            except Exception as exc:
                message = f"Could not remove {category} from {target.label}: {self._describe_error(exc)}"
                result.errors.append(message)
                logger.warning("Pair %s: %s", pair.display_name(), message, exc_info=True)
                count = 0
            if forward:
                result.removed += count
            else:
                result.removed_back += count
                result.removed += count
        elif plan.removals:
            result.removed += len(plan.removals)
            if not forward:
                result.removed_back += len(plan.removals)

        if plan.additions and not self._dry_run:
            try:
                totals = target.add(
                    category, [a.item for a in plan.additions], target_list,
                    **_add_kwargs(target, pair),
                ) or {}
                count = _total(totals, "added")
                result.unmapped += _total(totals, "not_found")
            except Exception as exc:
                message = f"Could not write {category} to {target.label}: {self._describe_error(exc)}"
                result.errors.append(message)
                logger.warning("Pair %s: %s", pair.display_name(), message, exc_info=True)
                count = 0
            if forward:
                result.added += count
            else:
                result.added_back += count
                result.added += count
        elif plan.additions:
            result.added += len(plan.additions)
            if not forward:
                result.added_back += len(plan.additions)

    def _baseline_for(self, pair, category: str):
        if self._state_store is not None:
            try:
                return self._state_store.baseline(pair.pair_id, category)
            except Exception:
                logger.debug("Could not read baseline", exc_info=True)
        return RouteBaseline(route_id=str(pair.pair_id), category=str(category))

    def _plan_history_category(self, pair, category, *, source_rows, target_items, source, target):
        """Plan watch history: a union, deduped in three layers.

        A failure here falls back to the legacy diff rather than taking the run
        with it — that path is already play-aware and additive-safe.
        """
        try:
            planned = plan_history(
                route_id=str(pair.pair_id),
                source_rows=source_rows, destination_rows=target_items,
                baseline=self._baseline_for(pair, category),
                target_records_plays=bool(getattr(target, "records_plays", False)),
                policy=normalize_policy(pair.removal_mode),
                source_provider=source.key, destination_provider=target.key,
                category=str(category),
            )
            self._plans[(str(pair.pair_id), str(category))] = planned.plan
            return planned
        except Exception:
            logger.warning("Could not build history plan", exc_info=True)
            return None

    def _plan_progress_category(self, pair, category, *, source_by_key, target_by_key, source, target):
        """Plan resume points: never rewind, never push a finished title on."""
        try:
            planned = plan_progress(
                route_id=str(pair.pair_id),
                source_by_key=source_by_key, destination_by_key=target_by_key,
                baseline=self._baseline_for(pair, category),
                policy=normalize_policy(pair.removal_mode),
                source_provider=source.key, destination_provider=target.key,
                category=str(category),
            )
        except Exception:
            logger.warning("Could not build progress plan", exc_info=True)
            return None
        self._plans[(str(pair.pair_id), str(category))] = planned.plan
        return planned.plan

    def _commit_history_baseline(self, pair, category, result, history_plan, wrote_added) -> None:
        """Record the plays this run actually carried across.

        Only confirmed writes enter the record. A play written down as carried
        when it was not would never be retried; one left out would be sent
        again, which for history means a duplicate.
        """
        if self._state_store is None or self._dry_run or history_plan is None:
            return
        try:
            if result.errors:
                self._state_store.record_failure(
                    pair.pair_id, category, "; ".join(result.errors)[:500],
                )
                return
            written = {id(item) for item in wrote_added or []}
            baseline = self._baseline_for(pair, category)
            items = dict(baseline.items)
            confirmed_keys = {
                action.key for action in history_plan.plan.additions
                if id(action.item) in written
            }
            for key, projected in history_plan.projected.items():
                if key in confirmed_keys or key not in {
                    a.key for a in history_plan.plan.additions
                }:
                    items[key] = projected
                else:
                    # Planned but not confirmed: keep what was already agreed so
                    # the next run tries again rather than believing it done.
                    items.setdefault(key, ItemState())
            self._state_store.commit(pair.pair_id, category, items=items)
        except Exception:
            logger.warning("Could not record history baseline", exc_info=True)

    def _commit_baseline(
        self, pair, category: str, result, *,
        source_by_key, target_by_key, added, removed, target_list,
    ) -> None:
        """Record what this run agreed on, so the next one can compare with it.

        The engine keeps its own state rather than leaving it to whoever called
        it: a second run in the same process has to see the first run's
        agreement, or two-way reconciliation and the removal protections only
        work when a particular caller remembers to persist for them.

        A dry run agrees to nothing. Neither does a run that errored — its
        writes are in an unknown state, so the previous agreement stands and the
        next run works the problem again.
        """
        if self._state_store is None or self._dry_run:
            return
        if category not in _PLANNED_CATEGORIES:
            return
        added_keys = {self._comparison_key(i, target_list) for i in added or []}
        removed_keys = {self._comparison_key(i, target_list) for i in removed or []}
        managed = set(self._managed_keys.get(pair.pair_id, {}).get(category, []))
        try:
            if result.errors:
                self._state_store.record_failure(
                    pair.pair_id, category, "; ".join(result.errors)[:500],
                )
                return
            items = {}
            for key in set(source_by_key) | set(target_by_key) | added_keys:
                on_source = key in source_by_key
                on_destination = (
                    key in target_by_key or key in added_keys
                ) and key not in removed_keys
                items[key] = ItemState(
                    source=STATE_PRESENT if on_source else STATE_ABSENT,
                    destination=STATE_PRESENT if on_destination else STATE_ABSENT,
                    synced=STATE_PRESENT if (on_source and on_destination) else STATE_ABSENT,
                    managed=key in managed,
                )
            self._state_store.commit(pair.pair_id, category, items=items)
        except Exception:
            logger.warning(
                "Could not record baseline for %s/%s", pair.display_name(), category,
                exc_info=True,
            )

    def _index_ownership(self, pairs) -> None:
        """Record which routes still require each item on each destination.

        An item may only be deleted when *no* active route's source still lists
        it. Without this, two routes feeding one destination fight: the first
        removes what left its source, and the second re-adds it on the next run
        because its own source still has it.
        """
        self._ownership = OwnershipIndex()
        for pair in pairs:
            source = self._adapters.get(pair.source)
            if source is None:
                continue
            scope = destination_scope(pair)
            for category in getattr(pair, "categories", ()) or ():
                if category not in _PLANNED_CATEGORIES:
                    continue
                try:
                    items = self._read_for_ownership(source, category, pair)
                except Exception:
                    # Its own run will report this properly; here a failed read
                    # simply contributes no claim, which is the safe direction.
                    logger.debug(
                        "Ownership pre-pass could not read %s/%s",
                        pair.source, category, exc_info=True,
                    )
                    continue
                keys = set()
                for raw in items:
                    item = enrich_identity(raw)
                    if has_portable_identity(item):
                        keys.add(item_key(item))
                self._ownership.record(
                    pair.pair_id, scope, category,
                    required_keys=keys,
                    managed_keys=self._managed_keys.get(pair.pair_id, {}).get(category, []),
                )

    def _read_for_ownership(self, source, category: str, pair) -> list:
        lists = getattr(pair, "source_lists", None)
        if self._read_cache is None:
            return source.fetch(category, lists) or []
        return self._read_cache.get_or_fetch(
            source.key, category, lists, lambda: source.fetch(category, lists),
        )

    def _plan_category(
        self, pair, category: str, *, source_by_key, target_by_key,
        source, target, source_trustworthy=True,
    ):
        """Plan the category from the baseline, then let the guard rule on it.

        Returns the plan the run may actually perform, or None for a category
        this planner does not cover yet.

        Only *membership* categories are planned here — "is this item on this
        list". History is an append-only event log with its own dedupe rules,
        and resume is a progress value where an item present on both sides may
        still need writing because the position moved. Handing either to a
        membership planner would silently turn "changed" into "already in sync",
        so both keep the legacy path until their own planners land.
        """
        if category not in _PLANNED_CATEGORIES:
            return None
        baseline = None
        if self._state_store is not None:
            try:
                baseline = self._state_store.baseline(pair.pair_id, category)
            except Exception:
                logger.debug("Could not read baseline for plan", exc_info=True)
        if baseline is None:
            baseline = RouteBaseline(route_id=str(pair.pair_id), category=str(category))
        try:
            plan = plan_membership(
                route_id=str(pair.pair_id), category=str(category),
                source_by_key=source_by_key, destination_by_key=target_by_key,
                baseline=baseline, policy=normalize_policy(pair.removal_mode),
                source_provider=source.key, destination_provider=target.key,
                source_trustworthy=source_trustworthy,
            )
        except Exception:
            # A planner failure must not take the run with it; the caller falls
            # back to the legacy diff, which is additive-safe.
            logger.warning("Could not build sync plan", exc_info=True)
            return None

        verdict = evaluate_safety(
            plan,
            policy=SafetyPolicy(
                enabled=self._guard_large_removals,
                max_removal_percent=self._guard_removal_percent,
                allow_destructive_override=self._allow_destructive_override,
            ),
            destination_size=len(target_by_key),
            source_size=len(source_by_key),
            source_trustworthy=source_trustworthy,
            baseline_established=baseline.allows_removals,
        )
        # An item another active route still requires must not go, whatever this
        # route's own view of it is — otherwise the two fight over it forever.
        contested = self._ownership.blocked_removals(
            pair.pair_id, destination_scope(pair), category,
            [action.key for action in plan.removals],
        )
        if contested:
            kept = tuple(a for a in plan.removals if a.key in contested)
            plan = replace(
                plan,
                removals=tuple(a for a in plan.removals if a.key not in contested),
                skipped=plan.skipped + tuple(
                    replace(
                        a, kind="skip", destructive=False,
                        reason="Another sync route still requires this item",
                    )
                    for a in kept
                ),
            )
            logger.info(
                "Pair %s: keeping %d %s item(s) another route still requires",
                pair.display_name(), len(contested), category,
            )

        key = (str(pair.pair_id), str(category))
        if verdict.blocked:
            # Kept before enforcement demotes them: the Issues page lists what
            # *would* have been deleted, and a demoted action no longer says so.
            self._plan_blocked_items[key] = [action.item for action in plan.removals]
        plan = enforce_safety(plan, verdict)
        self._plans[key] = plan
        self._plan_verdicts[key] = verdict
        return plan

    # ── baseline observation ───────────────────────────────────────────────
    #
    # These record what the run *saw*, for `sync.state_store`. They change no
    # decision in this class: the planner that consumes them lands separately.
    # Recording them from the first run means that planner has real baselines to
    # work with rather than starting blind.

    @staticmethod
    def _fetch_outcome(adapter, category: str, *, count: int = 0, error=None) -> FetchOutcome:
        """Classify one provider read.

        `PARTIAL` is not produced yet: no client currently reports that it read
        only some of its pages. When they learn to, this is the one place that
        has to change — everything downstream already refuses to conclude a
        removal from a fetch that is not `trustworthy_for_removals`.
        """
        if error is not None:
            return FetchOutcome(
                provider=str(getattr(adapter, "key", "") or ""),
                category=str(category),
                status=CrossSyncService._error_status(error),
                error=str(error)[:300],
            )
        complete = True
        try:
            complete = bool(adapter.last_read_complete())
        except Exception:
            logger.debug("Could not read fetch completeness", exc_info=True)
        return FetchOutcome(
            provider=str(getattr(adapter, "key", "") or ""),
            category=str(category),
            # A 200 carrying real items is still not trustworthy for removals if
            # a page never arrived, so completeness rides separately from status.
            status=(
                FetchStatus.PARTIAL if not complete
                else FetchStatus.SUCCESS_WITH_ITEMS if count
                else FetchStatus.SUCCESS_EMPTY
            ),
            item_count=int(count),
            complete=complete,
        )

    @staticmethod
    def _error_status(error) -> FetchStatus:
        text = f"{type(error).__name__}: {error}".lower()
        if "timeout" in text or "timed out" in text:
            return FetchStatus.TIMEOUT
        if "429" in text or "rate limit" in text:
            return FetchStatus.RATE_LIMITED
        if "401" in text or "403" in text or "unauthor" in text or "invalid_grant" in text:
            return FetchStatus.UNAUTHORIZED
        return FetchStatus.FAILED

    def _observe_failure(self, pair, category: str, error: str, **fetches) -> None:
        self._route_states[(str(pair.pair_id), str(category))] = RouteObservation(
            route_id=str(pair.pair_id), category=str(category),
            complete=False, error=str(error)[:500], **fetches,
        )

    def _observe_route(
        self, pair, category: str, *, source_by_key, target_by_key,
        added, removed, result, source_fetch, destination_fetch, target_list,
    ) -> None:
        """Build the per-item agreement this run establishes.

        The destination side is the state *after* the writes this run made, not
        the state it was read in — otherwise the next run would see everything
        this one just wrote as missing and write it all again.
        """
        added_keys = {self._comparison_key(item, target_list) for item in added or []}
        removed_keys = {self._comparison_key(item, target_list) for item in removed or []}
        managed = set(self._managed_keys.get(pair.pair_id, {}).get(category, []))

        items: dict[str, ItemState] = {}
        for key in set(source_by_key) | set(target_by_key) | added_keys:
            on_source = key in source_by_key
            on_destination = (key in target_by_key or key in added_keys) and key not in removed_keys
            state = ItemState(
                source=STATE_PRESENT if on_source else STATE_ABSENT,
                destination=STATE_PRESENT if on_destination else STATE_ABSENT,
                synced=STATE_PRESENT if (on_source and on_destination) else STATE_ABSENT,
                managed=key in managed,
            )
            if key in added_keys:
                state.action = "added"
            elif key in removed_keys:
                state.action = "removed"
            items[key] = state

        self._route_states[(str(pair.pair_id), str(category))] = RouteObservation(
            route_id=str(pair.pair_id), category=str(category), items=items,
            source_fetch=source_fetch, destination_fetch=destination_fetch,
            # A write that raised leaves the destination in a state this run
            # cannot describe, so the agreement must not be advanced from it.
            complete=not result.errors,
            error="; ".join(result.errors)[:500],
        )

    def _record_managed_keys(
        self, pair, category: str, source_by_key: dict, removed: list[dict], result: PairCategoryStats,
    ) -> None:
        """Track which keys this pair is responsible for on the target."""
        if self._dry_run:
            return
        pair_keys = self._managed_keys.setdefault(pair.pair_id, {})
        existing = set(pair_keys.get(category, []))
        existing.update(source_by_key.keys())
        for item in removed:
            existing.discard(item_key(item))
        ordered = sorted(existing)
        pair_keys[category] = ordered
        result.managed_keys = ordered

    def run_pairs(self, pairs) -> list[PairRunStats]:
        out: list[PairRunStats] = []
        with self._batch_cache() as cache:
            active = [pair for pair in pairs if getattr(pair, "enabled", True)]
            # Every route's source is read before any route writes, so a removal
            # can ask whether another route still needs the item. The batch cache
            # makes this nearly free: the same reads serve the run itself.
            self._index_ownership(active)
            for pair in pairs:
                if not getattr(pair, "enabled", True):
                    logger.info("Pair %s is disabled; skipping", pair.display_name())
                    continue
                self._check_cancelled()
                out.append(self._run_pair(pair))
            if cache.hits:
                logger.info(
                    "Cross-sync: %d provider read(s) served from the batch cache, %d fetched",
                    cache.hits, cache.misses,
                )
        return out
