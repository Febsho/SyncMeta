"""MDBList API client for loading a user's lists and items."""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import re
import secrets
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import MdbListConfig
from .rate_limit import RateLimiter, retry_on_rate_limit
from .http_timeouts import _env_timeout

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = (5, _env_timeout("SYNCMETA_MDBLIST_READ_TIMEOUT", 20))
# MDBList meters per-day on the free tier and returns X-RateLimit-* headers.
# This paces bursts; retry_on_rate_limit handles an actual 429 using those
# headers, including the absolute X-RateLimit-Reset epoch MDBList sends.
RATE_LIMIT_MAX = 100
RATE_LIMIT_WINDOW = 10.0


def _redact_secret_query(text: str) -> str:
    """Keep requests errors useful without echoing credentials in their URL."""
    return re.sub(
        r"(?i)([?&](?:apikey|api_key|access_token|refresh_token|client_secret)=)[^&\s]+",
        r"\1[redacted]",
        str(text or ""),
    )


class MdbListClient:
    """Client for the MDBList REST API."""

    _PUBLIC_LIST_CARD_RE = re.compile(r'<article class="related-list-card">(.*?)</article>', re.S)
    _HREF_RE = re.compile(r'<a href="(?P<href>/lists/[^"]+)"')
    _TITLE_RE = re.compile(r'<div class="related-list-meta__title">\s*<a [^>]+>(?P<title>.*?)</a>', re.S)
    _USER_RE = re.compile(r'<a class="related-list-meta__user" [^>]*>(?P<user>.*?)</a>', re.S)
    _TYPE_RE = re.compile(r'<span class="related-list-meta__type">(?P<type>.*?)</span>', re.S)
    _ITEMS_RE = re.compile(r'<span class="related-list-meta__items">(?P<items>\d+)\s+items</span>', re.S)
    _LIKES_RE = re.compile(r'<span class="related-list-meta__likes">\s*<span class="ui medium text">(?P<likes>\d+)</span>', re.S)
    _LIST_ID_RE = re.compile(r'href="/(?:movies|shows)/\?list=(?P<id>\d+)"')

    def __init__(self, config: MdbListConfig, cancel_requested_callback=None):
        self._config = config
        self._session = self._build_session()
        self._limiter = RateLimiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)
        self._cancel_requested_callback = cancel_requested_callback

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

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Accept": "application/json"})

        # allowed_methods stays GET-only on purpose. A write that timed out may
        # already have been applied, so retrying it duplicates list items or
        # watch records; write failures surface to the caller instead.
        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        try:
            from .api_logger import make_requests_hook
            session.hooks["response"].append(make_requests_hook("mdblist"))
        except Exception:
            pass
        return session

    # ── auth ───────────────────────────────────────────────────────────────
    # Two modes. An OAuth bearer token is preferred when present because it is
    # the one that carries the write scope; the apikey query parameter remains
    # for profiles that only ever pasted a key, which must keep working.

    def _auth_headers(self) -> dict:
        token = str(getattr(self._config, "access_token", "") or "").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _auth_params(self) -> dict:
        if self._auth_headers():
            return {}
        return {"apikey": self._config.api_key}

    def has_write_auth(self) -> bool:
        return bool(
            str(getattr(self._config, "access_token", "") or "").strip()
            or str(self._config.api_key or "").strip()
        )

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        request_params = dict(params or {})
        request_params.update(self._auth_params())
        url = f"{self._config.base_url}{path}"
        logger.debug("GET %s params=%s", url, request_params)
        self._check_cancelled()
        self._limiter.wait(self._cancel_requested_callback)
        response = self._session.get(
            url, params=request_params, headers=self._auth_headers(), timeout=REQUEST_TIMEOUT,
        )
        self._check_cancelled()
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise requests.HTTPError(
                _redact_secret_query(str(exc)), response=exc.response, request=exc.request,
            ) from None
        return response

    def _post(self, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
        return retry_on_rate_limit(
            lambda: self._post_once(path, payload, params),
            provider="MDBList",
            check_cancelled=self._check_cancelled,
        )

    def _post_once(self, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
        request_params = dict(params or {})
        request_params.update(self._auth_params())
        url = f"{self._config.base_url}{path}"
        logger.debug("POST %s", url)
        self._check_cancelled()
        self._limiter.wait(self._cancel_requested_callback)
        response = self._session.post(
            url,
            params=request_params,
            json=payload if payload is not None else {},
            headers=self._auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        self._check_cancelled()
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise requests.HTTPError(
                _redact_secret_query(str(exc)), response=exc.response, request=exc.request,
            ) from None
        try:
            return response.json() or {}
        except ValueError:
            return {}

    def get_user_lists(self) -> list[dict]:
        """Fetch the authenticated user's MDBList lists."""
        response = self._get("/lists/user/", {"sort": "rank", "unified": "false"})
        payload = response.json() or []
        items = [self._normalize_list_metadata(item) for item in payload]
        return [item for item in items if item]

    def search_public_lists(self, query: str) -> list[dict]:
        """Search public MDBList lists."""
        query = str(query or "").strip()
        if not query:
            return []

        attempts = [
            ("/search/lists", {"query": query}),
            ("/lists/search", {"query": query}),
        ]

        last_error: Exception | None = None
        for path, params in attempts:
            try:
                response = self._get(path, params)
                payload = response.json() or []
                batch = self._extract_list_results(payload)
                items = [self._normalize_list_metadata(item) for item in batch]
                normalized = [item for item in items if item and not item.get("private")]
                if normalized:
                    return normalized
            except requests.HTTPError as exc:
                last_error = exc
                if exc.response is not None and exc.response.status_code == 404:
                    continue
                raise

        fallback_items = self._search_public_lists_fallback(query)
        if fallback_items:
            return fallback_items
        if last_error:
            raise last_error
        return []

    def _search_public_lists_fallback(self, query: str) -> list[dict]:
        url = "https://mdblist.com/toplists/"
        self._check_cancelled()
        response = self._session.get(url, params={"public_list_name": query}, timeout=REQUEST_TIMEOUT)
        self._check_cancelled()
        response.raise_for_status()
        return self._parse_public_list_cards(response.text)

    @classmethod
    def _parse_public_list_cards(cls, page_html: str) -> list[dict]:
        results: list[dict] = []
        seen: set[tuple[int, str]] = set()

        for card_html in cls._PUBLIC_LIST_CARD_RE.findall(page_html or ""):
            href_match = cls._HREF_RE.search(card_html)
            title_match = cls._TITLE_RE.search(card_html)
            list_id_match = cls._LIST_ID_RE.search(card_html)
            media_match = cls._TYPE_RE.search(card_html)
            if not href_match or not title_match or not list_id_match or not media_match:
                continue

            mediatype_raw = html.unescape(media_match.group("type")).strip().lower()
            if mediatype_raw.startswith("movie"):
                mediatype = "movie"
            elif mediatype_raw.startswith("show") or mediatype_raw.startswith("tv"):
                mediatype = "show"
            else:
                continue

            list_id = int(list_id_match.group("id"))
            key = (list_id, mediatype)
            if key in seen:
                continue
            seen.add(key)

            href = href_match.group("href").strip("/")
            slug = href.split("/", 1)[1] if "/" in href else href.removeprefix("lists/")
            user_match = cls._USER_RE.search(card_html)
            items_match = cls._ITEMS_RE.search(card_html)
            likes_match = cls._LIKES_RE.search(card_html)

            results.append(
                {
                    "id": list_id,
                    "name": html.unescape(title_match.group("title")).strip(),
                    "slug": slug,
                    "user_name": html.unescape(user_match.group("user")).strip() if user_match else "",
                    "description": "",
                    "mediatype": mediatype,
                    "items": int(items_match.group("items")) if items_match else 0,
                    "likes": int(likes_match.group("likes")) if likes_match else 0,
                    "type": "public",
                    "private": False,
                }
            )

        return results

    def get_list_items(self, list_id: int) -> list[dict]:
        """Fetch all items for a list, following offset pagination."""
        offset = 0
        limit = 200
        items: list[dict] = []

        while True:
            response = self._get(
                f"/lists/{list_id}/items",
                {
                    "limit": limit,
                    "offset": offset,
                    "append_to_response": "genre",
                },
            )
            payload = response.json() or []
            batch = self._extract_items(payload)
            normalized = [self._normalize_item(item) for item in batch]
            items.extend(item for item in normalized if item)

            has_more = self._has_more(response, payload)
            if not has_more or not batch:
                break
            offset += limit

        logger.info("MDBList: fetched %d items for list %s", len(items), list_id)
        return items

    @staticmethod
    def _extract_items(payload: list | dict) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []

        if isinstance(payload.get("items"), list):
            return payload["items"]

        combined: list[dict] = []
        for key in ["movies", "shows", "tv", "results"]:
            values = payload.get(key)
            if isinstance(values, list):
                combined.extend(values)
        return combined

    @staticmethod
    def _extract_list_results(payload: list | dict) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []

        for key in ["results", "lists", "items", "data"]:
            values = payload.get(key)
            if isinstance(values, list):
                return values
        return []

    @staticmethod
    def _has_more(response: requests.Response, payload: list | dict) -> bool:
        header = str(response.headers.get("X-Has-More", "")).strip().lower()
        if header:
            return header == "true"
        if isinstance(payload, dict):
            for key in ["has_more", "hasMore", "next", "next_page"]:
                value = payload.get(key)
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)):
                    return bool(value)
                if isinstance(value, str) and value.strip():
                    return value.strip().lower() not in {"0", "false", "none", "null"}
        return False

    @staticmethod
    def _normalize_list_metadata(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None

        try:
            list_id = int(item.get("id"))
        except (TypeError, ValueError):
            return None

        mediatype = str(item.get("mediatype", "")).strip().lower()
        name = str(item.get("name", "")).strip()
        if mediatype not in {"movie", "show"} or not name:
            return None

        try:
            item_count = int(item.get("items", 0) or 0)
        except (TypeError, ValueError):
            item_count = 0

        try:
            likes = int(item.get("likes", 0) or 0)
        except (TypeError, ValueError):
            likes = 0

        return {
            "id": list_id,
            "name": name,
            "slug": str(item.get("slug", "")).strip(),
            "user_name": str(item.get("user_name", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "mediatype": mediatype,
            "items": item_count,
            "likes": likes,
            "type": str(item.get("type", "")).strip(),
            "private": bool(item.get("private", False)),
        }

    @staticmethod
    def _normalize_item(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None

        mediatype = str(item.get("mediatype", "")).strip().lower()
        if mediatype == "movie":
            media_type = "movie"
        elif mediatype == "show":
            media_type = "tv"
        else:
            return None

        ids = item.get("ids") or {}
        imdb_id = item.get("imdb_id") or ids.get("imdb")
        tmdb_id = ids.get("tmdb")
        tvdb_id = item.get("tvdb_id") or ids.get("tvdb")

        return {
            "title": item.get("title") or "Unknown",
            "year": item.get("release_year"),
            "media_type": media_type,
            "simkl_type": mediatype,
            "imdb_id": str(imdb_id) if imdb_id else None,
            "tmdb_id": str(tmdb_id) if tmdb_id else None,
            "mal_id": None,
            "anilist_id": None,
            "anidb_id": None,
            "tvdb_id": str(tvdb_id) if tvdb_id else None,
            "ids": ids,
            "status": None,
            "added_at": None,
        }

    # ── OAuth (PKCE) ───────────────────────────────────────────────────────
    # MDBList requires PKCE for every client, and every OAuth path needs its
    # trailing slash — without it the request fails. Authorization happens on
    # the site host, token exchange on the API host.

    @staticmethod
    def generate_pkce_pair() -> tuple[str, str]:
        """Return (code_verifier, code_challenge) for the S256 method."""
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    def build_authorize_url(self, redirect_uri: str, code_challenge: str, state: str = "") -> str:
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "read write",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if state:
            params["state"] = state
        return f"{self._config.site_url}/oauth/authorize/?{urlencode(params)}"

    def _token_request(self, data: dict) -> dict:
        url = f"{self._config.base_url}/oauth/token/"
        response = self._session.post(url, data=data, timeout=REQUEST_TIMEOUT)
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json() or {}
                detail = str(body.get("error_description") or body.get("error") or "").strip()
            except ValueError:
                detail = (response.text or "").strip()[:200]
            raise RuntimeError(
                f"MDBList token request failed ({response.status_code})"
                + (f": {detail}" if detail else "")
            )
        return response.json() or {}

    def exchange_code_for_token(self, code: str, code_verifier: str, redirect_uri: str) -> dict:
        return self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "code_verifier": code_verifier,
        })

    def refresh_access_token(self) -> dict:
        if not str(self._config.refresh_token or "").strip():
            raise RuntimeError("No MDBList refresh token stored")
        payload = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": self._config.refresh_token,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        })
        # Adopt the new token immediately so the in-flight call can be retried.
        if payload.get("access_token"):
            self._config.access_token = str(payload["access_token"])
        if payload.get("refresh_token"):
            self._config.refresh_token = str(payload["refresh_token"])
        return payload

    # ── sync surface ───────────────────────────────────────────────────────
    # Bodies are Trakt-shaped: {"movies": [...], "shows": [...]} with an ids
    # object. Watchlist is now part of the dedicated /watchlist surface, while
    # collection and watched history remain under /sync.

    _SYNC_PATHS = {
        "watchlist": "/watchlist/items",
        "collection": "/sync/collection",
        "history": "/sync/watched",
    }

    _SYNC_WRITE_PATHS = {
        "watchlist": ("/watchlist/items/add", "/watchlist/items/remove"),
        "collection": ("/sync/collection", "/sync/collection/remove"),
        "history": ("/sync/watched", "/sync/watched/remove"),
    }

    @classmethod
    def _sync_path(cls, category: str) -> str:
        path = cls._SYNC_PATHS.get(str(category or "").strip().lower())
        if not path:
            raise ValueError(f"MDBList has no sync endpoint for {category!r}")
        return path

    @staticmethod
    def _to_sync_payload(items: list[dict]) -> dict:
        """Group the app's items into MDBList's movies/shows request body.

        Items with neither an IMDB nor a TMDB id are dropped rather than sent:
        MDBList cannot resolve them, and a body full of id-less entries is how
        a write silently lands on the wrong title.
        """
        movies: list[dict] = []
        shows: list[dict] = []
        for item in items or []:
            ids: dict = {}
            imdb_id = str(item.get("imdb_id") or "").strip()
            tmdb_id = str(item.get("tmdb_id") or "").strip()
            if imdb_id:
                ids["imdb"] = imdb_id
            if tmdb_id.isdigit():
                ids["tmdb"] = int(tmdb_id)
            if not ids:
                continue
            entry: dict = {"ids": ids}
            if item.get("title"):
                entry["title"] = item["title"]
            if item.get("year"):
                entry["year"] = item["year"]
            watched_at = item.get("watched_at") or item.get("last_watched_at")
            if watched_at:
                entry["watched_at"] = watched_at
            (movies if item.get("media_type") == "movie" else shows).append(entry)
        return {"movies": movies, "shows": shows}

    @classmethod
    def _from_sync_payload(cls, payload: dict, category: str) -> list[dict]:
        """Flatten a /sync/* response into the app's item shape.

        The watched response nests the media under a "movie"/"show" key beside
        its timestamp; watchlist and collection return the entry directly.
        """
        out: list[dict] = []
        if not isinstance(payload, dict):
            return out
        for key, media_type in (("movies", "movie"), ("shows", "tv")):
            for row in payload.get(key) or []:
                if not isinstance(row, dict):
                    continue
                media = row.get("movie") or row.get("show") or row
                if not isinstance(media, dict):
                    continue
                ids = media.get("ids") or {}
                imdb_id = ids.get("imdb") or media.get("imdb_id")
                tmdb_id = ids.get("tmdb") or media.get("tmdb_id") or media.get("id")
                if not imdb_id and not tmdb_id:
                    continue
                out.append({
                    "title": media.get("title") or "Unknown",
                    "year": media.get("year") or media.get("release_year"),
                    "media_type": media_type,
                    "simkl_type": "movie" if media_type == "movie" else "show",
                    "imdb_id": str(imdb_id) if imdb_id else None,
                    "tmdb_id": str(tmdb_id) if tmdb_id else None,
                    "mal_id": None,
                    "anilist_id": None,
                    "anidb_id": None,
                    "tvdb_id": str(ids.get("tvdb")) if ids.get("tvdb") else None,
                    "ids": ids,
                    "status": None,
                    "added_at": row.get("listed_at") or row.get("collected_at"),
                    "watched_at": row.get("last_watched_at") or row.get("watched_at"),
                })
        return out

    def get_sync_items(self, category: str) -> list[dict]:
        """Read one sync category, preferring the current cursor pagination."""
        path = self._sync_path(category)
        offset = 0
        limit = 1000
        cursor = ""
        items: list[dict] = []
        while True:
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            else:
                # Kept as a fallback for older MDBList deployments. The current
                # API returns next_cursor and never reaches the second offset.
                params["offset"] = offset
            response = self._get(path, params)
            payload = response.json() or {}
            batch = self._from_sync_payload(payload, category)
            items.extend(batch)
            pagination = payload.get("pagination") if isinstance(payload, dict) else None
            pagination = pagination if isinstance(pagination, dict) else {}
            next_cursor = str(
                pagination.get("next_cursor")
                or response.headers.get("X-Next-Cursor", "")
                or ""
            ).strip()
            has_more = bool(next_cursor or pagination.get("has_more") or self._has_more(response, payload))
            if not has_more or not batch:
                break
            if next_cursor:
                if next_cursor == cursor:
                    break
                cursor = next_cursor
            else:
                offset += limit
        logger.info("MDBList: fetched %d %s item(s)", len(items), category)
        return items

    def add_sync_items(self, category: str, items: list[dict]) -> dict:
        payload = self._to_sync_payload(items)
        if not payload["movies"] and not payload["shows"]:
            return {"added": 0}
        paths = self._SYNC_WRITE_PATHS.get(str(category or "").strip().lower())
        if not paths:
            raise ValueError(f"MDBList has no sync endpoint for {category!r}")
        result = self._post(paths[0], payload)
        logger.info(
            "MDBList: added %d movie(s) and %d show(s) to %s",
            len(payload["movies"]), len(payload["shows"]), category,
        )
        return result

    def remove_sync_items(self, category: str, items: list[dict]) -> dict:
        payload = self._to_sync_payload(items)
        if not payload["movies"] and not payload["shows"]:
            return {"removed": 0}
        paths = self._SYNC_WRITE_PATHS.get(str(category or "").strip().lower())
        if not paths:
            raise ValueError(f"MDBList has no sync endpoint for {category!r}")
        result = self._post(paths[1], payload)
        logger.info(
            "MDBList: removed %d movie(s) and %d show(s) from %s",
            len(payload["movies"]), len(payload["shows"]), category,
        )
        return result

    # ── static lists ───────────────────────────────────────────────────────

    def create_list(self, name: str, private: bool = True) -> dict:
        return self._post("/lists/user/add", {"name": name, "private": bool(private)})

    def change_list_items(self, list_id, items: list[dict], action: str) -> dict:
        """Add or remove items on a static list. `action` is 'add' or 'remove'."""
        action = str(action or "").strip().lower()
        if action not in ("add", "remove"):
            raise ValueError(f"Unsupported MDBList list action {action!r}")
        # This endpoint takes bare id objects, not the ids-wrapped sync shape.
        movies, shows = [], []
        for item in items or []:
            entry: dict = {}
            imdb_id = str(item.get("imdb_id") or "").strip()
            tmdb_id = str(item.get("tmdb_id") or "").strip()
            if imdb_id:
                entry["imdb"] = imdb_id
            if tmdb_id.isdigit():
                entry["tmdb"] = int(tmdb_id)
            if not entry:
                continue
            (movies if item.get("media_type") == "movie" else shows).append(entry)
        if not movies and not shows:
            return {"changed": 0}
        return self._post(f"/lists/{list_id}/items/{action}", {"movies": movies, "shows": shows})
