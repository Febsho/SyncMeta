# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
# Run the app locally
python web.py                        # http://127.0.0.1:8080

# Run all tests
python -m unittest discover -v
# Or with pytest:
python -m pytest -v

# Run a single test file
python -m pytest tests/test_sync_service.py -v

# Run a single test
python -m pytest tests/test_sync_service.py::TestSyncService::test_pmdb_native_watchlist_reconciles_removed_and_added_items -v
```

**Known permanent test exclusions:** none. CI should fail on any test failure.

## Architecture

**Entry point:** `web.py` — Flask app. Owns the `ProfileStore` singleton, `SyncRunner` thread pool, custom `ProfileScheduler`, and all HTTP routes (`/api/profile/status`, `/api/profile/sync`, `/api/profile/activity/sync`, `/api/profile/sync/runs`, `/api/profile/sync/run-details`, `/admin/*`, etc.). Compresses JSON responses ≥512 bytes via `@app.after_request` gzip hook.

**Sync pipeline:** `src/sync_service.py` — `SyncService` is the main orchestrator. Called once per sync run:
1. `_sync_simkl` / `_sync_trakt` / `_sync_anilist` / `_sync_mdblist` — fetch each source's lists in parallel using `ThreadPoolExecutor`
2. `_sync_list` — writes a single merged list to PMDB, handles stale-item removal
3. `_sync_history` — syncs watch history
4. `_sync_resume` — syncs Trakt resume progress
5. `_sync_pmdb_watchlist` — syncs plan-to-watch into PMDB native watchlist

`SyncStats` dataclass tracks per-run counters plus `synced_keys: list[str]` (populated by `_sync_list` and used to persist `pmdb_watchlist_managed_keys`).

**Cross-service pairs:** `src/cross_sync.py` + `src/providers.py` run capability-aware one-way/two-way pairs. Pair definitions live in `options.sync_pairs`; per-pair ownership lives in `activity_state.pair_managed_keys`, and automatic due state lives in `pair_sync_schedule`. Pair autosync is opt-in, clamps to 12h, and runs through the shared `SyncRunner` even when the main PublicMetaDB schedule is off. `/api/profile/pairs/run` is synchronous by default for API compatibility; the UI sends `background: true` to queue it and poll live status/logs.

**ID resolution:** `src/matcher.py` — `ItemMatcher` resolves cross-provider IDs (TMDB ↔ SIMKL ↔ AniList ↔ MAL ↔ IMDB). Uses Fribb anime mapping (`src/fribb_client.py`), an anime prequel-chain cache (`src/anime_mapping_store.py`), and per-episode PMDB fallback. Thread-safe in-memory cache.

**Profile persistence:** `src/profile_store.py` — JSON-backed store (`/app/data/profiles.json`). Credentials are Fernet-encrypted. `activity_state` dict persists per-profile runtime state: last-sync cursors/timestamps, `pmdb_watchlist_managed_keys` (keys SyncMeta previously wrote to PMDB watchlist — used to avoid removing manually-added entries). `list_state` persists per-row fingerprints. `sync_runs_detailed` keeps the last 25 detailed diagnostic records.

**Config:** `src/config.py` — dataclass hierarchy: `AppConfig` contains `SimklConfig`, `TraktConfig`, `AniListConfig`, `MdbListConfig`, `PublicMetaDBConfig`, `SyncConfig`. `SyncConfig.pmdb_watchlist_managed_keys: list[str]` is the persisted set of watchlist keys written by SyncMeta.

**Clients:** One file per provider (`simkl_client.py`, `trakt_client.py`, `anilist_client.py`, `mdblist_client.py`, `publicmetadb_client.py`, `fribb_client.py`). Each handles auth, rate limiting, and API calls for its provider.

**Frontend:** `templates/index.html` — single-page app, no build step, vanilla JS. Key patterns:
- `fetchStatus(force)` polls `/status` every 2s during sync; has `_statusGeneration` counter to discard stale renders
- `_forceStatusRefresh()` bumps `_statusGeneration`, clears in-flight request, immediately re-fetches — called after every action button success
- All action buttons (`triggerSync`, `triggerActivitySync`, `saveProfile`, `loadProfile`) give immediate visual feedback (disable + label change) before any `await`, and restore on failure
- `fetchUnresolved()` is only called on `sync_running` transition (true→false), not on every poll

## Key Invariants

**PMDB Watchlist managed-keys filter:** `_remove_stale` in `sync_service.py` accepts `managed_keys: frozenset[str] | None`. If `managed_keys` is truthy (non-empty), only items whose key is in `managed_keys` are eligible for removal — this preserves manually-added PMDB entries. An empty frozenset (bootstrap/first-sync) is falsy and falls back to full-removal behavior. Keys are persisted in `activity_state.pmdb_watchlist_managed_keys` by `_merge_activity_results` in `profile_store.py` after each sync.

**Stale poll guard:** `_statusGeneration` is incremented before any forced status refresh. Each `fetchStatus` call captures the generation at start; if it differs when results arrive, the render is discarded. Prevents a queued 2s-poll response from overwriting a just-triggered sync state.

**Parallel SIMKL fetching:** `_sync_simkl` submits all `(media_type, status_key)` combinations to a `ThreadPoolExecutor(max_workers=min(SYNCMETA_SIMKL_FETCH_WORKERS, len(fetch_jobs)))`, then sorts results back to canonical order by original job index before processing.

**Scheduling defaults:** List/catalog sync defaults to 12h and clamps to a 6h minimum. Watch history and resume progress default to manual; when automatic sync is enabled they clamp to a 24h minimum. Automatic list/history/resume schedules add deterministic per-profile jitter so multiple profiles do not all start together on small VPS hosts.

**SIMKL → Trakt destinations:** Plan to Watch always uses Trakt's native watchlist. Watching, Completed, On Hold, and Dropped use separate private `SyncMeta · SIMKL <Status>` personal lists created/reused on demand. Dry runs never create those lists. Do not collapse these statuses into Trakt Collection; that changes their meaning and can trigger Trakt HTTP 420 account limits.

**Anime root resolution:** For anime, `ItemMatcher` walks the prequel chain to find the root title so all seasons/cours resolve to the same PMDB entry. The chain is cached in `anime_mapping_store.py`.
