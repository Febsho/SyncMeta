"""Turn TMDB-keyed watch history back into AniList progress numbers.

AniList stores one *absolute* progress count per cour ("watched 7 of 12"), while
every other service in SyncMeta speaks TMDB season/episode. Writing history into
AniList therefore needs the inverse of the Fribb/offset mapping that
`providers.enrich_identity` applies on the way out — and getting it wrong is not
a stray row that can be deleted afterwards, it is a *rewrite* of the user's real
progress, possibly backwards.

So the inverse here is built by running the forward mapper over the user's own
AniList entries and recording where each local episode lands. Two properties
follow from that, and both matter more than coverage:

* the inverse can never disagree with the forward mapping, because it *is* the
  forward mapping, read in the other direction; and
* only entries already on the user's list can ever be written to, since nothing
  else is in the index. A history row for an anime they do not track is skipped
  rather than conjuring a list entry for it.

Four further rules keep a write honest. They are the reason this module exists
rather than a one-line max():

1. **A coordinate claimed by two entries is refused, not guessed.** Overlapping
   Fribb mappings do occur; picking one would write progress to the wrong cour.
2. **Progress advances only along a contiguous run from episode 1.** AniList's
   number means "watched up to N", so a history containing episodes 1, 2, 3 and
   5 supports progress 3, never 5 — 5 would silently claim episode 4.
3. **Progress never decreases.** A partial or stale history must not roll back
   a count the user set themselves. This is the guard that makes the whole
   operation non-destructive.
4. **Progress never exceeds the entry's own episode count.**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Cap on how far the forward mapper is walked per entry when building the
# index. Cours are short; a number past this is bad metadata, and each step
# costs a mapping lookup.
_MAX_INDEX_EPISODES = 500


@dataclass
class ProgressUpdate:
    """One AniList entry whose progress should move forward."""

    media_id: int
    title: str
    status: str
    current_progress: int
    new_progress: int


@dataclass
class ProgressPlan:
    updates: list[ProgressUpdate] = field(default_factory=list)
    #: Rows that matched no entry on the user's list.
    unmatched: int = 0
    #: Rows whose coordinate is claimed by more than one AniList entry.
    ambiguous: int = 0
    #: Entries already at or past the progress the history supports.
    already_current: int = 0
    #: Entries where the history only supports a lower count than AniList holds.
    would_regress: int = 0


def _int_or_none(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coordinate(item: dict) -> tuple | None:
    """The (tmdb_id, season, episode) key both sides are compared on."""
    tmdb_id = _int_or_none(item.get("tmdb_id") or (item.get("ids") or {}).get("tmdb"))
    if not tmdb_id:
        return None
    media_type = str(item.get("media_type") or "").strip().lower()
    if media_type == "movie":
        return (tmdb_id, None, None)
    season = _int_or_none(item.get("season"))
    episode = _int_or_none(item.get("episode"))
    if season is None or episode is None:
        return None
    return (tmdb_id, season, episode)


def _entry_episode_count(entry: dict) -> int:
    return _int_or_none(entry.get("anilist_episode_count")) or 0


def build_reverse_index(entries: list[dict]) -> tuple[dict[tuple, tuple[int, int]], set[tuple]]:
    """Map TMDB coordinates back to (anilist media id, local episode).

    Built by walking the *forward* mapper over each entry's own episode range,
    so the result is guaranteed to agree with how those episodes were exported.
    Coordinates that more than one entry claims are collected separately and
    must be refused by the caller rather than resolved to either candidate.
    """
    from .providers import enrich_identity

    index: dict[tuple, tuple[int, int]] = {}
    ambiguous: set[tuple] = set()

    for entry in entries:
        media_id = _int_or_none(entry.get("anilist_id"))
        if not media_id:
            continue
        media_type = str(entry.get("media_type") or "").strip().lower()

        if media_type == "movie":
            coordinate = _coordinate(enrich_identity(dict(entry)))
            if coordinate is None:
                continue
            _claim(index, ambiguous, coordinate, media_id, 1)
            continue

        total = _entry_episode_count(entry)
        if total <= 0 or total > _MAX_INDEX_EPISODES:
            if total > _MAX_INDEX_EPISODES:
                logger.info(
                    "AniList progress: '%s' reports %d episodes — above the index cap, skipping",
                    entry.get("title", "Unknown"), total,
                )
            continue

        for local_episode in range(1, total + 1):
            try:
                mapped = enrich_identity({**entry, "season": 1, "episode": local_episode})
            except Exception:
                logger.debug(
                    "AniList progress: forward mapping failed for %r episode %d",
                    entry.get("title"), local_episode, exc_info=True,
                )
                continue
            coordinate = _coordinate(mapped)
            if coordinate is None:
                continue
            _claim(index, ambiguous, coordinate, media_id, local_episode)

    for coordinate in ambiguous:
        index.pop(coordinate, None)
    if ambiguous:
        logger.info(
            "AniList progress: %d episode coordinate(s) are claimed by more than one entry — refusing those",
            len(ambiguous),
        )
    return index, ambiguous


def _claim(
    index: dict[tuple, tuple[int, int]],
    ambiguous: set[tuple],
    coordinate: tuple,
    media_id: int,
    local_episode: int,
) -> None:
    existing = index.get(coordinate)
    if existing is not None and existing != (media_id, local_episode):
        ambiguous.add(coordinate)
        return
    index[coordinate] = (media_id, local_episode)


def _contiguous_from_one(episodes: set[int]) -> int:
    """How far an unbroken run from episode 1 reaches.

    AniList progress means "watched up to N", so this is the largest number that
    claims nothing the history does not actually contain.
    """
    reached = 0
    while (reached + 1) in episodes:
        reached += 1
    return reached


def plan_progress_updates(entries: list[dict], items: list[dict]) -> ProgressPlan:
    """Decide which AniList entries should move forward, and how far."""
    plan = ProgressPlan()
    if not entries or not items:
        return plan

    index, ambiguous = build_reverse_index(entries)
    if not index and not ambiguous:
        plan.unmatched = len(items)
        return plan

    entries_by_id: dict[int, dict] = {}
    for entry in entries:
        media_id = _int_or_none(entry.get("anilist_id"))
        if media_id and media_id not in entries_by_id:
            entries_by_id[media_id] = entry

    watched_by_entry: dict[int, set[int]] = {}
    for item in items:
        coordinate = _coordinate(item)
        if coordinate is None:
            plan.unmatched += 1
            continue
        if coordinate in ambiguous:
            plan.ambiguous += 1
            continue
        match = index.get(coordinate)
        if match is None:
            plan.unmatched += 1
            continue
        media_id, local_episode = match
        watched_by_entry.setdefault(media_id, set()).add(local_episode)

    for media_id, episodes in watched_by_entry.items():
        entry = entries_by_id.get(media_id)
        if entry is None:
            continue
        supported = _contiguous_from_one(episodes)
        if supported <= 0:
            # Episodes matched, but none of them extend a run from the start —
            # there is no progress number that describes this without lying.
            plan.already_current += 1
            continue
        total = _entry_episode_count(entry)
        if total > 0:
            supported = min(supported, total)
        current = _int_or_none(entry.get("anilist_progress")) or 0
        if supported < current:
            plan.would_regress += 1
            continue
        if supported == current:
            plan.already_current += 1
            continue
        plan.updates.append(ProgressUpdate(
            media_id=media_id,
            title=str(entry.get("title") or f"AniList {media_id}"),
            # Preserving the entry's own status is deliberate: this writes a
            # count, and must not also re-shelve the entry.
            status=str(entry.get("anilist_status") or "").strip(),
            current_progress=current,
            new_progress=supported,
        ))

    return plan
