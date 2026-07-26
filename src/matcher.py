"""Matching logic to resolve SIMKL items to PublicMetaDB TMDB IDs."""

import logging
import threading
import time
import unicodedata
from dataclasses import dataclass

import requests

from .publicmetadb_client import PublicMetaDBClient

logger = logging.getLogger(__name__)

_TITLE_STOPWORDS = {
    "the", "a", "an", "of", "and", "to", "no", "vs", "part", "season", "movie",
    "tv", "ova", "ona", "special",
}

# External ID types to try, in order, when TMDB is missing.
_DEFAULT_LOOKUP_CHAIN = [
    ("imdb", "imdb_id"),
    ("mal", "mal_id"),
    ("anilist", "anilist_id"),
    ("anidb", "anidb_id"),
    ("tvdb", "tvdb_id"),
]

# Anime usually has stronger AniList/MAL signals than IMDb. Prefer those first.
_ANIME_LOOKUP_CHAIN = [
    ("anilist", "anilist_id"),
    ("mal", "mal_id"),
    ("anidb", "anidb_id"),
    ("imdb", "imdb_id"),
    ("tvdb", "tvdb_id"),
]

_ROOT_LOOKUP_CHAIN = [
    ("mal", "root_mal_id"),
    ("anilist", "root_anilist_id"),
]

# Failed resolutions are remembered for this long before being retried.
_FAILED_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# TMDB IDs confirmed to be wrong community-submitted mappings in PMDB for anime
# entries.  Any PMDB external-mapping result that returns one of these IDs for an
# anime item is silently rejected so the item falls through to Fribb or manual
# resolution.  Extend this set if new pollution patterns are discovered.
_BLOCKED_ANIME_PMDB_TMDB_IDS: frozenset[int] = frozenset([
    277700,  # Wrong franchise-root for multiple anime series
    154634,  # Wrong mapping confirmed in repair script
    317316,  # Wrong mapping confirmed in repair script
    298754,  # Wrong mapping confirmed in repair script
])


def _coerce_tmdb_id(value: object, media_type: str = "") -> int | None:
    if isinstance(value, dict):
        media_key = str(media_type or "").strip().lower()
        candidates: list[str] = []
        if media_key:
            candidates.append(media_key)
        if media_key == "tv":
            candidates.extend(["show", "series"])
        elif media_key == "movie":
            candidates.append("movies")
        candidates.extend(["tv", "movie", "show", "series"])
        for key in candidates:
            if key in value:
                return _coerce_tmdb_id(value.get(key), media_type)
        return None
    if isinstance(value, (list, tuple)):
        # Fribb stores movie ids as a list.  Only a single-element list is
        # unambiguous; a longer one must fall through to manual resolution
        # rather than silently picking the first entry.
        if len(value) != 1:
            return None
        return _coerce_tmdb_id(value[0], media_type)
    try:
        if value is None or value == "":
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MatchResult:
    tmdb_id: int | None
    resolution_kind: str
    unresolved_reason: str | None = None
    match_confidence: str = "verified"
    anime_mapping_source: str | None = None
    candidate_tmdb_id: int | None = None


class ItemMatcher:
    """Resolve normalized items to TMDB IDs usable by PublicMetaDB."""

    def __init__(
        self,
        pmdb: PublicMetaDBClient,
        anime_root_resolver=None,
        initial_cache: dict | None = None,
        initial_failed_cache: dict[str, str] | None = None,
    ):
        self._pmdb = pmdb
        # Pre-populate with persisted resolutions from a previous sync run so
        # unchanged items resolve instantly without any external API calls.
        self._cache: dict[str, int | None] = {
            str(key): coerced
            for key, value in (initial_cache or {}).items()
            for coerced in [_coerce_tmdb_id(value)]
            if coerced is not None
        }
        # Lock protecting _cache and _failed_cache so concurrent provider syncs
        # (e.g. SIMKL shows + AniList anime) don't race on cache writes.
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._stats = {
            "lookups": 0,
            "cache_hits": 0,
            "failed_cache_hits": 0,
            "direct_hits": 0,
            "mapping_hits": 0,
            "root_hits": 0,
            "lookup_unavailable": 0,
            "missing_id_failures": 0,
            "not_found_failures": 0,
        }
        # Optional callable(anilist_id: int | None, mal_id: int | None) -> dict | None
        # Returns {"root": media_dict, ...} from the AniList prequel chain.
        # Used as a last resort for anime sequels that fail all direct lookups.
        self._anime_root_resolver = anime_root_resolver
        # Failed resolutions keyed by cache_key → unix timestamp of failure.
        # Items in here are skipped until the TTL expires, avoiding redundant
        # AniList chain-walks and PMDB lookups for permanently unresolvable entries.
        # Populated by _load_failed_cache below, which restores the reason a
        # resolution failed alongside its timestamp.  Without it, every cached
        # failure reported the generic "cached_miss" after a restart and the
        # Issues panel lost the real explanation for the whole TTL.
        self._failed_reason_cache: dict[str, str] = {}
        self._failed_cache: dict[str, float] = self._load_failed_cache(initial_failed_cache)

    def _load_failed_cache(self, raw: dict[str, str] | None) -> dict[str, float]:
        """Convert the persisted failed cache to float unix timestamps.

        Values are ``"<iso-timestamp>|<reason>"``.  The reason is carried in the
        same string so the store's schema stays an opaque ``dict[str, str]``;
        entries written before the reason was persisted have no separator and
        simply load without one.
        """
        if not raw:
            return {}
        result: dict[str, float] = {}
        now = time.time()
        cutoff = now - _FAILED_CACHE_TTL_SECONDS
        for key, value in raw.items():
            try:
                import datetime
                iso, _, reason = str(value).partition("|")
                ts = datetime.datetime.fromisoformat(iso).timestamp()
                if ts > cutoff:
                    result[key] = ts
                    if reason:
                        self._failed_reason_cache[key] = reason
            except Exception:
                pass
        return result

    @property
    def resolution_cache(self) -> dict[str, int]:
        """Return only successful resolutions for persistence (excludes failures)."""
        return {k: v for k, v in self._cache.items() if isinstance(v, int)}

    @property
    def failed_resolution_cache(self) -> dict[str, str]:
        """Recent failures as {cache_key: "iso_timestamp|reason"} for persistence.

        The reason rides along in the value so a restart keeps the real
        explanation ("unconfirmed_mapping", "missing_anime_mapping", ...) instead
        of degrading every entry to "cached_miss" until the TTL expires.
        """
        import datetime
        now = time.time()
        cutoff = now - _FAILED_CACHE_TTL_SECONDS
        out: dict[str, str] = {}
        for key, ts in self._failed_cache.items():
            if ts <= cutoff:
                continue
            iso = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
            reason = self._failed_reason_cache.get(key, "")
            out[key] = f"{iso}|{reason}" if reason else iso
        return out

    def stats_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def preseed_anime_from_fribb(self, items: list[dict]) -> int:
        from . import fribb_client
        seeded = 0
        for item in items:
            if str(item.get("anime_resolve_mode") or "") != "list_identity":
                continue
            cache_key = self._cache_key(item)
            if cache_key in self._cache:
                continue
            ids = item.get("ids", {})
            entry = None
            for id_key, lookup_fn in (
                (item.get("anilist_id") or ids.get("anilist"), fribb_client.lookup_by_anilist),
                (item.get("mal_id") or ids.get("mal"), fribb_client.lookup_by_mal),
            ):
                if not id_key:
                    continue
                try:
                    entry = lookup_fn(int(id_key))
                except (TypeError, ValueError):
                    continue
                if entry is not None:
                    break
            if not isinstance(entry, dict):
                continue
            tmdb_id, mapped_media_type = fribb_client.extract_tmdb(
                entry.get("themoviedb_id") or entry.get("themoviedb")
            )
            if not tmdb_id or tmdb_id in _BLOCKED_ANIME_PMDB_TMDB_IDS:
                continue
            # Never seed a movie id onto a tv item (or vice versa) — the mapping
            # namespace must agree with the item before it can bypass lookup.
            item_media_type = str(item.get("media_type") or "").strip().lower()
            if mapped_media_type and item_media_type and item_media_type != mapped_media_type:
                continue
            with self._lock:
                if cache_key not in self._cache:
                    self._cache[cache_key] = tmdb_id
                    seeded += 1
        if seeded:
            logger.info("Pre-seeded %d anime items from Fribb into resolution cache", seeded)
        return seeded

    def resolve_tmdb_id(self, item: dict) -> int | None:
        return self.resolve_match(item).tmdb_id

    def resolve_match(self, item: dict) -> MatchResult:
        """Return a detailed match result for a normalized item."""
        cache_key = self._cache_key(item)

        # Fast path: check caches without the lock (reads are safe in CPython).
        with self._lock:
            self._stats["lookups"] += 1
        if cache_key in self._cache:
            cached_tmdb_id = self._cache[cache_key]
            with self._lock:
                self._stats["cache_hits"] += 1
            if cached_tmdb_id is not None:
                return MatchResult(tmdb_id=cached_tmdb_id, resolution_kind="cache")
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
            )
        failed_at = self._failed_cache.get(cache_key)
        if failed_at is not None and (time.time() - failed_at) < _FAILED_CACHE_TTL_SECONDS:
            with self._lock:
                self._stats["failed_cache_hits"] += 1
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
            )

        # Coordinate cache misses per key so only duplicate lookups block each
        # other; distinct items can still resolve concurrently.
        with self._lock:
            if cache_key in self._cache:
                cached_tmdb_id = self._cache[cache_key]
                if cached_tmdb_id is not None:
                    return MatchResult(tmdb_id=cached_tmdb_id, resolution_kind="cache")
                return MatchResult(
                    tmdb_id=None,
                    resolution_kind="unresolved",
                    unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
                )
            failed_at = self._failed_cache.get(cache_key)
            if failed_at is not None and (time.time() - failed_at) < _FAILED_CACHE_TTL_SECONDS:
                return MatchResult(
                    tmdb_id=None,
                    resolution_kind="unresolved",
                    unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
                )
            in_flight = self._inflight.get(cache_key)
            if in_flight is None:
                in_flight = threading.Event()
                self._inflight[cache_key] = in_flight
                is_owner = True
            else:
                is_owner = False

        if not is_owner:
            in_flight.wait()
            tmdb_id = self._cache.get(cache_key)
            if tmdb_id is not None:
                return MatchResult(tmdb_id=tmdb_id, resolution_kind="cache")
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
            )

        try:
            result = self._try_resolve(item)
        except Exception:
            with self._lock:
                self._inflight.pop(cache_key, None)
                in_flight.set()
            raise

        with self._lock:
            self._cache[cache_key] = result.tmdb_id
            if result.tmdb_id is None:
                self._failed_cache[cache_key] = time.time()
                self._failed_reason_cache[cache_key] = result.unresolved_reason or "not_found"
            else:
                self._failed_cache.pop(cache_key, None)
                self._failed_reason_cache.pop(cache_key, None)
            self._inflight.pop(cache_key, None)
            in_flight.set()
            return result

    def _record_match_stat(self, result: MatchResult) -> None:
        if result.tmdb_id is not None:
            if result.resolution_kind == "direct_tmdb":
                self._stats["direct_hits"] += 1
            elif result.resolution_kind in {"external_mapping", "fribb_exact"}:
                self._stats["mapping_hits"] += 1
            elif result.resolution_kind == "root_series":
                self._stats["root_hits"] += 1
            return
        if result.unresolved_reason == "lookup_unavailable":
            self._stats["lookup_unavailable"] += 1
        elif result.unresolved_reason == "missing_ids":
            self._stats["missing_id_failures"] += 1
        else:
            self._stats["not_found_failures"] += 1

    def _lookup_external_mapping(
        self,
        id_type: str,
        ext_id: str,
        media_type: str,
        title_variants=None,
    ) -> tuple[int | None, str, int, str]:
        """Return (tmdb_id, status, votes, title) from PMDB external-ID mapping lookup.

        ``votes`` is the community vote count on the chosen entry.  Zero means
        the mapping was submitted but never independently verified.

        PMDB can return several candidate mappings for one external ID.  When
        ``title_variants`` is supplied, the highest-voted candidate whose title is
        compatible with one of them is chosen instead of simply the highest-voted
        one — otherwise a wrong top-voted mapping hides a correct lower-voted
        mapping behind it and the item is lost.  With no variants supplied the
        behaviour is unchanged: the top-voted candidate wins.
        """
        try:
            detailed_lookup = getattr(self._pmdb, "lookup_by_external_id_detailed", None)
            if callable(detailed_lookup):
                detail = detailed_lookup(id_type, ext_id, media_type) or {}
                try:
                    _votes = int(detail.get("votes") or 0)
                except (TypeError, ValueError):
                    _votes = 0
                best_tmdb_id = _coerce_tmdb_id(detail.get("tmdb_id"), media_type)
                best_title = str(detail.get("title") or "")

                candidates = detail.get("candidates")
                if title_variants and isinstance(candidates, (list, tuple)) and len(candidates) > 1:
                    if not self._titles_are_compatible_any(title_variants, best_title):
                        for candidate in candidates:
                            if not isinstance(candidate, dict):
                                continue
                            candidate_tmdb_id = _coerce_tmdb_id(candidate.get("tmdb_id"), media_type)
                            candidate_title = str(candidate.get("title") or "")
                            if not candidate_tmdb_id or candidate_tmdb_id == best_tmdb_id:
                                continue
                            if self._titles_are_compatible_any(title_variants, candidate_title):
                                try:
                                    candidate_votes = int(candidate.get("votes") or 0)
                                except (TypeError, ValueError):
                                    candidate_votes = 0
                                logger.info(
                                    "[resolve] PMDB %s=%s: top-voted tmdb=%s title=%r is"
                                    " incompatible; using lower-voted tmdb=%d title=%r"
                                    " (votes=%d)",
                                    id_type, ext_id, best_tmdb_id, best_title,
                                    candidate_tmdb_id, candidate_title, candidate_votes,
                                )
                                return candidate_tmdb_id, str(detail.get("status") or "hit"), candidate_votes, candidate_title

                return (
                    best_tmdb_id,
                    str(detail.get("status") or "miss"),
                    _votes,
                    best_title,
                )
            tmdb_id = self._pmdb.lookup_by_external_id(id_type, ext_id, media_type)
            tmdb_id = _coerce_tmdb_id(tmdb_id, media_type)
            return tmdb_id, "hit" if tmdb_id else "miss", 0, ""
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in {401, 403}:
                logger.warning(
                    "PMDB mapping lookup failed for %s=%s (%s) with %s; treating mapping lookup as unavailable",
                    id_type,
                    ext_id,
                    media_type,
                    status_code,
                )
                return None, "lookup_unavailable", 0, ""
            raise

    @staticmethod
    def _normalized_title_tokens(title: str) -> set[str]:
        # Decompose to NFKD and drop combining marks so transliterations that
        # differ only in diacritics tokenize identically (e.g. "Jūjutsu" and
        # "Jujutsu", "Pokémon" and "Pokemon").
        decomposed = unicodedata.normalize("NFKD", str(title or ""))
        folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
        cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in folded)
        return {
            token for token in cleaned.split()
            if token and token not in _TITLE_STOPWORDS and not token.isdigit()
        }

    @classmethod
    def _titles_are_compatible(cls, source_title: str, mapped_title: str) -> bool:
        return cls._titles_are_compatible_any([source_title], mapped_title)

    @classmethod
    def _titles_are_compatible_any(cls, source_titles, mapped_title: str) -> bool:
        """True when *any* known title variant is compatible with the mapping.

        Anime carries several equally-legitimate titles — romaji, English,
        native and a list of synonyms — and providers disagree about which one to
        report.  Comparing a single variant means a romaji source title against
        an English mapped title (e.g. "Shingeki no Kyojin" vs "Attack on Titan")
        shares no tokens at all and the *correct* mapping gets rejected.  Trying
        every variant can only recover items: a mapping the single-variant check
        already accepted still matches on that same variant.

        Note an empty ``mapped_title`` is deliberately treated as compatible —
        PMDB omits the title on some records, and treating "unknown" as a
        mismatch would reject correct mappings, which is the opposite of intent.
        """
        if not mapped_title:
            return True
        mapped_tokens = cls._normalized_title_tokens(mapped_title)
        if not mapped_tokens:
            return True

        candidates = [t for t in (source_titles or []) if str(t or "").strip()]
        if not candidates:
            return True

        saw_tokens = False
        for source_title in candidates:
            source_tokens = cls._normalized_title_tokens(source_title)
            if not source_tokens:
                continue
            saw_tokens = True
            if source_tokens == mapped_tokens:
                return True
            overlap = len(source_tokens & mapped_tokens)
            if not overlap:
                continue
            union = len(source_tokens | mapped_tokens)
            if (overlap / max(1, union)) >= 0.75:
                return True
        # No variant produced comparable tokens at all — nothing to judge on.
        return not saw_tokens

    @staticmethod
    def _title_variants(item: dict, *, primary: str = "") -> list[str]:
        """Collect every known title for an item, most-canonical first.

        ``title_variants`` is populated by the provider clients from the titles
        they already fetch (romaji/english/native plus AniList synonyms), so no
        extra API calls are needed to widen the comparison.
        """
        variants: list[str] = []
        seen: set[str] = set()
        raw = [primary, item.get("title"), item.get("root_title")]
        extra = item.get("title_variants")
        if isinstance(extra, (list, tuple, set)):
            raw.extend(extra)
        elif isinstance(extra, str):
            raw.append(extra)
        for candidate in raw:
            text = str(candidate or "").strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            variants.append(text)
        return variants

    def _try_resolve(self, item: dict) -> MatchResult:
        title = item.get("title", "Unknown")
        media_type = item["media_type"]
        is_anime = item.get("simkl_type") == "anime"
        ids = item.get("ids", {})
        title_variants = self._title_variants(item, primary=title)
        anime_resolve_mode = self._anime_resolve_mode(item)
        allow_root_series = bool(item.get("allow_root_series")) or anime_resolve_mode in {"history_identity", "resume_identity"}

        if item.get("prefer_root_series") or allow_root_series:
            root_result = self._resolve_root_series(item, ids, media_type)
            if root_result.tmdb_id:
                with self._lock:
                    self._record_match_stat(root_result)
                return root_result

        if is_anime:
            year = item.get("year", "")
            anilist_id = item.get("anilist_id") or ids.get("anilist") or ""
            mal_id_log = item.get("mal_id") or ids.get("mal") or ""
            anidb_id_log = item.get("anidb_id") or ids.get("anidb") or ""
            logger.info(
                "[resolve] anime '%s' (%s) media_type=%s mode=%s  "
                "anilist=%s mal=%s anidb=%s",
                title, year, media_type, anime_resolve_mode,
                anilist_id, mal_id_log, anidb_id_log,
            )

            # For list_identity: try PMDB external mapping FIRST — it has per-show
            # accurate TMDB IDs (e.g. Shippuden→1553, Boruto→65334). Fribb often
            # stores the franchise root TMDB for sequels, causing false duplicates.
            # However we apply two safety filters:
            #   1. Block known-bad TMDB IDs that have polluted PMDB community data.
            #   2. Reject zero-vote (unconfirmed, self-submitted) mappings: try Fribb
            #      first; only fall back to the 0-vote PMDB result if Fribb also fails.
            if anime_resolve_mode == "list_identity":
                pmdb_candidate: int | None = None
                pmdb_candidate_source: str = ""
                pmdb_candidate_ext_id: str = ""
                pmdb_candidate_votes: int = 0
                # First blocked ID seen is surfaced as a candidate hint in the
                # unresolved summary so the user can understand why their anime
                # is stuck and manually pick a better TMDB entry.
                blocked_candidate: int | None = None
                for id_type, item_key in self._lookup_chain_for_item(item):
                    ext_id = item.get(item_key) or ids.get(id_type) or ids.get(item_key)
                    if not ext_id:
                        continue
                    ext_id = str(ext_id)
                    tmdb_id, status, votes, mapped_title = self._lookup_external_mapping(
                        id_type, ext_id, media_type, title_variants=title_variants,
                    )
                    if not tmdb_id:
                        logger.debug(
                            "[resolve] anime '%s' PMDB %s=%s → miss (status=%s)",
                            title, id_type, ext_id, status,
                        )
                        continue
                    # Reject known-bad TMDB IDs (confirmed wrong community mappings).
                    if tmdb_id in _BLOCKED_ANIME_PMDB_TMDB_IDS:
                        logger.warning(
                            "[resolve] anime '%s' — PMDB %s=%s returned blocked TMDB %d"
                            " (known bad mapping); skipping",
                            title, id_type, ext_id, tmdb_id,
                        )
                        if blocked_candidate is None:
                            blocked_candidate = tmdb_id
                        continue
                    logger.info(
                        "[resolve] anime '%s' PMDB %s=%s → tmdb=%d (votes=%d title=%r)",
                        title, id_type, ext_id, tmdb_id, votes, mapped_title,
                    )
                    if mapped_title and not self._titles_are_compatible_any(title_variants, mapped_title):
                        logger.warning(
                            "[resolve] anime '%s' — PMDB %s=%s returned incompatible title %r for tmdb=%d"
                            " (checked %d title variant(s)); skipping",
                            title, id_type, ext_id, mapped_title, tmdb_id, len(title_variants),
                        )
                        continue
                    if votes > 0:
                        # Community-verified mapping → accept immediately.
                        result = MatchResult(
                            tmdb_id=tmdb_id,
                            resolution_kind="external_mapping",
                            match_confidence="verified",
                            anime_mapping_source=id_type,
                        )
                        with self._lock:
                            self._record_match_stat(result)
                        return result
                    # Zero-vote mapping: stash as candidate and check Fribb first.
                    if pmdb_candidate is None:
                        pmdb_candidate = tmdb_id
                        pmdb_candidate_source = id_type
                        pmdb_candidate_ext_id = ext_id
                        pmdb_candidate_votes = votes

                # Fribb exact lookup — primary for non-list_identity, cross-check here.
                fribb_result = self._try_exact_anime_fribb_lookup(item)
                if fribb_result.tmdb_id:
                    logger.info(
                        "[resolve] anime '%s' Fribb → tmdb=%d (source=%s)",
                        title, fribb_result.tmdb_id, fribb_result.anime_mapping_source,
                    )
                    if pmdb_candidate and pmdb_candidate != fribb_result.tmdb_id:
                        logger.warning(
                            "[resolve] anime '%s' — Fribb tmdb=%d disagrees with"
                            " PMDB %s=%s tmdb=%d (votes=%d); preferring Fribb",
                            title, fribb_result.tmdb_id,
                            pmdb_candidate_source, pmdb_candidate_ext_id,
                            pmdb_candidate, pmdb_candidate_votes,
                        )
                    with self._lock:
                        self._record_match_stat(fribb_result)
                    return fribb_result

                # Fribb has no entry and PMDB result is unconfirmed (0 votes).
                # We cannot verify this mapping — returning it risks polluting PMDB
                # with wrong entries (e.g. "An Affirmative Act" for an anime item).
                # Mark as unresolved so the user can manually confirm via the UI.
                if pmdb_candidate is not None:
                    logger.warning(
                        "[resolve] anime '%s' — PMDB %s=%s returned unconfirmed tmdb=%d"
                        " (0 votes) and Fribb has no entry; marking unresolved"
                        " to avoid polluting PMDB with wrong mapping",
                        title, pmdb_candidate_source, pmdb_candidate_ext_id, pmdb_candidate,
                    )
                    result = MatchResult(
                        tmdb_id=None,
                        resolution_kind="unresolved",
                        unresolved_reason="unconfirmed_mapping",
                        match_confidence="unresolved",
                        anime_mapping_source=pmdb_candidate_source,
                        candidate_tmdb_id=pmdb_candidate,
                    )
                    with self._lock:
                        self._record_match_stat(result)
                    return result

                logger.info(
                    "[resolve] anime '%s' — PMDB miss, Fribb miss → unresolved",
                    title,
                )
                unresolved_reason = fribb_result.unresolved_reason or "missing_anime_mapping"
                if unresolved_reason == "not_found":
                    unresolved_reason = "missing_anime_mapping"
                # Surface the blocked ID (if any) as the candidate hint so the
                # unresolved panel shows what was rejected and why.
                candidate = fribb_result.candidate_tmdb_id or blocked_candidate
                result = MatchResult(
                    tmdb_id=None,
                    resolution_kind="unresolved",
                    unresolved_reason=unresolved_reason,
                    match_confidence=fribb_result.match_confidence or "unresolved",
                    anime_mapping_source=fribb_result.anime_mapping_source or "fribb_exact",
                    candidate_tmdb_id=candidate,
                )
                # This is the most common anime failure mode, and it was the one
                # terminal return that skipped the stat, under-counting it.
                with self._lock:
                    self._record_match_stat(result)
                return result

            # Fribb exact lookup — primary for non-list_identity modes.
            fribb_result = self._try_exact_anime_fribb_lookup(item)
            if fribb_result.tmdb_id:
                logger.info(
                    "[resolve] anime '%s' Fribb → tmdb=%d (mode=%s source=%s)",
                    title, fribb_result.tmdb_id, anime_resolve_mode,
                    fribb_result.anime_mapping_source,
                )
                with self._lock:
                    self._record_match_stat(fribb_result)
                return fribb_result

        # Do NOT collapse anime list identity to the franchise root up front.
        # Root-chain lookups are useful for history remapping, but for title/list
        # sync they can incorrectly turn Boruto into Naruto or UBW into Fate/Zero.
        # Prefer direct external mappings and verified direct TMDB first.

        if not is_anime:
            tmdb_raw = item.get("tmdb_id")
            tmdb_id = _coerce_tmdb_id(tmdb_raw, media_type)
            if tmdb_id:
                try:
                    logger.debug("Resolved '%s' via direct TMDB ID: %d", title, tmdb_id)
                    result = MatchResult(
                        tmdb_id=tmdb_id,
                        resolution_kind="direct_tmdb",
                        match_confidence="verified",
                        anime_mapping_source="direct_tmdb" if is_anime else None,
                    )
                    with self._lock:
                        self._record_match_stat(result)
                    return result
                except (ValueError, TypeError):
                    pass

        had_lookup_candidate = False
        lookup_unavailable = False

        for id_type, item_key in self._lookup_chain_for_item(item):
            ext_id = item.get(item_key) or ids.get(id_type) or ids.get(item_key)
            if not ext_id:
                continue
            had_lookup_candidate = True
            ext_id = str(ext_id)
            tmdb_id, status, _votes, _mapped_title = self._lookup_external_mapping(
                id_type, ext_id, media_type,
                title_variants=title_variants if is_anime else None,
            )
            if tmdb_id:
                # For anime items, apply the same safety guards used in list_identity
                # mode so wrong community mappings can't enter the library via the
                # history/resume/generic fallback path either.
                if is_anime:
                    if tmdb_id in _BLOCKED_ANIME_PMDB_TMDB_IDS:
                        logger.warning(
                            "[resolve] anime '%s' (mode=%s) — fallback: PMDB %s=%s"
                            " returned blocked TMDB %d (known bad mapping); skipping",
                            title, anime_resolve_mode, id_type, ext_id, tmdb_id,
                        )
                        continue
                    if _mapped_title and not self._titles_are_compatible_any(title_variants, _mapped_title):
                        logger.warning(
                            "[resolve] anime '%s' (mode=%s) — fallback: PMDB %s=%s"
                            " returned incompatible title %r for TMDB %d"
                            " (checked %d title variant(s)); skipping",
                            title, anime_resolve_mode, id_type, ext_id,
                            _mapped_title, tmdb_id, len(title_variants),
                        )
                        continue
                    if _votes == 0:
                        fribb_check = self._try_exact_anime_fribb_lookup(item)
                        if fribb_check.tmdb_id:
                            logger.info(
                                "[resolve] anime '%s' (mode=%s) — 0-vote PMDB %s=%s"
                                " overridden by Fribb → tmdb=%d",
                                title, anime_resolve_mode, id_type, ext_id,
                                fribb_check.tmdb_id,
                            )
                            with self._lock:
                                self._record_match_stat(fribb_check)
                            return fribb_check
                    logger.info(
                        "[resolve] anime '%s' (mode=%s) — fallback: PMDB %s=%s → tmdb=%d"
                        " (votes=%d title=%r) accepted",
                        title, anime_resolve_mode, id_type, ext_id, tmdb_id,
                        _votes, _mapped_title,
                    )
                else:
                    logger.debug("Resolved '%s' via %s lookup (%s -> %d)", title, id_type, ext_id, tmdb_id)
                result = MatchResult(
                    tmdb_id=tmdb_id,
                    resolution_kind="external_mapping",
                    match_confidence="verified",
                    anime_mapping_source=id_type if is_anime else None,
                )
                with self._lock:
                    self._record_match_stat(result)
                return result
            if status == "lookup_unavailable":
                lookup_unavailable = True

        if (not is_anime and not item.get("prefer_root_series")) or (is_anime and allow_root_series):
            root_result = self._resolve_root_series(item, ids, media_type)
            if root_result.tmdb_id:
                with self._lock:
                    self._record_match_stat(root_result)
                return root_result
            if root_result.unresolved_reason == "lookup_unavailable":
                lookup_unavailable = True

        tmdb_raw = item.get("tmdb_id")
        tmdb_id = _coerce_tmdb_id(tmdb_raw, media_type)
        if tmdb_id:
            if not is_anime or self._can_accept_anime_direct_tmdb(item):
                try:
                    logger.debug("Resolved '%s' via direct TMDB ID: %d", title, tmdb_id)
                    result = MatchResult(
                        tmdb_id=tmdb_id,
                        resolution_kind="direct_tmdb",
                        match_confidence="verified",
                        anime_mapping_source="direct_tmdb" if is_anime else None,
                    )
                    with self._lock:
                        self._record_match_stat(result)
                    return result
                except (ValueError, TypeError):
                    pass
            elif had_lookup_candidate:
                logger.warning(
                    "Skipping direct TMDB fallback for SIMKL anime '%s' because its AniList/MAL identity could not be verified",
                    title,
                )

        if not had_lookup_candidate and not item.get("tmdb_id"):
            result = MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason="missing_ids",
                match_confidence="unresolved",
            )
            with self._lock:
                self._record_match_stat(result)
            return result

        logger.warning(
            "Could not resolve TMDB ID for '%s' (year=%s, ids=%s)",
            title,
            item.get("year"),
            {k: v for k, v in ids.items() if v},
        )
        result = MatchResult(
            tmdb_id=None,
            resolution_kind="unresolved",
            unresolved_reason="lookup_unavailable" if lookup_unavailable else "not_found",
            match_confidence="unresolved",
        )
        with self._lock:
            self._record_match_stat(result)
        return result

    def _resolve_root_series(self, item: dict, ids: dict, media_type: str) -> MatchResult:
        title = item.get("title", "Unknown")
        root_title = item.get("root_title") or title
        root_title_variants = self._title_variants(item, primary=root_title)
        lookup_unavailable = False
        for id_type, item_key in _ROOT_LOOKUP_CHAIN:
            ext_id = item.get(item_key) or ids.get(item_key) or ids.get(f"root_{id_type}")
            if not ext_id:
                continue
            ext_id = str(ext_id)
            tmdb_id, status, _votes, mapped_title = self._lookup_external_mapping(
                id_type, ext_id, media_type, title_variants=root_title_variants,
            )
            if tmdb_id:
                if tmdb_id in _BLOCKED_ANIME_PMDB_TMDB_IDS:
                    logger.warning(
                        "[resolve] root-series '%s' — PMDB %s=%s returned blocked TMDB %d"
                        " (known bad mapping); skipping",
                        title, id_type, ext_id, tmdb_id,
                    )
                    continue
                if mapped_title and not self._titles_are_compatible_any(root_title_variants, mapped_title):
                    logger.warning(
                        "[resolve] root-series '%s' — PMDB %s=%s returned incompatible title %r"
                        " for tmdb=%d (checked %d title variant(s)); skipping",
                        title, id_type, ext_id, mapped_title, tmdb_id, len(root_title_variants),
                    )
                    continue
                logger.info(
                    "Resolved '%s' via root-series %s lookup (%s -> %d, root='%s')",
                    title, id_type, ext_id, tmdb_id,
                    item.get("root_title") or "Unknown",
                )
                return MatchResult(
                    tmdb_id=tmdb_id,
                    resolution_kind="root_series",
                    match_confidence="verified",
                    anime_mapping_source="root_chain",
                )
            if status == "lookup_unavailable":
                lookup_unavailable = True
        return MatchResult(
            tmdb_id=None,
            resolution_kind="unresolved",
            unresolved_reason="lookup_unavailable" if lookup_unavailable else "not_found",
            match_confidence="unresolved",
            anime_mapping_source="root_chain",
        )

    def _try_exact_anime_fribb_lookup(self, item: dict) -> MatchResult:
        """Use exact AniList/MAL anime-lists mappings before broader fallback logic."""
        from . import fribb_client

        ids = item.get("ids", {})
        entry = None

        lookup_order = (
            ("anilist", item.get("anilist_id") or ids.get("anilist"), fribb_client.lookup_by_anilist),
            ("mal", item.get("mal_id") or ids.get("mal"), fribb_client.lookup_by_mal),
            ("anidb", item.get("anidb_id") or ids.get("anidb"), fribb_client.lookup_by_anidb),
            ("simkl", ids.get("simkl"), fribb_client.lookup_by_simkl),
            ("imdb", item.get("imdb_id") or ids.get("imdb"), fribb_client.lookup_by_imdb),
        )
        exact_source = None
        exact_value = None
        for source_name, raw_value, lookup_fn in lookup_order:
            if not raw_value:
                continue
            try:
                lookup_value = int(raw_value) if source_name != "imdb" else str(raw_value)
                entry = lookup_fn(lookup_value)
            except (TypeError, ValueError):
                entry = None
            if entry is not None:
                exact_source = source_name
                exact_value = raw_value
                break

        if not isinstance(entry, dict):
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason="not_found",
                match_confidence="unresolved",
                anime_mapping_source="fribb_exact",
            )

        item_media_type = str(item.get("media_type") or "").strip().lower()
        tmdb_id, mapped_media_type = fribb_client.extract_tmdb(
            entry.get("themoviedb_id") or entry.get("themoviedb")
        )
        if not tmdb_id:
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason="not_found",
                match_confidence="unresolved",
                anime_mapping_source="fribb_exact",
            )

        # The media type is taken from the key the mapping is stored under, not
        # from the entry's `type` field.  `type` disagrees with the actual TMDB
        # namespace in both directions — hundreds of MOVIE-typed entries carry a
        # `tv` id, and hundreds of OVA/SPECIAL/ONA entries carry a `movie` id —
        # so gating on it rejects correct mappings for anime films, OVAs and
        # specials alike.  Only a genuine namespace conflict is a rejection.
        if mapped_media_type and item_media_type and item_media_type != mapped_media_type:
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason="not_found",
                match_confidence="ambiguous",
                anime_mapping_source="fribb_exact",
                candidate_tmdb_id=tmdb_id,
            )

        logger.debug(
            "Resolved anime '%s' via exact Fribb mapping (%s -> %d)",
            item.get("title", "Unknown"),
            exact_value or item.get("anilist_id") or item.get("mal_id") or ids.get("mal"),
            tmdb_id,
        )
        return MatchResult(
            tmdb_id=tmdb_id,
            resolution_kind="fribb_exact",
            match_confidence="exact",
            anime_mapping_source=f"fribb_exact:{exact_source}" if exact_source else "fribb_exact",
        )

    def _can_accept_anime_direct_tmdb(self, item: dict) -> bool:
        """Allow raw TMDB only after the anime identity is verified."""
        if item.get("simkl_type") != "anime":
            return True
        identity_present = bool(
            item.get("anilist_id")
            or item.get("mal_id")
            or item.get("anidb_id")
            or (item.get("ids") or {}).get("anilist")
            or (item.get("ids") or {}).get("mal")
            or (item.get("ids") or {}).get("anidb")
        )
        try:
            tmdb_raw = item.get("tmdb_id")
            tmdb_id = _coerce_tmdb_id(tmdb_raw, item.get("media_type") or "")
            if tmdb_id:
                from . import fribb_client
                ids = item.get("ids") or {}
                lookup_order = (
                    (item.get("anilist_id") or ids.get("anilist"), fribb_client.lookup_by_anilist, False),
                    (item.get("mal_id") or ids.get("mal"), fribb_client.lookup_by_mal, False),
                    (item.get("anidb_id") or ids.get("anidb"), fribb_client.lookup_by_anidb, False),
                    (ids.get("simkl"), fribb_client.lookup_by_simkl, False),
                    (item.get("imdb_id") or ids.get("imdb"), fribb_client.lookup_by_imdb, True),
                )
                exact_entry = None
                for raw_id, lookup_fn, is_str in lookup_order:
                    if not raw_id:
                        continue
                    try:
                        exact_entry = lookup_fn(str(raw_id) if is_str else int(raw_id))
                    except (TypeError, ValueError):
                        exact_entry = None
                    if exact_entry is not None:
                        break
                if exact_entry is not None:
                    fribb_tmdb, mapped_media_type = fribb_client.extract_tmdb(
                        exact_entry.get("themoviedb_id") or exact_entry.get("themoviedb")
                    )
                    item_media_type = str(item.get("media_type") or "").strip().lower()
                    if mapped_media_type and item_media_type and item_media_type != mapped_media_type:
                        # The mapping is for the other TMDB namespace, so it
                        # cannot vouch for this item's raw id.
                        return False
                    if fribb_tmdb:
                        return tmdb_id == fribb_tmdb
        except Exception:
            logger.debug("Anime direct TMDB verification failed; trying softer fallback", exc_info=True)
        if not self._anime_root_resolver:
            return identity_present

        anilist_id: int | None = None
        mal_id: int | None = None
        try:
            if item.get("anilist_id"):
                anilist_id = int(item["anilist_id"])
            if item.get("mal_id"):
                mal_id = int(item["mal_id"])
        except (TypeError, ValueError):
            return identity_present and str(item.get("media_type") or "").strip().lower() == "movie"

        if not anilist_id and not mal_id:
            return identity_present and str(item.get("media_type") or "").strip().lower() == "movie"

        try:
            root_context = self._anime_root_resolver(anilist_id, mal_id)
        except Exception:
            logger.debug("Anime identity verification failed", exc_info=True)
            return identity_present and str(item.get("media_type") or "").strip().lower() == "movie"
        root = (root_context or {}).get("root") if isinstance(root_context, dict) else None
        return isinstance(root, dict) and bool(root.get("id"))

    @staticmethod
    def _lookup_chain_for_item(item: dict) -> list[tuple[str, str]]:
        if item.get("simkl_type") == "anime":
            return _ANIME_LOOKUP_CHAIN
        return _DEFAULT_LOOKUP_CHAIN

    @staticmethod
    def _anime_resolve_mode(item: dict) -> str:
        mode = str(item.get("anime_resolve_mode") or "").strip().lower()
        if mode in {"list_identity", "history_identity", "resume_identity"}:
            return mode
        return "generic"

    def _try_anime_root_lookup(self, item: dict, media_type: str) -> MatchResult:
        """Walk the AniList prequel chain and look up the root-series TMDB ID."""
        title = item.get("title", "Unknown")
        anilist_id: int | None = None
        mal_id: int | None = None
        try:
            if item.get("anilist_id"):
                anilist_id = int(item["anilist_id"])
            if item.get("mal_id"):
                mal_id = int(item["mal_id"])
        except (ValueError, TypeError):
            pass

        if not anilist_id and not mal_id:
            return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="missing_ids")

        root_context = self._anime_root_resolver(anilist_id, mal_id)
        root = (root_context or {}).get("root") if isinstance(root_context, dict) else None
        if not isinstance(root, dict):
            return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="not_found")

        # If root is the same item, it's already a root series — let direct lookup handle it.
        if root.get("id") and root.get("id") == anilist_id:
            return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="not_found")

        for id_type, root_key in [("mal", "idMal"), ("anilist", "id")]:
            root_ext_id = root.get(root_key)
            if root_ext_id:
                tmdb_id, status, _votes, _mapped_title = self._lookup_external_mapping(id_type, str(root_ext_id), media_type)
                if tmdb_id:
                    if tmdb_id in _BLOCKED_ANIME_PMDB_TMDB_IDS:
                        logger.warning(
                            "[resolve] root-chain '%s' — PMDB %s=%s returned blocked TMDB %d"
                            " (known bad mapping); skipping",
                            title, id_type, root_ext_id, tmdb_id,
                        )
                        continue
                    logger.info(
                        "Resolved '%s' via root-series %s lookup (%s -> %d, root='%s')",
                        title, id_type, root_ext_id, tmdb_id,
                        self._media_title(root),
                    )
                    return MatchResult(tmdb_id=tmdb_id, resolution_kind="root_series")
                if status == "lookup_unavailable":
                    return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="lookup_unavailable")
        return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="not_found")

    @staticmethod
    def _media_title(media: dict) -> str:
        titles = media.get("title") or {}
        if isinstance(titles, dict):
            return titles.get("english") or titles.get("romaji") or str(media.get("id", "?"))
        return str(titles) or str(media.get("id", "?"))

    @staticmethod
    def _cache_key(item: dict) -> str:
        ids = item.get("ids", {})
        # Prefer top-level ID fields (present in unresolved summaries and history
        # items) with fallback to the nested ids dict (present in raw SIMKL items).
        # This ensures the key is identical whether computed from a full item or a
        # reconstructed unresolved-item summary.
        anilist_id = item.get("anilist_id") or ids.get("anilist") or ""
        mal_id = item.get("mal_id") or ids.get("mal") or ""
        root_mal = item.get("root_mal_id") or ids.get("root_mal") or ""
        root_anilist = item.get("root_anilist_id") or ids.get("root_anilist") or ""
        simkl_id = item.get("simkl_id") or ids.get("simkl") or ""
        resolver_mode = str(item.get("anime_resolve_mode") or "")
        return (
            f"{item.get('media_type', '')}:"
            f"{resolver_mode}:"
            f"{simkl_id}:"
            f"{item.get('imdb_id', '')}:"
            f"{item.get('tmdb_id', '')}:"
            f"{mal_id}:"
            f"{anilist_id}:"
            f"{root_mal}:"
            f"{root_anilist}:"
            f"{item.get('title', '')}:{item.get('year', '')}"
        )
