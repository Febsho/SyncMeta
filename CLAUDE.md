# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the app locally
python web.py                        # http://127.0.0.1:8080

# Run the whole suite — no exclusions needed
python -m unittest discover -v

# Run a single test file
python -m unittest tests.test_sync_service -v

# Run a single test
python -m unittest tests.test_matcher.ItemMatcherTests.test_anime_movie_resolves_via_fribb_list_valued_mapping -v
```

**There are no permanent test exclusions.** Earlier notes claimed two, but both
were symptoms of a broken distro `cryptography` install, not real defects. If
`tests/test_profile_store.py` or `tests/test_web.py` fail at import with
`pyo3_runtime.PanicException` (or `test_stop_sync_endpoint_marks_profile_as_stopping`
appears to fail), reinstall the wheel rather than skipping:

```bash
pip install --ignore-installed "cryptography>=42,<44"
pip install --ignore-installed blinker flask python-dotenv   # if flask/dotenv are missing
```

## Architecture

**Entry point:** `web.py` — Flask app. Owns the `ProfileStore` singleton, `SyncRunner` thread pool, APScheduler background scheduler, and all HTTP routes (`/status`, `/sync`, `/activity`, `/config`, `/admin/*`, etc.). Compresses JSON responses ≥512 bytes via `@app.after_request` gzip hook.

**Sync pipeline:** `src/sync_service.py` — `SyncService` is the main orchestrator. Called once per sync run:
1. `_sync_simkl` / `_sync_trakt` / `_sync_anilist` / `_sync_mdblist` — fetch each source's lists in parallel using `ThreadPoolExecutor`
2. `_sync_list` — writes a single merged list to PMDB, handles stale-item removal
3. `_sync_history` — syncs watch history
4. `_sync_resume` — syncs Trakt resume progress
5. `_sync_pmdb_watchlist` — syncs plan-to-watch into PMDB native watchlist

`SyncStats` dataclass tracks per-run counters plus `synced_keys: list[str]` (populated by `_sync_list` and used to persist `pmdb_watchlist_managed_keys`).

**ID resolution:** `src/matcher.py` — `ItemMatcher` resolves cross-provider IDs (TMDB ↔ SIMKL ↔ AniList ↔ MAL ↔ IMDB). Uses Fribb anime mapping (`src/fribb_client.py`), an anime prequel-chain cache (`src/anime_mapping_store.py`), and per-episode PMDB fallback. Thread-safe in-memory cache.

**Profile persistence:** `src/profile_store.py` — JSON-backed store (`/app/data/profiles.json`). Credentials are Fernet-encrypted (AES-128-CBC). `activity_state` dict persists per-profile runtime state: last-sync cursors/timestamps, `pmdb_watchlist_managed_keys` (keys SyncMeta previously wrote to PMDB watchlist — used to avoid removing manually-added entries).

**Config:** `src/config.py` — dataclass hierarchy: `AppConfig` contains `SimklConfig`, `TraktConfig`, `AniListConfig`, `MdbListConfig`, `PublicMetaDBConfig`, `SyncConfig`. `SyncConfig.pmdb_watchlist_managed_keys: list[str]` is the persisted set of watchlist keys written by SyncMeta.

**Clients:** One file per provider (`simkl_client.py`, `trakt_client.py`, `anilist_client.py`, `mdblist_client.py`, `publicmetadb_client.py`, `fribb_client.py`). Each handles auth, rate limiting, and API calls for its provider.

**Frontend:** `templates/index.html` — single-page app, no build step, vanilla JS. Key patterns:
- `fetchStatus(force)` polls `/status` every 2s during sync; has `_statusGeneration` counter to discard stale renders
- `_forceStatusRefresh()` bumps `_statusGeneration`, clears in-flight request, immediately re-fetches — called after every action button success
- All action buttons (`triggerSync`, `triggerActivitySync`, `saveProfile`, `loadProfile`) give immediate visual feedback (disable + label change) before any `await`, and restore on failure
- `fetchUnresolved()` is only called on `sync_running` transition (true→false), not on every poll

## Key Invariants

**PMDB Watchlist managed-keys filter:** `_remove_stale` in `sync_service.py` accepts `managed_keys: frozenset[str] | None`. If `managed_keys` is truthy (non-empty), only items whose key is in `managed_keys` are eligible for removal — this preserves manually-added PMDB entries. An empty frozenset (bootstrap/first-sync) is falsy and falls back to full-removal behavior. Keys are persisted in `activity_state.pmdb_watchlist_managed_keys` by `_merge_activity_results` in `profile_store.py` after each sync.

**Fribb feed shapes — do not assume scalars.** Verified against the live
`Fribb/anime-lists` feed (42,868 entries). Reading these as plain values is what
made every anime-movie and IMDB mapping silently unreachable:

| Field | Actual shape | Notes |
|---|---|---|
| `themoviedb_id` | **always** a dict: `{"tv": int}` or `{"movie": [int, ...]}` | Never a scalar. 22 entries carry more than one movie id. |
| `imdb_id` | **always** a list | 46 entries carry more than one. |
| `season` | `{"tvdb": N, "tmdb": N}` | Not `thetvdb_season`, which does not exist. |
| `episode_offset` | `{"tvdb": N, "tmdb": N}` | Not `thetvdb_epoffset`, which does not exist. |
| `themoviedb` | does not exist | Any reader of it is a dead branch. |

Always go through `anime_mapping_store.extract_tmdb()` (exposed as
`fribb_client.extract_tmdb`), which returns `(tmdb_id, media_type)`, and
`fribb_client.single_imdb_id()`. **Media type comes from the dict key, never from
the entry's `type` field** — `type` disagrees with the TMDB namespace in both
directions (715 `MOVIE`-typed entries carry a `tv` id; 386 `OVA`/`SPECIAL`/`ONA`
entries carry a `movie` id), so gating on it rejects correct mappings. An
ambiguous multi-valued id must resolve to `None` rather than being guessed at.

The anime-lists XML likewise carries `tmdbtv`, `tmdbseason`, `tmdboffset` and a
movie-side `tmdbid`; `resolve_tvdb_episode_from_anidb_episode` returns `tmdb_*`
keys when present so callers can skip the PMDB `anime-seasons` round trip.
Season 0 always means specials and is left untouched.

**Anime title matching is multi-variant.** `_titles_are_compatible_any` accepts
if *any* known title variant is compatible, because providers disagree about
language — a romaji source title against an English mapped title shares zero
tokens and a single-variant check rejected the correct mapping. Variants arrive
on the item as `title_variants` (AniList romaji/english/native + `synonyms`,
SIMKL alternates, and the slug-derived hint from `fribb_client.title_hints`).
Tokens are NFKD-folded so diacritics don't matter. An empty mapped title is
deliberately treated as compatible. Do not add digit or season-number comparison
as a rejection signal: digits are dropped on purpose so root-series matches
succeed.

**`candidate_tmdb_id` is a rejected mapping, not an answer.** It records what the
matcher declined (unverified zero-vote community data, or a blocklisted id) purely
as a hint for the user. Never return it as an automatic resolution — that
re-applies the mapping the safety guards refused.

**Log endpoints are session-scoped.** `/api/logs` and `/api/logs/clear` take the
profile from the session and ignore any caller-supplied value;
`log_capture.snapshot()` only filters when given a non-empty profile id, so an
empty one leaks every profile's logs. `log_capture.clear(profile_id)` must stay
scoped and must not reset the shared `_seq`, which would strand other clients'
cursors. Note the UI deliberately uses `log_capture` rather than the
`ProfileLogStore`-backed `/api/profile/logs`: the latter reads a contextvar, and
contextvars are not inherited by `ThreadPoolExecutor` workers, where the sync
pipeline does its work.

**Attribute escaping:** `esc()` escapes `&`, `<`, `>` only — it is for element
text. Use `escAttr()` inside quoted attributes, which also escapes quotes.
Provider ids and list names come from third-party APIs.

**Stale poll guard:** `_statusGeneration` is incremented before any forced status refresh. Each `fetchStatus` call captures the generation at start; if it differs when results arrive, the render is discarded — checked *before* any profile-identity or `localStorage` write, so a superseded response cannot mutate persisted state. `statusFetchInFlight` is released via a per-call token so a finishing request never clears a newer handle. `holdFastPolling()` keeps the 2s interval until the backend confirms the run is running or queued, since a just-triggered sync still reports `sync_running: false`.

**Parallel SIMKL fetching:** `_sync_simkl` submits all `(media_type, status_key)` combinations to a `ThreadPoolExecutor(max_workers=min(8, len(fetch_jobs)))`, then sorts results back to canonical order by original job index before processing.

**Anime root resolution:** For anime, `ItemMatcher` walks the prequel chain to find the root title so all seasons/cours resolve to the same PMDB entry. The chain is cached in `anime_mapping_store.py`.
