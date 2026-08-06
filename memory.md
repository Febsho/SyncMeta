# SyncMeta Project Memory

This file is a compact working memory for future code changes. Keep it current when behavior or architecture changes.

## Project Shape

- Docker-first Flask web app. Main entrypoint is `web.py`.
- No CLI entrypoint is supported anymore.
- Main UI is a single template: `templates/index.html`.
- Core sync orchestration lives in `src/sync_service.py`.
- Persistent multi-user state lives in JSON through `src/profile_store.py`.
- Source/API clients:
  - `src/simkl_client.py`
  - `src/anilist_client.py`
  - `src/trakt_client.py`
  - `src/mdblist_client.py`
  - `src/publicmetadb_client.py`
  - `src/tmdb_client.py` (Library-view poster/title lookups only; module-level 24h cache shared across profiles)
- Matching external ids to PMDB/TMDB ids lives in `src/matcher.py`.
- The Library view (`/api/profile/library/*`) browses PMDB lists and watch history with posters; it needs the profile's optional TMDB API key (`credentials.tmdb.api_key`, v3 key or v4 read token) for titles/posters and degrades to bare TMDB ids without one.
- The Sync view hosts both the per-service pipeline cards (the old Settings→Lists/Behavior content, same element ids, each service → PMDB) and the cross-service sync pairs; the dashboard shows a per-service Sync Pipelines status panel.
- The dashboard shows a Live Sync Activity panel while a sync runs; it tails the session-scoped `/api/logs` stream, and the sync pipeline logs each list add/remove and watched-history write at INFO so they appear there and in the Logs view.

## Storage And Secrets

- Profile store path is controlled by `PROFILE_STORE_FILE`, normally `/app/data/profiles.json`.
- Encryption key is either `SYNCMETA_MASTER_KEY` or generated/persisted as `/app/data/profiles.key`.
- Never commit `data/profiles.key`.
- Profile passwords are hashed.
- Source credentials are encrypted at rest and are overwrite-only in the UI.
- Saved secrets should not be returned raw to the browser.

## Web Flow

- Browser auth uses server-side sessions.
- Changing a profile password requires the current password (`current_password`),
  signed in or not; the profile UUID alone is never enough. A successful change
  invalidates sessions issued before it.
- Optional site-wide gate is controlled by `SITE_ACCESS_PASSWORD`.
- Important routes in `web.py`:
  - `/api/profile/save`
  - `/api/profile/status`
  - `/api/profile/sync`
  - `/api/profile/sync/stop`
  - `/api/profile/activity/sync`
  - `/api/profile/activity/history/clear`
  - `/api/simkl/pin/start`
  - `/api/simkl/pin/check`
  - `/api/trakt/device/start`
  - `/api/trakt/device/check`
  - `/api/trakt/catalogs`
  - `/api/mdblist/lists`

## Scheduler

- Scheduler is inside `web.py` and polls every 5 seconds.
- It waits SYNCMETA_SCHEDULER_STARTUP_GRACE_SECONDS (20s) before its first claim,
  because the first HTTP request is what starts it, and claims at most
  SYNCMETA_SCHEDULER_CLAIM_BATCH profiles per poll.
- Gunicorn workers must stay at 1 (scheduler/SyncRunner are per-process); raise
  threads instead. docker-compose.yml now has `env_file: .env`.
- Disable with `DISABLE_PROFILE_SCHEDULER=1`.
- Automatic background sync applies to list sync.
- Trakt resume progress can auto-run when enabled; current default interval is 10 minutes in `profile_store.py`.
- Watch history is manual-only.
- `activity_resume_source: "off"` is honoured explicitly. The trakt fallback in
  `_normalize_resume_source` is a migration for the legacy
  `trakt_sync_resume_progress` boolean and must only apply to an UNSET value —
  normalize writes that boolean back out, so catching "off" too made the
  override re-arm on every save and resume could never be turned off.
- One sync per profile is claimed at a time through `ProfileStore`.

## Sync Modes

- `lists`: normal list sync.
- `history`: manual watch history import.
- `resume`: resume/progress sync.
- Manual dashboard buttons save the profile first, but should not refresh source pickers or start unrelated sync modes.
- Activity-only syncs should not overwrite `last_results`; they update `activity_results`.
- Running syncs expose `sync_live_results` so the dashboard can update progress while work is still running.

## List Sync Behavior

- `SyncService._sync_list()` resolves source items, creates/loads a PMDB list, adds missing items, optionally removes stale items.
- `remove_missing` removes items no longer in the source.
- `delete_disabled_lists` deletes SyncMeta-managed PMDB lists that are no longer selected.
- Managed list metadata is stored in profile state so dashboard delete can unselect the matching source selection.
- If two sources want the same PMDB display name, `SyncService` creates a collision-safe actual name.

## Source Defaults And Visibility

- SIMKL and AniList selections default empty until linked/user-selected.
- Linked defaults should not re-enable themselves after the user clears all statuses.
- Visibility defaults:
  - SIMKL private
  - AniList private
  - Trakt personal private
  - Trakt public public
  - MDBList public

## SIMKL Notes

- SIMKL list endpoints use `/sync/all-items/{type}/{status}`.
- Type mapping:
  - SyncMeta `shows` -> SIMKL `tv`
  - SyncMeta `movies` -> SIMKL `movie`
  - SyncMeta `anime` -> SIMKL `anime`
- Status mapping includes `plantowatch -> plan to watch` and `hold -> on hold`.
- SIMKL anime is the trickiest area:
  - Anime movies must be treated as PMDB movies, not fake one-episode TV.
  - Season 2+ anime often needs root-series mapping through AniList/MAL.
  - Some PMDB anime metadata merges sequel seasons into one TV season.
  - Some SIMKL anime payloads only expose aggregate watched counts.
  - Avoid expensive AniList root lookups when direct TMDB mapping is enough.
  - AniList 429s should fail fast/cool down so SIMKL sync can keep moving and Stop can respond.
  - Anime list entries without anime-specific ids should be skipped to avoid non-anime pollution.
  - Season/part anime list entries can set `prefer_root_series` so PMDB matching favors the root anime.

## Trakt Notes

- Device auth is used in the web UI.
- `401 Unauthorized` usually means expired/revoked/bad token, not rate limiting.
- Rate limit would normally be `429`.
- Trakt supports:
  - split movie/show watchlist
  - default catalogs
  - liked lists
  - personal created lists
  - discover/public lists
  - watch history import
  - resume progress sync

## MDBList Notes

- Supports account lists and public-list search.
- Public search may need the HTML/toplists fallback if the API path returns nothing.
- No longer source-only: it reads AND writes watchlist, collection and watch
  history through its `/sync/*` API (marked BETA by MDBList), and adds/removes
  items on static lists via `/lists/{id}/items/{add|remove}`.
- Auth is an `apikey` query param OR an OAuth bearer token; the client prefers
  the bearer. OAuth is authorization-code + PKCE, needs the client secret too,
  and every OAuth path requires a trailing slash.
- History is account-level only; a curated list has no watch dates.
- `/sync/ratings`, `/sync/paused`, `/sync/dropped` exist but are not wired.

## Rate Limits And Retries

- `src/rate_limit.py`: `retry_on_rate_limit` (writes retry ONLY on 429, never on
  5xx/timeout — a landed `/sync/history` write would be duplicated) and
  `RateLimiter` (sliding window, used by every client now, not just PMDB).
- Session-level urllib3 `Retry` is GET-only on every client.
- `src/http_timeouts.py`: read timeouts are env-tunable. Trakt/SIMKL were 6s and
  are now 20s — the old value caused the reported read-timeout storms.

## PublicMetaDB Notes

- Lists use `/api/external/lists`.
- List item delete uses `/api/external/lists/:listId/items/:itemId`.
- Watched history uses `/api/external/watched`.
- Resume uses `/api/external/resume` and batch save.
- PMDB watched-history clearing must keep reloading page 1 until empty because page snapshots can shift while deleting.

## UI Notes

- `templates/index.html` is dense and stateful; prefer small, targeted edits.
- Dashboard sections:
  - summary cards
  - Activity Sync
  - Latest Sync Results
  - Sync History
- Sync Pairs run controls live in that panel's header only; duplicating them in
  the page topbar under the same ids left the lower copies unbound and dead.
- Running pairs saves unsaved edits first, and a run that reports per-category
  errors gets an error toast, not a green "complete".
- Pair cards in the Sync view are collapsed until clicked; the head is a button
  with no controls in it, and open state is keyed on pair_id (not index, not the
  pair object, which fetchPairs replaces).
- The dashboard renders pairs as .svc-card cards matching the service pipelines.
- Latest Sync Results is paginated at 25 rows per page.
- Sync History displays only the newest 25 runs.
- Mobile tables should allow horizontal scroll.
- The nav wraps to a second row below 700px; it must never clip a destination,
  since Settings (Connections) is where a new user has to start.
- Mobile check: no view may give the page a horizontal scrollbar at 320-390px.
  Grid tracks set to `1fr` need `minmax(0,1fr)` or a wide child blows them out.
- Service connection dots show connected state based on credentials, not selected lists.
- Settings -> Profile hides the Quick Setup steps once a profile is open, and the
  profile UUID has a Copy button next to it.
- All pollers skip their work while the browser tab is hidden and force a fresh
  render on return.

## Tests

- Standard command:
  - `python -m unittest discover -v`
- Focused tests by area:
  - Web/UI routes: `python -m unittest tests.test_web -v`
  - Sync behavior: `python -m unittest tests.test_sync_service -v`
  - Profiles/storage: `python -m unittest tests.test_profile_store -v`
  - SIMKL parsing: `python -m unittest tests.test_simkl_client -v`
  - Trakt parsing: `python -m unittest tests.test_trakt_client -v`
  - PMDB client: `python -m unittest tests.test_publicmetadb_client -v`
- For template JavaScript checks, extract the dashboard script and run `node --check`.

## Git And Deployment

- Main working repo used for pushes has been:
  - `C:\Users\justi\Documents\Dev\SyncMeta-for-PublicMetaDB-publish`
- Keep `main` and `dev` aligned when requested.
- Do not include local secret files.
- VPS update pattern:
  - `git pull`
  - `docker compose up -d --build`
  - hard refresh browser if UI still looks old.

## Current Caution

- As of this memory update, the worktree has an untracked `data/profiles.key`.
- Also check for local modifications before starting new work:
  - `git status --short`
