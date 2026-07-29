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

**Clients:** One file per provider (`simkl_client.py`, `trakt_client.py`, `anilist_client.py`, `mdblist_client.py`, `publicmetadb_client.py`, `fribb_client.py`). Each handles auth, rate limiting, and API calls for its provider. Trakt/SIMKL/AniList carry both read *and* write APIs; MDBList is read-only. `tmdb_client.py` is separate from the sync pipeline: it only serves the Library view's poster/title lookups using the profile's optional TMDB API key (`credentials.tmdb.api_key`), with a module-level 24h cache shared across profiles because poster metadata is public.

**Library browser:** `/api/profile/library/overview|items|history|history/title` in `web.py` — session-scoped, reads the profile's own PMDB lists and watch history and enriches entries with TMDB title/year/poster when a TMDB key is saved. History is grouped one row per title (a binged show must not render one poster per play); the per-episode breakdown — episode names and still thumbnails via `TmdbClient.get_season_episodes`, one request per season — lives behind `history/title`. Without a key the endpoints still succeed (`tmdb_configured: false`) and the UI shows a connect-TMDB notice; a rejected key comes back as `tmdb_error` on a 200, never a failure.

**Cross-service sync:** `src/providers.py` + `src/cross_sync.py`. The main pipeline only writes to PMDB; a *sync pair* copies one category from any provider to any other. `providers.py` wraps each client in a `ProviderAdapter` declaring the categories it can read/write; `cross_sync.CrossSyncService.run_pair()` fetches both sides, diffs on `providers.item_key`, and adds/removes on the target. Pairs live in `options.sync_pairs` (see `SyncPair` in `config.py`), and per-pair ownership in `activity_state.pair_managed_keys`. Endpoints: `/api/profile/pairs`, `/pairs/save`, `/pairs/run`.

**Frontend:** `templates/index.html` — single-page app, no build step, vanilla JS. Key patterns:
- `fetchStatus(force)` polls `/status` every 2s during sync; has `_statusGeneration` counter to discard stale renders
- `_forceStatusRefresh()` bumps `_statusGeneration`, clears in-flight request, immediately re-fetches — called after every action button success
- All action buttons (`triggerSync`, `triggerActivitySync`, `saveProfile`, `loadProfile`) give immediate visual feedback (disable + label change) before any `await`, and restore on failure
- `fetchUnresolved()` is only called on `sync_running` transition (true→false), not on every poll
- The dashboard's Live Sync Activity panel appears only while `sync_running`; it tails the session-scoped `/api/logs` stream on its own 2s interval (`startLiveActivityFeed`/`stopLiveActivityFeed`), driven from `renderDashboard` via `updateLiveActivityPanel(profile)`. The sync pipeline logs every list add/remove and history write at INFO so those lines show up here and in the Logs view

## Key Invariants

**PMDB Watchlist managed-keys filter:** `_remove_stale` in `sync_service.py` accepts `managed_keys: frozenset[str] | None`. If `managed_keys` is truthy (non-empty), only items whose key is in `managed_keys` are eligible for removal — this preserves manually-added PMDB entries. An empty frozenset (bootstrap/first-sync) is falsy and falls back to full-removal behavior. Keys are persisted in `activity_state.pmdb_watchlist_managed_keys` by `_merge_activity_results` in `profile_store.py` after each sync.

**Sync pairs are one-way and additive by default.** A pair is `source → target`;
both directions means two pairs. `removal_mode` is `additive` (never deletes),
`managed` (deletes only keys this pair previously wrote, so manual additions on
the target survive — the same invariant as `pmdb_watchlist_managed_keys`), or
`mirror` (deletes anything the source lacks). An unrecognised mode must refuse to
delete rather than fall through to a destructive default. Managed keys are scoped
per pair id, so duplicate ids would let two pairs delete each other's items —
`_normalize_sync_pairs` assigns and de-duplicates them.

**Cross-provider identity must be normalized before diffing.** Keys come from
`providers.item_key` and are TMDB-based, but AniList reports only AniList/MAL
ids. Keyed naively the same show yields two different keys, nothing ever matches,
and a pair re-adds its whole source list every run. `providers.enrich_identity()`
maps anime-native ids to TMDB through the offline anime data first, and refuses a
mapping whose namespace disagrees with the item's `media_type`. There is a
regression test asserting a second run writes nothing.

**A failed read of the *target* must abort that category.** Without the target's
current contents every source item looks new and the pair would duplicate the
entire list.

**AniList has no watch-history mapping.** It tracks progress per series, not
individual episode plays, so `AniListAdapter` advertises no history support at
either end. Do not add one — it would silently write wrong data.

**Pair sources are the service's own lists, not generic categories.** Each
adapter's `list_sources()` returns what that service calls its lists (SIMKL
`status:<name>:<media_type>`, AniList `status:<STATUS>`, Trakt
watchlist/collection/history plus `list:<user>/<slug>`, PMDB `watchlist` and
`list:<id>`, MDBList `list:<id>`), each tagged with the neutral category it feeds.
`SyncPair.source_lists` holds the chosen keys; empty means the provider default.
An adapter must not fall back to a whole category when the selection names only
other categories — that would silently sync far more than asked.

**A destination list is only offered where the service supports one.**
`supports_target_lists` is true for Trakt (`/users/{user}/lists/{slug}/items`) and
PMDB, false for SIMKL and AniList (no writable custom lists) and MDBList (no
writes). `SyncPair.target_list` is cleared when the target changes, since list
keys are provider-specific. Liked Trakt lists are sources only — they belong to
another user and cannot be written to.

**An options payload that omits `sync_pairs` must not delete them.** Pairs are
edited on their own screen and the settings form never submits them, but
`normalize_profile_options` turns a missing key into `[]` — so
`update_profile`/`update_profile_by_id` carry the stored pairs forward whenever
`"sync_pairs" not in options`, and every "Save Profile" used to wipe every pair.
An explicitly supplied empty list still clears them.

**A public list search is only offered where one exists.**
`supports_list_search` is true for Trakt (`/search/list`) and MDBList only;
`search_lists` returns `[]` everywhere else, and a search box that can only ever
come back empty reads as a broken feature. It rides to the UI as
`has_list_search` in `describe()` — the unconfigured-provider stub in
`_pair_capabilities` must keep answering the same shape.

**The pair editor renders scoped, not wholesale.** A provider can offer a
hundred lists, so rebuilding every card on every keystroke is what made the
editor lag. Events are delegated once onto `#pairs-list`; `renderPairs()`
(all cards) is only for add/remove, `renderPairCard(i)` for source/target/
category/removal changes, and `renderPairListOptions(i)` / 
`renderPairListSelection(i)` for filtering and ticking — the narrower two leave
the filter field focused and the caret in place. Per-card editor state lives in
a `WeakMap` keyed on the pair object, never on its index, which shifts.

**A chosen list must stay visible even when its entry is gone.** Saved pairs
carry list *keys* only, and a list picked from a search is not in
`list_sources()`. Selections are therefore rendered as chips from
`pair.source_lists` itself — labels resolved through `pairListLabels` and falling
back to the key — independent of the filter, the category boxes, and whether the
provider's lists have loaded. Rendering only the ticked checkboxes made a saved
selection look lost.

**MDBList is source-only.** `MdbListAdapter` declares `writes = ()` — the client
has no write path and MDBList's own watch-status sync is handled by its Trakt/Plex
integrations. It reads the union of `mdblist.selected_lists`, and since an MDBList
list has no watched/unwatched semantics the same items answer both the watchlist
and collection categories. Do not add write methods without checking the API
actually supports them.

**Writing needs no re-authentication, except AniList.** Trakt's device-flow token
and SIMKL's PIN token already permit `/sync` writes. AniList mutations require an
access token that existing username-only profiles do not have, so
`can_write()` / `write_blocked_reason()` report it and the UI offers AniList as a
source but not a target until one is obtained. All of it stays optional: a
username alone still reads a public list, and `merge_credentials` preserves a
blank submission so leaving a secret empty keeps the stored one.

**AniList connect flow.** AniList cannot redirect back to this app, so its own pin
endpoint is used: `redirect_uri` is always the fixed
`https://anilist.co/api/v2/oauth/pin` (not the site root, unlike SIMKL/Trakt), the
user authorizes with `response_type=code`, and the code they paste is exchanged
server-side by `anilist_client.exchange_code_for_token`. The code is single-use and
short-lived, so `/api/anilist/auth/check` persists the token immediately via
`ProfileStore.update_anilist_auth` rather than waiting for a profile save.

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
