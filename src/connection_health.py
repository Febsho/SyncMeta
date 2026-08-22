"""Read-only provider connection checks and normalized health results."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

import requests

from .anilist_client import GRAPHQL_URL, REQUEST_TIMEOUT as ANILIST_TIMEOUT
from .config import MdbListConfig, PublicMetaDBConfig, SimklConfig, TraktConfig
from .mdblist_client import MdbListClient
from .publicmetadb_client import PublicMetaDBClient
from .simkl_client import SimklClient
from .tmdb_client import TmdbClient, TmdbError
from .trakt_client import TraktAuthenticationError, TraktClient

PROVIDERS = ("pmdb", "tmdb", "simkl", "trakt", "anilist", "mdblist")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(provider: str, status: str, code: str, message: str, *, readable: bool = False,
            writable: bool = False, identity: str = "", recovery_action: str = "none",
            retry_after: int | None = None) -> dict:
    value = {
        "provider": provider,
        "status": status,
        "code": code,
        "message": message,
        "checked_at": _now(),
        "capabilities": {"readable": bool(readable), "writable": bool(writable)},
        "identity": str(identity or ""),
        "recovery_action": recovery_action,
    }
    if retry_after is not None:
        value["retry_after"] = retry_after
    return value


def _configured(provider: str, credentials: dict) -> bool:
    row = credentials.get(provider) or {}
    if provider == "simkl":
        return bool(row.get("client_id") and row.get("access_token"))
    if provider == "trakt":
        return bool(row.get("client_id") and row.get("access_token"))
    if provider == "anilist":
        return bool(row.get("username"))
    if provider == "mdblist":
        # Either auth mode is enough: an OAuth token is a working credential,
        # and a profile that connected that way has no api key at all.
        return bool(row.get("api_key") or row.get("access_token"))
    return bool(row.get("api_key"))


def _error(provider: str, exc: Exception) -> dict:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(exc, TmdbError):
        status_code = exc.status_code
    retry_after = None
    if response is not None:
        try:
            retry_after = int(response.headers.get("Retry-After"))
        except (TypeError, ValueError):
            retry_after = None
    if isinstance(exc, TraktAuthenticationError) or status_code == 401:
        return _result(provider, "error", "auth_invalid", "Authentication was rejected.",
                       recovery_action="reconnect" if provider in {"trakt", "simkl", "anilist"} else "edit_credentials")
    if status_code == 403:
        return _result(provider, "error", "permission_missing", "The credential lacks permission or was revoked.",
                       recovery_action="reconnect" if provider in {"trakt", "simkl", "anilist"} else "edit_credentials")
    if status_code == 404 and provider == "anilist":
        return _result(provider, "error", "unknown_user", "The AniList username was not found.",
                       recovery_action="correct_username")
    if status_code == 429:
        return _result(provider, "degraded", "rate_limited", "The provider is rate limiting checks. Try again later.",
                       recovery_action="retry", retry_after=retry_after)
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return _result(provider, "degraded", "unavailable", "The provider could not be reached.", recovery_action="retry")
    if isinstance(status_code, int) and status_code >= 500:
        return _result(provider, "degraded", "unavailable", f"The provider returned HTTP {status_code}.", recovery_action="retry")
    return _result(provider, "error", "unknown", "The connection check failed.", recovery_action="retry")


def _check_anilist(credentials: dict) -> dict:
    row = credentials["anilist"]
    token = str(row.get("access_token") or "").strip()
    username = str(row.get("username") or "").strip()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        query = "query { Viewer { name } }"
        variables = {}
    else:
        query = "query ($name: String) { User(name: $name) { name } }"
        variables = {"name": username}
    response = requests.post(
        GRAPHQL_URL,
        headers=headers,
        json={"query": query, "variables": variables},
        timeout=ANILIST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json() or {}
    data = payload.get("data") or {}
    user = data.get("Viewer" if token else "User")
    if not isinstance(user, dict) or not user.get("name"):
        error = requests.HTTPError("AniList authentication failed" if token else "AniList user not found", response=response)
        response.status_code = 401 if token else 404
        raise error
    identity = str(user["name"])
    return _result("anilist", "healthy", "ok", "AniList is reachable.", readable=True,
                   writable=bool(token), identity=identity)


def check_provider(provider: str, credentials: dict, *, trakt_token_callback: Callable | None = None) -> dict:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    if not _configured(provider, credentials):
        return _result(provider, "unconfigured", "missing_credentials", "This provider is not configured.",
                       recovery_action="correct_username" if provider == "anilist" else "edit_credentials")
    try:
        row = credentials[provider]
        if provider == "pmdb":
            PublicMetaDBClient(PublicMetaDBConfig(api_key=row["api_key"])).get_lists()
            return _result(provider, "healthy", "ok", "PublicMetaDB is reachable.", readable=True, writable=True)
        if provider == "tmdb":
            TmdbClient(row["api_key"]).validate_key()
            return _result(provider, "healthy", "ok", "TMDB is reachable.", readable=True)
        if provider == "simkl":
            SimklClient(SimklConfig(client_id=row["client_id"], client_secret=row.get("client_secret", ""),
                                    access_token=row["access_token"])).get_activities()
            return _result(provider, "healthy", "ok", "SIMKL is reachable.", readable=True, writable=True)
        if provider == "trakt":
            client = TraktClient(
                TraktConfig(client_id=row["client_id"], client_secret=row.get("client_secret", ""),
                            access_token=row["access_token"], refresh_token=row.get("refresh_token", ""),
                            access_token_expires_at=row.get("access_token_expires_at", ""), enabled=True),
                token_refreshed_callback=trakt_token_callback,
            )
            client.get_last_activities()
            return _result(provider, "healthy", "ok", "Trakt is reachable.", readable=True, writable=True,
                           identity=row.get("username", ""))
        if provider == "anilist":
            return _check_anilist(credentials)
        mdblist_config = MdbListConfig(
            api_key=row.get("api_key", ""),
            access_token=row.get("access_token", ""),
        )
        MdbListClient(mdblist_config).get_user_lists()
        return _result(provider, "healthy", "ok", "MDBList is reachable.", readable=True,
                       writable=bool(row.get("access_token")))
    except Exception as exc:
        return _error(provider, exc)


def check_connections(credentials: dict, providers: list[str] | None = None,
                      *, trakt_token_callback: Callable | None = None) -> list[dict]:
    selected = list(dict.fromkeys(providers or PROVIDERS))
    unknown = [provider for provider in selected if provider not in PROVIDERS]
    if unknown:
        raise ValueError(f"Unknown provider: {unknown[0]}")
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(selected)))) as pool:
        futures = {
            pool.submit(check_provider, provider, credentials, trakt_token_callback=trakt_token_callback): provider
            for provider in selected
        }
        for future in as_completed(futures):
            provider = futures[future]
            try:
                results[provider] = future.result()
            except Exception as exc:  # defensive isolation between providers
                results[provider] = _error(provider, exc)
    return [results[provider] for provider in selected]
