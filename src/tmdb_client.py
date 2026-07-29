"""Minimal TMDB metadata client used by the Library browser.

Fetches title/year/poster for a ``(tmdb_id, media_type)`` pair with the
user's own TMDB API key. Results are cached in a module-level TTL cache
shared across profiles — poster metadata is public and identical for every
user, so one profile's lookups warm the cache for everyone.

Accepts either a v3 API key (32-char hex, sent as the ``api_key`` query
parameter) or a v4 read access token (JWT, sent as a Bearer header).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
POSTER_BASE_URL = "https://image.tmdb.org/t/p/w342"
STILL_BASE_URL = "https://image.tmdb.org/t/p/w300"
REQUEST_TIMEOUT = 15
_CACHE_TTL_SECONDS = 24 * 3600
_MAX_CACHE_ENTRIES = 20000
_BATCH_WORKERS = 8

_cache_lock = threading.Lock()
# (media_type, tmdb_id) -> (expires_at_monotonic, details dict | None)
_cache: dict[tuple[str, int], tuple[float, dict | None]] = {}
# (tmdb_id, season) -> (expires_at_monotonic, {episode_number: details})
_season_cache: dict[tuple[int, int], tuple[float, dict[int, dict]]] = {}


def normalize_media_type(media_type: str) -> str:
    value = str(media_type or "").strip().lower()
    if value in {"tv", "show", "shows", "series", "anime"}:
        return "tv"
    return "movie"


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _season_cache.clear()


class TmdbError(RuntimeError):
    """Raised when TMDB rejects the request (bad key, network failure)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TmdbClient:
    def __init__(self, api_key: str):
        self._api_key = str(api_key or "").strip()
        self._session = requests.Session()
        # v4 read access tokens are JWTs; v3 keys are short hex strings.
        if self._api_key.startswith("eyJ") or len(self._api_key) > 60:
            self._session.headers["Authorization"] = f"Bearer {self._api_key}"
            self._use_query_key = False
        else:
            self._use_query_key = True

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def validate_key(self) -> bool:
        """Cheap authentication check; raises TmdbError with detail on failure."""
        self._request("/configuration")
        return True

    def _request(self, path: str, params: dict | None = None) -> dict:
        query = dict(params or {})
        if self._use_query_key:
            query["api_key"] = self._api_key
        try:
            resp = self._session.get(f"{TMDB_BASE_URL}{path}", params=query, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TmdbError(f"TMDB request failed: {exc}") from exc
        if resp.status_code == 401:
            raise TmdbError("TMDB rejected the API key", status_code=401)
        if resp.status_code == 404:
            raise TmdbError("Not found on TMDB", status_code=404)
        if not resp.ok:
            raise TmdbError(f"TMDB returned HTTP {resp.status_code}", status_code=resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise TmdbError("TMDB returned a non-JSON response") from exc

    def get_details(self, tmdb_id: int, media_type: str) -> dict | None:
        """Return {title, year, poster_url, ...} for the entry, or None if unknown.

        Cache negative results too so a deleted TMDB entry is not re-fetched on
        every page load.
        """
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return None
        if tmdb_id <= 0:
            return None
        kind = normalize_media_type(media_type)
        cache_key = (kind, tmdb_id)
        now = time.monotonic()
        with _cache_lock:
            cached = _cache.get(cache_key)
            if cached and cached[0] > now:
                return dict(cached[1]) if cached[1] is not None else None

        details: dict | None
        try:
            raw = self._request(f"/{kind}/{tmdb_id}")
            title = str(raw.get("title") or raw.get("name") or "").strip()
            date = str(raw.get("release_date") or raw.get("first_air_date") or "").strip()
            poster_path = str(raw.get("poster_path") or "").strip()
            details = {
                "tmdb_id": tmdb_id,
                "media_type": kind,
                "title": title,
                "year": date[:4] if len(date) >= 4 else "",
                "poster_url": f"{POSTER_BASE_URL}{poster_path}" if poster_path else "",
                "overview": str(raw.get("overview") or "")[:280],
            }
        except TmdbError as exc:
            if exc.status_code == 404:
                details = None
            else:
                # Auth/network failures must not be cached as "missing".
                raise

        with _cache_lock:
            if len(_cache) >= _MAX_CACHE_ENTRIES:
                _cache.clear()
            _cache[cache_key] = (now + _CACHE_TTL_SECONDS, dict(details) if details else None)
        return details

    def get_season_episodes(self, tmdb_id: int, season: int) -> dict[int, dict]:
        """Return {episode_number: {name, still_url, air_date}} for one TV season.

        One request covers every episode of the season, so callers resolving a
        whole watch history never fetch episode-by-episode. An unknown season
        caches as empty rather than erroring — old history rows can reference
        seasons TMDB has since renumbered.
        """
        try:
            tmdb_id = int(tmdb_id)
            season = int(season)
        except (TypeError, ValueError):
            return {}
        if tmdb_id <= 0 or season < 0:
            return {}
        cache_key = (tmdb_id, season)
        now = time.monotonic()
        with _cache_lock:
            cached = _season_cache.get(cache_key)
            if cached and cached[0] > now:
                return {number: dict(entry) for number, entry in cached[1].items()}

        episodes: dict[int, dict] = {}
        try:
            raw = self._request(f"/tv/{tmdb_id}/season/{season}")
            for entry in raw.get("episodes") or []:
                try:
                    number = int(entry.get("episode_number"))
                except (TypeError, ValueError):
                    continue
                still_path = str(entry.get("still_path") or "").strip()
                episodes[number] = {
                    "name": str(entry.get("name") or "").strip(),
                    "still_url": f"{STILL_BASE_URL}{still_path}" if still_path else "",
                    "air_date": str(entry.get("air_date") or "").strip(),
                }
        except TmdbError as exc:
            if exc.status_code != 404:
                raise

        with _cache_lock:
            if len(_season_cache) >= _MAX_CACHE_ENTRIES:
                _season_cache.clear()
            _season_cache[cache_key] = (now + _CACHE_TTL_SECONDS, {number: dict(entry) for number, entry in episodes.items()})
        return episodes

    def get_details_batch(self, refs: list[tuple[int, str]]) -> dict[tuple[str, int], dict]:
        """Fetch details for many (tmdb_id, media_type) pairs concurrently.

        Returns a map keyed on (normalized_media_type, tmdb_id). Individual
        failures are skipped; an auth failure aborts with TmdbError so the
        caller can tell the user their key is wrong.
        """
        unique: dict[tuple[str, int], tuple[int, str]] = {}
        for tmdb_id, media_type in refs:
            try:
                numeric_id = int(tmdb_id)
            except (TypeError, ValueError):
                continue
            if numeric_id <= 0:
                continue
            unique[(normalize_media_type(media_type), numeric_id)] = (numeric_id, media_type)
        if not unique:
            return {}

        results: dict[tuple[str, int], dict] = {}
        auth_error: TmdbError | None = None
        with ThreadPoolExecutor(max_workers=min(_BATCH_WORKERS, len(unique))) as pool:
            futures = {
                pool.submit(self.get_details, numeric_id, media_type): cache_key
                for cache_key, (numeric_id, media_type) in unique.items()
            }
            for future in as_completed(futures):
                cache_key = futures[future]
                try:
                    details = future.result()
                except TmdbError as exc:
                    if exc.status_code == 401 and auth_error is None:
                        auth_error = exc
                    continue
                except Exception:
                    logger.debug("TMDB lookup failed for %s", cache_key, exc_info=True)
                    continue
                if details:
                    results[cache_key] = details
        if auth_error is not None and not results:
            raise auth_error
        return results
