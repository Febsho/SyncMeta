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

**Information architecture — five destinations, not six views.** The nav is
`Overview · Activity · Library · Issues · Settings`, plus a search button
(Ctrl/Cmd+K) and a GitHub icon in `.nav-tools`. Nothing was deleted in the move;
`switchView` keeps aliases (`dashboard`→overview, `sync`/`rules`/`lists`/
`behavior`/`connections`/`stats`→settings, `logs`/`history`→activity,
`mapping`→issues) so every old deep link and in-app call still lands somewhere
sensible. Where the old views went:

| Was | Now |
|---|---|
| Dashboard | **Overview** — health hero, three metric tiles, sync graph, pairs panel, recent activity, latest sync results |
| Sync (pair editor + hidden pipeline cards) | **Settings → Sync routes** (`#stab-routes`) |
| Schedule / history / resume options (were inside the hidden `#sync-settings`) | **Settings → Schedule & safety** (`#stab-schedule`) — `#pipe-schedule` and `#pipe-activity` were physically moved out of that hidden wrapper, which is why they are editable again |
| Logs | **Activity**, inside a `<details class="tech-details">` disclosure |
| Stats | **Settings → About** (`#stab-about`) |
| Unresolved / errors / Anime Health | **Issues** |
| Sync History panel | removed — the Activity page's Runs list is the same record, paginated server-side through `/api/profile/sync/runs`. `renderDashboard`'s history block is null-guarded rather than deleted |

Input ids did not change anywhere, so `gatherOptions`/`gatherCreds`/
`populateForm` and every existing handler are untouched by the move.

**Nothing on these surfaces is synthesised.** Every number renders from
`/status`, `/api/profile/sync/runs`, `/api/profile/unresolved` or
`/api/profile/library/*`. Where a value has no backing datum the UI omits it:
`metricValue(null)` prints `—`, a run's duration is only shown when `phase_timings`
actually recorded one, and the search dialog reports an empty library rather than
inventing rows. If a control cannot be wired to real behaviour it is not built —
the conflict-resolution policy from the redesign brief was left out for exactly
this reason, since no backend implements it.

**Global health is a dot plus a phrase, never a colour.** `setHealthState` drives
`#health-pill` through `healthy | running | attention | failed | auth | idle`.
`renderHealthHero` ranks them: a running sync first, then a fatal `sync_error`,
then a provider whose `connection_health` is `error`, then unresolved/error
counts, then "nothing configured". A progress bar is drawn only when the pipeline
reported a denominator.

**The sync graph is drawn from the saved pairs, not from a picture of the app.**
`renderSyncGraph` groups `options.sync_pairs` by target and draws one flow per
destination; the bracket is an SVG stretched to the source count and is replaced
by a short vertical rule below 860px, where the flow stacks. An edge's state
folds the *connection* in: a green last run on a service whose token just expired
still reads `✕ Authentication required`, because the route cannot work now.
Clicking a node opens `#provider-detail-panel` — its routes, categories, last
result and recovery button, all from the same payload.

**Errors are translated, and the raw text is kept.** `friendlyError` maps
provider/HTTP shapes ("Trakt … 401 … invalid_grant") to a consequence a user can
act on, and `technicalDetailsHtml` puts the original inside a "Show technical
details" disclosure. Call sites that know the provider prefix the message with
its label first, since the patterns key on the provider name.

**Dialogs, not `window.confirm`.** `confirmDialog({title, body, confirmLabel,
destructive})` returns a promise, traps focus, closes on Escape and the backdrop,
and restores focus to the opener. `openPreviewDialog` renders a finished dry run
from `dry_run_preview` on the result rows — a dry run sets `pendingPreviewRun`,
and `maybeOpenPreview` opens it once, keyed on `latest_run_id` so a repeated poll
of the same completed run does not reopen it. "Apply changes" starts a real sync.

**Intervals are presets; seconds are the storage format.**
`installScheduleSelects` inserts a `<select>` in front of each seconds input and
moves the input into a `.interval-custom` wrapper. The input keeps its id, so the
form plumbing is unchanged; a stored value that is not a preset selects "Custom…"
and reveals the raw field rather than being silently rounded.

**Browser notifications only.** `notifySettings` lives in `localStorage`, asks for
permission on enable, and fires from the status poll through `maybeNotify`, which
keys each event on a signature so a repeated poll of the same state notifies once.
No webhook channels are offered, because none is implemented server-side.

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
- **A fingerprint-skipped row says "unchanged", never "0 resolved".** A row whose source fingerprint matches the previous run returns from `_sync_list` before the resolver, so it carries `items_skipped_fingerprint == items_fetched` and `items_resolved == 0`. Printing that as `11 items → 0 resolved` reads as the matcher having failed on every item. Its bar segment is `--z800`, the same colour as the track, so an untouched row reads as an empty bar rather than the full grey slab a 100% carried-over segment drew. A row with nothing fetched draws no bar at all — `barTotal` is floored at 1, so an empty list would otherwise plot 100% of nothing
- **A Latest Sync Results row expands to show its list's posters.** `/api/profile/library/list-preview` takes a list *name* — the dashboard rows carry no PMDB id — and resolves it via `managed_lists`, falling back to `find_list_by_name`, so that mapping stays server-side. Only the shown slice (`LIST_PREVIEW_LIMIT`) is sent to TMDB; enriching a 400-item list to render 18 posters is the expensive way. `expandedResultRows` is keyed on `list_name`, never an index or `row_key`, because the dashboard re-renders on every poll and rows move between pages and filters. Toggling flips classes directly rather than re-rendering, or the `grid-template-rows: 0fr→1fr` transition would not run from its current state; results are cached per list so a re-render keeps an open row open without refetching. The click handler ignores events inside `.result-row-actions` so Details/Delete do not also toggle the row
- The Anime Health panel (`renderAnimeHealth`) is two `.svc-card .svc-card-static` cards — Anime Matching (verified / ambiguous / unresolved, with the same composition bar) and Mapping Help (overrides / remapped / reviewed) — not the four `.card` tiles it used to be, which were the last of the old tile language on the dashboard. Its panel-header state is a dot plus its name like every other service state; the card chips carry a *different* value (match rate, in use / none yet) so the header is not repeated an inch below itself
- The Activity Sync cards (`buildActivityCard`) reuse `.svc-card` so watch history and resume read as more service→PMDB pipelines. They are `.svc-card-static` (no click affordance). The headline number used to repeat the first stat box verbatim, which is what made that panel twice the size it needed to be
- The dashboard's Cross-Service Pairs panel (`renderPairsDashPanel`) renders each pair as a `.svc-card .svc-card-static` in a `.svc-grid`, the same card language as the service pipelines — it was a flat `.pipe-status-row`, so two panels showing the same kind of thing looked like different eras of the app. It stays its **own** panel (see below); only the card vocabulary is shared. Hidden when no pair exists. It renders from `/status` alone — the public profile already carries both `options.sync_pairs` and `last_pair_results` — so it costs no extra request and needs no `fetchPairs()`. Rows carry `data-dash-pair-run` and are handled by one delegated listener (`bindPairsDashEvents`); a real run calls `_forceStatusRefresh()`, a dry run does not, since it writes nothing the panel reads
- The dashboard's Live Sync Activity panel appears only while `sync_running`; it tails the session-scoped `/api/logs` stream on its own 2s interval (`startLiveActivityFeed`/`stopLiveActivityFeed`), driven from `renderDashboard` via `updateLiveActivityPanel(profile)`. The sync pipeline logs every list add/remove and history write at INFO so those lines show up here and in the Logs view

**The sync engine is moving into `src/sync/`, incrementally.** `sync_service.py`
and `cross_sync.py` answer "copy this category from A to B"; `src/sync/` answers
the question underneath — *what actually changed since the two sides last
agreed*, which is the only thing that can tell a user's edit apart from a
provider hiccup. Landed so far:

* `sync/models.py` — `FetchStatus`/`FetchOutcome`, `ItemState`, `RouteBaseline`,
  `RouteObservation`
* `sync/state_store.py` — `SyncStateStore`, per-profile baselines
* `sync/planner.py` — `plan_membership` / `plan_two_way`, the immutable `SyncPlan`

**The planner decides from the baseline; the old diff decided from presence.**
Comparing a source list against a destination list says only that they differ —
"missing on the destination" is either an item the user just added on the source
or one they just deleted on the destination, and those want opposite actions.
`plan_membership` compares *both* current states against the last agreement, so
absence on the source is only a deletion when the baseline says the source used
to have it. Three cases separate it from `_items_to_remove`: a half-read source
(removals dropped, additions kept), a route with no baseline yet (nothing may be
deleted, including right after the `pair_managed_keys` migration), and a
destination item the source never had (kept unless the policy is `mirror`).

**A plan is immutable, and preview and execution must share one.** `SyncPlan` is
a frozen dataclass of frozen `PlannedAction`s, each carrying the reason a person
should be shown. Two code paths — one to preview, one to execute — is how a
preview becomes a lie; there is one planner and the executor consumes what it
produced.

**Two-way is planned in one pass, and a real conflict is reported, not resolved.**
`plan_two_way` asks *which side moved* rather than which side differs: one side
changed and the other did not means the changed side wins in either direction;
both changed and now agree means nothing to do; both changed and still disagree
is a conflict, and by default neither side is touched, because letting run order
pick a winner silently discards one of the user's two edits. `latest_change` is
deliberately not offered for membership — several providers expose no reliable
per-item modification time, and a policy that degrades to "whichever we read
second" is worse than reporting the conflict.

* `sync/safety.py` — threshold and evidence checks over a plan
* `sync/executor.py` — performs a plan, reporting each action's fate

**Every category is planned, but each kind of data gets its own planner.**
Membership (`_PLANNED_CATEGORIES` — watchlist and collection) asks "is it on the
list"; `sync/history.py` asks "did this happen and have we already carried it";
`sync/progress.py` asks "is this further along". Sharing one planner would
collapse exactly the distinctions each exists to keep — a membership planner
calls a moved playback position "already in sync", and calls a rewatch a
duplicate. `_run_category` dispatches on the category; each planner falls back to
the legacy diff if it raises, so a planner bug degrades rather than fails the
run. **Two-way** plans every category too: membership through `plan_two_way`,
history by planning both directions from the *same* pair of reads (a union has
no deletion to order, and the baseline's record of what was carried is what stops
a play echoing back), and resume by planning both directions — each refuses to
write when the destination is already further, so applying both is furthest-wins
without either side's turn deciding it. `_history_adds` / `_resume_matches`
survive only as the fallback when a planner raises.

**History's dedupe has to read the live ledger, not the initial destination
read.** Two plays of one episode arriving in the same batch is the case that
catches this: the first makes the episode known, and to a destination that
records watched state rather than plays the second must then be refused. A check
written against the destination's contents *before* the run let both through,
which is one duplicate play per batch, forever.

**A route may not delete until it has one confirmed sync behind it.** This is a
real behaviour change: the first run of every existing route after this shipped
adds but removes nothing, then establishes a baseline and behaves normally.
Without a baseline "absent from the source" cannot be told apart from "the
source never had it", and the adopted `pair_managed_keys` cannot fill the gap
because they never recorded the source side.

**The guard blocks in two tiers, and only one is overridable.** Threshold blocks
(too many removals, too large a share) can be waived by a person acting on a
preview — `allow_destructive_override`, passed only by `/pairs/run`, never by
the scheduler. Hard blocks cannot be waived by anyone: an unreadable source or
destination, a missing baseline, or a source returning nothing at all while the
destination holds plenty. A person may decide a large deletion is right; nobody
gets to decide that a failed read really was empty. Blocking is *demotion* —
`SyncPlan.without_removals` keeps the additions, since whatever made the
removals unsafe says nothing about items the source positively reported.

**History is a union, and dedupes in three layers.** `sync/history.py`. An
episode absent from the source is not evidence to delete it — providers expose
different windows of the same history — so only an explicit `mirror` route
removes, whole episodes only, once a baseline exists. Deduplication runs
strongest-first: the source's own stable event id (`event_id`, persisted in
`ItemState.event_ids`), then this route's record of the plays it already carried
(`ItemState.plays`), then the destination's current plays matched through
`PlaySet`'s tolerance window. The second layer is what stops a two-way route
re-importing its own write; the third is what makes a repeated run idempotent.
A row reporting watched *state* — no timestamp, `cursor_exempt`, or
`anilist_derived` — may create the first play of an unknown episode but never a
second, so repeated state syncs write nothing.

**Resume never rewinds.** `sync/progress.py`. Below 2% is an accidental open, at
or above 90% the title is finished and pushing it on would make a watched show
look abandoned near the end, and a destination further along is never
overwritten. Furthest-wins is deliberate over most-recent: no provider here
exposes a portable per-item modification time, and furthest is the only
deterministic rule that cannot rewind either side because of run order.

**A truncated read is `PARTIAL`, and only two providers can tell.** PublicMetaDB
detects a page it promised that came back empty; MDBList detects a pagination
cursor that stops advancing. Both surface through `ProviderAdapter.last_read_complete()`.
Trakt, SIMKL and AniList end their reads on an unambiguous signal and have no
known silent-truncation path, so they report complete — stated rather than
assumed, and the one place to change when that stops being true.

**A removal asks the destination, not the route.** `sync/ownership.py`. Two
routes commonly feed one target (SIMKL → Trakt and MDBList → Trakt both wanting
Movie X). Per-route `managed` decides whether a route *may* delete; it cannot
decide whether it *should*, because the other route will re-add on its next run
and the item flickers forever. `run_pairs` therefore reads every route's source
before any route writes — the batch cache makes that nearly free — and a removal
is dropped when another active route's source still lists the item.

**A partial run is neither a success nor a failure.** `ExecutionResult.complete`
is false while anything is outstanding — a failed write, one the provider never
confirmed, an action the guard blocked — and only a complete run may `commit` a
baseline. An item the provider *explicitly* could not match is `not_found` and
does **not** hold the route back: retrying will not change its answer, and one
permanently unmappable title must not stop a route ever agreeing on anything.
Batched writes mean an adapter reports "added: 8" for ten items without saying
which two it dropped; the unconfirmed remainder is recorded as `unconfirmed` and
stays outstanding rather than being claimed either way.

**Only a run that actually succeeded may advance the agreement.** `commit` is
the sole method that moves `last_successful_sync`, bumps `sync_version` and
replaces the item states. `record_failure` writes the error and leaves the
agreement alone; `record_partial` folds in the writes that *did* land — so the
next run neither repeats them nor believes the rest done — without declaring
success. A provider outage can therefore never teach the engine that everything
it failed to read was deleted.

**A read that failed is not an empty list.** Every fetch is classified into a
`FetchStatus`, and only `SUCCESS_WITH_ITEMS`/`SUCCESS_EMPTY` are
`trustworthy_for_removals`. A timeout, a 401, a rate limit and a half-read page
all look identical to "the user deleted everything" if you only count what came
back. `FetchOutcome.complete` covers the subtle case: a 200 carrying real items
is fine to add from and still unsafe to delete from if a page never arrived.
`PARTIAL` is modelled but not yet produced — no client reports incomplete
pagination today; `cross_sync._fetch_outcome` is the single place that changes
when they learn to.

**A route with no baseline may add but never remove.** A fresh route sits in
`baseline_initializing` until one run completes. The migration from
`pair_managed_keys` adopts ownership — that store already recorded what a pair
wrote — but deliberately does *not* mark the route established, because it never
recorded what the *source* looked like and so cannot answer "did this disappear
since we agreed". Guessing there would let the first run after an upgrade delete
on the strength of a comparison it never made.

**Baselines live beside the Library, not in `profiles.json`.** One entry per item
per category per route runs to megabytes; `profiles.json` is rewritten under a
lock on every profile mutation and deep-copied on every status poll, which is
why `pair_managed_keys` — a fraction of the size — already had to be dropped from
`/status`. `CrossSyncService.route_states` is likewise server-side only and is
deliberately *not* folded into `PairCategoryStats`, which is serialized into
`last_pair_results` and does ride the poll.

**A large deletion is verified, a small one is not.** `_verify_removals` re-reads
the destination after removing `_VERIFY_REMOVALS_ABOVE` items or more and
reports any that are still there. An adapter's "deleted: 12" is what it *sent*,
and a provider that silently no-ops leaves the next run planning the same
deletion forever while the count going down gives nobody a reason to look. Small
removals are deliberately not verified — the point is to catch the expensive
mistakes, not to double every sync's request count.

**Duplicate plays are found by scanning, and removed only on request.**
`sync/duplicates.py` applies the same tolerance window backwards over stored
history: plays of one episode chained within the window are one viewing recorded
several times, plays weeks apart are separate viewings. The engine will never do
this on its own — to a union a duplicate is indistinguishable from a rewatch it
must preserve — so `/api/profile/history/duplicates` scans by default and
requires `confirm` *plus* an `expected_redundant` count matching the scan before
it deletes anything. The earliest play of each cluster is kept, and an
incomplete PMDB read refuses the scan outright, since half the history looks
like half the duplicates.

**Clearing the Library drops its routes' baselines with it.**
`/api/profile/data/clear-library` empties `LibraryStore` and then calls
`forget_route` for every pair with `library` at either end. Clearing the store
alone is a trap: a route reading *from* the Library holds a baseline saying its
source had thousands of items, so an empty Library reads as the user having
deleted them all. The safety guard would refuse the removal — a source returning
nothing while the destination holds plenty is a hard block — but the route would
then sit against that block instead of starting over. Dropping the baselines
puts those routes back in `baseline_initializing`, where they may add and may
not remove. Routes that never touch the Library keep theirs.

**The Library hub is recommended, never imposed.** With three or more services
connected and no Library route yet, the quick-setup builder explains why N
two-way routes through the Library beat the N×(N−1) it takes to wire every pair
directly: each service is read once per sync rather than once per route, and
there are no loops for a deletion to travel. Existing direct routes keep working
and "Add advanced route" still builds them.

**Route shapes are named, never refused.** `sync/topology.py` flags two one-way
routes pointing at each other (one two-way route reconciles from a single
baseline; two decide independently and re-add each other's deletions) and cycles
of three or more (additions still settle, but a deletion can travel the loop and
return as an addition). Several routes writing one destination is reported as
information only — it is legitimate and common, and the ownership rules already
handle it. Nothing is forbidden: the user may have a reason, and refusing to run
would be worse than the shape.

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

**Managed keys never ride the status poll.** `_public_profile` returns
`activity_state` through `_public_activity_state`, which ships the four cursors
and deliberately drops `pmdb_watchlist_managed_keys` and `pair_managed_keys`.
Those are server-side ownership bookkeeping — every reader
(`_config_from_profile`, both `CrossSyncService` call sites) works off the
*private* profile and no client has ever read them — but they grow with one key
per item per pair per category, history down to episode granularity, so a real
profile carried ~200KB into every response. `/status` polls every 2s during a
sync and the whole dict was deep-copied under the store lock each time. The
cursors stay because the clear-cursors endpoint reports them back to confirm the
reset. There is a regression test asserting the keys are absent from `/status`
and still present on the private profile.

For the same reason `_public_profile` does **not** copy `unresolved_items`: it is
only counted and aggregated there, never returned (the UI fetches it from
`/unresolved`), and it is unbounded. And `update_sync_status` /
`update_sync_progress` return `None` — they fire many times per run from the
pipeline's callbacks and every caller discards the result, so building a public
profile in them was work no one read.

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

**Pairs are the headline; the PMDB pipeline is the legacy path.** The Sync view
and the dashboard both lead with pairs, and the original service→PublicMetaDB
pipeline sits below them under "PublicMetaDB Pipeline". It is *not* deprecated
and nothing about it changed — a pair can express everything it does, but
existing profiles keep running on it. Do not re-order these back: two panels
that do the same kind of thing read as one feature, and the one users should
reach for has to come first.

**A pair's `visibility` describes lists it creates, never lists that exist.**
`SyncPair.visibility` is `private` (default) or `public` and is passed to
`adapter.add()` only when the adapter sets `supports_visibility` — true for
PublicMetaDB and Trakt, the only writable providers with a notion of list
privacy, and the same flag hides the control in the pair editor everywhere else.
Gating the *call* on the flag (`cross_sync._add_kwargs`) rather than widening
every adapter signature keeps providers that ignore privacy — and their test
doubles — out of it. Two things this must keep right: an unknown or missing
value resolves to `private`, because publishing someone's watchlist over a typo
is the one unacceptable failure; and a list that already exists is never
re-flagged, so a pair writing into a list the user made private on Trakt does
not make it public.

**An unusually large removal is paused, not performed.** A source that answers
with far less than it holds — a half-read page, a token that just expired, an
outage — is indistinguishable from the user having deleted everything, except by
how much it would destroy. `CrossSyncService` takes `guard_large_removals` /
`guard_removal_percent` (from `SyncConfig`, defaulting on at 20%) and
`_guard_blocks` refuses a removal over the threshold, recording it in
`PairCategoryStats.blocked_removals` and surfacing it on the Issues page. Two
things it must keep right: small lists are exempt (`_GUARD_MIN_TARGET_SIZE` /
`_GUARD_MIN_REMOVALS` — removing 2 of 3 items is 67% and entirely ordinary, and
pausing it would be noise); and in a two-way pair a blocked side's keys are
excluded from the *adds* as well, and stay in the agreed managed set, so the run
is a genuine no-op for them rather than the other service re-adding them.

**Manual anime mappings are reviewable and undoable.** They were already
persisted by `resolve_unresolved_item` and already reused by the matcher;
`list_anime_mappings` / `remove_anime_mapping` (behind
`/api/profile/anime/mappings` and `/api/profile/anime/mappings/delete`) only make
them visible. Removal has to clear all four homes together —
`manual_resolution_cache`, the live `resolution_cache`, `anime_manual_overrides`
and the `anime_review_decisions` entry — or the UI would say the override is gone
while the resolver kept applying it.

**Sync pairs are additive by default.** `removal_mode` is `additive` (never deletes),
`managed` (deletes only keys this pair previously wrote, so manual additions on
the target survive — the same invariant as `pmdb_watchlist_managed_keys`), or
`mirror` (deletes anything the source lacks). An unrecognised mode must refuse to
delete rather than fall through to a destructive default. Managed keys are scoped
per pair id, so duplicate ids would let two pairs delete each other's items —
`_normalize_sync_pairs` assigns and de-duplicates them.

**The Library is a local provider, and the hub.** `src/library_store.py` is
SyncMeta's own store; `LibraryAdapter` exposes it as a provider so any service
can sync into it and out of it. It is always writable — no credential, no rate
limit — which is what makes it usable as the middle of a fan-in/fan-out. It is
first in `PROVIDER_ORDER` and its adapter is built for every profile, so
`_build_provider_adapters` takes a `profile_id`; a call site that forgets it
silently drops Library from the pair editor.

Because it is always configured *and* listed first, a new pair must not simply
take the first usable source — that made every pair open as `Library → …`, the
one direction a new user does not want yet. `addPair()` defaults to the first
connected **remote** source with **Library as the target**, which is the fan-in
the library exists for, and the pairs empty state offers exactly that pair.

**`providers.ADAPTER_TYPES` is the only provider registry.** Adding a provider
means adding its class to `_ADAPTER_CLASSES`; `ADAPTER_TYPES`, `PROVIDER_ORDER`
and `PROVIDER_LABELS` are all derived from it. `/api/profile/pairs/save` used to
validate against its own hand-written copy of that mapping, which never learned
about Library — so the editor offered Library at both ends and every pair using
it came back `unknown provider` and was lost. Do not re-declare the mapping at a
call site; import it. A rejected save also returns `pair_index`, and the editor
opens and outlines that card — the message names "Pair 3" and counting collapsed
cards is not the user's job.

**One entry per series, seasons inside it.** This is the TVDB/Trakt shape and
the only one in which SIMKL and AniList can agree: SIMKL lists an anime per
season, AniList per cour, often with a different id each. `series_key()` is
therefore *series*-level and TMDB-first — an anime-native id is the fallback so
an unmapped AniList entry is still storable, but it is deliberately last, since
keying on it would put two seasons of one show in two rows and lose the whole
point. Identity merging is fill-only: a later source may add an id or title the
first lacked but must never overwrite one, or the entry would flip between
romaji and English on every run. Anime-ness is sticky for the same reason — a
provider that does not model anime must not downgrade an entry another one
already identified.

**Watched state is per episode, stored sparsely.** `{season}x{episode}` keys, a
movie at `0x0`. A show play with no episode number is *dropped*, never guessed:
inventing one claims episodes nobody watched, which is exactly what SIMKL's
aggregate counts would produce. Season 0 is specials and is kept.

**PublicMetaDB can remove watch history — the unit is the episode.**
`DELETE /api/external/watched` deletes every play matching a filter, and
`bulk_delete_watched` has always existed on the client, but `PmdbAdapter.remove`
had no `CATEGORY_HISTORY` branch and fell through to `_unsupported`. Since
`writes` advertised the capability, a pair validated fine and then failed
mid-run with "PublicMetaDB cannot remove from 'history'". Removing the whole
episode rather than one play record is deliberate and matches every other
provider — Trakt's `/sync/history/remove` takes a seasons/episodes tree and
clears each episode outright — and deleting a single record would leave that
episode's rewatches behind for the next run to find and try again. The
episode-scoped rule below applies in this direction too: a row naming a show but
no episode would wipe that show's entire history, so it is dropped.

When adding a category to an adapter's `writes`, implement **both** `add` and
`remove`: the declaration is what pair validation trusts, so a missing branch
does not surface until a real run is halfway done.

**A history row that names no episode is never written to any service.** Trakt,
SIMKL and MDBList all read a show entry carrying no `seasons` tree as *the whole
show*, so flattening one watched episode to its series is not a smaller write —
it marks every season the user has never seen. `providers.is_episode_scoped` /
`episode_scoped_history` state the rule, each client's payload builder takes an
`episode_scoped` flag for its history endpoints, and dropped rows are counted
into `not_found` rather than silently discarded. Watchlist and collection are
untouched by this: there a bare show entry genuinely *is* the item. MDBList had
the worst version of it — `_to_sync_payload` dropped season/episode outright, so
every episode written to `/sync/watched` marked the whole show; it now sends the
Trakt-shaped tree, and `_from_sync_payload` reads that tree back one row per
episode so the next run does not see the whole show as unsynced. SIMKL also
stamped the play date on the *show*, which put the first episode's date on every
other episode grouped into the same entry; `watched_at` now rides the episode.

**The same viewing, reported twice, is one play.** Services do not agree on
*when* a play happened — Trakt stamps the scrobble, SIMKL stamps when its server
recorded it, an importer stamps whatever it was handed — so the same watch
arrives seconds or minutes apart. Matched on the exact second it looked like a
fresh rewatch at every hop, so one viewing multiplied into one play per service
and grew on every run. `providers.PlaySet` matches within
`PLAY_MATCH_WINDOW_SECONDS` (default 900, `SYNCMETA_PLAY_MATCH_WINDOW`, exposed
in the admin panel), bisecting a sorted list because it is consulted once per
source row. The window is wider than provider drift and narrower than a real
repeat viewing, which cannot happen faster than the runtime. `_history_adds`
seeds one ledger per episode from the target's stamps and updates it as rows are
accepted, so two source rows a few seconds apart cannot both be written either;
`library_store._record_extra_play` uses the same ledger, which is also what stops
an entry written before `plays` existed gaining a phantom second play.

**Watching something twice is two rows, and both have to arrive.** `item_key`
answers "which episode", so every play of one episode shares a key — diffing
history on identity alone keeps the first row and discards every rewatch, which
is why a second viewing never reached the other service. `cross_sync._history_adds`
matches a play on identity *and* `providers.normalize_watched_at` (UTC seconds;
sub-second precision is dropped because no provider round-trips it, and two
spellings of one instant must not read as two plays), and it is used by both the
one-way and the two-way path.

Extra plays only go to a target that can hold them. `ProviderAdapter.records_plays`
declares that — true for Trakt, PublicMetaDB and the Library, false for SIMKL,
AniList and MDBList, which report watched *state*: one row back however many
plays they were sent, so writing a rewatch there would leave it looking missing
on every subsequent run and the pair would re-send it forever. It is declared,
never sniffed. An episode the target lacks entirely is always added, whatever
kind of target it is. `ReadCache.apply_write` is likewise exempted for history:
its usual replace-on-add would drop the older play and make a rewatch just
written look like the only one.

**The Library keeps every play, not just the first.** A slot in `watched` still
holds one date, because that is what the coverage views read; `plays[slot]` is
the full list, and `fetch("history")` emits one row per play. Two rules keep it
from inventing history: a row carrying no timestamp of its own is *presence* — a
watched-state read, an AniList progress count, a SIMKL aggregate — and can only
ever confirm what is stored; and a `cursor_exempt` or `anilist_derived` row is
never a rewatch, since its date is the entry's and moves whenever the entry is
touched. An entry written before `plays` existed is seeded from its one date, so
its first play is not recounted as a second.

**"Anime" is a flag on a TMDB namespace, never a third namespace.**
`src/media_kind.py` classifies into movie / show / anime / anime_movie. The
namespace comes from TMDB (or Fribb's mapping key); the anime flag comes from
anime-native ids or a source explicitly saying anime. Taking SIMKL's `anime`
media type as a namespace is what made anime films into fake one-episode TV.
An empty filter selection means everything, so an untouched filter never hides
anything.

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

**Trakt history is a play log; Trakt watched state is the truth.**
`/sync/history` is paginated and read forward from `trakt_history_cursor`, so an
episode missed on an earlier run — an aborted sync, a resolver failure, plays
scrobbled before the account was connected — is never revisited, and the earlier
seasons of a long-running show stay permanently absent. `/sync/watched`
(`TraktClient.get_watched_state`) answers the other question in one unpaginated
response: every watched episode of every show, season by season.
`_reconcile_trakt_watched_state` runs it behind
`options.trakt_reconcile_watched_history` (and always under `full_history_sync`).

Four things this has to keep right:

* **It writes presence, not plays.** `/sync/watched` carries one
  `last_watched_at` per episode, never the individual play timestamps, so an
  episode is written only when PMDB has *no* record of it. A fully imported show
  writes nothing on the next run — there is a regression test on that. Trakt's
  `plays` count rides along as data and is deliberately not expanded into
  records: every copy would carry the same timestamp and PMDB's `dedupe=true`
  write collapses them anyway. Rewatches stay the play log's job.
  (`trakt_sync_full_watch_counts` is still unwired for exactly this reason.)
* **It never moves the cursor.** Watched-state rows are `cursor_exempt`;
  `stats.history_cursor` is computed from the play log alone. A `last_watched_at`
  far ahead of the cursor would otherwise skip every real play between the two.
* **It runs last**, after `_write_watched_history_items` has refreshed
  `existing_counts` from PMDB, so it only fills what is genuinely still missing.
* **A failed watched-state read is recorded, not fatal** — the incremental
  import in the same run is still valid.

It is off by default because it is a second whole-account read per history run.
SIMKL needs no equivalent: `/sync/all-items` already reports per-episode watched
state, and its multi-season anime problem is aggregate counts, which a different
endpoint does not solve.

**A season's coverage is only claimed when TMDB supplied the total.**
`/library/history/title` returns `seasons: [{season, watched, total}]`, and
`total` stays 0 without a TMDB key — the UI then groups by season and prints the
count without a bar. Drawing a coverage bar against a guessed episode count is
worse than drawing none, since the whole point of the view is to show which
episodes of a multi-season show are *missing*.

**SIMKL reconciliation is the window, not the endpoint.** SIMKL needs no second
endpoint — `/sync/all-items` already reports per-episode watched state. What its
cursor costs is the *window*: `date_from` hides everything older, so an episode
missed on an earlier run is never offered again. `simkl_reconcile_watched_history`
therefore drops the filter and reads the whole state, letting the existing
per-key dedupe skip what PMDB already holds; it also re-enables the
completed-anime fallback fetch, which is otherwise skipped on cursor runs and is
exactly the context a reconciling run needs. `items_reconciled` counts adds
older than the cursor and is clamped to `items_added`, since the writer reports
successes in aggregate — it is a bound, not a separately verified count.

**A cursor_exempt row never advances a history cursor.** `_latest_history_cursor`
skips them. These rows carry a *state* timestamp — a series-level
`last_watched_at`, or one synthesized from an aggregate count — not the moment
of a play, so letting one set the cursor would skip every real play recorded
between the old cursor and that date. SIMKL's synthesized rows and every Trakt
and AniList watched-state row carry the flag.

**AniList history is derived, and says so.** AniList records no watch history:
no play log, no per-episode record, no per-episode timestamp. `progress: 12` is
all that exists. `AniListClient.get_watched_history()` turns that into episodes
1-12 of the entry, stamped with the entry's own `completedAt` (falling back to
`updatedAt`), and `_sync_anilist_watched_history` places them via the same
anime remapper SIMKL's aggregate counts use. This is opt-in as the `anilist`
choice in `activity_history_source`, and the UI states the trade-off.

Rules it must keep: rows are `cursor_exempt` **and** `anilist_derived`, and the
pass writes **presence only** — an episode PMDB already has is never written
again, so a rewatch is not representable and a second run writes nothing (there
is a regression test). An entry with no `completedAt` *and* no `updatedAt` is
**skipped, not written**: stamping today's date onto a years-old watch is worse
than omitting it. Progress is clamped to the entry's own episode count (AniList
disagreeing with itself) and to `_MAX_DERIVED_EPISODES`. PLANNING is excluded.
Prefer Trakt or SIMKL for anything they track — those are reported plays; this
is an approximation.

**In pairs, AniList history is readable and never writable.** `AniListAdapter`
advertises `CATEGORY_HISTORY` under `reads` only, offered as one account-level
`history` source (status chips scope list reads, not history — the same rule as
SIMKL and Trakt). A pair carrying those rows onward is exporting derived dates,
so the pair editor states that where the pair is built; the receiving service
cannot tell them apart afterwards.

**Writing history to AniList is a progress count, and `anilist_progress` owns
every rule that makes it safe.** A history row arriving at AniList is TMDB-keyed
season/episode, while AniList stores one *absolute* progress number per cour,
and `save_entry` sets that number outright — a wrong answer does not add a
spurious row, it overwrites what the user actually watched.

The inverse mapping is therefore built by running the **forward** mapper
(`providers.enrich_identity`) over the user's *own* AniList entries and
recording where each local episode lands. Two properties fall out of that and
must be preserved if this is ever rewritten: the inverse cannot disagree with
the forward mapping, because it *is* that mapping read backwards; and only
entries already on the user's list can ever be written, since nothing else is
in the index — a history row for an anime they do not track is skipped, never
turned into a new list entry.

Four refusals do the rest, and none is optional:

* **An ambiguous coordinate is refused, not guessed.** Overlapping Fribb
  mappings occur; picking one writes progress to the wrong cour.
* **Progress follows a contiguous run from episode 1.** The number means
  "watched up to N", so episodes {1,2,3,5} support 3, never 5 — 5 would claim
  episode 4. A history starting mid-season supports nothing at all.
* **Progress never decreases.** This is the guard that makes the write
  non-destructive: a partial or stale history must not roll back a count the
  user set themselves.
* **Progress never exceeds the entry's own episode count.**

`remove()` still refuses `CATEGORY_HISTORY` outright: removing history would
mean lowering a count, the one edit the user cannot undo. A mirror or managed
pair reports that rather than acting on it. The entry's own status rides
through unchanged — this writes a count, it does not re-shelve the entry.

Because a two-way pair writes both ends, `/pairs/save` now also checks the
reverse direction (`target.reads` / `source.writes`) for two-way pairs.
Validating only source-read/target-write accepted `AniList ↔ Trakt` on history,
which saves fine and then fails halfway through its first run.

**Pair sources are the service's own lists, not generic categories.** Each
adapter's `list_sources()` returns what that service calls its lists (SIMKL
`status:<name>:<media_type>`, AniList `status:<STATUS>`, Trakt
watchlist/collection/history plus `list:<user>/<slug>`, PMDB `watchlist` and
`list:<id>`, MDBList `list:<id>`), each tagged with the neutral category it feeds.
`SyncPair.source_lists` holds the chosen keys; empty means the provider default.
An adapter must not fall back to a whole category when the selection names only
other categories — that would silently sync far more than asked.

**PMDB's native lists are matched by type, never by name.**
`publicmetadb_client.NATIVE_LIST_TYPES` is `{"watchlist", "picks"}` — singletons
the account owns, as opposed to the custom lists SyncMeta names and creates.
`get_or_create_list` resolves those through `find_list_by_type`, because the user
may well have renamed theirs and matching on our label would create a second one
beside it. `PmdbAdapter` offers each under its own key (`watchlist`, `picks`) and
skips that type in the generic list loop, so a renamed Picks is not also offered
as `list:<id>` — two entries for one list means half the pairs write to a list
that merely looks right. Picks feeds `CATEGORY_WATCHLIST`, the same category the
generic custom lists use.

Two things this has to keep right: picks is read **only when explicitly
selected** (folding it into the default would make every plain watchlist pair
quietly sync a second list), and a read never creates it — only a write does,
since conjuring a Picks list on an account that never had one, just to report it
empty, is the kind of side effect a source is not allowed to have.

**PublicMetaDB's native watchlist is a plan-to-watch list, and only that.**
Several unrelated things map onto `CATEGORY_WATCHLIST` because it is the only
category that fits them — a curated MDBList list, a Trakt personal or liked
list, PMDB's own Picks — but none of them means "plan to watch". Treating the
category as if it did filled a real watchlist with 4,560 entries that mirrored
the user's collection almost exactly.

`providers.PLANNED_FLAG` is the answer: every adapter stamps each watchlist item
with whether the list it came from actually means plan-to-watch — true for SIMKL
`plantowatch`, AniList `PLANNING`, Trakt's own watchlist, MDBList's own
`/watchlist` and PMDB's native watchlist; false for every curated or custom list.
`is_planned` returns True/False/**None**, and None is load-bearing: an item
stored before the flag existed, or produced by a third-party adapter, is accepted
rather than silently dropped. Only an explicit False is refused.

Three things this has to keep right:

* **The refusal shapes the diff, not just the write.** `ProviderAdapter.accepts`
  is consulted in `_run_category` *before* the source set is built, and refused
  items are counted into `PairCategoryStats.skipped_unsupported`. Filtering only
  at the write would leave the item in the source set looking present, so a
  stale copy already on the target could never be recognised as stale — and the
  entries the user wants gone could never be removed. `PmdbAdapter.add` filters
  again anyway, so a direct adapter call cannot bypass it either.
* **A named destination list is exempt.** `target_list` starting with `list:`, or
  `picks`, means the user chose that list, and the plan-to-watch rule does not
  apply there.
* **The Library must not launder a curated list.** It is the hub, so what it
  cannot answer the next service downstream has to guess at. It used to record
  *any* watchlist-section item as "Planning" whether or not the source said so,
  which is exactly how MDBList lists reached PMDB looking like plan-to-watch. It
  now persists `entry["planned"]` from the flag, emits it on a watchlist read,
  and clears it when the item leaves the watchlist section. Being planned on any
  one source wins — the same title can sit in a curated list *and* genuinely be
  planned elsewhere.

Removing the entries that should never have been there is `removal_mode`'s job,
not this rule's: `managed` deletes only keys the pair itself wrote, so anything
added to PMDB by hand survives. The large-removal guard still applies, so a
first cleanup run of thousands of entries is paused and reported rather than
performed.

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

**Either MDBList credential is complete on its own — ask, never assume the key.**
The client prefers the bearer and never sends both, so a profile that finished
the OAuth flow has *no* api key at all. Every "is MDBList set up?" check must
accept either, and each one that did not is a way a working login looked broken:
`_configured_sources_for_profile`, `validate_config`, `connection_health`'s
`_configured` and its probe, `/api/mdblist/lists`, and the UI's connection dots
and list-refresh gates (`mdblistHasCredential()`). Note the source list and the
validator are coupled — teaching only the former to accept OAuth makes
`validate_config` demand an api key and blocks the save.

**MDBList tokens expire, so the refresh must be wired, not merely written.**
`refresh_access_token` existed with no callers, so an OAuth connection died
silently 30 days after it was made and the only cure was redoing the whole flow.
It now follows Trakt's shape: refresh ahead of expiry
(`TOKEN_REFRESH_SKEW_SECONDS`) and once more on a 401, retry the request, and
hand the rotated pair to `token_refreshed_callback` so it is persisted — an
in-memory refresh is lost on the next process. `_attempt_token_refresh` adopts
the new credential itself rather than trusting `refresh_access_token` to have
done it, because that is what the retry path reads. Three rules: only refresh
when a bearer is actually in use (an api-key 401 is a bad key, and retrying is
pointless), only once per client instance (the refresh token rotates, so a
second concurrent exchange replays a spent token), and a 401 *is* safe to repeat
even for a write — like a 429 it is rejected before any work, unlike the 5xx and
timeout cases `retry_on_rate_limit` deliberately refuses.

**MDBList auth is two modes.** An `apikey` query parameter (read, and what
existing profiles have) or an OAuth `Authorization: Bearer` token; the client
prefers the bearer and never sends both. OAuth is **authorization code + PKCE**
— MDBList requires PKCE for every client *and* still requires the client secret.
Authorization is on the site host (`mdblist.com/oauth/authorize/`), token
exchange on the API host, and **every OAuth path needs its trailing slash** or
the request fails. Tokens last 30 days and refresh. The PKCE verifier is held
server-side in `PendingPkceStore` and is single-use: it is the proof the code
belongs to the flow that started it, so it must never round-trip through the
browser. It is keyed on a per-flow id carried in an **http-only cookie**, not on
the profile — the connect flow has to work before a profile exists, and keying
it on the profile made MDBList the only service that could not be connected
during first-time setup (Trakt's device flow always could). The owning profile
id rides along in the entry purely so `clear_profile` can still purge a deleted
profile's unfinished flows. Like Trakt's device check, `/auth/check` persists
straight to the profile when there is a session and otherwise returns the tokens
for the browser to submit with the profile — and withholds them from the
response whenever it did persist them. `/api/mdblist/auth/check` persists via
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

**The anime mappings are prewarmed at startup, not on first lookup.** Both
`AnimeMappingStore` sources load lazily, which meant the first anime lookup of a
process downloaded and indexed ~43k Fribb entries plus a large XML *inside a
sync*, on a worker thread, holding the store lock — every other anime lookup in
that run queued behind it, and a slow GitHub put that cost on the user's sync.
`_start_anime_mapping_prewarm()` in `web.py` runs `anime_mapping_store.prewarm()`
on a daemon thread after the scheduler's startup grace. It must never raise: a
mapping that cannot load degrades matching, it does not stop the app. Failures
are recorded in `_load_errors` and surfaced by `cache_metadata()` as
`fribb_error`/`xml_error`, because the admin panel's old "Not loaded / Never"
could not distinguish a broken download from "nothing has asked for it yet" —
and a silently unloaded mapping looks exactly like anime that cannot be matched.

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
