"""Flask web UI for persistent SIMKL/AniList -> PublicMetaDB sync profiles."""

from __future__ import annotations

import copy
import contextvars
import gzip as _gzip
import logging
import os
import queue
import re
import secrets
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.env_settings import SettingsError, SettingsStore, apply_overrides
from src.env_settings import DEFAULT_SETTINGS_FILENAME as _SETTINGS_FILENAME
from src.env_settings import describe as _describe_settings

# ── environment bootstrap ────────────────────────────────────────────────────
# This has to happen before the src modules below are imported: every tunable is
# an `os.getenv` read into a module constant at import time, so a value that
# arrives afterwards is simply never seen. `load_dotenv` used to run further
# down, which is why .env could set web.py's constants but not sync_service's.
_BASE_ENV = dict(os.environ)  # what the process was given, before .env/overrides
load_dotenv(Path(__file__).resolve().parent / ".env")
PROFILE_STORE_FILE = Path(
    os.getenv("PROFILE_STORE_FILE", str(Path(__file__).resolve().parent / "data" / "profiles.json"))
)
_ENV_FILE_ENV = {k: v for k, v in os.environ.items() if k not in _BASE_ENV}
_settings_store = SettingsStore(PROFILE_STORE_FILE.parent / _SETTINGS_FILENAME)
apply_overrides(_settings_store)
# The panel reports where each value came from, so it needs the environment as
# it stood before the override layer was applied.
_ENV_BEFORE_OVERRIDES = {**_BASE_ENV, **_ENV_FILE_ENV}

from src.config import (  # noqa: E402
    AniListConfig,
    AppConfig,
    MdbListConfig,
    PublicMetaDBConfig,
    SimklConfig,
    SyncConfig,
    SyncPair,
    TraktConfig,
    validate_config,
)
from src.anilist_client import (
    OAUTH_PIN_REDIRECT_URI as ANILIST_PIN_REDIRECT_URI,
    AniListClient,
    build_authorize_url as anilist_build_authorize_url,
    exchange_code_for_token as anilist_exchange_code_for_token,
)
from src.matcher import ItemMatcher
from src.mdblist_client import MdbListClient
from src.publicmetadb_client import PublicMetaDBClient
from src.profile_store import ProfileStore, merge_credentials, normalize_credentials, normalize_profile_options
from src.simkl_client import SimklClient
from src.tmdb_client import TmdbClient, TmdbError, normalize_media_type as tmdb_media_kind
from src.sync_service import SyncCancelled, SyncService, SyncStats, _status_list_name
from src.trakt_client import TraktAuthenticationError, TraktClient
from src.cross_sync import CrossSyncService
from src.providers import (
    ALL_CATEGORIES,
    ALL_PAIR_MODES,
    ALL_REMOVAL_MODES,
    CATEGORY_LABELS,
    PAIR_MODE_LABELS,
    PROVIDER_ORDER,
    REMOVAL_MODE_LABELS,
    TWO_WAY_REMOVAL_MODES,
    AniListAdapter,
    MdbListAdapter,
    PmdbAdapter,
    SimklAdapter,
    TraktAdapter,
)
from src import log_capture
from src.connection_health import PROVIDERS as HEALTH_PROVIDERS, check_connections

app = Flask(__name__, template_folder="templates", static_folder="static")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
log_capture.install(level=logging.INFO)
logger = logging.getLogger("web")
logger.setLevel(logging.INFO)

SCHEDULER_POLL_SECONDS = max(5, int(os.getenv("SYNCMETA_SCHEDULER_POLL_SECONDS", "5") or "5"))
MAX_CONCURRENT_SYNCS = max(1, int(os.getenv("SYNCMETA_MAX_CONCURRENT_SYNCS", "1") or "1"))
# Head start for the web tier before the scheduler claims anything. See
# ProfileScheduler._run: the first request is what starts the scheduler, so
# without this it competes with the stampede it just caused.
SCHEDULER_STARTUP_GRACE_SECONDS = max(0, int(os.getenv("SYNCMETA_SCHEDULER_STARTUP_GRACE_SECONDS", "20") or "20"))
# How many profiles one poll may claim. Beyond the pool's capacity a claim only
# fills the queue while showing the profile as running.
SCHEDULER_CLAIM_BATCH = max(1, int(os.getenv("SYNCMETA_SCHEDULER_CLAIM_BATCH", str(MAX_CONCURRENT_SYNCS)) or "1"))
SESSION_COOKIE_NAME = "syncmeta_session"
ACCESS_COOKIE_NAME = "syncmeta_site_access"
SESSION_TTL_SECONDS = int(os.getenv("SYNCMETA_SESSION_TTL_SECONDS", "2592000"))
LOGIN_MAX_ATTEMPTS = int(os.getenv("SYNCMETA_LOGIN_MAX_ATTEMPTS", "10"))
LOGIN_WINDOW_SECONDS = int(os.getenv("SYNCMETA_LOGIN_WINDOW_SECONDS", "900"))
SITE_ACCESS_PASSWORD = os.getenv("SITE_ACCESS_PASSWORD", "").strip()
ACCESS_MAX_ATTEMPTS = int(os.getenv("SYNCMETA_ACCESS_MAX_ATTEMPTS", "10"))
ACCESS_WINDOW_SECONDS = int(os.getenv("SYNCMETA_ACCESS_WINDOW_SECONDS", "900"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_COOKIE_NAME = "syncmeta_admin"
ADMIN_SESSION_TTL = 3600
PROFILE_LOG_LIMIT = max(100, int(os.getenv("SYNCMETA_PROFILE_LOG_LIMIT", "500") or "500"))
_server_start_time = time.time()

SIMKL_STATUS_BY_LABEL = {
    "watching": "watching",
    "plan to watch": "plantowatch",
    "completed": "completed",
    "on hold": "hold",
    "dropped": "dropped",
}

ANILIST_STATUS_BY_LABEL = {
    "watching": "CURRENT",
    "planning": "PLANNING",
    "completed": "COMPLETED",
    "completed ona": "COMPLETED_ONA",
    "completed ova": "COMPLETED_OVA",
    "completed movie": "COMPLETED_MOVIE",
    "paused": "PAUSED",
    "dropped": "DROPPED",
}

_profile_store = ProfileStore(PROFILE_STORE_FILE)
_scheduler_lock = threading.Lock()
_scheduler_started = False
_log_profile_id: contextvars.ContextVar[str] = contextvars.ContextVar("syncmeta_profile_id", default="")


def _stable_session_secret() -> str:
    explicit = str(os.getenv("SYNCMETA_SESSION_SECRET", "")).strip()
    if explicit:
        return explicit
    master = str(os.getenv("SYNCMETA_MASTER_KEY", "")).strip()
    if master:
        return master
    key_file = Path(os.getenv("SYNCMETA_MASTER_KEY_FILE", PROFILE_STORE_FILE.parent / "profiles.key"))
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "syncmeta-dev-session-secret"


class ServerSessionStore:
    """Signed cookie session store with legacy in-memory-token fallback."""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS, salt: str = "syncmeta-session"):
        self._ttl_seconds = ttl_seconds
        self._signer = URLSafeTimedSerializer(_stable_session_secret(), salt=salt)
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self._revoked: dict[str, float] = {}
        # Sessions are stateless signed tokens, so there is no list of issued
        # cookies to walk when a profile needs every device signed out. Each
        # token carries the profile's epoch instead; bumping it strands every
        # token minted before the bump. Best effort, like _revoked: an in-memory
        # counter does not survive a process restart.
        self._epochs: dict[str, int] = {}

    def create(self, profile_id: str) -> str:
        now = time.time()
        with self._lock:
            epoch = self._epochs.get(profile_id, 0)
        payload = {
            "profile_id": profile_id,
            "nonce": secrets.token_urlsafe(8),
            "iat": int(now),
            "epoch": epoch,
        }
        return self._signer.dumps(payload)

    def destroy_profile_sessions(self, profile_id: str) -> None:
        """Invalidate every signed session previously issued for a profile."""
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            return
        with self._lock:
            self._epochs[profile_id] = self._epochs.get(profile_id, 0) + 1

    def get_profile_id(self, token: str | None) -> str | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._prune_revoked_locked(now)
            if token in self._revoked:
                return None
        try:
            payload = self._signer.loads(token, max_age=self._ttl_seconds)
            if not isinstance(payload, dict):
                return None
            profile_id = str(payload.get("profile_id", "")).strip()
            if not profile_id:
                return None
            with self._lock:
                current_epoch = self._epochs.get(profile_id, 0)
            if int(payload.get("epoch", 0) or 0) < current_epoch:
                return None
            return profile_id
        except SignatureExpired:
            return None
        except BadSignature:
            pass

        # Legacy support for cookies issued before signed sessions existed.
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return None
            if session["expires_at"] <= now:
                self._sessions.pop(token, None)
                return None
            session["expires_at"] = now + self._ttl_seconds
            return session["profile_id"]

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        expires_at = time.time() + self._ttl_seconds
        with self._lock:
            self._sessions.pop(token, None)
            self._revoked[token] = expires_at

    def _prune_revoked_locked(self, now: float) -> None:
        stale = [token for token, expires_at in self._revoked.items() if expires_at <= now]
        for token in stale:
            self._revoked.pop(token, None)


class ProfileLogStore:
    """Bounded in-memory log buffer partitioned by profile id."""

    def __init__(self, per_profile_limit: int = PROFILE_LOG_LIMIT):
        self._per_profile_limit = per_profile_limit
        self._lock = threading.RLock()
        self._seq = 0
        self._entries: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=self._per_profile_limit))

    def append(self, profile_id: str, record: logging.LogRecord) -> None:
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            return
        message = _sanitize_error_text(record.getMessage())
        if not message:
            return
        with self._lock:
            self._seq += 1
            self._entries[profile_id].append({
                "id": self._seq,
                "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            })

    def get(self, profile_id: str, after: int = 0, limit: int = 200) -> dict:
        profile_id = str(profile_id or "").strip()
        limit = min(max(int(limit or 200), 1), self._per_profile_limit)
        after = max(int(after or 0), 0)
        with self._lock:
            entries = list(self._entries.get(profile_id, ()))
        filtered = [entry for entry in entries if int(entry.get("id", 0)) > after]
        if len(filtered) > limit:
            filtered = filtered[-limit:]
        latest_id = entries[-1]["id"] if entries else after
        return {"entries": filtered, "latest_id": latest_id}

    def clear(self, profile_id: str) -> None:
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            return
        with self._lock:
            self._entries.pop(profile_id, None)


class ProfileLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        profile_id = str(getattr(record, "profile_id", "") or _log_profile_id.get("") or "").strip()
        if profile_id:
            _profile_log_store.append(profile_id, record)


class PendingPkceStore:
    """Short-lived PKCE verifiers, held server-side per profile.

    The verifier is the proof that the code being exchanged belongs to the
    request that started the flow, so it must never travel through the browser.
    In memory and best-effort across a restart, like the session revocation set:
    losing one only means the user presses Connect again.
    """

    def __init__(self, ttl_seconds: int = 900):
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, str, float]] = {}

    def put(self, profile_id: str, verifier: str, redirect_uri: str) -> None:
        with self._lock:
            self._prune_locked()
            self._pending[profile_id] = (verifier, redirect_uri, time.time() + self._ttl_seconds)

    def take(self, profile_id: str) -> tuple[str, str] | None:
        """Single use: an authorization code may only be exchanged once."""
        with self._lock:
            self._prune_locked()
            entry = self._pending.pop(profile_id, None)
        if not entry:
            return None
        verifier, redirect_uri, _expires = entry
        return verifier, redirect_uri

    def _prune_locked(self) -> None:
        now = time.time()
        for key in [k for k, (_v, _r, exp) in self._pending.items() if exp <= now]:
            self._pending.pop(key, None)


class LoginAttemptLimiter:
    """Sliding-window limiter for profile login attempts."""

    def __init__(self, max_attempts: int = LOGIN_MAX_ATTEMPTS, window_seconds: int = LOGIN_WINDOW_SECONDS):
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._lock = threading.RLock()
        self._attempts: dict[str, list[float]] = {}

    def is_limited(self, key: str) -> bool:
        with self._lock:
            attempts = self._prune_locked(key)
            return len(attempts) >= self._max_attempts

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            attempts = self._prune_locked(key)
            attempts.append(now)
            self._attempts[key] = attempts

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def _prune_locked(self, key: str) -> list[float]:
        now = time.time()
        attempts = [stamp for stamp in self._attempts.get(key, []) if now - stamp < self._window_seconds]
        if attempts:
            self._attempts[key] = attempts
        else:
            self._attempts.pop(key, None)
        return attempts


_session_store = ServerSessionStore()
_mdblist_pkce_store = PendingPkceStore()
_login_limiter = LoginAttemptLimiter()
_access_store = ServerSessionStore(salt="syncmeta-site-access")
_access_limiter = LoginAttemptLimiter(max_attempts=ACCESS_MAX_ATTEMPTS, window_seconds=ACCESS_WINDOW_SECONDS)
_profile_log_store = ProfileLogStore()
_profile_log_handler = ProfileLogHandler()
_profile_log_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_profile_log_handler)


_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|authorization)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(bearer)\s+[A-Za-z0-9._\-]+"),
]


def _sanitize_error_text(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)
    return cleaned


def _stats_to_summary_dict(stats: SyncStats, run_id: str = "") -> dict:
    data = asdict(stats)
    data["error_count"] = len(data.pop("errors", []))
    data.pop("unresolved_items", None)
    data.pop("dry_run_preview", None)
    data.pop("sample_failed_titles", None)
    data.pop("sample_unresolved_titles", None)
    data["has_details"] = bool(
        data.get("error_count")
        or data.get("items_skipped_unresolved")
        or data.get("items_skipped_fingerprint")
    )
    if run_id:
        data["run_id"] = run_id
    return data


def _stats_to_detail_dict(stats: SyncStats) -> dict:
    data = asdict(stats)
    data["errors"] = [_sanitize_error_text(item) for item in data.get("errors", []) if str(item or "").strip()]
    data["error_count"] = len(data["errors"])
    data["sample_failed_titles"] = [str(item) for item in data.get("sample_failed_titles", []) if str(item or "").strip()]
    data["sample_unresolved_titles"] = [str(item) for item in data.get("sample_unresolved_titles", []) if str(item or "").strip()]
    data["has_details"] = bool(
        data.get("errors")
        or data.get("unresolved_items")
        or data.get("sample_failed_titles")
        or data.get("sample_unresolved_titles")
        or data.get("items_skipped_fingerprint")
    )
    return data


def _sanitize_run_detail(run: dict) -> dict:
    sanitized = copy.deepcopy(run or {})
    sanitized["error_message"] = _sanitize_error_text(str(sanitized.get("error_message", "") or ""))
    rows = []
    for row in sanitized.get("rows", []) or []:
        if not isinstance(row, dict):
            continue
        next_row = copy.deepcopy(row)
        next_row["errors"] = [_sanitize_error_text(item) for item in (next_row.get("errors") or []) if str(item or "").strip()]
        next_row["error_count"] = len(next_row["errors"])
        rows.append(next_row)
    sanitized["rows"] = rows
    return sanitized


def _stats_to_dict(stats: SyncStats) -> dict:
    return _stats_to_summary_dict(stats)


def _config_from_profile(profile: dict, dry_run: bool = False, sync_modes: dict | None = None) -> AppConfig:
    credentials = normalize_credentials(profile.get("credentials"))
    options = normalize_profile_options(profile.get("options"))
    activity_state = profile.get("activity_state", {}) if isinstance(profile.get("activity_state"), dict) else {}
    anilist_username = credentials["anilist"]["username"]
    trakt_username = credentials["trakt"]["username"]
    modes = {
        "lists": True,
        "history": options["activity_history_source"] != "off",
        "resume": options["activity_resume_source"] != "off",
    }
    if isinstance(sync_modes, dict):
        modes["lists"] = bool(sync_modes.get("lists", False))
        modes["history"] = bool(sync_modes.get("history", False)) and options["activity_history_source"] != "off"
        modes["resume"] = bool(sync_modes.get("resume", False)) and options["activity_resume_source"] != "off"

    return AppConfig(
        simkl=SimklConfig(
            client_id=credentials["simkl"]["client_id"],
            client_secret=credentials["simkl"]["client_secret"],
            access_token=credentials["simkl"]["access_token"],
            selected_statuses=credentials["simkl"]["selected_statuses"],
        ),
        anilist=AniListConfig(
            username=anilist_username,
            access_token=credentials["anilist"]["access_token"],
            enabled=bool(anilist_username),
            selected_statuses=credentials["anilist"]["selected_statuses"],
        ),
        trakt=TraktConfig(
            client_id=credentials["trakt"]["client_id"],
            client_secret=credentials["trakt"]["client_secret"],
            access_token=credentials["trakt"]["access_token"],
            refresh_token=credentials["trakt"]["refresh_token"],
            access_token_expires_at=credentials["trakt"]["access_token_expires_at"],
            username=trakt_username,
            enabled=bool(credentials["trakt"]["client_id"] and credentials["trakt"]["access_token"]),
            sync_watchlist=credentials["trakt"]["sync_watchlist"],
            sync_watchlist_movies=credentials["trakt"]["sync_watchlist_movies"],
            sync_watchlist_shows=credentials["trakt"]["sync_watchlist_shows"],
            sync_liked_lists=credentials["trakt"]["sync_liked_lists"],
            selected_lists=credentials["trakt"]["selected_lists"],
        ),
        mdblist=MdbListConfig(
            api_key=credentials["mdblist"]["api_key"],
            client_id=credentials["mdblist"].get("client_id", ""),
            client_secret=credentials["mdblist"].get("client_secret", ""),
            access_token=credentials["mdblist"].get("access_token", ""),
            refresh_token=credentials["mdblist"].get("refresh_token", ""),
            access_token_expires_at=credentials["mdblist"].get("access_token_expires_at", ""),
            # An OAuth token is a working credential on its own, so a profile
            # that connected via OAuth and never pasted an API key is enabled.
            enabled=bool(
                (credentials["mdblist"]["api_key"] or credentials["mdblist"].get("access_token"))
                and credentials["mdblist"]["selected_lists"]
            ),
            selected_lists=credentials["mdblist"]["selected_lists"],
        ),
        pmdb=PublicMetaDBConfig(api_key=credentials["pmdb"]["api_key"]),
        sync=SyncConfig(
            remove_missing=options["remove_missing"],
            delete_disabled_lists=options["delete_disabled_lists"],
            dry_run=dry_run,
            media_types=options["media_types"],
            simkl_sync_watched_history=modes["history"] and options["activity_history_source"] == "simkl",
            simkl_history_anime_only=bool(options.get("simkl_history_anime_only", False)),
            trakt_sync_watched_history=modes["history"] and options["activity_history_source"] == "trakt",
            simkl_history_cursor=str(activity_state.get("simkl_history_cursor", "") or "").strip(),
            trakt_history_cursor=str(activity_state.get("trakt_history_cursor", "") or "").strip(),
            simkl_activities_ts=str(activity_state.get("simkl_activities_ts", "") or "").strip(),
            trakt_activities_ts=str(activity_state.get("trakt_activities_ts", "") or "").strip(),
            full_history_sync=bool(isinstance(sync_modes, dict) and sync_modes.get("full_history")),
            trakt_watched_history_interval_seconds=options["trakt_watched_history_interval_seconds"],
            trakt_resume_progress_interval_seconds=options["trakt_resume_progress_interval_seconds"],
            trakt_sync_full_watch_counts=False,
            trakt_reconcile_watched_history=False,
            trakt_sync_resume_progress=modes["resume"] and options["activity_resume_source"] == "trakt",
            simkl_visibility=options["simkl_visibility"],
            anilist_visibility=options["anilist_visibility"],
            trakt_personal_visibility=options["trakt_personal_visibility"],
            trakt_public_visibility=options["trakt_public_visibility"],
            mdblist_visibility=options["mdblist_visibility"],
            simkl_sync_to_pmdb_watchlist=options["simkl_sync_to_pmdb_watchlist"],
            trakt_sync_to_pmdb_watchlist=options["trakt_sync_to_pmdb_watchlist"],
            anilist_sync_to_pmdb_watchlist=options["anilist_sync_to_pmdb_watchlist"],
            pmdb_watchlist_managed_keys=list(activity_state.get("pmdb_watchlist_managed_keys") or []),
            sync_pairs=list(options.get("sync_pairs") or []),
        ),
    )


def _configured_sources(config: AppConfig) -> list[str]:
    sources = []
    if (
        config.simkl.client_id
        and config.simkl.access_token
        and (
            any(config.simkl.selected_statuses.get(media_type) for media_type in ["shows", "movies", "anime"])
            or config.sync.simkl_sync_watched_history
        )
    ):
        sources.append("simkl")
    if config.anilist.enabled and config.anilist.selected_statuses:
        sources.append("anilist")
    if (
        config.trakt.enabled
        and (
            config.trakt.sync_watchlist_movies
            or config.trakt.sync_watchlist_shows
            or config.trakt.sync_liked_lists
            or config.trakt.selected_lists
            or config.sync.trakt_sync_watched_history
            or config.sync.trakt_sync_resume_progress
        )
    ):
        sources.append("trakt")
    if config.mdblist.enabled:
        sources.append("mdblist")
    return sources


def _validate_profile_configuration(credentials: dict, options: dict) -> tuple[AppConfig | None, list[str]]:
    try:
        normalized_profile = {
            "credentials": normalize_credentials(credentials),
            "options": normalize_profile_options(options),
        }
    except ValueError as exc:
        return None, [str(exc)]

    config = _config_from_profile(normalized_profile, dry_run=False)
    sources = _configured_sources(config)
    if not sources:
        return None, ["Configure at least one source (SIMKL, AniList, Trakt, or MDBList)"]

    errors = validate_config(config, sources)
    return config, errors


def _json_error(
    message: str,
    status_code: int,
    details: list[str] | None = None,
    *,
    provider: str | None = None,
    hint: str | None = None,
):
    payload: dict = {"error": message}
    if details:
        payload["details"] = details
    if provider:
        payload["provider"] = provider
    if hint:
        payload["hint"] = hint
    payload["status_code"] = status_code
    return jsonify(payload), status_code


_PROVIDER_STATUS_HINTS = {
    401: "Check that your access token / API key is saved and hasn't expired. Reconnect the provider in Settings.",
    403: "The provider rejected this request — usually because the token is missing the required scope or was revoked.",
    404: "The provider returned 'not found'. The list or resource may have been deleted on their end.",
    429: "The provider is rate-limiting requests. Wait a minute and try again.",
}


#: Errors we raise ourselves already say what went wrong and what to do about
#: it, so appending a guessed hint only contradicts them.
_SELF_EXPLANATORY_ERRORS: tuple[type, ...] = (TraktAuthenticationError,)


def _derive_provider_hint(provider: str, exc: Exception, explicit_hint: str | None = None) -> str | None:
    if explicit_hint:
        return explicit_hint
    if isinstance(exc, _SELF_EXPLANATORY_ERRORS):
        return None
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status in _PROVIDER_STATUS_HINTS:
        return _PROVIDER_STATUS_HINTS[status]
    message = str(exc).lower()
    if "timeout" in message or "timed out" in message:
        return f"The {provider} API did not respond in time. Retry, or check their status page."
    # Match how requests/urllib3 actually word a transport failure. A bare
    # "connection" test also fired on any message that merely mentioned the
    # Connections screen, telling the user to check their network when the real
    # problem was a rejected API key.
    if provider and any(
        phrase in message
        for phrase in ("connectionerror", "connection error", "connection refused",
                       "connection aborted", "connection reset",
                       "failed to establish a new connection", "name or service not known")
    ):
        return f"Could not reach the {provider} API. Check your network connection."
    return None


def _extract_upstream_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _profile_response(profile: dict, include_credentials: bool = False):
    payload = dict(profile)
    if not include_credentials:
        payload.pop("credentials", None)
    payload["queue_status"] = _sync_runner.snapshot(payload.get("profile_id"))
    try:
        private_profile = _profile_store.get_private_profile_by_id(payload.get("profile_id"))
        config = _config_from_profile(private_profile)
        payload["connection_readiness"] = _connection_readiness(
            config, list((private_profile.get("connection_health") or {}).values())
        )
    except (KeyError, ValueError, TypeError):
        payload["connection_readiness"] = {"ready": False, "blockers": ["Check your connections."], "warnings": []}
    return jsonify({"profile": payload})


def _request_client_key() -> str:
    forwarded = str(request.headers.get("X-Forwarded-For", "")).split(",")[0].strip()
    return forwarded or request.remote_addr or "unknown"


def _session_token() -> str | None:
    return request.cookies.get(SESSION_COOKIE_NAME)


def _access_token() -> str | None:
    return request.cookies.get(ACCESS_COOKIE_NAME)


def _current_profile_id() -> str | None:
    return _session_store.get_profile_id(_session_token())


def _has_site_access() -> bool:
    if not SITE_ACCESS_PASSWORD:
        return True
    return bool(_access_store.get_profile_id(_access_token()))


def _current_public_profile(include_credentials: bool = False) -> dict | None:
    profile_id = _current_profile_id()
    if not profile_id:
        return None
    try:
        return _profile_store.get_profile_by_id(profile_id, include_credentials=include_credentials)
    except KeyError:
        _session_store.destroy(_session_token())
        return None


def _public_profile_by_request_id(profile_id: object) -> dict | None:
    """Return read-only dashboard state for a known profile UUID.

    This intentionally never includes credentials. It lets the dashboard recover
    after a lost in-memory session while a background sync is still running.
    Mutating endpoints still require a valid server-side session.
    """
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return None
    try:
        return _profile_store.get_profile_by_id(profile_id, include_credentials=False)
    except (KeyError, ValueError):
        return None


def _current_private_profile() -> dict | None:
    profile_id = _current_profile_id()
    if not profile_id:
        return None
    try:
        return _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        _session_store.destroy(_session_token())
        return None


def _cookie_secure() -> bool:
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto", "")).split(",")[0].strip().lower()
    return request.is_secure or forwarded_proto == "https"


def _with_session_cookie(response, session_token: str):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=_cookie_secure(),
    )
    return response


def _with_access_cookie(response, access_token: str):
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=_cookie_secure(),
    )
    return response


def _clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE_NAME, httponly=True, samesite="Lax", secure=_cookie_secure())
    return response


def _clear_access_cookie(response):
    response.delete_cookie(ACCESS_COOKIE_NAME, httponly=True, samesite="Lax", secure=_cookie_secure())
    return response


def _run_profile_sync(profile: dict, dry_run: bool = False, sync_modes: dict | None = None) -> None:
    profile_id = profile["profile_id"]
    log_capture.set_profile_context(profile_id)
    modes = sync_modes or profile.get("pending_sync_modes") or {"lists": True, "history": False, "resume": False}
    log_token = _log_profile_id.set(profile_id)
    try:
        try:
            latest_profile = _profile_store.get_private_profile_by_id(profile_id)
            latest_profile["pending_sync_modes"] = modes
            profile = latest_profile
        except KeyError:
            logger.warning("Queued sync skipped because profile %s no longer exists", profile_id[:8])
            return
        if _profile_store.is_sync_cancel_requested(profile_id):
            logger.info("Queued sync cancelled before start for profile %s", profile_id[:8])
            _profile_store.record_sync_cancelled(profile_id, dry_run=dry_run, sync_modes=modes)
            return
        _profile_store.update_sync_status(profile_id, "Starting sync")
        result_dicts: list[dict] = []
        detailed_result_dicts: list[dict] = []
        service = None
        run_regular = any(bool(modes.get(key)) for key in ("lists", "history", "resume"))
        config = _config_from_profile(profile, dry_run=dry_run, sync_modes=modes)
        if run_regular:
            service = SyncService(
            config,
            status_callback=lambda status: _profile_store.update_sync_status(profile_id, status),
            progress_callback=lambda results: _profile_store.update_sync_progress(profile_id, results),
            managed_lists=profile.get("managed_lists", []),
            cancel_requested_callback=lambda: _profile_store.is_sync_cancel_requested(profile_id),
            sync_modes=modes,
            # Merge manual_resolution_cache on top so user-mapped items are
            # resolved from the very first API call of this sync run, regardless
            # of whether record_sync_success has been called yet.
            resolution_cache={
                **profile.get("resolution_cache", {}),
                **profile.get("manual_resolution_cache", {}),
            },
            failed_resolution_cache=profile.get("failed_resolution_cache", {}),
            manual_list_additions=profile.get("manual_list_additions", {}),
            list_state=profile.get("list_state", {}),
            trakt_token_refreshed_callback=lambda at, rt, exp="": _profile_store.update_trakt_tokens(profile_id, at, rt, exp),
            )
            results = service.run()
            run_id = str(profile.get("sync_job_id", "") or "")
            result_dicts = [_stats_to_summary_dict(stats, run_id=run_id) for stats in results]
            detailed_result_dicts = [_stats_to_detail_dict(stats) for stats in results]

        if modes.get("pairs"):
            selected_ids = set(str(value) for value in (modes.get("pair_ids") or []))
            pairs = [
                pair for pair in _sync_pairs_from_config(config)
                if pair.enabled and (not selected_ids or pair.pair_id in selected_ids)
            ]
            pair_service = CrossSyncService(
                _build_provider_adapters(
                    config,
                    cancel_requested_callback=lambda: _profile_store.is_sync_cancel_requested(profile_id),
                ),
                dry_run=dry_run,
                managed_keys=(profile.get("activity_state") or {}).get("pair_managed_keys") or {},
                cancel_requested_callback=lambda: _profile_store.is_sync_cancel_requested(profile_id),
                status_callback=lambda status: _profile_store.update_sync_status(profile_id, status),
            )
            pair_results = pair_service.run_pairs(pairs)
            if not dry_run:
                _profile_store.update_pair_managed_keys(profile_id, pair_service.managed_keys)
                _profile_store.update_pair_last_results(
                    profile_id, [result.to_dict() for result in pair_results], dry_run=False,
                )
        _profile_store.record_sync_success(
            profile_id,
            result_dicts,
            dry_run=dry_run,
            managed_lists=service.managed_lists if service is not None else None,
            sync_modes=modes,
            resolution_cache=service.resolution_cache if service is not None else None,
            failed_resolution_cache=service.failed_resolution_cache if service is not None else None,
            detailed_results=detailed_result_dicts,
            list_state=service.list_state if service is not None else None,
        )
    except SyncCancelled:
        logger.info("Sync stopped for profile %s", profile_id[:8])
        _profile_store.record_sync_cancelled(profile_id, dry_run=dry_run, sync_modes=modes)
    except Exception as exc:  # pragma: no cover - exercised in integration use
        import traceback as _tb
        tb_str = _tb.format_exc()
        logger.exception("Sync failed for profile %s", profile_id[:8])
        error_detail = _sanitize_error_text(f"{exc}\n\nTraceback:\n{tb_str}")
        _profile_store.record_sync_error(
            profile_id,
            error_detail,
            dry_run=dry_run,
            sync_modes=modes,
        )
    finally:
        _log_profile_id.reset(log_token)


class SyncRunner:
    """Runs sync jobs through a bounded worker pool instead of unbounded threads."""

    def __init__(self, max_workers: int = MAX_CONCURRENT_SYNCS):
        self._max_workers = max(1, int(max_workers))
        self._queue: queue.Queue[tuple[dict, bool, dict | None]] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._started = False
        self._running_profile_ids: set[str] = set()
        self._skip_ids: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for idx in range(self._max_workers):
                worker = threading.Thread(
                    target=self._worker_loop,
                    name=f"sync-worker-{idx + 1}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
            self._started = True

    def enqueue(self, profile: dict, dry_run: bool = False, sync_modes: dict | None = None) -> None:
        self.start()
        self._queue.put((profile, dry_run, sync_modes))

    def queue_size(self) -> int:
        return self._queue.qsize()

    def snapshot(self, profile_id: str | None = None) -> dict:
        with self._lock:
            running_ids = set(self._running_profile_ids)
            max_workers = self._max_workers
        with self._queue.mutex:
            queued_items = list(self._queue.queue)
        queued_profile_ids = [str(item[0].get("profile_id", "")).strip() for item in queued_items]
        profile_queue_position = None
        if profile_id:
            normalized_profile_id = str(profile_id).strip()
            try:
                profile_queue_position = queued_profile_ids.index(normalized_profile_id) + 1
            except ValueError:
                profile_queue_position = None
        return {
            "max_concurrent": max_workers,
            "running": len(running_ids),
            "queued": len(queued_profile_ids),
            "profile_queue_position": profile_queue_position,
            "profile_running": bool(profile_id and str(profile_id).strip() in running_ids),
        }

    def cancel_if_queued(self, profile_id: str) -> bool:
        """Mark a queued-but-not-yet-running profile for immediate cancellation.

        Returns True if the profile is queued (worker hasn't started it yet) so
        the caller should call record_sync_cancelled immediately.  Returns False
        if a worker is actively running the sync, in which case the caller should
        use the graceful-stop path (request_sync_cancel).
        """
        with self._lock:
            if profile_id in self._running_profile_ids:
                return False
            queued_ids = {
                str(item[0].get("profile_id", "")).strip()
                for item in list(self._queue.queue)
                if item and isinstance(item[0], dict)
            }
            if profile_id not in queued_ids:
                return False
            self._skip_ids.add(profile_id)
            return True

    def _worker_loop(self) -> None:
        while True:
            profile, dry_run, sync_modes = self._queue.get()
            profile_id = str(profile.get("profile_id", "")).strip()
            if profile_id:
                with self._lock:
                    if profile_id in self._skip_ids:
                        # Cancelled while queued — already recorded by stop endpoint.
                        self._skip_ids.discard(profile_id)
                        self._queue.task_done()
                        continue
                    self._running_profile_ids.add(profile_id)
            try:
                _run_profile_sync(profile, dry_run=dry_run, sync_modes=sync_modes)
            finally:
                if profile_id:
                    with self._lock:
                        self._running_profile_ids.discard(profile_id)
                self._queue.task_done()


def _find_managed_list(profile: dict, list_name: str) -> dict | None:
    for item in profile.get("managed_lists", []):
        if str(item.get("list_name", "")).strip() == list_name:
            return item
    return None


def _make_anime_root_resolver(config: AppConfig):
    client = AniListClient(config.anilist if config.anilist.enabled else AniListConfig())

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


def _build_provider_adapters(config: AppConfig, cancel_requested_callback=None) -> dict:
    """Build the provider adapters available for cross-service sync pairs.

    Only services with usable credentials are included, so a pair referencing an
    unconfigured provider reports that rather than failing mid-run. Whether a
    provider may be *written* to is a separate question the adapter answers.
    """
    adapters: dict = {}

    if config.simkl.client_id and config.simkl.access_token:
        adapters["simkl"] = SimklAdapter(
            SimklClient(config.simkl, cancel_requested_callback=cancel_requested_callback),
            media_types=list(config.sync.media_types),
        )
    if config.trakt.enabled and config.trakt.client_id and config.trakt.access_token:
        adapters["trakt"] = TraktAdapter(
            TraktClient(config.trakt, cancel_requested_callback=cancel_requested_callback),
        )
    if config.anilist.username:
        # Readable with just a username; the adapter reports that writing needs a
        # token, so existing profiles keep working without re-authenticating.
        adapters["anilist"] = AniListAdapter(
            AniListClient(config.anilist, cancel_requested_callback=cancel_requested_callback),
        )
    if config.mdblist.api_key or config.mdblist.access_token:
        # Gate on either credential alone. mdblist.enabled additionally requires
        # selected lists, which would make MDBList vanish from the pair editor
        # with no explanation before any list had been picked — and an OAuth
        # connection is a working credential even with no API key pasted.
        adapters["mdblist"] = MdbListAdapter(
            MdbListClient(config.mdblist),
            selected_lists=list(config.mdblist.selected_lists or []),
        )
    if config.pmdb.api_key:
        adapters["pmdb"] = PmdbAdapter(
            PublicMetaDBClient(config.pmdb, cancel_requested_callback=cancel_requested_callback),
        )
    return adapters


def _sync_pairs_from_config(config: AppConfig) -> list:
    pairs = []
    for raw in config.sync.sync_pairs or []:
        try:
            pairs.append(SyncPair.from_dict(raw))
        except (ValueError, TypeError) as exc:
            logger.warning("Ignoring invalid sync pair %r: %s", raw, exc)
    return pairs


PROVIDER_LABELS = {
    "trakt": "Trakt",
    "simkl": "SIMKL",
    "anilist": "AniList",
    "mdblist": "MDBList",
    "pmdb": "PublicMetaDB",
}


def _provider_unavailable_reason(config: AppConfig, key: str) -> str:
    """Why a provider offers nothing, in the user's terms.

    Returned for providers with no adapter so the pair editor can show them
    greyed out with a reason instead of omitting them, which leaves no way to
    tell a missing setting from a bug.
    """
    if key == "trakt":
        if not config.trakt.client_id or not config.trakt.access_token:
            return "Connect Trakt in Connections first."
        if not config.trakt.enabled:
            return "Trakt is connected but disabled."
    elif key == "simkl":
        if not config.simkl.client_id or not config.simkl.access_token:
            return "Connect SIMKL in Connections first."
    elif key == "anilist":
        return "Add your AniList username in Connections first."
    elif key == "mdblist":
        return "Add your MDBList API key, or connect MDBList, in Connections first."
    elif key == "pmdb":
        return "Add your PublicMetaDB API key in Connections first."
    return "Not configured."


def _source_blocked_reason(config: AppConfig, key: str) -> str:
    """Why a configured provider still cannot act as a source.

    MDBList no longer needs a selected list: its account watchlist, collection
    and watch history are readable on their own, and only the static-list
    sources depend on a selection.
    """
    return ""


def _pair_capabilities(config: AppConfig) -> dict:
    """Provider capability report for the pair editor."""
    adapters = _build_provider_adapters(config)
    providers = []
    for key in PROVIDER_ORDER:
        adapter = adapters.get(key)
        if adapter is None:
            providers.append({
                "key": key,
                "label": PROVIDER_LABELS.get(key, key),
                "configured": False,
                "reads": [],
                "writes": [],
                "write_blocked_reason": "",
                # Same shape as a configured provider so callers never have to
                # special-case the unconfigured branch.
                "has_lists": False,
                "has_target_lists": False,
                "target_list_categories": [],
                "has_list_search": False,
                "has_visibility": False,
                "unavailable_reason": _provider_unavailable_reason(config, key),
            })
            continue
        described = adapter.describe()
        described["configured"] = True
        described["unavailable_reason"] = _source_blocked_reason(config, key)
        providers.append(described)
    return {
        "providers": providers,
        "categories": [
            {"key": key, "label": CATEGORY_LABELS[key]} for key in ALL_CATEGORIES
        ],
        "removal_modes": [
            {"key": key, "label": REMOVAL_MODE_LABELS[key]} for key in ALL_REMOVAL_MODES
        ],
        "pair_modes": [
            {"key": key, "label": PAIR_MODE_LABELS[key]} for key in ALL_PAIR_MODES
        ],
        # Which removal modes two-way accepts, so the editor can narrow the list
        # rather than offering one the backend will silently downgrade.
        "two_way_removal_modes": list(TWO_WAY_REMOVAL_MODES),
        "pair_interval_min_seconds": 43200,
        "pair_interval_default_seconds": 43200,
        "destination_rules": [
            {
                "source": "simkl", "target": "trakt", "category": "watchlist",
                "destination": "native_watchlist",
                "description": "SIMKL Plan to Watch is synced to the Trakt watchlist.",
            },
            {
                "source": "simkl", "target": "trakt", "category": "collection",
                "destination": "custom_lists_by_status",
                "description": "Watching, Completed, On Hold and Dropped use separate SyncMeta-managed Trakt lists.",
            },
        ],
    }


def _connection_readiness(config: AppConfig, checks: list[dict]) -> dict:
    by_provider = {row.get("provider"): row for row in checks if isinstance(row, dict)}
    blockers: list[str] = []
    warnings: list[str] = []
    pmdb = by_provider.get("pmdb") or {}
    if pmdb.get("status") != "healthy":
        blockers.append("Connect and verify PublicMetaDB.")

    source_keys = ("simkl", "trakt", "anilist", "mdblist")
    readable_sources = [
        key for key in source_keys
        if (by_provider.get(key) or {}).get("status") == "healthy"
        and ((by_provider.get(key) or {}).get("capabilities") or {}).get("readable")
    ]
    if not readable_sources:
        blockers.append("Connect and verify at least one sync source.")

    adapters = _build_provider_adapters(config)
    pair_service = CrossSyncService(adapters)
    for pair in _sync_pairs_from_config(config):
        if not pair.enabled:
            continue
        problem = pair_service.validate_pair(pair)
        if problem:
            blockers.append(f"{pair.name or pair.pair_id}: {problem}")

    for key, row in by_provider.items():
        if row.get("status") == "degraded":
            warnings.append(f"{PROVIDER_LABELS.get(key, key)}: {row.get('message', 'Connection is degraded.')}")
    return {"ready": not blockers, "blockers": blockers, "warnings": warnings}


def _resolve_unresolved_item_automatically(private_profile: dict, item: dict) -> int | None:
    # candidate_tmdb_id is deliberately NOT returned up front. It is the ID the
    # matcher just declined — either an unconfirmed zero-vote community mapping or
    # one on the known-bad blocklist — and it is recorded only as a hint for the
    # user. Handing it back as an automatic answer would re-apply the exact
    # mapping the safety guards rejected. Instead, run a real resolve below; the
    # matcher will return the candidate only if it can now verify it.
    config = _config_from_profile(private_profile, dry_run=False, sync_modes={"lists": True, "history": False, "resume": False})
    pmdb = PublicMetaDBClient(config.pmdb)
    matcher = ItemMatcher(
        pmdb,
        anime_root_resolver=_make_anime_root_resolver(config),
        initial_cache={
            **(private_profile.get("resolution_cache") or {}),
            **(private_profile.get("manual_resolution_cache") or {}),
        },
        # Force a fresh retry against PMDB/community mappings instead of honoring
        # the persisted failed cache TTL from the last sync run.
        initial_failed_cache={},
    )
    return matcher.resolve_tmdb_id({
        "title": item.get("title"),
        # Carried through so the widened title comparison still applies here.
        "title_variants": item.get("title_variants") or [],
        "year": item.get("year"),
        "media_type": item.get("media_type"),
        "simkl_type": item.get("simkl_type"),
        "tmdb_id": item.get("tmdb_id"),
        "imdb_id": item.get("imdb_id"),
        "mal_id": item.get("mal_id"),
        "anilist_id": item.get("anilist_id"),
        "root_mal_id": item.get("root_mal_id"),
        "root_anilist_id": item.get("root_anilist_id"),
        "anidb_id": item.get("anidb_id"),
        "tvdb_id": item.get("tvdb_id"),
        # Part of ItemMatcher._cache_key. Omitting it produced a different key
        # than the one stored on the unresolved item, so the result was cached
        # where the next sync would never look for it.
        "anime_resolve_mode": item.get("anime_resolve_mode") or "",
    })


def _parse_tmdb_id(raw_value: object) -> int:
    candidate = str(raw_value or "").strip()
    if not candidate:
        raise ValueError("tmdb_id must be a positive integer")
    try:
        tmdb_id = int(candidate)
    except (TypeError, ValueError):
        match = re.search(r"(?<!\d)(\d+)", candidate)
        if not match:
            raise ValueError("tmdb_id must be a positive integer")
        tmdb_id = int(match.group(1))
    if tmdb_id <= 0:
        raise ValueError("tmdb_id must be a positive integer")
    return tmdb_id


def _contribute_manual_resolution_mapping(pmdb: PublicMetaDBClient, item: dict | None, tmdb_id: int) -> None:
    if not item:
        return
    media_type = str(item.get("media_type") or "").strip()
    if not media_type:
        return
    ids = item if isinstance(item, dict) else {}
    seen: set[tuple[str, str]] = set()
    for id_type, item_key in [
        ("mal", "mal_id"),
        ("anilist", "anilist_id"),
        ("mal", "root_mal_id"),
        ("anilist", "root_anilist_id"),
        ("imdb", "imdb_id"),
        ("tvdb", "tvdb_id"),
        ("anidb", "anidb_id"),
        ("trakt", "trakt_id"),
    ]:
        id_value = ids.get(item_key)
        if not id_value:
            continue
        key = (id_type, str(id_value))
        if key in seen:
            continue
        seen.add(key)
        try:
            pmdb.create_id_mapping(tmdb_id, media_type, id_type, str(id_value))
        except Exception as exc:
            logger.debug("Failed to contribute manual PMDB mapping %s=%s: %s", id_type, id_value, exc)


def _apply_unresolved_resolution(
    profile_id: str,
    private_profile: dict,
    cache_key: str,
    target_item: dict | None,
    tmdb_id: int,
) -> dict:
    # Detailed logging for debugging anime mapping issues.
    logger.info(
        "[manual-map] profile=%s | title=%r year=%s type=%s simkl_type=%s"
        " | simkl_id=%s mal_id=%s anilist_id=%s"
        " | suggested_tmdb=%s → manual_tmdb=%s"
        " | unresolved_reason=%s | cache_key=%s",
        profile_id[:8],
        (target_item or {}).get("title"),
        (target_item or {}).get("year"),
        (target_item or {}).get("media_type"),
        (target_item or {}).get("simkl_type"),
        (target_item or {}).get("simkl_id"),
        (target_item or {}).get("mal_id"),
        (target_item or {}).get("anilist_id"),
        (target_item or {}).get("candidate_tmdb_id"),
        tmdb_id,
        (target_item or {}).get("unresolved_reason"),
        cache_key,
    )

    remaining = _profile_store.resolve_item_manually(profile_id, cache_key, tmdb_id)

    pmdb_result = None
    pmdb_skip_reason: str | None = None
    if target_item:
        try:
            credentials = normalize_credentials(private_profile.get("credentials", {}))
            pmdb_api_key = credentials.get("pmdb", {}).get("api_key", "")
            if not pmdb_api_key:
                pmdb_skip_reason = "no_pmdb_api_key"
                logger.warning(
                    "[manual-map] ✗ skipping immediate PMDB add for tmdb_id=%s"
                    " — no PMDB API key configured (profile=%s); will add on next sync",
                    tmdb_id, profile_id[:8],
                )
            else:
                pmdb = PublicMetaDBClient(PublicMetaDBConfig(api_key=pmdb_api_key))
                _contribute_manual_resolution_mapping(pmdb, target_item, tmdb_id)
                media_type = str(target_item.get("media_type") or "").strip()
                list_name = str(target_item.get("list_name") or "").strip()
                managed_lists = private_profile.get("managed_lists") or []
                pmdb_list_id = None
                for ml in managed_lists:
                    if str(ml.get("list_name", "")).strip() == list_name:
                        pmdb_list_id = ml.get("list_id")
                        break
                if not media_type:
                    pmdb_skip_reason = "missing_media_type"
                    logger.warning(
                        "[manual-map] ✗ skipping immediate PMDB add for tmdb_id=%s"
                        " — target item has no media_type (list=%r profile=%s); will add on next sync",
                        tmdb_id, list_name, profile_id[:8],
                    )
                elif not pmdb_list_id:
                    pmdb_skip_reason = "list_not_found"
                    logger.warning(
                        "[manual-map] ✗ skipping immediate PMDB add for tmdb_id=%s (%s)"
                        " — list %r not found in managed_lists (profile=%s, %d managed lists);"
                        " will add on next sync",
                        tmdb_id, media_type, list_name, profile_id[:8], len(managed_lists),
                    )
                else:
                    pmdb_result = pmdb.add_item_to_list(str(pmdb_list_id), tmdb_id, media_type)
                    logger.info(
                        "[manual-map] ✓ added tmdb_id=%s (%s) to list '%s'"
                        " (pmdb_list=%s profile=%s) → pmdb_result=%s",
                        tmdb_id, media_type, list_name, pmdb_list_id, profile_id[:8], pmdb_result,
                    )
        except Exception as exc:
            pmdb_skip_reason = "api_error"
            logger.warning(
                "[manual-map] ✗ failed to immediately add tmdb_id=%s to PMDB list (profile=%s): %s"
                " — override is saved, will retry on next sync",
                tmdb_id, profile_id[:8], exc,
            )
    else:
        pmdb_skip_reason = "item_not_in_unresolved"
        logger.warning(
            "[manual-map] target item not found in unresolved list for cache_key=%s (profile=%s);"
            " override stored in manual_resolution_cache — will apply on next sync",
            cache_key, profile_id[:8],
        )

    try:
        updated_profile = _profile_store.get_profile_by_id(profile_id, include_credentials=False)
    except Exception:
        updated_profile = None

    return {
        "status": "resolved",
        "tmdb_id": tmdb_id,
        "pmdb_added": pmdb_result is not None,
        "pmdb_skip_reason": pmdb_skip_reason,
        "items": remaining,
        "profile": updated_profile,
    }


def _remove_trakt_selected_list(selected_lists: list[dict], selection: dict) -> list[dict]:
    user = str(selection.get("user", "")).strip().lower()
    slug = str(selection.get("slug", "")).strip().lower()
    source = str(selection.get("list_source", "")).strip().lower()
    name = str(selection.get("name", "")).strip()

    remaining = []
    for item in selected_lists:
        item_user = str(item.get("user", "")).strip().lower()
        item_slug = str(item.get("slug", "")).strip().lower()
        item_source = str(item.get("source", "")).strip().lower()
        if user and slug and item_user == user and item_slug == slug:
            continue
        if source and name and item_source == source and str(item.get("name", "")).strip() == name:
            continue
        remaining.append(item)
    return remaining


def _display_status_label(display_name: str) -> str:
    return str(display_name or "").split(" - ", 1)[0].strip().lower()


def _remove_managed_selection(profile: dict, managed_entry: dict) -> dict:
    credentials = normalize_credentials(profile.get("credentials"))
    selection = managed_entry.get("selection") if isinstance(managed_entry.get("selection"), dict) else {}
    source = str(selection.get("source", "")).strip().lower()

    if source == "simkl":
        media_type = str(selection.get("media_type", "")).strip().lower()
        status = str(selection.get("status", "")).strip()
        if media_type in credentials["simkl"]["selected_statuses"]:
            credentials["simkl"]["selected_statuses"][media_type] = [
                item for item in credentials["simkl"]["selected_statuses"][media_type]
                if item != status
            ]
        return credentials

    if source == "anilist":
        status = str(selection.get("status", "")).strip()
        credentials["anilist"]["selected_statuses"] = [
            item for item in credentials["anilist"]["selected_statuses"]
            if item != status
        ]
        return credentials

    if source == "trakt":
        kind = str(selection.get("kind", "")).strip().lower()
        if kind == "watchlist":
            media_type = str(selection.get("media_type", "")).strip().lower()
            if media_type == "movies":
                credentials["trakt"]["sync_watchlist_movies"] = False
            elif media_type == "shows":
                credentials["trakt"]["sync_watchlist_shows"] = False
            else:
                credentials["trakt"]["sync_watchlist_movies"] = False
                credentials["trakt"]["sync_watchlist_shows"] = False
            credentials["trakt"]["sync_watchlist"] = (
                credentials["trakt"]["sync_watchlist_movies"] or credentials["trakt"]["sync_watchlist_shows"]
            )
            return credentials
        if kind == "default":
            catalog_key = str(selection.get("catalog_key", "")).strip()
            name = str(selection.get("name", "")).strip()
            credentials["trakt"]["selected_lists"] = [
                item for item in credentials["trakt"]["selected_lists"]
                if not (
                    str(item.get("source", "")).strip().lower() == "default"
                    and (
                        (catalog_key and str(item.get("catalog_key", "")).strip() == catalog_key)
                        or (name and str(item.get("name", "")).strip() == name)
                    )
                )
            ]
            return credentials
        if kind == "selected-list":
            credentials["trakt"]["selected_lists"] = _remove_trakt_selected_list(
                credentials["trakt"]["selected_lists"],
                selection,
            )
            return credentials
        if kind == "liked-auto":
            trakt_config = TraktConfig(
                client_id=credentials["trakt"]["client_id"],
                client_secret=credentials["trakt"]["client_secret"],
                access_token=credentials["trakt"]["access_token"],
                refresh_token=credentials["trakt"]["refresh_token"],
                access_token_expires_at=credentials["trakt"]["access_token_expires_at"],
                username=credentials["trakt"]["username"],
            )
            liked_lists = TraktClient(
                trakt_config,
                token_refreshed_callback=lambda at, rt, exp="": _profile_store.update_trakt_tokens(
                    str(profile.get("profile_id", "") or ""), at, rt, exp
                ),
            ).get_liked_lists_metadata()
            remaining_liked = [
                item for item in liked_lists
                if not (
                    str(item.get("user", "")).strip().lower() == str(selection.get("user", "")).strip().lower()
                    and str(item.get("slug", "")).strip().lower() == str(selection.get("slug", "")).strip().lower()
                )
            ]
            existing_non_liked = [
                item for item in credentials["trakt"]["selected_lists"]
                if str(item.get("source", "")).strip().lower() != "liked"
            ]
            credentials["trakt"]["sync_liked_lists"] = False
            credentials["trakt"]["selected_lists"] = existing_non_liked + remaining_liked
            return credentials

    if source == "mdblist":
        list_id = str(selection.get("id", "")).strip()
        mediatype = str(selection.get("mediatype", "")).strip().lower()
        credentials["mdblist"]["selected_lists"] = [
            item for item in credentials["mdblist"]["selected_lists"]
            if not (
                str(item.get("id", "")).strip() == list_id
                and str(item.get("mediatype", "")).strip().lower() == mediatype
            )
        ]
        return credentials

    # Fallbacks for older managed-list records without selection metadata.
    source_name = str(managed_entry.get("source_name", "")).strip()
    display_name = str(managed_entry.get("display_name", "")).strip()
    if source_name == "SIMKL":
        status = SIMKL_STATUS_BY_LABEL.get(_display_status_label(display_name), "")
        if status:
            for media_type, statuses in credentials["simkl"]["selected_statuses"].items():
                credentials["simkl"]["selected_statuses"][media_type] = [
                    item for item in statuses
                    if item != status
                ]
    elif source_name == "AniList":
        status = ANILIST_STATUS_BY_LABEL.get(_display_status_label(display_name), "")
        credentials["anilist"]["selected_statuses"] = [
            item for item in credentials["anilist"]["selected_statuses"]
            if item != status
        ]
    elif source_name == "MDBList":
        credentials["mdblist"]["selected_lists"] = [
            item for item in credentials["mdblist"]["selected_lists"]
            if str(item.get("name", "")).strip() != display_name
        ]
    elif source_name.startswith("Trakt"):
        if display_name == _status_list_name("movies", "watchlist"):
            credentials["trakt"]["sync_watchlist_movies"] = False
            credentials["trakt"]["sync_watchlist"] = credentials["trakt"]["sync_watchlist_shows"]
        elif display_name == _status_list_name("shows", "watchlist"):
            credentials["trakt"]["sync_watchlist_shows"] = False
            credentials["trakt"]["sync_watchlist"] = credentials["trakt"]["sync_watchlist_movies"]
        else:
            credentials["trakt"]["selected_lists"] = [
                item for item in credentials["trakt"]["selected_lists"]
                if str(item.get("name", "")).strip() != display_name
            ]
    return credentials


def _remove_matching_list_name(credentials: dict, list_name: str) -> dict:
    target = str(list_name).strip()
    if not target:
        return credentials

    for media_type, statuses in credentials["simkl"]["selected_statuses"].items():
        credentials["simkl"]["selected_statuses"][media_type] = [
            status for status in statuses
            if _status_list_name(media_type, status) != target
        ]

    credentials["anilist"]["selected_statuses"] = [
        status for status in credentials["anilist"]["selected_statuses"]
        if _status_list_name("anime", status) != target
    ]

    if target in {
        _status_list_name("shows", "watchlist"),
        _status_list_name("movies", "watchlist"),
    }:
        if target == _status_list_name("movies", "watchlist"):
            credentials["trakt"]["sync_watchlist_movies"] = False
        if target == _status_list_name("shows", "watchlist"):
            credentials["trakt"]["sync_watchlist_shows"] = False
        credentials["trakt"]["sync_watchlist"] = (
            credentials["trakt"]["sync_watchlist_movies"] or credentials["trakt"]["sync_watchlist_shows"]
        )

    credentials["trakt"]["selected_lists"] = [
        item for item in credentials["trakt"]["selected_lists"]
        if str(item.get("name", "")).strip() != target
    ]

    credentials["mdblist"]["selected_lists"] = [
        item for item in credentials["mdblist"]["selected_lists"]
        if str(item.get("name", "")).strip() != target
    ]

    return credentials


class ProfileScheduler:
    """Polls stored profiles and runs due syncs in background threads."""

    def __init__(self, store: ProfileStore, poll_seconds: int = SCHEDULER_POLL_SECONDS):
        self._store = store
        self._poll_seconds = poll_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="profile-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the poll loop to end. It may still be inside a claim; that run
        finishes rather than being torn down mid-sync."""
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        # The scheduler is started lazily by the first HTTP request, and every
        # profile whose schedule lapsed while the server was down is due the
        # instant it polls. So the very request that brings the app up used to
        # trigger a stampede of syncs and then have to compete with it — after a
        # restart the site appeared to hang for exactly the person who woke it.
        # Serve for a moment before claiming anything.
        if SCHEDULER_STARTUP_GRACE_SECONDS > 0:
            self._stop.wait(SCHEDULER_STARTUP_GRACE_SECONDS)
        first_poll = True
        while not self._stop.is_set():
            # Claimed in batches: everything past the pool's capacity would only
            # sit in the queue anyway, while being flagged as running to the
            # dashboard. The rest is picked up by the next poll.
            due = self._store.claim_due_profiles(limit=SCHEDULER_CLAIM_BATCH)
            for i, profile in enumerate(due):
                if first_poll and i > 0:
                    # Stagger startup syncs by 30s each to avoid hammering APIs
                    self._stop.wait(30)
                    if self._stop.is_set():
                        return
                _sync_runner.enqueue(profile, False, profile.get("pending_sync_modes"))
            first_poll = False
            self._stop.wait(self._poll_seconds)


_scheduler = ProfileScheduler(_profile_store)
_sync_runner = SyncRunner(MAX_CONCURRENT_SYNCS)


def _scheduler_disabled() -> bool:
    return str(os.getenv("DISABLE_PROFILE_SCHEDULER", "")).strip() in {"1", "true", "yes", "on"}


def _ensure_scheduler_started() -> None:
    global _scheduler_started
    if _scheduler_disabled():
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _sync_runner.start()
        _scheduler.start()
        _scheduler_started = True


def _apply_scheduler_enabled() -> None:
    """Start or stop the scheduler to match DISABLE_PROFILE_SCHEDULER now.

    Without this, toggling the setting from the admin panel would only take
    effect on the next restart, which is the opposite of what an emergency
    "stop all automatic syncing" switch is for.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_disabled():
            _scheduler.stop()
            _scheduler_started = False
        else:
            _sync_runner.start()
            _scheduler.start()
            _scheduler_started = True


@app.before_request
def _before_request() -> None:
    _ensure_scheduler_started()
    profile_id = _current_profile_id()
    if profile_id:
        request.environ["syncmeta_log_token"] = _log_profile_id.set(profile_id)
    if not SITE_ACCESS_PASSWORD:
        return
    # Profile login and creation must always be reachable so users can
    # authenticate before they have a site-access cookie.
    allowed_paths = {"/access", "/api/profile/login", "/api/profile/save", "/api/profile/password/reset"}
    if request.path in allowed_paths or request.path.startswith("/static/"):
        return
    if _has_site_access():
        return
    if request.path.startswith("/api/"):
        # Return 401 without clearing the cookie — the cookie may still be valid
        # for other requests and clearing it would cascade-lock the user out.
        return make_response(jsonify({"error": "Site password required"}), 401)
    return _clear_access_cookie(make_response(render_template("access.html", error=None), 401))


@app.after_request
def _reset_log_profile(response):
    token = request.environ.pop("syncmeta_log_token", None)
    if token is not None:
        _log_profile_id.reset(token)
    return response


@app.after_request
def _compress_response(response):
    if (
        "gzip" not in request.headers.get("Accept-Encoding", "")
        or not response.content_type.startswith("application/json")
        or response.status_code < 200
        or response.status_code >= 300
        or response.direct_passthrough
    ):
        return response
    data = response.get_data()
    if len(data) < 512:
        return response
    compressed = _gzip.compress(data, compresslevel=6)
    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    response.headers["Content-Length"] = len(compressed)
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True, "service": "syncmeta"})


@app.route("/api/site/stats", methods=["GET"])
def api_site_stats():
    return jsonify(_profile_store.get_site_stats())


@app.route("/access", methods=["GET", "POST"])
def access():
    if not SITE_ACCESS_PASSWORD:
        return redirect(url_for("index"))
    if _has_site_access():
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        body = request.form if request.form else (request.get_json(silent=True) or {})
        password = str(body.get("password", "")).strip()
        client_key = _request_client_key()

        if _access_limiter.is_limited(client_key):
            error = "Too many access attempts. Please wait and try again."
        elif password != SITE_ACCESS_PASSWORD:
            _access_limiter.record_failure(client_key)
            error = "Wrong site password."
        else:
            _access_limiter.clear(client_key)
            access_token = _access_store.create("site-access")
            response = redirect(url_for("index"))
            return _with_access_cookie(response, access_token)

    return render_template("access.html", error=error)


@app.route("/api/profile/login", methods=["POST"])
def api_profile_login():
    body = request.get_json(silent=True) or {}
    profile_id = body.get("profile_id", "")
    password = body.get("password", "")
    client_key = _request_client_key()

    if _login_limiter.is_limited(client_key):
        return _json_error(
            "Too many login attempts. Please wait and try again.",
            429,
            hint=f"Login is locked for up to {LOGIN_WINDOW_SECONDS // 60} minutes. Wait before retrying.",
        )

    try:
        profile = _profile_store.get_profile(profile_id, password, include_credentials=True)
    except KeyError:
        _login_limiter.record_failure(client_key)
        return _json_error("Profile not found", 404)
    except PermissionError:
        _login_limiter.record_failure(client_key)
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        _login_limiter.record_failure(client_key)
        return _json_error(str(exc), 400)

    _login_limiter.clear(client_key)
    session_token = _session_store.create(profile["profile_id"])
    logger.info("Profile signed in", extra={"profile_id": profile["profile_id"]})
    return _with_session_cookie(_profile_response(profile, include_credentials=True), session_token)


@app.route("/api/profile/logout", methods=["POST"])
def api_profile_logout():
    logger.info("Profile signed out")
    _session_store.destroy(_session_token())
    return _clear_session_cookie(make_response(jsonify({"status": "logged_out"})))


@app.route("/api/profile/password/reset", methods=["POST"])
def api_profile_password_reset():
    body = request.get_json(silent=True) or {}
    new_password = str(body.get("new_password", ""))
    current_password = str(body.get("current_password", "") or body.get("password", ""))
    profile_id = _current_profile_id() or str(body.get("profile_id", "")).strip()
    client_key = _request_client_key()

    if not profile_id:
        return _json_error("Profile UUID is required", 400)
    if not current_password:
        return _json_error("Current profile password is required", 400)

    # Same limiter as sign-in: this endpoint verifies a password, so without it
    # it would be an unthrottled oracle for guessing one.
    if _login_limiter.is_limited(client_key):
        return _json_error(
            "Too many password attempts. Please wait and try again.",
            429,
            hint=f"Locked for up to {LOGIN_WINDOW_SECONDS // 60} minutes. Wait before retrying.",
        )

    try:
        profile = _profile_store.change_profile_password(profile_id, current_password, new_password)
    except KeyError:
        _login_limiter.record_failure(client_key)
        return _json_error("Profile not found", 404)
    except PermissionError:
        _login_limiter.record_failure(client_key)
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    _login_limiter.clear(client_key)
    # Any other browser holding a session for this profile is invalidated: a
    # password change is how an owner locks out a device they no longer trust.
    _session_store.destroy_profile_sessions(profile["profile_id"])
    session_token = _session_store.create(profile["profile_id"])
    return _with_session_cookie(_profile_response(profile, include_credentials=True), session_token)


@app.route("/api/profile/delete", methods=["POST"])
def api_profile_delete():
    body = request.get_json(silent=True) or {}
    confirm_text = str(body.get("confirm_text", "")).strip().upper()
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    if confirm_text != "DELETE":
        return _json_error("Type DELETE to confirm profile deletion", 400)

    try:
        _profile_store.delete_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except RuntimeError as exc:
        return _json_error(str(exc), 409)

    _session_store.destroy(_session_token())
    return _clear_session_cookie(make_response(jsonify({"status": "deleted"})))


@app.route("/api/simkl/pin/start", methods=["POST"])
def api_simkl_pin_start():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    if not client_id and private_profile:
        client_id = private_profile["credentials"]["simkl"]["client_id"]

    if not client_id:
        return _json_error("SIMKL client ID is required", 400)

    try:
        client = SimklClient(SimklConfig(client_id=client_id))
        pin_data = client.request_pin()
    except Exception as exc:
        logger.exception("Failed to start SIMKL PIN auth")
        return _json_error(
            f"Failed to start SIMKL auth: {exc}",
            400,
            provider="SIMKL",
            hint=_derive_provider_hint("SIMKL", exc, "Double-check your SIMKL client ID in Settings."),
        )

    response = {
        "user_code": pin_data.get("user_code"),
        "verification_url": pin_data.get("verification_url"),
        "interval": pin_data.get("interval", 5),
        "expires_in": pin_data.get("expires_in", 900),
    }
    return jsonify(response)


@app.route("/api/simkl/pin/check", methods=["POST"])
def api_simkl_pin_check():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    user_code = str(body.get("user_code", "")).strip()
    if not client_id and private_profile:
        client_id = private_profile["credentials"]["simkl"]["client_id"]

    if not client_id:
        return _json_error("SIMKL client ID is required", 400)
    if not user_code:
        return _json_error("SIMKL user code is required", 400)

    try:
        client = SimklClient(SimklConfig(client_id=client_id))
        check = client.check_pin(user_code) or {}
    except Exception as exc:
        logger.exception("Failed to check SIMKL PIN auth")
        return _json_error(
            f"Failed to check SIMKL auth: {exc}",
            400,
            provider="SIMKL",
            hint=_derive_provider_hint("SIMKL", exc),
        )

    if check.get("result") == "OK" and check.get("access_token"):
        return jsonify({
            "status": "approved",
            "access_token": check["access_token"],
        })

    return jsonify({
        "status": "pending",
        "message": check.get("message", ""),
    })


@app.route("/api/anilist/auth/start", methods=["POST"])
def api_anilist_auth_start():
    """Return the AniList authorize URL for the pin flow."""
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    if not client_id and private_profile:
        client_id = private_profile["credentials"]["anilist"]["client_id"]
    if not client_id:
        return _json_error("AniList client ID is required", 400)

    return jsonify({
        "authorize_url": anilist_build_authorize_url(client_id),
        "redirect_uri": ANILIST_PIN_REDIRECT_URI,
    })


@app.route("/api/anilist/auth/check", methods=["POST"])
def api_anilist_auth_check():
    """Exchange the pasted AniList code for an access token."""
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    code = str(body.get("code", "")).strip()

    # Fall back to the stored credentials so a saved secret need not be retyped.
    if private_profile:
        stored = private_profile["credentials"]["anilist"]
        client_id = client_id or stored["client_id"]
        client_secret = client_secret or stored["client_secret"]

    if not client_id:
        return _json_error("AniList client ID is required", 400)
    if not client_secret:
        return _json_error("AniList client secret is required", 400)
    if not code:
        return _json_error("Paste the code AniList showed you", 400)

    try:
        access_token = anilist_exchange_code_for_token(client_id, client_secret, code)
    except ValueError as exc:
        return _json_error(
            str(exc), 400,
            provider="AniList",
            hint="The code is single-use and short-lived — start the connect flow again if in doubt.",
        )
    except Exception as exc:
        logger.exception("AniList token exchange failed")
        return _json_error(f"AniList authentication failed: {exc}", 400, provider="AniList")

    saved = False
    profile_id = _current_profile_id()
    if profile_id:
        # Persist immediately so the token survives even if the user navigates
        # away without pressing Save.
        try:
            _profile_store.update_anilist_auth(profile_id, client_id, client_secret, access_token)
            saved = True
        except KeyError:
            saved = False

    return jsonify({"status": "approved", "access_token": access_token, "saved": saved})


@app.route("/api/mdblist/auth/start", methods=["POST"])
def api_mdblist_auth_start():
    """Begin the MDBList PKCE flow and return the authorize URL."""
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    redirect_uri = str(body.get("redirect_uri", "")).strip()
    if private_profile:
        stored = private_profile["credentials"]["mdblist"]
        client_id = client_id or stored.get("client_id", "")
        client_secret = client_secret or stored.get("client_secret", "")

    if not client_id:
        return _json_error("MDBList client ID is required", 400)
    if not redirect_uri:
        return _json_error("A redirect URL is required", 400)

    profile_id = _current_profile_id()
    if not profile_id:
        return _json_error("Sign in before connecting MDBList", 401)

    verifier, challenge = MdbListClient.generate_pkce_pair()
    # The verifier never goes to the browser: it is the proof that the code
    # being exchanged belongs to the request that started this flow.
    _mdblist_pkce_store.put(profile_id, verifier, redirect_uri)

    client = MdbListClient(MdbListConfig(client_id=client_id, client_secret=client_secret))
    return jsonify({
        "authorize_url": client.build_authorize_url(redirect_uri, challenge),
        "redirect_uri": redirect_uri,
    })


@app.route("/api/mdblist/auth/check", methods=["POST"])
def api_mdblist_auth_check():
    """Exchange the pasted MDBList code for an access token."""
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    code = str(body.get("code", "")).strip()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    if private_profile:
        stored = private_profile["credentials"]["mdblist"]
        client_id = client_id or stored.get("client_id", "")
        client_secret = client_secret or stored.get("client_secret", "")

    if not code:
        return _json_error("Paste the code MDBList showed you", 400)
    if not client_id:
        return _json_error("MDBList client ID is required", 400)
    if not client_secret:
        return _json_error("MDBList client secret is required", 400)

    profile_id = _current_profile_id()
    if not profile_id:
        return _json_error("Sign in before connecting MDBList", 401)

    pending = _mdblist_pkce_store.take(profile_id)
    if not pending:
        return _json_error(
            "That authorization has expired", 400,
            provider="MDBList",
            hint="Press Connect again to start a fresh authorization.",
        )
    verifier, redirect_uri = pending

    try:
        client = MdbListClient(MdbListConfig(client_id=client_id, client_secret=client_secret))
        payload = client.exchange_code_for_token(code, verifier, redirect_uri)
    except Exception as exc:
        logger.exception("MDBList token exchange failed")
        return _json_error(
            f"MDBList authentication failed: {exc}", 400,
            provider="MDBList",
            hint="The code is single-use and short-lived — start the connect flow again if in doubt.",
        )

    access_token = str(payload.get("access_token", "")).strip()
    if not access_token:
        return _json_error("MDBList did not return an access token", 400, provider="MDBList")

    expires_at = ""
    try:
        expires_in = int(payload.get("expires_in") or 0)
        if expires_in > 0:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    except (TypeError, ValueError):
        expires_at = ""

    # Persisted immediately: the code is single-use, so losing it to an
    # unsaved form would mean starting the whole flow over.
    saved = False
    try:
        _profile_store.update_mdblist_auth(
            profile_id,
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
            refresh_token=str(payload.get("refresh_token", "")).strip(),
            access_token_expires_at=expires_at,
        )
        saved = True
    except KeyError:
        saved = False

    return jsonify({"status": "approved", "saved": saved, "scope": payload.get("scope", "")})


@app.route("/api/trakt/device/start", methods=["POST"])
def api_trakt_device_start():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    if private_profile:
        client_id = client_id or private_profile["credentials"]["trakt"]["client_id"]
        client_secret = client_secret or private_profile["credentials"]["trakt"]["client_secret"]

    if not client_id:
        return _json_error("Trakt client ID is required", 400)
    if not client_secret:
        return _json_error("Trakt client secret is required", 400)

    try:
        client = TraktClient(TraktConfig(client_id=client_id, client_secret=client_secret))
        data = client.request_device_code()
    except Exception as exc:
        logger.exception("Failed to start Trakt device auth")
        return _json_error(
            f"Failed to start Trakt auth: {exc}",
            400,
            provider="Trakt",
            hint=_derive_provider_hint("Trakt", exc, "Verify the Trakt client ID and secret in Settings."),
        )

    return jsonify({
        "device_code": data.get("device_code"),
        "user_code": data.get("user_code"),
        "verification_url": data.get("verification_url") or "https://trakt.tv/activate",
        "interval": data.get("interval", 5),
        "expires_in": data.get("expires_in", 600),
    })


@app.route("/api/trakt/device/check", methods=["POST"])
def api_trakt_device_check():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    device_code = str(body.get("device_code", "")).strip()
    if private_profile:
        client_id = client_id or private_profile["credentials"]["trakt"]["client_id"]
        client_secret = client_secret or private_profile["credentials"]["trakt"]["client_secret"]

    if not client_id:
        return _json_error("Trakt client ID is required", 400)
    if not client_secret:
        return _json_error("Trakt client secret is required", 400)
    if not device_code:
        return _json_error("Trakt device code is required", 400)

    try:
        client = TraktClient(TraktConfig(client_id=client_id, client_secret=client_secret))
        data = client.poll_device_token(device_code) or {}
    except Exception as exc:
        response = getattr(exc, "response", None)
        payload = {}
        if response is not None and getattr(response, "text", ""):
            try:
                payload = response.json() or {}
            except ValueError:
                payload = {}

        error_code = str(payload.get("error", "")).strip().lower()
        if error_code in {"authorization_pending", "slow_down"}:
            return jsonify({
                "status": "pending",
                "message": payload.get("error_description") or payload.get("message") or payload.get("error") or "",
            })
        if error_code in {"expired_token", "access_denied"}:
            return jsonify({
                "status": "failed",
                "message": payload.get("error_description") or payload.get("message") or payload.get("error") or "",
            }), 400

        logger.warning("Trakt device auth check failed (error=%s): %s", error_code or "unknown", exc)
        return _json_error(
            f"Failed to check Trakt auth: {exc}",
            400,
            provider="Trakt",
            hint=_derive_provider_hint("Trakt", exc),
        )

    if data.get("access_token"):
        expires_at = TraktClient._expires_at_from_token_payload(data)
        profile_id = _current_profile_id()
        if profile_id:
            _profile_store.update_trakt_tokens(
                profile_id,
                str(data.get("access_token") or ""),
                str(data.get("refresh_token") or ""),
                expires_at,
            )
        return jsonify({
            "status": "approved",
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token", ""),
            "access_token_expires_at": expires_at,
            "saved": bool(profile_id),
        })

    return jsonify({
        "status": "pending",
        "message": data.get("error", "") or data.get("message", ""),
    })


@app.route("/api/trakt/catalogs", methods=["POST"])
def api_trakt_catalogs():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    access_token = str(body.get("access_token", "")).strip()
    refresh_token = str(body.get("refresh_token", "")).strip()
    access_token_expires_at = str(body.get("access_token_expires_at", "")).strip()
    query = str(body.get("query", "")).strip()
    if private_profile:
        client_id = client_id or private_profile["credentials"]["trakt"]["client_id"]
        client_secret = client_secret or private_profile["credentials"]["trakt"]["client_secret"]
        access_token = access_token or private_profile["credentials"]["trakt"]["access_token"]
        refresh_token = refresh_token or private_profile["credentials"]["trakt"]["refresh_token"]
        access_token_expires_at = access_token_expires_at or private_profile["credentials"]["trakt"]["access_token_expires_at"]

    if not client_id:
        return _json_error("Trakt client ID is required", 400)
    if not access_token:
        return _json_error("Trakt access token is required", 400)

    try:
        profile_id = str(private_profile.get("profile_id", "") or "") if private_profile else ""
        client = TraktClient(
            TraktConfig(
                client_id=client_id,
                client_secret=client_secret,
                access_token=access_token,
                refresh_token=refresh_token,
                access_token_expires_at=access_token_expires_at,
            ),
            token_refreshed_callback=(
                (lambda at, rt, exp="": _profile_store.update_trakt_tokens(profile_id, at, rt, exp))
                if profile_id else None
            ),
        )
        if query:
            items = client.search_lists(query)
        else:
            items = client.get_personal_lists_metadata() + client.get_liked_lists_metadata()
    except Exception as exc:
        logger.exception("Failed to load Trakt catalogs")
        upstream = _extract_upstream_status(exc)
        fallback_hint = "Your Trakt access token may be expired — reconnect Trakt in Settings." if upstream == 401 else None
        return _json_error(
            f"Failed to load Trakt catalogs: {exc}",
            400,
            provider="Trakt",
            hint=_derive_provider_hint("Trakt", exc, fallback_hint),
        )

    payload = {"items": items, "query": query}
    if client._config.access_token and client._config.access_token != access_token:
        payload["token_refreshed"] = True
        payload["access_token"] = client._config.access_token
        payload["refresh_token"] = client._config.refresh_token
        payload["access_token_expires_at"] = client._config.access_token_expires_at
        payload["saved"] = bool(profile_id)
    return jsonify(payload)


@app.route("/api/mdblist/lists", methods=["POST"])
def api_mdblist_lists():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    api_key = str(body.get("api_key", "")).strip()
    query = str(body.get("query", "")).strip()
    if not api_key and private_profile:
        api_key = private_profile["credentials"]["mdblist"]["api_key"]

    if not api_key:
        return _json_error("MDBList API key is required", 400)

    try:
        client = MdbListClient(MdbListConfig(api_key=api_key))
        items = client.search_public_lists(query) if query else client.get_user_lists()
    except Exception as exc:
        logger.exception("Failed to load MDBList lists")
        upstream = _extract_upstream_status(exc)
        fallback_hint = "Your MDBList API key looks invalid — copy a fresh key from mdblist.com settings." if upstream in (401, 403) else None
        return _json_error(
            f"Failed to load MDBList lists: {exc}",
            400,
            provider="MDBList",
            hint=_derive_provider_hint("MDBList", exc, fallback_hint),
        )

    return jsonify({"items": items, "query": query})


@app.route("/api/profile/save", methods=["POST"])
def api_profile_save():
    body = request.get_json(silent=True) or {}
    credentials = body.get("credentials", {})
    options = body.get("options", {})
    password = body.get("password", "")
    profile_id = str(body.get("profile_id", "")).strip()
    session_profile_id = _current_profile_id()
    validation_credentials = credentials

    # Saving with a UUID + password and no session authenticates and mints a
    # session cookie, so it is a second sign-in door. Without the limiter it is
    # an unthrottled way to guess a password that /api/profile/login rate-limits.
    guesses_password = not session_profile_id and bool(profile_id)
    client_key = _request_client_key()
    if guesses_password and _login_limiter.is_limited(client_key):
        return _json_error(
            "Too many sign-in attempts. Please wait and try again.",
            429,
            hint=f"Locked for up to {LOGIN_WINDOW_SECONDS // 60} minutes. Wait before retrying.",
        )

    try:
        if session_profile_id:
            existing_profile = _profile_store.get_private_profile_by_id(session_profile_id)
            validation_credentials = merge_credentials(existing_profile.get("credentials"), credentials)
        elif profile_id and password:
            existing_profile = _profile_store.get_private_profile_by_id(profile_id)
            validation_credentials = merge_credentials(existing_profile.get("credentials"), credentials)
    except KeyError:
        pass

    _, errors = _validate_profile_configuration(validation_credentials, options)
    if errors:
        return _json_error("Configuration errors", 400, errors)

    try:
        if session_profile_id:
            if profile_id and profile_id != session_profile_id:
                return _json_error("You are already signed into a different profile", 409)
            profile = _profile_store.update_profile_by_id(session_profile_id, credentials, options)
            created = False
            session_token = _session_token()
        elif profile_id:
            profile = _profile_store.update_profile(profile_id, password, credentials, options)
            created = False
            session_token = _session_store.create(profile["profile_id"])
        else:
            profile = _profile_store.create_profile(password, credentials, options)
            created = True
            session_token = _session_store.create(profile["profile_id"])
    except KeyError:
        if guesses_password:
            _login_limiter.record_failure(client_key)
        return _json_error("Profile not found", 404)
    except PermissionError:
        if guesses_password:
            _login_limiter.record_failure(client_key)
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    if guesses_password:
        _login_limiter.clear(client_key)
    response = {"profile": profile, "created": created}
    return _with_session_cookie(make_response(jsonify(response)), session_token)


@app.route("/api/logs", methods=["GET"])
def api_logs():
    # The profile filter is taken from the session, never from the query string.
    # log_capture.snapshot() only filters when given a non-empty profile id, so a
    # caller-supplied (or missing) value would return every profile's logs.
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    after_seq = request.args.get("after", 0, type=int)
    limit = min(request.args.get("limit", 300, type=int), 500)
    entries = log_capture.snapshot(after_seq=after_seq, limit=limit, profile_id=profile_id)
    return jsonify({"entries": entries})


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    # Scoped to the caller: an unfiltered clear would destroy every other user's
    # buffered logs and reset the shared sequence counter under their cursors.
    log_capture.clear(profile_id)
    return jsonify({"ok": True})


@app.route("/api/profile/status", methods=["POST"])
def api_profile_status():
    body = request.get_json(silent=True) or {}
    include_credentials = bool(body.get("include_credentials", False))
    profile = _current_public_profile(include_credentials=include_credentials)
    if not profile:
        if not include_credentials:
            profile = _public_profile_by_request_id(body.get("profile_id"))
    if not profile:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401

    return _profile_response(profile, include_credentials=include_credentials)


@app.route("/api/profile/connections/check", methods=["POST"])
def api_profile_connections_check():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    try:
        private_profile = _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    body = request.get_json(silent=True) or {}
    requested = body.get("providers")
    if requested is not None and not isinstance(requested, list):
        return _json_error("providers must be a list", 400)
    providers = [str(value or "").strip().lower() for value in (requested or HEALTH_PROVIDERS)]
    if not providers:
        providers = list(HEALTH_PROVIDERS)
    unknown = [provider for provider in providers if provider not in HEALTH_PROVIDERS]
    if unknown:
        return _json_error(f"Unknown provider: {unknown[0]}", 400)

    has_draft = "credentials" in body
    draft = body.get("credentials") if isinstance(body.get("credentials"), dict) else {}
    credentials = (
        merge_credentials(private_profile.get("credentials"), draft)
        if has_draft else normalize_credentials(private_profile.get("credentials"))
    )
    force = bool(body.get("force", True)) or has_draft
    cached = private_profile.get("connection_health") or {}
    checks_by_provider: dict[str, dict] = {}
    to_check: list[str] = []
    now = datetime.now(timezone.utc)
    for provider in providers:
        row = cached.get(provider) if isinstance(cached.get(provider), dict) else None
        checked_at = None
        if row:
            try:
                checked_at = datetime.fromisoformat(str(row.get("checked_at") or ""))
                if checked_at.tzinfo is None:
                    checked_at = checked_at.replace(tzinfo=timezone.utc)
            except ValueError:
                checked_at = None
        if not force and checked_at and (now - checked_at).total_seconds() < 900:
            checks_by_provider[provider] = copy.deepcopy(row)
        else:
            to_check.append(provider)

    if to_check:
        fresh = check_connections(
            credentials,
            to_check,
            trakt_token_callback=lambda access, refresh, expires="": _profile_store.update_trakt_tokens(
                profile_id, access, refresh, expires
            ),
        )
        checks_by_provider.update({row["provider"]: row for row in fresh})
        if not has_draft:
            _profile_store.record_connection_health(profile_id, fresh)

    checks = [checks_by_provider[provider] for provider in providers]
    all_health = dict(cached)
    all_health.update(checks_by_provider)
    config_profile = copy.deepcopy(private_profile)
    config_profile["credentials"] = credentials
    config = _config_from_profile(config_profile)
    readiness = _connection_readiness(config, list(all_health.values()))
    return jsonify({"checks": checks, "readiness": readiness})


@app.route("/api/profile/sync/runs", methods=["POST"])
def api_profile_sync_runs():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    page = body.get("page", 1)
    page_size = body.get("page_size", 25)
    try:
        payload = _profile_store.get_sync_runs(profile_id, page=page, page_size=page_size)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except ValueError as exc:
        return _json_error(str(exc), 400)
    return jsonify(payload)


@app.route("/api/profile/sync/run-details", methods=["POST"])
def api_profile_sync_run_details():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    run_id = str(body.get("run_id", "")).strip()
    if not run_id:
        return _json_error("run_id is required", 400)
    try:
        payload = _profile_store.get_sync_run_detail(profile_id, run_id)
    except KeyError:
        return _json_error("Run not found", 404)
    return jsonify({"run": _sanitize_run_detail(payload)})


@app.route("/api/profile/logs", methods=["POST"])
def api_profile_logs():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    try:
        after = int(body.get("after", 0) or 0)
        limit = int(body.get("limit", 200) or 200)
    except (TypeError, ValueError):
        return _json_error("Invalid log cursor", 400)
    return jsonify(_profile_log_store.get(profile_id, after=after, limit=limit))


@app.route("/api/profile/logs/clear", methods=["POST"])
def api_profile_logs_clear():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    _profile_log_store.clear(profile_id)
    return jsonify({"status": "cleared"})


def _library_context() -> tuple[PublicMetaDBClient | None, str, dict | None, object | None]:
    """Resolve the signed-in profile's PMDB client, TMDB key and profile.

    Returns (pmdb_client, tmdb_api_key, private_profile, error_response). When
    error_response is not None the caller must return it directly.
    """
    private_profile = _current_private_profile()
    if not private_profile:
        return None, "", None, (_clear_session_cookie(_json_error("Sign in first", 401)[0]), 401)
    credentials = normalize_credentials(private_profile.get("credentials"))
    pmdb_key = credentials["pmdb"]["api_key"]
    if not pmdb_key:
        return None, "", None, _json_error("Add your PublicMetaDB API key in Settings first", 409)
    pmdb = PublicMetaDBClient(PublicMetaDBConfig(api_key=pmdb_key))
    return pmdb, credentials["tmdb"]["api_key"], private_profile, None


def _enrich_library_items(items: list[dict], tmdb_api_key: str) -> tuple[list[dict], str]:
    """Attach TMDB title/year/poster to items carrying tmdb_id + media_type.

    Returns (items, tmdb_error). A missing key is not an error — items simply
    come back without posters and the UI shows the connect-TMDB notice.
    """
    if not tmdb_api_key:
        return items, ""
    client = TmdbClient(tmdb_api_key)
    refs = [(item.get("tmdb_id"), item.get("media_type", "")) for item in items]
    try:
        details_map = client.get_details_batch(refs)
    except TmdbError as exc:
        return items, str(exc)
    for item in items:
        try:
            numeric_id = int(item.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            continue
        details = details_map.get((tmdb_media_kind(item.get("media_type", "")), numeric_id))
        if details:
            item["title"] = details["title"] or item.get("title") or ""
            item["year"] = details["year"]
            item["poster_url"] = details["poster_url"]
    return items, ""


@app.route("/api/profile/library/overview", methods=["POST"])
def api_profile_library_overview():
    pmdb, tmdb_key, private_profile, error_response = _library_context()
    if error_response is not None:
        return error_response
    try:
        raw_lists = pmdb.get_lists()
    except Exception as exc:
        logger.warning("Library overview: PMDB list fetch failed: %s", exc)
        return _json_error("Could not load your PublicMetaDB lists", 502)
    # SyncMeta-managed lists know which service wrote them; join on list id
    # first, falling back to the list name, so the UI can filter per service.
    managed = (private_profile or {}).get("managed_lists") or []
    source_by_id = {
        str(item.get("list_id", "")).strip(): str(item.get("source_name", "")).strip()
        for item in managed if str(item.get("list_id", "")).strip()
    }
    source_by_name = {
        str(item.get("list_name", "")).strip(): str(item.get("source_name", "")).strip()
        for item in managed if str(item.get("list_name", "")).strip()
    }
    lists = [{
        "id": str(entry.get("id", "")),
        "name": str(entry.get("name", "") or "Unnamed list"),
        "type": str(entry.get("type", "") or "custom"),
        "item_count": entry.get("item_count") or entry.get("itemCount"),
        "is_public": bool(entry.get("is_public") or entry.get("isPublic")),
        "source": source_by_id.get(str(entry.get("id", "")))
            or source_by_name.get(str(entry.get("name", "") or "").strip())
            or "",
    } for entry in raw_lists if entry.get("id")]
    lists.sort(key=lambda entry: (entry["type"] != "watchlist", entry["name"].lower()))
    return jsonify({"lists": lists, "tmdb_configured": bool(tmdb_key)})


@app.route("/api/profile/library/items", methods=["POST"])
def api_profile_library_items():
    pmdb, tmdb_key, private_profile, error_response = _library_context()
    if error_response is not None:
        return error_response
    body = request.get_json(silent=True) or {}
    list_id = str(body.get("list_id", "")).strip()
    if not list_id:
        return _json_error("list_id is required", 400)
    try:
        raw_items = pmdb.get_list_items(list_id)
    except Exception as exc:
        logger.warning("Library items: PMDB fetch failed for list %s: %s", list_id, exc)
        return _json_error("Could not load items for this list", 502)
    items = [{
        "id": str(entry.get("id", "")),
        "tmdb_id": entry.get("tmdb_id"),
        "media_type": str(entry.get("media_type", "") or ""),
        "title": str(entry.get("title") or entry.get("name") or "").strip(),
    } for entry in raw_items]
    items, tmdb_error = _enrich_library_items(items, tmdb_key)
    return jsonify({
        "items": items,
        "total": len(items),
        "tmdb_configured": bool(tmdb_key),
        "tmdb_error": tmdb_error,
    })


#: How many posters a dashboard row preview shows before saying "+N more".
LIST_PREVIEW_LIMIT = 18


@app.route("/api/profile/library/list-preview", methods=["POST"])
def api_profile_library_list_preview():
    """Posters for one synced list, addressed by *name* rather than PMDB id.

    The dashboard's sync results only carry the list name — the id lives in the
    profile's managed_lists — so resolving it here keeps that mapping on the
    server instead of shipping the whole managed_lists table to the client.
    """
    pmdb, tmdb_key, private_profile, error_response = _library_context()
    if error_response is not None:
        return error_response
    body = request.get_json(silent=True) or {}
    list_name = str(body.get("list_name", "")).strip()
    if not list_name:
        return _json_error("list_name is required", 400)

    list_id = ""
    for entry in (private_profile.get("managed_lists") or []):
        if str(entry.get("list_name", "")).strip() == list_name:
            list_id = str(entry.get("list_id", "")).strip()
            break
    if not list_id:
        # A list SyncMeta has not recorded as managed (or one renamed since) can
        # still be looked up by name rather than reporting it as missing.
        try:
            found = pmdb.find_list_by_name(list_name)
        except Exception as exc:
            logger.warning("List preview: PMDB lookup failed for %r: %s", list_name, exc)
            return _json_error("Could not look up this list", 502)
        list_id = str((found or {}).get("id", "")).strip()
    if not list_id:
        return jsonify({
            "items": [], "total": 0, "shown": 0,
            "tmdb_configured": bool(tmdb_key), "tmdb_error": "",
            "missing": True,
        })

    try:
        raw_items = pmdb.get_list_items(list_id)
    except Exception as exc:
        logger.warning("List preview: PMDB fetch failed for %s: %s", list_id, exc)
        return _json_error("Could not load items for this list", 502)

    total = len(raw_items)
    items = [{
        "id": str(entry.get("id", "")),
        "tmdb_id": entry.get("tmdb_id"),
        "media_type": str(entry.get("media_type", "") or ""),
        "title": str(entry.get("title") or entry.get("name") or "").strip(),
    } for entry in raw_items[:LIST_PREVIEW_LIMIT]]
    # Only the shown slice is enriched — pulling TMDB details for a 400-item
    # list to render 18 posters is the expensive way to do this.
    items, tmdb_error = _enrich_library_items(items, tmdb_key)
    return jsonify({
        "items": items,
        "total": total,
        "shown": len(items),
        "tmdb_configured": bool(tmdb_key),
        "tmdb_error": tmdb_error,
        "missing": False,
    })


def _normalized_history_entries(pmdb: PublicMetaDBClient) -> list[dict]:
    raw_entries = pmdb.get_watched_history()
    entries = []
    for entry in raw_entries:
        try:
            numeric_id = int(entry.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            continue
        if numeric_id <= 0:
            continue
        entries.append({
            "tmdb_id": numeric_id,
            "media_type": str(entry.get("media_type", "") or ""),
            "season": entry.get("season"),
            "episode": entry.get("episode"),
            "watched_at": str(entry.get("watched_at") or entry.get("watchedAt") or ""),
            "title": str(entry.get("title") or "").strip(),
        })
    return entries


@app.route("/api/profile/library/history", methods=["POST"])
def api_profile_library_history():
    """Watch history grouped one row per title, newest activity first.

    Raw plays would render a wall of duplicate posters for a binged show; the
    per-episode breakdown lives behind /library/history/title instead.
    """
    pmdb, tmdb_key, private_profile, error_response = _library_context()
    if error_response is not None:
        return error_response
    body = request.get_json(silent=True) or {}
    try:
        limit = max(1, min(int(body.get("limit", 500) or 500), 2000))
    except (TypeError, ValueError):
        return _json_error("Invalid limit", 400)
    try:
        entries = _normalized_history_entries(pmdb)
    except Exception as exc:
        logger.warning("Library history: PMDB fetch failed: %s", exc)
        return _json_error("Could not load your watch history", 502)

    groups: dict[tuple[str, int], dict] = {}
    for entry in entries:
        kind = tmdb_media_kind(entry["media_type"])
        key = (kind, entry["tmdb_id"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "tmdb_id": entry["tmdb_id"],
                "media_type": kind,
                "title": entry["title"],
                "play_count": 0,
                "episode_keys": set(),
                "last_watched_at": "",
            }
        group["play_count"] += 1
        if not group["title"] and entry["title"]:
            group["title"] = entry["title"]
        if entry["season"] is not None and entry["episode"] is not None:
            group["episode_keys"].add((entry["season"], entry["episode"]))
        if entry["watched_at"] > group["last_watched_at"]:
            group["last_watched_at"] = entry["watched_at"]

    items = []
    for group in groups.values():
        items.append({
            "tmdb_id": group["tmdb_id"],
            "media_type": group["media_type"],
            "title": group["title"],
            "play_count": group["play_count"],
            "episodes_watched": len(group["episode_keys"]),
            "last_watched_at": group["last_watched_at"],
        })
    items.sort(key=lambda item: item["last_watched_at"], reverse=True)
    total = len(items)
    items = items[:limit]
    items, tmdb_error = _enrich_library_items(items, tmdb_key)
    return jsonify({
        "items": items,
        "total": total,
        "total_plays": len(entries),
        "tmdb_configured": bool(tmdb_key),
        "tmdb_error": tmdb_error,
    })


@app.route("/api/profile/library/history/title", methods=["POST"])
def api_profile_library_history_title():
    """Per-title watch history: every episode watched (with TMDB episode names
    and thumbnails when a TMDB key is saved) or, for movies, every play."""
    pmdb, tmdb_key, private_profile, error_response = _library_context()
    if error_response is not None:
        return error_response
    body = request.get_json(silent=True) or {}
    try:
        tmdb_id = int(body.get("tmdb_id") or 0)
    except (TypeError, ValueError):
        return _json_error("tmdb_id is required", 400)
    if tmdb_id <= 0:
        return _json_error("tmdb_id is required", 400)
    kind = tmdb_media_kind(body.get("media_type", ""))

    try:
        entries = _normalized_history_entries(pmdb)
    except Exception as exc:
        logger.warning("Library history title: PMDB fetch failed: %s", exc)
        return _json_error("Could not load your watch history", 502)
    entries = [
        entry for entry in entries
        if entry["tmdb_id"] == tmdb_id and tmdb_media_kind(entry["media_type"]) == kind
    ]

    fallback_title = next((entry["title"] for entry in entries if entry["title"]), "")
    result = {
        "tmdb_id": tmdb_id,
        "media_type": kind,
        "title": fallback_title,
        "year": "",
        "poster_url": "",
        "episodes": [],
        "plays": [],
        "tmdb_configured": bool(tmdb_key),
        "tmdb_error": "",
    }

    if kind == "movie":
        result["plays"] = sorted((entry["watched_at"] for entry in entries), reverse=True)
    else:
        # Collapse repeat plays of the same episode; keep the latest date.
        by_episode: dict[tuple, dict] = {}
        for entry in entries:
            episode_key = (entry["season"], entry["episode"])
            row = by_episode.get(episode_key)
            if row is None:
                row = by_episode[episode_key] = {
                    "season": entry["season"],
                    "episode": entry["episode"],
                    "plays": 0,
                    "watched_at": "",
                }
            row["plays"] += 1
            if entry["watched_at"] > row["watched_at"]:
                row["watched_at"] = entry["watched_at"]
        episodes = list(by_episode.values())
        # Whole-show plays (no season/episode) sort last; numbered ones by S/E.
        episodes.sort(key=lambda row: (
            row["season"] is None,
            row["season"] if row["season"] is not None else 0,
            row["episode"] if row["episode"] is not None else 0,
        ))
        result["episodes"] = episodes

    if tmdb_key:
        client = TmdbClient(tmdb_key)
        try:
            details = client.get_details(tmdb_id, kind)
            if details:
                result["title"] = details["title"] or result["title"]
                result["year"] = details["year"]
                result["poster_url"] = details["poster_url"]
            seasons = sorted({
                int(row["season"]) for row in result["episodes"]
                if row["season"] is not None
            })
            for season in seasons:
                season_map = client.get_season_episodes(tmdb_id, season)
                for row in result["episodes"]:
                    if row["season"] != season or row["episode"] is None:
                        continue
                    try:
                        episode_details = season_map.get(int(row["episode"]))
                    except (TypeError, ValueError):
                        episode_details = None
                    if episode_details:
                        row["name"] = episode_details["name"]
                        row["still_url"] = episode_details["still_url"]
                        row["air_date"] = episode_details["air_date"]
        except TmdbError as exc:
            result["tmdb_error"] = str(exc)

    return jsonify(result)


@app.route("/api/profile/list/delete", methods=["POST"])
def api_profile_list_delete():
    body = request.get_json(silent=True) or {}
    list_name = str(body.get("list_name", "")).strip()
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    if not list_name:
        return _json_error("List name is required", 400)

    profile = _current_private_profile()
    if not profile:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    if profile.get("sync_running"):
        return _json_error("Wait for the current sync to finish before deleting a list", 409)

    managed_entry = _find_managed_list(profile, list_name)
    if not managed_entry:
        return _json_error("Managed list not found", 404)

    try:
        pmdb_client = PublicMetaDBClient(_config_from_profile(profile).pmdb)
        list_id = str(managed_entry.get("list_id", "")).strip()
        if list_id:
            pmdb_client.delete_list(list_id)
        else:
            existing = pmdb_client.find_list_by_name(list_name)
            if existing:
                pmdb_client.delete_list(str(existing.get("id", "")).strip())
        # Also unselect the source entry so the deleted PMDB list is not
        # recreated during the next list sync.
        updated_credentials = _remove_managed_selection(profile, managed_entry)
        updated_profile = _profile_store.delete_managed_list_by_id(
            profile_id, list_name, updated_credentials
        )
    except Exception as exc:
        logger.exception("Failed to delete managed list %s for profile %s", list_name, profile_id[:8])
        return _json_error(str(exc), 500)

    return _profile_response(updated_profile, include_credentials=True)


@app.route("/api/profile/sync", methods=["POST"])
def api_profile_sync():
    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", False))
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401

    try:
        profile = _profile_store.claim_profile_for_sync_by_id(profile_id, sync_modes={
            "lists": True,
            "history": False,
            "resume": False,
        })
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except RuntimeError:
        return _json_error("Sync already in progress", 409)

    sync_modes = {"lists": True, "history": False, "resume": False}
    _sync_runner.enqueue(profile, dry_run, sync_modes)
    logger.info("Queued %slist sync", "dry-run " if dry_run else "")
    return jsonify({"status": "started", "dry_run": dry_run, "queued_jobs": _sync_runner.queue_size()})


@app.route("/api/profile/activity/sync", methods=["POST"])
def api_profile_activity_sync():
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode", "")).strip().lower()
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    if mode not in {"history", "resume"}:
        return _json_error("Activity sync mode must be 'history' or 'resume'", 400)

    profile = _current_private_profile()
    if not profile:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    options = normalize_profile_options(profile.get("options"))
    if mode == "history" and options["activity_history_source"] == "off":
        return _json_error("Select a watch history source in Settings first", 409)
    if mode == "resume" and options["activity_resume_source"] == "off":
        return _json_error("Select a resume progress source in Settings first", 409)

    sync_modes = {
        "lists": False,
        "history": mode == "history",
        "resume": mode == "resume",
        "full_history": mode == "history",
    }

    try:
        claimed = _profile_store.claim_profile_for_sync_by_id(profile_id, sync_modes=sync_modes)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except RuntimeError:
        return _json_error("Sync already in progress", 409)

    _sync_runner.enqueue(claimed, False, sync_modes)
    logger.info("Queued %s sync", mode)
    return jsonify({"status": "started", "mode": mode, "queued_jobs": _sync_runner.queue_size()})


@app.route("/api/profile/activity/history/clear", methods=["POST"])
def api_profile_activity_history_clear():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401

    profile = _current_private_profile()
    if not profile:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    if profile.get("sync_running"):
        return _json_error("Wait for the current sync to finish before clearing watch history", 409)

    credentials = normalize_credentials(profile.get("credentials"))
    if not credentials["pmdb"]["api_key"]:
        return _json_error("Save your PublicMetaDB API key first", 409)

    try:
        deleted_count = PublicMetaDBClient(_config_from_profile(profile).pmdb).clear_watched_history()
    except Exception as exc:
        logger.exception("Failed to clear PublicMetaDB watch history for profile %s", profile_id[:8])
        return _json_error(str(exc), 500)

    try:
        profile = _profile_store.reset_history_import_state_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    return jsonify({"status": "cleared", "deleted_count": deleted_count, "profile": profile})


@app.route("/api/profile/sync/stop", methods=["POST"])
def api_profile_sync_stop():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401

    try:
        raw = _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    if not raw.get("sync_running"):
        return _json_error("No sync is currently running", 409)

    if _sync_runner.cancel_if_queued(profile_id):
        # Queued but not yet running — record cancellation immediately so the
        # profile is freed for a new sync without waiting for a worker slot.
        sync_modes = raw.get("pending_sync_modes")
        profile = _profile_store.record_sync_cancelled(profile_id, sync_modes=sync_modes)
        return jsonify({"status": "stopped", "profile": profile})

    try:
        profile = _profile_store.request_sync_cancel(profile_id)
    except RuntimeError as exc:
        return _json_error(str(exc), 409)

    return jsonify({"status": "stopping", "profile": profile})


@app.route("/api/profile/pairs", methods=["POST"])
def api_profile_pairs():
    """Return the profile's sync pairs plus what each provider can actually do."""
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    try:
        private_profile = _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    config = _config_from_profile(private_profile)
    pairs = []
    adapters = _build_provider_adapters(config)
    service = CrossSyncService(adapters)
    # Last outcome per pair, so the editor can show what a pair actually did
    # instead of only what it is configured to do.
    last_results = private_profile.get("last_pair_results") or {}
    pair_schedule = private_profile.get("pair_sync_schedule") or {}
    for pair in _sync_pairs_from_config(config):
        entry = pair.to_dict()
        # Surface why a pair cannot run so the editor can explain it in place
        # rather than only failing at run time.
        entry["problem"] = service.validate_pair(pair)
        entry["last_result"] = last_results.get(pair.pair_id) or None
        schedule = pair_schedule.get(pair.pair_id) if isinstance(pair_schedule.get(pair.pair_id), dict) else {}
        entry["last_sync_at"] = schedule.get("last_sync_at")
        entry["next_sync_at"] = schedule.get("next_sync_at")
        pairs.append(entry)

    return jsonify({"pairs": pairs, **_pair_capabilities(config)})


@app.route("/api/profile/pairs/lists", methods=["POST"])
def api_profile_pairs_lists():
    """Named lists for one provider.

    Kept out of the capability response on purpose: enumerating lists calls each
    provider's API, and doing it for every provider made opening the sync view
    wait on live network round trips.
    """
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    provider = str(body.get("provider", "") or "").strip().lower()
    if not provider:
        return _json_error("provider is required", 400)

    try:
        private_profile = _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    adapters = _build_provider_adapters(_config_from_profile(private_profile))
    adapter = adapters.get(provider)
    if adapter is None:
        return jsonify({"provider": provider, "lists": [], "results": []})

    role = str(body.get("role", "source") or "source").strip().lower()
    search = str(body.get("search", "") or "").strip()

    if role == "target":
        # Only providers that can genuinely write into a named list offer one.
        return jsonify({"provider": provider, "lists": adapter.safe_target_lists(), "results": []})

    return jsonify({
        "provider": provider,
        "lists": adapter.safe_list_sources(),
        "results": adapter.safe_search_lists(search),
    })


@app.route("/api/profile/pairs/save", methods=["POST"])
def api_profile_pairs_save():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    raw_pairs = body.get("pairs")
    if not isinstance(raw_pairs, list):
        return _json_error("pairs must be a list", 400)

    # Validate strictly here (unlike profile load, which drops bad entries) so
    # the user gets told what is wrong instead of silently losing a pair.
    normalized: list[dict] = []
    adapter_types = {
        "trakt": TraktAdapter, "simkl": SimklAdapter, "anilist": AniListAdapter,
        "mdblist": MdbListAdapter, "pmdb": PmdbAdapter,
    }
    for index, raw in enumerate(raw_pairs):
        try:
            pair = SyncPair.from_dict(raw)
        except (ValueError, TypeError) as exc:
            return _json_error(f"Pair {index + 1}: {exc}", 400)
        pair.pair_id = pair.pair_id or SyncPair._clean_pair_id(
            f"{pair.source}-{pair.target}-{index + 1}"
        ) or f"pair-{index + 1}"
        source_type = adapter_types.get(pair.source)
        target_type = adapter_types.get(pair.target)
        if source_type is None or target_type is None:
            return _json_error(f"Pair {index + 1}: unknown provider", 400)
        unsupported = [
            category for category in pair.categories
            if category not in source_type.reads or category not in target_type.writes
        ]
        if unsupported:
            return _json_error(
                f"Pair {index + 1}: {pair.source} → {pair.target} does not support "
                f"{', '.join(unsupported)}", 400,
            )
        if pair.target_list and not target_type.supports_target_lists:
            return _json_error(
                f"Pair {index + 1}: {target_type.label} does not support writable custom lists", 400,
            )
        if pair.target_list and any(
            category not in target_type.target_list_categories for category in pair.categories
        ):
            return _json_error(
                f"Pair {index + 1}: the selected custom list cannot receive every selected category", 400,
            )
        normalized.append(pair.to_dict())

    seen = set()
    for pair in normalized:
        if pair["pair_id"] in seen:
            return _json_error(f"Duplicate pair id {pair['pair_id']!r}", 400)
        seen.add(pair["pair_id"])

    try:
        profile = _profile_store.update_sync_pairs(profile_id, normalized)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except ValueError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"status": "saved", "profile": profile})


@app.route("/api/profile/pairs/run", methods=["POST"])
def api_profile_pairs_run():
    """Queue one pair, or every enabled pair, on the shared background runner."""
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    pair_id = str(body.get("pair_id", "") or "").strip()
    dry_run = bool(body.get("dry_run", False))

    try:
        private_profile = _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    config = _config_from_profile(private_profile)
    pairs = _sync_pairs_from_config(config)
    if pair_id:
        pairs = [p for p in pairs if p.pair_id == pair_id]
        if not pairs:
            return _json_error("Sync pair not found", 404)

    if not pairs:
        return _json_error("No sync pairs configured", 400)

    background = bool(body.get("background", False))
    if not background:
        activity_state = private_profile.get("activity_state") or {}
        service = CrossSyncService(
            _build_provider_adapters(config), dry_run=dry_run,
            managed_keys=activity_state.get("pair_managed_keys") or {},
            status_callback=lambda status: _profile_store.update_sync_status(profile_id, status),
        )
        log_token = _log_profile_id.set(profile_id)
        try:
            results = service.run_pairs(pairs)
        except SyncCancelled:
            return _json_error("Sync stopped", 409)
        except Exception as exc:
            logger.exception("Cross-service sync failed")
            return _json_error(f"Sync failed: {exc}", 500)
        finally:
            _log_profile_id.reset(log_token)
        if not dry_run:
            _profile_store.update_pair_managed_keys(profile_id, service.managed_keys)
            _profile_store.update_pair_last_results(profile_id, [r.to_dict() for r in results])
            _profile_store.mark_pairs_synced(profile_id, [pair.pair_id for pair in pairs])
        return jsonify({
            "status": "completed", "dry_run": dry_run,
            "results": [result.to_dict() for result in results],
            "provider_reads": service.last_run_provider_reads,
            "cached_reads": service.last_run_cache_hits,
        })

    try:
        modes = {
            "lists": False,
            "history": False,
            "resume": False,
            "pairs": True,
            "pair_ids": [pair.pair_id for pair in pairs],
        }
        claimed = _profile_store.claim_profile_for_sync_by_id(profile_id, modes)
    except RuntimeError as exc:
        return _json_error(str(exc), 409)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    _sync_runner.enqueue(claimed, dry_run, modes)
    return jsonify({
        "status": "queued",
        "dry_run": dry_run,
        "job_id": claimed.get("sync_job_id"),
        "pair_ids": modes["pair_ids"],
        "profile": _profile_store.get_profile_by_id(profile_id, include_credentials=False),
    }), 202


@app.route("/api/profile/unresolved", methods=["POST"])
def api_profile_unresolved():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    try:
        items = _profile_store.get_unresolved_items(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    return jsonify({"items": items})


@app.route("/api/profile/unresolved/resolve", methods=["POST"])
def api_profile_unresolved_resolve():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    cache_key = str(body.get("cache_key", "")).strip()
    tmdb_id_raw = body.get("tmdb_id")
    if not cache_key:
        return _json_error("cache_key is required", 400)
    try:
        tmdb_id = _parse_tmdb_id(tmdb_id_raw)
    except ValueError:
        return _json_error("tmdb_id must be a positive integer", 400)

    try:
        private_profile = _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    # Find the unresolved item so we know its media_type and target list.
    unresolved = _profile_store.get_unresolved_items(profile_id)
    target_item = next((i for i in unresolved if i.get("cache_key") == cache_key), None)

    return jsonify(_apply_unresolved_resolution(profile_id, private_profile, cache_key, target_item, tmdb_id))


@app.route("/api/profile/unresolved/auto-resolve", methods=["POST"])
def api_profile_unresolved_auto_resolve():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    cache_key = str(body.get("cache_key", "")).strip()
    if not cache_key:
        return _json_error("cache_key is required", 400)

    try:
        private_profile = _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    unresolved = _profile_store.get_unresolved_items(profile_id)
    target_item = next((i for i in unresolved if i.get("cache_key") == cache_key), None)
    if not target_item:
        return _json_error("Unresolved item not found", 404)

    tmdb_id = _resolve_unresolved_item_automatically(private_profile, target_item)
    if not tmdb_id:
        return jsonify({
            "status": "unresolved",
            "items": unresolved,
            "message": "No automatic mapping found yet.",
        }), 404

    return jsonify(_apply_unresolved_resolution(profile_id, private_profile, cache_key, target_item, tmdb_id))


@app.route("/api/profile/unresolved/dismiss", methods=["POST"])
def api_profile_unresolved_dismiss():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    cache_key = str(body.get("cache_key", "")).strip()
    if not cache_key:
        return _json_error("cache_key is required", 400)
    try:
        remaining = _profile_store.dismiss_unresolved_item(profile_id, cache_key)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    return jsonify({"status": "dismissed", "items": remaining})


# ── Admin panel ───────────────────────────────────────────────────────────────

_admin_sessions: dict[str, float] = {}
_admin_sessions_lock = threading.Lock()


def _create_admin_session() -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _admin_sessions_lock:
        expired = [k for k, v in list(_admin_sessions.items()) if v < now]
        for k in expired:
            del _admin_sessions[k]
        _admin_sessions[token] = now + ADMIN_SESSION_TTL
    return token


def _is_valid_admin_session(token: str | None) -> bool:
    if not token:
        return False
    with _admin_sessions_lock:
        exp = _admin_sessions.get(token)
        return bool(exp and time.time() < exp)


def _destroy_admin_session(token: str | None) -> None:
    if token:
        with _admin_sessions_lock:
            _admin_sessions.pop(token, None)


def _admin_token() -> str | None:
    return request.cookies.get(ADMIN_COOKIE_NAME)


def _is_admin() -> bool:
    return bool(ADMIN_PASSWORD) and _is_valid_admin_session(_admin_token())


def _admin_stats() -> dict:  # noqa: C901
    import datetime as _dt
    import os as _os
    import platform as _platform
    import sys as _sys
    from src import api_logger
    from src import anime_mapping_store as _ams

    # ── uptime ──────────────────────────────────────────────────────────────
    uptime_s = int(time.time() - _server_start_time)
    d, rem = divmod(uptime_s, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        uptime_str = f"{d}d {h}h {m}m"
    elif h:
        uptime_str = f"{h}h {m}m {s}s"
    else:
        uptime_str = f"{m}m {s}s"

    # ── profiles ─────────────────────────────────────────────────────────────
    with _profile_store._lock:
        profiles_raw = list(_profile_store._profiles.values())

    from src.profile_store import _configured_sources_for_profile, parse_iso_datetime
    active = [p for p in profiles_raw if p.get("sync_running")]
    errored = [p for p in profiles_raw if p.get("sync_error")]

    profile_rows = []
    for p in profiles_raw:
        pid = p.get("profile_id", "")
        sources = _configured_sources_for_profile(p)
        res_cache = p.get("resolution_cache") or {}
        fail_cache = p.get("failed_resolution_cache") or {}
        manual_cache = p.get("manual_resolution_cache") or {}
        unresolved = p.get("unresolved_items") or []
        managed = p.get("managed_lists") or []
        last_sync = p.get("last_sync") or ""
        next_sync = p.get("next_sync_at") or ""
        err = p.get("sync_error") or ""

        # humanise timestamps
        def _rel(iso: str) -> str:
            if not iso:
                return "—"
            try:
                dt = _dt.datetime.fromisoformat(iso)
                if dt.tzinfo:
                    dt = dt.astimezone().replace(tzinfo=None)
                diff = _dt.datetime.now() - dt
                secs = int(diff.total_seconds())
                if secs < 0:
                    secs = -secs
                    sign = "in "
                else:
                    sign = ""
                if secs < 60:
                    return f"{sign}{secs}s ago" if not sign else f"in {secs}s"
                if secs < 3600:
                    return f"{sign}{secs // 60}m ago" if not sign else f"in {secs // 60}m"
                if secs < 86400:
                    return f"{sign}{secs // 3600}h ago" if not sign else f"in {secs // 3600}h"
                return f"{sign}{secs // 86400}d ago" if not sign else f"in {secs // 86400}d"
            except Exception:
                return iso[:16]

        profile_rows.append({
            "id": pid[:8],
            "sources": sources,
            "managed_lists": len(managed),
            "last_sync": _rel(last_sync),
            "next_sync": _rel(next_sync),
            "running": bool(p.get("sync_running")),
            "error": err[:120] if err else "",
            "resolution_cache": len(res_cache),
            "failed_cache": len(fail_cache),
            "manual_cache": len(manual_cache),
            "unresolved": len(unresolved),
        })

    # ── site stats (last 24h) ────────────────────────────────────────────────
    try:
        site_stats = _profile_store.get_site_stats()
        last_24h = site_stats.get("last_24h", {})
    except Exception:
        last_24h = {}

    # ── cache / anime mapping ────────────────────────────────────────────────
    mapping_cache_meta = _ams.cache_metadata()
    fribb_meta = mapping_cache_meta.get("fribb", {})
    xml_meta = mapping_cache_meta.get("anime_lists_xml", {})

    # data dir sizes
    data_dir = PROFILE_STORE_FILE.parent
    cache_dir = data_dir / "cache"

    def _dir_size_mb(p: Path) -> str:
        try:
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            return f"{total / 1_048_576:.1f} MB"
        except Exception:
            return "—"

    def _file_size(p: Path) -> str:
        try:
            size = p.stat().st_size
            if size < 1024:
                return f"{size} B"
            if size < 1_048_576:
                return f"{size / 1024:.1f} KB"
            return f"{size / 1_048_576:.1f} MB"
        except Exception:
            return "—"

    # aggregate resolution caches across profiles
    total_res = sum(len(p.get("resolution_cache") or {}) for p in profiles_raw)
    total_fail = sum(len(p.get("failed_resolution_cache") or {}) for p in profiles_raw)
    total_manual = sum(len(p.get("manual_resolution_cache") or {}) for p in profiles_raw)
    total_unresolved = sum(len(p.get("unresolved_items") or []) for p in profiles_raw)

    cache_info = {
        "fribb_loaded": bool(fribb_meta.get("loaded")),
        "fribb_entries": int(fribb_meta.get("entries") or 0),
        "fribb_last_checked_at": fribb_meta.get("last_checked_at", ""),
        "fribb_cache_updated_at": fribb_meta.get("cache_updated_at", ""),
        "fribb_source_url": fribb_meta.get("source_url", ""),
        "fribb_etag": fribb_meta.get("etag", ""),
        "xml_loaded": bool(xml_meta.get("loaded")),
        "xml_entries": int(xml_meta.get("entries") or 0),
        "xml_last_checked_at": xml_meta.get("last_checked_at", ""),
        "xml_cache_updated_at": xml_meta.get("cache_updated_at", ""),
        "xml_source_url": xml_meta.get("source_url", ""),
        "xml_etag": xml_meta.get("etag", ""),
        "mapping_refresh_interval_seconds": int(mapping_cache_meta.get("refresh_interval_seconds") or 0),
        "season_group_cache": int(mapping_cache_meta.get("season_group_cache") or 0),
        "mapping_str_cache": int(mapping_cache_meta.get("mapping_string_cache") or 0),
        "resolution_cache_total": total_res,
        "failed_cache_total": total_fail,
        "manual_cache_total": total_manual,
        "unresolved_total": total_unresolved,
        "profiles_file_size": _file_size(PROFILE_STORE_FILE),
        "cache_dir_size": _dir_size_mb(cache_dir),
        "data_dir_size": _dir_size_mb(data_dir),
    }

    # ── system info ──────────────────────────────────────────────────────────
    mem_rss = "—"
    mem_vms = "—"
    try:
        import psutil as _ps
        proc = _ps.Process()
        mi = proc.memory_info()
        mem_rss = f"{mi.rss / 1_048_576:.1f} MB"
        mem_vms = f"{mi.vms / 1_048_576:.1f} MB"
    except Exception:
        pass

    system_info = {
        "python": _sys.version.split()[0],
        "platform": _platform.platform(terse=True),
        "pid": _os.getpid(),
        "mem_rss": mem_rss,
        "mem_vms": mem_vms,
        "started_at": _dt.datetime.fromtimestamp(_server_start_time).strftime("%Y-%m-%d %H:%M:%S"),
    }

    # ── API log ──────────────────────────────────────────────────────────────
    raw_counters = api_logger.counters()
    req_map = raw_counters.get("requests", {})
    err_map = raw_counters.get("errors", {})
    all_sources = sorted(set(list(req_map.keys()) + list(err_map.keys())))
    counters_normalized = {
        src: {"total": req_map.get(src, 0), "errors": err_map.get(src, 0)}
        for src in all_sources
    }
    total_calls = sum(v["total"] for v in counters_normalized.values())
    total_errors = sum(v["errors"] for v in counters_normalized.values())

    raw_log = api_logger.snapshot(300)
    for entry in raw_log:
        ts = entry.get("ts", 0)
        entry["ts_str"] = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"

    return {
        "uptime_seconds": uptime_s,
        "uptime_str": uptime_str,
        "profile_count": len(profiles_raw),
        "active_syncs": len(active),
        "errored_profiles": len(errored),
        "active_sync_ids": [p.get("profile_id", "")[:8] for p in active],
        "profile_rows": profile_rows,
        "last_24h": last_24h,
        "cache": cache_info,
        "system": system_info,
        "counters": counters_normalized,
        "total_api_calls": total_calls,
        "total_api_errors": total_errors,
        "log": raw_log,
    }


@app.route("/admin", methods=["GET"])
@app.route("/admin/", methods=["GET"])
def admin_dashboard():
    if not ADMIN_PASSWORD:
        return _json_error("Admin panel is not configured. Set ADMIN_PASSWORD env var.", 404)
    if not _is_admin():
        return render_template("admin.html", view="login", error=None), 401
    return render_template(
        "admin.html",
        view="dashboard",
        stats=_admin_stats(),
        settings=_describe_settings(_settings_store, _ENV_BEFORE_OVERRIDES),
    )


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return _json_error("Admin panel not configured", 404)
    password = (request.form.get("password") or "").strip()
    if not secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        return render_template("admin.html", view="login", error="Wrong password"), 401
    token = _create_admin_session()
    resp = make_response(redirect("/admin"))
    resp.set_cookie(ADMIN_COOKIE_NAME, token, httponly=True, samesite="Lax",
                    secure=_cookie_secure(), max_age=ADMIN_SESSION_TTL)
    return resp


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    _destroy_admin_session(_admin_token())
    resp = make_response(redirect("/admin"))
    resp.delete_cookie(ADMIN_COOKIE_NAME)
    return resp


@app.route("/admin/api/stats", methods=["GET"])
def admin_api_stats():
    if not ADMIN_PASSWORD:
        return _json_error("Admin panel not configured", 404)
    if not _is_admin():
        return _json_error("Not authorized", 401)
    return jsonify(_admin_stats())


def _live_apply_setting(key: str, value: str) -> bool:
    """Apply a changed setting to the running process where that is possible.

    Most tunables are captured into a module constant or a thread pool at import
    time and genuinely cannot move until a restart — the panel says so per
    setting rather than pretending otherwise. These few are read on every use,
    so they can change now.
    """
    if key == "ADMIN_PASSWORD":
        globals()["ADMIN_PASSWORD"] = value.strip()
        return True
    if key == "SITE_ACCESS_PASSWORD":
        globals()["SITE_ACCESS_PASSWORD"] = value.strip()
        return True
    if key == "SYNCMETA_SCHEDULER_CLAIM_BATCH":
        globals()["SCHEDULER_CLAIM_BATCH"] = max(1, int(value or "1"))
        return True
    if key == "DISABLE_PROFILE_SCHEDULER":
        _apply_scheduler_enabled()
        return True
    return False


@app.route("/admin/api/settings", methods=["GET"])
def admin_api_settings():
    if not ADMIN_PASSWORD:
        return _json_error("Admin panel not configured", 404)
    if not _is_admin():
        return _json_error("Not authorized", 401)
    return jsonify(_describe_settings(_settings_store, _ENV_BEFORE_OVERRIDES))


@app.route("/admin/api/settings", methods=["POST"])
def admin_api_settings_save():
    if not ADMIN_PASSWORD:
        return _json_error("Admin panel not configured", 404)
    if not _is_admin():
        return _json_error("Not authorized", 401)

    body = request.get_json(silent=True) or {}
    values = body.get("values")
    if not isinstance(values, dict):
        return _json_error("Expected a values object", 400)

    try:
        changed = _settings_store.set_many(values)
    except SettingsError as exc:
        return _json_error(str(exc), 400)
    except OSError as exc:
        return _json_error(f"Could not write {_settings_store.path}: {exc}", 500)

    applied_live: list[str] = []
    needs_restart: list[str] = []
    for key, value in changed.items():
        # An override that was cleared falls back to the environment value, which
        # is what the process should go back to.
        effective = value if value != "" else str(_ENV_BEFORE_OVERRIDES.get(key, "") or "")
        if value == "":
            os.environ.pop(key, None)
            if effective:
                os.environ[key] = effective
        else:
            os.environ[key] = value
        if _live_apply_setting(key, effective):
            applied_live.append(key)
        else:
            needs_restart.append(key)

    if changed:
        logger.info("Admin updated settings: %s", ", ".join(sorted(changed)))

    response = jsonify({
        "ok": True,
        "changed": sorted(changed),
        "applied_live": sorted(applied_live),
        "needs_restart": sorted(needs_restart),
        "settings": _describe_settings(_settings_store, _ENV_BEFORE_OVERRIDES),
    })
    if "SITE_ACCESS_PASSWORD" in changed and SITE_ACCESS_PASSWORD:
        # The site gate covers /admin too, so turning it on from this panel
        # would lock the admin out of the page they are standing on. They have
        # just proved they know the password — they chose it — so hand this
        # browser the cookie instead of bouncing it to /access.
        return _with_access_cookie(response, _access_store.create("site-access"))
    return response


@app.route("/admin/api/settings/reset", methods=["POST"])
def admin_api_settings_reset():
    if not ADMIN_PASSWORD:
        return _json_error("Admin panel not configured", 404)
    if not _is_admin():
        return _json_error("Not authorized", 401)

    key = str((request.get_json(silent=True) or {}).get("key") or "").strip()
    if key == "ADMIN_PASSWORD" and not str(_ENV_BEFORE_OVERRIDES.get(key, "") or "").strip():
        # Nothing to fall back to: resetting would leave the panel with no
        # password, which does not "restore the default" — it locks it out.
        return _json_error(
            "There is no ADMIN_PASSWORD in the environment to fall back to, so this "
            "cannot be reset — set a new password instead.", 400,
        )
    try:
        removed = _settings_store.reset(key)
    except SettingsError as exc:
        return _json_error(str(exc), 400)
    except OSError as exc:
        return _json_error(f"Could not write {_settings_store.path}: {exc}", 500)

    needs_restart = False
    if removed:
        fallback = str(_ENV_BEFORE_OVERRIDES.get(key, "") or "")
        os.environ.pop(key, None)
        if fallback:
            os.environ[key] = fallback
        needs_restart = not _live_apply_setting(key, fallback)

    response = jsonify({
        "ok": True,
        "removed": removed,
        "needs_restart": needs_restart,
        "settings": _describe_settings(_settings_store, _ENV_BEFORE_OVERRIDES),
    })
    if removed and key == "SITE_ACCESS_PASSWORD" and SITE_ACCESS_PASSWORD:
        # Same as saving one: reverting to the compose value re-arms the gate
        # in front of this very page.
        return _with_access_cookie(response, _access_store.create("site-access"))
    return response


@app.route("/admin/api/repair-anime-cache", methods=["POST"])
def admin_repair_anime_cache():
    """Run the anime cache repair inline (no subprocess required).

    Accepts optional JSON body: {"clear_anime_auto": true} to also wipe all
    non-manual anime list_identity auto-cache entries (nuclear re-resolve).

    This replicates the logic from scripts/repair_anime_cache.py but operates
    directly on the live profile store.  Returns a summary of changes made.
    """
    if not ADMIN_PASSWORD:
        return _json_error("Admin panel not configured", 404)
    if not _is_admin():
        return _json_error("Not authorized", 401)

    body = request.get_json(silent=True) or {}
    clear_anime_auto = bool(body.get("clear_anime_auto", False))

    FLAGGED_TMDB_IDS = {"277700", "154634", "317316", "298754"}
    report: list[str] = []
    total_cleared = 0

    def _cache_key_is_anime_list(ck: str) -> bool:
        parts = ck.split(":")
        if len(parts) < 11:
            return False
        resolver_mode = parts[1]
        mal_id = parts[5]
        anilist_id = parts[6]
        return resolver_mode == "list_identity" and (bool(anilist_id) or bool(mal_id))

    with _profile_store._lock:
        for pid, profile in _profile_store._profiles.items():
            rc = dict(profile.get("resolution_cache") or {})
            mrc = dict(profile.get("manual_resolution_cache") or {})
            frc = dict(profile.get("failed_resolution_cache") or {})
            unresolved = list(profile.get("unresolved_items") or [])
            changed = False

            # 1. Stale auto entries that conflict with manual entries.
            for ck, manual_tmdb in mrc.items():
                auto_tmdb = rc.get(ck)
                if auto_tmdb is not None and auto_tmdb != manual_tmdb:
                    report.append(f"{pid[:8]}: stale rc[{ck!r}]={auto_tmdb} vs manual={manual_tmdb} — cleared")
                    del rc[ck]
                    changed = True
                    total_cleared += 1

            # 2. (removed) Duplicate TMDB ID collision cleanup — no longer valid.
            #    PMDB is season-agnostic: multiple anime seasons legitimately share
            #    the same TMDB ID, so clearing "duplicates" just creates a loop.

            # 3. Flagged TMDB IDs — clear from auto cache so they get fresh lookups.
            for ck, tid in list(rc.items()):
                if str(tid) in FLAGGED_TMDB_IDS and ck not in mrc:
                    report.append(f"{pid[:8]}: flagged tmdb={tid} rc[{ck!r}] — cleared")
                    rc.pop(ck, None)
                    frc.pop(ck, None)
                    changed = True
                    total_cleared += 1

            # 4. Stale unresolved items already in manual cache.
            before = len(unresolved)
            unresolved = [i for i in unresolved if i.get("cache_key") not in mrc]
            if len(unresolved) != before:
                report.append(f"{pid[:8]}: removed {before - len(unresolved)} stale unresolved items")
                changed = True

            # 5. Nuclear clear: wipe all non-manual anime list_identity auto entries.
            #    Forces every anime to be re-resolved on the next sync, picking up
            #    any logic fixes since the entries were first cached.
            if clear_anime_auto:
                for ck in list(rc.keys()):
                    if ck not in mrc and _cache_key_is_anime_list(ck):
                        report.append(f"{pid[:8]}: [clear-anime-auto] rc[{ck!r}]={rc[ck]} — cleared")
                        rc.pop(ck, None)
                        frc.pop(ck, None)
                        changed = True
                        total_cleared += 1

            if changed:
                profile["resolution_cache"] = rc
                profile["failed_resolution_cache"] = frc
                profile["unresolved_items"] = unresolved

    if total_cleared or any("removed" in r for r in report):
        try:
            _profile_store._save_locked()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "report": report}), 500

    return jsonify({
        "ok": True,
        "total_cleared": total_cleared,
        "report": report,
        "message": f"Cleared {total_cleared} stale cache entries." if total_cleared else "Nothing to repair.",
    })


@app.route("/admin/api/refresh-anime-mappings", methods=["POST"])
def admin_refresh_anime_mappings():
    """Force an immediate re-check of Fribb and Anime-Lists upstream data.

    Resets the last-checked timestamps so the next lookup triggers a fresh
    ETag-conditional request.  If the upstream files changed, in-memory indexes
    are rebuilt; if not (304), the existing data is kept with no overhead.
    Returns the result: whether new data was downloaded or existing was current.
    """
    if not ADMIN_PASSWORD:
        return _json_error("Admin panel not configured", 404)
    if not _is_admin():
        return _json_error("Not authorized", 401)

    try:
        from src import anime_mapping_store as _ams  # noqa: PLC0415
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not import store: {exc}"}), 500

    try:
        refresh = _ams.force_refresh()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not refresh mappings: {exc}"}), 500

    results = []
    for item in refresh.get("results", []):
        source = str(item.get("source") or "mapping")
        label = "Fribb" if source == "fribb" else "Anime-Lists XML"
        duration = int(item.get("duration_ms") or 0)
        if item.get("ok"):
            results.append(f"{label}: checked in {duration}ms")
        else:
            results.append(f"{label}: ERROR - {item.get('error') or 'refresh failed'}")

    return jsonify({
        "ok": bool(refresh.get("ok")),
        "results": results,
        "metadata": refresh.get("metadata", {}),
        "message": " | ".join(results),
    })


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync dashboard web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
