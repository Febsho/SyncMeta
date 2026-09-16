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

Each of those slots also keeps a ``plays`` list of every timestamp it was given,
because "watched" and "watched three times" are different answers and the hub
has to be able to give the second one. ``watched`` keeps the first date, which
is what the coverage views read; ``plays`` is what a rewatch is carried out of
here on. A row with no timestamp of its own only ever confirms presence — it is
watched *state*, not a play — so it can never add a viewing.
"""

from __future__ import annotations

import json
import logging
import os
import copy
import re
import tempfile
import threading
import time
from pathlib import Path

from .media_kind import KIND_ANIME, KIND_ANIME_MOVIE, classify, normalize_namespace
from .providers import PLANNED_FLAG, PlaySet, is_planned

logger = logging.getLogger(__name__)

IDENTITY_MAPPING_VERSION = 2

#: Categories the Library holds, mirroring the pair categories.
SECTION_WATCHLIST = "watchlist"
SECTION_COLLECTION = "collection"
ALL_SECTIONS = (SECTION_WATCHLIST, SECTION_COLLECTION)

_PROVIDER_LABELS = {
    "simkl": "SIMKL", "trakt": "Trakt", "anilist": "AniList",
    "mdblist": "MDBList", "pmdb": "PublicMetaDB",
}
_STATUS_LABELS = {
    "completed": "Completed", "watching": "Watching",
    "plantowatch": "Planning", "planning": "Planning",
    "plan_to_watch": "Planning", "hold": "Paused", "paused": "Paused",
    "dropped": "Dropped",
}


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
            "provider_entries": [],
            "watched": {},
            # Every viewing, not just the first: {slot: [timestamp, ...]}.
            # `watched` keeps one date per episode because that is what the
            # coverage views ask for; `plays` is what makes the Library able to
            # carry a rewatch out to a service that records plays.
            "plays": {},
            "resume": {},
            "sources": [],
            "provider_states": {},
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
        confidence = str(item.get("match_confidence") or "").strip().lower()
        mapping_source = str(item.get("anime_mapping_source") or "").strip()
        if (entry.get("kind") in (KIND_ANIME, KIND_ANIME_MOVIE)
                and confidence in {"exact", "verified"} and mapping_source
                and not entry.get("canonical_identity")):
            entry["canonical_identity"] = {
                "tmdb_id": str(entry.get("tmdb_id") or ""),
                "namespace": entry.get("media_type"),
                "confidence": confidence, "source": mapping_source,
                "resolved_at": time.time(),
                "mapping_version": IDENTITY_MAPPING_VERSION,
                "evidence": list(item.get("mapping_evidence") or []) or [f"{provider}:{value}" for provider, value in (
                    ("anilist", entry.get("anilist_id")),
                    ("mal", entry.get("mal_id")),
                    ("anidb", entry.get("anidb_id")),
                ) if value],
            }
        season = item.get("season")
        if season is not None:
            try:
                number = int(season)
            except (TypeError, ValueError):
                number = None
            if number is not None:
                seasons = entry.setdefault("seasons", {})
                record = seasons.setdefault(str(number), {})
                record.setdefault("provider_entries", [])
                for field in ("title", "anilist_id", "mal_id", "anidb_id"):
                    source_field = "title" if field == "title" else field
                    value = item.get(f"season_{field}") or (item.get(source_field) if field != "title" else None)
                    if value and not record.get(field):
                        record[field] = value
        ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
        for provider in ("anilist", "simkl", "mal", "anidb"):
            provider_id = item.get(f"{provider}_id") or ids.get(provider)
            if not provider_id:
                continue
            segment = {"provider": provider, "id": str(provider_id),
                       "title": str(item.get("title") or "").strip()}
            if item.get("year"):
                segment["year"] = item["year"]
            for source_field, target_field in (
                ("local_season", "local_season"),
                ("tmdb_season", "tmdb_season"),
                ("tvdb_season", "tvdb_season"),
                ("episode_start", "episode_start"),
                ("episode_end", "episode_end"),
                ("episode_offset", "episode_offset"),
                ("tmdb_episode_start", "tmdb_episode_start"),
                ("tmdb_episode_end", "tmdb_episode_end"),
                ("tvdb_episode_start", "tvdb_episode_start"),
                ("tvdb_episode_end", "tvdb_episode_end"),
            ):
                if item.get(source_field) is not None:
                    segment[target_field] = item[source_field]
            entries = entry.setdefault("provider_entries", [])
            found = next((existing for existing in entries
                          if existing.get("provider") == provider and
                          str(existing.get("id")) == str(provider_id)), None)
            if found is None:
                entries.append(segment.copy())
            else:
                for field, value in segment.items():
                    found.setdefault(field, value)
            if season is not None and number is not None:
                season_entries = record.setdefault("provider_entries", [])
                found = next((existing for existing in season_entries
                              if existing.get("provider") == provider and
                              str(existing.get("id")) == str(provider_id)), None)
                if found is None:
                    season_entries.append(segment.copy())
                else:
                    for field, value in segment.items():
                        found.setdefault(field, value)
        entry["updated_at"] = time.time()

    @staticmethod
    def _remember_provider_state(entry: dict, item: dict, source: str, section: str) -> bool:
        """Persist the provider facts already present on a sync item.

        This deliberately records the source response while it is available;
        the Library inspector must not guess a remote state later or make the
        browser re-query connected services. Older rows without this metadata
        continue to render from their real ``sources`` list as "Synced".
        """
        provider = str(item.get("_syncmeta_source_provider") or source or "").strip().lower()
        if provider not in _PROVIDER_LABELS:
            return False
        ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
        provider_id = item.get(f"{provider}_id") or ids.get(provider)
        if provider == "pmdb":
            provider_id = item.get("pmdb_item_id") or provider_id or item.get("tmdb_id") or ids.get("tmdb")
        raw_status = str(
            item.get("_syncmeta_source_status") or item.get("status") or ""
        ).strip().lower()
        status = _STATUS_LABELS.get(raw_status)
        if not status:
            if section == "history":
                status = "Watched"
            elif section == SECTION_WATCHLIST and is_planned(item):
                # Only claim "Planning" when the source said so. Asserting it for
                # everything in the watchlist section laundered curated lists —
                # which carry no status at all — into plan-to-watch.
                status = "Planning"
            else:
                status = "Synced"
        timestamp = item.get("updated_at") or item.get("last_updated") or time.time()
        state = {
            "provider": provider,
            "label": _PROVIDER_LABELS[provider],
            "status": status,
            "provider_id": str(provider_id or "").strip(),
            "last_synced": timestamp,
        }
        states = entry.setdefault("provider_states", {})
        if states.get(provider) == state:
            return False
        states[provider] = state
        return True

    @staticmethod
    def _native_identities(value: dict) -> set[tuple[str, str]]:
        ids = value.get("ids") if isinstance(value.get("ids"), dict) else {}
        found = set()
        for provider in ("anilist", "mal", "anidb"):
            provider_id = value.get(f"{provider}_id") or ids.get(provider)
            if provider_id:
                found.add((provider, str(provider_id)))
        for part in value.get("provider_entries", []) or []:
            if (isinstance(part, dict) and part.get("provider") in {"anilist", "mal", "anidb"}
                    and part.get("id")):
                found.add((str(part["provider"]), str(part["id"])))
        return found

    def _entry_for_verified_write(self, key: str, item: dict) -> tuple[dict, bool]:
        """Return the canonical entry, repairing one uniquely matched old key."""
        entry = self._items.get(key)
        confidence = str(item.get("match_confidence") or
                         (item.get("canonical_identity") or {}).get("confidence") or "").lower()
        source = str(item.get("anime_mapping_source") or
                     (item.get("canonical_identity") or {}).get("source") or "").strip()
        if not key.startswith("tmdb:") or confidence not in {"exact", "verified"} or not source:
            if entry is None:
                entry = self._blank(key, item)
                self._items[key] = entry
            return entry, False
        native = self._native_identities(item)
        namespace = key.split(":", 2)[1]
        candidates = [candidate for candidate_key, candidate in self._items.items()
                      if (candidate_key != key
                          and normalize_namespace(candidate.get("media_type")) == namespace
                          and candidate.get("kind") in {KIND_ANIME, KIND_ANIME_MOVIE}
                          and native & self._native_identities(candidate))]
        if len(candidates) != 1:
            if entry is None:
                entry = self._blank(key, item)
                self._items[key] = entry
            return entry, False
        old = candidates[0]
        old_key = str(old.get("key") or "")
        if entry is None:
            entry = copy.deepcopy(old)
            self._items[key] = entry
        else:
            self._merge_rekey_state(entry, old)
        self._items.pop(old_key, None)
        entry["key"] = key
        entry["tmdb_id"] = key.rsplit(":", 1)[-1]
        previous_identity = copy.deepcopy(entry.get("canonical_identity") or {})
        if previous_identity:
            entry.setdefault("identity_repairs", []).append(previous_identity)
        entry.setdefault("identity_repairs", []).append({
            "previous_key": old_key, "source": source, "confidence": confidence,
            "resolved_at": time.time(),
        })
        entry["canonical_identity"] = {
            "tmdb_id": key.rsplit(":", 1)[-1], "namespace": namespace,
            "confidence": confidence, "source": source,
            "resolved_at": time.time(), "mapping_version": IDENTITY_MAPPING_VERSION,
            "previous_key": old_key,
            "evidence": list(item.get("mapping_evidence") or []),
        }
        return entry, True

    def add(self, section: str, items: list[dict], source: str = "") -> dict:
        """Put ``items`` into ``section``. Returns add/skip counts."""
        section = str(section or "").strip().lower()
        if section not in ALL_SECTIONS:
            return {"added": 0, "not_found": len(items or []), "batches": 0}
        added = 0
        skipped = 0
        changed = False
        with self._lock:
            for item in items or []:
                key = series_key(item)
                if not key:
                    skipped += 1
                    continue
                entry, repaired = self._entry_for_verified_write(key, item)
                changed = repaired or changed
                self._merge_identity(entry, item)
                changed = self._remember_provider_state(entry, item, source, section) or changed
                sections = entry.setdefault("sections", {})
                if section not in sections:
                    sections[section] = time.time()
                    added += 1
                if section == SECTION_WATCHLIST:
                    # Remember whether the source actually meant plan-to-watch.
                    # The Library is the hub, so anything it cannot answer here
                    # is a question the next service downstream has to guess at
                    # — and PMDB's watchlist guessed "yes" for everything.
                    planned = is_planned(item)
                    if planned is not None and entry.get("planned") is not True:
                        entry["planned"] = planned
                        changed = True
                if source and source not in entry.setdefault("sources", []):
                    entry["sources"].append(source)
                    changed = True
            if added or skipped or changed:
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
                    if section == SECTION_WATCHLIST:
                        entry.pop("planned", None)
                # An entry with no sections and no watch history is not "in" the
                # library any more; keeping it would make removals invisible.
                if not entry.get("sections") and not entry.get("watched") and not entry.get("resume"):
                    self._items.pop(key, None)
            if deleted:
                self._save_locked()
        return {"deleted": deleted}

    def mark_watched(self, items: list[dict], source: str = "") -> dict:
        """Record plays. A movie is stored as ``0x0`` so it has one slot."""
        added = 0
        skipped = 0
        changed = False
        with self._lock:
            for item in items or []:
                key = series_key(item)
                if not key:
                    skipped += 1
                    continue
                entry, repaired = self._entry_for_verified_write(key, item)
                changed = repaired or changed
                self._merge_identity(entry, item)
                changed = self._remember_provider_state(entry, item, source, "history") or changed
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
                plays = entry.setdefault("plays", {})
                replacement = str(item.get("_syncmeta_replaces_episode") or "").strip()
                if replacement and replacement != slot:
                    # Older SIMKL aggregate-history imports intentionally
                    # overflowed later seasons into S1 (for example Frieren
                    # 1x29 instead of 2x1). Corrected rows carry the exact stale
                    # slot they supersede so the next sync repairs existing
                    # libraries without touching unrelated watched episodes.
                    watched.pop(replacement, None)
                    plays.pop(replacement, None)
                reported = str(item.get("watched_at") or "").strip()
                if slot not in watched:
                    watched[slot] = reported or time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    )
                    plays[slot] = [watched[slot]]
                    added += 1
                elif self._record_extra_play(plays, slot, watched[slot], item, reported):
                    added += 1
                    changed = True
                if source and source not in entry.setdefault("sources", []):
                    entry["sources"].append(source)
                    changed = True
            if added or skipped or changed:
                self._save_locked()
        return {"added": added, "not_found": skipped, "batches": 1 if items else 0}

    @staticmethod
    def _record_extra_play(plays: dict, slot: str, first_seen: str, item: dict, reported: str) -> bool:
        """Record a rewatch of an episode the Library already has.

        Two rules keep this from inventing history. A row carrying no timestamp
        of its own is *presence* — a watched-state read, an AniList progress
        count, a SIMKL aggregate — and says nothing about how many times the
        episode was played, so it can only ever confirm what is already stored.
        And a timestamp already on file is the same play arriving again, which
        is what a re-read of the source looks like; only a genuinely new instant
        is a second viewing.
        """
        if not reported or item.get("cursor_exempt") or item.get("anilist_derived"):
            return False
        known = plays.get(slot)
        if not isinstance(known, list) or not known:
            # An entry stored before plays existed: seed it from the one date it
            # kept, so its first play is not recounted as a rewatch.
            known = [first_seen] if first_seen else []
            plays[slot] = known
        # Matched with tolerance, not on the exact second. The Library is the
        # hub, so it hears about one viewing from several services, each having
        # timestamped it slightly differently — compared exactly, one play
        # became one play per service and then fanned back out that way.
        ledger = PlaySet(known)
        if not ledger.stamped or ledger.matches(reported):
            return False
        known.append(reported)
        return True

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
                    (entry.get("plays") or {}).pop(slot, None)
                    deleted += 1
                if not entry.get("sections") and not entry.get("watched") and not entry.get("resume"):
                    self._items.pop(entry["key"], None)
            if deleted:
                self._save_locked()
        return {"deleted": deleted}

    def save_resume(self, items: list[dict], source: str = "") -> dict:
        """Upsert continue-watching positions at movie/episode granularity."""
        added = 0
        skipped = 0
        changed = False
        with self._lock:
            for item in items or []:
                key = series_key(item)
                if not key:
                    skipped += 1
                    continue
                entry, repaired = self._entry_for_verified_write(key, item)
                changed = repaired or changed
                self._merge_identity(entry, item)
                changed = self._remember_provider_state(entry, item, source, "resume") or changed
                if entry.get("media_type") == "movie":
                    slot = "0x0"
                else:
                    slot = _episode_key(item.get("season"), item.get("episode"))
                if slot is None:
                    skipped += 1
                    continue
                try:
                    position_ms = max(0, int(item.get("position_ms") or 0))
                    runtime_ms = max(0, int(item.get("runtime_ms") or 0))
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                previous = (entry.get("resume") or {}).get(slot)
                comparable = {
                    "position_ms": position_ms,
                    "runtime_ms": runtime_ms,
                    "progress": item.get("progress"),
                }
                previous_comparable = {
                    key: (previous or {}).get(key) for key in comparable
                }
                if previous_comparable != comparable:
                    value = {
                        **comparable,
                        "updated_at": item.get("updated_at") or time.time(),
                    }
                    entry.setdefault("resume", {})[slot] = value
                    added += 1
                    changed = True
                if source and source not in entry.setdefault("sources", []):
                    entry["sources"].append(source)
                    changed = True
            if added or skipped or changed:
                self._save_locked()
        return {"added": added, "not_found": skipped, "batches": 1 if items else 0}

    def remove_resume(self, items: list[dict]) -> dict:
        deleted = 0
        with self._lock:
            for item in items or []:
                entry = self._items.get(series_key(item))
                if not entry:
                    continue
                slot = "0x0" if entry.get("media_type") == "movie" else _episode_key(
                    item.get("season"), item.get("episode")
                )
                if slot and (entry.get("resume") or {}).pop(slot, None) is not None:
                    deleted += 1
                if not entry.get("sections") and not entry.get("watched") and not entry.get("resume"):
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
            "canonical_identity": entry.get("canonical_identity"),
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
            canonical = entry.get("canonical_identity") or {}
            if canonical.get("confidence") in {"exact", "verified"}:
                record = (entry.get("seasons") or {}).get(str(season)) or {}
                item["provider_segments"] = copy.deepcopy(record.get("provider_entries") or [])
        if episode is not None:
            item["episode"] = episode
        return item

    def fetch(self, section: str) -> list[dict]:
        section = str(section or "").strip().lower()
        with self._lock:
            if section == "history":
                out = []
                for entry in self._items.values():
                    stored_plays = entry.get("plays") or {}
                    for slot, watched_at in (entry.get("watched") or {}).items():
                        season, _, episode = slot.partition("x")
                        # One row per *play*. Emitting only the stored date would
                        # make the Library flatten every rewatch it was given,
                        # which is exactly what it exists not to do.
                        stamps = stored_plays.get(slot)
                        if not isinstance(stamps, list) or not stamps:
                            stamps = [watched_at]
                        for stamp in stamps:
                            if entry.get("media_type") == "movie":
                                item = self._as_sync_item(entry)
                            else:
                                item = self._as_sync_item(entry, int(season), int(episode))
                            item["watched_at"] = stamp
                            out.append(item)
                return out
            if section == "resume":
                out = []
                for entry in self._items.values():
                    for slot, state in (entry.get("resume") or {}).items():
                        season, _, episode = slot.partition("x")
                        if entry.get("media_type") == "movie":
                            item = self._as_sync_item(entry)
                        else:
                            item = self._as_sync_item(entry, int(season), int(episode))
                        item.update(state or {})
                        out.append(item)
                return out
            if section == "all":
                return [self._as_sync_item(entry) for entry in self._items.values()]
            out = []
            for entry in self._items.values():
                if section not in (entry.get("sections") or {}):
                    continue
                item = self._as_sync_item(entry)
                if section == SECTION_WATCHLIST and entry.get("planned") is not None:
                    # Pass the source's own answer on. An entry stored before
                    # this existed carries no flag, and stays unknown rather
                    # than being asserted either way.
                    item[PLANNED_FLAG] = bool(entry.get("planned"))
                out.append(item)
            return out

    def clear(self) -> int:
        """Remove every entry. Returns how many there were.

        The file is written even when the Library was already empty, so the
        on-disk state always matches what was asked for rather than depending on
        whether anything happened to be there.
        """
        with self._lock:
            removed = len(self._items)
            self._items = {}
            self._save_locked()
        logger.info("Cleared %d entry(ies) from library %s", removed, self._path)
        return removed

    def entries(self) -> list[dict]:
        """Every stored entry, for the Library UI."""
        with self._lock:
            return [dict(entry) for entry in self._items.values()]

    def entry(self, key: str) -> dict | None:
        with self._lock:
            found = self._items.get(str(key))
            return dict(found) if found else None

    def rekey_verified(self, old_key: str, new_key: str, *, source: str,
                       confidence: str = "verified") -> dict:
        """Repair a verified anime key without dropping plays or existing state.

        This explicit operation never guesses a replacement identity. Callers
        must supply the verified destination and the identity source.
        """
        if confidence not in {"exact", "verified"} or not source:
            raise ValueError("A verified identity source is required")
        parts = str(new_key).split(":")
        if len(parts) != 3 or parts[0] != "tmdb" or parts[1] not in {"tv", "movie"} or not parts[2].isdigit():
            raise ValueError("Destination must be a TMDB movie or TV identity")
        with self._lock:
            original = self._items.get(str(old_key))
            if original is None:
                return {"rekeyed": False, "reason": "source_missing"}
            if old_key == new_key:
                return {"rekeyed": False, "reason": "already_canonical"}
            if original.get("kind") not in {KIND_ANIME, KIND_ANIME_MOVIE}:
                raise ValueError("Identity repair requires an anime source")
            if normalize_namespace(original.get("media_type")) != parts[1]:
                raise ValueError("TMDB namespace conflicts with source")
            target = self._items.get(new_key)
            if target is not None and normalize_namespace(target.get("media_type")) != parts[1]:
                raise ValueError("TMDB namespace conflicts with destination")
            if target is not None and not self._same_anime_identity(original, target):
                raise ValueError("Destination has no shared anime-native identity")

            replacement = copy.deepcopy(target or original)
            if target is not None:
                self._merge_rekey_state(replacement, original)
            replacement["key"] = new_key
            replacement["tmdb_id"] = parts[2]
            replacement["kind"] = original["kind"]
            if replacement.get("canonical_identity"):
                replacement.setdefault("identity_repairs", []).append(
                    copy.deepcopy(replacement["canonical_identity"])
                )
            if target is not None and original.get("canonical_identity"):
                replacement.setdefault("identity_repairs", []).append(
                    copy.deepcopy(original["canonical_identity"])
                )
            replacement["canonical_identity"] = {
                "tmdb_id": parts[2], "namespace": parts[1],
                "confidence": confidence, "source": source,
                "resolved_at": time.time(), "previous_key": old_key,
                "mapping_version": IDENTITY_MAPPING_VERSION,
            }
            previous_items = self._items.copy()
            self._items[new_key] = replacement
            self._items.pop(old_key, None)
            try:
                self._save_locked()
            except Exception:
                self._items = previous_items
                raise
            return {"rekeyed": True, "merged": target is not None, "key": new_key}

    @staticmethod
    def _same_anime_identity(left: dict, right: dict) -> bool:
        for field in ("anilist_id", "mal_id", "anidb_id"):
            if left.get(field) and right.get(field) and str(left[field]) == str(right[field]):
                return True
        left_entries = {(part.get("provider"), str(part.get("id")))
                        for part in left.get("provider_entries", []) if isinstance(part, dict)}
        right_entries = {(part.get("provider"), str(part.get("id")))
                         for part in right.get("provider_entries", []) if isinstance(part, dict)}
        return bool(left_entries & right_entries)

    @staticmethod
    def _merge_rekey_state(target: dict, source: dict) -> None:
        for field in ("title", "year", "imdb_id", "anilist_id", "mal_id", "anidb_id", "planned"):
            if target.get(field) is None or target.get(field) == "":
                if source.get(field) is not None:
                    target[field] = source[field]
        target["added_at"] = min(target.get("added_at", time.time()), source.get("added_at", time.time()))
        target["updated_at"] = max(target.get("updated_at", 0), source.get("updated_at", 0))
        for field in ("sources",):
            values = target.setdefault(field, [])
            for value in source.get(field, []):
                if value not in values:
                    values.append(value)
        for field in ("sections", "watched", "provider_states"):
            values = target.setdefault(field, {})
            for key, value in source.get(field, {}).items():
                if key in values and values[key] != value:
                    target.setdefault("repair_conflicts", {}).setdefault(field, {})[key] = copy.deepcopy(value)
                values.setdefault(key, value)
        for slot, stamps in source.get("plays", {}).items():
            stored = target.setdefault("plays", {}).setdefault(slot, [])
            for stamp in stamps:
                if stamp not in stored:
                    stored.append(stamp)
        for slot, stamp in source.get("watched", {}).items():
            stored = target.setdefault("plays", {}).setdefault(slot, [])
            if stamp not in stored:
                stored.append(stamp)
        for slot, value in source.get("resume", {}).items():
            existing = target.setdefault("resume", {}).get(slot)
            if existing is not None and existing != value:
                target.setdefault("repair_conflicts", {}).setdefault("resume", {})[slot] = copy.deepcopy(value)
            if existing is None or str(value.get("updated_at", "")) > str(existing.get("updated_at", "")):
                target["resume"][slot] = copy.deepcopy(value)
        def merge_entries(destination, incoming):
            for part in incoming or []:
                if not isinstance(part, dict):
                    continue
                found = next((existing for existing in destination if
                              existing.get("provider") == part.get("provider") and
                              str(existing.get("id")) == str(part.get("id"))), None)
                if found is None:
                    destination.append(copy.deepcopy(part))
                else:
                    for field, value in part.items():
                        found.setdefault(field, value)
        merge_entries(target.setdefault("provider_entries", []), source.get("provider_entries", []))
        for number, record in source.get("seasons", {}).items():
            destination = target.setdefault("seasons", {}).setdefault(number, {})
            for field, value in record.items():
                if field != "provider_entries":
                    destination.setdefault(field, copy.deepcopy(value))
            merge_entries(destination.setdefault("provider_entries", []), record.get("provider_entries", []))

    def scan_identity_integrity(self) -> list[dict]:
        """Classify stored anime identities for review without changing data."""
        with self._lock:
            rows = copy.deepcopy(list(self._items.values()))
        native_keys: dict[tuple[str, str], set[str]] = {}
        for entry in rows:
            for part in entry.get("provider_entries", []):
                if isinstance(part, dict) and part.get("provider") and part.get("id"):
                    native_keys.setdefault((str(part["provider"]), str(part["id"])), set()).add(entry["key"])
        results = []
        for entry in rows:
            if entry.get("kind") not in {KIND_ANIME, KIND_ANIME_MOVIE}:
                continue
            reasons = []
            classification = "healthy"
            key = str(entry.get("key") or "")
            canonical = entry.get("canonical_identity") or {}
            if key.startswith("tmdb:") and not canonical:
                classification = "review"
                reasons.append("legacy_mapping_without_evidence")
            elif canonical and canonical.get("mapping_version") != IDENTITY_MAPPING_VERSION:
                classification = "review"
                reasons.append("stale_mapping_version")
            if not key.startswith("tmdb:"):
                classification = "unresolved"
                reasons.append("no_canonical_tmdb_identity")
            if key.startswith("tmdb:"):
                parts = key.split(":")
                if len(parts) != 3 or parts[1] != normalize_namespace(entry.get("media_type")):
                    classification = "likely_wrong"
                    reasons.append("namespace_conflict")
                elif canonical and (str(canonical.get("tmdb_id")) != parts[2] or
                                    canonical.get("namespace") != parts[1]):
                    classification = "needs_rekey"
                    reasons.append("provenance_disagrees_with_key")
            shared_keys = set()
            for part in entry.get("provider_entries", []):
                if isinstance(part, dict):
                    shared_keys.update(native_keys.get(
                        (str(part.get("provider")), str(part.get("id"))), set()
                    ))
            shared_keys.discard(key)
            if shared_keys:
                if classification == "healthy":
                    classification = "review"
                reasons.append("provider_entry_on_multiple_canonical_keys")
            segments = [part for part in entry.get("provider_entries", [])
                        if isinstance(part, dict) and part.get("provider") in {"anilist", "mal", "anidb"}]
            for index, first in enumerate(segments):
                for second in segments[index + 1:]:
                    if first.get("provider") != second.get("provider"):
                        continue
                    try:
                        years_apart = abs(int(first.get("year")) - int(second.get("year")))
                    except (TypeError, ValueError):
                        continue
                    first_words = set(re.findall(r"\w{4,}", str(first.get("title") or "").lower()))
                    second_words = set(re.findall(r"\w{4,}", str(second.get("title") or "").lower()))
                    first_words -= {"season", "cour", "part"}
                    second_words -= {"season", "cour", "part"}
                    if years_apart > 8 and first_words and second_words and not first_words & second_words:
                        if classification == "healthy":
                            classification = "review"
                        reasons.append("suspicious_provider_entry_collision")
                        break
                if "suspicious_provider_entry_collision" in reasons:
                    break
            results.append({
                "key": key, "title": entry.get("title") or "",
                "classification": classification, "reasons": reasons,
                "related_keys": sorted(shared_keys),
                "canonical_identity": canonical,
                "provider_entries": entry.get("provider_entries", []),
            })
        return results

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
                if entry.get("resume"):
                    out["by_section"]["resume"] = out["by_section"].get("resume", 0) + 1
            return out
