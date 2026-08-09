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

# Direction of a pair.
#
# one_way: read the source, write the target. Unambiguous, no conflicts.
# two_way: both services end up holding the union of the two. This is NOT two
#   one-way passes run back to back — that would let the order decide whether a
#   deletion propagates or gets resurrected by the opposite direction. Instead
#   the pair's managed-key set is treated as *the state both sides last agreed
#   on*, which is what makes a one-sided item interpretable: present on one side
#   and previously synced means it was deleted on the other, while present on one
#   side and never synced means it is new. See CrossSyncService._run_category.
MODE_ONE_WAY = "one_way"
MODE_TWO_WAY = "two_way"

ALL_PAIR_MODES = (MODE_ONE_WAY, MODE_TWO_WAY)

PAIR_MODE_LABELS = {
    MODE_ONE_WAY: "One-way — copy from the first service to the second",
    MODE_TWO_WAY: "Two-way — keep both services holding the same items",
}

#: Removal modes that mean something in two-way. `mirror` is "make the target
#: match the source", which cannot hold in both directions at once — applied
#: bidirectionally it just means whichever side ran first wins and the other's
#: unique items are destroyed. It is downgraded rather than offered.
TWO_WAY_REMOVAL_MODES = (REMOVAL_ADDITIVE, REMOVAL_MANAGED)

# Privacy of a list a pair *creates* on its target. Only two providers can act
# on this — PublicMetaDB and Trakt are the only ones that both accept writes and
# have a notion of list privacy — so `supports_visibility` gates the control
# rather than showing a setting that silently does nothing.
VISIBILITY_PRIVATE = "private"
VISIBILITY_PUBLIC = "public"

ALL_VISIBILITIES = (VISIBILITY_PRIVATE, VISIBILITY_PUBLIC)

VISIBILITY_LABELS = {
    VISIBILITY_PRIVATE: "Private — only you can see it",
    VISIBILITY_PUBLIC: "Public — anyone with the link can see it",
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
    # The same Fribb entry usually carries the IMDB id too. Trakt and SIMKL
    # match on IMDB more reliably than on a TMDB tv id, so an anime-native item
    # gains the id the target's writer actually wants.
    if not str(item.get("imdb_id") or ids.get("imdb") or "").strip():
        imdb_id = fribb_client.single_imdb_id(entry.get("imdb_id"))
        if imdb_id:
            enriched["imdb_id"] = imdb_id
            enriched_ids.setdefault("imdb", imdb_id)
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

    #: Whether items can be written into a *named list* on this provider rather
    #: than only its fixed watchlist/collection. SIMKL and AniList have no
    #: writable custom lists, so a custom-list destination must not be offered
    #: for them at all.
    # None means an older/third-party adapter did not declare the capability;
    # built-in adapters always declare True or False explicitly.
    supports_target_lists: bool | None = None
    #: True when this provider can create a list and be told whether it is
    #: public. False everywhere else, and the pair editor hides the control.
    supports_visibility: bool = False

    #: Whether ``search_lists`` actually queries the provider. Only Trakt and
    #: MDBList expose a public list search; offering the box elsewhere invites a
    #: search that can only ever come back empty.
    supports_list_search: bool = False
    # Categories for which a named destination is meaningful.  This is exposed
    # explicitly so clients do not offer a custom-list picker for watch history.
    target_list_categories: tuple[str, ...] = ()

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

    def fetch_target(self, category: str, target_list: str = "") -> list[dict]:
        """Read the exact destination that ``add``/``remove`` will mutate."""
        lists = [target_list] if str(target_list or "").strip() else None
        return self.fetch(category, lists)

    def list_sources(self) -> list[dict]:
        """Named lists this provider can read, for per-pair selection.

        Returns ``[{"key", "label", "categories"}]``.  Providers whose
        categories are single fixed lists (SIMKL statuses, AniList statuses)
        return nothing, and their pairs simply sync the whole category.
        """
        return []

    def target_lists(self) -> list[dict]:
        """Named lists items can be written into. Empty unless supported."""
        return []

    def search_lists(self, query: str) -> list[dict]:
        """Public lists matching a query, for adding one not already selected."""
        return []

    def safe_search_lists(self, query: str) -> list[dict]:
        if not str(query or "").strip():
            return []
        try:
            return list(self.search_lists(query))
        except Exception:
            logger.warning("%s: list search failed", self.label, exc_info=True)
            return []

    def safe_target_lists(self) -> list[dict]:
        if not self.supports_target_lists or not self.can_write():
            return []
        try:
            return list(self.target_lists())
        except Exception:
            logger.warning("%s: could not enumerate writable lists", self.label, exc_info=True)
            return []

    def add(
        self, category: str, items: list[dict], target_list: str = "",
        visibility: str = VISIBILITY_PRIVATE,
    ) -> dict:
        """Write ``items``. ``visibility`` applies only to a list this call has
        to create; an existing list is never re-flagged."""
        raise NotImplementedError

    def remove(self, category: str, items: list[dict], target_list: str = "") -> dict:
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
            "has_target_lists": self.supports_target_lists and self.can_write(),
            "target_list_categories": list(self.target_list_categories),
            "has_list_search": bool(self.supports_list_search),
            "has_visibility": bool(self.supports_visibility) and self.can_write(),
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
    supports_target_lists = True
    supports_visibility = True
    supports_list_search = True
    target_list_categories = (CATEGORY_WATCHLIST, CATEGORY_COLLECTION)

    _SIMKL_STATUS_LABELS = {
        "watching": "Watching",
        "completed": "Completed",
        "hold": "On Hold",
        "dropped": "Dropped",
    }

    def __init__(self, client):
        self._client = client

    def list_sources(self) -> list[dict]:
        """Trakt's own watchlist plus the user's personal and liked lists."""
        out = [
            {"key": "watchlist", "label": "Watchlist", "category": CATEGORY_WATCHLIST, "kind": "status"},
            {"key": "collection", "label": "Collection", "category": CATEGORY_COLLECTION, "kind": "status"},
            {"key": "history", "label": "Watch History", "category": CATEGORY_HISTORY, "kind": "status"},
        ]
        for meta in (self._client.get_personal_lists_metadata() or []):
            out.append({
                "key": f"list:{meta.get('user')}/{meta.get('slug')}",
                "label": f"{meta.get('name')} (personal)",
                "category": CATEGORY_WATCHLIST,
                "kind": "list",
            })
        for meta in (self._client.get_liked_lists_metadata() or []):
            out.append({
                "key": f"list:{meta.get('user')}/{meta.get('slug')}",
                "label": f"{meta.get('name')} (liked)",
                "category": CATEGORY_WATCHLIST,
                "kind": "list",
            })
        return out

    def _watchlist_items(self, source_lists: list[str] | None) -> list[dict]:
        """Native watchlist by default, or exactly the sources chosen.

        A selection that names only other categories (say `collection`) must not
        silently fall back to the whole watchlist, so it yields nothing here.
        """
        selected = [str(key) for key in (source_lists or []) if str(key).strip()]
        if selected and not any(
            key == "watchlist" or key.startswith("list:") for key in selected
        ):
            return []
        items: list[dict] = []
        seen: set[str] = set()

        def _extend(fetched, destination_key: str = ""):
            for item in fetched or []:
                if destination_key:
                    item = {**item, "_syncmeta_target_list": destination_key}
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
            _extend(self._client.get_list_items(user, slug), key)
        return items

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        selected_lists = [str(k) for k in (source_lists or []) if str(k).startswith("list:")]
        if selected_lists and category in (CATEGORY_WATCHLIST, CATEGORY_COLLECTION):
            return self._watchlist_items(selected_lists)
        if category == CATEGORY_WATCHLIST:
            return self._watchlist_items(source_lists)
        if category == CATEGORY_HISTORY:
            return list(self._client.get_watched_history() or [])
        if category == CATEGORY_COLLECTION:
            return list(self._client.get_collection() or [])
        return self._unsupported(category, "read")

    def _automatic_simkl_destinations(self, target_list: str) -> list[tuple[str, str]]:
        prefix = "auto:simkl:"
        if not str(target_list or "").startswith(prefix):
            return []
        statuses = [part for part in str(target_list)[len(prefix):].split(",") if part]
        existing = self._client.get_personal_lists_metadata() or []
        by_name = {str(meta.get("name") or "").strip().casefold(): meta for meta in existing}
        out: list[tuple[str, str]] = []
        for status in statuses:
            label = self._SIMKL_STATUS_LABELS.get(status, status.replace("_", " ").title())
            name = f"SyncMeta · SIMKL {label}"
            meta = by_name.get(name.casefold())
            if meta and meta.get("user") and meta.get("slug"):
                out.append((status, f"list:{meta['user']}/{meta['slug']}"))
        return out

    def fetch_target(self, category: str, target_list: str = "") -> list[dict]:
        automatic = self._automatic_simkl_destinations(target_list)
        if str(target_list or "").startswith("auto:simkl:"):
            # Missing lists correctly read as empty; creation happens only when
            # there is something to write, so dry runs remain side-effect free.
            items: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for status, destination_key in automatic:
                destination = self._parse_list_key(destination_key)
                if not destination:
                    continue
                for raw in self._client.get_list_items(destination[0], destination[1]) or []:
                    identity = (status, item_key(raw))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    items.append({
                        **raw,
                        "_syncmeta_source_status": status,
                        "_syncmeta_target_list": destination_key,
                    })
            return items
        return super().fetch_target(category, target_list)

    def _ensure_automatic_simkl_list(
        self, status: str, visibility: str = VISIBILITY_PRIVATE,
    ) -> tuple[str, str]:
        label = self._SIMKL_STATUS_LABELS.get(status, status.replace("_", " ").title())
        name = f"SyncMeta · SIMKL {label}"
        meta = self._client.get_or_create_personal_list(
            name, f"Maintained by SyncMeta from the SIMKL {label} status.",
            privacy=visibility,
        )
        return str(meta["user"]), str(meta["slug"])

    def target_lists(self) -> list[dict]:
        """The user's own Trakt lists. Liked lists belong to someone else and
        cannot be written to, so they are not offered."""
        return [
            {
                "key": f"list:{meta.get('user')}/{meta.get('slug')}",
                "label": str(meta.get("name") or meta.get("slug") or "Trakt list"),
            }
            for meta in (self._client.get_personal_lists_metadata() or [])
            if meta.get("user") and meta.get("slug")
        ]

    def search_lists(self, query: str) -> list[dict]:
        return [
            {
                "key": f"list:{meta.get('user')}/{meta.get('slug')}",
                "label": f"{meta.get('name')} (by {meta.get('user')})",
                "category": CATEGORY_WATCHLIST,
                "kind": "list",
            }
            for meta in (self._client.search_lists(query) or [])
            if meta.get("user") and meta.get("slug")
        ]

    @staticmethod
    def _parse_list_key(target_list: str) -> tuple[str, str] | None:
        reference = str(target_list or "").strip()
        if not reference.startswith("list:"):
            return None
        reference = reference.split(":", 1)[1]
        if "/" not in reference:
            return None
        user, slug = reference.split("/", 1)
        return (user, slug) if user and slug else None

    def add(
        self, category: str, items: list[dict], target_list: str = "",
        visibility: str = VISIBILITY_PRIVATE,
    ) -> dict:
        if str(target_list or "").startswith("auto:simkl:"):
            totals = {"added": 0, "not_found": 0, "batches": 0}
            grouped: dict[str, list[dict]] = {}
            for item in items:
                status = str(item.get("_syncmeta_source_status") or "completed")
                grouped.setdefault(status, []).append(item)
            for status, status_items in grouped.items():
                user, slug = self._ensure_automatic_simkl_list(status, visibility)
                result = self._client.add_to_custom_list(user, slug, status_items) or {}
                for key in totals:
                    totals[key] += int(result.get(key) or 0)
            return totals
        destination = self._parse_list_key(target_list)
        if destination:
            return self._client.add_to_custom_list(destination[0], destination[1], items)
        if category == CATEGORY_WATCHLIST:
            return self._client.add_to_watchlist(items)
        if category == CATEGORY_HISTORY:
            return self._client.add_to_history(items)
        if category == CATEGORY_COLLECTION:
            return self._client.add_to_collection(items)
        return self._unsupported(category, "write")

    def remove(self, category: str, items: list[dict], target_list: str = "") -> dict:
        if str(target_list or "").startswith("auto:simkl:"):
            totals = {"deleted": 0, "not_found": 0, "batches": 0}
            grouped: dict[str, list[dict]] = {}
            for item in items:
                destination_key = str(item.get("_syncmeta_target_list") or "")
                grouped.setdefault(destination_key, []).append(item)
            for destination_key, status_items in grouped.items():
                destination = self._parse_list_key(destination_key)
                if not destination:
                    totals["not_found"] += len(status_items)
                    continue
                result = self._client.remove_from_custom_list(destination[0], destination[1], status_items) or {}
                for key in totals:
                    totals[key] += int(result.get(key) or 0)
            return totals
        destination = self._parse_list_key(target_list)
        if destination:
            return self._client.remove_from_custom_list(destination[0], destination[1], items)
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
    supports_list_selection = True
    supports_target_lists = False

    def __init__(self, client, media_types: list[str] | None = None):
        self._client = client
        # SIMKL is queried per media type; default to everything it supports.
        self._media_types = list(media_types or ["shows", "movies", "anime"])

    #: SIMKL's own list names, with the neutral category each maps onto.
    _STATUSES = (
        ("watching", "Watching", CATEGORY_COLLECTION),
        ("plantowatch", "Plan to Watch", CATEGORY_WATCHLIST),
        ("completed", "Completed", CATEGORY_COLLECTION),
        ("hold", "On Hold", CATEGORY_COLLECTION),
        ("dropped", "Dropped", CATEGORY_COLLECTION),
    )
    _MEDIA_LABELS = {"shows": "Series", "movies": "Movies", "anime": "Anime"}

    def list_sources(self) -> list[dict]:
        out = []
        for status, status_label, category in self._STATUSES:
            for media_type in self._media_types:
                out.append({
                    "key": f"status:{status}:{media_type}",
                    "label": f"{status_label} — {self._MEDIA_LABELS.get(media_type, media_type.title())}",
                    "category": category,
                    "kind": "status",
                })
        return out

    def _fetch_status(self, status: str) -> list[dict]:
        out: list[dict] = []
        for media_type in self._media_types:
            grouped = self._client.get_status(status, [media_type]) or {}
            for item in grouped.get(media_type, []) or []:
                out.append({**item, "_syncmeta_source_status": status})
        return out

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        # Watch history is a fixed account-level source on SIMKL. Status chips
        # scope list/collection reads only; older pairs commonly contain those
        # chips alongside the History category, and treating them as a history
        # filter made the adapter return an empty list without ever calling the
        # history endpoint.
        if category == CATEGORY_HISTORY:
            return list(self._client.get_watched_history() or [])

        selected = self._selected_statuses(category, source_lists)
        if selected:
            items: list[dict] = []
            seen: set[str] = set()
            for status, media_type in selected:
                for item in self._client.get_status(status, [media_type]).get(media_type, []) or []:
                    item = {**item, "_syncmeta_source_status": status}
                    key = item_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(item)
            return items

        if source_lists:
            # A non-empty selection belonging only to another category means
            # this category was intentionally not selected; never fall back to
            # the full default status in that case.
            return []

        if category == CATEGORY_WATCHLIST:
            return self._fetch_status("plantowatch")
        if category == CATEGORY_COLLECTION:
            return self._fetch_status("completed")
        return self._unsupported(category, "read")

    def _selected_statuses(self, category: str, source_lists: list[str] | None) -> list[tuple[str, str]]:
        """Parse `status:<name>:<media_type>` keys belonging to this category."""
        by_status = {status: cat for status, _label, cat in self._STATUSES}
        out: list[tuple[str, str]] = []
        for key in source_lists or []:
            parts = str(key).split(":")
            if len(parts) != 3 or parts[0] != "status":
                continue
            status, media_type = parts[1], parts[2]
            if by_status.get(status) != category:
                continue
            out.append((status, media_type))
        return out

    def add(
        self, category: str, items: list[dict], target_list: str = "",
        visibility: str = VISIBILITY_PRIVATE,
    ) -> dict:
        if category == CATEGORY_HISTORY:
            return self._client.add_to_history(items)
        if category in (CATEGORY_WATCHLIST, CATEGORY_COLLECTION):
            return self._client.add_to_list(items, category)
        return self._unsupported(category, "write")

    def remove(self, category: str, items: list[dict], target_list: str = "") -> dict:
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
    supports_list_selection = True
    supports_target_lists = False

    _STATUS_FOR_CATEGORY = {
        CATEGORY_WATCHLIST: "PLANNING",
        CATEGORY_COLLECTION: "COMPLETED",
    }

    #: AniList's own list statuses, with the category each maps onto. Only
    #: PLANNING and COMPLETED can be written to; the rest are read-only sources.
    _STATUSES = (
        ("PLANNING", "Planning", CATEGORY_WATCHLIST),
        ("CURRENT", "Watching", CATEGORY_COLLECTION),
        ("COMPLETED", "Completed", CATEGORY_COLLECTION),
        ("PAUSED", "Paused", CATEGORY_COLLECTION),
        ("DROPPED", "Dropped", CATEGORY_COLLECTION),
    )

    def list_sources(self) -> list[dict]:
        return [
            {
                "key": f"status:{status}",
                "label": label,
                "category": category,
                "kind": "status",
            }
            for status, label, category in self._STATUSES
        ]

    def __init__(self, client):
        self._client = client

    def can_write(self) -> bool:
        return bool(self._client.can_write())

    def write_blocked_reason(self) -> str:
        return self._client.write_blocked_reason()

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        by_status = {status: cat for status, _label, cat in self._STATUSES}
        selected = [
            str(key).split(":", 1)[1]
            for key in (source_lists or [])
            if str(key).startswith("status:")
            and by_status.get(str(key).split(":", 1)[1]) == category
        ]
        if selected:
            items: list[dict] = []
            seen: set[str] = set()
            for status in selected:
                for item in self._client.get_status(status) or []:
                    key = item_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(item)
            return items

        status = self._STATUS_FOR_CATEGORY.get(category)
        if not status:
            return self._unsupported(category, "read")
        return list(self._client.get_status(status) or [])

    def add(
        self, category: str, items: list[dict], target_list: str = "",
        visibility: str = VISIBILITY_PRIVATE,
    ) -> dict:
        if category not in self._STATUS_FOR_CATEGORY:
            return self._unsupported(category, "write")
        return self._client.add_to_list(items, category)

    def remove(self, category: str, items: list[dict], target_list: str = "") -> dict:
        if category not in self._STATUS_FOR_CATEGORY:
            return self._unsupported(category, "remove from")
        return self._client.remove_from_list(items, category)


class PmdbAdapter(ProviderAdapter):
    key = "pmdb"
    label = "PublicMetaDB"
    reads = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)
    writes = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)
    supports_list_selection = True
    supports_target_lists = True
    supports_visibility = True
    target_list_categories = (CATEGORY_WATCHLIST, CATEGORY_COLLECTION)

    _COLLECTION_LIST_NAME = "SyncMeta · Collection"

    def __init__(self, client):
        self._client = client

    def can_write(self) -> bool:
        return bool(getattr(self._client, "_config", None) and self._client._config.api_key)

    def write_blocked_reason(self) -> str:
        if self.can_write():
            return ""
        return "PublicMetaDB needs an API key before it can be written to."

    def _watchlist_id(self, visibility: str = VISIBILITY_PRIVATE) -> str | None:
        existing = self._client.find_list_by_type("watchlist")
        if isinstance(existing, dict) and existing.get("id"):
            return str(existing["id"])
        created = self._client.get_or_create_list(
            "Watchlist", "SyncMeta cross-service watchlist",
            visibility == VISIBILITY_PUBLIC, "watchlist",
        )
        if isinstance(created, dict) and created.get("id"):
            return str(created["id"])
        return None

    def _collection_id(
        self, *, create: bool = False, visibility: str = VISIBILITY_PRIVATE,
    ) -> str | None:
        """Return the managed default collection list without creating on reads."""
        existing = self._client.find_list_by_name(self._COLLECTION_LIST_NAME)
        if isinstance(existing, dict) and existing.get("id"):
            return str(existing["id"])
        if not create:
            return None
        created = self._client.get_or_create_list(
            self._COLLECTION_LIST_NAME,
            "Completed and collection items managed by SyncMeta pairs",
            visibility == VISIBILITY_PUBLIC,
            "custom",
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
            "label": "Watchlist",
            "category": CATEGORY_WATCHLIST,
            "kind": "status",
        }, {
            "key": "collection",
            "label": "SyncMeta Collection",
            "category": CATEGORY_COLLECTION,
            "kind": "status",
        }]
        for entry in self._client.get_lists() or []:
            list_id = entry.get("id")
            if not list_id:
                continue
            if str(entry.get("list_type") or "").strip().lower() == "watchlist":
                continue  # already offered above
            if str(entry.get("name") or "").strip() == self._COLLECTION_LIST_NAME:
                continue  # offered above as the default collection
            out.append({
                "key": f"list:{list_id}",
                "label": str(entry.get("name") or f"List {list_id}"),
                "category": CATEGORY_WATCHLIST,
                "kind": "list",
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

    def _pmdb_collection_items(self, source_lists: list[str] | None) -> list[dict]:
        selected = [str(key) for key in (source_lists or []) if str(key).strip()]
        list_ids: list[str] = []
        if not selected or "collection" in selected:
            default_id = self._collection_id(create=False)
            if default_id:
                list_ids.append(default_id)
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
        if category == CATEGORY_COLLECTION:
            return self._pmdb_collection_items(source_lists)
        return self._unsupported(category, "read")

    def target_lists(self) -> list[dict]:
        out = [{"key": "watchlist", "label": "Watchlist"}]
        for entry in self._client.get_lists() or []:
            list_id = entry.get("id")
            if not list_id:
                continue
            if str(entry.get("list_type") or "").strip().lower() == "watchlist":
                continue
            out.append({"key": f"list:{list_id}", "label": str(entry.get("name") or f"List {list_id}")})
        return out

    def add(
        self, category: str, items: list[dict], target_list: str = "",
        visibility: str = VISIBILITY_PRIVATE,
    ) -> dict:
        totals = {"added": 0, "not_found": 0, "batches": 1 if items else 0}
        destination = str(target_list or "").strip()
        if destination.startswith("list:"):
            list_id = destination.split(":", 1)[1]
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
        if category == CATEGORY_WATCHLIST:
            list_id = self._watchlist_id(visibility)
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
        if category == CATEGORY_COLLECTION:
            list_id = self._collection_id(create=True, visibility=visibility)
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
        return self._unsupported(category, "write")

    def remove(self, category: str, items: list[dict], target_list: str = "") -> dict:
        totals = {"deleted": 0, "not_found": 0, "batches": 1 if items else 0}
        destination = str(target_list or "").strip()
        if destination.startswith("list:"):
            list_id = destination.split(":", 1)[1]
            for item in items:
                pmdb_item_id = item.get("pmdb_item_id")
                if not pmdb_item_id:
                    totals["not_found"] += 1
                    continue
                self._client.remove_item_from_list(list_id, str(pmdb_item_id))
                totals["deleted"] += 1
            return totals
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
        if category == CATEGORY_COLLECTION:
            list_id = self._collection_id(create=False)
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
    """Read/write adapter over MDBList's own sync surface and the user's lists.

    Two different things live behind this one provider:

    * MDBList's account-level sync API — ``/sync/watchlist``, ``/sync/collection``
      and ``/sync/watched`` — which is Trakt-shaped and both readable and
      writable. MDBList marks these endpoints BETA.
    * The user's curated static lists, read through ``/lists/{id}/items`` and
      written through ``/lists/{id}/items/{add|remove}``. A curated list has no
      watched/unwatched semantics, so the same items answer both the watchlist
      and collection categories, letting a user push a curation into either.

    Writing needs either an OAuth access token or an API key; a profile with
    neither can still be constructed but reports why it cannot be a target.
    """

    key = "mdblist"
    label = "MDBList"
    reads = (CATEGORY_WATCHLIST, CATEGORY_COLLECTION, CATEGORY_HISTORY)
    writes = (CATEGORY_WATCHLIST, CATEGORY_COLLECTION, CATEGORY_HISTORY)
    supports_list_selection = True
    supports_list_search = True
    supports_target_lists = True
    # A named MDBList list is a curation; watch history has no such destination,
    # the same split Trakt makes.
    target_list_categories = (CATEGORY_WATCHLIST, CATEGORY_COLLECTION)

    #: Sync sources that are not one of the user's static lists.
    _NATIVE_SOURCES = (
        ("watchlist", "Watchlist", CATEGORY_WATCHLIST),
        ("collection", "Collection", CATEGORY_COLLECTION),
        ("history", "Watch History", CATEGORY_HISTORY),
    )

    def __init__(self, client, selected_lists: list[dict] | None = None):
        self._client = client
        self._selected_lists = list(selected_lists or [])
        self._cache: list[dict] | None = None
        self._cache_key: str = ""

    def can_write(self) -> bool:
        return bool(self._client.has_write_auth())

    def write_blocked_reason(self) -> str:
        return (
            "MDBList needs an API key or an OAuth connection before it can be "
            "written to. Add one in Connections to use it as a sync target."
        )

    def list_sources(self) -> list[dict]:
        sources = [
            {"key": key, "label": label, "category": category, "kind": "status"}
            for key, label, category in self._NATIVE_SOURCES
        ]
        sources.extend(
            {
                "key": f"list:{entry.get('id')}",
                "label": str(entry.get("name") or f"List {entry.get('id')}"),
                "category": CATEGORY_WATCHLIST,
                "kind": "list",
            }
            for entry in self._selected_lists
            if entry.get("id")
        )
        return sources

    def target_lists(self) -> list[dict]:
        """The user's own static lists — the only ones this app can write to."""
        return [
            {
                "key": f"list:{entry.get('id')}",
                "label": str(entry.get("name") or f"List {entry.get('id')}"),
            }
            for entry in self._selected_lists
            if entry.get("id")
        ]

    def search_lists(self, query: str) -> list[dict]:
        return [
            {
                "key": f"list:{entry.get('id')}",
                "label": f"{entry.get('name')} (by {entry.get('user_name') or 'unknown'})",
                "category": CATEGORY_WATCHLIST,
                "kind": "list",
            }
            for entry in (self._client.search_public_lists(query) or [])
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

    @staticmethod
    def _list_id(target_list: str):
        reference = str(target_list or "").strip()
        if not reference.startswith("list:"):
            return None
        return reference.split(":", 1)[1] or None

    def _wants_native(self, category: str, source_lists: list[str] | None) -> bool:
        """Whether this read should come from the account-level sync API.

        A selection naming only static lists must never fall back to the whole
        account, and vice versa — that would silently sync far more than asked.
        With nothing selected the account-level category is the sensible default.
        """
        selected = [str(key) for key in (source_lists or []) if str(key).strip()]
        if category == CATEGORY_HISTORY:
            # Like SIMKL, Trakt and PMDB, MDBList history is one fixed account
            # feed. Curated-list selections cannot scope it and must not turn a
            # checked History category into a silent no-op.
            return True
        if not selected:
            return True
        return category in selected

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        if category not in self.reads:
            return self._unsupported(category, "read")

        selected = [str(key) for key in (source_lists or []) if str(key).strip()]
        picked_lists = [key for key in selected if key.startswith("list:")]

        items: list[dict] = []
        seen: set[str] = set()

        if self._wants_native(category, source_lists):
            for row in self._client.get_sync_items(category) or []:
                key = item_key(row)
                if key not in seen:
                    seen.add(key)
                    items.append(row)

        # History is account-level only: a curated list carries no watch dates,
        # so folding one in would invent history that never happened.
        if category != CATEGORY_HISTORY and (picked_lists or not selected):
            for row in self._selected_items(picked_lists or None):
                key = item_key(row)
                if key not in seen:
                    seen.add(key)
                    items.append(row)
        return items

    def fetch_target(self, category: str, target_list: str = "") -> list[dict]:
        list_id = self._list_id(target_list)
        if list_id:
            return list(self._selected_items([f"list:{list_id}"]))
        return self.fetch(category, None)

    def add(
        self, category: str, items: list[dict], target_list: str = "",
        visibility: str = VISIBILITY_PRIVATE,
    ) -> dict:
        if not self.can_write():
            raise ValueError(self.write_blocked_reason())
        if category not in self.writes:
            return self._unsupported(category, "write")
        list_id = self._list_id(target_list)
        if list_id:
            if category == CATEGORY_HISTORY:
                return self._unsupported(category, "write to a named list on")
            return self._client.change_list_items(list_id, items, "add")
        return self._client.add_sync_items(category, items)

    def remove(self, category: str, items: list[dict], target_list: str = "") -> dict:
        if not self.can_write():
            raise ValueError(self.write_blocked_reason())
        if category not in self.writes:
            return self._unsupported(category, "write")
        list_id = self._list_id(target_list)
        if list_id:
            if category == CATEGORY_HISTORY:
                return self._unsupported(category, "write to a named list on")
            return self._client.change_list_items(list_id, items, "remove")
        return self._client.remove_sync_items(category, items)


class LibraryAdapter(ProviderAdapter):
    """SyncMeta's own local library, exposed as a provider.

    It is the only provider that is always writable and never rate limited, so
    it is the natural hub: point every service at it once and any other pair can
    read from it without touching a remote API again. The store keeps one entry
    per *series* with seasons inside, which is what lets SIMKL's per-season and
    AniList's per-cour entries land on the same row instead of three.
    """

    key = "library"
    label = "Library"
    reads = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)
    writes = (CATEGORY_WATCHLIST, CATEGORY_HISTORY, CATEGORY_COLLECTION)
    supports_list_selection = True
    # Named lists inside the library would be a second grouping on top of the
    # sections, with no provider on the other side able to address them.
    supports_target_lists = False
    supports_visibility = False

    def __init__(self, store):
        self._store = store

    def can_write(self) -> bool:
        return self._store is not None

    def write_blocked_reason(self) -> str:
        return "" if self.can_write() else "The local library is unavailable."

    def list_sources(self) -> list[dict]:
        return [
            {"key": "section:watchlist", "label": "Library watchlist", "category": CATEGORY_WATCHLIST},
            {"key": "section:collection", "label": "Library collection", "category": CATEGORY_COLLECTION},
            {"key": "section:history", "label": "Library watch history", "category": CATEGORY_HISTORY},
        ]

    @staticmethod
    def _section_for(category: str, source_lists: list[str] | None) -> str | None:
        """Which library section a category should read.

        A selection naming only *other* categories must read nothing rather than
        falling back to the whole category — the same rule every other adapter
        follows, and the one that stops a pair syncing far more than asked.
        """
        selected = [str(value).strip() for value in (source_lists or []) if str(value).strip()]
        if selected:
            wanted = f"section:{category}"
            return category if wanted in selected else None
        return category

    def fetch(self, category: str, source_lists: list[str] | None = None) -> list[dict]:
        if category not in self.reads or self._store is None:
            return []
        section = self._section_for(category, source_lists)
        if section is None:
            return []
        return self._store.fetch(section)

    def fetch_target(self, category: str, target_list: str = "") -> list[dict]:
        if category not in self.writes or self._store is None:
            return []
        return self._store.fetch(category)

    def add(
        self, category: str, items: list[dict], target_list: str = "",
        visibility: str = VISIBILITY_PRIVATE,
    ) -> dict:
        if self._store is None:
            return self._unsupported(category, "write")
        if category == CATEGORY_HISTORY:
            return self._store.mark_watched(items, source="pair")
        if category in (CATEGORY_WATCHLIST, CATEGORY_COLLECTION):
            return self._store.add(category, items, source="pair")
        return self._unsupported(category, "write")

    def remove(self, category: str, items: list[dict], target_list: str = "") -> dict:
        if self._store is None:
            return self._unsupported(category, "remove from")
        if category == CATEGORY_HISTORY:
            return self._store.unmark_watched(items)
        if category in (CATEGORY_WATCHLIST, CATEGORY_COLLECTION):
            return self._store.remove(category, items)
        return self._unsupported(category, "remove from")


#: Every provider, in the order the UI lists them. Library is first: it is the
#: hub the others are meant to feed, and it is the one that always works.
#:
#: This tuple is the *only* place a provider is enrolled. The order, the key
#: lookup and the labels are all derived from it, because the alternative —
#: a second hand-written mapping somewhere else — is what let `/pairs/save`
#: reject every Library pair as an "unknown provider" while the pair editor
#: happily offered Library at both ends.
_ADAPTER_CLASSES = (
    LibraryAdapter,
    TraktAdapter,
    SimklAdapter,
    AniListAdapter,
    MdbListAdapter,
    PmdbAdapter,
)

#: provider key -> adapter class. Class attributes (`reads`, `writes`,
#: `supports_target_lists`, ...) are enough to validate a pair, so callers that
#: only need capabilities do not have to build a credentialed instance.
ADAPTER_TYPES: dict[str, type[ProviderAdapter]] = {
    cls.key: cls for cls in _ADAPTER_CLASSES
}

PROVIDER_ORDER = tuple(ADAPTER_TYPES)

#: Display names, taken from the adapters so a provider is named once.
PROVIDER_LABELS = {key: cls.label for key, cls in ADAPTER_TYPES.items()}
