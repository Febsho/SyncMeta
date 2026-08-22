"""Core sync logic for syncing configured sources into PublicMetaDB."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from . import fribb_client
from .anilist_client import AniListClient
from .config import AniListConfig, AppConfig
from .matcher import ItemMatcher, MatchResult
from .mdblist_client import MdbListClient
from .publicmetadb_client import PublicMetaDBClient
from .simkl_client import SimklClient
from .trakt_client import TraktAuthenticationError, TraktClient
from . import anime_mapping_store

logger = logging.getLogger(__name__)

def _env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = str(os.getenv(name, str(default)) or str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


_LIST_WRITE_WORKERS = _env_int("SYNCMETA_LIST_WRITE_WORKERS", 2, maximum=4)
_LIST_RESOLVE_WORKERS = _env_int("SYNCMETA_LIST_RESOLVE_WORKERS", 2, maximum=6)
_ACTIVITY_WRITE_WORKERS = _env_int("SYNCMETA_ACTIVITY_WRITE_WORKERS", 1, maximum=4)
_MAPPING_WRITE_WORKERS = _env_int("SYNCMETA_MAPPING_WRITE_WORKERS", 1, maximum=4)
_SOURCE_SYNC_WORKERS = _env_int("SYNCMETA_SOURCE_SYNC_WORKERS", 3, maximum=4)
_SIMKL_FETCH_WORKERS = _env_int("SYNCMETA_SIMKL_FETCH_WORKERS", 3, maximum=8)
_ACTIVITY_SOURCE_WORKERS = _env_int("SYNCMETA_ACTIVITY_SOURCE_WORKERS", 2, maximum=3)
_PREWARM_WORKERS = _env_int("SYNCMETA_PREWARM_WORKERS", 2, maximum=4)
_ANILIST_PREWARM_LIMIT = _env_int("SYNCMETA_ANILIST_PREWARM_LIMIT", 100, minimum=0, maximum=200)
_FUTURE_POLL_INTERVAL = 0.25


def _coerce_tmdb_id(value: object, media_type: str = "") -> int | None:
    if isinstance(value, dict):
        media_key = str(media_type or "").strip().lower()
        candidates: list[str] = []
        if media_key:
            candidates.append(media_key)
        if media_key == "tv":
            candidates.extend(["show", "series"])
        elif media_key == "movie":
            candidates.append("movies")
        candidates.extend(["tv", "movie", "show", "series"])
        for key in candidates:
            if key in value:
                return _coerce_tmdb_id(value.get(key), media_type)
        return None
    try:
        if value is None or value == "":
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


class SyncCancelled(Exception):
    """Raised when a running sync has been asked to stop."""

_TYPE_LABELS = {
    "shows": "Series",
    "movies": "Movies",
    "anime": "Anime",
}

_STATUS_LABELS = {
    "watching": "Watching",
    "plantowatch": "Plan to Watch",
    "completed": "Completed",
    "hold": "On Hold",
    "dropped": "Dropped",
    "watchlist": "Watchlist",
    "CURRENT": "Watching",
    "PLANNING": "Planning",
    "COMPLETED": "Completed",
    "PAUSED": "Paused",
    "DROPPED": "Dropped",
    "COMPLETED_ONA": "Completed ONA",
    "COMPLETED_OVA": "Completed OVA",
    "COMPLETED_MOVIE": "Completed Movie",
}


def _safe_season_int(value: object) -> int | None:
    """Coerce a season/offset value to int, tolerating absent and unusable input.

    Note 0 is meaningful here (season 0 = specials), so callers must distinguish
    ``None`` from ``0`` rather than testing truthiness.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status_list_name(media_type: str, status: str) -> str:
    """Build a PMDB list name like 'Plan to Watch - Anime'."""
    type_label = _TYPE_LABELS.get(media_type, media_type.title())
    status_label = _STATUS_LABELS.get(status, status.replace("_", " ").title())
    return f"{status_label} - {type_label}"


def _display_status_name(media_type: str, status: str) -> str:
    type_label = _TYPE_LABELS.get(media_type, media_type.title())
    status_label = _STATUS_LABELS.get(status, status.replace("_", " ").title())
    return f"{status_label} - {type_label}"


@dataclass
class SyncStats:
    """Counters for a single list sync."""

    list_name: str = ""
    display_name: str = ""
    source_name: str = ""
    row_key: str = ""
    row_type: str = "status_list"
    items_fetched: int = 0
    items_resolved: int = 0
    items_added: int = 0
    items_removed: int = 0
    items_skipped_duplicate: int = 0
    items_skipped_unresolved: int = 0
    items_skipped_fingerprint: int = 0
    # Episodes written by the Trakt watched-state reconciliation pass — plays
    # the paginated history log never reported, so they are gap-fills rather
    # than new activity. Counted separately so the dashboard can say so.
    items_reconciled: int = 0
    errors: list[str] = field(default_factory=list)
    history_cursor: str = ""
    activities_ts: str = ""  # last-seen source activities timestamp (for freshness skip)
    unresolved_items: list[dict] = field(default_factory=list)
    phase_timings: dict[str, float] = field(default_factory=dict)
    match_breakdown: dict[str, int] = field(default_factory=dict)
    unresolved_reason_counts: dict[str, int] = field(default_factory=dict)
    error_stage_counts: dict[str, int] = field(default_factory=dict)
    pmdb_metrics: dict[str, int] = field(default_factory=dict)
    sample_failed_titles: list[str] = field(default_factory=list)
    sample_unresolved_titles: list[str] = field(default_factory=list)
    # Populated only on dry-run list syncs. Each entry is a compact record of a
    # resolved item so the dashboard can show a preview of what would be added.
    dry_run_preview: list[dict] = field(default_factory=list)
    # Keys (tmdb_id:media_type) that this sync wrote to the list.  Only populated
    # for the PMDB Watchlist row so the profile store can persist them and use them
    # on the next run to distinguish SyncMeta-managed entries from manually-added ones.
    synced_keys: list[str] = field(default_factory=list)


# Cap preview items per list to keep the dry-run payload bounded even for
# profiles that have hundreds of items across many lists.
_DRY_RUN_PREVIEW_LIMIT = 50
_WATCHED_HISTORY_VERIFY_RETRIES = 3
_WATCHED_HISTORY_VERIFY_DELAY_SECONDS = 0.75
_SAMPLE_TITLES_LIMIT = 5


def _unresolved_item_summary(item: dict, list_name: str = "", unresolved_reason: str = "") -> dict:
    """Extract a compact, serialisable record for an item that could not be resolved."""
    ids = item.get("ids") or {}
    title_variants = item.get("title_variants")
    summary = {
        "title": item.get("title") or "Unknown",
        # Carried through so a later manual/auto retry keeps the same widened
        # title comparison the original resolve attempt had.
        "title_variants": [str(v) for v in title_variants if str(v or "").strip()]
        if isinstance(title_variants, (list, tuple)) else [],
        "year": item.get("year"),
        "media_type": item.get("media_type"),
        "simkl_type": item.get("simkl_type"),
        "tmdb_id": item.get("tmdb_id"),
        "imdb_id": item.get("imdb_id") or ids.get("imdb"),
        "mal_id": item.get("mal_id") or ids.get("mal"),
        "anilist_id": item.get("anilist_id") or ids.get("anilist"),
        "root_mal_id": item.get("root_mal_id") or ids.get("root_mal"),
        "root_anilist_id": item.get("root_anilist_id") or ids.get("root_anilist"),
        "anidb_id": item.get("anidb_id") or ids.get("anidb"),
        "tvdb_id": item.get("tvdb_id") or ids.get("tvdb"),
        "simkl_id": ids.get("simkl"),
        "list_name": list_name,
        "cache_key": ItemMatcher._cache_key(item),
        "anime_resolve_mode": str(item.get("anime_resolve_mode", "") or "").strip(),
        "anime_identity": dict(item.get("anime_identity", {})) if isinstance(item.get("anime_identity"), dict) else None,
        "match_confidence": str(item.get("match_confidence") or "").strip() or None,
        "anime_mapping_source": str(item.get("anime_mapping_source") or "").strip() or None,
        "candidate_tmdb_id": item.get("candidate_tmdb_id"),
    }
    if unresolved_reason:
        summary["unresolved_reason"] = unresolved_reason
    if item.get("simkl_type") == "anime":
        summary.update({
            "root_episode_offset": item.get("root_episode_offset") or 0,
            "has_root_ids": bool(item.get("root_anilist_id") or item.get("root_mal_id") or ids.get("root_anilist") or ids.get("root_mal")),
            "has_anime_ids": bool(item.get("anilist_id") or item.get("mal_id") or ids.get("anilist") or ids.get("mal")),
            "anime_conflict_reason": _anime_conflict_reason(item, unresolved_reason),
        })
    return summary


def _anime_conflict_reason(item: dict, unresolved_reason: str = "") -> str:
    if str(item.get("simkl_type", "")).strip().lower() != "anime":
        return ""
    media_type = str(item.get("media_type", "")).strip().lower()
    if unresolved_reason == "lookup_unavailable":
        return "pmdb external mismatch"
    if unresolved_reason == "missing_ids":
        return "missing fribb mapping"
    if media_type == "movie" and any(item.get(key) for key in ("root_anilist_id", "root_mal_id")):
        return "movie vs tv mismatch"
    if int(item.get("root_episode_offset") or 0) > 0:
        return "season split mismatch"
    if any(item.get(key) for key in ("root_anilist_id", "root_mal_id")):
        return "root collision"
    return "missing fribb mapping"


def _stable_item_identity(item: dict) -> dict:
    ids = item.get("ids") or {}
    return {
        "title": str(item.get("title") or "").strip(),
        "year": item.get("year"),
        "media_type": str(item.get("media_type") or "").strip().lower(),
        "simkl_type": str(item.get("simkl_type") or "").strip().lower(),
        "tmdb_id": item.get("tmdb_id"),
        "imdb_id": item.get("imdb_id") or ids.get("imdb"),
        "mal_id": item.get("mal_id") or ids.get("mal"),
        "anilist_id": item.get("anilist_id") or ids.get("anilist"),
        "tvdb_id": item.get("tvdb_id") or ids.get("tvdb"),
        "simkl_id": ids.get("simkl"),
        "status": str(item.get("status") or "").strip().lower(),
    }


class SyncService:
    """Orchestrates one-way sync into PublicMetaDB."""

    _shared_cache_lock = threading.Lock()
    _shared_fribb_lookup_cache: dict[tuple[str, str], dict | None] = {}
    _shared_anime_seasons_cache: dict[int, list[dict]] = {}
    _shared_anime_history_remap_cache: dict[tuple, dict | None] = {}

    def __init__(
        self,
        config: AppConfig,
        status_callback=None,
        progress_callback=None,
        managed_lists: list[dict] | None = None,
        cancel_requested_callback=None,
        sync_modes: dict | None = None,
        resolution_cache: dict | None = None,
        failed_resolution_cache: dict | None = None,
        manual_list_additions: dict | None = None,
        list_state: dict | None = None,
        trakt_token_refreshed_callback=None,
    ):
        self._config = config
        self._simkl = SimklClient(config.simkl, cancel_requested_callback=cancel_requested_callback)
        self._trakt = TraktClient(
            config.trakt,
            cancel_requested_callback=cancel_requested_callback,
            token_refreshed_callback=trakt_token_refreshed_callback,
        )
        self._mdblist = MdbListClient(config.mdblist, cancel_requested_callback=cancel_requested_callback)
        self._pmdb = PublicMetaDBClient(config.pmdb, cancel_requested_callback=cancel_requested_callback)
        self._anilist_root_client = AniListClient(
            config.anilist if config.anilist.enabled else AniListConfig(),
            cancel_requested_callback=cancel_requested_callback,
        )
        self._matcher = ItemMatcher(
            self._pmdb,
            anime_root_resolver=self._make_anime_root_resolver(),
            initial_cache=resolution_cache,
            initial_failed_cache=failed_resolution_cache,
        )
        self._status_callback = status_callback
        self._progress_callback = progress_callback
        self._managed_lists = self._normalize_managed_lists(managed_lists)
        self._managed_lists_lock = threading.Lock()
        self._cancel_requested_callback = cancel_requested_callback
        self._sync_modes = self._normalize_sync_modes(sync_modes, config)
        self._last_progress_publish = 0.0
        self._live_progress_rows: dict[str, dict] = {}
        self._mapping_contribution_lock = threading.Lock()
        self._contributed_mapping_keys: set[tuple[int, str, str, str]] = set()
        self._fribb_lookup_cache = self.__class__._shared_fribb_lookup_cache
        self._anime_seasons_cache = self.__class__._shared_anime_seasons_cache
        self._manual_list_additions: dict[str, list[dict]] = manual_list_additions or {}
        self._anime_history_remap_cache = self.__class__._shared_anime_history_remap_cache
        self._pmdb_cache_lock = threading.Lock()
        self._pmdb_run_list_index: dict[str, dict] | None = None
        self._pmdb_list_items_cache: dict[str, list[dict]] = {}
        self._list_state: dict[str, dict] = {
            str(key): dict(value)
            for key, value in (list_state or {}).items()
            if key and isinstance(value, dict)
        }
        self._current_source_activities: dict[str, str] = {}

    def _make_anime_root_resolver(self):
        """Return a callable used by ItemMatcher to lazily walk AniList prequel chains."""
        client = self._anilist_root_client

        def resolver(anilist_id: int | None, mal_id: int | None) -> dict | None:
            if not anilist_id and mal_id:
                anilist_id = client.get_anilist_id_by_mal(mal_id)
            if not anilist_id:
                return None
            get_ctx = getattr(client, "_get_root_context", None)
            if callable(get_ctx):
                return get_ctx(anilist_id)
            return None

        return resolver

    @property
    def list_state(self) -> dict[str, dict]:
        return {
            str(key): dict(value)
            for key, value in self._list_state.items()
            if key and isinstance(value, dict)
        }

    @staticmethod
    def _make_row_key(source_name: str, list_name: str, row_type: str, selection: dict | None = None) -> str:
        source = str(source_name or "").strip().lower() or "unknown"
        list_part = str(list_name or "").strip().lower()
        selection_part = ""
        if isinstance(selection, dict) and selection:
            selection_part = json.dumps(selection, sort_keys=True, separators=(",", ":"))
        payload = "|".join([source, row_type, list_part, selection_part])
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _infer_row_type(source_name: str, selection: dict | None = None, display_name: str = "") -> str:
        source = str((selection or {}).get("source") or source_name or "").strip().lower()
        kind = str((selection or {}).get("kind") or "").strip().lower()
        display = str(display_name or "").strip().lower()
        if display in {"watch history", "simkl watch history", "trakt watch history"}:
            return "history"
        if display in {"resume progress", "trakt resume progress", "simkl resume progress"}:
            return "resume"
        if display == "pmdb watchlist":
            return "core_rule"
        if source == "trakt" and kind in {"watchlist"}:
            return "core_rule"
        if source in {"trakt", "mdblist"} and kind in {"selected-list", "default", "liked-auto"}:
            return "catalog_import"
        if source == "mdblist":
            return "catalog_import"
        return "status_list"

    @staticmethod
    def _is_watched_up(item: dict) -> bool:
        """Return True if the user has watched all aired episodes of this show.

        SIMKL keeps shows as 'watching' even when all aired episodes are done
        (waiting for a future season).  This detects that state so callers can
        exclude these from the active watching list.
        """
        watched = int(item.get("watched_episodes_count") or 0)
        total = int(item.get("total_episodes_count") or 0)
        if watched <= 0 or total <= 0:
            return False
        return watched >= total

    @staticmethod
    def _compute_source_fingerprint(source_items: list[dict], activities_ts: str = "") -> str:
        normalized = [
            _stable_item_identity(item)
            for item in source_items or []
            if isinstance(item, dict)
        ]
        normalized.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        payload = {
            "activities_ts": str(activities_ts or "").strip(),
            "count": len(normalized),
            "items": normalized,
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _append_unique_sample(target: list[str], value: str, limit: int = _SAMPLE_TITLES_LIMIT) -> None:
        text = str(value or "").strip()
        if not text or text in target or len(target) >= limit:
            return
        target.append(text)

    def _record_error(self, stats: SyncStats, stage: str, message: str, item_title: str = "") -> None:
        stats.errors.append(str(message))
        stage_key = str(stage or "unknown").strip().lower() or "unknown"
        stats.error_stage_counts[stage_key] = int(stats.error_stage_counts.get(stage_key, 0)) + 1
        if item_title:
            self._append_unique_sample(stats.sample_failed_titles, item_title)

    def _remember_list_state(
        self,
        stats: SyncStats,
        source_fingerprint: str,
        desired_keys: set[str] | None = None,
    ) -> None:
        if not stats.row_key or self._config.sync.dry_run:
            return
        next_state = {
            "fingerprint": str(source_fingerprint or "").strip(),
            "activities_ts": str(stats.activities_ts or "").strip(),
            "updated_at": str(time.time()),
            "item_count": int(stats.items_fetched or 0),
            "last_resolved_count": int(stats.items_resolved or 0),
        }
        if desired_keys:
            next_state["write_keys"] = sorted(str(key) for key in desired_keys if key)
        self._list_state[stats.row_key] = next_state

    @property
    def resolution_cache(self) -> dict[str, int]:
        return self._matcher.resolution_cache

    @property
    def failed_resolution_cache(self) -> dict[str, str]:
        return self._matcher.failed_resolution_cache

    @property
    def simkl(self) -> SimklClient:
        return self._simkl

    @property
    def managed_lists(self) -> list[dict]:
        return [dict(item) for item in sorted(self._managed_lists.values(), key=lambda item: item["list_name"])]

    def _resolve_match(self, item: dict) -> "MatchResult":
        from .matcher import MatchResult
        if hasattr(self._matcher, "resolve_match"):
            result = self._matcher.resolve_match(item)
        else:
            tmdb_id = self._matcher.resolve_tmdb_id(item)
            if tmdb_id is not None:
                result = MatchResult(tmdb_id=tmdb_id, resolution_kind="external_mapping")
            else:
                result = MatchResult(tmdb_id=None, resolution_kind="unresolved")

        # For anime items, Fribb's per-show AniList→TMDB mapping is more accurate
        # than PMDB community data, which can collapse sequels into the franchise
        # root (e.g. PMDB maps Fate/Zero's AniList ID to Fate/stay night TMDB).
        # Fribb maintains correct per-show entries (Fate/Zero → 44382, not 30887).
        # Always prefer Fribb when it has an entry that disagrees with PMDB.
        #
        # Exception: if PMDB used root_series AND Fribb has no entry, accept PMDB
        # (root is better than nothing).  If Fribb has no entry and PMDB is
        # unconfirmed (0-vote / ambiguous), log a warning — the result may be wrong.
        if (
            str(item.get("simkl_type", "")).strip().lower() == "anime"
            and result.resolution_kind in ("external_mapping", "root_series")
            and result.tmdb_id is not None
            and result.anime_mapping_source not in ("fribb_exact", "fribb_fallback")
        ):
            fribb_tmdb = self._resolve_tmdb_id_via_fribb(item)
            if fribb_tmdb is None:
                # Fribb has no entry at all.  Log severity depends on confidence.
                if result.match_confidence != "verified":
                    logger.warning(
                        "[resolve-post] anime '%s' — PMDB %s returned unconfirmed"
                        " tmdb=%d (confidence=%s) and Fribb has no entry;"
                        " result may be wrong (anilist=%s mal=%s)",
                        item.get("title"),
                        result.resolution_kind,
                        result.tmdb_id,
                        result.match_confidence,
                        item.get("anilist_id") or (item.get("ids") or {}).get("anilist"),
                        item.get("mal_id") or (item.get("ids") or {}).get("mal"),
                    )
                else:
                    logger.debug(
                        "[resolve-post] anime '%s' — Fribb has no entry;"
                        " keeping PMDB tmdb=%d (verified)",
                        item.get("title"), result.tmdb_id,
                    )
            elif fribb_tmdb != result.tmdb_id:
                logger.info(
                    "[resolve-post] anime '%s' — Fribb tmdb=%d overrides"
                    " PMDB tmdb=%d (kind=%s confidence=%s)",
                    item.get("title"), fribb_tmdb, result.tmdb_id,
                    result.resolution_kind, result.match_confidence,
                )
                return MatchResult(
                    tmdb_id=fribb_tmdb,
                    resolution_kind="fribb_exact",
                    match_confidence="exact",
                    anime_mapping_source="fribb_exact",
                    candidate_tmdb_id=result.tmdb_id,
                )
        return result

    def _resolve_tmdb_id_via_fribb(self, item: dict) -> int | None:
        """Return the direct-entry TMDB ID from Fribb for this anime item.

        For list identity we must not fall back to franchise-root AniList/MAL
        IDs here. Otherwise sequel entries like Naruto Shippuden, Boruto, or
        Fate variants can be "corrected" back onto the root series and then get
        deduped away in PMDB.

        Type-guard: if Fribb classifies the entry as "movie" but the item's
        media_type is "tv" (or vice versa), we refuse to override the PMDB
        external-mapping result.  Applying a type-mismatched Fribb TMDB ID
        would push the wrong entry into the PMDB list with the wrong type.
        """
        try:
            fribb = self._lookup_fribb_entry(item, allow_root_fallback=False)
            if not fribb:
                return None
            raw = fribb.get("themoviedb_id") or fribb.get("themoviedb")
            if not raw:
                return None
            tmdb_id, mapped_media_type = fribb_client.extract_tmdb(raw)
            if not tmdb_id or tmdb_id <= 0:
                return None

            # Validate namespace consistency before returning the override.  The
            # key the mapping is stored under ("tv"/"movie") is authoritative;
            # the entry's `type` field is not, since it routinely disagrees with
            # the TMDB namespace for films, OVAs and specials.
            item_media_type = str(item.get("media_type") or "").strip().lower()
            if mapped_media_type and item_media_type and item_media_type != mapped_media_type:
                logger.debug(
                    "Fribb namespace mismatch for %r — mapping is %r but item has"
                    " media_type=%r; skipping Fribb override (tmdb=%s)",
                    item.get("title"), mapped_media_type, item_media_type, tmdb_id,
                )
                return None

            return tmdb_id
        except Exception:
            pass
        return None

    def run(self) -> list[SyncStats]:
        """Execute a full sync cycle. Returns stats per synced list."""
        self._check_cancelled()
        self._set_status("Preparing sync")
        flags = []
        if self._config.sync.dry_run:
            flags.append("DRY RUN")
        if self._config.sync.remove_missing:
            flags.append("remove_missing")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        logger.info("═" * 60)
        logger.info("▶  SYNC STARTED%s", flag_str)
        logger.info("   modes: %s", ", ".join(k for k, v in self._sync_modes.items() if v) or "none")
        logger.info("═" * 60)

        # ── Phase 0: Source freshness check ───────────────────────────────────
        # Fetch each source's last-activities timestamp (one cheap API call per
        # source).  If it matches the stored value from the previous sync, that
        # source hasn't changed at all — skip it entirely.  On first run the
        # stored value is "" so the check always falls through.
        # full_history_sync overrides the skip for history.
        force = self._config.sync.full_history_sync
        simkl_ts = self._fetch_simkl_activities_ts()
        trakt_ts = self._fetch_trakt_activities_ts()
        self._current_source_activities = {
            "simkl": simkl_ts,
            "trakt": trakt_ts,
        }
        simkl_unchanged = bool(
            not force
            and simkl_ts
            and simkl_ts == self._config.sync.simkl_activities_ts
        )
        trakt_unchanged = bool(
            not force
            and trakt_ts
            and trakt_ts == self._config.sync.trakt_activities_ts
        )
        if simkl_unchanged:
            logger.info("SIMKL unchanged since last sync — skipping SIMKL sources")
        if trakt_unchanged:
            logger.info("Trakt unchanged since last sync — skipping Trakt sources")

        all_stats: list[SyncStats] = []
        if self._sync_modes["lists"]:
            logger.info("── List Sync ──────────────────────────────────────────")
            self._prime_pmdb_list_index()
            anilist_enabled = self._config.anilist.enabled and bool(self._config.anilist.username)
            if anilist_enabled and not self._config.anilist.selected_statuses:
                # User connected AniList but didn't pick statuses — use defaults.
                self._config.anilist.selected_statuses = ["CURRENT", "PLANNING", "COMPLETED"]
                logger.info("AniList: no statuses selected, using defaults: %s", self._config.anilist.selected_statuses)
            source_jobs: list[tuple[str, object]] = []
            if not simkl_unchanged:
                source_jobs.append(("SIMKL", self._sync_simkl))
            if anilist_enabled:
                source_jobs.append(("AniList", self._sync_anilist))
            if self._config.trakt.enabled and not trakt_unchanged:
                source_jobs.append(("Trakt", self._sync_trakt))
            if self._config.mdblist.enabled:
                source_jobs.append(("MDBList", self._sync_mdblist))

            if source_jobs:
                cancelled = False
                pool = ThreadPoolExecutor(max_workers=min(_SOURCE_SYNC_WORKERS, len(source_jobs)))
                shutdown_wait = True
                from . import log_capture as _lc
                _parent_profile = _lc.get_profile_context()
                def _wrap(fn):
                    def _inner():
                        _lc.set_profile_context(_parent_profile)
                        return fn()
                    return _inner
                try:
                    futures = {
                        pool.submit(_wrap(fn)): name
                        for name, fn in source_jobs
                    }
                    for future in self._iter_completed_futures(futures):
                        source_name = futures[future]
                        try:
                            result = future.result()
                            all_stats.extend(result)
                            logger.info("%s sync completed: %d list(s)", source_name, len(result))
                        except SyncCancelled:
                            cancelled = True
                        except Exception as exc:
                            logger.error("%s sync failed: %s", source_name, exc, exc_info=True)
                        self._publish_progress(all_stats, force=True)
                except SyncCancelled:
                    shutdown_wait = False
                    raise
                finally:
                    pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)
                if cancelled:
                    raise SyncCancelled("Sync stopped by user")

        if self._sync_modes["lists"] and any([
            self._config.sync.simkl_sync_to_pmdb_watchlist,
            self._config.sync.trakt_sync_to_pmdb_watchlist,
            self._config.sync.anilist_sync_to_pmdb_watchlist,
        ]):
            logger.info("── PMDB Watchlist Sync ────────────────────────────────")
            wl_stats = self._sync_pmdb_watchlist()
            if wl_stats:
                all_stats.append(wl_stats)
                self._publish_progress(all_stats, force=True)

        if self._sync_modes["history"] or self._sync_modes["resume"]:
            logger.info("── Watch History / Resume ─────────────────────────────")
            activity_rows: list[SyncStats] = []
            if self._config.simkl.access_token and not simkl_unchanged:
                activity_rows.extend(self._sync_simkl_activity())
            elif self._config.simkl.access_token and simkl_unchanged:
                logger.info("SIMKL history/resume skipped — source unchanged")
            if self._config.trakt.enabled:
                # Trakt history uses its own cursor; skip flag still applies to
                # resume progress (no cursor there).
                activity_rows.extend(self._sync_trakt_activity(trakt_unchanged=trakt_unchanged))
            # Stamp freshness timestamps before merging so profile_store saves them.
            for row in activity_rows:
                if not row.activities_ts:
                    src = row.source_name.lower()
                    if "simkl" in src and simkl_ts:
                        row.activities_ts = simkl_ts
                    elif "trakt" in src and trakt_ts:
                        row.activities_ts = trakt_ts
            all_stats.extend(self._merge_activity_stats(activity_rows))
            self._publish_progress(all_stats, force=True)

        if self._sync_modes["lists"] and not self._config.sync.dry_run and self._config.sync.delete_disabled_lists:
            desired_names = {stats.list_name for stats in all_stats if stats.list_name}
            self._delete_disabled_lists(desired_names)

        # Stamp freshness timestamps on all stats rows so profile_store can
        # persist them even if no activity (history/resume) rows were emitted.
        for row in all_stats:
            if not row.activities_ts:
                src = row.source_name.lower()
                if "simkl" in src and simkl_ts:
                    row.activities_ts = simkl_ts
                elif "trakt" in src and trakt_ts:
                    row.activities_ts = trakt_ts

        self._set_status("Finalizing sync results")
        self._log_results(all_stats)
        return all_stats

    def _sync_pmdb_watchlist(self) -> SyncStats | None:
        """Merge selected watchlist items from enabled providers into the PMDB native watchlist."""
        # Collect which sources are active, then fetch them concurrently.
        fetch_jobs: list[tuple[str, object]] = []
        simkl_enabled = bool(self._config.sync.simkl_sync_to_pmdb_watchlist and self._config.simkl.access_token)
        trakt_enabled = bool(self._config.sync.trakt_sync_to_pmdb_watchlist and self._config.trakt.enabled)
        anilist_enabled = bool(self._config.sync.anilist_sync_to_pmdb_watchlist and self._config.anilist.enabled)

        def _fetch_simkl_plantowatch() -> list[dict]:
            result: list[dict] = []
            for simkl_type in self._config.sync.media_types:
                grouped = self._simkl.get_status("plantowatch", [simkl_type])
                result.extend(grouped.get(simkl_type, []))
            return result

        workers = sum([simkl_enabled, trakt_enabled, anilist_enabled])
        selection = {"source": "pmdb", "kind": "watchlist-merge"}
        all_items: list[dict] = []
        try:
            if workers == 0:
                pass
            elif workers == 1:
                if simkl_enabled:
                    all_items.extend(_fetch_simkl_plantowatch())
                elif trakt_enabled:
                    all_items.extend(self._trakt.get_watchlist() or [])
                else:
                    all_items.extend(self._anilist_root_client.get_status("PLANNING") or [])
            else:
                self._set_status("Fetching watchlist sources")
                with ThreadPoolExecutor(max_workers=min(_SOURCE_SYNC_WORKERS, workers)) as pool:
                    futures = []
                    if simkl_enabled:
                        futures.append(pool.submit(_fetch_simkl_plantowatch))
                    if trakt_enabled:
                        futures.append(pool.submit(self._trakt.get_watchlist))
                    if anilist_enabled:
                        futures.append(pool.submit(self._anilist_root_client.get_status, "PLANNING"))
                    for future in futures:
                        result = future.result()
                        all_items.extend(result or [])
        except SyncCancelled:
            raise
        except Exception as exc:
            # A failed source read means the merged view is incomplete; running
            # the merge anyway would remove every item that source contributed
            # (force_remove_missing=True). Skip the merge and report the error
            # instead of aborting the whole sync run.
            row_type = self._infer_row_type("Combined", selection, "PMDB Watchlist")
            stats = SyncStats(
                list_name="Watchlist",
                display_name="PMDB Watchlist",
                source_name="Combined",
                row_key=self._make_row_key("Combined", "Watchlist", row_type, selection),
                row_type=row_type,
            )
            self._record_error(
                stats,
                "fetch",
                f"Watchlist source fetch failed, PMDB watchlist left untouched: {exc}",
            )
            logger.error("PMDB watchlist merge skipped — source fetch failed: %s", exc)
            return stats

        return self._sync_list(
            all_items,
            "Watchlist",
            "SyncMeta combined watchlist",
            display_name="PMDB Watchlist",
            source_name="Combined",
            is_public=False,
            list_type="watchlist",
            force_remove_missing=True,
            allow_empty_sync=True,
            managed_keys=frozenset(self._config.sync.pmdb_watchlist_managed_keys),
            selection=selection,
        )

    def _sync_simkl(self) -> list[SyncStats]:
        """Sync all configured SIMKL lists."""
        media_types = list(self._config.sync.media_types)

        stats: list[SyncStats] = []
        if not media_types:
            return stats

        # Build all (type, status) fetch jobs in the canonical order so that
        # list processing later preserves the expected ordering.
        fetch_jobs: list[tuple[str, str]] = []
        for simkl_type in media_types:
            statuses = self._config.simkl.selected_statuses.get(simkl_type, [])
            fetch_jobs.extend((simkl_type, sk) for sk in statuses)

        if not fetch_jobs:
            return stats

        self._set_status("Fetching SIMKL lists")
        simkl_fetch_started = time.perf_counter()

        def _fetch_one(simkl_type: str, status_key: str) -> tuple[str, str, list[dict]]:
            t0 = time.perf_counter()
            grouped = self._simkl.get_status(status_key, [simkl_type])
            items = grouped.get(simkl_type, [])
            logger.info(
                "Fetched SIMKL %s %s in %.2fs (%d items)",
                _STATUS_LABELS.get(status_key, status_key),
                simkl_type,
                time.perf_counter() - t0,
                len(items),
            )
            return (simkl_type, status_key, items)

        job_order = {job: i for i, job in enumerate(fetch_jobs)}
        all_items_by_status: list[tuple[str, str, list[dict]]] = []
        with ThreadPoolExecutor(max_workers=min(_SIMKL_FETCH_WORKERS, len(fetch_jobs))) as pool:
            futures = {pool.submit(_fetch_one, t, s): (t, s) for t, s in fetch_jobs}
            for future in self._iter_completed_futures(futures):
                self._check_cancelled()
                try:
                    all_items_by_status.append(future.result())
                except SyncCancelled:
                    raise
                except Exception as exc:
                    t, s = futures[future]
                    logger.error("Failed to fetch SIMKL %s %s: %s", t, s, exc)

        all_items_by_status.sort(key=lambda x: job_order.get((x[0], x[1]), 9999))
        logger.info(
            "Fetched SIMKL selections in %.2fs (%d buckets)",
            time.perf_counter() - simkl_fetch_started,
            len(all_items_by_status),
        )

        anime_prewarm_ids = sorted({
            int(item["anilist_id"])
            for simkl_type, _, items in all_items_by_status
            if simkl_type == "anime"
            for item in items
            if (
                item.get("anilist_id")
                and str(item.get("media_type", "")).strip().lower() == "tv"
                and not item.get("root_anilist_id")
            )
        })
        if anime_prewarm_ids:
            self._set_status("Pre-warming SIMKL anime metadata cache")
            self._prewarm_simkl_anime_ids(anime_prewarm_ids[:_ANILIST_PREWARM_LIMIT])

        all_anime_items = [
            item
            for simkl_type, _, items in all_items_by_status
            if simkl_type == "anime"
            for item in items
        ]
        if all_anime_items and hasattr(self._matcher, "preseed_anime_from_fribb"):
            self._matcher.preseed_anime_from_fribb(all_anime_items)

        for simkl_type, status_key, items in all_items_by_status:
            self._check_cancelled()
            # Filter out "watching" shows where the user has watched all aired
            # episodes — SIMKL keeps these as "watching" because a new season
            # might air, but they clutter the active watchlist in PMDB.
            if (
                self._config.sync.simkl_skip_watched_up_shows
                and status_key == "watching"
                and simkl_type in ("shows", "anime")
            ):
                before = len(items)
                items = [
                    item for item in items
                    if not self._is_watched_up(item)
                ]
                skipped = before - len(items)
                if skipped:
                    logger.info(
                        "Skipped %d fully-watched-up %s from SIMKL Watching list",
                        skipped, simkl_type,
                    )
            name = _status_list_name(simkl_type, status_key)
            display_name = _display_status_name(simkl_type, status_key)
            description = (
                f"Auto-synced '{_STATUS_LABELS.get(status_key, status_key)}' "
                f"{_TYPE_LABELS.get(simkl_type, simkl_type)} from SIMKL"
            )

            self._publish_pending_list(name, display_name, "SIMKL")

            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=display_name,
                    source_name="SIMKL",
                    activities_ts=self._current_source_activities.get("simkl", ""),
                    is_public=self._config.sync.simkl_visibility == "public",
                    force_remove_missing=(simkl_type == "anime"),
                    selection={
                        "source": "simkl",
                        "media_type": simkl_type,
                        "status": status_key,
                    },
                )
            )

        return stats

    def _sync_trakt_activity(self, trakt_unchanged: bool = False) -> list[SyncStats]:
        stats: list[SyncStats] = []

        if self._config.sync.trakt_sync_watched_history:
            stats.append(self._sync_trakt_watched_history())

        # Resume progress has no cursor; skip when Trakt unchanged.
        if self._config.sync.trakt_sync_resume_progress and not trakt_unchanged:
            stats.append(self._sync_trakt_resume_progress())
        elif self._config.sync.trakt_sync_resume_progress and trakt_unchanged:
            logger.info("Trakt resume progress skipped — source unchanged")

        return stats

    def _sync_simkl_activity(self) -> list[SyncStats]:
        stats: list[SyncStats] = []

        if self._config.sync.simkl_sync_watched_history:
            stats.append(self._sync_simkl_watched_history())

        return stats

    def _sync_simkl_watched_history(self) -> SyncStats:
        stats = SyncStats(
            list_name="",
            display_name="SIMKL Watch History",
            source_name="SIMKL",
            row_key=self._make_row_key("simkl", "watch_history", "history", {"mode": "history"}),
            row_type="history",
        )
        self._set_status("Fetching SIMKL watched history")
        full_sync = self._config.sync.full_history_sync
        cursor = str(self._config.sync.simkl_history_cursor or "").strip()
        since = None if full_sync or not cursor else cursor
        # Overlap three independent fetches: SIMKL history, PMDB history, and
        # the SIMKL completed-anime list (used as episode-level fallback later).
        # The fallback scans a whole status list, so skip it for cursor-based
        # incremental runs where the changed history rows are enough context.
        with ThreadPoolExecutor(max_workers=min(_ACTIVITY_SOURCE_WORKERS, 3)) as pool:
            f_simkl = pool.submit(self._simkl.get_watched_history, since=since)
            f_pmdb = pool.submit(self._pmdb.get_watched_history)
            f_completed = (
                pool.submit(self._fetch_simkl_completed_anime)
                if full_sync or not cursor
                else pool.submit(lambda: [])
            )
            items = f_simkl.result()
            try:
                existing_items = f_pmdb.result()
            except Exception as exc:
                self._record_error(stats, "pmdb_read", f"Failed to load PublicMetaDB watched history: {exc}")
                return stats
            completed_anime = f_completed.result()

        logger.info("  SIMKL completed anime: %d entries", len(completed_anime))
        if self._config.sync.simkl_history_anime_only:
            items = [item for item in items if str(item.get("simkl_type", "")).strip().lower() == "anime"]
        items = self._expand_simkl_aggregate_history(items)
        stats.history_cursor = self._latest_history_cursor(items, cursor)
        stats.items_fetched = len(items)

        existing_counts: dict[str, int] = {}
        for existing_item in existing_items:
            key = self._watched_identity_key(existing_item)
            if key:
                existing_counts[key] = existing_counts.get(key, 0) + 1

        # Track which completed anime already have per-episode records so the
        # season-level fallback pass can skip them (avoids double-counting).
        shows_with_episode_records: set[str] = set()

        # Count how many times each key appears in source (multi-watch support).
        source_seen: dict[str, int] = {}
        pending_items: list[dict] = []
        total = len(items)
        for idx, item in enumerate(items):
            self._check_cancelled()
            if idx % 50 == 0:
                self._set_status(f"Resolving SIMKL history ({idx}/{total})")
            if self._fast_skip_existing_history_item(item, existing_counts, source_seen, stats):
                continue
            item = self._resolve_activity_item(item)
            item = self._remap_simkl_anime_history_item(item)

            key = self._watched_identity_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            stats.items_resolved += 1

            # Record that this show has per-episode data so the season-level
            # fallback pass won't also mark it (which would double-count).
            if str(item.get("simkl_type", "")).strip().lower() == "anime":
                for id_key in ("anilist_id", "root_anilist_id", "mal_id", "root_mal_id"):
                    val = item.get(id_key)
                    if val:
                        shows_with_episode_records.add(f"{id_key}:{val}")

            self._increment_history_source_seen(source_seen, key)
            # Only add if PMDB has fewer plays than source for this key.
            if existing_counts.get(key, 0) >= source_seen[key]:
                stats.items_skipped_duplicate += 1
                continue
            if self._config.sync.dry_run:
                existing_counts[key] = existing_counts.get(key, 0) + 1
                stats.items_added += 1
                continue
            pending_items.append(item)

        self._write_watched_history_items(
            pending_items,
            existing_counts,
            stats,
            status_message="Writing SIMKL history to PublicMetaDB",
            total_source=total,
        )

        # Season-level fallback: only for completed anime that produced zero
        # per-episode records (e.g. user marked complete without tracking each
        # episode in SIMKL).  Shows that already have episode records are
        # skipped to prevent PMDB double-counting.
        self._sync_completed_anime_seasons(stats, existing_counts, completed_anime, shows_with_episode_records)
        return stats

    def _fetch_simkl_completed_anime(self) -> list[dict]:
        """Return all TV anime entries from the user's SIMKL completed list."""
        try:
            grouped = self._simkl.get_status("completed", ["anime"])
            return [
                item for item in grouped.get("anime", [])
                if item.get("media_type") != "movie"
            ]
        except Exception as exc:
            logger.warning("Could not fetch SIMKL completed anime: %s", exc)
            return []

    def _sync_completed_anime_seasons(
        self,
        stats: SyncStats,
        existing_counts: dict[str, int],
        completed_anime: list[dict],
        shows_with_episode_records: set[str],
    ) -> None:
        """Season-level fallback for completed anime with no per-episode history.

        Only fires for shows where SIMKL returned zero individual episode records.
        Shows that already have episode-level entries are skipped entirely to
        prevent PMDB from double-counting plays.
        """
        fallback_count = 0
        skipped_has_episodes = 0
        skipped_without_completed_evidence = 0
        pending_items: list[dict] = []
        for item in completed_anime:
            self._check_cancelled()

            if not self._has_completed_anime_evidence(item):
                skipped_without_completed_evidence += 1
                continue

            # Skip shows that already have per-episode records from the history
            # pass — adding a season mark on top would double-count in PMDB.
            item_has_records = any(
                f"{id_key}:{item.get(id_key)}" in shows_with_episode_records
                for id_key in ("anilist_id", "root_anilist_id", "mal_id", "root_mal_id")
                if item.get(id_key)
            )
            if item_has_records:
                skipped_has_episodes += 1
                continue

            resolved = self._resolve_activity_item(item)

            # Use Fribb + PMDB anime-seasons to find the right TMDB season,
            # same pipeline as episode remapping.
            fribb = self._lookup_fribb_entry(resolved)
            tmdb_id = _coerce_tmdb_id(resolved.get("tmdb_id"), "tv") or 0
            tmdb_season = 1

            if fribb is not None:
                remapped = self._remap_via_fribb(fribb, tmdb_id, 1)
                if remapped:
                    if remapped.get("tmdb_id"):
                        tmdb_id = _coerce_tmdb_id(remapped["tmdb_id"], "tv") or 0
                    tmdb_season = remapped.get("season", 1)
            elif tmdb_id > 0:
                offset = int(resolved.get("root_episode_offset") or 0)
                if offset > 0:
                    try:
                        anime_seasons = self._get_cached_anime_seasons(tmdb_id)
                        if anime_seasons:
                            remapped = self._map_episode_via_anime_seasons(anime_seasons, offset, 1)
                            if remapped:
                                tmdb_season = remapped.get("season", 1)
                    except Exception:
                        pass

            if not tmdb_id:
                continue

            season_key = f"{tmdb_id}:tv:{tmdb_season}:"
            if existing_counts.get(season_key, 0) > 0:
                stats.items_skipped_duplicate += 1
                continue

            if self._config.sync.dry_run:
                existing_counts[season_key] = 1
                stats.items_added += 1
                continue
            pending_items.append({
                **item,
                "tmdb_id": tmdb_id,
                "media_type": "tv",
                "season": tmdb_season,
                "episode": None,
                "watched_at": item.get("last_watched_at") or item.get("watched_at"),
            })

        before_added = stats.items_added
        self._write_watched_history_items(
            pending_items,
            existing_counts,
            stats,
            status_message="Marking completed anime seasons in PublicMetaDB",
        )
        fallback_count += max(0, stats.items_added - before_added)

        if skipped_has_episodes:
            logger.info(
                "  Season fallback skipped %d show(s) that already had episode records",
                skipped_has_episodes,
            )
        if skipped_without_completed_evidence:
            logger.info(
                "  Season fallback skipped %d show(s) without explicit completed/watched evidence",
                skipped_without_completed_evidence,
            )
        if fallback_count:
            logger.info(
                "  Season fallback marked %d show(s) with no episode history",
                fallback_count,
            )

    @staticmethod
    def _has_completed_anime_evidence(item: dict) -> bool:
        status = str(item.get("status") or "").strip().lower().replace("_", " ")
        if status in {"completed", "complete"}:
            return True
        return bool(
            item.get("watched_at")
            or item.get("last_watched_at")
            or item.get("last_watched")
            or item.get("completed_at")
        )

    def _expand_simkl_aggregate_history(self, items: list[dict]) -> list[dict]:
        expanded: list[dict] = []
        for item in items:
            aggregate_count = item.get("aggregate_watched_count")
            if aggregate_count:
                resolved = self._resolve_activity_item(item)
                aggregate_rows = self._simkl.expand_aggregate_history_item(resolved)
                if aggregate_rows:
                    expanded.extend({**row, "aggregate_source_expanded": True} for row in aggregate_rows)
                else:
                    aggregate_rows = self._expand_simkl_aggregate_anime_item(resolved)
                    if aggregate_rows:
                        expanded.extend(
                            {
                                **self._remap_simkl_anime_history_item(row),
                                "aggregate_source_expanded": True,
                            }
                            for row in aggregate_rows
                        )
                    else:
                        logger.info(
                            "Skipping aggregate SIMKL anime history for '%s' because it could not be safely mapped to concrete seasons/episodes",
                            resolved.get("title", "Unknown"),
                        )
                continue
            expanded.append(item)
        return self._dedupe_activity_history_items(expanded)

    def _expand_simkl_aggregate_anime_item(self, item: dict) -> list[dict]:
        """Expand aggregate anime history through the anime remap pipeline.

        Seasonal anime is often represented as a franchise root plus an episode
        offset. Expanding those items through raw TMDB season plans tends to
        collapse into Season 1. For smaller anime runs, synthesize Season 1
        episode rows first, then let the normal anime remapper place them into
        the correct root TMDB season/episode.
        """
        if str(item.get("simkl_type", "")).strip().lower() != "anime":
            return []
        if str(item.get("media_type", "")).strip().lower() != "tv":
            return []
        try:
            watched_total = int(item.get("aggregate_watched_count") or 0)
        except (TypeError, ValueError):
            return []
        if watched_total <= 0:
            return []

        if not _coerce_tmdb_id(item.get("tmdb_id"), "tv"):
            return []
        has_root_offset = int(item.get("root_episode_offset") or 0) > 0
        has_anime_ids = any(item.get(key) for key in ("anilist_id", "root_anilist_id", "mal_id", "root_mal_id"))
        if not has_root_offset and not has_anime_ids:
            return []
        if watched_total > 200:
            return []

        watched_at = item.get("watched_at")
        return [
            {
                **item,
                "season": 1,
                "episode": episode_number,
                "watched_at": watched_at,
            }
            for episode_number in range(1, watched_total + 1)
        ]

    def _remap_simkl_anime_history_item(self, item: dict) -> dict:
        """Remap a SIMKL anime TV history item to the correct PMDB season+episode.

        Resolution chain (first success wins):
          1. Fribb anime-lists  →  PMDB anime-seasons  (TVDB season → exact TMDB range)
          2. Fribb anime-lists  →  direct TVDB season  (tvdb_season == tmdb_season heuristic)
          3. PMDB anime-seasons with absolute offset    (legacy path, offset-based)
          4. Single-season TMDB heuristic              (only for shows with 1 TMDB season)
        """
        if str(item.get("simkl_type", "")).strip().lower() != "anime":
            return item
        if str(item.get("media_type", "")).strip().lower() != "tv":
            return item
        if item.get("aggregate_source_expanded"):
            return item

        try:
            episode = int(item.get("episode") or 0)
            tmdb_id = _coerce_tmdb_id(item.get("tmdb_id"), "tv") or 0
            offset = int(item.get("root_episode_offset") or 0)
        except (TypeError, ValueError):
            return item
        if episode <= 0:
            return item
        simkl_season = int(item.get("season") or 1)

        cache_key = (
            str(item.get("tmdb_id") or ""),
            str(item.get("anilist_id") or ""),
            str(item.get("root_anilist_id") or ""),
            str(item.get("mal_id") or ""),
            str(item.get("root_mal_id") or ""),
            offset,
            simkl_season,
            episode,
        )
        cached = self._anime_history_remap_cache.get(cache_key)
        if cached is not None:
            return {**item, **cached}

        def _build_tmdb_season_map(tid: int) -> dict[int, int]:
            """Build {tmdb_season: total_episode_count} from PMDB anime-seasons data.

            PMDB anime-seasons maps TVDB arcs to TMDB season+start. We aggregate
            episode_count per tmdb_season to get how many episodes each TMDB season
            actually has, without scraping TMDB's website.
            """
            try:
                seasons = self._get_cached_anime_seasons(tid)
            except Exception:
                return {}
            totals: dict[int, int] = {}
            for s in seasons:
                try:
                    ts = int(s["tmdb_season"])
                    ec = int(s["episode_count"])
                    tmdb_start = int(s["tmdb_episode_start"])
                    # max episode in this arc within the tmdb_season
                    max_ep = tmdb_start + ec - 1
                    totals[ts] = max(totals.get(ts, 0), max_ep)
                except (TypeError, ValueError, KeyError):
                    continue
            return totals

        # Build season episode-count maps from PMDB data (reliable API, no scraping).
        season_plan_map: dict[int, int] = _build_tmdb_season_map(tmdb_id) if tmdb_id > 0 else {}
        # Also keep TMDB scraper plan as secondary fallback for validation only.
        try:
            _tmdb_plan = self._simkl._get_tmdb_season_plan_cached(tmdb_id) if tmdb_id > 0 else []
            for _sn, _cnt in _tmdb_plan:
                if _sn > 0 and _sn not in season_plan_map:
                    season_plan_map[_sn] = _cnt
        except Exception:
            pass

        def _validate_and_fix(remapped: dict) -> dict | None:
            """Validate remapped season/episode against known episode counts.

            - Redistribute across known seasons when cumulative counts are reliable.
            - Treat 0-episode placeholder seasons as invalid targets unless a
              redistribution lands on a real season.
            - Refuse unsafe remaps for sequel/root-offset items when PMDB season
              topology is incomplete or inconsistent.
            - Only allow season-1 overflow when the mapping evidence says this is
              effectively a single-season target.
            Returns a (possibly corrected) remapped dict, or None if unresolvable.
            """
            target_season = int(remapped.get("season") or 1)
            target_episode = int(remapped.get("episode") or 1)
            target_tmdb_id = _coerce_tmdb_id(remapped.get("tmdb_id"), "tv") or tmdb_id
            plan = season_plan_map if target_tmdb_id == tmdb_id else _build_tmdb_season_map(target_tmdb_id)
            season_ep_count = plan.get(target_season)
            if season_ep_count is None:
                return remapped  # No data — accept as-is

            absolute = offset + episode
            known_positive_seasons = sorted(sn for sn, cnt in plan.items() if int(cnt or 0) > 0)
            has_placeholder_seasons = any(int(cnt or 0) == 0 for cnt in plan.values())
            has_multi_season_evidence = (
                offset > 0
                or bool(item.get("root_anilist_id") or item.get("root_mal_id"))
                or len(known_positive_seasons) > 1
                or has_placeholder_seasons
            )
            only_season_one_known = known_positive_seasons == [1]

            if season_ep_count == 0 or target_episode > season_ep_count:
                redistributed = self._map_absolute_via_season_plan(plan, absolute)
                if redistributed:
                    out = {**remapped, **redistributed}
                    if target_tmdb_id != tmdb_id:
                        out["tmdb_id"] = target_tmdb_id
                    return out
                if season_ep_count == 0:
                    return None
                if only_season_one_known and not has_multi_season_evidence:
                    return {**remapped, "season": 1, "episode": absolute}
                return None
            return remapped

        # ── Path 1: Anime-Lists XML exact remap ────────────────────────────────
        xml_remapped = self._remap_via_anime_lists_xml(item, tmdb_id, episode)
        if xml_remapped:
            validated = _validate_and_fix(xml_remapped)
            if validated is None:
                self._anime_history_remap_cache[cache_key] = {"tmdb_id": None}
                return {**item, "tmdb_id": None}
            self._anime_history_remap_cache[cache_key] = dict(validated)
            return {**item, **validated}

        # ── Path 2 & 3: Fribb anime-lists ──────────────────────────────────────
        fribb = self._lookup_fribb_entry(item, allow_root_fallback=False)
        if fribb is not None:
            remapped = self._remap_via_fribb(fribb, tmdb_id, episode)
            if remapped:
                validated = _validate_and_fix(remapped)
                if validated is None:
                    self._anime_history_remap_cache[cache_key] = {"tmdb_id": None}
                    return {**item, "tmdb_id": None}
                remapped = validated
                self._anime_history_remap_cache[cache_key] = dict(remapped)
                return {**item, **remapped}

        # Remaining paths only make sense when there is a non-zero offset
        # (offset == 0 means the item IS the root season; no remapping needed).
        if tmdb_id <= 0:
            return item
        if offset <= 0:
            if season_plan_map:
                validated = _validate_and_fix(item)
                if validated is None:
                    return {**item, "tmdb_id": None}
                return {**item, **validated}
            return item


        # ── Path 3: PMDB anime-seasons with absolute episode offset ─────────────
        try:
            anime_seasons = self._get_cached_anime_seasons(tmdb_id)
            if anime_seasons:
                remapped = self._map_episode_via_anime_seasons(anime_seasons, offset, episode)
                if remapped:
                    validated = _validate_and_fix(remapped)
                    if validated is None:
                        self._anime_history_remap_cache[cache_key] = {"tmdb_id": None}
                        return {**item, "tmdb_id": None}
                    remapped = validated
                    self._anime_history_remap_cache[cache_key] = dict(remapped)
                    return {**item, **remapped}
        except Exception:
            pass

        # ── Path 4: PMDB season map cumulative distribution ────────────────────
        # Handles both single-season and multi-season shows using episode counts
        # from PMDB anime-seasons (reliable API, no scraping required).
        try:
            if season_plan_map:
                absolute = offset + episode
                redistributed = self._map_absolute_via_season_plan(season_plan_map, absolute)
                if redistributed:
                    self._anime_history_remap_cache[cache_key] = dict(redistributed)
                    return {**item, **redistributed}
                known_positive_seasons = sorted(sn for sn, cnt in season_plan_map.items() if int(cnt or 0) > 0)
                has_multi_season_evidence = (
                    offset > 0
                    or bool(item.get("root_anilist_id") or item.get("root_mal_id"))
                    or len(known_positive_seasons) > 1
                )
                if known_positive_seasons == [1] and not has_multi_season_evidence:
                    remapped = {"season": 1, "episode": absolute}
                    self._anime_history_remap_cache[cache_key] = dict(remapped)
                    return {**item, **remapped}
        except Exception:
            pass

        self._anime_history_remap_cache[cache_key] = {}
        return item

    @staticmethod
    def _map_absolute_via_season_plan(season_plan_map: dict[int, int], absolute: int) -> dict | None:
        """Distribute an absolute episode number across TMDB seasons using cumulative counts.

        season_plan_map: {season_number: episode_count, ...}
        Returns {"season": N, "episode": E} or None if the episode is out of range.
        """
        if not season_plan_map or absolute <= 0:
            return None
        cumulative = 0
        for season_number in sorted(season_plan_map):
            ep_count = season_plan_map[season_number]
            if ep_count <= 0:
                continue
            if cumulative < absolute <= cumulative + ep_count:
                return {"season": season_number, "episode": absolute - cumulative}
            cumulative += ep_count
        return None

    def _lookup_fribb_entry(self, item: dict, allow_root_fallback: bool = True) -> dict | None:
        """Return the Fribb anime-lists entry for this anime item, or None.

        Direct AniList/MAL IDs should be preferred for list identity so sequel
        shows stay distinct. Root-ID fallback is still useful for history/remap
        flows where we need franchise-level season topology.
        """
        from . import fribb_client
        ids = item.get("ids") or {}
        anilist_candidates = [item.get("anilist_id"), ids.get("anilist")]
        if allow_root_fallback:
            anilist_candidates.extend([item.get("root_anilist_id"), ids.get("root_anilist")])

        for raw in anilist_candidates:
            if raw:
                try:
                    cache_key = ("anilist", str(int(raw)))
                    if cache_key in self._fribb_lookup_cache:
                        return self._fribb_lookup_cache[cache_key]
                    entry = fribb_client.lookup_by_anilist(int(raw))
                    if entry:
                        self._fribb_lookup_cache[cache_key] = entry
                        return entry
                    self._fribb_lookup_cache[cache_key] = None
                except (TypeError, ValueError):
                    pass

        mal_candidates = [item.get("mal_id"), ids.get("mal")]
        if allow_root_fallback:
            mal_candidates.extend([item.get("root_mal_id"), ids.get("root_mal")])

        for raw in mal_candidates:
            if raw:
                try:
                    cache_key = ("mal", str(int(raw)))
                    if cache_key in self._fribb_lookup_cache:
                        return self._fribb_lookup_cache[cache_key]
                    entry = fribb_client.lookup_by_mal(int(raw))
                    if entry:
                        self._fribb_lookup_cache[cache_key] = entry
                        return entry
                    self._fribb_lookup_cache[cache_key] = None
                except (TypeError, ValueError):
                    pass

        anidb_candidates = [item.get("anidb_id"), ids.get("anidb")]
        for raw in anidb_candidates:
            if raw:
                try:
                    cache_key = ("anidb", str(int(raw)))
                    if cache_key in self._fribb_lookup_cache:
                        return self._fribb_lookup_cache[cache_key]
                    entry = fribb_client.lookup_by_anidb(int(raw))
                    if entry:
                        self._fribb_lookup_cache[cache_key] = entry
                        return entry
                    self._fribb_lookup_cache[cache_key] = None
                except (TypeError, ValueError):
                    pass

        simkl_candidates = [ids.get("simkl")]
        for raw in simkl_candidates:
            if raw:
                try:
                    cache_key = ("simkl", str(int(raw)))
                    if cache_key in self._fribb_lookup_cache:
                        return self._fribb_lookup_cache[cache_key]
                    entry = fribb_client.lookup_by_simkl(int(raw))
                    if entry:
                        self._fribb_lookup_cache[cache_key] = entry
                        return entry
                    self._fribb_lookup_cache[cache_key] = None
                except (TypeError, ValueError):
                    pass

        imdb_candidates = [item.get("imdb_id"), ids.get("imdb")]
        for raw in imdb_candidates:
            if raw:
                cache_key = ("imdb", str(raw))
                if cache_key in self._fribb_lookup_cache:
                    return self._fribb_lookup_cache[cache_key]
                entry = fribb_client.lookup_by_imdb(str(raw))
                if entry:
                    self._fribb_lookup_cache[cache_key] = entry
                    return entry
                self._fribb_lookup_cache[cache_key] = None

        return None

    def _remap_via_anime_lists_xml(self, item: dict, item_tmdb_id: int, episode: int) -> dict | None:
        anidb_id = item.get("anidb_id") or (item.get("ids") or {}).get("anidb")
        if not anidb_id:
            fribb = self._lookup_fribb_entry(item, allow_root_fallback=False)
            if isinstance(fribb, dict):
                anidb_id = fribb.get("anidb_id")
        try:
            anidb_id_int = int(anidb_id or 0)
        except (TypeError, ValueError):
            return None
        if anidb_id_int <= 0:
            return None

        anidb_season = 1
        try:
            maybe_season = int(item.get("season") or 1)
            if maybe_season > 0:
                anidb_season = maybe_season
        except (TypeError, ValueError):
            pass

        remapped_tvdb = anime_mapping_store.resolve_tvdb_episode_from_anidb_episode(
            anidb_id_int,
            int(episode),
            anidb_season=anidb_season,
        )
        if not remapped_tvdb:
            return None

        tvdb_id = int(remapped_tvdb["tvdb_id"])
        tvdb_season = int(remapped_tvdb["tvdb_season"])
        tvdb_episode = int(remapped_tvdb["tvdb_episode"])
        tmdb_id = item_tmdb_id

        if tmdb_id <= 0:
            fribb = self._lookup_fribb_entry(item, allow_root_fallback=False)
            if isinstance(fribb, dict):
                # themoviedb_id is a media-type-keyed dict; int() on it raised and
                # silently left tmdb_id at 0, abandoning the remap.
                extracted, _mapped_media_type = fribb_client.extract_tmdb(
                    fribb.get("themoviedb_id") or fribb.get("themoviedb")
                )
                tmdb_id = extracted or 0

        # The mapping data may carry TMDB coordinates directly, which removes the
        # need to translate the TVDB season through PMDB anime-seasons at all.
        xml_tmdb_season = remapped_tvdb.get("tmdb_season")
        xml_tmdb_episode = remapped_tvdb.get("tmdb_episode")
        if xml_tmdb_season is not None and xml_tmdb_episode is not None:
            if int(xml_tmdb_season) <= 0:
                return None  # Season 0 = specials; leave untouched
            xml_tmdb_id = remapped_tvdb.get("tmdb_id") or tmdb_id
            out: dict = {"season": int(xml_tmdb_season), "episode": int(xml_tmdb_episode)}
            if xml_tmdb_id and int(xml_tmdb_id) != item_tmdb_id:
                out["tmdb_id"] = int(xml_tmdb_id)
            return out

        if tmdb_id <= 0:
            return None

        anime_seasons = self._get_cached_anime_seasons(tmdb_id)
        remapped = self._map_episode_via_tvdb_season(anime_seasons, tvdb_season, tvdb_episode) if anime_seasons else None
        if remapped:
            out = {"season": remapped["season"], "episode": remapped["episode"]}
            if tmdb_id != item_tmdb_id:
                out["tmdb_id"] = tmdb_id
            return out

        return {"season": tvdb_season, "episode": tvdb_episode, **({"tmdb_id": tmdb_id} if tmdb_id != item_tmdb_id else {})}

    @staticmethod
    def _fribb_season_offset(fribb: dict, namespace: str) -> tuple[int | None, int]:
        """Read Fribb's season / episode offset for one id namespace.

        Both fields are dicts keyed by namespace upstream — ``season`` is
        ``{"tvdb": N, "tmdb": N}`` and ``episode_offset`` is ``{"tvdb": N,
        "tmdb": N}``.  Returns ``(season, offset)`` with ``season`` None when the
        entry carries nothing for that namespace.
        """
        season_raw = fribb.get("season")
        season = None
        if isinstance(season_raw, dict) and namespace in season_raw:
            season = _safe_season_int(season_raw.get(namespace))
        offset_raw = fribb.get("episode_offset")
        offset = 0
        if isinstance(offset_raw, dict):
            offset = _safe_season_int(offset_raw.get(namespace)) or 0
        return season, offset

    def _remap_via_fribb(self, fribb: dict, item_tmdb_id: int, episode: int) -> dict | None:
        """Use Fribb's season info to remap an episode into TMDB numbering.

        Fribb records which season of a target database this AniList/MAL entry
        belongs to, plus the episode offset within that season.  It carries these
        for both TVDB and TMDB; when the TMDB pair is present it is a direct
        answer and no PMDB round trip is needed.  Otherwise the TVDB season is
        translated through PMDB anime-seasons (whose season_number is the TVDB
        season), falling back to assuming tvdb_season == tmdb_season, which holds
        for the vast majority of modern seasonal anime.
        """
        # Prefer the Fribb-provided TMDB ID when the item doesn't have one.
        tmdb_id = item_tmdb_id
        fribb_tmdb, _mapped_media_type = fribb_client.extract_tmdb(
            fribb.get("themoviedb_id") or fribb.get("themoviedb")
        )
        if not tmdb_id and fribb_tmdb:
            tmdb_id = fribb_tmdb

        # Direct TMDB coordinates: no translation, no PMDB lookup.
        tmdb_season, tmdb_offset = self._fribb_season_offset(fribb, "tmdb")
        if tmdb_season is not None:
            if tmdb_season <= 0:
                return None  # Season 0 = specials; leave untouched
            out: dict = {"season": tmdb_season, "episode": tmdb_offset + episode}
            if tmdb_id and tmdb_id != item_tmdb_id:
                out["tmdb_id"] = tmdb_id
            return out

        tvdb_season, tvdb_epoffset = self._fribb_season_offset(fribb, "tvdb")
        if tvdb_season is None:
            return None
        if tvdb_season <= 0:
            return None  # Season 0 = specials; leave untouched

        # Episode number within the TVDB season
        tvdb_episode = tvdb_epoffset + episode

        # Try PMDB anime-seasons: season_number in PMDB == TVDB season number.
        if tmdb_id > 0:
            try:
                anime_seasons = self._get_cached_anime_seasons(tmdb_id)
                if anime_seasons:
                    remapped = self._map_episode_via_tvdb_season(anime_seasons, tvdb_season, tvdb_episode)
                    if remapped:
                        out = {"season": remapped["season"], "episode": remapped["episode"]}
                        if tmdb_id != item_tmdb_id:
                            out["tmdb_id"] = tmdb_id
                        return out
            except Exception:
                pass

        # Fallback: tvdb_season == tmdb_season holds for most shows where each
        # cour / arc gets its own TMDB season (AoT, DanDaDan, etc.).
        out: dict = {"season": tvdb_season, "episode": tvdb_episode}
        if tmdb_id > 0 and tmdb_id != item_tmdb_id:
            out["tmdb_id"] = tmdb_id
        return out

    def _remap_trakt_anime_episode(self, item: dict) -> dict:
        """Remap a Trakt TV episode from TVDB to TMDB numbering via PMDB anime-seasons.

        Trakt uses TVDB season/episode numbering; PMDB expects TMDB numbering.
        PMDB anime-seasons is the canonical source: if the community has added
        season mappings for this TMDB ID the episode is remapped; otherwise the
        item is written as-is (TVDB == TMDB for the vast majority of anime).

        Empty PMDB responses are cached per TMDB ID so non-anime shows only pay
        a single 404 per sync run — no Fribb pre-filter needed.
        """
        if item.get("media_type") != "tv":
            return item
        try:
            tmdb_id = _coerce_tmdb_id(item.get("tmdb_id"), "tv") or 0
            tvdb_season = int(item.get("season") or 0)
            tvdb_episode = int(item.get("episode") or 0)
        except (TypeError, ValueError):
            return item
        if tmdb_id <= 0 or tvdb_season <= 0 or tvdb_episode <= 0:
            return item

        anime_seasons = self._get_cached_anime_seasons(tmdb_id)
        if anime_seasons:
            remapped = self._map_episode_via_tvdb_season(anime_seasons, tvdb_season, tvdb_episode)
            if remapped:
                if remapped["season"] == tvdb_season and remapped["episode"] == tvdb_episode:
                    return item  # Numbering already matches — no change
                logger.info(
                    "Remapped Trakt anime '%s' (tmdb=%d) via PMDB seasons: S%dE%d → S%dE%d",
                    item.get("title", "Unknown"), tmdb_id,
                    tvdb_season, tvdb_episode,
                    remapped["season"], remapped["episode"],
                )
                return {**item, "season": remapped["season"], "episode": remapped["episode"]}

        fribb = self._lookup_fribb_entry(item, allow_root_fallback=False)
        if fribb is not None:
            remapped = self._remap_via_fribb(fribb, tmdb_id, tvdb_episode)
            if remapped and (remapped.get("season") != tvdb_season or remapped.get("episode") != tvdb_episode):
                logger.info(
                    "Remapped Trakt anime '%s' (tmdb=%d) via Fribb: S%dE%d → S%dE%d",
                    item.get("title", "Unknown"), tmdb_id,
                    tvdb_season, tvdb_episode,
                    remapped.get("season", tvdb_season),
                    remapped.get("episode", tvdb_episode),
                )
                return {**item, **remapped}

        return item

    def _get_cached_anime_seasons(self, tmdb_id: int) -> list[dict]:
        tmdb_id = int(tmdb_id)
        if tmdb_id <= 0:
            return []
        with self.__class__._shared_cache_lock:
            cached = self._anime_seasons_cache.get(tmdb_id)
        if cached is not None:
            return list(cached)
        seasons = list(self._pmdb.get_anime_seasons(tmdb_id))
        with self.__class__._shared_cache_lock:
            self._anime_seasons_cache[tmdb_id] = list(seasons)
        return list(seasons)

    @staticmethod
    def _map_episode_via_tvdb_season(
        seasons: list[dict], tvdb_season: int, tvdb_episode: int
    ) -> dict | None:
        """Map a TVDB season+episode to a TMDB season+episode using PMDB anime-seasons.

        PMDB stores the community mapping as: season_number (= TVDB season) →
        tmdb_season + tmdb_episode_start.  We find the matching entry and compute
        the TMDB episode from the start offset.
        """
        for s in seasons:
            try:
                if int(s["season_number"]) != tvdb_season:
                    continue
                tmdb_season = int(s["tmdb_season"])
                tmdb_start = int(s["tmdb_episode_start"])
            except (TypeError, ValueError, KeyError):
                continue
            return {"season": tmdb_season, "episode": tmdb_start + (tvdb_episode - 1)}
        return None

    @staticmethod
    def _map_episode_via_anime_seasons(seasons: list[dict], offset: int, episode: int) -> dict | None:
        """Legacy path: map absolute episode (offset + episode) via PMDB anime-seasons.

        Used when Fribb data is unavailable.  Accumulates episode_count across
        sorted season entries to locate which arc the absolute episode falls in.
        """
        absolute = offset + episode

        valid = [
            s for s in seasons
            if s.get("season_number") is not None
            and s.get("episode_count")
            and s.get("tmdb_season") is not None
            and s.get("tmdb_episode_start") is not None
        ]
        if not valid:
            return None

        valid.sort(key=lambda s: int(s.get("season_number") or 0))

        cumulative = 0
        for s in valid:
            try:
                ep_count = int(s["episode_count"])
                tmdb_season = int(s["tmdb_season"])
                tmdb_start = int(s["tmdb_episode_start"])
            except (TypeError, ValueError):
                return None
            if ep_count <= 0:
                return None

            season_abs_start = cumulative + 1
            season_abs_end = cumulative + ep_count

            if season_abs_start <= absolute <= season_abs_end:
                return {
                    "season": tmdb_season,
                    "episode": tmdb_start + (absolute - season_abs_start),
                }

            cumulative += ep_count

        return None

    def _sync_trakt_watched_history(self) -> SyncStats:
        stats = SyncStats(
            list_name="",
            display_name="Trakt Watch History",
            source_name="Trakt",
            row_key=self._make_row_key("trakt", "watch_history", "history", {"mode": "history"}),
            row_type="history",
        )
        cursor = self._config.sync.trakt_history_cursor or ""
        since = None if self._config.sync.full_history_sync or not cursor else cursor
        # /sync/history is a play log read forward from a cursor, so an episode
        # missed on an earlier run is never revisited. /sync/watched carries the
        # complete per-episode watched state of every show in one response and
        # is the only way a partially-imported multi-season show backfills.
        reconcile = bool(
            self._config.sync.trakt_reconcile_watched_history
            or self._config.sync.full_history_sync
        )
        self._set_status("Fetching Trakt watched history…")
        _executor = ThreadPoolExecutor(max_workers=min(_ACTIVITY_SOURCE_WORKERS, 3))
        f_trakt = _executor.submit(self._trakt.get_watched_history, since=since, status_callback=self._set_status)
        f_watched = (
            _executor.submit(self._trakt.get_watched_state, status_callback=self._set_status)
            if reconcile else None
        )
        f_pmdb = _executor.submit(self._pmdb.get_watched_history)
        _executor.shutdown(wait=False)

        try:
            items = f_trakt.result()
        except TraktAuthenticationError as exc:
            self._record_error(stats, "fetch", str(exc))
            return stats

        watched_rows: list[dict] = []
        if f_watched is not None:
            try:
                watched_rows = f_watched.result()
            except TraktAuthenticationError as exc:
                self._record_error(stats, "fetch", str(exc))
                return stats
            except Exception as exc:
                # The incremental import above is still valid without it, so a
                # failed reconciliation is recorded and skipped, not fatal.
                self._record_error(
                    stats, "fetch", f"Trakt watched-state reconciliation failed: {exc}"
                )
                watched_rows = []

        stats.items_fetched = len(items) + len(watched_rows)
        # Advance the cursor to the latest watched_at seen in this batch. Only
        # the play log may move it: watched-state rows carry last_watched_at,
        # not per-play stamps, so treating them as cursor input would skip plays.
        stats.history_cursor = self._latest_history_cursor(items, cursor)
        if not items and not watched_rows:
            # Nothing new — keep existing cursor and return immediately.
            logger.info("Trakt history: no new events since cursor — skipping PMDB fetch")
            return stats

        try:
            existing_items = f_pmdb.result()
        except Exception as exc:
            self._record_error(stats, "pmdb_read", f"Failed to load PublicMetaDB watched history: {exc}")
            return stats

        existing_counts: dict[str, int] = {}
        for existing_item in existing_items:
            key = self._watched_identity_key(existing_item)
            if key:
                existing_counts[key] = existing_counts.get(key, 0) + 1

        source_seen: dict[str, int] = {}
        pending_items: list[dict] = []
        total = len(items)
        for idx, item in enumerate(items):
            self._check_cancelled()
            if idx % 50 == 0:
                self._set_status(f"Resolving Trakt history ({idx}/{total})")
            if self._fast_skip_existing_history_item(item, existing_counts, source_seen, stats):
                continue
            item = self._resolve_activity_item(item)
            item = self._remap_trakt_anime_episode(item)
            key = self._watched_identity_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            stats.items_resolved += 1
            self._increment_history_source_seen(source_seen, key)
            if existing_counts.get(key, 0) >= source_seen[key]:
                stats.items_skipped_duplicate += 1
                continue
            if self._config.sync.dry_run:
                existing_counts[key] = existing_counts.get(key, 0) + 1
                stats.items_added += 1
                continue
            pending_items.append(item)
        self._write_watched_history_items(
            pending_items,
            existing_counts,
            stats,
            status_message="Writing Trakt history to PublicMetaDB",
            total_source=total,
        )
        # Runs last so it sees the counts the write above just refreshed from
        # PMDB, and therefore only fills what is genuinely still missing.
        self._reconcile_trakt_watched_state(watched_rows, existing_counts, stats)
        return stats

    def _reconcile_trakt_watched_state(
        self,
        rows: list[dict],
        existing_counts: dict[str, int],
        stats: SyncStats,
    ) -> None:
        """Write episodes Trakt's watched state has and PublicMetaDB does not.

        These rows carry presence, not plays: /sync/watched reports one
        last_watched_at per episode, so an episode is written only when PMDB
        has no record of it at all. A show that is already fully imported
        therefore writes nothing on the next run, while a partially-imported
        multi-season anime backfills every season it is missing.

        Rewatch counts stay the play log's job — Trakt's `plays` number has no
        per-play timestamps behind it, so expanding it here would write records
        that PMDB's deduping write collapses anyway.
        """
        if not rows:
            return

        self._set_status(f"Reconciling Trakt watched state ({len(rows)} episodes)")
        total = len(rows)
        pending_items: list[dict] = []
        queued_keys: set[str] = set()
        for index, row in enumerate(rows):
            self._check_cancelled()
            if index % 200 == 0:
                self._set_status(f"Reconciling Trakt watched state ({index}/{total})")
            item = self._remap_trakt_anime_episode(self._resolve_activity_item(row))
            key = self._watched_identity_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            stats.items_resolved += 1
            # The anime remapper can fold two source coordinates onto one PMDB
            # episode, so a key already queued this pass is a duplicate too.
            if int(existing_counts.get(key, 0) or 0) > 0 or key in queued_keys:
                stats.items_skipped_duplicate += 1
                continue
            queued_keys.add(key)
            if self._config.sync.dry_run:
                existing_counts[key] = 1
                stats.items_added += 1
                stats.items_reconciled += 1
                continue
            pending_items.append(item)

        if not pending_items:
            logger.info("Trakt watched-state reconciliation: nothing missing from PublicMetaDB")
            return

        added_before = stats.items_added
        self._write_watched_history_items(
            pending_items,
            existing_counts,
            stats,
            status_message="Backfilling Trakt watched state into PublicMetaDB",
            total_source=len(pending_items),
        )
        backfilled = max(0, stats.items_added - added_before)
        stats.items_reconciled += backfilled
        logger.info(
            "Trakt watched-state reconciliation: backfilled %d episode(s) missing from PublicMetaDB",
            backfilled,
        )

    def _sync_trakt_resume_progress(self) -> SyncStats:
        stats = SyncStats(
            list_name="",
            display_name="Trakt Resume Progress",
            source_name="Trakt",
            row_key=self._make_row_key("trakt", "resume_progress", "resume", {"mode": "resume"}),
            row_type="resume",
        )
        self._set_status("Fetching Trakt playback progress")
        try:
            items = self._trakt.get_playback_progress()
        except TraktAuthenticationError as exc:
            self._record_error(stats, "fetch", str(exc))
            return stats
        stats.items_fetched = len(items)

        normalized_items: list[dict] = []
        effectively_watched_items: list[dict] = []
        for item in items:
            self._check_cancelled()
            raw_progress = item.get("progress")
            if raw_progress is not None:
                try:
                    pct = float(raw_progress)
                    if pct >= 80.0:
                        # ≥80% — PMDB dev guidance: do not submit as a resume point
                        # (which would show "80% in progress"). Submit as watched so
                        # PMDB marks it completed with no leftover progress entry.
                        resolved = self._resolve_activity_item(item)
                        if resolved.get("tmdb_id"):
                            effectively_watched_items.append(resolved)
                            logger.debug(
                                "Treating resume for %s (progress=%.0f%%) as watched",
                                item.get("title", "unknown"), pct,
                            )
                        else:
                            stats.items_skipped_unresolved += 1
                        continue
                except (TypeError, ValueError):
                    pass
            item = self._resolve_activity_item(item)
            item = self._remap_trakt_anime_episode(item)
            key = self._resume_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            normalized_items.append(item)
            stats.items_resolved += 1

        if self._config.sync.dry_run:
            stats.items_added = len(normalized_items) + len(effectively_watched_items)
            return stats

        if effectively_watched_items:
            self._set_status("Writing near-complete Trakt items as watched to PublicMetaDB")
            existing_watched = {}
            try:
                existing_watched = self._count_watched_history_identities(self._pmdb.get_watched_history())
            except Exception:
                pass
            # Pre-filter items already marked watched in PMDB (by identity, regardless of timestamp).
            filtered_effective: list[dict] = []
            for item in effectively_watched_items:
                identity_key = self._watched_identity_key(item)
                if identity_key and existing_watched.get(identity_key, 0) > 0:
                    stats.items_skipped_duplicate += 1
                else:
                    filtered_effective.append(item)
            if filtered_effective:
                self._write_watched_history_items(
                    filtered_effective,
                    existing_watched,
                    stats,
                    "Writing near-complete Trakt items as watched",
                )

        if not normalized_items:
            return stats

        try:
            existing_resume_points = self._pmdb.get_resume_points()
        except Exception as exc:
            self._record_error(stats, "pmdb_read", f"Failed to load PublicMetaDB resume points: {exc}")
            return stats

        try:
            existing_watched_items = self._pmdb.get_watched_history()
        except Exception as exc:
            self._record_error(stats, "pmdb_read", f"Failed to load PublicMetaDB watched history: {exc}")
            return stats

        existing_resume_by_key: dict[str, dict] = {}
        for item in existing_resume_points:
            key = self._resume_key(item)
            if key:
                existing_resume_by_key[key] = item

        existing_watched_counts: dict[str, int] = {}
        for item in existing_watched_items:
            key = self._watched_identity_key(item)
            if key:
                existing_watched_counts[key] = existing_watched_counts.get(key, 0) + 1

        payloads: list[dict] = []
        completed_items: list[dict] = []
        for item in normalized_items:
            self._check_cancelled()
            key = self._resume_key(item)
            runtime_ms = int(item.get("runtime_ms", 0) or 0)
            position_ms = int(item.get("position_ms", 0) or 0)
            if runtime_ms > 0 and position_ms >= runtime_ms:
                if existing_watched_counts.get(key, 0) > 0:
                    stats.items_skipped_duplicate += 1
                    continue
                completed_items.append(item)
                continue
            payload = {
                "tmdb_id": _coerce_tmdb_id(item.get("tmdb_id"), item.get("media_type") or "") or 0,
                "media_type": item["media_type"],
                "position_ms": position_ms,
                "runtime_ms": runtime_ms,
            }
            if item.get("season") is not None:
                payload["season"] = int(item["season"])
            if item.get("episode") is not None:
                payload["episode"] = int(item["episode"])
            existing = existing_resume_by_key.get(key)
            if existing and self._resume_matches(existing, payload):
                stats.items_skipped_duplicate += 1
                continue
            payloads.append(payload)

        self._write_watched_history_items(
            completed_items,
            existing_watched_counts,
            stats,
            status_message="Writing Trakt completed playback items to PublicMetaDB",
            total_source=len(normalized_items),
        )

        if not payloads:
            return stats

        for chunk in self._chunked(payloads, 50):
            self._check_cancelled()
            try:
                self._set_status("Writing Trakt resume progress to PublicMetaDB")
                response = self._pmdb.save_resume_points_batch(chunk) or {}
                payloads_by_key = {self._resume_key(item): item for item in chunk}
                saved_count = 0
                for result in response.get("results", []):
                    action = str(result.get("action", "")).strip().lower()
                    payload = payloads_by_key.get(self._resume_key(result), {})
                    runtime_ms = int(payload.get("runtime_ms", 0) or 0)
                    position_ms = int(payload.get("position_ms", 0) or 0)
                    local_completion = runtime_ms > 0 and position_ms >= runtime_ms
                    if action == "saved" or (action == "completed" and not local_completion):
                        stats.items_added += 1
                        saved_count += 1
                stats.items_skipped_duplicate += max(0, len(chunk) - saved_count)
            except SyncCancelled:
                raise
            except Exception as exc:
                self._record_error(stats, "pmdb_write", f"Failed to sync resume batch: {exc}")
                continue
        return stats

    def _sync_anilist(self) -> list[SyncStats]:
        """Sync configured AniList anime statuses."""
        if "anime" not in self._config.sync.media_types:
            logger.info("AniList sync skipped — 'anime' not in media_types: %s", self._config.sync.media_types)
            return []
        logger.info("── AniList Sync ───────────────────────────────────────")

        from .anilist_client import AniListClient

        client = AniListClient(
            self._config.anilist,
            cancel_requested_callback=self._cancel_requested_callback,
        )
        stats: list[SyncStats] = []

        # Collect all items from every status first, then pre-warm the shared
        # AniList prequel-chain cache concurrently before the list sync begins.
        # This ensures SIMKL anime (which runs concurrently) gets cache hits for
        # any AniList IDs it shares, instead of each thread walking the same chains.
        all_items_by_status: list[tuple[str, list[dict]]] = []
        fetch_started = time.perf_counter()
        fetched_by_status = client.get_statuses(list(self._config.anilist.selected_statuses))
        logger.info(
            "Fetched AniList selections in %.2fs (%d statuses)",
            time.perf_counter() - fetch_started,
            len(self._config.anilist.selected_statuses),
        )
        # Formats whose synthetic status keys include single-episode items that
        # were reclassified as media_type="movie" in anilist_client.  For those
        # keys we must not drop movies, since e.g. a 1-episode ONA IS the thing
        # the user wants in their "Completed ONA" list.
        _FORMAT_KEYS = frozenset(AniListClient._FORMAT_FILTER_MAP.keys())

        for status_key in self._config.anilist.selected_statuses:
            self._check_cancelled()
            self._set_status(f"Fetching AniList {_STATUS_LABELS.get(status_key, status_key)} anime")
            raw_items = fetched_by_status.get(status_key, [])
            if status_key in _FORMAT_KEYS:
                # Format-specific list (ONA, OVA, MOVIE): include both tv and
                # movie media_types so single-episode ONAs/OVAs are not lost.
                items = [
                    item for item in raw_items
                    if str(item.get("media_type") or "").strip().lower() in {"tv", "movie"}
                ]
            else:
                # Plain status (COMPLETED, WATCHING, …): only episodic TV entries.
                # Single-episode ONA/OVA/SPECIAL entries get media_type="movie"
                # in anilist_client and are intentionally routed to the format-
                # specific list instead; drop them here to avoid duplicates.
                items = [
                    item for item in raw_items
                    if str(item.get("media_type") or "").strip().lower() == "tv"
                ]
            logger.info(
                "Loaded AniList %s (%d/%d anime items, %d dropped)",
                _STATUS_LABELS.get(status_key, status_key),
                len(items),
                len(raw_items),
                len(raw_items) - len(items),
            )
            all_items_by_status.append((status_key, items))

        # Pre-warm the shared prequel-chain cache for all unique AniList IDs.
        # Uses up to 4 threads; the shared lock in _get_root_context prevents
        # duplicate chain walks for the same ID.
        all_anilist_ids = list({
            int(item["anilist_id"])
            for _, items in all_items_by_status
            for item in items
            if item.get("anilist_id")
        })
        simkl_anime_enabled = bool(
            self._config.simkl.selected_statuses.get("anime")
            and "anime" in self._config.sync.media_types
        )
        if all_anilist_ids:
            prewarm_ids = all_anilist_ids[:_ANILIST_PREWARM_LIMIT]
            self._set_status("Pre-warming anime metadata cache")
            get_ctx = getattr(client, "_get_root_context", None)
            if callable(get_ctx):
                pool = ThreadPoolExecutor(max_workers=min(_PREWARM_WORKERS, 4))
                shutdown_wait = True
                try:
                    futures = {pool.submit(get_ctx, aid): aid for aid in prewarm_ids}
                    for future in self._iter_completed_futures(futures):
                        try:
                            future.result()
                        except Exception as exc:
                            logger.debug("Cache warm failed for anilist_id=%s: %s", futures[future], exc)
                except SyncCancelled:
                    shutdown_wait = False
                    raise
                finally:
                    pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)
            logger.info(
                "Pre-warmed anime chain cache for %d/%d AniList IDs",
                len(prewarm_ids),
                len(all_anilist_ids),
            )

        all_anilist_items = [item for _, items in all_items_by_status for item in items]
        if all_anilist_items and hasattr(self._matcher, "preseed_anime_from_fribb"):
            self._matcher.preseed_anime_from_fribb(all_anilist_items)

        for status_key, items in all_items_by_status:
            self._check_cancelled()
            name = _status_list_name("anime", status_key)
            display_name = _display_status_name("anime", status_key)
            self._publish_pending_list(name, display_name, "AniList")
            description = f"Auto-synced '{_STATUS_LABELS.get(status_key, status_key)}' anime from AniList"
            logger.info("AniList: syncing '%s' with %d items", display_name, len(items))
            try:
                result = self._sync_list(
                    items,
                    name,
                    description,
                    display_name=display_name,
                    source_name="AniList",
                    is_public=self._config.sync.anilist_visibility == "public",
                    force_remove_missing=True,
                    selection={
                        "source": "anilist",
                        "status": status_key,
                    },
                )
                stats.append(result)
                logger.info(
                    "AniList: '%s' done — fetched=%d resolved=%d added=%d errors=%d",
                    display_name, result.items_fetched, result.items_resolved,
                    result.items_added, len(result.errors),
                )
            except SyncCancelled:
                raise
            except Exception as exc:
                logger.error("AniList: failed to sync '%s': %s", display_name, exc, exc_info=True)

        return stats

    def _sync_trakt(self) -> list[SyncStats]:
        """Sync configured Trakt watchlist and list sources."""
        stats: list[SyncStats] = []

        should_sync_watchlist_movies = self._config.trakt.sync_watchlist_movies and "movies" in self._config.sync.media_types
        should_sync_watchlist_shows = self._config.trakt.sync_watchlist_shows and "shows" in self._config.sync.media_types
        if should_sync_watchlist_movies or should_sync_watchlist_shows:
            self._check_cancelled()
            if should_sync_watchlist_movies:
                self._publish_pending_list(_status_list_name("movies", "watchlist"), _display_status_name("movies", "watchlist"), "Trakt")
            if should_sync_watchlist_shows:
                self._publish_pending_list(_status_list_name("shows", "watchlist"), _display_status_name("shows", "watchlist"), "Trakt")
            self._set_status("Fetching Trakt watchlist")
            watchlist_items = self._trakt.get_watchlist()
            grouped = {
                "shows": [item for item in watchlist_items if item["media_type"] == "tv"],
                "movies": [item for item in watchlist_items if item["media_type"] == "movie"],
            }
            if should_sync_watchlist_movies:
                items = grouped.get("movies", [])
                stats.append(
                    self._sync_list(
                        items,
                        _status_list_name("movies", "watchlist"),
                        "Auto-synced Trakt movie watchlist",
                        display_name=_display_status_name("movies", "watchlist"),
                        source_name="Trakt",
                        activities_ts=self._current_source_activities.get("trakt", ""),
                        is_public=self._config.sync.trakt_personal_visibility == "public",
                        selection={
                            "source": "trakt",
                            "kind": "watchlist",
                            "media_type": "movies",
                        },
                    )
                )
            if should_sync_watchlist_shows:
                items = grouped.get("shows", [])
                stats.append(
                    self._sync_list(
                        items,
                        _status_list_name("shows", "watchlist"),
                        "Auto-synced Trakt show watchlist",
                        display_name=_display_status_name("shows", "watchlist"),
                        source_name="Trakt",
                        activities_ts=self._current_source_activities.get("trakt", ""),
                        is_public=self._config.sync.trakt_personal_visibility == "public",
                        selection={
                            "source": "trakt",
                            "kind": "watchlist",
                            "media_type": "shows",
                        },
                    )
                )

        selected_lists = self._dedupe_trakt_lists(self._config.trakt.selected_lists)
        selected_default_lists = [item for item in selected_lists if item.get("source") == "default"]
        selected_liked_lists = [item for item in selected_lists if item.get("source") == "liked"]
        selected_personal_lists = [item for item in selected_lists if item.get("source") == "personal"]
        selected_public_lists = [item for item in selected_lists if item.get("source") == "discover"]

        fetched_selected_trakt = self._fetch_selected_trakt_lists(
            selected_default_lists,
            selected_liked_lists,
            selected_personal_lists,
            selected_public_lists,
        )

        if self._config.trakt.sync_liked_lists:
            self._check_cancelled()
            self._set_status("Fetching Trakt liked lists")
            for liked_list in self._trakt.get_liked_lists():
                self._check_cancelled()
                self._publish_pending_list(liked_list["name"], liked_list["name"], f"Trakt by {liked_list['user']}")
                items = self._filter_trakt_items(liked_list["items"])
                name = liked_list["name"]
                description = f"Auto-synced liked Trakt list '{liked_list['name']}'"
                stats.append(
                    self._sync_list(
                        items,
                        name,
                        description,
                        display_name=liked_list["name"],
                        source_name=f"Trakt by {liked_list['user']}",
                        activities_ts=self._current_source_activities.get("trakt", ""),
                        is_public=self._config.sync.trakt_public_visibility == "public",
                        selection={
                            "source": "trakt",
                            "kind": "liked-auto",
                            "list_source": "liked",
                            "user": liked_list.get("user", ""),
                            "slug": liked_list.get("slug", ""),
                            "name": liked_list.get("name", ""),
                        },
                    )
                )

        for trakt_list, items in fetched_selected_trakt:
            self._check_cancelled()
            source_kind = str(trakt_list.get("source", "")).strip()
            is_default = source_kind == "default"
            source_name = "Trakt" if is_default else f"Trakt by {trakt_list['user']}"
            description = (
                f"Auto-synced Trakt default catalog '{trakt_list['name']}'"
                if is_default
                else (
                    f"Auto-synced your Trakt list '{trakt_list['name']}'"
                    if source_kind == "personal"
                    else f"Auto-synced Trakt list '{trakt_list['name']}' by {trakt_list['user']}"
                )
            )
            is_public = (
                self._config.sync.trakt_personal_visibility == "public"
                if source_kind in {"default", "personal"}
                else self._config.sync.trakt_public_visibility == "public"
            )
            selection = {
                "source": "trakt",
                "kind": "default" if is_default else "selected-list",
                "list_source": source_kind,
                "user": trakt_list.get("user", ""),
                "slug": trakt_list.get("slug", ""),
                "name": trakt_list.get("name", ""),
            }
            if is_default:
                selection["catalog_key"] = trakt_list.get("catalog_key", "")
            stats.append(
                self._sync_list(
                    items,
                    trakt_list["name"],
                    description,
                    display_name=trakt_list["name"],
                    source_name=source_name,
                    activities_ts=self._current_source_activities.get("trakt", ""),
                    is_public=is_public,
                    selection=selection,
                )
            )

        return stats

    def _sync_mdblist(self) -> list[SyncStats]:
        """Sync selected MDBList lists."""
        stats: list[SyncStats] = []

        for mdblist, items in self._fetch_selected_mdblist_lists(self._dedupe_mdblist_lists(self._config.mdblist.selected_lists)):
            self._check_cancelled()
            name = mdblist["name"]
            description = f"Auto-synced MDBList '{mdblist['name']}'"
            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=mdblist["name"],
                    source_name="MDBList",
                    is_public=self._config.sync.mdblist_visibility == "public",
                    selection={
                        "source": "mdblist",
                        "id": mdblist.get("id"),
                        "mediatype": mdblist.get("mediatype", ""),
                        "name": mdblist.get("name", ""),
                    },
                )
            )

        return stats

    def _filter_trakt_items(self, items: list[dict]) -> list[dict]:
        filtered = []
        for item in items:
            if item["media_type"] == "movie" and "movies" not in self._config.sync.media_types:
                continue
            if item["media_type"] == "tv" and "shows" not in self._config.sync.media_types:
                continue
            filtered.append(item)
        return filtered

    def _fetch_selected_trakt_lists(
        self,
        selected_default_lists: list[dict],
        selected_liked_lists: list[dict],
        selected_personal_lists: list[dict],
        selected_public_lists: list[dict],
    ) -> list[tuple[dict, list[dict]]]:
        def trakt_job_key(trakt_list: dict) -> tuple[str, str, str, str, str]:
            return (
                str(trakt_list.get("source", "")),
                str(trakt_list.get("user", "")),
                str(trakt_list.get("slug", "")),
                str(trakt_list.get("catalog_key", "")),
                str(trakt_list.get("name", "")),
            )

        jobs: list[tuple[dict, str]] = []
        for trakt_list in selected_default_lists:
            self._publish_pending_list(trakt_list["name"], trakt_list["name"], "Trakt")
            jobs.append((trakt_list, "default"))
        for trakt_list in selected_liked_lists + selected_personal_lists + selected_public_lists:
            self._publish_pending_list(trakt_list["name"], trakt_list["name"], f"Trakt by {trakt_list['user']}")
            jobs.append((trakt_list, "selected"))
        if not jobs:
            return []

        results_by_key: dict[tuple[str, str, str, str, str], list[dict]] = {}
        pool = ThreadPoolExecutor(max_workers=min(_SOURCE_SYNC_WORKERS, len(jobs)))
        shutdown_wait = True
        try:
            futures = {}
            for trakt_list, mode in jobs:
                key = trakt_job_key(trakt_list)
                if mode == "default":
                    self._set_status(f"Fetching Trakt catalog {trakt_list['name']}")
                    futures[pool.submit(
                        self._trakt.get_default_catalog,
                        trakt_list.get("catalog_key") or trakt_list.get("slug", ""),
                    )] = (key, trakt_list)
                else:
                    self._set_status(f"Fetching Trakt list {trakt_list['name']}")
                    futures[pool.submit(
                        self._trakt.get_list_items,
                        trakt_list["user"],
                        trakt_list["slug"],
                    )] = (key, trakt_list)
            for future in self._iter_completed_futures(futures):
                key, trakt_list = futures[future]
                try:
                    results_by_key[key] = self._filter_trakt_items(future.result() or [])
                except Exception as exc:
                    logger.error("Failed to fetch Trakt source '%s': %s", trakt_list.get("name", ""), exc)
                    results_by_key[key] = []
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

        ordered_results: list[tuple[dict, list[dict]]] = []
        for trakt_list, _ in jobs:
            key = trakt_job_key(trakt_list)
            ordered_results.append((trakt_list, results_by_key.get(key, [])))
        return ordered_results

    def _filter_mdblist_items(self, items: list[dict]) -> list[dict]:
        filtered = []
        for item in items:
            if item["media_type"] == "movie" and "movies" not in self._config.sync.media_types:
                continue
            if item["media_type"] == "tv" and "shows" not in self._config.sync.media_types:
                continue
            filtered.append(item)
        return filtered

    def _fetch_selected_mdblist_lists(self, selected_lists: list[dict]) -> list[tuple[dict, list[dict]]]:
        if not selected_lists:
            return []
        for mdblist in selected_lists:
            self._publish_pending_list(mdblist["name"], mdblist["name"], "MDBList")

        results_by_key: dict[tuple[int, str], list[dict]] = {}
        pool = ThreadPoolExecutor(max_workers=min(_SOURCE_SYNC_WORKERS, len(selected_lists)))
        shutdown_wait = True
        try:
            futures = {
                pool.submit(self._mdblist.get_list_items, mdblist["id"]): mdblist
                for mdblist in selected_lists
            }
            for future in self._iter_completed_futures(futures):
                mdblist = futures[future]
                key = (int(mdblist["id"]), str(mdblist.get("mediatype", "")))
                try:
                    results_by_key[key] = self._filter_mdblist_items(future.result() or [])
                except Exception as exc:
                    logger.error("Failed to fetch MDBList '%s': %s", mdblist.get("name", ""), exc)
                    results_by_key[key] = []
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

        return [
            (mdblist, results_by_key.get((int(mdblist["id"]), str(mdblist.get("mediatype", ""))), []))
            for mdblist in selected_lists
        ]

    @staticmethod
    def _dedupe_trakt_lists(items: list[dict]) -> list[dict]:
        unique: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in items or []:
            user = str(item.get("user", "")).strip()
            slug = str(item.get("slug", "")).strip()
            name = str(item.get("name", "")).strip()
            if not user or not slug or not name:
                continue
            key = (user.lower(), slug.lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    @staticmethod
    def _dedupe_mdblist_lists(items: list[dict]) -> list[dict]:
        unique: list[dict] = []
        seen: set[tuple[int, str]] = set()
        for item in items or []:
            try:
                list_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            mediatype = str(item.get("mediatype", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            if mediatype not in {"movie", "show"} or not name:
                continue
            key = (list_id, mediatype)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _sync_list(
        self,
        source_items: list[dict],
        list_name: str,
        list_description: str,
        display_name: str | None = None,
        source_name: str | None = None,
        is_public: bool = False,
        selection: dict | None = None,
        activities_ts: str = "",
        list_type: str = "custom",
        force_remove_missing: bool = False,
        allow_empty_sync: bool = False,
        managed_keys: frozenset[str] | None = None,
    ) -> SyncStats:
        """Sync a single source list to a PublicMetaDB list."""
        actual_list_name = self._resolve_managed_list_name(list_name, source_name or "", selection)
        row_type = self._infer_row_type(source_name or "", selection, display_name or list_name)
        row_key = self._make_row_key(source_name or "", actual_list_name, row_type, selection)
        stats = SyncStats(
            list_name=actual_list_name,
            display_name=display_name or list_name,
            source_name=source_name or "",
            row_key=row_key,
            row_type=row_type,
            items_fetched=len(source_items),
            activities_ts=str(activities_ts or "").strip(),
        )
        self._remember_progress_row(stats)
        self._set_status(f"Processing {actual_list_name}")
        logger.info("  ┌ '%s'  (%d source items)", actual_list_name, len(source_items))

        should_remove_missing = self._config.sync.remove_missing or force_remove_missing
        source_fingerprint = self._compute_source_fingerprint(source_items, stats.activities_ts)
        previous_state = self._list_state.get(row_key, {})

        if not source_items and not allow_empty_sync:
            logger.debug("  │ No items, skipping '%s'", actual_list_name)
            self._publish_progress([stats], force=True)
            return stats

        if (
            previous_state
            and previous_state.get("fingerprint")
            and previous_state.get("fingerprint") == source_fingerprint
        ):
            stats.items_skipped_fingerprint = len(source_items)
            stats.match_breakdown["skipped:fingerprint"] = len(source_items)
            stats.phase_timings["resolve_seconds"] = 0.0
            stats.phase_timings["pmdb_read_seconds"] = 0.0
            stats.phase_timings["pmdb_write_seconds"] = 0.0
            self._remember_list_state(stats, source_fingerprint, set(previous_state.get("write_keys") or []))
            self._publish_progress([stats], force=True)
            logger.info("  │ Fingerprint match, skipping unchanged row '%s'", actual_list_name)
            return stats

        self._set_status(f"Resolving IDs for {actual_list_name}")
        resolve_started = time.perf_counter()
        stats_snapshot = getattr(self._matcher, "stats_snapshot", None)
        matcher_stats_before = (
            stats_snapshot() if callable(stats_snapshot) else {"lookups": 0, "cache_hits": 0, "failed_cache_hits": 0}
        )
        pmdb_stats_snapshot = getattr(self._pmdb, "stats_snapshot", None)
        pmdb_stats_before = pmdb_stats_snapshot() if callable(pmdb_stats_snapshot) else {}
        resolved: list[dict] = []
        pending_mapping_contributions: list[tuple[int, str, str, str]] = []
        if source_items:
            resolve_pool = ThreadPoolExecutor(max_workers=min(_LIST_RESOLVE_WORKERS, len(source_items)))
            resolve_futures = {
                resolve_pool.submit(self._resolve_match, item): (index, item)
                for index, item in enumerate(source_items)
            }
            try:
                for future in self._iter_completed_futures(resolve_futures):
                    self._check_cancelled()
                    source_index, item = resolve_futures[future]
                    try:
                        match_result = future.result()
                    except SyncCancelled:
                        raise
                    except Exception:
                        stats.items_skipped_unresolved += 1
                        continue
                    tmdb_id = match_result.tmdb_id
                    if tmdb_id is not None:
                        resolved.append({
                            **item,
                            "_syncmeta_source_index": source_index,
                            "resolved_tmdb_id": tmdb_id,
                            "match_confidence": match_result.match_confidence,
                            "anime_mapping_source": match_result.anime_mapping_source,
                        })
                        stats.items_resolved += 1
                        stats.match_breakdown[match_result.resolution_kind] = (
                            int(stats.match_breakdown.get(match_result.resolution_kind, 0)) + 1
                        )
                        if match_result.match_confidence:
                            confidence_key = f"confidence:{match_result.match_confidence}"
                            stats.match_breakdown[confidence_key] = (
                                int(stats.match_breakdown.get(confidence_key, 0)) + 1
                            )
                        if match_result.resolution_kind == "root_series":
                            self._contribute_id_mapping(item, tmdb_id, resolution_kind="root_series")
                    else:
                        stats.items_skipped_unresolved += 1
                        unresolved_reason = match_result.unresolved_reason or "not_found"
                        stats.unresolved_reason_counts[unresolved_reason] = (
                            int(stats.unresolved_reason_counts.get(unresolved_reason, 0)) + 1
                        )
                        unresolved_summary = _unresolved_item_summary(item, list_name=stats.list_name, unresolved_reason=unresolved_reason)
                        unresolved_summary["match_confidence"] = match_result.match_confidence
                        unresolved_summary["anime_mapping_source"] = match_result.anime_mapping_source
                        if match_result.candidate_tmdb_id:
                            unresolved_summary["candidate_tmdb_id"] = match_result.candidate_tmdb_id
                        stats.unresolved_items.append(unresolved_summary)
                        self._append_unique_sample(stats.sample_unresolved_titles, item.get("title") or "Unknown")
                    self._publish_progress([stats])
            except SyncCancelled:
                resolve_pool.shutdown(wait=False, cancel_futures=True)
                raise
            finally:
                resolve_pool.shutdown(wait=False)
        resolved.sort(key=lambda item: int(item.get("_syncmeta_source_index", 0)))
        resolve_elapsed = time.perf_counter() - resolve_started
        matcher_stats_after = (
            stats_snapshot() if callable(stats_snapshot) else {"lookups": 0, "cache_hits": 0, "failed_cache_hits": 0}
        )
        cache_hits = max(0, matcher_stats_after["cache_hits"] - matcher_stats_before["cache_hits"])
        failed_cache_hits = max(
            0,
            matcher_stats_after["failed_cache_hits"] - matcher_stats_before["failed_cache_hits"],
        )
        lookup_count = max(0, matcher_stats_after["lookups"] - matcher_stats_before["lookups"])
        unresolved_rate = (
            (stats.items_skipped_unresolved / stats.items_fetched) if stats.items_fetched else 0.0
        )
        cache_hit_rate = ((cache_hits + failed_cache_hits) / lookup_count) if lookup_count else 0.0
        stats.phase_timings["resolve_seconds"] = round(resolve_elapsed, 4)

        if self._config.sync.dry_run:
            return self._dry_run_report(resolved, actual_list_name, stats)

        try:
            self._set_status(f"Preparing PublicMetaDB list {actual_list_name}")
            pmdb_list = self._get_or_create_pmdb_list_cached(actual_list_name, list_description, is_public=is_public, list_type=list_type)
            self._register_managed_list(
                actual_list_name,
                str(pmdb_list.get("id", "")),
                display_name or list_name,
                source_name or "",
                selection,
            )
        except SyncCancelled:
            raise
        except Exception as exc:
            self._record_error(stats, "pmdb_list", f"Failed to get/create list: {exc}")
            logger.error("Failed to get/create list '%s': %s", actual_list_name, exc)
            self._publish_progress([stats], force=True)
            return stats

        list_id = pmdb_list["id"]

        self._set_status(f"Loading existing items from {actual_list_name}")
        pmdb_read_started = time.perf_counter()
        existing_items = self._get_cached_list_items(list_id)
        existing_map = self._build_existing_map(existing_items)
        pmdb_read_elapsed = time.perf_counter() - pmdb_read_started
        stats.phase_timings["pmdb_read_seconds"] = round(pmdb_read_elapsed, 4)
        desired_keys: set[str] = set()
        desired_key_owners: dict[str, dict] = {}  # key → first item that claimed it
        pending_adds: list[tuple[dict, int, str, str]] = []
        for item in resolved:
            self._check_cancelled()
            tmdb_id = item["resolved_tmdb_id"]
            media_type = item["media_type"]
            key = f"{tmdb_id}:{media_type}"
            if key in desired_keys:
                owner = desired_key_owners.get(key, {})
                owner_root = owner.get("root_anilist_id") or owner.get("root_mal_id") or owner.get("anilist_id") or owner.get("mal_id")
                this_root = item.get("root_anilist_id") or item.get("root_mal_id") or item.get("anilist_id") or item.get("mal_id")
                is_collision = bool(owner_root and this_root and owner_root != this_root)
                if is_collision:
                    # Different source IDs mapping to the same TMDB entry — bad
                    # community mapping.  Flag as collision so the user can fix it.
                    stats.items_skipped_unresolved += 1
                    unresolved_summary = _unresolved_item_summary(item, list_name=stats.list_name, unresolved_reason="tmdb_collision")
                    unresolved_summary["candidate_tmdb_id"] = tmdb_id
                    stats.unresolved_items.append(unresolved_summary)
                    self._append_unique_sample(stats.sample_unresolved_titles, item.get("title") or "Unknown")
                    logger.warning(
                        "TMDB collision: '%s' and '%s' both map to %s:%s",
                        owner.get("title"), item.get("title"), tmdb_id, media_type,
                    )
                else:
                    # Same franchise or no IDs to compare — legitimate duplicate.
                    stats.items_skipped_duplicate += 1
                self._publish_progress([stats])
                continue
            desired_keys.add(key)
            desired_key_owners[key] = item

            if key in existing_map:
                stats.items_skipped_duplicate += 1
                self._publish_progress([stats])
                continue
            pending_adds.append((item, tmdb_id, media_type, key))

        # Inject manually-resolved items so remove_missing never evicts them.
        if self._should_preserve_manual_list_additions(source_name or "", selection):
            for manual_entry in self._manual_list_additions.get(actual_list_name, []):
                m_tmdb_id = manual_entry.get("tmdb_id")
                m_media_type = manual_entry.get("media_type") or "movie"
                if not m_tmdb_id:
                    continue
                m_key = f"{m_tmdb_id}:{m_media_type}"
                desired_keys.add(m_key)
                if m_key not in existing_map:
                    synthetic = {"title": f"manual:{m_tmdb_id}", "media_type": m_media_type}
                    pending_adds.append((synthetic, m_tmdb_id, m_media_type, m_key))

        pmdb_write_started = time.perf_counter()
        if pending_adds:
            self._set_status(f"Adding items to {actual_list_name}")
            pool = ThreadPoolExecutor(max_workers=min(_LIST_WRITE_WORKERS, len(pending_adds)))
            shutdown_wait = True
            try:
                futures = {
                    pool.submit(self._pmdb.add_item_to_list, list_id, tmdb_id, media_type): (item, tmdb_id, media_type, key)
                    for item, tmdb_id, media_type, key in pending_adds
                }
                for future in self._iter_completed_futures(futures):
                    item, tmdb_id, media_type, key = futures[future]
                    try:
                        result = future.result()
                        cached_item = {
                            "tmdb_id": tmdb_id,
                            "media_type": media_type,
                        }
                        if isinstance(result, dict):
                            cached_item.update(result)
                        existing_map[key] = cached_item
                        self._record_cached_list_item_add(list_id, cached_item)
                        stats.items_added += 1
                        logger.info(
                            "  │ + '%s' (tmdb %s, %s) → '%s'",
                            item.get("title") or f"tmdb {tmdb_id}", tmdb_id, media_type, actual_list_name,
                        )
                    except SyncCancelled:
                        raise
                    except Exception as exc:
                        self._record_error(
                            stats,
                            "pmdb_write",
                            f"Failed to add '{item['title']}' (tmdb={tmdb_id}): {exc}",
                            item_title=item.get("title", "Unknown"),
                        )
                    self._publish_progress([stats], force=True)
            except SyncCancelled:
                shutdown_wait = False
                raise
            finally:
                pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

        # Expose the keys this sync owns so callers (e.g. watchlist) can persist them.
        stats.synced_keys = sorted(desired_keys)

        if should_remove_missing:
            self._set_status(f"Removing stale items from {actual_list_name}")
            stats.items_removed = self._remove_stale(list_id, existing_items, desired_keys, managed_keys)
            self._publish_progress([stats], force=True)

        if pending_mapping_contributions:
            self._set_status(f"Contributing PMDB mappings for {actual_list_name}")
            self._flush_id_mapping_contributions(pending_mapping_contributions)

        pmdb_write_elapsed = time.perf_counter() - pmdb_write_started
        stats.phase_timings["pmdb_write_seconds"] = round(pmdb_write_elapsed, 4)
        pmdb_stats_after = pmdb_stats_snapshot() if callable(pmdb_stats_snapshot) else {}
        if pmdb_stats_before or pmdb_stats_after:
            stats.pmdb_metrics = self._delta_counter_snapshot(pmdb_stats_after, pmdb_stats_before)
        self._remember_list_state(stats, source_fingerprint, desired_keys)

        logger.info(
            "  └ '%s'  resolved=%d  added=%d  dup=%d  unresolved=%d  removed=%d%s  [resolve=%.2fs pmdb_read=%.2fs pmdb_write=%.2fs cache_hit=%.0f%% unresolved=%.0f%%]",
            actual_list_name,
            stats.items_resolved,
            stats.items_added,
            stats.items_skipped_duplicate,
            stats.items_skipped_unresolved,
            stats.items_removed,
            f"  ⚠ {len(stats.errors)} error(s)" if stats.errors else "",
            resolve_elapsed,
            pmdb_read_elapsed,
            pmdb_write_elapsed,
            cache_hit_rate * 100.0,
            unresolved_rate * 100.0,
        )
        return stats

    def _prewarm_simkl_anime_cache(self) -> None:
        """Pre-warm the shared AniList prequel-chain cache for SIMKL anime items.

        Fetches all SIMKL anime IDs from the configured statuses, filters to
        those not already cached, then concurrently walks their prequel chains.
        This ensures the shared cache is hot before the per-status list sync
        loop runs, so each item resolves instantly via cache hits.
        """
        from .anilist_client import _SHARED_ROOT_CONTEXT_CACHE

        anime_statuses = self._config.simkl.selected_statuses.get("anime", [])
        if not anime_statuses:
            return

        # Collect all unique AniList IDs from every SIMKL anime status.
        anilist_ids: set[int] = set()
        try:
            for status_key in anime_statuses:
                grouped = self._simkl.get_status(status_key, ["anime"])
                for item in grouped.get("anime", []):
                    aid = item.get("anilist_id")
                    if aid:
                        try:
                            anilist_ids.add(int(aid))
                        except (TypeError, ValueError):
                            pass
        except Exception as exc:
            logger.debug("SIMKL anime pre-warm fetch failed: %s", exc)
            return

        self._prewarm_simkl_anime_ids([aid for aid in anilist_ids if aid not in _SHARED_ROOT_CONTEXT_CACHE])

    def _prewarm_simkl_anime_ids(self, uncached: list[int]) -> None:
        if not uncached:
            logger.info("SIMKL anime chain cache: all requested IDs already cached")
            return

        logger.info(
            "Pre-warming SIMKL anime chain cache: %d candidate IDs",
            len(uncached),
        )
        get_ctx = getattr(self._anilist_root_client, "_get_root_context", None)
        if not callable(get_ctx):
            return

        pool = ThreadPoolExecutor(max_workers=min(_PREWARM_WORKERS, 4))
        shutdown_wait = True
        try:
            futures = {pool.submit(get_ctx, aid): aid for aid in uncached}
            for future in self._iter_completed_futures(futures):
                try:
                    future.result()
                except Exception as exc:
                    logger.debug("Chain pre-warm failed for anilist_id=%s: %s", futures[future], exc)
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

    def _collect_id_mapping_contributions(
        self,
        item: dict,
        tmdb_id: int,
        resolution_kind: str | None = None,
    ) -> list[tuple[int, str, str, str]]:
        return []

    def _flush_id_mapping_contributions(self, contributions: list[tuple[int, str, str, str]]) -> None:
        return

    def _contribute_id_mapping(self, item: dict, tmdb_id: int, resolution_kind: str | None = None) -> None:
        if resolution_kind != "root_series":
            return
        media_type = item.get("media_type", "tv")
        root_anilist = item.get("root_anilist_id") or str(item.get("ids", {}).get("root_anilist") or "")
        root_mal = item.get("root_mal_id") or str(item.get("ids", {}).get("root_mal") or "")
        for id_type, id_value in [("anilist", root_anilist), ("mal", root_mal)]:
            if id_value:
                try:
                    self._pmdb.create_id_mapping(tmdb_id, media_type, id_type, str(id_value))
                except Exception as exc:
                    logger.debug("Could not contribute %s ID mapping: %s", id_type, exc)

    def _should_backfill_pmdb_mapping(self, item: dict) -> bool:
        return False

    def _dry_run_report(self, resolved: list[dict], list_name: str, stats: SyncStats) -> SyncStats:
        logger.info(
            "  └ [DRY RUN] '%s'  would add=%d  unresolved=%d",
            list_name,
            len(resolved),
            stats.items_skipped_unresolved,
        )
        preview: list[dict] = []
        for item in resolved:
            logger.debug(
                "    [DRY RUN] %s  year=%s  tmdb=%s  type=%s",
                item["title"],
                item.get("year"),
                item["resolved_tmdb_id"],
                item["media_type"],
            )
            if len(preview) < _DRY_RUN_PREVIEW_LIMIT:
                preview.append({
                    "title": item.get("title") or "Unknown",
                    "year": item.get("year"),
                    "tmdb_id": item.get("resolved_tmdb_id"),
                    "media_type": item.get("media_type"),
                })
        stats.dry_run_preview = preview
        return stats

    def _write_watched_history_items(
        self,
        items: list[dict],
        existing_counts: dict[str, int],
        stats: SyncStats,
        status_message: str,
        total_source: int = 0,
    ) -> None:
        if not items:
            return

        existing_counts_before = {
            str(key): int(value or 0)
            for key, value in (existing_counts or {}).items()
            if key
        }
        deduped_items: list[dict] = []
        pending_seen: set[str] = set()
        pending_identity_keys: set[str] = set()
        for item in items:
            key = self._watched_write_key(item)
            if key and key in pending_seen:
                stats.items_skipped_duplicate += 1
                continue
            if key:
                pending_seen.add(key)
            identity_key = self._watched_identity_key(item)
            if identity_key:
                pending_identity_keys.add(identity_key)
            deduped_items.append(item)

        items = deduped_items
        if not items:
            return

        total_to_write = len(items)
        written = 0
        successful_writes = 0
        self._set_status(f"{status_message} (0/{total_to_write} new)")
        pool = ThreadPoolExecutor(max_workers=min(_ACTIVITY_WRITE_WORKERS, len(items)))
        shutdown_wait = True
        try:
            futures = {
                pool.submit(
                    self._pmdb.mark_watched,
                    tmdb_id=_coerce_tmdb_id(item.get("tmdb_id"), item.get("media_type") or "") or 0,
                    media_type=item["media_type"],
                    season=item.get("season"),
                    episode=item.get("episode"),
                    watched_at=item.get("watched_at"),
                    dedupe=True,
                ): item
                for item in items
            }
            for future in self._iter_completed_futures(futures):
                item = futures[future]
                key = self._watched_identity_key(item)
                try:
                    future.result()
                    if key:
                        existing_counts[key] = int(existing_counts.get(key, 0) or 0) + 1
                    stats.items_added += 1
                    successful_writes += 1
                    written += 1
                    episode_suffix = (
                        f" S{item['season']}E{item['episode']}"
                        if item.get("season") is not None and item.get("episode") is not None
                        else ""
                    )
                    logger.info(
                        "  │ ✓ Watched '%s'%s (tmdb %s, %s)",
                        item.get("title") or f"tmdb {item.get('tmdb_id')}",
                        episode_suffix, item.get("tmdb_id"), item.get("media_type"),
                    )
                    if written % 10 == 0 or written == total_to_write:
                        self._set_status(f"{status_message} ({written}/{total_to_write} new)")
                        self._publish_progress([stats])
                except SyncCancelled:
                    raise
                except Exception as exc:
                    self._record_error(
                        stats,
                        "pmdb_write",
                        f"Failed to import watched item '{item.get('title', 'Unknown')}': {exc}",
                        item_title=item.get("title", "Unknown"),
                    )
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

        if not successful_writes:
            return

        try:
            refreshed_counts = self._verify_watched_history_counts(existing_counts_before, pending_identity_keys)
        except Exception:
            logger.warning("Could not verify PMDB watched history totals after write", exc_info=True)
            return

        actual_added = 0
        for key in pending_identity_keys:
            actual_added += max(0, int(refreshed_counts.get(key, 0) or 0) - int(existing_counts_before.get(key, 0) or 0))

        if actual_added < successful_writes:
            corrected_duplicates = successful_writes - actual_added
            stats.items_added = max(0, stats.items_added - corrected_duplicates)
            stats.items_skipped_duplicate += corrected_duplicates
            logger.info(
                "Adjusted watched history import stats after PMDB verification: %d write(s) were already present server-side",
                corrected_duplicates,
            )

        for key in pending_identity_keys:
            if key:
                existing_counts[key] = int(refreshed_counts.get(key, 0) or 0)

    def _verify_watched_history_counts(
        self,
        existing_counts_before: dict[str, int],
        pending_identity_keys: set[str],
    ) -> dict[str, int]:
        last_counts: dict[str, int] = {}
        for attempt in range(_WATCHED_HISTORY_VERIFY_RETRIES):
            refreshed_counts = self._count_watched_history_identities(self._pmdb.get_watched_history())
            last_counts = refreshed_counts
            if self._watched_history_verification_complete(
                refreshed_counts,
                existing_counts_before,
                pending_identity_keys,
            ):
                return refreshed_counts
            if attempt < _WATCHED_HISTORY_VERIFY_RETRIES - 1:
                time.sleep(_WATCHED_HISTORY_VERIFY_DELAY_SECONDS * (attempt + 1))
        return last_counts

    @staticmethod
    def _watched_history_verification_complete(
        refreshed_counts: dict[str, int],
        existing_counts_before: dict[str, int],
        pending_identity_keys: set[str],
    ) -> bool:
        for key in pending_identity_keys:
            before_count = int(existing_counts_before.get(key, 0) or 0)
            after_count = int(refreshed_counts.get(key, 0) or 0)
            if after_count <= before_count:
                return False
        return True

    def _remove_stale(
        self,
        list_id: str,
        existing_items: list[dict],
        desired_keys: set[str],
        managed_keys: frozenset[str] | None = None,
    ) -> int:
        stale_items: list[dict] = []
        for item in existing_items:
            self._check_cancelled()
            key = f"{item.get('tmdb_id')}:{item.get('media_type')}"
            if key not in desired_keys:
                # When managed_keys is provided AND non-empty, only remove items that
                # SyncMeta previously added.  Items added directly in PMDB are not in
                # managed_keys and are therefore preserved.
                # An empty managed_keys means this is the first sync (no history yet),
                # so fall back to removing all stale items as usual.
                if managed_keys:
                    if key not in managed_keys:
                        continue
                stale_items.append(item)

        removed = 0
        if stale_items:
            pool = ThreadPoolExecutor(max_workers=min(_LIST_WRITE_WORKERS, len(stale_items)))
            shutdown_wait = True
            try:
                futures = {
                    pool.submit(self._pmdb.remove_item_from_list, list_id, item["id"]): item
                    for item in stale_items
                }
                for future in self._iter_completed_futures(futures):
                    item = futures[future]
                    try:
                        future.result()
                        removed += 1
                        self._record_cached_list_item_remove(list_id, item.get("id", ""))
                        logger.info(
                            "  │ − '%s' (tmdb %s, %s) removed as stale",
                            item.get("title") or item.get("name") or f"tmdb {item.get('tmdb_id')}",
                            item.get("tmdb_id"), item.get("media_type"),
                        )
                    except SyncCancelled:
                        raise
                    except Exception as exc:
                        logger.error("Failed to remove item %s: %s", item.get("id"), exc)
            except SyncCancelled:
                shutdown_wait = False
                raise
            finally:
                pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)
        if removed:
            logger.info("  │ Removed %d stale item(s) from list %s", removed, list_id)
        return removed

    @staticmethod
    def _should_preserve_manual_list_additions(source_name: str, selection: dict | None) -> bool:
        # Manual list additions must always be preserved regardless of source or
        # media type.  Previously SIMKL-anime lists were excluded here, which
        # meant that items manually mapped via the "Map" button were evicted from
        # the PMDB list on every subsequent sync (because force_remove_missing=True
        # for anime and the auto-resolver still couldn't resolve the item).
        # AniList lists are excluded only because they manage their own additions
        # through the AniList sync path, not via the manual-map flow.
        source = str((selection or {}).get("source") or source_name or "").strip().lower()
        if source == "anilist":
            return False
        return True


    @staticmethod
    def _build_existing_map(items: list[dict]) -> dict[str, dict]:
        result = {}
        for item in items:
            tmdb_id = item.get("tmdb_id")
            media_type = item.get("media_type")
            if tmdb_id and media_type:
                result[f"{tmdb_id}:{media_type}"] = item
        return result

    @staticmethod
    def _log_results(all_stats: list[SyncStats]) -> None:
        total_added = sum(s.items_added for s in all_stats)
        total_removed = sum(s.items_removed for s in all_stats)
        total_errors = sum(len(s.errors) for s in all_stats)

        logger.info("── Sync Summary ───────────────────────────────────────────")
        for stats in all_stats:
            if not stats.list_name and not stats.items_fetched and not stats.items_added and not stats.errors:
                continue
            parts = [f"added={stats.items_added}"]
            if stats.items_removed:
                parts.append(f"removed={stats.items_removed}")
            if stats.items_skipped_duplicate:
                parts.append(f"dup={stats.items_skipped_duplicate}")
            if stats.items_skipped_unresolved:
                parts.append(f"unresolved={stats.items_skipped_unresolved}")
            if stats.errors:
                parts.append(f"⚠ errors={len(stats.errors)}")
            logger.info("  %-45s %s", f"'{stats.display_name or stats.list_name}'", "  ".join(parts))
            for err in stats.errors:
                logger.error("    ✗ %s", err)

        logger.info("── Total: added=%d  removed=%d  errors=%d ─────────────────",
                    total_added, total_removed, total_errors)
        logger.info("▶  SYNC COMPLETE")
        logger.info("═" * 60)

    def _set_status(self, status: str) -> None:
        self._check_cancelled()
        if self._status_callback:
            try:
                self._status_callback(status)
            except Exception:
                logger.debug("Status callback failed", exc_info=True)

    def _publish_progress(self, rows: list[SyncStats], force: bool = False) -> None:
        if not self._progress_callback:
            return
        now = time.monotonic()
        if not force and now - self._last_progress_publish < 5.0:
            return
        try:
            for row in rows:
                self._remember_progress_row(row)
            payload = list(self._live_progress_rows.values())
            self._progress_callback(payload)
            self._last_progress_publish = now
        except Exception:
            logger.debug("Progress callback failed", exc_info=True)

    def _remember_progress_row(self, row: SyncStats) -> None:
        key = f"{row.display_name or row.list_name or ''}|{row.source_name or ''}"
        self._live_progress_rows[key] = {
            "list_name": row.list_name,
            "display_name": row.display_name,
            "source_name": row.source_name,
            "row_key": row.row_key,
            "row_type": row.row_type,
            "has_details": bool(row.errors or row.unresolved_items or row.sample_failed_titles or row.sample_unresolved_titles),
            "items_fetched": row.items_fetched,
            "items_resolved": row.items_resolved,
            "items_added": row.items_added,
            "items_removed": row.items_removed,
            "items_skipped_duplicate": row.items_skipped_duplicate,
            "items_skipped_unresolved": row.items_skipped_unresolved,
            "items_skipped_fingerprint": row.items_skipped_fingerprint,
            "error_count": len(row.errors),
            "phase_timings": dict(row.phase_timings),
            "match_breakdown": dict(row.match_breakdown),
            "unresolved_reason_counts": dict(row.unresolved_reason_counts),
            "pmdb_metrics": dict(row.pmdb_metrics),
        }

    def _publish_pending_list(self, list_name: str, display_name: str, source_name: str) -> None:
        self._publish_progress([
            SyncStats(
                list_name=list_name,
                display_name=display_name,
                source_name=source_name,
                items_fetched=0,
            )
        ], force=True)

    def _prime_pmdb_list_index(self) -> None:
        refresh = getattr(self._pmdb, "refresh_lists_index", None)
        if not callable(refresh):
            return
        try:
            index = refresh() or {}
        except Exception as exc:
            logger.debug("Could not prefetch PMDB list index: %s", exc)
            return
        with self._pmdb_cache_lock:
            self._pmdb_run_list_index = dict(index)

    def _get_or_create_pmdb_list_cached(self, name: str, description: str, is_public: bool = False, list_type: str = "custom") -> dict:
        lookup_name = str(name or "").strip()
        with self._pmdb_cache_lock:
            # For watchlist type, search by type key in cache
            if list_type == "watchlist":
                existing = None
                if self._pmdb_run_list_index is not None:
                    for item in self._pmdb_run_list_index.values():
                        if str(item.get("type", "")).lower() == "watchlist":
                            existing = item
                            break
            else:
                existing = None if self._pmdb_run_list_index is None else self._pmdb_run_list_index.get(lookup_name)
        if existing:
            return dict(existing)
        try:
            pmdb_list = self._pmdb.get_or_create_list(name, description, is_public=is_public, list_type=list_type)
        except TypeError:
            pmdb_list = self._pmdb.get_or_create_list(name, description, is_public=is_public)
        with self._pmdb_cache_lock:
            if self._pmdb_run_list_index is None:
                self._pmdb_run_list_index = {}
            cache_key = str(pmdb_list.get("name", lookup_name)).strip() or lookup_name
            self._pmdb_run_list_index[cache_key] = dict(pmdb_list)
        return pmdb_list

    def _get_cached_list_items(self, list_id: str) -> list[dict]:
        with self._pmdb_cache_lock:
            cached = self._pmdb_list_items_cache.get(str(list_id))
        if cached is not None:
            return [dict(item) for item in cached]
        items = list(self._pmdb.get_list_items(list_id))
        with self._pmdb_cache_lock:
            self._pmdb_list_items_cache[str(list_id)] = [dict(item) for item in items]
        return [dict(item) for item in items]

    def _record_cached_list_item_add(self, list_id: str, item: dict) -> None:
        with self._pmdb_cache_lock:
            cached = self._pmdb_list_items_cache.get(str(list_id))
            if cached is None:
                return
            cached.append(dict(item))

    def _record_cached_list_item_remove(self, list_id: str, item_id: str) -> None:
        with self._pmdb_cache_lock:
            cached = self._pmdb_list_items_cache.get(str(list_id))
            if cached is None:
                return
            self._pmdb_list_items_cache[str(list_id)] = [
                dict(item) for item in cached
                if str(item.get("id", "")) != str(item_id)
            ]

    @staticmethod
    def _delta_counter_snapshot(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
        return {
            key: max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
            for key in sorted(set(before) | set(after))
        }

    def _iter_completed_futures(self, futures) -> object:
        pending = set(futures.keys() if isinstance(futures, dict) else futures)
        while pending:
            self._check_cancelled()
            done, pending = wait(
                pending,
                timeout=_FUTURE_POLL_INTERVAL,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                yield future
        self._check_cancelled()

    def _check_cancelled(self) -> None:
        if not self._cancel_requested_callback:
            return
        try:
            if self._cancel_requested_callback():
                raise SyncCancelled("Sync stopped by user")
        except SyncCancelled:
            raise
        except Exception:
            logger.debug("Cancel callback failed", exc_info=True)

    @staticmethod
    def _watched_key(item: dict) -> str:
        tmdb_id = item.get("tmdb_id")
        media_type = item.get("media_type")
        if not tmdb_id or not media_type:
            return ""
        season = item.get("season")
        episode = item.get("episode")
        watched_at = item.get("watched_at")
        return f"{tmdb_id}:{media_type}:{season if season is not None else ''}:{episode if episode is not None else ''}:{watched_at if watched_at is not None else 'null'}"

    @staticmethod
    def _watched_identity_key(item: dict) -> str:
        tmdb_id = item.get("tmdb_id")
        media_type = item.get("media_type")
        if not tmdb_id or not media_type:
            return ""
        season = item.get("season")
        episode = item.get("episode")
        return f"{tmdb_id}:{media_type}:{season if season is not None else ''}:{episode if episode is not None else ''}"

    def _count_watched_history_identities(self, items: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items or []:
            key = self._watched_identity_key(item)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _watched_write_key(item: dict) -> str:
        identity_key = SyncService._watched_identity_key(item)
        if not identity_key:
            return ""
        watched_at = str(item.get("watched_at") or "").strip()
        return f"{identity_key}:{watched_at}"

    @staticmethod
    def _resume_key(item: dict) -> str:
        tmdb_id = item.get("tmdb_id")
        media_type = item.get("media_type")
        if not tmdb_id or not media_type:
            return ""
        season = item.get("season")
        episode = item.get("episode")
        return f"{tmdb_id}:{media_type}:{season if season is not None else ''}:{episode if episode is not None else ''}"

    @staticmethod
    def _resume_matches(existing: dict, payload: dict) -> bool:
        return (
            int(existing.get("position_ms", 0) or 0) == int(payload.get("position_ms", 0) or 0)
            and int(existing.get("runtime_ms", 0) or 0) == int(payload.get("runtime_ms", 0) or 0)
        )

    @staticmethod
    def _increment_history_source_seen(source_seen: dict[str, int], key: str) -> int:
        next_count = source_seen.get(key, 0) + 1
        source_seen[key] = next_count
        return next_count

    def _can_fast_skip_history_item(self, item: dict) -> bool:
        if not self._watched_identity_key(item):
            return False
        if item.get("aggregate_watched_count"):
            return False
        if str(item.get("simkl_type", "")).strip().lower() == "anime":
            return False
        if self._should_force_anime_re_resolve(item):
            return False
        return True

    def _fast_skip_existing_history_item(
        self,
        item: dict,
        existing_counts: dict[str, int],
        source_seen: dict[str, int],
        stats: SyncStats,
    ) -> bool:
        if not self._can_fast_skip_history_item(item):
            return False
        key = self._watched_identity_key(item)
        if not key:
            return False
        seen_count = self._increment_history_source_seen(source_seen, key)
        if existing_counts.get(key, 0) >= seen_count:
            stats.items_resolved += 1
            stats.items_skipped_duplicate += 1
            return True
        return False

    def _resolve_activity_item(self, item: dict) -> dict:
        if self._should_force_anime_re_resolve(item):
            match_result = self._resolve_match(item)
            tmdb_id = match_result.tmdb_id
            if tmdb_id is not None:
                if self._should_backfill_pmdb_mapping(item):
                    self._contribute_id_mapping(item, tmdb_id, resolution_kind=match_result.resolution_kind)
                return {
                    **item,
                    "tmdb_id": tmdb_id,
                }
        direct_tmdb_id = _coerce_tmdb_id(item.get("tmdb_id"), item.get("media_type") or "")
        if direct_tmdb_id:
            if self._should_backfill_pmdb_mapping(item):
                try:
                    self._contribute_id_mapping(item, direct_tmdb_id, resolution_kind="direct_tmdb")
                except (TypeError, ValueError):
                    pass
            return {**item, "tmdb_id": direct_tmdb_id}
        match_result = self._resolve_match(item)
        tmdb_id = match_result.tmdb_id
        if tmdb_id is None:
            return item
        if self._should_backfill_pmdb_mapping(item):
            self._contribute_id_mapping(item, tmdb_id, resolution_kind=match_result.resolution_kind)
        return {
            **item,
            "tmdb_id": tmdb_id,
        }

    @staticmethod
    def _should_force_anime_re_resolve(item: dict) -> bool:
        return (
            str(item.get("simkl_type", "")).strip().lower() == "anime"
            and str(item.get("media_type", "")).strip().lower() == "tv"
            and not (item.get("aggregate_watched_count") and item.get("tmdb_id"))
            and any(item.get(key) for key in ("anilist_id", "root_anilist_id", "mal_id", "root_mal_id"))
        )

    def _dedupe_activity_history_items(self, items: list[dict]) -> list[dict]:
        deduped: list[dict] = []
        seen: set[str] = set()
        for item in items:
            key = self._watched_identity_key(item)
            if not key:
                key = (
                    f"{item.get('title', '')}:"
                    f"{item.get('watched_at', '')}:"
                    f"{item.get('aggregate_watched_count', '')}:"
                    f"{item.get('simkl_type', '')}"
                )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _chunked(items: list[dict], size: int) -> list[list[dict]]:
        return [items[index:index + size] for index in range(0, len(items), size)]

    def _merge_activity_stats(self, rows: list[SyncStats]) -> list[SyncStats]:
        grouped: dict[str, SyncStats] = {}

        for row in rows:
            if "Watch History" in row.display_name:
                key = "watch_history"
                display_name = "Watch History"
            elif "Resume Progress" in row.display_name:
                key = "resume_progress"
                display_name = "Resume Progress"
            else:
                continue

            aggregate = grouped.get(key)
            if not aggregate:
                aggregate = SyncStats(
                    list_name="",
                    display_name=display_name,
                    source_name="",
                    row_key=self._make_row_key("activity", display_name, key, {"mode": key}),
                    row_type="history" if key == "watch_history" else "resume",
                )
                grouped[key] = aggregate

            aggregate.items_fetched += row.items_fetched
            aggregate.items_resolved += row.items_resolved
            aggregate.items_added += row.items_added
            aggregate.items_removed += row.items_removed
            aggregate.items_skipped_duplicate += row.items_skipped_duplicate
            aggregate.items_skipped_unresolved += row.items_skipped_unresolved
            aggregate.items_skipped_fingerprint += row.items_skipped_fingerprint
            aggregate.items_reconciled += row.items_reconciled
            aggregate.errors.extend(list(row.errors))
            for key, value in (row.error_stage_counts or {}).items():
                aggregate.error_stage_counts[key] = int(aggregate.error_stage_counts.get(key, 0)) + int(value or 0)
            for title in row.sample_failed_titles or []:
                SyncService._append_unique_sample(aggregate.sample_failed_titles, title)
            for title in row.sample_unresolved_titles or []:
                SyncService._append_unique_sample(aggregate.sample_unresolved_titles, title)
            aggregate.row_type = row.row_type or aggregate.row_type
            aggregate.row_key = row.row_key or aggregate.row_key
            if row.history_cursor:
                aggregate.history_cursor = row.history_cursor

            existing_sources = {part.strip() for part in aggregate.source_name.split("+") if part.strip()}
            if row.source_name:
                existing_sources.add(row.source_name)
            aggregate.source_name = " + ".join(sorted(existing_sources))

        return [grouped[key] for key in ("watch_history", "resume_progress") if key in grouped]

    def _fetch_simkl_activities_ts(self) -> str:
        """Return SIMKL's top-level 'all' activities timestamp, or '' on failure."""
        if not self._config.simkl.access_token:
            return ""
        try:
            data = self._simkl.get_activities() or {}
            ts = str(data.get("all", "") or "").strip()
            return ts
        except Exception as exc:
            logger.debug("Could not fetch SIMKL activities: %s", exc)
            return ""

    def _fetch_trakt_activities_ts(self) -> str:
        """Return Trakt's top-level 'all' last_activities timestamp, or '' on failure."""
        if not self._config.trakt.enabled:
            return ""
        try:
            data = self._trakt.get_last_activities()
            ts = str(data.get("all", "") or "").strip()
            return ts
        except Exception as exc:
            logger.debug("Could not fetch Trakt last_activities: %s", exc)
            return ""

    @staticmethod
    def _latest_history_cursor(items: list[dict], existing_cursor: str = "") -> str:
        latest = str(existing_cursor).strip()
        for item in items or []:
            watched_at = str(item.get("watched_at", "") or "").strip()
            if watched_at and watched_at > latest:
                latest = watched_at
        return latest

    @staticmethod
    def _normalize_sync_modes(sync_modes: dict | None, config: AppConfig | None = None) -> dict[str, bool]:
        if sync_modes is None:
            raw = {
                "lists": True,
                "history": bool((config.sync.simkl_sync_watched_history or config.sync.trakt_sync_watched_history)) if config else False,
                "resume": bool(config.sync.trakt_sync_resume_progress) if config else False,
            }
        else:
            raw = sync_modes
        return {
            "lists": bool(raw.get("lists", True)),
            "history": bool(raw.get("history", False)),
            "resume": bool(raw.get("resume", False)),
        }

    @staticmethod
    def _normalize_managed_lists(items: list[dict] | None) -> dict[str, dict]:
        normalized: dict[str, dict] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            list_name = str(item.get("list_name", "")).strip()
            if not list_name:
                continue
            normalized[list_name] = {
                "list_name": list_name,
                "list_id": str(item.get("list_id", "")).strip(),
                "display_name": str(item.get("display_name", "")).strip(),
                "source_name": str(item.get("source_name", "")).strip(),
                "selection": dict(item.get("selection", {})) if isinstance(item.get("selection"), dict) else {},
            }
        return normalized

    @staticmethod
    def _selection_identity(selection: dict | None) -> str:
        if not isinstance(selection, dict):
            return ""
        source = str(selection.get("source", "")).strip().lower()
        if source == "simkl":
            return f"simkl:{selection.get('media_type', '')}:{selection.get('status', '')}"
        if source == "anilist":
            return f"anilist:{selection.get('status', '')}"
        if source == "mdblist":
            return f"mdblist:{selection.get('id', '')}:{selection.get('mediatype', '')}"
        if source == "trakt":
            kind = str(selection.get("kind", "")).strip().lower()
            if kind == "watchlist":
                return f"trakt:watchlist:{selection.get('media_type', '')}"
            if kind == "default":
                return f"trakt:default:{selection.get('catalog_key', '')}"
            return f"trakt:list:{selection.get('user', '')}:{selection.get('slug', '')}"
        return ""

    @staticmethod
    def _selection_suffix(source_name: str, selection: dict | None) -> str:
        if not isinstance(selection, dict):
            return source_name or "SyncMeta"
        source = str(selection.get("source", "")).strip().lower()
        if source == "trakt":
            kind = str(selection.get("kind", "")).strip().lower()
            if kind == "watchlist":
                media_type = str(selection.get("media_type", "")).strip().lower()
                label = _TYPE_LABELS.get(media_type, media_type.title() or "Watchlist")
                return f"Trakt {label}"
            if kind == "default":
                return "Trakt"
            user = str(selection.get("user", "")).strip()
            return f"Trakt {user}" if user else "Trakt"
        if source == "mdblist":
            return "MDBList"
        if source == "anilist":
            return "AniList"
        if source == "simkl":
            return "SIMKL"
        return source_name or "SyncMeta"

    def _resolve_managed_list_name(self, base_name: str, source_name: str, selection: dict | None) -> str:
        with self._managed_lists_lock:
            return self._resolve_managed_list_name_locked(base_name, source_name, selection)

    def _resolve_managed_list_name_locked(self, base_name: str, source_name: str, selection: dict | None) -> str:
        identity = self._selection_identity(selection)
        if identity:
            for item in self._managed_lists.values():
                if self._selection_identity(item.get("selection")) == identity:
                    return str(item.get("list_name", "")).strip() or base_name

        candidate = base_name
        suffix = self._selection_suffix(source_name, selection)
        attempt = 0

        while True:
            conflict = False
            existing_managed = self._managed_lists.get(candidate)
            if existing_managed and self._selection_identity(existing_managed.get("selection")) != identity:
                conflict = True
            elif attempt == 0:
                try:
                    existing_pmdb = self._pmdb.find_list_by_name(candidate)
                except Exception:
                    existing_pmdb = None
                if existing_pmdb:
                    existing_id = str(existing_pmdb.get("id", "")).strip()
                    managed_match = any(str(item.get("list_id", "")).strip() == existing_id for item in self._managed_lists.values())
                    if not managed_match:
                        conflict = True

            if not conflict:
                # Pre-register a placeholder so concurrent threads see this name
                # is taken and pick a disambiguated variant.
                self._managed_lists[candidate] = {
                    "list_name": candidate,
                    "list_id": "",
                    "display_name": base_name,
                    "source_name": source_name,
                    "selection": dict(selection or {}),
                }
                return candidate

            attempt += 1
            candidate = f"{base_name} ({suffix})" if attempt == 1 else f"{base_name} ({suffix} {attempt})"

    def _register_managed_list(
        self,
        list_name: str,
        list_id: str,
        display_name: str,
        source_name: str,
        selection: dict | None = None,
    ) -> None:
        with self._managed_lists_lock:
            self._managed_lists[list_name] = {
                "list_name": list_name,
                "list_id": list_id,
                "display_name": display_name,
                "source_name": source_name,
                "selection": dict(selection or {}),
            }

    def _delete_disabled_lists(self, desired_names: set[str]) -> None:
        stale_names = [list_name for list_name in self._managed_lists if list_name not in desired_names]
        for list_name in stale_names:
            self._check_cancelled()
            entry = self._managed_lists.get(list_name, {})
            list_id = str(entry.get("list_id", "")).strip()
            try:
                self._set_status(f"Deleting disabled PublicMetaDB list {list_name}")
                if not list_id:
                    existing = self._pmdb.find_list_by_name(list_name)
                    if not existing:
                        logger.info("Managed list '%s' no longer exists in PublicMetaDB", list_name)
                        self._managed_lists.pop(list_name, None)
                        continue
                    list_id = str(existing.get("id", "")).strip()
                self._pmdb.delete_list(list_id)
                logger.info("Deleted disabled PublicMetaDB list '%s' (id=%s)", list_name, list_id)
                self._managed_lists.pop(list_name, None)
            except SyncCancelled:
                raise
            except Exception as exc:
                logger.error("Failed to delete disabled PublicMetaDB list '%s': %s", list_name, exc)
