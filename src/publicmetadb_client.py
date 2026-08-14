"""PublicMetaDB API client for managing lists and items."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import PublicMetaDBConfig
# Moved to the shared module so every provider can pace itself, not just this one.
from .rate_limit import RateLimiter
from .http_timeouts import _env_timeout

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = (5, _env_timeout("SYNCMETA_PMDB_READ_TIMEOUT", 20))
_CANCEL_POLL_INTERVAL = 0.25

# Rate limit: 300 requests per 10 seconds
RATE_LIMIT_MAX = 280  # Leave some headroom
RATE_LIMIT_WINDOW = 10.0

#: PMDB list types that are singletons owned by the account rather than lists
#: SyncMeta names and creates. They are looked up by ``type``, never by name,
#: so a renamed one is still found instead of being duplicated.
NATIVE_LIST_TYPES = frozenset({"watchlist", "picks"})


def _coerce_tmdb_id(value: object, media_type: str = "") -> int | None:
    """Normalize PMDB mapping IDs that may be returned as int or media dict."""
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


@dataclass
class PublicMetaDBStats:
    mapping_lookup_hits: int = 0
    mapping_lookup_misses: int = 0
    mapping_lookup_auth_soft_misses: int = 0
    mapping_lookup_errors: int = 0
    list_write_successes: int = 0
    list_write_failures: int = 0


class PublicMetaDBClient:
    """Client for the PublicMetaDB external API."""

    def __init__(self, config: PublicMetaDBConfig, cancel_requested_callback=None):
        self._config = config
        self._session_local = threading.local()
        self._limiter = RateLimiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)
        self._lists_lock = threading.Lock()
        self._lists_by_name: dict[str, dict] | None = None
        self._lists_by_type: dict[str, dict] = {}
        self._cancel_requested_callback = cancel_requested_callback
        self._mapping_auth_warning_logged = False
        self._stats = PublicMetaDBStats()
        self._stats_lock = threading.Lock()

    def _check_cancelled(self) -> None:
        if not self._cancel_requested_callback:
            return
        try:
            if self._cancel_requested_callback():
                from .sync_service import SyncCancelled
                raise SyncCancelled("Sync stopped by user")
        except SyncCancelled:
            raise
        except Exception:
            logger.debug("Cancel callback failed", exc_info=True)

    def stats_snapshot(self) -> dict[str, int]:
        with self._stats_lock:
            return vars(self._stats).copy()

    def _record_stat(self, field_name: str, delta: int = 1) -> None:
        with self._stats_lock:
            setattr(self._stats, field_name, int(getattr(self._stats, field_name, 0)) + delta)

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        })

        retry = Retry(
            total=3,
            backoff_factor=2.0,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["GET", "POST", "DELETE"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        try:
            from .api_logger import make_requests_hook
            session.hooks["response"].append(make_requests_hook("pmdb"))
        except Exception:
            pass
        return session

    def _session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = self._build_session()
            self._session_local.session = session
        return session

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        self._check_cancelled()
        self._limiter.wait(self._cancel_requested_callback)
        url = f"{self._config.base_url}{path}"
        logger.debug("%s %s", method, url)
        resp = self._session().request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        self._check_cancelled()
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.text:
            return None
        return resp.json()

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict | None:
        return self._request("POST", path, json=data)

    def _delete(self, path: str) -> dict | None:
        return self._request("DELETE", path)

    # Watch history

    def get_watched_history(self) -> list[dict]:
        return self._get_paginated_items("/api/external/watched")

    def mark_watched(
        self,
        tmdb_id: int,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
        watched_at: str | None = None,
        dedupe: bool = False,
    ) -> dict | None:
        payload = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        }
        if season is not None:
            payload["season"] = season
        if episode is not None:
            payload["episode"] = episode
        if watched_at is not None:
            payload["watched_at"] = watched_at
        path = "/api/external/watched?dedupe=true" if dedupe else "/api/external/watched"
        return self._post(path, payload)

    def delete_watched_entry(self, watched_id: str) -> bool:
        try:
            self._delete(f"/api/external/watched/{watched_id}")
            logger.info("Deleted watched entry %s", watched_id)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.info("Watched entry %s was already gone", watched_id)
                return False
            raise

    def bulk_delete_watched(
        self,
        tmdb_id: int,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> bool:
        """Delete all play entries for a title (or specific season/episode) in one call.

        Uses DELETE /api/external/watched?tmdb_id=X&media_type=Y which wipes every
        play record for that title at once — far fewer API calls than deleting by ID.
        """
        params: dict = {"tmdb_id": tmdb_id, "media_type": media_type}
        if season is not None:
            params["season"] = season
        if episode is not None:
            params["episode"] = episode
        try:
            self._request("DELETE", "/api/external/watched", params=params)
            logger.info("Bulk-deleted watched history for tmdb_id=%s (%s)", tmdb_id, media_type)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return False
            raise

    def clear_watched_history(self) -> int:
        """Delete all watched history entries by individual ID.

        Always re-fetches page 1 after each round of deletions so that
        entries which shift up after deletes are not missed.
        """
        total_deleted = 0

        while True:
            resp = self._get("/api/external/watched", params={"page": 1, "perPage": 100})
            if not resp:
                break
            if isinstance(resp, list):
                items = resp
            else:
                items = list(resp.get("items", []))

            if not items:
                break

            ids = [item["id"] for item in items if "id" in item]
            if not ids:
                break

            for watched_id in ids:
                if self.delete_watched_entry(watched_id):
                    total_deleted += 1

        logger.info("Cleared watched history: %d entries deleted", total_deleted)
        return total_deleted

    # Resume / continue watching

    def get_resume_points(self) -> list[dict]:
        return self._get_paginated_items("/api/external/resume")

    def save_resume_point(
        self,
        tmdb_id: int,
        media_type: str,
        position_ms: int,
        runtime_ms: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict | None:
        payload = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "position_ms": position_ms,
            "runtime_ms": runtime_ms,
        }
        if season is not None:
            payload["season"] = season
        if episode is not None:
            payload["episode"] = episode
        return self._post("/api/external/resume", payload)

    def save_resume_points_batch(self, items: list[dict]) -> dict | None:
        return self._post("/api/external/resume/batch", {"items": items})

    def delete_resume_point(self, resume_id: str) -> bool:
        try:
            self._delete(f"/api/external/resume/{resume_id}")
            logger.info("Deleted resume point %s", resume_id)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.info("Resume point %s was already gone", resume_id)
                return False
            raise

    def _get_paginated_items(self, path: str, per_page: int = 100) -> list[dict]:
        resp = self._get(path, params={"page": 1, "perPage": per_page})
        if not resp:
            return []
        if isinstance(resp, list):
            return list(resp)
        items = list(resp.get("items", []))
        total_pages = int(resp.get("totalPages", 1) or 1)
        if total_pages <= 1 or not items:
            return items

        # Fetch remaining pages concurrently — each is independent.
        pages_data: dict[int, list] = {1: items}
        with ThreadPoolExecutor(max_workers=min(8, total_pages - 1)) as pool:
            futures = {
                pool.submit(self._get, path, {"page": p, "perPage": per_page}): p
                for p in range(2, total_pages + 1)
            }
            for future in as_completed(futures):
                page_num = futures[future]
                page_resp = future.result()
                if isinstance(page_resp, list):
                    pages_data[page_num] = page_resp
                elif page_resp:
                    pages_data[page_num] = list(page_resp.get("items", []))
                else:
                    pages_data[page_num] = []

        all_items: list[dict] = []
        for p in range(1, total_pages + 1):
            all_items.extend(pages_data.get(p, []))
        return all_items

    # ── Mapping lookups ────────────────────────────────────────────

    def lookup_by_external_id_detailed(self, id_type: str, id_value: str, media_type: str) -> dict:
        """Resolve an external ID to a TMDB ID with a richer status code."""
        try:
            resp = self._get("/api/external/mappings/lookup", params={
                "id_type": id_type,
                "id_value": id_value,
                "media_type": media_type,
            })
            if resp and resp.get("results"):
                self._record_stat("mapping_lookup_hits")
                results = resp["results"]
                # Prefer the community-upvoted result. PMDB may return multiple
                # candidate mappings for the same external ID (e.g. both a wrong
                # franchise-root mapping contributed by an old sync and the correct
                # per-show mapping voted in by the community). Taking the highest-
                # voted entry avoids landing on the wrong franchise entry.
                best = max(results, key=lambda r: r.get("votes", 0))

                def _result_title(entry: dict) -> str:
                    return (
                        entry.get("title")
                        or entry.get("name")
                        or entry.get("english_title")
                        or entry.get("original_title")
                        or ""
                    )

                # Expose every candidate, highest-voted first, so callers can
                # apply their own title checks across the whole set instead of
                # only the top-voted one.  When the top result carries a title
                # that does not match the item, a lower-voted candidate is often
                # the correct mapping and would otherwise be discarded unseen.
                candidates = [
                    {
                        "tmdb_id": _coerce_tmdb_id(entry.get("tmdb_id"), media_type),
                        "votes": int(entry.get("votes") or 0),
                        "title": _result_title(entry),
                    }
                    for entry in sorted(results, key=lambda r: r.get("votes", 0), reverse=True)
                ]
                return {
                    "tmdb_id": _coerce_tmdb_id(best.get("tmdb_id"), media_type),
                    "status": "hit",
                    # Expose vote count so callers can apply extra scrutiny on
                    # zero-vote (self-submitted, unconfirmed) mappings.
                    "votes": int(best.get("votes") or 0),
                    "result_count": len(results),
                    "title": _result_title(best),
                    "candidates": [c for c in candidates if c["tmdb_id"]],
                }
            self._record_stat("mapping_lookup_misses")
            return {"tmdb_id": None, "status": "miss"}
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self._record_stat("mapping_lookup_misses")
                return {"tmdb_id": None, "status": "miss"}
            if e.response is not None and e.response.status_code == 401:
                self._record_stat("mapping_lookup_auth_soft_misses")
                if not self._mapping_auth_warning_logged:
                    logger.warning(
                        "PMDB mapping lookup returned 401 Unauthorized; "
                        "treating external-ID lookup as unavailable for this sync run"
                    )
                    self._mapping_auth_warning_logged = True
                return {"tmdb_id": None, "status": "lookup_unavailable"}
            self._record_stat("mapping_lookup_errors")
            raise

    def lookup_by_external_id(self, id_type: str, id_value: str, media_type: str) -> int | None:
        """Resolve an external ID (imdb, mal, anidb, tvdb, etc.) to a TMDB ID."""
        detail = self.lookup_by_external_id_detailed(id_type, id_value, media_type)
        return detail.get("tmdb_id")

    def create_id_mapping(
        self,
        tmdb_id: int,
        media_type: str,
        id_type: str,
        id_value: str,
    ) -> bool:
        """Submit a new external ID → TMDB mapping to PMDB for community benefit.

        id_type must be one of: imdb, tvdb, mal, anilist, anidb, trakt.
        Returns True if the mapping was accepted.
        """
        try:
            resp = self._post("/api/external/mappings", {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "id_type": id_type,
                "id_value": str(id_value),
            })
            return bool(resp and resp.get("success"))
        except requests.HTTPError:
            return False

    # ── Anime seasons ──────────────────────────────────────────────

    def get_anime_seasons(self, tmdb_id: int) -> list[dict]:
        """Fetch PMDB anime season mappings for a TMDB show.

        Each entry maps a logical anime season (season_number) to a TMDB
        season/episode range via tmdb_season, tmdb_episode_start, tmdb_episode_end.
        Results are cached per tmdb_id for the lifetime of this client instance.
        """
        cache = getattr(self, "_anime_seasons_cache", None)
        if cache is None:
            cache = {}
            object.__setattr__(self, "_anime_seasons_cache", cache)
        if tmdb_id in cache:
            return cache[tmdb_id]
        try:
            resp = self._get("/api/external/anime-seasons", params={"tmdb_id": tmdb_id})
            if isinstance(resp, list):
                seasons = resp
            elif isinstance(resp, dict):
                seasons = resp.get("items") or resp.get("seasons") or []
            else:
                seasons = []
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                seasons = []
            else:
                raise
        cache[tmdb_id] = seasons
        return seasons

    # ── List management ────────────────────────────────────────────

    def get_lists(self) -> list[dict]:
        """Fetch all user lists, paginated."""
        all_lists = []
        page = 1
        while True:
            resp = self._get("/api/external/lists", params={"page": page, "perPage": 50})
            if not resp:
                break
            items = resp.get("items", [])
            all_lists.extend(items)
            if page >= resp.get("totalPages", 1):
                break
            page += 1
        with self._lists_lock:
            self._lists_by_name = {
                str(item.get("name", "")).strip(): item
                for item in all_lists
                if str(item.get("name", "")).strip()
            }
            self._lists_by_type = {}
            for item in all_lists:
                t = str(item.get("type", "")).strip().lower()
                if t and t not in self._lists_by_type:
                    self._lists_by_type[t] = item
        return all_lists

    def refresh_lists_index(self) -> dict[str, dict]:
        self.get_lists()
        with self._lists_lock:
            return dict(self._lists_by_name or {})

    def find_list_by_name(self, name: str) -> dict | None:
        """Find a list by exact name match."""
        lookup_name = str(name or "").strip()
        if not lookup_name:
            return None
        with self._lists_lock:
            cached = None if self._lists_by_name is None else self._lists_by_name.get(lookup_name)
        if cached:
            return cached
        self.get_lists()
        with self._lists_lock:
            return None if self._lists_by_name is None else self._lists_by_name.get(lookup_name)

    def create_list(self, name: str, description: str = "", is_public: bool = False, list_type: str = "custom") -> dict:
        """Create a new list and return its metadata."""
        resp = self._post("/api/external/lists", data={
            "name": name,
            "description": description,
            "is_public": is_public,
            "type": list_type,
        })
        if resp and resp.get("success"):
            logger.info("Created list '%s' (id=%s)", name, resp["item"]["id"])
            self._record_stat("list_write_successes")
            with self._lists_lock:
                if self._lists_by_name is not None:
                    self._lists_by_name[str(name).strip()] = resp["item"]
            return resp["item"]
        self._record_stat("list_write_failures")
        raise RuntimeError(f"Failed to create list '{name}': {resp}")

    def find_list_by_type(self, list_type: str) -> dict | None:
        """Find a list by its type (e.g. 'watchlist'). Returns the first match."""
        lookup = str(list_type or "").strip().lower()
        if not lookup:
            return None
        with self._lists_lock:
            cached = self._lists_by_type.get(lookup)
        if cached:
            return cached
        self.get_lists()
        with self._lists_lock:
            return self._lists_by_type.get(lookup)

    def get_or_create_list(self, name: str, description: str = "", is_public: bool = False, list_type: str = "custom") -> dict:
        """Find a list by name (or by type for a native list), or create it.

        A native list is a singleton PMDB owns by type rather than by name, so
        it must be matched on type: the user may well have renamed it, and
        matching on our name would create a second one alongside theirs.
        """
        if list_type in NATIVE_LIST_TYPES:
            existing = self.find_list_by_type(list_type)
            if existing:
                logger.debug("Found existing %s '%s' (id=%s)", list_type, existing.get("name"), existing["id"])
                return existing
        else:
            existing = self.find_list_by_name(name)
            if existing:
                logger.debug("Found existing list '%s' (id=%s)", name, existing["id"])
                return existing
        return self.create_list(name, description, is_public=is_public, list_type=list_type)

    def delete_list(self, list_id: str) -> bool:
        """Delete a list by ID. Returns False if it is already gone."""
        try:
            self._delete(f"/api/external/lists/{list_id}")
            logger.info("Deleted list %s", list_id)
            with self._lists_lock:
                if self._lists_by_name is not None:
                    for name, item in list(self._lists_by_name.items()):
                        if str(item.get("id", "")).strip() == str(list_id).strip():
                            self._lists_by_name.pop(name, None)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.info("List %s was already gone", list_id)
                return False
            raise

    # ── List items ─────────────────────────────────────────────────

    def get_list_items(self, list_id: str) -> list[dict]:
        """Fetch all items in a list, paginated."""
        all_items = []
        page = 1
        while True:
            resp = self._get(f"/api/external/lists/{list_id}/items", params={
                "page": page, "perPage": 100,
            })
            if not resp:
                break
            items = resp.get("items", [])
            all_items.extend(items)
            if page >= resp.get("totalPages", 1):
                break
            page += 1
        return all_items

    def add_item_to_list(self, list_id: str, tmdb_id: int, media_type: str) -> dict | None:
        """Add a single item to a list."""
        resp = self._post(f"/api/external/lists/{list_id}/items", data={
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        })
        if resp and resp.get("success"):
            logger.debug("Added tmdb_id=%s (%s) to list %s", tmdb_id, media_type, list_id)
            self._record_stat("list_write_successes")
            return resp.get("item")
        logger.warning("Failed to add tmdb_id=%s to list %s: %s", tmdb_id, list_id, resp)
        self._record_stat("list_write_failures")
        return None

    def add_items_to_list_batch(self, list_id: str, items: list[dict]) -> list[dict]:
        """Add multiple items to a list via the batch endpoint.

        Falls back to individual adds if the batch endpoint is unavailable.
        Each item dict must have 'tmdb_id' (int) and 'media_type' (str).
        Returns a list of result dicts, one per input item.
        """
        if not items:
            return []
        payload = {
            "items": [
                {"tmdb_id": item["tmdb_id"], "media_type": item["media_type"]}
                for item in items
            ]
        }
        try:
            resp = self._post(f"/api/external/lists/{list_id}/items/batch", payload)
            if isinstance(resp, dict) and resp.get("results"):
                results = resp["results"]
                for r in results:
                    if r.get("success"):
                        self._record_stat("list_write_successes")
                    else:
                        self._record_stat("list_write_failures")
                return results
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.debug("Batch endpoint not available; falling back to individual adds")
            else:
                raise
        # Fallback: add individually
        results = []
        for item in items:
            result = self.add_item_to_list(list_id, item["tmdb_id"], item["media_type"])
            results.append({"success": result is not None, "item": result})
        return results

    def remove_item_from_list(self, list_id: str, item_id: str) -> None:
        """Remove an item from a list by its list-item ID."""
        try:
            self._delete(f"/api/external/lists/{list_id}/items/{item_id}")
            self._record_stat("list_write_successes")
        except Exception:
            self._record_stat("list_write_failures")
            raise
        logger.debug("Removed item %s from list %s", item_id, list_id)
