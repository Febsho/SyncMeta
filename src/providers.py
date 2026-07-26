"""Provider adapters that let any service act as a sync source or target.

The rest of the codebase syncs in one direction: providers are read, and
PublicMetaDB is written.  Cross-service sync pairs need every service to be
usable at both ends, so this module wraps each client in a uniform adapter.

Items are passed around in the same normalized dict shape the provider clients
already emit (``title``, ``year``, ``media_type``, ``tmdb_id``, ``imdb_id``,
``mal_id``, ``anilist_id``, ``ids``, ...), so there is no separate translation
layer to keep in step.

Identity across services is keyed on TMDB id where known, because that is what
``ItemMatcher`` already resolves everything to.  Anything without one falls back
to the next most portable id.  The key is what pair diffing and the managed-key
removal mode are recorded against, so it must be stable across runs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Categories ─────────────────────────────────────────────────────────────
# Neutral names; each adapter maps them onto whatever its service calls them.
CATEGORY_WATCHLIST = "watchlist"
CATEGORY_HISTORY = "history"
CATEGORY_COLLECTION = "collection"

ALL_CATEGORIES = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)

CATEGORY_LABELS = {
    CATEGORY_WATCHLIST: "Watchlist / Plan to Watch",
    CATEGORY_HISTORY: "Watch History",
    CATEGORY_COLLECTION: "Completed / Collection",
}

# ── Removal modes ──────────────────────────────────────────────────────────
# additive: only ever add to the target (default; cannot damage a library)
# managed:  may remove, but only keys this pair previously wrote
# mirror:   may remove anything on the target that is not in the source
REMOVAL_ADDITIVE = "additive"
REMOVAL_MANAGED = "managed"
REMOVAL_MIRROR = "mirror"

ALL_REMOVAL_MODES = (REMOVAL_ADDITIVE, REMOVAL_MANAGED, REMOVAL_MIRROR)

REMOVAL_MODE_LABELS = {
    REMOVAL_ADDITIVE: "Additive only — never remove from the target",
    REMOVAL_MANAGED: "Mirror, but only items this pair added",
    REMOVAL_MIRROR: "Full mirror — may remove items added manually on the target",
}


def item_key(item: dict) -> str:
    """Return a stable cross-provider identity key for an item.

    TMDB is preferred because every provider either supplies it or can be mapped
    to it, which is what makes an id from one service comparable with an id from
    another.  Episodes are keyed down to season/episode so history diffs work at
    episode granularity.
    """
    ids = item.get("ids") or {}

    def _first(*values) -> str:
        for value in values:
            text = str(value or "").strip()
            if text and text.lower() not in {"none", "null"}:
                return text
        return ""

    media_type = str(item.get("media_type") or "").strip().lower() or "unknown"

    namespace = ""
    value = ""
    for candidate_namespace, candidate_value in (
        ("tmdb", _first(item.get("tmdb_id"), ids.get("tmdb"))),
        ("imdb", _first(item.get("imdb_id"), ids.get("imdb"))),
        ("anilist", _first(item.get("anilist_id"), ids.get("anilist"))),
        ("mal", _first(item.get("mal_id"), ids.get("mal"))),
        ("tvdb", _first(item.get("tvdb_id"), ids.get("tvdb"))),
        ("anidb", _first(item.get("anidb_id"), ids.get("anidb"))),
        ("simkl", _first(item.get("simkl_id"), ids.get("simkl"))),
    ):
        if candidate_value:
            namespace, value = candidate_namespace, candidate_value
            break

    if not value:
        # Last resort so unidentifiable items still diff against themselves
        # rather than colliding with every other unidentifiable item.
        namespace = "title"
        value = f"{_first(item.get('title'))}:{_first(item.get('year'))}".lower()

    key = f"{media_type}:{namespace}:{value}"

    season = item.get("season")
    episode = item.get("episode")
    if season is not None and episode is not None:
        try:
            key = f"{key}:s{int(season)}e{int(episode)}"
        except (TypeError, ValueError):
            pass
    return key


def has_portable_identity(item: dict) -> bool:
    """Whether an item carries an id that can be matched on another service.

    Items that fall back to a title/year key cannot be compared across providers
    with any confidence, so callers count them as unmapped rather than acting on
    them.
    """
    return ":title:" not in item_key(item)


def enrich_identity(item: dict) -> dict:
    """Add a TMDB id to anime-native items so keys compare across services.

    AniList reports AniList/MAL ids and no TMDB id, while Trakt and PMDB report
    TMDB.  Without this the same show read from two services would produce two
    different keys, every item would look new on every run, and a pair would
    re-add its whole source list forever.

    Returns the item unchanged when nothing can be added.  Never overwrites an
    id the provider already supplied.
    """
    ids = item.get("ids") or {}
    if str(item.get("tmdb_id") or ids.get("tmdb") or "").strip():
        return item

    from . import fribb_client

    entry = None
    try:
        for value, lookup in (
            (item.get("anilist_id") or ids.get("anilist"), fribb_client.lookup_by_anilist),
            (item.get("mal_id") or ids.get("mal"), fribb_client.lookup_by_mal),
            (item.get("anidb_id") or ids.get("anidb"), fribb_client.lookup_by_anidb),
            (item.get("simkl_id") or ids.get("simkl"), fribb_client.lookup_by_simkl),
        ):
            if not value:
                continue
            try:
                entry = lookup(int(value))
            except (TypeError, ValueError):
                entry = None
            if entry is not None:
                break
    except Exception:
        logger.debug("Identity enrichment failed for %r", item.get("title"), exc_info=True)
        return item

    if not isinstance(entry, dict):
        return item

    tmdb_id, mapped_media_type = fribb_client.extract_tmdb(entry.get("themoviedb_id"))
    if not tmdb_id:
        return item

    item_media_type = str(item.get("media_type") or "").strip().lower()
    if mapped_media_type and item_media_type and item_media_type != mapped_media_type:
        # The mapping is for the other TMDB namespace; adopting it would key the
        # item as the wrong thing entirely.
        return item

    enriched = dict(item)
    enriched["tmdb_id"] = str(tmdb_id)
    enriched_ids = dict(ids)
    enriched_ids.setdefault("tmdb", str(tmdb_id))
    enriched["ids"] = enriched_ids
    return enriched


class ProviderAdapter:
    """Uniform read/write surface over one service."""

    key: str = ""
    label: str = ""

    #: Categories this adapter can read and write. A category present in
    #: ``writes`` still requires ``can_write()`` to be true at runtime.
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()

    #: Whether this provider has named lists worth offering per pair. False for
    #: providers whose categories are single fixed lists (SIMKL/AniList statuses).
    supports_list_selection: bool = False

    def can_write(self) -> bool:
        """Whether the configured credentials permit writing at all."""
        return True

    def write_blocked_reason(self) -> str:
        """Human-readable explanation when ``can_write()`` is False."""
        return ""

    def readable_categories(self) -> tuple[str, ...]:
        return self.reads

    def writable_categories(self) -> tuple[str, ...]:
        return self.writes if self.can_write() else ()

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        """Read a category.

        ``source_lists`` narrows the read to specific named lists when the
        provider has them (see ``list_sources``).  Empty or None means the
        provider's default for that category — e.g. Trakt's own watchlist.
        """
        raise NotImplementedError

    def list_sources(self) -> list[dict]:
        """Named lists this provider can read, for per-pair selection.

        Returns ``[{"key", "label", "categories"}]``.  Providers whose
        categories are single fixed lists (SIMKL statuses, AniList statuses)
        return nothing, and their pairs simply sync the whole category.
        """
        return []

    def add(self, category: str, items: list[dict]) -> dict:
        raise NotImplementedError

    def remove(self, category: str, items: list[dict]) -> dict:
        raise NotImplementedError

    # ── helpers ────────────────────────────────────────────────────────────

    def describe(self) -> dict:
        """Capability summary for the UI."""
        return {
            "key": self.key,
            "label": self.label,
            "reads": list(self.readable_categories()),
            "writes": list(self.writable_categories()),
            "write_blocked_reason": "" if self.can_write() else self.write_blocked_reason(),
            # Named lists are deliberately NOT included: enumerating them hits
            # every provider's API, which made opening the pair editor wait on
            # live network calls. They are fetched on demand instead.
            "has_lists": bool(self.reads) and self.supports_list_selection,
        }

    def safe_list_sources(self) -> list[dict]:
        """list_sources() but never raising — it hits the network for some
        providers, and a capability report must not fail because one is down."""
        try:
            return list(self.list_sources())
        except Exception:
            logger.warning("%s: could not enumerate lists", self.label, exc_info=True)
            return []

    def _unsupported(self, category: str, action: str) -> dict:
        raise ValueError(f"{self.label} cannot {action} {category!r}")


class TraktAdapter(ProviderAdapter):
    key = "trakt"
    label = "Trakt"
    reads = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)
    writes = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)
    supports_list_selection = True

    def __init__(self, client):
        self._client = client

    def list_sources(self) -> list[dict]:
        """Trakt's own watchlist plus the user's personal and liked lists."""
        out = [{
            "key": "watchlist",
            "label": "Watchlist (native)",
            "categories": [CATEGORY_WATCHLIST],
        }]
        for meta in (self._client.get_personal_lists_metadata() or []):
            out.append({
                "key": f"list:{meta.get('user')}/{meta.get('slug')}",
                "label": f"{meta.get('name')} (personal)",
                "categories": [CATEGORY_WATCHLIST, CATEGORY_COLLECTION],
            })
        for meta in (self._client.get_liked_lists_metadata() or []):
            out.append({
                "key": f"list:{meta.get('user')}/{meta.get('slug')}",
                "label": f"{meta.get('name')} (liked)",
                "categories": [CATEGORY_WATCHLIST, CATEGORY_COLLECTION],
            })
        return out

    def _watchlist_items(self, source_lists: list[str] | None) -> list[dict]:
        """Native watchlist by default, or exactly the custom lists chosen."""
        selected = [str(key) for key in (source_lists or []) if str(key).strip()]
        items: list[dict] = []
        seen: set[str] = set()

        def _extend(fetched):
            for item in fetched or []:
                key = item_key(item)
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)

        if not selected or "watchlist" in selected:
            _extend(self._client.get_watchlist())
        for key in selected:
            if not key.startswith("list:"):
                continue
            reference = key.split(":", 1)[1]
            if "/" not in reference:
                continue
            user, slug = reference.split("/", 1)
            _extend(self._client.get_list_items(user, slug))
        return items

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        if category == CATEGORY_WATCHLIST:
            return self._watchlist_items(source_lists)
        if category == CATEGORY_HISTORY:
            return list(self._client.get_watched_history() or [])
        if category == CATEGORY_COLLECTION:
            return list(self._client.get_collection() or [])
        return self._unsupported(category, "read")

    def add(self, category: str, items: list[dict]) -> dict:
        if category == CATEGORY_WATCHLIST:
            return self._client.add_to_watchlist(items)
        if category == CATEGORY_HISTORY:
            return self._client.add_to_history(items)
        if category == CATEGORY_COLLECTION:
            return self._client.add_to_collection(items)
        return self._unsupported(category, "write")

    def remove(self, category: str, items: list[dict]) -> dict:
        if category == CATEGORY_WATCHLIST:
            return self._client.remove_from_watchlist(items)
        if category == CATEGORY_HISTORY:
            return self._client.remove_from_history(items)
        if category == CATEGORY_COLLECTION:
            return self._client.remove_from_collection(items)
        return self._unsupported(category, "remove from")


class SimklAdapter(ProviderAdapter):
    key = "simkl"
    label = "SIMKL"
    reads = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)
    writes = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)

    def __init__(self, client, media_types: list[str] | None = None):
        self._client = client
        # SIMKL is queried per media type; default to everything it supports.
        self._media_types = list(media_types or ["shows", "movies", "anime"])

    def _fetch_status(self, status: str) -> list[dict]:
        out: list[dict] = []
        for media_type in self._media_types:
            grouped = self._client.get_status(status, [media_type]) or {}
            out.extend(grouped.get(media_type, []) or [])
        return out

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        if category == CATEGORY_WATCHLIST:
            return self._fetch_status("plantowatch")
        if category == CATEGORY_COLLECTION:
            return self._fetch_status("completed")
        if category == CATEGORY_HISTORY:
            return list(self._client.get_watched_history() or [])
        return self._unsupported(category, "read")

    def add(self, category: str, items: list[dict]) -> dict:
        if category == CATEGORY_HISTORY:
            return self._client.add_to_history(items)
        if category in (CATEGORY_WATCHLIST, CATEGORY_COLLECTION):
            return self._client.add_to_list(items, category)
        return self._unsupported(category, "write")

    def remove(self, category: str, items: list[dict]) -> dict:
        if category == CATEGORY_HISTORY:
            return self._client.remove_from_history(items)
        if category in (CATEGORY_WATCHLIST, CATEGORY_COLLECTION):
            return self._client.remove_from_list(items, category)
        return self._unsupported(category, "remove from")


class AniListAdapter(ProviderAdapter):
    key = "anilist"
    label = "AniList"
    reads = (CATEGORY_WATCHLIST, CATEGORY_COLLECTION)
    # AniList tracks progress per series, not individual episode plays, so there
    # is no honest mapping for watch history. Advertising one would silently
    # write wrong data, so history is left unsupported at both ends.
    writes = (CATEGORY_WATCHLIST, CATEGORY_COLLECTION)

    _STATUS_FOR_CATEGORY = {
        CATEGORY_WATCHLIST: "PLANNING",
        CATEGORY_COLLECTION: "COMPLETED",
    }

    def __init__(self, client):
        self._client = client

    def can_write(self) -> bool:
        return bool(self._client.can_write())

    def write_blocked_reason(self) -> str:
        return self._client.write_blocked_reason()

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        status = self._STATUS_FOR_CATEGORY.get(category)
        if not status:
            return self._unsupported(category, "read")
        return list(self._client.get_status(status) or [])

    def add(self, category: str, items: list[dict]) -> dict:
        if category not in self._STATUS_FOR_CATEGORY:
            return self._unsupported(category, "write")
        return self._client.add_to_list(items, category)

    def remove(self, category: str, items: list[dict]) -> dict:
        if category not in self._STATUS_FOR_CATEGORY:
            return self._unsupported(category, "remove from")
        return self._client.remove_from_list(items, category)


class PmdbAdapter(ProviderAdapter):
    key = "pmdb"
    label = "PublicMetaDB"
    reads = (CATEGORY_WATCHLIST, CATEGORY_HISTORY)
    writes = (CATEGORY_WATCHLIST, CATEGORY_HISTORY)
    supports_list_selection = True

    def __init__(self, client):
        self._client = client

    def can_write(self) -> bool:
        return bool(getattr(self._client, "_config", None) and self._client._config.api_key)

    def write_blocked_reason(self) -> str:
        if self.can_write():
            return ""
        return "PublicMetaDB needs an API key before it can be written to."

    def _watchlist_id(self) -> str | None:
        existing = self._client.find_list_by_type("watchlist")
        if isinstance(existing, dict) and existing.get("id"):
            return str(existing["id"])
        created = self._client.get_or_create_list(
            "Watchlist", "SyncMeta cross-service watchlist", False, "watchlist",
        )
        if isinstance(created, dict) and created.get("id"):
            return str(created["id"])
        return None

    @staticmethod
    def _normalize_pmdb_entry(entry: dict) -> dict | None:
        tmdb_id = entry.get("tmdb_id") or (entry.get("media") or {}).get("tmdb_id")
        if not tmdb_id:
            return None
        media_type = str(entry.get("media_type") or (entry.get("media") or {}).get("media_type") or "").lower()
        return {
            "title": entry.get("title") or (entry.get("media") or {}).get("title") or "Unknown",
            "year": entry.get("year") or (entry.get("media") or {}).get("year"),
            "media_type": media_type or "movie",
            "tmdb_id": str(tmdb_id),
            "ids": {"tmdb": str(tmdb_id)},
            "pmdb_item_id": entry.get("id"),
        }

    def list_sources(self) -> list[dict]:
        """PublicMetaDB's own lists, so a pair can read one custom list."""
        out = [{
            "key": "watchlist",
            "label": "Watchlist (native)",
            "categories": [CATEGORY_WATCHLIST],
        }]
        for entry in self._client.get_lists() or []:
            list_id = entry.get("id")
            if not list_id:
                continue
            if str(entry.get("list_type") or "").strip().lower() == "watchlist":
                continue  # already offered above
            out.append({
                "key": f"list:{list_id}",
                "label": str(entry.get("name") or f"List {list_id}"),
                "categories": [CATEGORY_WATCHLIST],
            })
        return out

    def _pmdb_watchlist_items(self, source_lists: list[str] | None) -> list[dict]:
        selected = [str(key) for key in (source_lists or []) if str(key).strip()]
        list_ids: list[str] = []
        if not selected or "watchlist" in selected:
            native = self._watchlist_id()
            if native:
                list_ids.append(native)
        for key in selected:
            if key.startswith("list:"):
                list_ids.append(key.split(":", 1)[1])

        items: list[dict] = []
        seen: set[str] = set()
        for list_id in list_ids:
            for raw in self._client.get_list_items(list_id) or []:
                normalized = self._normalize_pmdb_entry(raw)
                if not normalized:
                    continue
                key = item_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                items.append(normalized)
        return items

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        if category == CATEGORY_WATCHLIST:
            return self._pmdb_watchlist_items(source_lists)
        if category == CATEGORY_HISTORY:
            raw = self._client.get_watched_history() or []
            out: list[dict] = []
            for entry in raw:
                normalized = self._normalize_pmdb_entry(entry)
                if not normalized:
                    continue
                for field in ("season", "episode", "watched_at"):
                    if entry.get(field) is not None:
                        normalized[field] = entry[field]
                out.append(normalized)
            return out
        return self._unsupported(category, "read")

    def add(self, category: str, items: list[dict]) -> dict:
        totals = {"added": 0, "not_found": 0, "batches": 1 if items else 0}
        if category == CATEGORY_WATCHLIST:
            list_id = self._watchlist_id()
            if not list_id:
                totals["not_found"] = len(items)
                return totals
            payload = []
            for item in items:
                tmdb_id = str(item.get("tmdb_id") or "").strip()
                if not tmdb_id.isdigit():
                    totals["not_found"] += 1
                    continue
                payload.append({"tmdb_id": int(tmdb_id), "media_type": item.get("media_type") or "movie"})
            if payload:
                self._client.add_items_to_list_batch(list_id, payload)
                totals["added"] += len(payload)
            return totals
        if category == CATEGORY_HISTORY:
            for item in items:
                tmdb_id = str(item.get("tmdb_id") or "").strip()
                if not tmdb_id.isdigit():
                    totals["not_found"] += 1
                    continue
                self._client.mark_watched(
                    int(tmdb_id),
                    item.get("media_type") or "movie",
                    season=item.get("season"),
                    episode=item.get("episode"),
                    watched_at=item.get("watched_at"),
                )
                totals["added"] += 1
            return totals
        return self._unsupported(category, "write")

    def remove(self, category: str, items: list[dict]) -> dict:
        totals = {"deleted": 0, "not_found": 0, "batches": 1 if items else 0}
        if category == CATEGORY_WATCHLIST:
            list_id = self._watchlist_id()
            if not list_id:
                totals["not_found"] = len(items)
                return totals
            for item in items:
                pmdb_item_id = item.get("pmdb_item_id")
                if not pmdb_item_id:
                    totals["not_found"] += 1
                    continue
                self._client.remove_item_from_list(list_id, str(pmdb_item_id))
                totals["deleted"] += 1
            return totals
        return self._unsupported(category, "remove from")


class MdbListAdapter(ProviderAdapter):
    """Source-only adapter over the user's selected MDBList lists.

    MDBList exposes list *reading* only — this app has no write path, and
    MDBList's own watch-status sync is handled by its Trakt/Plex integrations.
    So it can feed other services but can never be a target, which
    ``writes = ()`` reports rather than failing at run time.

    An MDBList list is a curated collection with no watched/unwatched semantics,
    so the same combined items answer both the watchlist and collection
    categories, letting a user push a curation into either.
    """

    key = "mdblist"
    label = "MDBList"
    reads = (CATEGORY_WATCHLIST, CATEGORY_COLLECTION)
    writes = ()
    supports_list_selection = True

    def __init__(self, client, selected_lists: list[dict] | None = None):
        self._client = client
        self._selected_lists = list(selected_lists or [])
        self._cache: list[dict] | None = None
        self._cache_key: str = ""

    def can_write(self) -> bool:
        return False

    def write_blocked_reason(self) -> str:
        return (
            "MDBList can only be read from. It exposes no write API here, so it "
            "can feed other services but cannot be a sync target."
        )

    def list_sources(self) -> list[dict]:
        return [
            {
                "key": f"list:{entry.get('id')}",
                "label": str(entry.get("name") or f"List {entry.get('id')}"),
                "categories": [CATEGORY_WATCHLIST, CATEGORY_COLLECTION],
            }
            for entry in self._selected_lists
            if entry.get("id")
        ]

    def _selected_items(self, source_lists: list[str] | None = None) -> list[dict]:
        chosen = {
            str(key).split(":", 1)[1]
            for key in (source_lists or [])
            if str(key).startswith("list:")
        }
        cache_key = ",".join(sorted(chosen))
        if self._cache is not None and self._cache_key == cache_key:
            return self._cache
        items: list[dict] = []
        seen: set[str] = set()
        for entry in self._selected_lists:
            if chosen and str(entry.get("id")) not in chosen:
                continue
            list_id = entry.get("id")
            if not list_id:
                continue
            try:
                fetched = self._client.get_list_items(int(list_id)) or []
            except Exception:
                logger.warning(
                    "MDBList: could not read list %s (%s)",
                    list_id, entry.get("name") or "unnamed", exc_info=True,
                )
                continue
            for item in fetched:
                key = item_key(item)
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
        self._cache = items
        self._cache_key = cache_key
        return items

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        if category in (CATEGORY_WATCHLIST, CATEGORY_COLLECTION):
            return list(self._selected_items(source_lists))
        return self._unsupported(category, "read")

    def add(self, category: str, items: list[dict]) -> dict:
        raise ValueError(self.write_blocked_reason())

    def remove(self, category: str, items: list[dict]) -> dict:
        raise ValueError(self.write_blocked_reason())


#: Stable provider ordering for UI listings.
PROVIDER_ORDER = ("trakt", "simkl", "anilist", "mdblist", "pmdb")
