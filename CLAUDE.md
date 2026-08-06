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

**Clients:** One file per provider (`simkl_client.py`, `trakt_client.py`, `anilist_client.py`, `mdblist_client.py`, `publicmetadb_client.py`, `fribb_client.py`). Each handles auth, rate limiting, and API calls for its provider. Trakt/SIMKL/AniList/MDBList all carry both read *and* write APIs. `tmdb_client.py` is separate from the sync pipeline: it only serves the Library view's poster/title lookups using the profile's optional TMDB API key (`credentials.tmdb.api_key`), with a module-level 24h cache shared across profiles because poster metadata is public.

**Library browser:** `/api/profile/library/overview|items|history|history/title` in `web.py` — session-scoped, reads the profile's own PMDB lists and watch history and enriches entries with TMDB title/year/poster when a TMDB key is saved. History is grouped one row per title (a binged show must not render one poster per play); the per-episode breakdown — episode names and still thumbnails via `TmdbClient.get_season_episodes`, one request per season — lives behind `history/title`. Without a key the endpoints still succeed (`tmdb_configured: false`) and the UI shows a connect-TMDB notice; a rejected key comes back as `tmdb_error` on a 200, never a failure.

**Cross-service sync:** `src/providers.py` + `src/cross_sync.py`. The main pipeline only writes to PMDB; a *sync pair* copies one category from any provider to any other. `providers.py` wraps each client in a `ProviderAdapter` declaring the categories it can read/write; `cross_sync.CrossSyncService.run_pair()` fetches both sides, diffs on `providers.item_key`, and adds/removes on the target. Pairs live in `options.sync_pairs` (see `SyncPair` in `config.py`), and per-pair ownership in `activity_state.pair_managed_keys`. Endpoints: `/api/profile/pairs`, `/pairs/save`, `/pairs/run`.

**Frontend:** `templates/index.html` — single-page app, no build step, vanilla JS.

**Visual language** (shared tokens in `:root`): surfaces are `--surface` on a
`--line` hairline at 12px radius; a value is titled by a monospace uppercase
overline (`--mono`, 10px, `.1em` tracking, `--label` colour) and set at **weight
500 with negative tracking**, not 700 — heavy weights at these sizes read as
chunky against the thin rules. Bars are 2px. Colour is reserved for status dots
(which carry a soft glow ring), deltas, and the primary action; everything else
is greyscale. `.panel-header` is mono/uppercase, but meta text and controls
inside it are explicitly reset to sentence case — they read as shouting
otherwise. Selected filter buttons invert to a light fill rather than taking the
brand accent, so a filter row reads as one segmented control. **No webfonts** —
the app is self-hosted and must render offline, so `--mono` is a system stack.
Below 700px the nav **wraps to a second row** rather than scrolling behind a
hidden scrollbar — seven items do not fit a phone, and the clipped ones included
Settings, which holds Connections and is where a new user has to start. No view
may give the page a horizontal scrollbar at 320px; a grid track written `1fr`
needs `minmax(0,1fr)`, since a grid item's default `min-width:auto` lets a wide
child stretch the track past the viewport.
A service's state is always a **dot plus its name**, never prose: the pair
capability row (`.pair-cap`) is chips in that vocabulary, having been a run of
`"Trakt: not connected"` lines joined by `<br>`. Form controls outside a
`.form-row` need the dark treatment applied explicitly (`.pair-flow-end select`,
`.library-picker-controls select`) or they fall back to the browser's native
light control on a black page.

Key patterns:
- `fetchStatus(force)` polls `/status` every 2s during sync; has `_statusGeneration` counter to discard stale renders
- **Every poller skips its work while `document.hidden`.** A background tab still runs its timers, so an unattended dashboard kept requesting `/status` every 2s for a whole sync, plus the log and live-activity tails at 2s each. The intervals stay installed and the callback returns early instead, so no polling state is torn down and rebuilt; one `visibilitychange` listener forces an immediate refresh of whatever is running on return, so a hidden tab never comes back showing stale numbers
- `_forceStatusRefresh()` bumps `_statusGeneration`, clears in-flight request, immediately re-fetches — called after every action button success
- All action buttons (`triggerSync`, `triggerActivitySync`, `saveProfile`, `loadProfile`) give immediate visual feedback (disable + label change) before any `await`, and restore on failure
- `fetchUnresolved()` is only called on `sync_running` transition (true→false), not on every poll
- Sync settings (per-service list/status pickers, visibility, watchlist toggles, schedule, history/resume) live in the **Sync view** as collapsible per-service "pipeline" cards (`#sync-settings`, `togglePipelineCard`/`updatePipelineSummaries`), not in Settings — Settings keeps only Profile/Connections/Danger Zone. The inputs kept their original ids when they moved, so `gatherOptions`/`gatherCreds`/`populateForm` are unchanged; old `lists`/`behavior`/`rules` deep links redirect to the Sync view. The pipeline cards are *virtual* representations of the existing main pipeline (each service → PMDB) — they are not `options.sync_pairs` and no migration happens
- The dashboard's Sync Pipelines panel (`renderPipelinesPanel`) aggregates `last_results`/`sync_live_results` per `source_name` into one **card** per service (`.svc-card`), sorted so services with errors then unresolved come first. It covers *only* the main service→PMDB pipeline; cross-service pairs used to be appended to it, which made two different things read as one pipeline
- **Clicking a service card filters Latest Sync Results to that service.** `currentServiceFilter` is deliberately separate from `currentResultsFilter` so the two compose ("Trakt" + "Unresolved"); clicking the active card clears it, and the filter-button counts are scoped to the selected service so they never advertise rows the selection cannot show
- **Bars show composition, not resolve rate.** The service cards and the result rows both segment `fetched` into added / carried-over / unresolved. The result rows previously plotted `resolved / fetched`, which is ~100% for nearly every list, so every row drew a full bar whether the run changed anything or not
- **A Latest Sync Results row expands to show its list's posters.** `/api/profile/library/list-preview` takes a list *name* — the dashboard rows carry no PMDB id — and resolves it via `managed_lists`, falling back to `find_list_by_name`, so that mapping stays server-side. Only the shown slice (`LIST_PREVIEW_LIMIT`) is sent to TMDB; enriching a 400-item list to render 18 posters is the expensive way. `expandedResultRows` is keyed on `list_name`, never an index or `row_key`, because the dashboard re-renders on every poll and rows move between pages and filters. Toggling flips classes directly rather than re-rendering, or the `grid-template-rows: 0fr→1fr` transition would not run from its current state; results are cached per list so a re-render keeps an open row open without refetching. The click handler ignores events inside `.result-row-actions` so Details/Delete do not also toggle the row
- The Activity Sync cards (`buildActivityCard`) reuse `.svc-card` so watch history and resume read as more service→PMDB pipelines. They are `.svc-card-static` (no click affordance). The headline number used to repeat the first stat box verbatim, which is what made that panel twice the size it needed to be
- The dashboard's Cross-Service Pairs panel (`renderPairsDashPanel`) renders each pair as a `.svc-card .svc-card-static` in a `.svc-grid`, the same card language as the service pipelines — it was a flat `.pipe-status-row`, so two panels showing the same kind of thing looked like different eras of the app. It stays its **own** panel (see below); only the card vocabulary is shared. Hidden when no pair exists. It renders from `/status` alone — the public profile already carries both `options.sync_pairs` and `last_pair_results` — so it costs no extra request and needs no `fetchPairs()`. Rows carry `data-dash-pair-run` and are handled by one delegated listener (`bindPairsDashEvents`); a real run calls `_forceStatusRefresh()`, a dry run does not, since it writes nothing the panel reads
- The dashboard's Live Sync Activity panel appears only while `sync_running`; it tails the session-scoped `/api/logs` stream on its own 2s interval (`startLiveActivityFeed`/`stopLiveActivityFeed`), driven from `renderDashboard` via `updateLiveActivityPanel(profile)`. The sync pipeline logs every list add/remove and history write at INFO so those lines show up here and in the Logs view

## Key Invariants

**The scheduler shares the web process, so it must yield to it.** `ProfileScheduler`
is started lazily by `_before_request`, and every profile whose schedule lapsed
while the server was down is due the moment it first polls — so the request that
boots the app used to trigger a stampede of syncs and then queue behind it, which
is what made the site appear to hang (and the proxy return 502) right after a
restart. `SCHEDULER_STARTUP_GRACE_SECONDS` gives the web tier a head start, and
`claim_due_profiles(limit=...)` claims at most `SCHEDULER_CLAIM_BATCH` per poll —
a claim marks a profile running, so claiming past the runner's capacity only
fills its queue while the dashboard advertises work that has not started.
**Gunicorn workers must stay at 1**: the scheduler and `SyncRunner` are
per-process, so a second worker is a second scheduler running every sync twice.
Threads are the dial to turn. Note `sync_running` is deliberately *not*
persisted (`_hydrate_profile` forces it `False`), so a killed process cannot
wedge a profile as permanently running; there is a test pinning that.

**PMDB Watchlist managed-keys filter:** `_remove_stale` in `sync_service.py` accepts `managed_keys: frozenset[str] | None`. If `managed_keys` is truthy (non-empty), only items whose key is in `managed_keys` are eligible for removal — this preserves manually-added PMDB entries. An empty frozenset (bootstrap/first-sync) is falsy and falls back to full-removal behavior. Keys are persisted in `activity_state.pmdb_watchlist_managed_keys` by `_merge_activity_results` in `profile_store.py` after each sync.

**Two-way is one pass, never two one-way runs.** `SyncPair.mode` is `one_way`
(default) or `two_way`. Running two one-way passes back to back would let the
*order* decide the outcome: whichever direction goes first re-adds an item the
user just deleted on the other side, so a deletion propagates or is resurrected
depending on which service happens to be named first. `_run_category_two_way`
instead reads both sides once and treats the pair's managed-key set as *the
state both sides last agreed on*, which is what makes a one-sided item
interpretable — previously synced means deleted on the other side, never synced
means new. There is a regression test asserting the result is the same when the
two services are named the other way round. `mirror` has no bidirectional
meaning (applied both ways it just means one side wins and the other's unique
items are destroyed), so `from_dict` downgrades it to `managed` and the editor
does not offer it. Two-way writes both ends, so a read-only provider (MDBList,
AniList without a token) is rejected at either position.

**Sync pairs are additive by default.** `removal_mode` is `additive` (never deletes),
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

**Pair reads are cached per batch, and a write must be folded back in.**
`cross_sync.ReadCache` memoizes `(provider, category, source_lists)` so fanning
one service out to several targets reads it once instead of once per pair. The
cache is scoped to a batch — `run_pairs` opens one, a bare `run_pair` opens its
own, and `_batch_cache` is re-entrant so nesting never resets the outer one. It
must never outlive a run, or a later run would diff against stale data. Its
safety rests on `apply_write`: once a pair writes to a target, any other pair in
the same batch has to see the new contents or it re-adds what was just written
(two sources feeding one target is the common case). A write updates the exact
list it was read from and drops every other view of that provider; a write that
*raised* invalidates the provider outright, since a partial write may have
landed. `cached_reads` is reported per category so the saving is visible rather
than merely claimed.

**A pair id is an internal handle, not display text.** `SyncPair._clean_pair_id`
restricts it to `[A-Za-z0-9-_]` (64 chars). It is echoed into HTML attributes and
keys `pair_managed_keys`, and `escAttr` is *not* sufficient protection on its
own: it encodes `'` as `&#39;`, which the HTML parser decodes back before an
inline handler is compiled. Generated ids are built from `source`/`target`, which
are free text until an adapter is looked up, so those go through the same filter.
Render pair ids via `data-` attributes and delegated listeners, never inline
`onclick`.

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

**Running pairs saves them first.** A run executes server-side against the
*saved* pairs, so a card the user just added is invisible to it — "Run All" on a
screen showing one pair used to answer "No sync pairs configured".
`runPairs()` therefore calls `savePairs(false)` when `pairsHaveUnsavedEdits()`
and aborts the run if that save fails. Dirtiness is a comparison against
`pairsSavedSnapshot` (the serialized payload as the server last reported it, set
in `fetchPairs`) rather than a flag, because a pair is mutated from a dozen
places that would each have to remember to set one. The run controls live in the
Sync Pairs **panel header** only: they were duplicated in the page topbar under
the same ids, so `getElementById` bound the topbar copy and the lower buttons
were dead — and "Run All" beside the service pipelines read as running those too.

**A 200 from `/pairs/run` is not a successful run.** Provider read/write
failures come back as per-run and per-category `errors` strings on an otherwise
fine payload, so the toast counts them and reports an error rather than a green
"complete" contradicting the red text rendered directly beneath it.

**Pair cards are collapsed until opened.** A pair is ~600px of controls, so two
of them buried the service pipelines above. `.pair-card-body` toggles on a
`.open` class, the same idiom as `.pipeline-card` — not the `grid-template-rows`
transition the result rows use, which is for animating a few posters, not a card
holding hundreds of list checkboxes. Three things this has to get right:
the head carries **no controls** (it is one `<button>`, so the whole strip
toggles and the keyboard works for free) — the pair-name input lived there
first and covered the head, swallowing every click meant for the toggle, so it
moved into the body; the toggle **flips the class rather than re-rendering**,
or a rebuild would throw away focus and the caret in another card's list
filter; and open state is keyed on `pair.pair_id` (with a client-side
`_clientKey` until the server assigns one) in `expandedPairKeys`, never on the
index, which shifts on remove, and never on the pair object, which
`fetchPairs()` replaces wholesale. `savePairs()` carries open state across that
replacement **by position**, since a first save is exactly when a pair's key
changes.

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

**Admin settings are an override layer, not a `.env` writer.** `src/env_settings.py`
holds an allow-list of editable variables; `/admin` renders it and
`/admin/api/settings` writes it. It does **not** write `.env`, and must not be
changed to: on Docker that file is on the host, the container gets its values
through compose, and `load_dotenv` does not override an already-set variable —
so an in-container `.env` write would be both ignored while running and lost on
the next `up`. Overrides go to `data/settings.json` (the mounted volume) and
`apply_overrides` pushes them into `os.environ` at the *top* of `web.py`, before
the `src` imports — every tunable is an `os.getenv` read into a module constant
at import time, so a value that arrives afterwards is never seen. That is also
why `load_dotenv` moved up there: it used to run below the imports, where it
could set `web.py`'s constants but not `sync_service`'s.

Things this has to keep getting right: only keys in `SETTINGS` are writable
(`SYNCMETA_MASTER_KEY` and `PROFILE_STORE_FILE` are in `LOCKED_KEYS` — shown as
locked rather than hidden, because a value you cannot see is one you cannot
debug); the whole batch validates before anything is written, so one bad field
cannot half-apply a form; a blank secret means *unchanged*, never *cleared*,
since the panel never shows the current value; `ADMIN_PASSWORD` can be neither
cleared nor reset-to-nothing, which would lock the panel out permanently; and
turning on `SITE_ACCESS_PASSWORD` hands the current browser an access cookie,
because the site gate covers `/admin` too and would otherwise evict the admin
from the page they just used. Most settings genuinely cannot apply until a
restart and say so; `_live_apply_setting` covers the few read on every use.
The panel also flags variables set in the environment that SyncMeta does not
read (`ENCRYPTION_KEY` is the common one) — a misspelt secret is otherwise
silently absent.

**Reads may be retried automatically; writes may not.** `src/rate_limit.py`
holds both halves of this, because urllib3's `Retry` takes one status list for
every method and cannot express the difference.

* The session-level `Retry` is **`allowed_methods=["GET"]`** on every client. It
  already honours `Retry-After` for 429/503/413, so reads need nothing else.
* A write is only safe to retry when it provably did *not* take effect. A 429 is
  exactly that — rejected before any work. A 5xx or a read timeout is **not**:
  the write may have landed, and `/sync/history` is not idempotent, so an
  automatic retry turns one play into two. Writes therefore go through
  `retry_on_rate_limit`, which retries a 429 and nothing else.

`retry_after_seconds` reads `Retry-After`, `X-RateLimit-Reset-After` and
MDBList's absolute `X-RateLimit-Reset` epoch, clamping the result — a provider
asking for an hour must not stall a sync, and a skewed clock must not produce a
negative wait.

**Every client paces itself, not just PublicMetaDB.** `RateLimiter` (moved out of
`publicmetadb_client.py` into `rate_limit.py`) is a sliding window applied in
each client's `_get`/`_post`. Trakt's numbers come from its published ceiling of
1000 GET per 5 minutes; SIMKL's and MDBList's are deliberately loose, there to
stop a wide parallel fetch bursting rather than to slow a normal sync. The wait
loop polls `cancel_requested_callback` instead of sleeping through, because
parked on a limiter is exactly where Stop has to stay responsive.

**Read timeouts are tunable, and 6s was too short.** Trakt and SIMKL used a 6s
read timeout, which a large watchlist fetched with `extended=full` routinely
exceeds — every one of those became three retries and then a failed sync, which
is what the reported timeout storms were. They are now 20s by default via
`http_timeouts._env_timeout`, overridable with `SYNCMETA_TRAKT_READ_TIMEOUT` /
`SYNCMETA_SIMKL_READ_TIMEOUT` and clamped to 2-180s.

**MDBList reads and writes — two different surfaces behind one provider.** It was
source-only until its API grew a Trakt-shaped sync surface; the old note said
"no write path" and is wrong now. `MdbListAdapter` handles:

* the **account-level sync API** — `/sync/watchlist`, `/sync/collection`,
  `/sync/watched` — readable and writable, bodies shaped
  `{"movies": [...], "shows": [...]}` with an `ids` object. MDBList marks these
  **BETA**.
* the user's **static lists**, read via `/lists/{id}/items` and written via
  `/lists/{id}/items/{add|remove}`, which takes *bare* id objects, not the
  ids-wrapped sync shape. A curated list has no watched/unwatched semantics, so
  the same items answer both watchlist and collection.

Rules that are easy to get wrong:
- **A named-list selection must not widen to the whole account**, and an
  account-level read must not fold in a curated list — `_wants_native()` decides,
  and syncing far more than asked is the failure it prevents.
- **History is account-level only.** A curated list carries no watch dates, so
  folding one into `CATEGORY_HISTORY` would invent history that never happened;
  `target_list_categories` excludes it for the same reason.
- **Items with neither an IMDB nor a TMDB id are dropped, not sent.** MDBList
  cannot resolve them and a body of id-less entries is how a write lands on the
  wrong title.
- **POST stays out of the retry policy** (`allowed_methods=["GET"]`). A write
  that timed out may already have landed, so retrying duplicates records.

`/sync/ratings`, `/sync/paused` and `/sync/dropped` also exist and are
deliberately **not** wired — the app has no ratings or dropped concept, and
resume is a main-pipeline feature rather than a pair category. Adding either
means threading a new category through `config.py`, `providers.py`,
`cross_sync.py` and the pair editor; do not add half-wired client methods.

**MDBList auth is two modes.** An `apikey` query parameter (read, and what
existing profiles have) or an OAuth `Authorization: Bearer` token; the client
prefers the bearer and never sends both. OAuth is **authorization code + PKCE**
— MDBList requires PKCE for every client *and* still requires the client secret.
Authorization is on the site host (`mdblist.com/oauth/authorize/`), token
exchange on the API host, and **every OAuth path needs its trailing slash** or
the request fails. Tokens last 30 days and refresh. The PKCE verifier is held
server-side in `PendingPkceStore` keyed by profile and is single-use: it is the
proof the code belongs to the flow that started it, so it must never round-trip
through the browser. `/api/mdblist/auth/check` persists via
`update_mdblist_auth` the moment the exchange succeeds — the AniList lesson, the
code is single-use and short-lived.

**Writing needs no re-authentication, except AniList and MDBList.** Trakt's device-flow token
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

**The profile UUID is a handle, not a credential.** It is printed in the UI, kept
in `localStorage`, and `_public_profile_by_request_id` accepts it unauthenticated
for read-only dashboard state — so nothing that grants *authority* may rest on
knowing it. `/api/profile/password/reset` therefore requires `current_password`
whether or not a session is present (`ProfileStore.change_profile_password`
authenticates before rehashing; there is no unauthenticated
`reset_..._by_id` path, and adding one back is an account takeover: the endpoint
also mints a session cookie). It shares `_login_limiter` with sign-in, since a
password-verifying endpoint without a limiter is a guessing oracle. A successful
change calls `ServerSessionStore.destroy_profile_sessions`, which bumps an
in-memory per-profile epoch stamped into every signed token — sessions are
stateless, so this is the only way to evict cookies issued earlier, and like
`_revoked` it is best-effort across a process restart. There is no
forgot-password path by design; recovery is the operator's `profiles.json`.

**Every door that checks a password shares `_login_limiter`.** `/api/profile/save`
with a UUID + password and no session authenticates and mints a session cookie —
it is a second sign-in path, not just a write — so it throttles on the same
client key as `/api/profile/login` and `/api/profile/password/reset`. Note config
validation runs *before* authentication there, so a limiter-exempt branch must
stay limited to requests that never reach `update_profile`.

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
