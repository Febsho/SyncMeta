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
from dataclasses import dataclass, field

from .providers import (
    CATEGORY_HISTORY,
    CATEGORY_RESUME,
    MODE_ONE_WAY,
    REMOVAL_ADDITIVE,
    REMOVAL_MANAGED,
    REMOVAL_MIRROR,
    ALL_VISIBILITIES,
    VISIBILITY_PRIVATE,
    enrich_identity,
    has_portable_identity,
    item_key,
    normalize_watched_at,
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

    So a play is matched on identity *and* its timestamp, but only where the
    target can hold more than one: a service that reports watched *state* hands
    back a single row however many plays it was sent, so writing the extra ones
    would leave them looking missing on every subsequent run and the pair would
    re-send them forever. ``records_plays`` on the adapter decides that, and
    ``target_plays`` is empty when it is false.

    An episode the target does not have at all is always added, whichever kind
    of target it is — that is the ordinary first-play case.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for key, item in source_rows:
        stamp = normalize_watched_at(item.get("watched_at"))
        if key in target_by_key:
            known = target_plays.get(key)
            if not known:
                # Already watched there, and the target keeps no per-play
                # record to add a rewatch to.
                continue
            if not stamp or stamp in known:
                continue
        if (key, stamp) in seen:
            continue
        seen.add((key, stamp))
        out.append(item)
    return out


#: A list has to be big enough, and lose enough, for a mass deletion to be
#: surprising. Below these the guard stays silent — removing 2 of 3 items is a
#: perfectly ordinary edit and pausing it would be noise.
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
    ):
        self._adapters = dict(adapters or {})
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
        # {pair_id: {category: [key, ...]}} — keys this pair has written before.
        self._managed_keys = {
            str(pair_id): {
                str(category): list(keys or [])
                for category, keys in (categories or {}).items()
            }
            for pair_id, categories in (managed_keys or {}).items()
        }
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
        target_plays: dict[str, set[str]] = {}
        keeps_plays = is_history and bool(getattr(target, "records_plays", False))
        for raw_item in target_items:
            item = enrich_identity(raw_item)
            key = self._comparison_key(item, target_list)
            target_by_key.setdefault(key, item)
            if keeps_plays:
                stamp = normalize_watched_at(item.get("watched_at"))
                if stamp:
                    target_plays.setdefault(key, set()).add(stamp)

        if is_history:
            to_add = _history_adds(source_rows, target_by_key, target_plays)
            result.skipped_existing = max(0, len(source_rows) - len(to_add))
        else:
            to_add = [
                item for key, item in source_by_key.items()
                if key not in target_by_key
                or (category == CATEGORY_RESUME and not self._resume_matches(item, target_by_key[key]))
            ]
            result.skipped_existing = len(source_by_key) - len(to_add)

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
            plays: dict[str, set[str]] = {}
            for raw in raw_items:
                item = enrich_identity(raw)
                if count_unmapped and not has_portable_identity(item):
                    result.unmapped += 1
                    continue
                key = item_key(item)
                out.setdefault(key, item)
                if is_history:
                    rows.append((key, item))
                    stamp = normalize_watched_at(item.get("watched_at"))
                    if stamp:
                        plays.setdefault(key, set()).add(stamp)
            return out, rows, plays

        first_by_key, first_rows, first_plays = _index(sides["first"], True)
        second_by_key, second_rows, second_plays = _index(sides["second"], True)
        result.source_items = len(first_by_key)
        result.target_items = len(second_by_key)

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
        items: list[dict] | None = None,
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
        })
        logger.warning(
            "Pair %s: refusing to remove %d of %d %s items (%d%%) from %s — "
            "over the %d%% safety threshold; nothing was deleted",
            pair.display_name(), removals, target_size, category, percent,
            where, self._guard_removal_percent,
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
