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
    REMOVAL_ADDITIVE,
    REMOVAL_MANAGED,
    REMOVAL_MIRROR,
    enrich_identity,
    has_portable_identity,
    item_key,
)

logger = logging.getLogger(__name__)


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
        removed_keys = {item_key(item) for item in (removed or [])}
        kept = [item for item in current if item_key(item) not in removed_keys]
        kept.extend(added or [])
        self._entries[key] = kept

    def invalidate_provider(self, provider: str) -> None:
        for key in [k for k in self._entries if k[0] == str(provider)]:
            self._entries.pop(key, None)


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
    errors: list[str] = field(default_factory=list)
    managed_keys: list[str] = field(default_factory=list)
    #: Provider reads this category answered from the batch cache instead of the
    #: network. Surfaced so the saving is visible rather than merely claimed.
    cached_reads: int = 0

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "source_items": self.source_items,
            "target_items": self.target_items,
            "added": self.added,
            "removed": self.removed,
            "unmapped": self.unmapped,
            "skipped_existing": self.skipped_existing,
            "cached_reads": self.cached_reads,
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
    def error_count(self) -> int:
        return len(self.errors) + sum(len(c.errors) for c in self.categories)

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "removal_mode": self.removal_mode,
            "dry_run": self.dry_run,
            "added": self.added,
            "removed": self.removed,
            "unmapped": self.unmapped,
            "cached_reads": self.cached_reads,
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
    ):
        self._adapters = dict(adapters or {})
        self._dry_run = bool(dry_run)
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

        readable = set(source.readable_categories())
        writable = set(target.writable_categories())
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

    # ── execution ──────────────────────────────────────────────────────────

    def run_pair(self, pair) -> PairRunStats:
        with self._batch_cache():
            return self._run_pair(pair)

    def _run_pair(self, pair) -> PairRunStats:
        stats = PairRunStats(
            pair_id=pair.pair_id,
            name=pair.display_name(),
            source=pair.source,
            target=pair.target,
            removal_mode=pair.removal_mode,
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
            stats.categories.append(self._run_category(pair, source, target, category))

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
            target_items = _read(target, None)
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
        source_by_key: dict[str, dict] = {}
        for raw_item in source_items:
            item = enrich_identity(raw_item)
            if not has_portable_identity(item):
                # Nothing portable to match on; count it rather than guessing.
                result.unmapped += 1
                continue
            source_by_key.setdefault(item_key(item), item)

        target_by_key: dict[str, dict] = {}
        for raw_item in target_items:
            item = enrich_identity(raw_item)
            target_by_key.setdefault(item_key(item), item)

        to_add = [item for key, item in source_by_key.items() if key not in target_by_key]
        result.skipped_existing = len(source_by_key) - len(to_add)

        to_remove = self._items_to_remove(pair, category, source_by_key, target_by_key)

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
                    totals = target.add(category, to_add, getattr(pair, "target_list", "")) or {}
                    result.added = int(totals.get("added") or 0)
                    result.unmapped += int(totals.get("not_found") or 0)
                    wrote_added = to_add
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
                    totals = target.remove(category, to_remove, getattr(pair, "target_list", "")) or {}
                    result.removed = int(totals.get("deleted") or 0)
                    wrote_removed = to_remove
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
                target.key, category, None, added=wrote_added, removed=wrote_removed,
            )

        self._record_managed_keys(pair, category, source_by_key, to_remove, result)
        return result

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
