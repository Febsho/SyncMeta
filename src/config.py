"""Configuration management for SyncMeta."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


SIMKL_LINKED_SELECTED_STATUSES = {
    "shows": ["watching", "plantowatch"],
    "movies": ["plantowatch"],
    "anime": ["watching", "plantowatch"],
}

SIMKL_DEFAULT_SELECTED_STATUSES = {
    "shows": [],
    "movies": [],
    "anime": [],
}

ANILIST_LINKED_SELECTED_STATUSES = ["CURRENT", "PLANNING"]
ANILIST_DEFAULT_SELECTED_STATUSES = []


@dataclass
class SimklConfig:
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    selected_statuses: dict[str, list[str]] = field(
        default_factory=lambda: {key: list(values) for key, values in SIMKL_DEFAULT_SELECTED_STATUSES.items()}
    )
    base_url: str = "https://api.simkl.com"


@dataclass
class AniListConfig:
    username: str = ""
    access_token: str = ""  # Optional, only needed for private lists
    # Only needed to obtain an access token through the Connect flow. Profiles
    # that paste a token directly, or only read a public list, never set these.
    client_id: str = ""
    client_secret: str = ""
    enabled: bool = False
    selected_statuses: list[str] = field(default_factory=lambda: list(ANILIST_DEFAULT_SELECTED_STATUSES))


@dataclass
class TraktConfig:
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    access_token_expires_at: str = ""
    username: str = ""
    enabled: bool = False
    sync_watchlist: bool = True
    sync_watchlist_movies: bool | None = None
    sync_watchlist_shows: bool | None = None
    sync_liked_lists: bool = True
    selected_lists: list[dict] = field(default_factory=list)
    base_url: str = "https://api.trakt.tv"

    def __post_init__(self) -> None:
        if self.sync_watchlist_movies is None:
            self.sync_watchlist_movies = self.sync_watchlist
        if self.sync_watchlist_shows is None:
            self.sync_watchlist_shows = self.sync_watchlist


@dataclass
class MdbListConfig:
    # An API key alone still works and is the simpler read-only path. OAuth is
    # what unlocks writing, so both auth modes are carried and the client
    # prefers the bearer token when one is present.
    api_key: str = ""
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    access_token_expires_at: str = ""
    enabled: bool = False
    selected_lists: list[dict] = field(default_factory=list)
    base_url: str = "https://api.mdblist.com"
    # OAuth authorization lives on the site, not the API host.
    site_url: str = "https://mdblist.com"


@dataclass
class PublicMetaDBConfig:
    api_key: str = ""
    base_url: str = "https://publicmetadb.com"


@dataclass
class SyncConfig:
    remove_missing: bool = False
    delete_disabled_lists: bool = False
    dry_run: bool = False
    interval_minutes: int = 0
    media_types: list[str] = field(default_factory=lambda: ["shows", "movies", "anime"])
    simkl_sync_watched_history: bool = False
    simkl_history_anime_only: bool = False
    simkl_reconcile_watched_history: bool = False
    anilist_sync_watched_history: bool = False
    simkl_sync_resume_progress: bool = False
    simkl_resume_use_next_up_fallback: bool = False
    trakt_sync_watched_history: bool = False
    simkl_history_cursor: str = ""
    trakt_history_cursor: str = ""
    simkl_activities_ts: str = ""   # last-seen SIMKL /sync/activities "all" timestamp
    trakt_activities_ts: str = ""   # last-seen Trakt /sync/last_activities "all" timestamp
    full_history_sync: bool = False  # True = ignore cursor, fetch everything
    trakt_watched_history_interval_seconds: int = 86400
    trakt_resume_progress_interval_seconds: int = 3600
    trakt_sync_full_watch_counts: bool = False
    trakt_reconcile_watched_history: bool = False
    trakt_sync_resume_progress: bool = False
    simkl_skip_watched_up_shows: bool = True  # Skip "watching" shows where all aired episodes are watched
    simkl_visibility: str = "private"
    anilist_visibility: str = "private"
    trakt_personal_visibility: str = "private"
    trakt_public_visibility: str = "public"
    mdblist_visibility: str = "public"
    simkl_sync_to_pmdb_watchlist: bool = False
    trakt_sync_to_pmdb_watchlist: bool = False
    anilist_sync_to_pmdb_watchlist: bool = False
    pmdb_watchlist_managed_keys: list[str] = field(default_factory=list)
    # Cross-service sync pairs (source -> target). Empty by default so existing
    # profiles are untouched until a pair is created.
    sync_pairs: list[dict] = field(default_factory=list)


@dataclass
class SyncPair:
    """A cross-service sync between two providers.

    One-way (the default) reads `source` and writes `target`. Two-way keeps both
    holding the union of the two — expressed as one pair rather than a pair in
    each direction, because a correct two-way sync has to decide *together*
    whether a one-sided item is new or deleted, which two independent pairs
    cannot do. `removal_mode` defaults to additive so a new pair can never
    delete anything.
    """

    pair_id: str = ""
    name: str = ""
    source: str = ""
    target: str = ""
    categories: list[str] = field(default_factory=list)
    # Specific named lists on the source to read. Empty means the provider's
    # default for each category (e.g. Trakt's own watchlist).
    source_lists: list[str] = field(default_factory=list)
    # Named list on the target to write into. Empty means the target's own
    # watchlist/collection for the category. Only meaningful for providers that
    # declare supports_target_lists.
    target_list: str = ""
    removal_mode: str = "additive"
    #: "one_way" (default) or "two_way". Two-way keeps both services holding the
    #: union of the two rather than running a second pair in reverse.
    mode: str = "one_way"
    enabled: bool = True
    #: Privacy of any list this pair *creates* on the target. It has no effect
    #: on a list that already exists — SyncMeta does not re-flag someone else's
    #: list because a pair happened to write to it. Providers that have no
    #: notion of list privacy ignore it.
    visibility: str = "private"
    # Every pair owns its schedule independently and can never run more often
    # than every 12 hours.
    auto_sync: bool = False
    interval_seconds: int = 43200

    def is_two_way(self) -> bool:
        return self.mode == "two_way"

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "categories": list(self.categories),
            "source_lists": list(self.source_lists),
            "target_list": self.target_list,
            "removal_mode": self.removal_mode,
            "mode": self.mode,
            "enabled": bool(self.enabled),
            "visibility": self.visibility,
            "auto_sync": bool(self.auto_sync),
            "interval_seconds": int(self.interval_seconds),
        }

    #: A pair id is an internal handle, not user-facing text: it is echoed into
    #: HTML attributes and used to key stored state. Restricting it to this
    #: alphabet keeps it inert wherever it is rendered, rather than relying on
    #: every call site escaping it correctly.
    _PAIR_ID_ALLOWED = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )

    @classmethod
    def _clean_pair_id(cls, value: object) -> str:
        text = str(value or "").strip()
        return "".join(ch for ch in text if ch in cls._PAIR_ID_ALLOWED)[:64]

    @classmethod
    def from_dict(cls, raw: dict) -> "SyncPair":
        from .providers import (
            ALL_CATEGORIES,
            ALL_VISIBILITIES,
            ALL_PAIR_MODES,
            ALL_REMOVAL_MODES,
            MODE_ONE_WAY,
            MODE_TWO_WAY,
            REMOVAL_ADDITIVE,
            REMOVAL_MANAGED,
            TWO_WAY_REMOVAL_MODES,
            VISIBILITY_PRIVATE,
        )

        if not isinstance(raw, dict):
            raise ValueError("Sync pair must be an object")
        source = str(raw.get("source", "") or "").strip().lower()
        target = str(raw.get("target", "") or "").strip().lower()
        if not source or not target:
            raise ValueError("Sync pair needs both a source and a target")
        if source == target:
            raise ValueError("A sync pair's source and target must differ")

        categories = [
            str(value).strip().lower()
            for value in (raw.get("categories") or [])
            if str(value).strip().lower() in ALL_CATEGORIES
        ]
        # Preserve canonical category order regardless of input order.
        categories = [c for c in ALL_CATEGORIES if c in categories]
        if not categories:
            raise ValueError("Sync pair needs at least one category")

        removal_mode = str(raw.get("removal_mode", "") or "").strip().lower()
        if removal_mode not in ALL_REMOVAL_MODES:
            # Unknown or absent modes fall back to the safe default rather than
            # inheriting a destructive one.
            removal_mode = REMOVAL_ADDITIVE

        mode = str(raw.get("mode", "") or "").strip().lower()
        if mode not in ALL_PAIR_MODES:
            mode = MODE_ONE_WAY
        if mode == MODE_TWO_WAY and removal_mode not in TWO_WAY_REMOVAL_MODES:
            # "Make the target match the source" has no bidirectional meaning —
            # run both ways it just means one side wins and the other's unique
            # items are destroyed. Downgrade to the managed equivalent rather
            # than dropping the pair, which would lose the user's config.
            logger.warning(
                "Sync pair %r is two-way; downgrading removal mode %r to %r",
                raw.get("name") or raw.get("pair_id") or f"{source}->{target}",
                removal_mode, REMOVAL_MANAGED,
            )
            removal_mode = REMOVAL_MANAGED

        source_lists = [
            str(value).strip()
            for value in (raw.get("source_lists") or [])
            if str(value).strip()
        ]

        visibility = str(raw.get("visibility", "") or "").strip().lower()
        if visibility not in ALL_VISIBILITIES:
            # Private is the safe default: a list nobody asked to publish must
            # not become public because the value was missing or misspelt.
            visibility = VISIBILITY_PRIVATE

        interval_raw = raw.get("interval_seconds", 43200)
        try:
            interval_seconds = int(interval_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Pair sync interval must be a whole number of seconds") from exc
        interval_seconds = max(43200, interval_seconds)

        return cls(
            pair_id=cls._clean_pair_id(raw.get("pair_id")),
            name=str(raw.get("name", "") or "").strip(),
            source=source,
            target=target,
            categories=categories,
            source_lists=source_lists,
            target_list=str(raw.get("target_list", "") or "").strip(),
            removal_mode=removal_mode,
            mode=mode,
            enabled=bool(raw.get("enabled", True)),
            visibility=visibility,
            auto_sync=bool(raw.get("auto_sync", False)),
            interval_seconds=interval_seconds,
        )

    def display_name(self) -> str:
        arrow = "↔" if self.is_two_way() else "→"
        return self.name or f"{self.source} {arrow} {self.target}"


@dataclass
class AppConfig:
    simkl: SimklConfig = field(default_factory=SimklConfig)
    anilist: AniListConfig = field(default_factory=AniListConfig)
    trakt: TraktConfig = field(default_factory=TraktConfig)
    mdblist: MdbListConfig = field(default_factory=MdbListConfig)
    pmdb: PublicMetaDBConfig = field(default_factory=PublicMetaDBConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    state_file: str = "sync_state.json"


def load_config(config_path: str | None = None) -> AppConfig:
    """Load configuration from environment variables, optionally overlaid with a JSON config file."""
    cfg = AppConfig()

    cfg.simkl.client_id = os.getenv("SIMKL_CLIENT_ID", "")
    cfg.simkl.client_secret = os.getenv("SIMKL_CLIENT_SECRET", "")
    cfg.simkl.access_token = os.getenv("SIMKL_ACCESS_TOKEN", "")

    cfg.anilist.username = os.getenv("ANILIST_USERNAME", "")
    cfg.anilist.access_token = os.getenv("ANILIST_ACCESS_TOKEN", "")

    cfg.trakt.client_id = os.getenv("TRAKT_CLIENT_ID", "")
    cfg.trakt.client_secret = os.getenv("TRAKT_CLIENT_SECRET", "")
    cfg.trakt.access_token = os.getenv("TRAKT_ACCESS_TOKEN", "")
    cfg.trakt.refresh_token = os.getenv("TRAKT_REFRESH_TOKEN", "")
    cfg.trakt.access_token_expires_at = os.getenv("TRAKT_ACCESS_TOKEN_EXPIRES_AT", "")
    cfg.trakt.username = os.getenv("TRAKT_USERNAME", "")

    cfg.mdblist.api_key = os.getenv("MDBLIST_API_KEY", "")

    trakt_enabled_env = os.getenv("TRAKT_ENABLED", "")
    if trakt_enabled_env:
        cfg.trakt.enabled = trakt_enabled_env.lower() == "true"
    else:
        cfg.trakt.enabled = bool(cfg.trakt.client_id and cfg.trakt.access_token)
    cfg.trakt.sync_watchlist = os.getenv("TRAKT_SYNC_WATCHLIST", "true").lower() == "true"
    cfg.trakt.sync_watchlist_movies = os.getenv("TRAKT_SYNC_WATCHLIST_MOVIES", str(cfg.trakt.sync_watchlist)).lower() == "true"
    cfg.trakt.sync_watchlist_shows = os.getenv("TRAKT_SYNC_WATCHLIST_SHOWS", str(cfg.trakt.sync_watchlist)).lower() == "true"
    cfg.trakt.sync_liked_lists = os.getenv("TRAKT_SYNC_LIKED_LISTS", "true").lower() == "true"

    anilist_enabled_env = os.getenv("ANILIST_ENABLED", "")
    if anilist_enabled_env:
        cfg.anilist.enabled = anilist_enabled_env.lower() == "true"
    else:
        cfg.anilist.enabled = bool(cfg.anilist.username)

    mdblist_enabled_env = os.getenv("MDBLIST_ENABLED", "")
    if mdblist_enabled_env:
        cfg.mdblist.enabled = mdblist_enabled_env.lower() == "true"
    else:
        cfg.mdblist.enabled = bool(cfg.mdblist.api_key)

    cfg.pmdb.api_key = os.getenv("PMDB_API_KEY", "")
    cfg.sync.remove_missing = os.getenv("SYNC_REMOVE_MISSING", "false").lower() == "true"
    cfg.sync.delete_disabled_lists = os.getenv("SYNC_DELETE_DISABLED_LISTS", "false").lower() == "true"
    cfg.sync.dry_run = os.getenv("SYNC_DRY_RUN", "false").lower() == "true"
    cfg.sync.simkl_sync_watched_history = os.getenv("SIMKL_SYNC_WATCHED_HISTORY", "false").lower() == "true"
    cfg.sync.simkl_history_anime_only = os.getenv("SIMKL_HISTORY_ANIME_ONLY", "false").lower() == "true"
    cfg.sync.simkl_sync_resume_progress = os.getenv("SIMKL_SYNC_RESUME_PROGRESS", "false").lower() == "true"
    cfg.sync.simkl_resume_use_next_up_fallback = os.getenv("SIMKL_RESUME_USE_NEXT_UP_FALLBACK", "false").lower() == "true"
    cfg.sync.trakt_sync_watched_history = os.getenv("TRAKT_SYNC_WATCHED_HISTORY", "false").lower() == "true"
    watched_interval = os.getenv("TRAKT_WATCHED_HISTORY_INTERVAL_SECONDS", "86400")
    cfg.sync.trakt_watched_history_interval_seconds = int(watched_interval) if watched_interval.isdigit() else 86400
    resume_interval = os.getenv("TRAKT_RESUME_PROGRESS_INTERVAL_SECONDS", "3600")
    cfg.sync.trakt_resume_progress_interval_seconds = int(resume_interval) if resume_interval.isdigit() else 3600
    cfg.sync.trakt_sync_full_watch_counts = os.getenv("TRAKT_SYNC_FULL_WATCH_COUNTS", "false").lower() == "true"
    cfg.sync.trakt_reconcile_watched_history = os.getenv("TRAKT_RECONCILE_WATCHED_HISTORY", "false").lower() == "true"
    cfg.sync.trakt_sync_resume_progress = os.getenv("TRAKT_SYNC_RESUME_PROGRESS", "false").lower() == "true"

    interval = os.getenv("SYNC_INTERVAL_MINUTES", "0")
    cfg.sync.interval_minutes = int(interval) if interval.isdigit() else 0

    media_types_env = os.getenv("SYNC_MEDIA_TYPES", "")
    if media_types_env:
        cfg.sync.media_types = [t.strip() for t in media_types_env.split(",") if t.strip()]

    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path, encoding="utf-8") as file_handle:
                data = json.load(file_handle)
            _apply_config_file(cfg, data)

    return cfg


def _apply_config_file(cfg: AppConfig, data: dict) -> None:
    """Apply config file values only where env vars are not set."""
    simkl = data.get("simkl", {})
    if not cfg.simkl.client_id:
        cfg.simkl.client_id = simkl.get("client_id", "")
    if not cfg.simkl.client_secret:
        cfg.simkl.client_secret = simkl.get("client_secret", "")
    if not cfg.simkl.access_token:
        cfg.simkl.access_token = simkl.get("access_token", "")
    cfg.simkl.selected_statuses = simkl.get("selected_statuses", cfg.simkl.selected_statuses)

    anilist = data.get("anilist", {})
    if not cfg.anilist.username:
        cfg.anilist.username = anilist.get("username", "")
    if not cfg.anilist.access_token:
        cfg.anilist.access_token = anilist.get("access_token", "")
    cfg.anilist.selected_statuses = anilist.get("selected_statuses", cfg.anilist.selected_statuses)
    if not os.getenv("ANILIST_ENABLED"):
        if "enabled" in anilist:
            cfg.anilist.enabled = anilist["enabled"]
        else:
            cfg.anilist.enabled = bool(cfg.anilist.username)

    trakt = data.get("trakt", {})
    if not cfg.trakt.client_id:
        cfg.trakt.client_id = trakt.get("client_id", "")
    if not cfg.trakt.client_secret:
        cfg.trakt.client_secret = trakt.get("client_secret", "")
    if not cfg.trakt.access_token:
        cfg.trakt.access_token = trakt.get("access_token", "")
    if not cfg.trakt.refresh_token:
        cfg.trakt.refresh_token = trakt.get("refresh_token", "")
    if not cfg.trakt.access_token_expires_at:
        cfg.trakt.access_token_expires_at = trakt.get("access_token_expires_at", "")
    if not cfg.trakt.username:
        cfg.trakt.username = trakt.get("username", "")
    if not os.getenv("TRAKT_ENABLED"):
        cfg.trakt.enabled = trakt.get("enabled", bool(cfg.trakt.client_id and cfg.trakt.access_token))
    if not os.getenv("TRAKT_SYNC_WATCHLIST"):
        cfg.trakt.sync_watchlist = trakt.get("sync_watchlist", True)
    if not os.getenv("TRAKT_SYNC_WATCHLIST_MOVIES"):
        cfg.trakt.sync_watchlist_movies = trakt.get("sync_watchlist_movies", cfg.trakt.sync_watchlist)
    if not os.getenv("TRAKT_SYNC_WATCHLIST_SHOWS"):
        cfg.trakt.sync_watchlist_shows = trakt.get("sync_watchlist_shows", cfg.trakt.sync_watchlist)
    if not os.getenv("TRAKT_SYNC_LIKED_LISTS"):
        cfg.trakt.sync_liked_lists = trakt.get("sync_liked_lists", True)
    cfg.trakt.selected_lists = trakt.get("selected_lists", [])

    mdblist = data.get("mdblist", {})
    if not cfg.mdblist.api_key:
        cfg.mdblist.api_key = mdblist.get("api_key", "")
    cfg.mdblist.selected_lists = mdblist.get("selected_lists", [])
    if not os.getenv("MDBLIST_ENABLED"):
        cfg.mdblist.enabled = mdblist.get("enabled", bool(cfg.mdblist.api_key and cfg.mdblist.selected_lists))

    pmdb = data.get("pmdb", {})
    if not cfg.pmdb.api_key:
        cfg.pmdb.api_key = pmdb.get("api_key", "")

    sync = data.get("sync", {})
    if "remove_missing" in sync and not os.getenv("SYNC_REMOVE_MISSING"):
        cfg.sync.remove_missing = sync["remove_missing"]
    if "delete_disabled_lists" in sync and not os.getenv("SYNC_DELETE_DISABLED_LISTS"):
        cfg.sync.delete_disabled_lists = sync["delete_disabled_lists"]
    if "dry_run" in sync and not os.getenv("SYNC_DRY_RUN"):
        cfg.sync.dry_run = sync["dry_run"]
    if "simkl_sync_watched_history" in sync and not os.getenv("SIMKL_SYNC_WATCHED_HISTORY"):
        cfg.sync.simkl_sync_watched_history = bool(sync["simkl_sync_watched_history"])
    if "simkl_history_anime_only" in sync and not os.getenv("SIMKL_HISTORY_ANIME_ONLY"):
        cfg.sync.simkl_history_anime_only = bool(sync["simkl_history_anime_only"])
    if "simkl_sync_resume_progress" in sync and not os.getenv("SIMKL_SYNC_RESUME_PROGRESS"):
        cfg.sync.simkl_sync_resume_progress = bool(sync["simkl_sync_resume_progress"])
    if "simkl_resume_use_next_up_fallback" in sync and not os.getenv("SIMKL_RESUME_USE_NEXT_UP_FALLBACK"):
        cfg.sync.simkl_resume_use_next_up_fallback = bool(sync["simkl_resume_use_next_up_fallback"])
    if "trakt_sync_watched_history" in sync and not os.getenv("TRAKT_SYNC_WATCHED_HISTORY"):
        cfg.sync.trakt_sync_watched_history = bool(sync["trakt_sync_watched_history"])
    if "trakt_watched_history_interval_seconds" in sync and not os.getenv("TRAKT_WATCHED_HISTORY_INTERVAL_SECONDS"):
        cfg.sync.trakt_watched_history_interval_seconds = int(sync["trakt_watched_history_interval_seconds"])
    if "trakt_resume_progress_interval_seconds" in sync and not os.getenv("TRAKT_RESUME_PROGRESS_INTERVAL_SECONDS"):
        cfg.sync.trakt_resume_progress_interval_seconds = int(sync["trakt_resume_progress_interval_seconds"])
    if "trakt_sync_full_watch_counts" in sync and not os.getenv("TRAKT_SYNC_FULL_WATCH_COUNTS"):
        cfg.sync.trakt_sync_full_watch_counts = bool(sync["trakt_sync_full_watch_counts"])
    if "trakt_reconcile_watched_history" in sync and not os.getenv("TRAKT_RECONCILE_WATCHED_HISTORY"):
        cfg.sync.trakt_reconcile_watched_history = bool(sync["trakt_reconcile_watched_history"])
    if "trakt_sync_resume_progress" in sync and not os.getenv("TRAKT_SYNC_RESUME_PROGRESS"):
        cfg.sync.trakt_sync_resume_progress = bool(sync["trakt_sync_resume_progress"])
    if "interval_minutes" in sync and not os.getenv("SYNC_INTERVAL_MINUTES"):
        cfg.sync.interval_minutes = sync["interval_minutes"]
    if "media_types" in sync and not os.getenv("SYNC_MEDIA_TYPES"):
        cfg.sync.media_types = sync["media_types"]
    if "simkl_skip_watched_up_shows" in sync:
        cfg.sync.simkl_skip_watched_up_shows = bool(sync["simkl_skip_watched_up_shows"])
    if "simkl_visibility" in sync:
        cfg.sync.simkl_visibility = sync["simkl_visibility"]
    if "anilist_visibility" in sync:
        cfg.sync.anilist_visibility = sync["anilist_visibility"]
    if "trakt_personal_visibility" in sync:
        cfg.sync.trakt_personal_visibility = sync["trakt_personal_visibility"]
    if "trakt_public_visibility" in sync:
        cfg.sync.trakt_public_visibility = sync["trakt_public_visibility"]
    if "mdblist_visibility" in sync:
        cfg.sync.mdblist_visibility = sync["mdblist_visibility"]
    if "state_file" in data:
        cfg.state_file = data["state_file"]


def validate_config(cfg: AppConfig, sources: list[str] | None = None) -> list[str]:
    """Return a list of configuration errors (empty if valid)."""
    errors = []
    check_simkl = sources is None or "simkl" in sources
    check_anilist = sources is None or "anilist" in sources
    check_trakt = sources is None or "trakt" in sources
    check_mdblist = sources is None or "mdblist" in sources

    if check_simkl:
        if not cfg.simkl.client_id:
            errors.append("SIMKL_CLIENT_ID is required")
        if not cfg.simkl.access_token:
            errors.append("SIMKL_ACCESS_TOKEN is required (use the web UI PIN auth flow to authenticate)")
        if (
            not any(cfg.simkl.selected_statuses.get(media_type) for media_type in ["shows", "movies", "anime"])
            and not cfg.sync.simkl_sync_watched_history
            and not cfg.sync.simkl_sync_resume_progress
        ):
            errors.append("Select at least one SIMKL status to sync")

    if check_anilist and cfg.anilist.enabled:
        if not cfg.anilist.username:
            errors.append("ANILIST_USERNAME is required when AniList is enabled")
        if not cfg.anilist.selected_statuses:
            errors.append("Select at least one AniList status to sync")

    if check_trakt and cfg.trakt.enabled:
        if not cfg.trakt.client_id:
            errors.append("TRAKT_CLIENT_ID is required when Trakt is enabled")
        if not cfg.trakt.client_secret:
            errors.append("TRAKT_CLIENT_SECRET is required when Trakt is enabled")
        if not cfg.trakt.access_token:
            errors.append("TRAKT_ACCESS_TOKEN is required when Trakt is enabled")
        if (
            not cfg.trakt.sync_watchlist_movies
            and not cfg.trakt.sync_watchlist_shows
            and not cfg.trakt.sync_liked_lists
            and not cfg.trakt.selected_lists
            and not cfg.sync.trakt_sync_watched_history
            and not cfg.sync.trakt_sync_resume_progress
        ):
            errors.append("Enable at least one Trakt source: watchlist movies, watchlist shows, liked lists, selected lists, watched history, or resume progress")

    if check_mdblist and cfg.mdblist.enabled:
        if not cfg.mdblist.api_key and not cfg.mdblist.access_token:
            errors.append("An MDBList API key or OAuth connection is required when MDBList is enabled")
        if not cfg.mdblist.selected_lists:
            errors.append("Select at least one MDBList list to sync")

    if not cfg.pmdb.api_key:
        errors.append("PMDB_API_KEY is required")
    return errors
