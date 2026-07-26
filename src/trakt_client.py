"""Trakt API client for watchlist and list syncing."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import TraktConfig

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = (5, 6)
# Trakt accepts large /sync payloads; batch so one failure costs less work.
TRAKT_SYNC_BATCH_SIZE = 100
TOKEN_REFRESH_SKEW_SECONDS = 3600

DEFAULT_CATALOGS = {
    "trending-movies": {
        "name": "Trending Movies",
        "path": "/movies/trending",
        "media_kind": "movie",
    },
    "trending-series": {
        "name": "Trending Series",
        "path": "/shows/trending",
        "media_kind": "show",
    },
    "popular-movies": {
        "name": "Popular Movies",
        "path": "/movies/popular",
        "media_kind": "movie",
    },
    "popular-series": {
        "name": "Popular Series",
        "path": "/shows/popular",
        "media_kind": "show",
    },
    "recommended-movies": {
        "name": "Recommended Movies",
        "path": "/recommendations/movies",
        "media_kind": "movie",
    },
    "recommended-series": {
        "name": "Recommended Series",
        "path": "/recommendations/shows",
        "media_kind": "show",
    },
}


class TraktAuthenticationError(RuntimeError):
    """Raised when Trakt rejects the saved access token."""


class TraktClient:
    """Client for the Trakt API."""

    def __init__(self, config: TraktConfig, cancel_requested_callback=None, token_refreshed_callback=None):
        self._config = config
        self._session = self._build_session()
        self._cancel_requested_callback = cancel_requested_callback
        self._token_refreshed_callback = token_refreshed_callback
        self._refresh_lock = threading.Lock()
        self._refresh_attempted = False
        self._refresh_succeeded = False

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        try:
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _expires_at_from_token_payload(data: dict) -> str:
        try:
            expires_in = int(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in <= 0:
            return ""
        try:
            created_at = int(data.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0
        base = datetime.fromtimestamp(created_at, timezone.utc) if created_at > 0 else datetime.now(timezone.utc)
        return (base + timedelta(seconds=expires_in)).isoformat()

    def _access_token_expiring_soon(self) -> bool:
        expires_at = self._parse_datetime(getattr(self._config, "access_token_expires_at", ""))
        if expires_at is None:
            return False
        return expires_at <= datetime.now(timezone.utc) + timedelta(seconds=TOKEN_REFRESH_SKEW_SECONDS)

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
            pass

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "trakt-api-version": "2",
        })
        if self._config.client_id:
            session.headers["trakt-api-key"] = self._config.client_id
        if self._config.access_token:
            session.headers["Authorization"] = f"Bearer {self._config.access_token}"

        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        try:
            from .api_logger import make_requests_hook
            session.hooks["response"].append(make_requests_hook("trakt"))
        except Exception:
            pass
        return session

    def _attempt_token_refresh(self) -> bool:
        """Try to refresh the access token using the stored refresh_token.

        Uses a lock so concurrent calls only attempt one refresh; subsequent
        callers get the result of the first attempt.  Returns True if the
        session now has a valid access token.
        """
        if not (self._config.refresh_token and self._config.client_id and self._config.client_secret):
            return False
        with self._refresh_lock:
            if self._refresh_attempted:
                return self._refresh_succeeded
            self._refresh_attempted = True
            try:
                resp = self._session.post(
                    f"{self._config.base_url}/oauth/token",
                    json={
                        "refresh_token": self._config.refresh_token,
                        "client_id": self._config.client_id,
                        "client_secret": self._config.client_secret,
                        "grant_type": "refresh_token",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json() or {}
                new_access = str(data.get("access_token") or "").strip()
                new_refresh = str(data.get("refresh_token") or "").strip()
                new_expires_at = self._expires_at_from_token_payload(data)
                if not new_access:
                    logger.warning("Trakt token refresh returned no access_token")
                    return False
                self._config.access_token = new_access
                if new_refresh:
                    self._config.refresh_token = new_refresh
                if new_expires_at:
                    self._config.access_token_expires_at = new_expires_at
                self._session.headers["Authorization"] = f"Bearer {new_access}"
                logger.info("Trakt access token refreshed automatically")
                if self._token_refreshed_callback:
                    try:
                        self._token_refreshed_callback(new_access, new_refresh or self._config.refresh_token, new_expires_at)
                    except TypeError:
                        self._token_refreshed_callback(new_access, new_refresh or self._config.refresh_token)
                    except Exception as cb_exc:
                        logger.warning("Trakt token refresh callback failed: %s", cb_exc)
                self._refresh_succeeded = True
                return True
            except Exception as exc:
                logger.warning("Trakt token refresh failed: %s", exc)
                return False

    def _request_response(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self._config.base_url}{path}"
        self._check_cancelled()
        if not path.startswith("/oauth/") and self._access_token_expiring_soon():
            self._attempt_token_refresh()
        response = self._session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        self._check_cancelled()
        if response.status_code == 401:
            if self._attempt_token_refresh():
                response = self._session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
                self._check_cancelled()
                if response.status_code == 401:
                    raise TraktAuthenticationError("Trakt token expired, reconnect Trakt.")
            else:
                raise TraktAuthenticationError("Trakt token expired, reconnect Trakt.")
        response.raise_for_status()
        return response

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        response = self._request_response(method, path, **kwargs)
        if response.status_code == 204 or not response.text:
            return None
        return response.json()

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict | list | None:
        return self._request("POST", path, json=data)

    def get_last_activities(self) -> dict:
        """Return Trakt /sync/last_activities.  Raises on auth/network errors."""
        data = self._get("/sync/last_activities")
        return data if isinstance(data, dict) else {}

    def request_device_code(self) -> dict:
        data = self._post("/oauth/device/code", {"client_id": self._config.client_id})
        if not data:
            raise RuntimeError("Failed to start Trakt device authentication")
        return data

    def poll_device_token(self, device_code: str) -> dict:
        try:
            data = self._post("/oauth/device/token", {
                "code": device_code,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            })
            if not data:
                raise RuntimeError("Failed to retrieve Trakt device token")
            return data
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.text:
                try:
                    return exc.response.json()
                except ValueError:
                    pass
            raise

    def get_watchlist(self) -> list[dict]:
        raw = self._get("/sync/watchlist", params={"extended": "full"}) or []
        return [item for item in (self._normalize_watchlist_entry(entry) for entry in raw) if item]

    def get_liked_lists(self) -> list[dict]:
        liked_lists = []
        for meta in self.get_liked_lists_metadata():
            items = self.get_list_items(meta["user"], meta["slug"])
            liked_lists.append({**meta, "items": items})
        return liked_lists

    def get_personal_lists(self) -> list[dict]:
        personal_lists = []
        for meta in self.get_personal_lists_metadata():
            items = self.get_list_items(meta["user"], meta["slug"])
            personal_lists.append({**meta, "items": items})
        return personal_lists

    def get_liked_lists_metadata(self) -> list[dict]:
        raw = self._get("/users/likes/lists", params={"extended": "full", "limit": 100}) or []
        metadata = []
        for entry in raw:
            normalized = self._normalize_list_metadata(entry, source="liked")
            if normalized:
                metadata.append(normalized)
        return metadata

    def get_personal_lists_metadata(self) -> list[dict]:
        raw = self._get("/users/me/lists", params={"extended": "full", "limit": 100}) or []
        metadata = []
        for entry in raw:
            normalized = self._normalize_list_metadata(entry, source="personal")
            if normalized:
                metadata.append(normalized)
        return metadata

    def search_lists(self, query: str) -> list[dict]:
        query = str(query or "").strip()
        if not query:
            return []
        raw = self._get("/search/list", params={
            "query": query,
            "extended": "full",
            "limit": 20,
            "page": 1,
        }) or []
        results = []
        for entry in raw:
            normalized = self._normalize_list_metadata(entry, source="discover")
            if normalized:
                results.append(normalized)
        return results

    def get_list_items(self, username: str, slug: str) -> list[dict]:
        raw = self._get(f"/users/{username}/lists/{slug}/items/movie,show", params={"extended": "full"}) or []
        items = []
        for entry in raw:
            normalized = self._normalize_watchlist_entry(entry)
            if normalized:
                items.append(normalized)
        return items

    def get_default_catalog(self, catalog_key: str) -> list[dict]:
        catalog = DEFAULT_CATALOGS.get(catalog_key)
        if not catalog:
            return []

        raw = self._get(catalog["path"], params={"limit": 20, "extended": "full"}) or []
        items = []
        for entry in raw:
            normalized = self._normalize_catalog_entry(entry, catalog["media_kind"])
            if normalized:
                items.append(normalized)
        return items

    def get_watched_history(self, since: str | None = None, status_callback=None) -> list[dict]:
        history: list[dict] = []
        history.extend(self._get_paginated_history("/sync/history/movies", self._normalize_movie_history_entry, since=since, status_callback=status_callback, label="movies"))
        history.extend(self._get_paginated_history("/sync/history/episodes", self._normalize_episode_history_entry, since=since, status_callback=status_callback, label="episodes"))
        if since:
            history = [item for item in history if self._is_history_after(item, since)]
        return history

    def get_playback_progress(self) -> list[dict]:
        progress: list[dict] = []
        progress.extend(self._get_paginated_playback("/sync/playback/movies", self._normalize_movie_playback_entry))
        progress.extend(self._get_paginated_playback("/sync/playback/episodes", self._normalize_episode_playback_entry))
        return progress

    def _get_paginated_history(self, path: str, normalizer, since: str | None = None, status_callback=None, label: str = "") -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            self._check_cancelled()
            if status_callback:
                desc = label or path.split("/")[-1]
                status_callback(f"Fetching Trakt history ({desc}: {len(items)} fetched, page {page}…)")
            params: dict = {"page": page, "limit": 100, "extended": "full"}
            if since:
                params["start_at"] = since
            raw = self._get(path, params=params) or []
            if not raw:
                break
            for entry in raw:
                normalized = normalizer(entry)
                if normalized:
                    items.append(normalized)
            if not isinstance(raw, list) or len(raw) < 100:
                break
            page += 1
        return items

    def _get_paginated_playback(self, path: str, normalizer) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            self._check_cancelled()
            raw = self._get(path, params={"page": page, "limit": 100, "extended": "full"}) or []
            if not raw:
                break
            for entry in raw:
                normalized = normalizer(entry)
                if normalized:
                    items.append(normalized)
            if not isinstance(raw, list) or len(raw) < 100:
                break
            page += 1
        return items

    @staticmethod
    def _parse_history_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        try:
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _is_history_after(self, item: dict, since: str) -> bool:
        watched_at = self._parse_history_datetime(item.get("watched_at"))
        since_dt = self._parse_history_datetime(since)
        if since_dt is None:
            return True
        if watched_at is None:
            return False
        return watched_at > since_dt

    def _normalize_list_metadata(self, entry: dict, source: str) -> dict | None:
        list_data = entry.get("list") if isinstance(entry, dict) and entry.get("list") else entry
        if not isinstance(list_data, dict):
            return None

        user = list_data.get("user") or {}
        ids = list_data.get("ids") or {}
        username = (
            user.get("ids", {}).get("slug")
            or user.get("username")
            or self._config.username
            or "me"
        )
        slug = ids.get("slug") or ids.get("trakt")
        if not username or not slug:
            return None

        return {
            "name": list_data.get("name") or f"List {slug}",
            "description": list_data.get("description") or "",
            "user": str(username),
            "slug": str(slug),
            "trakt_id": ids.get("trakt"),
            "item_count": int(list_data.get("item_count") or 0),
            "likes": int(list_data.get("likes") or 0),
            "share_link": list_data.get("share_link") or "",
            "source": source,
            "catalog_key": "",
        }

    @staticmethod
    def _normalize_catalog_entry(entry: dict, media_kind: str) -> dict | None:
        media = entry.get(media_kind) if isinstance(entry, dict) and entry.get(media_kind) else entry
        if not isinstance(media, dict):
            return None

        ids = media.get("ids", {})
        if media_kind == "movie":
            media_type = "movie"
            trakt_type = "movies"
        else:
            media_type = "tv"
            trakt_type = "shows"

        return {
            "title": media.get("title", "Unknown"),
            "year": media.get("year"),
            "media_type": media_type,
            "simkl_type": trakt_type,
            "imdb_id": ids.get("imdb"),
            "tmdb_id": str(ids["tmdb"]) if ids.get("tmdb") else None,
            "mal_id": None,
            "anilist_id": None,
            "anidb_id": None,
            "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
            "ids": ids,
            "status": None,
            "added_at": entry.get("listed_at"),
        }

    @staticmethod
    def _normalize_watchlist_entry(entry: dict) -> dict | None:
        entry_type = entry.get("type")
        if entry_type == "movie":
            media = entry.get("movie")
            media_type = "movie"
            trakt_type = "movies"
        elif entry_type == "show":
            media = entry.get("show")
            media_type = "tv"
            trakt_type = "shows"
        else:
            return None

        if not media:
            return None

        ids = media.get("ids", {})
        return {
            "title": media.get("title", "Unknown"),
            "year": media.get("year"),
            "media_type": media_type,
            "simkl_type": trakt_type,
            "imdb_id": ids.get("imdb"),
            "tmdb_id": str(ids["tmdb"]) if ids.get("tmdb") else None,
            "mal_id": None,
            "anilist_id": None,
            "anidb_id": None,
            "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
            "ids": ids,
            "status": entry.get("listed_at"),
            "added_at": entry.get("listed_at"),
        }

    @staticmethod
    def _normalize_movie_history_entry(entry: dict) -> dict | None:
        movie = entry.get("movie") if isinstance(entry, dict) else None
        if not isinstance(movie, dict):
            return None
        ids = movie.get("ids", {})
        tmdb_id = ids.get("tmdb")
        if not tmdb_id:
            return None
        return {
            "tmdb_id": int(tmdb_id),
            "media_type": "movie",
            "watched_at": entry.get("watched_at"),
            "title": movie.get("title", "Unknown"),
        }

    @staticmethod
    def _normalize_episode_history_entry(entry: dict) -> dict | None:
        show = entry.get("show") if isinstance(entry, dict) else None
        episode = entry.get("episode") if isinstance(entry, dict) else None
        if not isinstance(show, dict) or not isinstance(episode, dict):
            return None
        show_ids = show.get("ids", {})
        tmdb_id = show_ids.get("tmdb")
        season = episode.get("season")
        number = episode.get("number")
        if not tmdb_id or season is None or number is None:
            return None
        # Season 0 = Trakt specials — PMDB has no S0 concept, skip them.
        # Episode 0 = pre-season specials/pilots stored as E0 — skip these too.
        if int(season) == 0 or int(number) == 0:
            return None
        return {
            "tmdb_id": int(tmdb_id),
            "media_type": "tv",
            "season": int(season),
            "episode": int(number),
            "watched_at": entry.get("watched_at"),
            "title": show.get("title", "Unknown"),
        }

    @staticmethod
    def _normalize_movie_playback_entry(entry: dict) -> dict | None:
        movie = entry.get("movie") if isinstance(entry, dict) else None
        if not isinstance(movie, dict):
            return None
        ids = movie.get("ids", {})
        tmdb_id = ids.get("tmdb")
        runtime = movie.get("runtime")
        progress = entry.get("progress")
        if not tmdb_id or runtime in (None, 0) or progress is None:
            return None
        runtime_ms = int(runtime) * 60_000
        position_ms = int(round(runtime_ms * (float(progress) / 100.0)))
        # No useful resume point if position is zero (just started or rounding).
        if position_ms <= 0:
            return None
        return {
            "tmdb_id": int(tmdb_id),
            "media_type": "movie",
            "position_ms": position_ms,
            "runtime_ms": runtime_ms,
            "progress": float(progress),
            "paused_at": entry.get("paused_at"),
            "title": movie.get("title", "Unknown"),
        }

    @staticmethod
    def _normalize_episode_playback_entry(entry: dict) -> dict | None:
        show = entry.get("show") if isinstance(entry, dict) else None
        episode = entry.get("episode") if isinstance(entry, dict) else None
        if not isinstance(show, dict) or not isinstance(episode, dict):
            return None
        show_ids = show.get("ids", {})
        tmdb_id = show_ids.get("tmdb")
        runtime = episode.get("runtime")
        progress = entry.get("progress")
        season = episode.get("season")
        number = episode.get("number")
        if not tmdb_id or runtime in (None, 0) or progress is None or season is None or number is None:
            return None
        # Season 0 = Trakt specials — PMDB has no S0 concept, skip them.
        # Episode 0 = pre-season specials/pilots stored as E0 — skip these too.
        if int(season) == 0 or int(number) == 0:
            return None
        runtime_ms = int(runtime) * 60_000
        position_ms = int(round(runtime_ms * (float(progress) / 100.0)))
        # No useful resume point if position is zero (just started or rounding).
        if position_ms <= 0:
            return None
        return {
            "tmdb_id": int(tmdb_id),
            "media_type": "tv",
            "season": int(season),
            "episode": int(number),
            "position_ms": position_ms,
            "runtime_ms": runtime_ms,
            "progress": float(progress),
            "paused_at": entry.get("paused_at"),
            "title": show.get("title", "Unknown"),
        }

    # ── Write API ──────────────────────────────────────────────────────────
    #
    # Trakt's /sync endpoints take items grouped by media kind, each identified
    # by whatever external ids are known.  The OAuth token obtained by the
    # existing device flow already carries write scope, so no re-authentication
    # is needed to use these.

    @staticmethod
    def _write_ids(item: dict) -> dict:
        """Build a Trakt `ids` object from a canonical item.

        Trakt matches on any provided id, so send every one we know and let it
        pick.  Sending an empty object would silently match nothing.
        """
        ids: dict = {}
        source_ids = item.get("ids") or {}

        def _first(*values):
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
            return ""

        trakt_id = _first(item.get("trakt_id"), source_ids.get("trakt"))
        if trakt_id.isdigit():
            ids["trakt"] = int(trakt_id)
        imdb_id = _first(item.get("imdb_id"), source_ids.get("imdb"))
        if imdb_id:
            ids["imdb"] = imdb_id
        tmdb_id = _first(item.get("tmdb_id"), source_ids.get("tmdb"))
        if tmdb_id.isdigit():
            ids["tmdb"] = int(tmdb_id)
        tvdb_id = _first(item.get("tvdb_id"), source_ids.get("tvdb"))
        if tvdb_id.isdigit():
            ids["tvdb"] = int(tvdb_id)
        return ids

    @classmethod
    def _build_sync_payload(cls, items: list[dict], *, with_watched_at: bool = False) -> dict:
        """Group canonical items into Trakt's {movies: [...], shows: [...]} shape.

        When an item carries season/episode numbers the show is expressed as a
        nested seasons/episodes tree, which is how Trakt scopes history to
        individual episodes rather than a whole show.
        """
        movies: list[dict] = []
        shows_by_key: dict[str, dict] = {}

        for item in items:
            ids = cls._write_ids(item)
            if not ids:
                continue
            media_type = str(item.get("media_type") or "").strip().lower()
            watched_at = item.get("watched_at") if with_watched_at else None

            if media_type == "movie":
                entry: dict = {"ids": ids}
                if watched_at:
                    entry["watched_at"] = watched_at
                movies.append(entry)
                continue

            # Group episodes of the same show under one show entry.
            key = json.dumps(ids, sort_keys=True)
            show = shows_by_key.setdefault(key, {"ids": ids})
            season_number = item.get("season")
            episode_number = item.get("episode")
            if season_number is None or episode_number is None:
                continue
            try:
                season_number = int(season_number)
                episode_number = int(episode_number)
            except (TypeError, ValueError):
                continue

            seasons = show.setdefault("seasons", [])
            season = next((s for s in seasons if s.get("number") == season_number), None)
            if season is None:
                season = {"number": season_number, "episodes": []}
                seasons.append(season)
            episode: dict = {"number": episode_number}
            if watched_at:
                episode["watched_at"] = watched_at
            season["episodes"].append(episode)

        payload: dict = {}
        if movies:
            payload["movies"] = movies
        if shows_by_key:
            payload["shows"] = list(shows_by_key.values())
        return payload

    def _sync_write(self, path: str, items: list[dict], *, with_watched_at: bool = False) -> dict:
        """POST canonical items to a Trakt /sync endpoint in batches."""
        totals: dict = {"added": 0, "deleted": 0, "not_found": 0, "batches": 0}
        for start in range(0, len(items), TRAKT_SYNC_BATCH_SIZE):
            chunk = items[start:start + TRAKT_SYNC_BATCH_SIZE]
            payload = self._build_sync_payload(chunk, with_watched_at=with_watched_at)
            if not payload:
                continue
            response = self._post(path, payload) or {}
            totals["batches"] += 1
            for bucket in ("added", "deleted", "existing", "updated"):
                counts = response.get(bucket)
                if isinstance(counts, dict):
                    key = "deleted" if bucket == "deleted" else "added"
                    totals[key] += sum(int(v or 0) for v in counts.values() if isinstance(v, (int, float)))
            not_found = response.get("not_found")
            if isinstance(not_found, dict):
                totals["not_found"] += sum(
                    len(v) for v in not_found.values() if isinstance(v, list)
                )
        return totals

    def add_to_watchlist(self, items: list[dict]) -> dict:
        return self._sync_write("/sync/watchlist", items)

    def remove_from_watchlist(self, items: list[dict]) -> dict:
        return self._sync_write("/sync/watchlist/remove", items)

    def add_to_collection(self, items: list[dict]) -> dict:
        return self._sync_write("/sync/collection", items)

    def remove_from_collection(self, items: list[dict]) -> dict:
        return self._sync_write("/sync/collection/remove", items)

    def add_to_history(self, items: list[dict]) -> dict:
        return self._sync_write("/sync/history", items, with_watched_at=True)

    def remove_from_history(self, items: list[dict]) -> dict:
        return self._sync_write("/sync/history/remove", items)
