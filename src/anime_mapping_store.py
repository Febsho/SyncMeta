"""Offline anime mapping store.

Combines two upstream sources used by aiometadata's resolver approach:

- Fribb anime-lists JSON for exact title/ID -> TMDB identity
- Anime-Lists XML for AniDB -> TVDB season/episode remapping

The store keeps local cache files under ``data/cache`` and refreshes them using
ETag-aware requests when possible.  All lookups are in-memory after the first
load so sync-time matching does not depend on live network calls.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ANIME_LISTS_XML_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-full.xml"
REQUEST_TIMEOUT = (5, 30)

# Re-check upstream for updates this often.  The check is ETag-conditional so
# if nothing changed GitHub returns 304 and we skip re-parsing entirely.
_REFRESH_INTERVAL_SECONDS: float = 7 * 24 * 3600  # 1 week

_CACHE_DIR = Path("data") / "cache"
_FRIBB_CACHE_PATH = _CACHE_DIR / "anime-list-full.json.cache"
_FRIBB_META_PATH = _CACHE_DIR / "anime-list-full.json.meta.json"
_XML_CACHE_PATH = _CACHE_DIR / "anime-list-full.xml.cache"
_XML_META_PATH = _CACHE_DIR / "anime-list-full.xml.meta.json"


def _safe_int(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, dict):
            extracted = value.get("tv") or value.get("movie") or next(iter(value.values()), None)
            if extracted is None or isinstance(extracted, dict):
                return None
            if isinstance(extracted, (list, tuple)):
                # Fribb stores movie ids as a list.  Only a single-element list is
                # unambiguous; anything longer must not be guessed at.
                if len(extracted) != 1:
                    return None
                extracted = extracted[0]
            return int(extracted)
        if isinstance(value, (list, tuple)):
            if len(value) != 1:
                return None
            return int(value[0])
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_tmdb(value: object) -> tuple[int | None, str | None]:
    """Return ``(tmdb_id, media_type)`` from a Fribb ``themoviedb_id`` value.

    The upstream feed always stores this as a dict keyed by media type, and the
    two keys carry different value shapes::

        {"tv": 26209}     -> (26209, "tv")
        {"movie": [128]}  -> (128, "movie")
        {"movie": [1, 2]} -> (None, "movie")   # ambiguous: refuse to guess

    The media type in the key is authoritative — far more so than the entry's
    ``type`` field, which disagrees with it in both directions (e.g. entries
    typed ``MOVIE`` that carry a ``tv`` id, and ``OVA``/``SPECIAL`` entries that
    carry a ``movie`` id).  Returning it lets callers validate an item's media
    type against the mapping instead of against ``type``.

    A bare scalar is tolerated for forward/backward compatibility, in which case
    the media type is unknown and ``None`` is returned alongside the id.
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, dict):
        for media_type in ("tv", "movie"):
            if media_type in value:
                return _safe_int(value.get(media_type)), media_type
        # Unknown key shape: extract what we can but claim no media type.
        return _safe_int(value), None
    return _safe_int(value), None


def _iter_imdb_ids(value: object) -> list[str]:
    """Normalize a Fribb ``imdb_id`` (always a list upstream) to clean strings."""
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for candidate in raw:
        text = str(candidate or "").strip()
        if text:
            out.append(text)
    return out


class AnimeMappingStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fribb_loaded = False
        self._xml_loaded = False
        self._fribb_last_checked: float = 0.0   # unix timestamp of last upstream check
        self._xml_last_checked: float = 0.0
        self._fribb_by_anilist: dict[int, dict] = {}
        self._fribb_by_mal: dict[int, dict] = {}
        self._fribb_by_anidb: dict[int, dict] = {}
        self._fribb_by_simkl: dict[int, dict] = {}
        self._fribb_by_tmdb: dict[int, list[dict]] = {}
        self._fribb_by_imdb: dict[str, list[dict]] = {}
        self._fribb_by_tvdb: dict[int, list[dict]] = {}
        self._xml_by_anidb: dict[int, ET.Element] = {}
        self._xml_by_tvdb: dict[int, list[ET.Element]] = {}
        self._xml_by_tmdb: dict[int, list[ET.Element]] = {}
        self._xml_by_imdb: dict[str, list[ET.Element]] = {}
        self._same_season_group_cache: dict[tuple[str, str], dict] = {}
        self._mapping_string_cache: dict[str, list[dict]] = {}
        #: Why a source is not loaded. Without this the admin panel can only say
        #: "Not loaded", which is indistinguishable from "nothing asked for it
        #: yet" — and a mapping that silently fails to load looks exactly like
        #: anime that simply cannot be matched.
        self._load_errors: dict[str, str] = {}
        self._prewarm_started = False

    def lookup_fribb(
        self,
        *,
        anilist_id: int | None = None,
        mal_id: int | None = None,
        anidb_id: int | None = None,
        simkl_id: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
    ) -> dict | None:
        self._ensure_fribb_loaded()
        if anilist_id:
            entry = self._fribb_by_anilist.get(int(anilist_id))
            if entry:
                return entry
        if mal_id:
            entry = self._fribb_by_mal.get(int(mal_id))
            if entry:
                return entry
        if anidb_id:
            entry = self._fribb_by_anidb.get(int(anidb_id))
            if entry:
                return entry
        if simkl_id:
            entry = self._fribb_by_simkl.get(int(simkl_id))
            if entry:
                return entry
        if tmdb_id:
            entries = self._fribb_by_tmdb.get(int(tmdb_id)) or []
            if len(entries) == 1:
                return entries[0]
        if imdb_id:
            entries = self._fribb_by_imdb.get(str(imdb_id).strip()) or []
            if len(entries) == 1:
                return entries[0]
        if tvdb_id:
            entries = self._fribb_by_tvdb.get(int(tvdb_id)) or []
            if len(entries) == 1:
                return entries[0]
        return None

    def lookup_fribb_entries_by_tmdb(self, tmdb_id: int) -> list[dict]:
        """Return every anime entry belonging to a TMDB title.

        A TMDB TV title can represent several AniList/SIMKL titles (one per
        season or cour).  ``lookup_fribb(tmdb_id=...)`` deliberately only
        returns unambiguous identities, but writers need the complete set so
        they can select the entry matching a canonical TMDB season.
        """
        self._ensure_fribb_loaded()
        return list(self._fribb_by_tmdb.get(int(tmdb_id)) or [])

    def validate_tmdb(self, entry: dict | None, tmdb_id: int | None) -> bool:
        if not entry or not tmdb_id:
            return False
        mapped, _media_type = extract_tmdb(
            entry.get("themoviedb_id") or entry.get("themoviedb")
        )
        return bool(mapped and mapped == int(tmdb_id))

    def resolve_tvdb_episode_from_anidb_episode(
        self,
        anidb_id: int,
        anidb_episode: int,
        anidb_season: int = 1,
    ) -> dict | None:
        """Resolve an AniDB episode to TVDB — and to TMDB when the feed says so.

        The result always carries ``tvdb_*`` keys.  It additionally carries
        ``tmdb_season`` / ``tmdb_episode`` / ``tmdb_id`` when the entry records
        TMDB coordinates, letting callers skip translating the TVDB season
        through PMDB entirely.
        """
        result = self._resolve_tvdb_episode(anidb_id, anidb_episode, anidb_season)
        if not result:
            return result
        return self._with_tmdb_coordinates(result, anidb_id, anidb_episode)

    def _with_tmdb_coordinates(self, result: dict, anidb_id: int, anidb_episode: int) -> dict:
        """Attach TMDB coordinates to a TVDB resolution when the feed has them.

        Only applied when the entry-level ``episodeoffset`` produced the result.
        Per-range ``mapping-list`` offsets and absolute numbering are TVDB-specific
        and have no TMDB counterpart upstream, so those keep the TVDB path.
        """
        anime_entry = self._xml_by_anidb.get(int(anidb_id))
        if anime_entry is None:
            return result
        attrs = anime_entry.attrib
        tmdb_season = _safe_int(attrs.get("tmdbseason"))
        if tmdb_season is None:
            return result
        entry_offset = _safe_int(attrs.get("episodeoffset")) or 0
        if _safe_int(result.get("tvdb_episode")) != int(anidb_episode) + entry_offset:
            return result

        enriched = dict(result)
        enriched["tmdb_season"] = tmdb_season
        enriched["tmdb_episode"] = int(anidb_episode) + (_safe_int(attrs.get("tmdboffset")) or 0)
        tmdb_id = _safe_int(attrs.get("tmdbtv")) or _safe_int(attrs.get("tmdbid"))
        if tmdb_id:
            enriched["tmdb_id"] = tmdb_id
        return enriched

    def _resolve_tvdb_episode(
        self,
        anidb_id: int,
        anidb_episode: int,
        anidb_season: int = 1,
    ) -> dict | None:
        self._ensure_xml_loaded()
        anime_entry = self._xml_by_anidb.get(int(anidb_id))
        if anime_entry is None or anidb_episode <= 0:
            return None

        attrs = anime_entry.attrib
        tvdb_id = _safe_int(attrs.get("tvdbid"))
        if not tvdb_id:
            return None

        default_tvdb_season = attrs.get("defaulttvdbseason")
        episode_offset = _safe_int(attrs.get("episodeoffset")) or 0

        if default_tvdb_season == "a":
            return self._resolve_absolute_numbering(anime_entry, tvdb_id, anidb_season, anidb_episode)
        if default_tvdb_season == "0":
            return None

        try:
            tvdb_season = int(default_tvdb_season or 0)
        except (TypeError, ValueError):
            return None
        if tvdb_season <= 0:
            return None

        same_season_entries = self._get_same_season_group(anime_entry)
        if len(same_season_entries["same_season_entries"]) == 1:
            return {
                "tvdb_id": tvdb_id,
                "tvdb_season": tvdb_season,
                "tvdb_episode": anidb_episode + episode_offset,
            }

        for mapping in self._get_mapping_list(anime_entry):
            mapping_attrs = mapping.attrib
            mapping_anidb_season = _safe_int(mapping_attrs.get("anidbseason"))
            if mapping_anidb_season != anidb_season:
                continue
            start = _safe_int(mapping_attrs.get("start"))
            end = _safe_int(mapping_attrs.get("end"))
            offset = _safe_int(mapping_attrs.get("offset"))
            target_season = _safe_int(mapping_attrs.get("tvdbseason")) or tvdb_season
            if start is not None and end is not None and offset is not None:
                if start <= anidb_episode <= end:
                    return {
                        "tvdb_id": tvdb_id,
                        "tvdb_season": target_season,
                        "tvdb_episode": anidb_episode + offset,
                    }

        sorted_entries = same_season_entries["sorted_offset_entries"]
        current_index = same_season_entries["index_by_anidb_id"].get(int(attrs.get("anidbid") or 0))
        if current_index is not None:
            current_offset = sorted_entries[current_index]["offset"]
            next_offset = sorted_entries[current_index + 1]["offset"] if current_index + 1 < len(sorted_entries) else None
            if next_offset is not None and anidb_episode >= next_offset:
                return None
            return {
                "tvdb_id": tvdb_id,
                "tvdb_season": tvdb_season,
                "tvdb_episode": anidb_episode + current_offset,
            }
        return {
            "tvdb_id": tvdb_id,
            "tvdb_season": tvdb_season,
            "tvdb_episode": anidb_episode + episode_offset,
        }

    def get_xml_entries_by_tmdb(self, tmdb_id: int) -> list[ET.Element]:
        self._ensure_xml_loaded()
        return list(self._xml_by_tmdb.get(int(tmdb_id)) or [])

    def get_xml_entries_by_tvdb(self, tvdb_id: int) -> list[ET.Element]:
        self._ensure_xml_loaded()
        return list(self._xml_by_tvdb.get(int(tvdb_id)) or [])

    def cache_metadata(self) -> dict:
        """Return local cache status without doing network work."""
        with self._lock:
            return {
                "refresh_interval_seconds": int(_REFRESH_INTERVAL_SECONDS),
                "fribb": self._cache_file_metadata(
                    loaded=self._fribb_loaded,
                    last_checked=self._fribb_last_checked,
                    entries=len(self._fribb_by_anilist) if self._fribb_loaded else 0,
                    cache_path=_FRIBB_CACHE_PATH,
                    meta_path=_FRIBB_META_PATH,
                    source_url=FRIBB_URL,
                ),
                "anime_lists_xml": self._cache_file_metadata(
                    loaded=self._xml_loaded,
                    last_checked=self._xml_last_checked,
                    entries=len(self._xml_by_anidb) if self._xml_loaded else 0,
                    cache_path=_XML_CACHE_PATH,
                    meta_path=_XML_META_PATH,
                    source_url=ANIME_LISTS_XML_URL,
                ),
                "season_group_cache": len(self._same_season_group_cache),
                "mapping_string_cache": len(self._mapping_string_cache),
                "fribb_error": self._load_errors.get("fribb", ""),
                "xml_error": self._load_errors.get("anime_lists_xml", ""),
                "prewarm_started": self._prewarm_started,
            }

    def prewarm(self) -> dict:
        """Load both mappings now, tolerating failure.

        They are otherwise loaded lazily by the first anime lookup — which
        happens *inside* a sync, on a worker thread, holding the store lock
        while ~40MB of JSON and a large XML are fetched and indexed. Every other
        anime lookup in that run queues behind it, and if the download is slow
        the cost lands on the user's sync rather than on idle startup. Calling
        this from a background thread at boot moves that work off the sync path
        and makes the admin panel's "Loaded" state mean something.
        """
        with self._lock:
            self._prewarm_started = True
        results = {}
        for source_name, loader in (
            ("fribb", self._ensure_fribb_loaded),
            ("anime_lists_xml", self._ensure_xml_loaded),
        ):
            started = time.monotonic()
            try:
                loader()
                with self._lock:
                    self._load_errors.pop(source_name, None)
                results[source_name] = {
                    "ok": True,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            except Exception as exc:
                # Never raise: a mapping that cannot load must degrade matching,
                # not stop the app from starting.
                logger.warning("Anime mapping prewarm failed for %s: %s", source_name, exc)
                with self._lock:
                    self._load_errors[source_name] = str(exc)
                results[source_name] = {"ok": False, "error": str(exc)}
        return results

    def force_refresh(self) -> dict:
        """Force an ETag-aware upstream re-check while preserving good cached data."""
        started = time.monotonic()
        results: list[dict] = []
        for source_name, loader in (
            ("fribb", lambda: self._ensure_fribb_loaded(force=True)),
            ("anime_lists_xml", lambda: self._ensure_xml_loaded(force=True)),
        ):
            source_started = time.monotonic()
            try:
                loader()
                results.append({
                    "source": source_name,
                    "ok": True,
                    "duration_ms": int((time.monotonic() - source_started) * 1000),
                })
            except Exception as exc:
                results.append({
                    "source": source_name,
                    "ok": False,
                    "error": str(exc),
                    "duration_ms": int((time.monotonic() - source_started) * 1000),
                })
        return {
            "ok": all(item.get("ok") for item in results),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "results": results,
            "metadata": self.cache_metadata(),
        }

    @staticmethod
    def _cache_file_metadata(
        *,
        loaded: bool,
        last_checked: float,
        entries: int,
        cache_path: Path,
        meta_path: Path,
        source_url: str,
    ) -> dict:
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        cache_mtime = 0.0
        cache_size = 0
        if cache_path.exists():
            try:
                stat = cache_path.stat()
                cache_mtime = stat.st_mtime
                cache_size = stat.st_size
            except Exception:
                pass
        return {
            "loaded": bool(loaded),
            "entries": int(entries or 0),
            "source_url": str(meta.get("source_url") or source_url),
            "etag": str(meta.get("etag") or ""),
            "last_modified": str(meta.get("last_modified") or ""),
            "last_checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_checked)) if last_checked else "",
            "cache_updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cache_mtime)) if cache_mtime else "",
            "cache_size_bytes": int(cache_size or 0),
        }

    def _ensure_fribb_loaded(self, force: bool = False) -> None:
        """Load (or refresh) the Fribb mapping data.

        First call downloads and parses the full JSON.  Subsequent calls are
        no-ops until _REFRESH_INTERVAL_SECONDS has elapsed; then an ETag-
        conditional request is sent.  If GitHub returns 304 (no change) the
        in-memory data is kept as-is and only the check timestamp is updated.
        If new content is returned the index is rebuilt from scratch.
        """
        now = time.time()
        # Fast path: data loaded and not yet due for a refresh check.
        if not force and self._fribb_loaded and (now - self._fribb_last_checked) < _REFRESH_INTERVAL_SECONDS:
            return
        with self._lock:
            now = time.time()
            if not force and self._fribb_loaded and (now - self._fribb_last_checked) < _REFRESH_INTERVAL_SECONDS:
                return
            changed, data = self._load_or_download_json_with_change(
                FRIBB_URL, _FRIBB_CACHE_PATH, _FRIBB_META_PATH,
            )
            self._fribb_last_checked = time.time()
            if not changed and self._fribb_loaded:
                # ETag confirmed no upstream change — keep existing in-memory data.
                logger.debug("Fribb anime-lists unchanged (304); keeping in-memory index")
                return
            if not isinstance(data, list):
                if self._fribb_loaded:
                    logger.warning("Fribb refresh failed; keeping existing in-memory data")
                    return
                raise RuntimeError("Fribb anime map is unavailable")
            logger.info("(Re-)indexing Fribb anime-lists (%d entries)", len(data))
            self._fribb_by_anilist.clear()
            self._fribb_by_mal.clear()
            self._fribb_by_anidb.clear()
            self._fribb_by_simkl.clear()
            self._fribb_by_tmdb.clear()
            self._fribb_by_imdb.clear()
            self._fribb_by_tvdb.clear()
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                for field_name, target in (
                    ("anilist_id", self._fribb_by_anilist),
                    ("mal_id", self._fribb_by_mal),
                    ("anidb_id", self._fribb_by_anidb),
                    ("simkl_id", self._fribb_by_simkl),
                ):
                    entry_id = _safe_int(entry.get(field_name))
                    if entry_id:
                        target[entry_id] = entry
                tvdb_id = _safe_int(entry.get("tvdb_id"))
                if tvdb_id:
                    self._fribb_by_tvdb.setdefault(tvdb_id, []).append(entry)
                tmdb_id, _tmdb_media_type = extract_tmdb(
                    entry.get("themoviedb_id") or entry.get("themoviedb")
                )
                if tmdb_id:
                    self._fribb_by_tmdb.setdefault(tmdb_id, []).append(entry)
                # imdb_id is always a list upstream, and 46 entries carry more
                # than one.  Index every id so each resolves back to the entry.
                for imdb_id in _iter_imdb_ids(entry.get("imdb_id")):
                    self._fribb_by_imdb.setdefault(imdb_id, []).append(entry)
            self._fribb_loaded = True

    def _ensure_xml_loaded(self, force: bool = False) -> None:
        """Load (or refresh) the Anime-Lists XML mapping data."""
        now = time.time()
        if not force and self._xml_loaded and (now - self._xml_last_checked) < _REFRESH_INTERVAL_SECONDS:
            return
        with self._lock:
            now = time.time()
            if not force and self._xml_loaded and (now - self._xml_last_checked) < _REFRESH_INTERVAL_SECONDS:
                return
            changed, xml_text = self._load_or_download_text_with_change(
                ANIME_LISTS_XML_URL, _XML_CACHE_PATH, _XML_META_PATH,
            )
            self._xml_last_checked = time.time()
            if not changed and self._xml_loaded:
                logger.debug("Anime-Lists XML unchanged (304); keeping in-memory index")
                return
            if not xml_text:
                if self._xml_loaded:
                    logger.warning("Anime-Lists XML refresh failed; keeping existing in-memory data")
                    return
                raise RuntimeError("Anime-Lists XML is unavailable")
            logger.info("(Re-)parsing Anime-Lists XML")
            root = ET.fromstring(xml_text)
            self._xml_by_anidb.clear()
            self._xml_by_tvdb.clear()
            self._xml_by_tmdb.clear()
            self._xml_by_imdb.clear()
            self._same_season_group_cache.clear()
            self._mapping_string_cache.clear()
            for anime in root.findall("anime"):
                attrs = anime.attrib
                anidb_id = _safe_int(attrs.get("anidbid"))
                if anidb_id:
                    self._xml_by_anidb[anidb_id] = anime
                tvdb_id = _safe_int(attrs.get("tvdbid"))
                if tvdb_id:
                    self._xml_by_tvdb.setdefault(tvdb_id, []).append(anime)
                # tmdbtv is the series id; tmdbid is the movie id.  Index both so
                # anime films are reachable, not just series.
                for tmdb_attr in ("tmdbtv", "tmdbid"):
                    tmdb_id = _safe_int(attrs.get(tmdb_attr))
                    if tmdb_id:
                        self._xml_by_tmdb.setdefault(tmdb_id, []).append(anime)
                imdb_ids = str(attrs.get("imdbid") or "").strip()
                if imdb_ids:
                    for imdb_id in [part.strip() for part in imdb_ids.split(",") if part.strip() and part.strip() != "unknown"]:
                        self._xml_by_imdb.setdefault(imdb_id, []).append(anime)
            self._xml_loaded = True

    def _load_or_download_json_with_change(
        self, url: str, cache_path: Path, meta_path: Path,
    ) -> tuple[bool, object]:
        """Return (changed, parsed_data).  changed=False means a 304 was received."""
        changed, text = self._load_or_download_text_with_change(url, cache_path, meta_path)
        return changed, (json.loads(text) if text else None)

    @staticmethod
    def _load_or_download_text_with_change(
        url: str, cache_path: Path, meta_path: Path,
    ) -> tuple[bool, str]:
        """Fetch url with ETag conditional request.

        Returns (changed, text):
          changed=False → server returned 304; text is the existing cache content.
          changed=True  → new content was downloaded; text is the new content.
          On any error, falls back to the cache file with changed=True so the
          caller re-parses from disk (safe even if data didn't actually change).
        """
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        headers: dict[str, str] = {}
        etag = str(meta.get("etag") or "").strip()
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code == 304 and cache_path.exists():
                # Nothing changed upstream — caller can skip re-parsing.
                return False, cache_path.read_text(encoding="utf-8")
            response.raise_for_status()
            text = response.text
            cache_path.write_text(text, encoding="utf-8")
            next_meta = {
                "etag": response.headers.get("ETag") or response.headers.get("etag") or "",
                "last_modified": response.headers.get("Last-Modified") or response.headers.get("last-modified") or "",
                "source_url": url,
            }
            meta_path.write_text(json.dumps(next_meta, indent=2, sort_keys=True), encoding="utf-8")
            return True, text
        except Exception as exc:
            if cache_path.exists():
                logger.warning("Using cached anime mapping for %s after refresh failure: %s", url, exc)
                # Treat as changed so existing in-memory data is rebuilt from cache.
                return True, cache_path.read_text(encoding="utf-8")
            logger.warning("Failed to load anime mapping %s: %s", url, exc)
            return True, ""

    def _resolve_absolute_numbering(
        self,
        anime_entry: ET.Element,
        tvdb_id: int,
        anidb_season: int,
        anidb_episode: int,
    ) -> dict | None:
        for mapping in self._get_mapping_list(anime_entry):
            attrs = mapping.attrib
            mapping_anidb_season = _safe_int(attrs.get("anidbseason"))
            if mapping_anidb_season != anidb_season:
                continue
            tvdb_season = _safe_int(attrs.get("tvdbseason"))
            if not tvdb_season:
                continue
            offset = _safe_int(attrs.get("offset")) or 0
            start = _safe_int(attrs.get("start"))
            end = _safe_int(attrs.get("end"))
            if start is not None and end is not None:
                if start <= anidb_episode <= end:
                    return {
                        "tvdb_id": tvdb_id,
                        "tvdb_season": tvdb_season,
                        "tvdb_episode": anidb_episode + offset,
                    }
            elif start is not None and anidb_episode >= start:
                return {
                    "tvdb_id": tvdb_id,
                    "tvdb_season": tvdb_season,
                    "tvdb_episode": anidb_episode + offset,
                }
            else:
                mapping_ranges = self._get_cached_episode_mapping((mapping.text or "").strip())
                for mapping_range in mapping_ranges:
                    if mapping_range["start"] <= anidb_episode <= mapping_range["end"]:
                        return {
                            "tvdb_id": tvdb_id,
                            "tvdb_season": tvdb_season,
                            "tvdb_episode": anidb_episode + offset,
                        }
        return None

    def _get_mapping_list(self, anime_entry: ET.Element) -> list[ET.Element]:
        mapping_list = anime_entry.find("mapping-list")
        if mapping_list is None:
            return []
        return list(mapping_list.findall("mapping"))

    def _get_same_season_group(self, anime_entry: ET.Element) -> dict:
        attrs = anime_entry.attrib
        key = (str(attrs.get("tvdbid") or ""), str(attrs.get("defaulttvdbseason") or ""))
        cached = self._same_season_group_cache.get(key)
        if cached:
            return cached
        tvdb_id = _safe_int(attrs.get("tvdbid")) or 0
        default_season = str(attrs.get("defaulttvdbseason") or "")
        entries = [
            entry for entry in self._xml_by_tvdb.get(tvdb_id, [])
            if str(entry.attrib.get("defaulttvdbseason") or "") == default_season
        ]
        sorted_entries = sorted(entries, key=lambda entry: _safe_int(entry.attrib.get("episodeoffset")) or 0)
        sorted_offset_entries = [
            {"entry": entry, "offset": _safe_int(entry.attrib.get("episodeoffset")) or 0}
            for entry in sorted_entries
        ]
        index_by_anidb_id = {}
        for idx, entry in enumerate(sorted_entries):
            anidb_id = _safe_int(entry.attrib.get("anidbid"))
            if anidb_id:
                index_by_anidb_id[anidb_id] = idx
        group = {
            "same_season_entries": entries,
            "sorted_entries": sorted_entries,
            "sorted_offset_entries": sorted_offset_entries,
            "index_by_anidb_id": index_by_anidb_id,
        }
        self._same_season_group_cache[key] = group
        return group

    def _get_cached_episode_mapping(self, mapping_string: str) -> list[dict]:
        if not mapping_string:
            return []
        cached = self._mapping_string_cache.get(mapping_string)
        if cached is not None:
            return cached
        parsed: list[dict] = []
        for raw_part in mapping_string.split(";"):
            part = raw_part.strip()
            if not part or "-" not in part:
                continue
            start_raw, end_raw = [p.strip() for p in part.split("-", 1)]
            start = _safe_int(start_raw)
            end = _safe_int(end_raw)
            if start is not None and end is not None:
                parsed.append({"start": start, "end": end})
        self._mapping_string_cache[mapping_string] = parsed
        return parsed


_STORE = AnimeMappingStore()


def lookup_fribb(**kwargs) -> dict | None:
    return _STORE.lookup_fribb(**kwargs)


def validate_tmdb(entry: dict | None, tmdb_id: int | None) -> bool:
    return _STORE.validate_tmdb(entry, tmdb_id)


def cache_metadata() -> dict:
    return _STORE.cache_metadata()


def force_refresh() -> dict:
    return _STORE.force_refresh()


def prewarm() -> dict:
    return _STORE.prewarm()


def resolve_tvdb_episode_from_anidb_episode(
    anidb_id: int,
    anidb_episode: int,
    anidb_season: int = 1,
) -> dict | None:
    return _STORE.resolve_tvdb_episode_from_anidb_episode(anidb_id, anidb_episode, anidb_season)


def lookup_fribb_entries_by_tmdb(tmdb_id: int) -> list[dict]:
    return _STORE.lookup_fribb_entries_by_tmdb(int(tmdb_id))


def get_xml_entries_by_tmdb(tmdb_id: int) -> list[ET.Element]:
    return _STORE.get_xml_entries_by_tmdb(tmdb_id)


def get_xml_entries_by_tvdb(tvdb_id: int) -> list[ET.Element]:
    return _STORE.get_xml_entries_by_tvdb(tvdb_id)
