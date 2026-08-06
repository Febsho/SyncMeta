"""Admin-editable runtime settings.

Every tunable in this app is an environment variable read once at import time.
That is fine for a self-hoster who owns the shell, and useless for one who does
not: on Docker the ``.env`` file lives on the *host*, the container gets its
values through ``docker compose``, and ``load_dotenv`` deliberately does not
override a variable that is already set — so an in-container write to ``.env``
would be both lost on the next ``up`` and ignored while the container ran.

So overrides are not written to ``.env``. They go to a small JSON file beside
``profiles.json``, which is the one path guaranteed to be on the mounted volume,
and :func:`apply_overrides` pushes them into ``os.environ`` at the very top of
``web.py`` — before anything imports the modules that read them. Precedence is
therefore:

    built-in default  <  process environment (.env / compose)  <  admin override

which is the order the admin panel shows, so a value that looks wrong can be
traced to the layer that set it.

Only keys in :data:`SETTINGS` may be written. This is a deliberate allow-list
rather than a free-form editor: ``SYNCMETA_MASTER_KEY`` decrypts every stored
credential, ``PROFILE_STORE_FILE`` decides which file is the database, and a
typo in either is unrecoverable data loss rather than a bad setting.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    help: str
    kind: str = "int"          # int | bool | secret
    default: str = ""
    minimum: int | None = None
    maximum: int | None = None
    #: True when the running process cannot pick the new value up, because it
    #: was captured into a module constant or a thread pool at import time.
    restart_required: bool = True
    #: Never send the value to the browser; report only whether it is set.
    secret: bool = False


GROUPS: tuple[tuple[str, str], ...] = (
    ("access", "Access & security"),
    ("scheduler", "Scheduler"),
    ("concurrency", "Sync concurrency"),
    ("network", "Network timeouts"),
    ("logging", "Logging"),
)


SETTINGS: tuple[Setting, ...] = (
    # ── access ───────────────────────────────────────────────────────────────
    Setting(
        "ADMIN_PASSWORD", "Admin password", "access",
        "Password for this panel. Clearing it would lock the panel out entirely, "
        "so it cannot be set empty here.",
        kind="secret", secret=True, restart_required=False,
    ),
    Setting(
        "SITE_ACCESS_PASSWORD", "Site access password", "access",
        "Optional gate in front of the whole app. Leave empty for no gate.",
        kind="secret", secret=True, restart_required=False,
    ),
    Setting(
        "SYNCMETA_SESSION_TTL_SECONDS", "Session lifetime", "access",
        "How long a signed-in browser stays signed in, in seconds.",
        default="2592000", minimum=300, maximum=31536000,
    ),
    Setting(
        "SYNCMETA_LOGIN_MAX_ATTEMPTS", "Login attempts allowed", "access",
        "Failed sign-ins from one client before it is throttled.",
        default="10", minimum=1, maximum=1000,
    ),
    Setting(
        "SYNCMETA_LOGIN_WINDOW_SECONDS", "Login throttle window", "access",
        "Seconds the login attempt counter looks back over.",
        default="900", minimum=30, maximum=86400,
    ),

    # ── scheduler ────────────────────────────────────────────────────────────
    Setting(
        "DISABLE_PROFILE_SCHEDULER", "Disable the scheduler", "scheduler",
        "Stops all automatic syncing. Manual runs still work.",
        kind="bool", default="0", restart_required=False,
    ),
    Setting(
        "SYNCMETA_SCHEDULER_POLL_SECONDS", "Poll interval", "scheduler",
        "How often the scheduler looks for profiles that are due.",
        default="5", minimum=5, maximum=3600,
    ),
    Setting(
        "SYNCMETA_SCHEDULER_STARTUP_GRACE_SECONDS", "Startup grace", "scheduler",
        "Head start for the web tier after a restart, before the scheduler claims "
        "anything. Too low and the request that boots the app queues behind the "
        "stampede of overdue syncs it just triggered.",
        default="20", minimum=0, maximum=600,
    ),
    Setting(
        "SYNCMETA_SCHEDULER_CLAIM_BATCH", "Claims per poll", "scheduler",
        "How many due profiles one poll may start. Claiming past the runner's "
        "capacity only fills its queue while the dashboard says they are running.",
        default="1", minimum=1, maximum=50, restart_required=False,
    ),
    Setting(
        "SYNCMETA_SCHEDULE_JITTER_SECONDS", "Schedule jitter", "scheduler",
        "Random spread applied to each profile's next run so they do not all fire "
        "at the same second.",
        default="900", minimum=0, maximum=21600,
    ),

    # ── concurrency ──────────────────────────────────────────────────────────
    Setting(
        "SYNCMETA_MAX_CONCURRENT_SYNCS", "Concurrent profile syncs", "concurrency",
        "Profiles that may sync at once. The scheduler and the runner share the "
        "web process, so raising this competes with serving the UI.",
        default="1", minimum=1, maximum=8,
    ),
    Setting(
        "SYNCMETA_SOURCE_SYNC_WORKERS", "Source fetch workers", "concurrency",
        "Services fetched in parallel within one sync.",
        default="3", minimum=1, maximum=4,
    ),
    Setting(
        "SYNCMETA_LIST_RESOLVE_WORKERS", "List resolve workers", "concurrency",
        "Parallel ID resolutions per list.",
        default="2", minimum=1, maximum=6,
    ),
    Setting(
        "SYNCMETA_LIST_WRITE_WORKERS", "List write workers", "concurrency",
        "Parallel writes into PublicMetaDB per list.",
        default="2", minimum=1, maximum=4,
    ),
    Setting(
        "SYNCMETA_ACTIVITY_WRITE_WORKERS", "Activity write workers", "concurrency",
        "Parallel writes for watch history and resume progress.",
        default="1", minimum=1, maximum=4,
    ),
    Setting(
        "SYNCMETA_SIMKL_FETCH_WORKERS", "SIMKL fetch workers", "concurrency",
        "SIMKL type/status combinations fetched in parallel.",
        default="3", minimum=1, maximum=8,
    ),
    Setting(
        "SYNCMETA_ANILIST_PREWARM_LIMIT", "AniList prewarm limit", "concurrency",
        "Anime titles pre-resolved before a sync starts. 0 disables prewarming.",
        default="100", minimum=0, maximum=200,
    ),

    # ── network ──────────────────────────────────────────────────────────────
    Setting(
        "SYNCMETA_TRAKT_READ_TIMEOUT", "Trakt read timeout", "network",
        "Seconds to wait for Trakt to answer. A large watchlist needs more than a "
        "few seconds; too low turns every fetch into a retry storm.",
        default="20", minimum=2, maximum=180,
    ),
    Setting(
        "SYNCMETA_SIMKL_READ_TIMEOUT", "SIMKL read timeout", "network",
        "Seconds to wait for SIMKL to answer.",
        default="20", minimum=2, maximum=180,
    ),
    Setting(
        "SYNCMETA_MDBLIST_READ_TIMEOUT", "MDBList read timeout", "network",
        "Seconds to wait for MDBList to answer.",
        default="20", minimum=2, maximum=180,
    ),
    Setting(
        "SYNCMETA_ANILIST_READ_TIMEOUT", "AniList read timeout", "network",
        "Seconds to wait for AniList to answer.",
        default="20", minimum=2, maximum=180,
    ),
    Setting(
        "SYNCMETA_PMDB_READ_TIMEOUT", "PublicMetaDB read timeout", "network",
        "Seconds to wait for PublicMetaDB to answer.",
        default="20", minimum=2, maximum=180,
    ),

    # ── logging ──────────────────────────────────────────────────────────────
    Setting(
        "SYNCMETA_PROFILE_LOG_LIMIT", "Log lines kept per profile", "logging",
        "Ring buffer size for the in-app log view.",
        default="500", minimum=100, maximum=20000,
    ),
)

SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

#: Editing any of these from a web panel is a way to lose data, not a setting.
#: They are listed so the panel can *show* them as locked rather than silently
#: pretending they do not exist.
LOCKED_KEYS: tuple[tuple[str, str], ...] = (
    ("SYNCMETA_MASTER_KEY", "Decrypts every stored credential — a wrong value makes them unreadable."),
    ("SYNCMETA_MASTER_KEY_FILE", "Points at the encryption key file."),
    ("SYNCMETA_SESSION_SECRET", "Signs session cookies; changing it signs everyone out."),
    ("PROFILE_STORE_FILE", "Which file is the database."),
)

DEFAULT_SETTINGS_FILENAME = "settings.json"

#: Variables SyncMeta really does read, but which are not panel-editable and are
#: not dangerous either — mostly consumed by the container entrypoint. Listing
#: them keeps them out of the "not recognised" warning.
_READ_ELSEWHERE = frozenset({
    "SYNCMETA_GUNICORN_WORKERS",
    "SYNCMETA_GUNICORN_THREADS",
    "SYNCMETA_GUNICORN_TIMEOUT",
    "SYNCMETA_CPU_LIMIT",
    "SYNCMETA_MEMORY_LIMIT",
    "SYNCMETA_MEMORY_RESERVATION",
    "SYNCMETA_LIST_SYNC_JITTER_SECONDS",
    "SYNCMETA_HISTORY_SYNC_JITTER_SECONDS",
    "SYNCMETA_RESUME_SYNC_JITTER_SECONDS",
    "SYNCMETA_PAIR_SYNC_JITTER_SECONDS",
    "SYNCMETA_ACCESS_MAX_ATTEMPTS",
    "SYNCMETA_ACCESS_WINDOW_SECONDS",
    "SYNCMETA_MAPPING_WRITE_WORKERS",
    "SYNCMETA_ACTIVITY_SOURCE_WORKERS",
    "SYNCMETA_PREWARM_WORKERS",
    "ANILIST_ROOT_CACHE_FILE",
})

_OUR_PREFIXES = ("SYNCMETA_", "ADMIN_", "SITE_ACCESS_", "PROFILE_STORE", "DISABLE_PROFILE")

#: Names people reach for that SyncMeta does not read. Worth calling out: a
#: self-hoster who set ENCRYPTION_KEY expecting it to encrypt the profile store
#: has no encryption key set at all, and no way to find that out.
_NEAR_MISSES = frozenset({
    "ENCRYPTION_KEY",
    "SECRET_KEY",
    "MASTER_KEY",
    "FLASK_SECRET_KEY",
    "SESSION_SECRET",
    "ADMIN_PASS",
    "ADMIN_TOKEN",
})


class SettingsError(ValueError):
    """A rejected value, with a message meant for the admin panel."""


def coerce(setting: Setting, raw: object) -> str:
    """Validate ``raw`` for ``setting`` and return it as an env-string.

    Raises :class:`SettingsError` with a message the panel can show verbatim.
    """
    if setting.kind == "bool":
        if isinstance(raw, bool):
            return "1" if raw else "0"
        text = str(raw or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return "1"
        if text in {"", "0", "false", "no", "off"}:
            return "0"
        raise SettingsError(f"{setting.label}: expected a yes/no value.")

    if setting.kind == "secret":
        text = str(raw or "")
        # A password with leading/trailing spaces is almost always a paste
        # accident, and the reader strips them anyway.
        return text.strip()

    text = str(raw if raw is not None else "").strip()
    if text == "":
        raise SettingsError(f"{setting.label}: cannot be empty.")
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        raise SettingsError(f"{setting.label}: must be a whole number.") from None
    if setting.minimum is not None and value < setting.minimum:
        raise SettingsError(f"{setting.label}: minimum is {setting.minimum}.")
    if setting.maximum is not None and value > setting.maximum:
        raise SettingsError(f"{setting.label}: maximum is {setting.maximum}.")
    return str(value)


class SettingsStore:
    """The admin override layer, persisted as JSON next to ``profiles.json``."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._overrides: dict[str, str] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("Could not read %s (%s); ignoring overrides", self._path, exc)
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            setting = SETTINGS_BY_KEY.get(str(key))
            if setting is None:
                # A key we no longer recognise: drop it rather than pushing an
                # unknown name into the environment.
                logger.info("Dropping unknown stored setting %r", key)
                continue
            try:
                self._overrides[setting.key] = coerce(setting, value)
            except SettingsError as exc:
                logger.warning("Stored setting %s is invalid (%s); ignoring", key, exc)

    def overrides(self) -> dict[str, str]:
        with self._lock:
            return dict(self._overrides)

    def set_many(self, values: dict[str, object]) -> dict[str, str]:
        """Validate and persist ``values``. Returns the keys actually changed.

        Validation happens for the whole batch before anything is written, so a
        single bad field cannot leave half a form applied.
        """
        cleaned: dict[str, str | None] = {}
        for key, raw in values.items():
            setting = SETTINGS_BY_KEY.get(str(key))
            if setting is None:
                raise SettingsError(f"{key} is not an editable setting.")
            if setting.kind == "secret":
                text = str(raw or "").strip()
                if text == "":
                    # An empty secret field means "leave it alone" — the panel
                    # never shows the current value, so blank cannot mean clear.
                    continue
                if text == _CLEAR_SENTINEL:
                    if setting.key == "ADMIN_PASSWORD":
                        raise SettingsError(
                            "Admin password cannot be cleared here — it would lock this panel."
                        )
                    cleaned[setting.key] = None
                    continue
            cleaned[setting.key] = coerce(setting, raw)

        changed: dict[str, str] = {}
        with self._lock:
            for key, value in cleaned.items():
                if value is None:
                    if self._overrides.pop(key, None) is not None:
                        changed[key] = ""
                    continue
                if self._overrides.get(key) != value:
                    self._overrides[key] = value
                    changed[key] = value
            if changed:
                self._save_locked()
        return changed

    def reset(self, key: str) -> bool:
        """Drop one override so the environment/default shows through again."""
        if key not in SETTINGS_BY_KEY:
            raise SettingsError(f"{key} is not an editable setting.")
        with self._lock:
            if self._overrides.pop(key, None) is None:
                return False
            self._save_locked()
        return True

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a half-written settings file would be read at the next
        # boot, which is exactly when it cannot be fixed through the UI.
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._overrides, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, self._path)
            try:
                os.chmod(self._path, 0o600)  # it can hold passwords
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


#: What the panel sends to clear a secret (blank means "unchanged").
_CLEAR_SENTINEL = "__clear__"
CLEAR_SENTINEL = _CLEAR_SENTINEL


def apply_overrides(store: SettingsStore) -> dict[str, str]:
    """Push stored overrides into ``os.environ``.

    Must run before any module that reads these variables is imported, which in
    practice means the top of ``web.py``.
    """
    applied = store.overrides()
    for key, value in applied.items():
        os.environ[key] = value
    return applied


def describe(store: SettingsStore, base_env: dict[str, str] | None = None) -> dict:
    """Everything the admin panel needs to render the settings form.

    ``base_env`` is the process environment as it was *before* overrides were
    applied — the panel uses it to say where a value came from, which is the
    only way to explain why a compose variable appears to be ignored.
    """
    base = base_env if base_env is not None else {}
    overrides = store.overrides()
    rows = []
    for setting in SETTINGS:
        override = overrides.get(setting.key)
        from_env = str(base.get(setting.key, "") or "").strip()
        if override is not None:
            source, effective = "override", override
        elif from_env:
            source, effective = "environment", from_env
        else:
            source, effective = "default", setting.default
        row = {
            "key": setting.key,
            "label": setting.label,
            "group": setting.group,
            "help": setting.help,
            "kind": setting.kind,
            "default": setting.default,
            "minimum": setting.minimum,
            "maximum": setting.maximum,
            "restart_required": setting.restart_required,
            "secret": setting.secret,
            "source": source,
            "overridden": override is not None,
            "env_value_present": bool(from_env),
        }
        if setting.secret:
            row["value"] = ""
            row["is_set"] = bool(effective)
        else:
            row["value"] = effective
        rows.append(row)

    known = set(SETTINGS_BY_KEY) | {k for k, _ in LOCKED_KEYS} | _READ_ELSEWHERE
    unknown = sorted(
        k for k in base
        if (k.startswith(_OUR_PREFIXES) or k in _NEAR_MISSES) and k not in known
    )
    return {
        "groups": [{"id": gid, "label": label} for gid, label in GROUPS],
        "settings": rows,
        "locked": [{"key": k, "reason": reason, "is_set": bool(base.get(k))} for k, reason in LOCKED_KEYS],
        "unrecognised": unknown,
        "path": str(store.path),
        "clear_sentinel": CLEAR_SENTINEL,
    }
