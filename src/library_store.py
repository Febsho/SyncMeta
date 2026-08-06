"""The local Library: SyncMeta's own copy of what you watch.

Until now every sync had to name two remote services. That makes SyncMeta a
pipe rather than a place, and it means there is nowhere to answer "what do I
actually have" without asking somebody else's API. The Library is a local store
that behaves like one more provider: services sync *into* it, and it syncs
*out* to anywhere else.

Two decisions shape the whole file.

**One entry per series, seasons inside it.** This is the TVDB/Trakt shape, and
it is the only shape in which SIMKL and AniList can agree. SIMKL lists an anime
per season; AniList lists it per cour, often with a different id for each. Keyed
naively that is three rows for one show, and no amount of deduping at display
time fixes it, because the rows carry different ids. So an item's identity is
its *series*, and seasons are a field. ``ItemMatcher`` already resolves an anime
season back to its root series — this is the storage shape that makes that
resolution worth doing.

**Watched state is per episode, stored sparsely.** A show is not "watched"; some
of its episodes are. Storing a set of ``(season, episode)`` pairs is what lets
the Library answer which episodes are watched without inventing a completion
percentage from a count, which is what made SIMKL's aggregate counts unusable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from .media_kind import KIND_ANIME, KIND_ANIME_MOVIE, classify, normalize_namespace

logger = logging.getLogger(__name__)

#: Categories the Library holds, mirroring the pair categories.
SECTION_WATCHLIST = "watchlist"
SECTION_COLLECTION = "collection"
ALL_SECTIONS = (SECTION_WATCHLIST, SECTION_COLLECTION)


def series_key(item: dict) -> str:
    """Identity of an item's *series*, not of one season of it.

    TMDB ids are preferred because every provider can be mapped onto them. An
    anime-native id is the fallback, since an AniList entry that has not been
    mapped yet still has to be storable — but it is deliberately last, or two
    seasons of the same show would key differently and the whole point of the
    series shape would be lost.
    """
    if not isinstance(item, dict):
        return ""
    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    space = normalize_namespace(item.get("media_type"))
    tmdb = str(item.get("tmdb_id") or ids.get("tmdb") or "").strip()
    if tmdb.isdigit():
        return f"tmdb:{space}:{tmdb}"
    imdb = str(item.get("imdb_id") or ids.get("imdb") or "").strip()
    if imdb:
        return f"imdb:{imdb}"
    for field, prefix in (
        ("anilist_id", "anilist"), ("mal_id", "mal"), ("anidb_id", "anidb"),
    ):
        value = str(item.get(field) or ids.get(prefix) or "").strip()
        if value:
            return f"{prefix}:{value}"
    title = str(item.get("title") or "").strip().lower()
    year = str(item.get("year") or "").strip()
    return f"title:{space}:{title}:{year}" if title else ""


def _episode_key(season: object, episode: object) -> str | None:
    try:
        season_number = int(season)
        episode_number = int(episode)
    except (TypeError, ValueError):
        return None
    if episode_number <= 0:
        return None
    # Season 0 is specials everywhere; it is kept, not normalised away.
    return f"{season_number}x{episode_number}"


class LibraryStore:
    """Per-profile local library, persisted as one JSON file.

    Deliberately the same storage idiom as ``ProfileStore``: a small JSON file
    on the mounted volume, written atomically, guarded by one lock. A database
    would be a better fit at ten times this size and a worse fit for a
    self-hosted app that must survive being copied around as a directory.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("Could not read library %s (%s); starting empty", self._path, exc)
            return
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, dict):
            return
        for key, entry in items.items():
            if isinstance(entry, dict):
                self._items[str(key)] = entry

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=".library-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "items": self._items}, handle)
            os.replace(tmp_name, self._path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    # ── writing ────────────────────────────────────────────────────────────

    def _blank(self, key: str, item: dict) -> dict:
        return {
            "key": key,
            "title": str(item.get("title") or "").strip(),
            "year": item.get("year"),
            "media_type": normalize_namespace(item.get("media_type")),
            "kind": classify(item),
            "tmdb_id": item.get("tmdb_id") or (item.get("ids") or {}).get("tmdb"),
            "imdb_id": item.get("imdb_id") or (item.get("ids") or {}).get("imdb"),
            "anilist_id": item.get("anilist_id") or (item.get("ids") or {}).get("anilist"),
            "mal_id": item.get("mal_id") or (item.get("ids") or {}).get("mal"),
            "anidb_id": item.get("anidb_id") or (item.get("ids") or {}).get("anidb"),
            "sections": {},
            "seasons": {},
            "watched": {},
            "sources": [],
            "added_at": time.time(),
            "updated_at": time.time(),
        }

    def _merge_identity(self, entry: dict, item: dict) -> None:
        """Fold a second provider's view of the same series into one entry.

        Every field is fill-only: a later source may *add* an id or a title the
        first one lacked, but must never overwrite one that is already there.
        Otherwise the last service to sync would decide the title, and the entry
        would flip between SIMKL's romaji and AniList's English on every run.
        """
        ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
        for field, alias in (
            ("tmdb_id", "tmdb"), ("imdb_id", "imdb"), ("anilist_id", "anilist"),
            ("mal_id", "mal"), ("anidb_id", "anidb"),
        ):
            if not entry.get(field):
                value = item.get(field) or ids.get(alias)
                if value:
                    entry[field] = value
        if not entry.get("title"):
            entry["title"] = str(item.get("title") or "").strip()
        if not entry.get("year") and item.get("year"):
            entry["year"] = item.get("year")
        # Anime-ness is sticky: one source knowing it is anime is enough, and a
        # source that does not model anime must not downgrade it back.
        if entry.get("kind") not in (KIND_ANIME, KIND_ANIME_MOVIE):
            entry["kind"] = classify({**item, "media_type": entry.get("media_type")})
        season = item.get("season")
        if season is not None:
            try:
                number = int(season)
            except (TypeError, ValueError):
                number = None
            if number is not None:
                seasons = entry.setdefault("seasons", {})
                record = seasons.setdefault(str(number), {})
                for field in ("title", "anilist_id", "mal_id", "anidb_id"):
                    source_field = "title" if field == "title" else field
                    value = item.get(f"season_{field}") or (item.get(source_field) if field != "title" else None)
                    if value and not record.get(field):
                        record[field] = value
        entry["updated_at"] = time.time()

    def add(self, section: str, items: list[dict], source: str = "") -> dict:
        """Put ``items`` into ``section``. Returns add/skip counts."""
        section = str(section or "").strip().lower()
        if section not in ALL_SECTIONS:
            return {"added": 0, "not_found": len(items or []), "batches": 0}
        added = 0
        skipped = 0
        with self._lock:
            for item in items or []:
                key = series_key(item)
                if not key:
                    skipped += 1
                    continue
                entry = self._items.get(key)
                if entry is None:
                    entry = self._blank(key, item)
                    self._items[key] = entry
                self._merge_identity(entry, item)
                sections = entry.setdefault("sections", {})
                if section not in sections:
                    sections[section] = time.time()
                    added += 1
                if source and source not in entry.setdefault("sources", []):
                    entry["sources"].append(source)
            if added or skipped:
                self._save_locked()
        return {"added": added, "not_found": skipped, "batches": 1 if items else 0}

    def remove(self, section: str, items: list[dict]) -> dict:
        section = str(section or "").strip().lower()
        deleted = 0
        with self._lock:
            for item in items or []:
                key = series_key(item)
                entry = self._items.get(key)
                if not entry:
                    continue
                if entry.get("sections", {}).pop(section, None) is not None:
                    deleted += 1
                # An entry with no sections and no watch history is not "in" the
                # library any more; keeping it would make removals invisible.
                if not entry.get("sections") and not entry.get("watched"):
                    self._items.pop(key, None)
            if deleted:
                self._save_locked()
        return {"deleted": deleted}

    def mark_watched(self, items: list[dict], source: str = "") -> dict:
        """Record plays. A movie is stored as ``0x0`` so it has one slot."""
        added = 0
        skipped = 0
        with self._lock:
            for item in items or []:
                key = series_key(item)
                if not key:
                    skipped += 1
                    continue
                entry = self._items.get(key)
                if entry is None:
                    entry = self._blank(key, item)
                    self._items[key] = entry
                self._merge_identity(entry, item)
                if entry.get("media_type") == "movie":
                    slot = "0x0"
                else:
                    slot = _episode_key(item.get("season"), item.get("episode"))
                    if slot is None:
                        # A show play with no episode number cannot be placed;
                        # inventing one would claim episodes nobody watched.
                        skipped += 1
                        continue
                watched = entry.setdefault("watched", {})
                if slot not in watched:
                    watched[slot] = str(item.get("watched_at") or "") or time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    )
                    added += 1
                if source and source not in entry.setdefault("sources", []):
                    entry["sources"].append(source)
            if added or skipped:
                self._save_locked()
        return {"added": added, "not_found": skipped, "batches": 1 if items else 0}

    def unmark_watched(self, items: list[dict]) -> dict:
        deleted = 0
        with self._lock:
            for item in items or []:
                entry = self._items.get(series_key(item))
                if not entry:
                    continue
                watched = entry.get("watched") or {}
                if entry.get("media_type") == "movie":
                    slot = "0x0"
                else:
                    slot = _episode_key(item.get("season"), item.get("episode"))
                if slot and watched.pop(slot, None) is not None:
                    deleted += 1
                if not entry.get("sections") and not entry.get("watched"):
                    self._items.pop(entry["key"], None)
            if deleted:
                self._save_locked()
        return {"deleted": deleted}

    # ── reading ────────────────────────────────────────────────────────────

    def _as_sync_item(self, entry: dict, season: object = None, episode: object = None) -> dict:
        item = {
            "title": entry.get("title") or "",
            "year": entry.get("year"),
            "media_type": entry.get("media_type") or "tv",
            "kind": entry.get("kind"),
            "tmdb_id": entry.get("tmdb_id"),
            "imdb_id": entry.get("imdb_id"),
            "anilist_id": entry.get("anilist_id"),
            "mal_id": entry.get("mal_id"),
            "anidb_id": entry.get("anidb_id"),
            "ids": {
                key: entry.get(f"{key}_id")
                for key in ("tmdb", "imdb", "anilist", "mal", "anidb")
                if entry.get(f"{key}_id")
            },
        }
        if season is not None:
            item["season"] = season
        if episode is not None:
            item["episode"] = episode
        return item

    def fetch(self, section: str) -> list[dict]:
        section = str(section or "").strip().lower()
        with self._lock:
            if section == "history":
                out = []
                for entry in self._items.values():
                    for slot, watched_at in (entry.get("watched") or {}).items():
                        season, _, episode = slot.partition("x")
                        if entry.get("media_type") == "movie":
                            item = self._as_sync_item(entry)
                        else:
                            item = self._as_sync_item(entry, int(season), int(episode))
                        item["watched_at"] = watched_at
                        out.append(item)
                return out
            return [
                self._as_sync_item(entry)
                for entry in self._items.values()
                if section in (entry.get("sections") or {})
            ]

    def entries(self) -> list[dict]:
        """Every stored entry, for the Library UI."""
        with self._lock:
            return [dict(entry) for entry in self._items.values()]

    def entry(self, key: str) -> dict | None:
        with self._lock:
            found = self._items.get(str(key))
            return dict(found) if found else None

    def counts(self) -> dict:
        with self._lock:
            out = {"total": len(self._items), "by_kind": {}, "by_section": {}}
            for entry in self._items.values():
                kind = str(entry.get("kind") or "show")
                out["by_kind"][kind] = out["by_kind"].get(kind, 0) + 1
                for section in (entry.get("sections") or {}):
                    out["by_section"][section] = out["by_section"].get(section, 0) + 1
                if entry.get("watched"):
                    out["by_section"]["history"] = out["by_section"].get("history", 0) + 1
            return out
