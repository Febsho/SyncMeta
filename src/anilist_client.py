"""AniList GraphQL API client for fetching user anime lists."""

import atexit
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import AniListConfig
from . import fribb_client
from .http_timeouts import _env_timeout

logger = logging.getLogger(__name__)

# Module-level shared cache for AniList root-chain data.
# AniList prequel chains are global facts (not user-specific), so all
# AniListClient instances across all profiles share the same cache.
# This avoids re-fetching the same chain data per user.
_SHARED_ROOT_CACHE: dict[int, dict | None] = {}
_SHARED_ROOT_CONTEXT_CACHE: dict[int, dict | None] = {}
_SHARED_ROOT_CONTEXT_CACHED_AT: dict[int, int] = {}
_SHARED_ROOT_INFLIGHT: dict[int, threading.Event] = {}
_SHARED_CACHE_LOCK = threading.Lock()
_PERSISTED_CACHE_LOADED = False
_PERSISTED_CACHE_DIRTY = False
_PERSISTED_CACHE_LAST_SAVE = 0.0

_ROOT_CACHE_TTL_SECONDS = 30 * 24 * 3600
_PERSISTED_CACHE_VERSION = 1
_PERSISTED_CACHE_MIN_SAVE_INTERVAL = 2.0

GRAPHQL_URL = "https://graphql.anilist.co"
OAUTH_TOKEN_URL = "https://anilist.co/api/v2/oauth/token"
OAUTH_AUTHORIZE_URL = "https://anilist.co/api/v2/oauth/authorize"
# AniList cannot redirect back to an arbitrary app, so its own "pin" endpoint is
# used: the user authorises, AniList shows a code, and they paste it back here.
OAUTH_PIN_REDIRECT_URI = "https://anilist.co/api/v2/oauth/pin"
REQUEST_TIMEOUT = (5, _env_timeout("SYNCMETA_ANILIST_READ_TIMEOUT", 20))
_CANCEL_POLL_INTERVAL = 0.25
# Safety cap on episodes derived from one AniList progress count. A long-runner
# legitimately passes 1000 (One Piece), but a number far past that is bad data,
# and each derived episode becomes its own PublicMetaDB write.
_MAX_DERIVED_EPISODES = 2000

# AniList statuses we care about
ANILIST_STATUS_WATCHING = "CURRENT"
ANILIST_STATUS_PLAN_TO_WATCH = "PLANNING"
ANILIST_STATUS_COMPLETED = "COMPLETED"
ANILIST_STATUS_PAUSED = "PAUSED"
ANILIST_STATUS_DROPPED = "DROPPED"

_USER_QUERY = """
query ($name: String) {
  User(name: $name) {
    name
  }
}
"""

_LIST_QUERY = """
query ($userName: String, $status: MediaListStatus) {
  MediaListCollection(userName: $userName, type: ANIME, status: $status) {
    lists {
      entries {
        id
        progress
        completedAt {
          year
          month
          day
        }
        updatedAt
        media {
          id
          idMal
          title {
            romaji
            english
            native
          }
          synonyms
          seasonYear
          format
          episodes
        }
      }
    }
  }
}
"""

_MEDIA_RELATIONS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    idMal
    episodes
    format
    seasonYear
    startDate {
      year
      month
      day
    }
    title {
      romaji
      english
    }
    relations {
      edges {
        relationType
        node {
          id
          idMal
          episodes
          format
          seasonYear
          startDate {
            year
            month
            day
          }
          title {
            romaji
            english
          }
        }
      }
    }
  }
}
"""

_SAVE_ENTRY_MUTATION = """
mutation ($mediaId: Int, $status: MediaListStatus, $progress: Int) {
  SaveMediaListEntry(mediaId: $mediaId, status: $status, progress: $progress) {
    id
    status
    progress
  }
}
"""

_DELETE_ENTRY_MUTATION = """
mutation ($id: Int) {
  DeleteMediaListEntry(id: $id) {
    deleted
  }
}
"""

_MAL_TO_ANILIST_QUERY = """
query ($idMal: Int) {
  Media(idMal: $idMal, type: ANIME) {
    id
  }
}
"""

_ROOT_FORMAT_PRIORITY = {
    "TV": 0,
    "TV_SHORT": 0,
    "ONA": 1,
    "OVA": 2,
    "SPECIAL": 3,
    "MOVIE": 4,
}


def build_authorize_url(client_id: str) -> str:
    """Authorization-code URL for the pin redirect."""
    from urllib.parse import urlencode

    query = urlencode({
        "client_id": str(client_id or "").strip(),
        "redirect_uri": OAUTH_PIN_REDIRECT_URI,
        "response_type": "code",
    })
    return f"{OAUTH_AUTHORIZE_URL}?{query}"


def exchange_code_for_token(client_id: str, client_secret: str, code: str) -> str:
    """Trade the pin code for a long-lived access token.

    Raises ValueError with a user-facing message when AniList rejects the
    exchange, which is nearly always a mistyped code or a redirect URL that does
    not match the one configured on the AniList client.
    """
    payload = {
        "grant_type": "authorization_code",
        "client_id": str(client_id or "").strip(),
        "client_secret": str(client_secret or "").strip(),
        "redirect_uri": OAUTH_PIN_REDIRECT_URI,
        "code": str(code or "").strip(),
    }
    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ValueError(f"Could not reach AniList: {exc}") from exc

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.status_code >= 400 or not data.get("access_token"):
        detail = str(
            data.get("hint")
            or data.get("message")
            or data.get("error_description")
            or data.get("error")
            or ""
        ).strip()
        if not detail:
            detail = f"AniList returned HTTP {response.status_code}"
        raise ValueError(
            f"AniList rejected the authorization code: {detail}. Check the code "
            f"and that the client's Redirect URL is exactly {OAUTH_PIN_REDIRECT_URI}."
        )
    return str(data["access_token"])


def _persistent_cache_path() -> Path:
    configured = os.getenv("ANILIST_ROOT_CACHE_FILE", "").strip()
    if configured:
        return Path(configured)
    return Path("data") / "anilist_root_cache.json"


def _mark_persistent_cache_dirty() -> None:
    global _PERSISTED_CACHE_DIRTY
    _PERSISTED_CACHE_DIRTY = True


def _serialize_cache_entry(context: dict | None, cached_at: int) -> dict:
    return {
        "context": context,
        "cached_at": cached_at,
    }


def _load_persistent_root_cache() -> None:
    global _PERSISTED_CACHE_LOADED
    if _PERSISTED_CACHE_LOADED:
        return
    with _SHARED_CACHE_LOCK:
        if _PERSISTED_CACHE_LOADED:
            return
        path = _persistent_cache_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _PERSISTED_CACHE_LOADED = True
            return
        except Exception:
            logger.warning("Failed to load AniList root cache from %s", path, exc_info=True)
            _PERSISTED_CACHE_LOADED = True
            return

        if not isinstance(payload, dict) or payload.get("version") != _PERSISTED_CACHE_VERSION:
            _PERSISTED_CACHE_LOADED = True
            return

        entries = payload.get("entries")
        if not isinstance(entries, dict):
            _PERSISTED_CACHE_LOADED = True
            return

        now = int(time.time())
        cutoff = now - _ROOT_CACHE_TTL_SECONDS
        expired = False
        loaded = 0
        for raw_media_id, raw_entry in entries.items():
            try:
                media_id = int(raw_media_id)
            except (TypeError, ValueError):
                expired = True
                continue
            if not isinstance(raw_entry, dict):
                expired = True
                continue
            cached_at = raw_entry.get("cached_at")
            context = raw_entry.get("context")
            try:
                cached_at_int = int(cached_at)
            except (TypeError, ValueError):
                expired = True
                continue
            if cached_at_int < cutoff:
                expired = True
                continue
            if context is not None and not isinstance(context, dict):
                expired = True
                continue
            _SHARED_ROOT_CONTEXT_CACHE[media_id] = context
            _SHARED_ROOT_CONTEXT_CACHED_AT[media_id] = cached_at_int
            root = (context or {}).get("root") if isinstance(context, dict) else None
            _SHARED_ROOT_CACHE[media_id] = root if isinstance(root, dict) else root
            loaded += 1

        _PERSISTED_CACHE_LOADED = True
        if expired:
            _mark_persistent_cache_dirty()
        logger.info("Loaded %d persisted AniList root cache entries", loaded)


def _save_persistent_root_cache(force: bool = False) -> None:
    global _PERSISTED_CACHE_DIRTY, _PERSISTED_CACHE_LAST_SAVE
    if not _PERSISTED_CACHE_LOADED:
        return
    now_monotonic = time.monotonic()
    if not force and (not _PERSISTED_CACHE_DIRTY or now_monotonic - _PERSISTED_CACHE_LAST_SAVE < _PERSISTED_CACHE_MIN_SAVE_INTERVAL):
        return

    with _SHARED_CACHE_LOCK:
        if not force and (not _PERSISTED_CACHE_DIRTY or now_monotonic - _PERSISTED_CACHE_LAST_SAVE < _PERSISTED_CACHE_MIN_SAVE_INTERVAL):
            return
        now = int(time.time())
        entries = {}
        for media_id, context in _SHARED_ROOT_CONTEXT_CACHE.items():
            cached_at = int(_SHARED_ROOT_CONTEXT_CACHED_AT.get(int(media_id), now))
            if context is not None and not isinstance(context, dict):
                continue
            entries[str(int(media_id))] = _serialize_cache_entry(context, cached_at)
        path = _persistent_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(json.dumps({
                "version": _PERSISTED_CACHE_VERSION,
                "saved_at": now,
                "ttl_seconds": _ROOT_CACHE_TTL_SECONDS,
                "entries": entries,
            }, indent=2, sort_keys=True), encoding="utf-8")
            tmp_path.replace(path)
            _PERSISTED_CACHE_DIRTY = False
            _PERSISTED_CACHE_LAST_SAVE = now_monotonic
        except Exception:
            logger.warning("Failed to save AniList root cache to %s", path, exc_info=True)


def _reset_persistent_root_cache_state() -> None:
    global _PERSISTED_CACHE_LOADED, _PERSISTED_CACHE_DIRTY, _PERSISTED_CACHE_LAST_SAVE
    with _SHARED_CACHE_LOCK:
        _SHARED_ROOT_CACHE.clear()
        _SHARED_ROOT_CONTEXT_CACHE.clear()
        _SHARED_ROOT_CONTEXT_CACHED_AT.clear()
        _SHARED_ROOT_INFLIGHT.clear()
        _PERSISTED_CACHE_LOADED = False
        _PERSISTED_CACHE_DIRTY = False
        _PERSISTED_CACHE_LAST_SAVE = 0.0


atexit.register(_save_persistent_root_cache, True)


def _safe_progress(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _entry_watched_at(entry: dict) -> str:
    """Best available date for an AniList entry, as an ISO-8601 UTC stamp.

    AniList records no per-episode timestamps at all — `completedAt` is the day
    the user marked the whole entry finished, and `updatedAt` is the last time
    they touched it. Every episode derived from one entry therefore shares this
    single date; it is the entry's date, not a play time, and callers must mark
    the rows cursor_exempt so it can never be mistaken for one.
    """
    completed = entry.get("completedAt") if isinstance(entry, dict) else None
    if isinstance(completed, dict):
        try:
            year = int(completed.get("year") or 0)
            month = int(completed.get("month") or 0)
            day = int(completed.get("day") or 0)
        except (TypeError, ValueError):
            year = month = day = 0
        if year > 0:
            # A partial AniList date (year only, or year+month) is real data;
            # clamp the missing parts rather than dropping the whole date.
            return "%04d-%02d-%02dT00:00:00Z" % (year, max(1, month), max(1, day))
    updated = entry.get("updatedAt") if isinstance(entry, dict) else None
    try:
        updated = int(updated or 0)
    except (TypeError, ValueError):
        updated = 0
    if updated > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated))
    return ""


class AniListClient:
    """Client for the AniList GraphQL API (public, no auth required for public lists)."""

    def __init__(self, config: AniListConfig, cancel_requested_callback=None):
        _load_persistent_root_cache()
        self._config = config
        self._session = self._build_session()
        self._status_cache: dict[str, list[dict]] = {}
        self._cancel_requested_callback = cancel_requested_callback
        self._username_resolved = False
        # Point to the module-level shared caches so all instances benefit
        # from chain data already fetched by another user's sync.
        self._root_cache = _SHARED_ROOT_CACHE
        self._root_context_cache = _SHARED_ROOT_CONTEXT_CACHE

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

    def _sleep_with_cancel(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_CANCEL_POLL_INTERVAL, remaining))

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if self._config.access_token:
            session.headers["Authorization"] = f"Bearer {self._config.access_token}"

        retry = Retry(
            total=3,
            backoff_factor=1.5,
            # 429 is handled manually in _query to honour the Retry-After header.
            status_forcelist=[500, 502, 503],
            allowed_methods=["POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        return session

    def _query(self, query: str, variables: dict, _retries: int = 3) -> dict | None:
        logger.debug("AniList query variables=%s", variables)
        try:
            self._check_cancelled()
            resp = self._session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                timeout=REQUEST_TIMEOUT,
            )
            self._check_cancelled()
            if resp.status_code == 429 and _retries > 0:
                # Honour the server-supplied Retry-After (seconds) rather than
                # using blind exponential backoff.
                retry_after = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset-After")
                try:
                    wait = max(1.0, min(float(retry_after), 120.0))
                except (TypeError, ValueError):
                    wait = 60.0
                logger.warning("AniList rate limited; retrying in %.1fs (variables=%s)", wait, variables)
                self._sleep_with_cancel(wait)
                return self._query(query, variables, _retries=_retries - 1)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("AniList request failed for variables=%s: %s", variables, exc)
            return None
        if "errors" in data:
            logger.error("AniList GraphQL errors: %s", data["errors"])
            return None
        return data.get("data")

    def get_anilist_id_by_mal(self, mal_id: int) -> int | None:
        """Resolve a MAL ID to its AniList ID, or None if not found."""
        data = self._query(_MAL_TO_ANILIST_QUERY, {"idMal": mal_id})
        media = (data or {}).get("Media")
        if isinstance(media, dict):
            return media.get("id")
        return None

    def get_watching(self) -> list[dict]:
        """Fetch anime with status CURRENT (watching)."""
        return self.get_status(ANILIST_STATUS_WATCHING)

    def get_plan_to_watch(self) -> list[dict]:
        """Fetch anime with status PLANNING (plan to watch)."""
        return self.get_status(ANILIST_STATUS_PLAN_TO_WATCH)

    # Maps synthetic status keys (used in config/UI) to (base_status, format_filter).
    _FORMAT_FILTER_MAP: dict[str, tuple[str, str]] = {
        "COMPLETED_ONA": ("COMPLETED", "ONA"),
        "COMPLETED_OVA": ("COMPLETED", "OVA"),
        "COMPLETED_MOVIE": ("COMPLETED", "MOVIE"),
    }

    def get_status(self, status: str) -> list[dict]:
        """Fetch anime for any supported AniList status.

        Synthetic statuses like COMPLETED_ONA / COMPLETED_MOVIE fetch the
        underlying base status and post-filter by AniList media format.
        """
        return self.get_statuses([status]).get(status, [])

    # Statuses that imply the user actually watched something. PLANNING is
    # excluded: a plan-to-watch entry has no progress to derive from, and one
    # that somehow carries progress is a list the user re-shelved, not history.
    HISTORY_STATUSES = (
        ANILIST_STATUS_COMPLETED,
        ANILIST_STATUS_WATCHING,
        ANILIST_STATUS_PAUSED,
        ANILIST_STATUS_DROPPED,
    )

    def get_watched_history(self) -> list[dict]:
        """Derive episode rows from AniList progress counts.

        AniList stores no watch history: there is no play log, no per-episode
        record, and no per-episode timestamp. All it knows is `progress` — how
        many episodes of a cour the user has watched — plus the date the entry
        itself was completed or last touched.

        So these rows are *derived*, not reported. `progress: 12` becomes
        episodes 1-12 of that entry, every one stamped with the entry's single
        date, and the anime remapper places them onto the root series using the
        same offset machinery SIMKL's aggregate counts already use. They are
        marked `cursor_exempt` and `anilist_derived` so no caller can mistake
        them for plays: they say "this episode has been watched", nothing more,
        and rewatches are not representable at all.
        """
        rows: list[dict] = []
        undated = 0
        for status in self.HISTORY_STATUSES:
            self._check_cancelled()
            try:
                items = self.get_status(status)
            except Exception as exc:
                logger.warning("AniList history: could not fetch status '%s': %s", status, exc)
                continue
            for item in items:
                derived, skipped_undated = self._derive_history_rows(item, status)
                undated += skipped_undated
                rows.extend(derived)
        if undated:
            logger.info(
                "AniList history: skipped %d entr%s with no completion or update date — "
                "writing them would have stamped today's date on an old watch",
                undated, "y" if undated == 1 else "ies",
            )
        logger.info("AniList history: derived %d episode row(s) from progress counts", len(rows))
        return rows

    def _derive_history_rows(self, item: dict, status: str) -> tuple[list[dict], int]:
        progress = _safe_progress(item.get("anilist_progress"))
        watched_at = str(item.get("anilist_watched_at") or "").strip()
        media_type = str(item.get("media_type") or "").strip().lower()

        if media_type == "movie":
            # A film has no episode count to walk; completion is the signal.
            if progress <= 0 and status != ANILIST_STATUS_COMPLETED:
                return [], 0
            if not watched_at:
                return [], 1
            return [self._history_row(item, watched_at, season=None, episode=None)], 0

        if progress <= 0:
            return [], 0
        if not watched_at:
            return [], 1

        # A progress number above the entry's own episode count is AniList data
        # that disagrees with itself; trust the smaller of the two rather than
        # claiming episodes the season does not have.
        try:
            total_episodes = int(item.get("anilist_episode_count") or 0)
        except (TypeError, ValueError):
            total_episodes = 0
        if total_episodes > 0:
            progress = min(progress, total_episodes)
        if progress > _MAX_DERIVED_EPISODES:
            logger.info(
                "AniList history: '%s' reports %d episodes of progress — above the safety cap, skipping",
                item.get("title", "Unknown"), progress,
            )
            return [], 0

        return [
            self._history_row(item, watched_at, season=1, episode=number)
            for number in range(1, progress + 1)
        ], 0

    @staticmethod
    def _history_row(item: dict, watched_at: str, season: int | None, episode: int | None) -> dict:
        identity = item.get("anime_identity") or {}
        row = {
            **item,
            "watched_at": watched_at,
            # Derived from a count, never observed as a play. Both flags matter:
            # cursor_exempt keeps this date out of any history cursor, and
            # anilist_derived lets the pipeline treat the row as presence only.
            "cursor_exempt": True,
            "anilist_derived": True,
            "root_episode_offset": int(identity.get("root_episode_offset") or 0),
            "anime_resolve_mode": "history_identity",
        }
        if season is not None:
            row["season"] = season
        if episode is not None:
            row["episode"] = episode
        return row

    def get_statuses(self, statuses: list[str]) -> dict[str, list[dict]]:
        """Fetch multiple AniList statuses while reusing base-status responses.

        When a plain status like ``COMPLETED`` is requested alongside a
        format-specific synthetic status like ``COMPLETED_ONA``, items that
        belong to a synthetic bucket are excluded from the plain bucket so
        that they appear in exactly one list.

        Example: user has [COMPLETED, COMPLETED_ONA, COMPLETED_OVA].
        - COMPLETED      → all completed items, EXCLUDING ONA and OVA formats
        - COMPLETED_ONA  → completed items WHERE anilist_format == "ONA"
        - COMPLETED_OVA  → completed items WHERE anilist_format == "OVA"
        """
        # Build a map: base_status → set of formats claimed by synthetic keys.
        claimed_formats: dict[str, set[str]] = {}
        for status in statuses:
            base_status, fmt = self._base_status_and_filter(status)
            if fmt:
                claimed_formats.setdefault(base_status, set()).add(fmt)

        results: dict[str, list[dict]] = {}
        for status in statuses:
            base_status, format_filter = self._base_status_and_filter(status)
            base_items = self._fetch_base_status(base_status)
            if format_filter:
                # Synthetic key: only items matching this format (include both
                # tv and movie media_types — single-episode ONAs/OVAs are stored
                # as media_type="movie" but still belong in the ONA/OVA list).
                results[status] = [
                    item for item in base_items
                    if item.get("anilist_format") == format_filter
                ]
            else:
                # Plain key: exclude every format claimed by a synthetic sibling
                # that is also being synced, so items don't land in two lists.
                excluded = claimed_formats.get(base_status, set())
                if excluded:
                    results[status] = [
                        item for item in base_items
                        if item.get("anilist_format") not in excluded
                    ]
                else:
                    results[status] = list(base_items)
        return results

    @classmethod
    def _base_status_and_filter(cls, status: str) -> tuple[str, str | None]:
        if status in cls._FORMAT_FILTER_MAP:
            base_status, fmt = cls._FORMAT_FILTER_MAP[status]
            return base_status, fmt
        return status, None

    def _resolve_username(self) -> None:
        """Resolve the correct case for the AniList username.

        AniList's User query is case-insensitive but MediaListCollection
        requires the exact canonical casing.  One cheap query at the start
        of a sync avoids 404s for users who typed their name in the wrong case.
        """
        if self._username_resolved or not self._config.username:
            return
        self._username_resolved = True
        try:
            data = self._query(_USER_QUERY, {"name": self._config.username})
            if data and isinstance(data.get("User"), dict):
                canonical = data["User"].get("name") or self._config.username
                if canonical != self._config.username:
                    logger.info(
                        "AniList username corrected: '%s' -> '%s'",
                        self._config.username, canonical,
                    )
                    self._config.username = canonical
            else:
                logger.error(
                    "AniList user '%s' not found — check spelling in Settings. "
                    "AniList usernames are case-sensitive.",
                    self._config.username,
                )
        except Exception as exc:
            logger.warning("AniList username lookup failed for '%s': %s", self._config.username, exc)

    def _fetch_base_status(self, status: str) -> list[dict]:
        self._resolve_username()
        cached = self._status_cache.get(status)
        if cached is not None:
            return list(cached)
        data = self._query(_LIST_QUERY, {"userName": self._config.username, "status": status})
        if not data:
            return []

        collection = data.get("MediaListCollection")
        if not collection:
            return []

        items = []
        for lst in collection.get("lists", []):
            for entry in lst.get("entries", []):
                media = entry.get("media", {})
                normalized = self._normalize(media)
                if normalized:
                    # Carry the media-list entry id: AniList deletions are by
                    # entry, not by media, so removal is impossible without it.
                    if entry.get("id"):
                        normalized["anilist_entry_id"] = entry["id"]
                    # AniList tracks progress per entry, not per episode, so
                    # these two are all the watch data that exists here.
                    normalized["anilist_progress"] = _safe_progress(entry.get("progress"))
                    normalized["anilist_episode_count"] = _safe_progress(media.get("episodes"))
                    normalized["anilist_watched_at"] = _entry_watched_at(entry)
                    items.append(normalized)

        self._status_cache[status] = list(items)
        logger.info("AniList: fetched %d anime for status '%s'", len(items), status)
        return list(items)

    def _normalize(self, media: dict) -> dict | None:
        if not media:
            return None

        anilist_id = media.get("id")
        mal_id = media.get("idMal")
        title = self._media_title(media)
        fribb_entry = None
        if anilist_id:
            try:
                fribb_entry = fribb_client.lookup_by_anilist(int(anilist_id))
            except (TypeError, ValueError):
                fribb_entry = None
        if fribb_entry is None and mal_id:
            try:
                fribb_entry = fribb_client.lookup_by_mal(int(mal_id))
            except (TypeError, ValueError):
                fribb_entry = None
        ids = {
            "anilist": anilist_id,
            "mal": mal_id,
        }
        if isinstance(fribb_entry, dict):
            if fribb_entry.get("anidb_id"):
                ids["anidb"] = fribb_entry.get("anidb_id")
            if fribb_entry.get("tvdb_id"):
                ids["tvdb"] = fribb_entry.get("tvdb_id")
            # imdb_id is a list upstream; keep a single string so downstream
            # external-mapping lookups don't send "['tt0286390']" as the id.
            imdb_id = fribb_client.single_imdb_id(fribb_entry.get("imdb_id"))
            if imdb_id:
                ids["imdb"] = imdb_id
            if fribb_entry.get("themoviedb_id"):
                ids["tmdb"] = fribb_entry.get("themoviedb_id")
            if fribb_entry.get("simkl_id"):
                ids["simkl"] = fribb_entry.get("simkl_id")

        # Root IDs are resolved lazily by the matcher only when direct lookup
        # fails, avoiding an AniList API call for every item up front.
        root_anilist_id = None
        root_mal_id = None
        root_title = None

        fmt = str(media.get("format") or "").strip().upper()
        try:
            episodes = int(media.get("episodes") or 0)
        except (TypeError, ValueError):
            episodes = 0

        # AniList ONA/OVA/SPECIAL entries are mixed: some are episodic series,
        # others are effectively standalone films that PMDB indexes as movies.
        # Treat single-episode entries as movies so PMDB community mappings can
        # hit the correct target for cases like Star Fox Zero.
        if fmt == "MOVIE":
            media_type = "movie"
        elif fmt in {"ONA", "OVA", "SPECIAL"} and episodes == 1:
            media_type = "movie"
        else:
            media_type = "tv"

        root_episode_offset = 0
        if anilist_id and media_type == "tv":
            try:
                aid = int(anilist_id)
            except (TypeError, ValueError):
                aid = None
            if aid is not None:
                root_context = self._root_context_cache.get(aid)
                if root_context is None and aid in self._root_cache:
                    root_context = {"root": self._root_cache[aid], "episode_offset": 0}
                if isinstance(root_context, dict):
                    root_media = root_context.get("root")
                    if isinstance(root_media, dict) and root_media.get("id") != anilist_id:
                        root_anilist_id = root_media.get("id")
                        root_mal_id = root_media.get("idMal")
                        root_title = self._media_title(root_media)
                        root_episode_offset = root_context.get("episode_offset", 0) or 0

        return {
            "title": title,
            "title_variants": self._media_title_variants(media) + fribb_client.title_hints(fribb_entry),
            "year": media.get("seasonYear"),
            "media_type": media_type,
            "simkl_type": "anime",
            "imdb_id": ids.get("imdb"),
            "tmdb_id": str(ids["tmdb"]) if ids.get("tmdb") else None,
            "mal_id": str(mal_id) if mal_id else None,
            "anilist_id": str(anilist_id) if anilist_id else None,
            "root_mal_id": str(root_mal_id) if root_mal_id else None,
            "root_anilist_id": str(root_anilist_id) if root_anilist_id else None,
            "root_title": root_title,
            "anidb_id": str(ids["anidb"]) if ids.get("anidb") else None,
            "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
            "anilist_format": fmt,
            "anime_resolve_mode": "list_identity",
            "anime_identity": {
                "anilist_id": str(anilist_id) if anilist_id else None,
                "mal_id": str(mal_id) if mal_id else None,
                "anidb_id": str(ids["anidb"]) if ids.get("anidb") else None,
                "fribb_tmdb_id": str(ids["tmdb"]) if ids.get("tmdb") else None,
                "fribb_type": str(fribb_entry.get("type") or fmt) if isinstance(fribb_entry, dict) else fmt,
                "root_anilist_id": str(root_anilist_id) if root_anilist_id else None,
                "root_mal_id": str(root_mal_id) if root_mal_id else None,
                "root_episode_offset": root_episode_offset,
                "title": title,
                "year": media.get("seasonYear"),
                "source_status": None,
                "resolver_mode": "list_identity",
                "media_type": media_type,
            },
            "ids": ids,
            "status": None,
            "added_at": None,
        }

    def _get_root_media(self, media_id: int) -> dict | None:
        context = self._get_root_context(media_id)
        return (context or {}).get("root")

    def _get_root_context(self, media_id: int) -> dict | None:
        # Fast path: check shared cache without acquiring the lock.
        if media_id in self._root_context_cache:
            return self._root_context_cache[media_id]

        with _SHARED_CACHE_LOCK:
            if media_id in self._root_context_cache:
                return self._root_context_cache[media_id]
            if media_id in self._root_cache:
                root = self._root_cache[media_id]
                context = {"root": root, "episode_offset": 0}
                self._root_context_cache[media_id] = context
                _SHARED_ROOT_CONTEXT_CACHED_AT[media_id] = int(time.time())
                return context
            in_flight = _SHARED_ROOT_INFLIGHT.get(media_id)
            if in_flight is None:
                in_flight = threading.Event()
                _SHARED_ROOT_INFLIGHT[media_id] = in_flight
                is_owner = True
            else:
                is_owner = False

        if not is_owner:
            in_flight.wait()
            return self._root_context_cache.get(media_id)

        try:
            chain = self._fetch_root_chain(media_id)
            root = self._pick_root_candidate(chain)
            chronological_chain = list(reversed(chain))
            running_offset = 0
            context_by_id: dict[int, dict] = {}
            for media in chronological_chain:
                candidate_id = media.get("id")
                if candidate_id:
                    context_by_id[int(candidate_id)] = {
                        "root": root,
                        "episode_offset": running_offset,
                    }
                try:
                    running_offset += int(media.get("episodes") or 0)
                except (TypeError, ValueError):
                    pass
        except Exception:
            with _SHARED_CACHE_LOCK:
                _SHARED_ROOT_INFLIGHT.pop(media_id, None)
                in_flight.set()
            raise

        with _SHARED_CACHE_LOCK:
            for candidate in chain:
                candidate_id = candidate.get("id")
                if candidate_id:
                    self._root_cache[int(candidate_id)] = root
                    _SHARED_ROOT_CONTEXT_CACHED_AT[int(candidate_id)] = int(time.time())
                    self._root_context_cache[int(candidate_id)] = context_by_id.get(
                        int(candidate_id),
                        {"root": root, "episode_offset": 0},
                    )
            self._root_cache[media_id] = root
            _SHARED_ROOT_CONTEXT_CACHED_AT[media_id] = int(time.time())
            self._root_context_cache[media_id] = context_by_id.get(
                media_id,
                {"root": root, "episode_offset": 0},
            )
            _mark_persistent_cache_dirty()
            _SHARED_ROOT_INFLIGHT.pop(media_id, None)
            in_flight.set()
            context = self._root_context_cache[media_id]
        _save_persistent_root_cache()
        return context

    def _fetch_root_chain(self, media_id: int) -> list[dict]:
        seen: set[int] = set()
        chain: list[dict] = []
        current_id = media_id

        while current_id and current_id not in seen:
            seen.add(current_id)
            data = self._query(_MEDIA_RELATIONS_QUERY, {"id": current_id})
            media = (data or {}).get("Media")
            if not media:
                break

            chain.append(media)
            prequel = self._pick_prequel(media.get("relations", {}).get("edges", []))
            if not prequel:
                break
            current_id = prequel.get("id")

        return chain

    @classmethod
    def _pick_prequel(cls, edges: list[dict]) -> dict | None:
        prequels = [
            edge.get("node")
            for edge in edges
            if edge.get("relationType") == "PREQUEL" and edge.get("node")
        ]
        if not prequels:
            return None
        return min(prequels, key=cls._media_sort_key)

    @classmethod
    def _pick_root_candidate(cls, chain: list[dict]) -> dict | None:
        if not chain:
            return None
        return min(chain, key=cls._media_sort_key)

    @classmethod
    def _media_sort_key(cls, media: dict) -> tuple:
        start_date = media.get("startDate") or {}
        season_year = media.get("seasonYear") or 9999
        return (
            _ROOT_FORMAT_PRIORITY.get(media.get("format"), 9),
            start_date.get("year") or season_year,
            start_date.get("month") or 99,
            start_date.get("day") or 99,
            media.get("id") or 0,
        )

    @staticmethod
    def _media_title(media: dict) -> str:
        titles = media.get("title", {})
        return titles.get("english") or titles.get("romaji") or "Unknown"

    @staticmethod
    def _media_title_variants(media: dict) -> list[str]:
        """Every title AniList knows for this entry, most-canonical first.

        Providers disagree about which title to report — PMDB often holds the
        English title where AniList reports romaji — so the matcher compares
        against all of them rather than rejecting a correct mapping whose title
        simply happens to be in the other language.
        """
        titles = media.get("title") or {}
        candidates: list[str] = []
        if isinstance(titles, dict):
            candidates.extend([
                titles.get("english"),
                titles.get("romaji"),
                titles.get("native"),
            ])
        synonyms = media.get("synonyms")
        if isinstance(synonyms, (list, tuple)):
            candidates.extend(synonyms)

        variants: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            variants.append(text)
        return variants

    # ── Write API ──────────────────────────────────────────────────────────
    #
    # AniList mutations require an OAuth access token.  Reading a public list
    # only needs a username, so existing profiles may have no token at all —
    # `can_write()` reports that instead of failing mid-sync, and no existing
    # setup is disturbed.

    def can_write(self) -> bool:
        return bool(str(self._config.access_token or "").strip())

    def write_blocked_reason(self) -> str:
        if self.can_write():
            return ""
        return (
            "AniList needs an access token to be written to. Reading a public "
            "list only requires a username, so add a token in Connections to "
            "use AniList as a sync target."
        )

    # Neutral sync-pair categories mapped onto AniList list statuses.
    _STATUS_FOR_CATEGORY = {
        "watchlist": ANILIST_STATUS_PLAN_TO_WATCH,
        "collection": ANILIST_STATUS_COMPLETED,
    }

    def _resolve_media_id(self, item: dict) -> int | None:
        """Find the AniList media id for a canonical item.

        Items originating from AniList already carry one.  Anything else is
        mapped through the offline anime data: TMDB -> Fribb entry -> AniList id,
        with MAL as a fallback hop for entries that carry no AniList id.
        """
        ids = item.get("ids") or {}
        for value in (item.get("anilist_id"), ids.get("anilist")):
            try:
                if value and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                continue

        entry = None
        try:
            tmdb_id, _media_type = fribb_client.extract_tmdb(item.get("tmdb_id"))
            if tmdb_id:
                item_media_type = str(item.get("media_type") or "").strip().lower()
                entries = fribb_client.lookup_all_by_tmdb(int(tmdb_id)) if item_media_type == "tv" else []
                entry = self._select_tmdb_season_entry(entries, item)
                if entry is None:
                    entry = fribb_client.lookup_by_tmdb(int(tmdb_id))
            if entry is None:
                imdb_id = fribb_client.single_imdb_id(item.get("imdb_id") or ids.get("imdb"))
                if imdb_id:
                    entry = fribb_client.lookup_by_imdb(imdb_id)
        except Exception:
            logger.debug("AniList media-id lookup failed for %r", item.get("title"), exc_info=True)
            entry = None

        if isinstance(entry, dict):
            anilist_id = entry.get("anilist_id")
            if anilist_id:
                try:
                    return int(anilist_id)
                except (TypeError, ValueError):
                    pass
            mal_id = entry.get("mal_id")
            if mal_id:
                try:
                    resolved = self.get_anilist_id_by_mal(int(mal_id))
                except (TypeError, ValueError):
                    resolved = None
                if resolved:
                    return resolved

        mal_id = item.get("mal_id") or ids.get("mal")
        if mal_id:
            try:
                return self.get_anilist_id_by_mal(int(mal_id))
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _select_tmdb_season_entry(entries: list[dict], item: dict) -> dict | None:
        """Select AniList's season entry for canonical TMDB coordinates.

        TMDB/TVDB keep episodes beneath one show while AniList has a separate
        media id for many seasons and cours.  Prefer the entry whose Fribb TMDB
        season contains the canonical item.  For a show-level list item with no
        season, choose the earliest mapped season instead of rejecting the
        otherwise valid multi-entry mapping.
        """
        candidates = [entry for entry in entries if isinstance(entry, dict)]
        if not candidates:
            return None
        try:
            wanted_season = int(item.get("season")) if item.get("season") is not None else None
        except (TypeError, ValueError):
            wanted_season = None

        def tmdb_season(entry: dict) -> int | None:
            raw = entry.get("season")
            value = raw.get("tmdb") if isinstance(raw, dict) else None
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        if wanted_season is not None:
            exact = [entry for entry in candidates if tmdb_season(entry) == wanted_season]
            if len(exact) == 1:
                return exact[0]
            if exact:
                # Split cours can share a TMDB season. Use episode offsets to
                # pick the last cour that starts no later than this episode.
                try:
                    episode = int(item.get("episode"))
                except (TypeError, ValueError):
                    episode = None
                if episode is not None:
                    def offset(entry: dict) -> int:
                        raw = entry.get("episode_offset")
                        value = raw.get("tmdb", 0) if isinstance(raw, dict) else 0
                        try:
                            return int(value or 0)
                        except (TypeError, ValueError):
                            return 0
                    eligible = [entry for entry in exact if offset(entry) < episode]
                    if eligible:
                        return max(eligible, key=offset)
                return exact[0]

        ordered = sorted(
            enumerate(candidates),
            key=lambda pair: (tmdb_season(pair[1]) is None, tmdb_season(pair[1]) or 0, pair[0]),
        )
        return ordered[0][1]

    def save_entry(self, media_id: int, status: str, progress: int | None = None) -> dict | None:
        variables: dict = {"mediaId": int(media_id), "status": status}
        if progress is not None:
            variables["progress"] = int(progress)
        data = self._query(_SAVE_ENTRY_MUTATION, variables)
        if isinstance(data, dict):
            return data.get("SaveMediaListEntry")
        return None

    def delete_entry(self, entry_id: int) -> bool:
        data = self._query(_DELETE_ENTRY_MUTATION, {"id": int(entry_id)})
        if isinstance(data, dict):
            result = data.get("DeleteMediaListEntry") or {}
            return bool(result.get("deleted"))
        return False

    def add_to_list(self, items: list[dict], category: str) -> dict:
        """Set the list status for each item, adding it to the user's list."""
        status = self._STATUS_FOR_CATEGORY.get(category)
        if not status:
            raise ValueError(f"AniList has no status mapping for category {category!r}")
        totals = {"added": 0, "not_found": 0, "batches": 0}
        for item in items:
            media_id = self._resolve_media_id(item)
            if not media_id:
                totals["not_found"] += 1
                logger.info(
                    "AniList: no media id for %r (tmdb=%s) — cannot add",
                    item.get("title"), item.get("tmdb_id"),
                )
                continue
            # COMPLETED entries are given full progress so AniList does not show
            # them as completed-at-episode-0.
            progress = None
            if status == ANILIST_STATUS_COMPLETED:
                try:
                    episodes = int(item.get("total_episodes_count") or 0)
                except (TypeError, ValueError):
                    episodes = 0
                if episodes > 0:
                    progress = episodes
            if self.save_entry(media_id, status, progress) is not None:
                totals["added"] += 1
            else:
                totals["not_found"] += 1
        totals["batches"] = 1 if items else 0
        return totals

    def remove_from_list(self, items: list[dict], category: str) -> dict:
        """Delete media-list entries.

        AniList deletes by entry id.  Items that came from an AniList read carry
        one; anything else has to be located in the user's current list first.
        """
        status = self._STATUS_FOR_CATEGORY.get(category)
        if not status:
            raise ValueError(f"AniList has no status mapping for category {category!r}")
        entry_ids_by_media: dict[int, int] = {}
        for existing in self.get_status(status) or []:
            media_id = existing.get("anilist_id")
            entry_id = existing.get("anilist_entry_id")
            if media_id and entry_id:
                try:
                    entry_ids_by_media[int(media_id)] = int(entry_id)
                except (TypeError, ValueError):
                    continue

        totals = {"deleted": 0, "not_found": 0, "batches": 1 if items else 0}
        for item in items:
            entry_id = item.get("anilist_entry_id")
            if not entry_id:
                media_id = self._resolve_media_id(item)
                entry_id = entry_ids_by_media.get(int(media_id)) if media_id else None
            if not entry_id:
                totals["not_found"] += 1
                continue
            if self.delete_entry(int(entry_id)):
                totals["deleted"] += 1
            else:
                totals["not_found"] += 1
        return totals
