

# SyncMeta

[![Deploy to Docker](https://github.com/Febsho/SyncMeta/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Febsho/SyncMeta/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Self-hosted web app for synchronizing watchlists, collections, custom lists,
watch history and resume progress between SIMKL, Trakt, AniList, MDBList,
[PublicMetaDB](https://publicmetadb.com), and a local Library.

You run it in Docker, open it in a browser, connect your accounts, and it syncs
in the background.

## Quick start

```bash
git clone https://github.com/Febsho/SyncMeta.git
cd SyncMeta
cp .env.example .env
docker compose up -d syncmeta
```

The `.env` file is optional; `docker compose up -d syncmeta` also works without it.

Open `http://127.0.0.1:8080` and:

1. Enter a password and click **Save Profile**. You get a profile UUID back —
   copy it and keep it with the password. Together they are the only way back
   into the profile.
2. In **Settings -> Connections**, connect the services you want to use. A
   PublicMetaDB connection is optional; the local Library is always available.
3. In **Settings -> Sync routes**, choose a source, a destination, and the
   content to synchronize. Quick setup can create several routes to one target.
4. Click **Preview** to perform a dry run, then save and run the routes.

New routes are manual by default. Enable automatic sync and choose an interval
on each route when you want it to run in the background.

## What it does

**Sync routes.** A route copies items directly between any two compatible
services; PublicMetaDB is a normal source or destination, not a required hub. A
route can be one-way or two-way, chooses what happens when an item disappears
from one side (never remove, remove only what this route added, or mirror the
source exactly), and has its own automatic schedule. Where a target can create
lists, the route can select the destination and its initial visibility. An
existing list keeps the visibility configured on the service itself.

For SIMKL → Trakt, Plan to Watch maps to Trakt's native watchlist. Watching,
Completed, On Hold and Dropped are kept in separate Trakt lists that SyncMeta
creates or reuses, rather than being flattened into Trakt Collection.

| Service | Reads | Writes |
|---|---|---|
| SIMKL | Watch statuses for shows, movies and anime; history; resume | Watchlist, history and collection |
| Trakt | Watchlist, collection, history, personal and liked lists; resume | Watchlist, history, collection and personal lists |
| AniList | Status lists, custom lists and activity-derived history | Status lists and progress/history with an access token |
| MDBList | Watchlist, collection, history, account lists and public lists | Watchlist, collection, history and account lists |
| PublicMetaDB | Watchlist, Picks, collection, custom lists, history and resume | The same |
| Library (local) | All titles, watchlist, collection, history and resume | The same, always |

The source-list picker loads each provider's actual lists, not only its default
watchlist. Small catalogs appear as direct choices; larger catalogs can be
filtered in the picker. PublicMetaDB's native **Watchlist** and **Picks** lists
are always exposed even when its generic list endpoint omits them.

**Watch history and resume progress.** Both are normal route categories. Resume
can be read from SIMKL, Trakt, PublicMetaDB or Library, and written to
PublicMetaDB or Library. The route editor only offers directions that both ends
actually support.

Watch history is treated as a set of *events*, not a list of items. Syncing the
same watch twice never produces two plays, and watching something twice does
produce two — see [How syncing decides](#how-syncing-decides). Resume points are
never rewound: a position under 2% is treated as an accidental open, 90% or more
as a finished title rather than something to resume, and a service already
further along is left alone.

**MDBList OAuth.** An API key is enough to read and may also provide write
access. For OAuth, create an app at
[mdblist.com/developer](https://mdblist.com/developer/), paste the client id and
secret in Connections, and set the exact Redirect URL shown by SyncMeta on the
MDBList app. Press **Connect MDBList**, approve with **YES**, and the browser
returns to SyncMeta to finish the connection automatically; there is no code to
copy. MDBList marks its sync API as beta, so preview with a dry run before
trusting a real run.

**Provider-neutral behavior.** Scheduling, removal rules, watch history and
resume progress belong to each route rather than to a PublicMetaDB-only
pipeline. Older profile data is still understood and converted for
compatibility, but new configuration happens entirely through Sync routes.

**Anime matching.** Anime is matched across AniList, MAL, SIMKL, TMDB and IMDB
using the Fribb anime-lists data, with sequel seasons resolved back to the root
series. Anything it cannot match with confidence is listed as unresolved for you
to map by hand rather than guessed at.

**Library.** SyncMeta's own local store, and a sync source or target like any
other service — point every service at it once and any other route can read from
it without touching a remote API again. It holds **one entry per series with the
seasons inside it**, the shape Trakt and TVDB use, which is what makes SIMKL's
per-season anime entries and AniList's per-cour entries land on the same row
instead of three. Filter by **movies, shows, anime and anime films** (anime is
tracked as a flag on the TMDB type, so an anime film is a film), by watchlist /
collection / watched / resume, search by title, and sort or page through large
libraries. Click a title to see its seasons and exactly which episodes are
watched. Posters, titles and episode names need a free TMDB API key; without one
it still works and shows ids and episode numbers.

The same Library page can browse the connected SIMKL, Trakt, AniList, MDBList
and PublicMetaDB accounts directly. Their native feeds and service lists are
available as separate choices, including AniList custom lists and PublicMetaDB
Picks. Remote results support search, sorting and pagination, and AniList
episode activity is grouped into one series card instead of repeated cards.

**Dashboard.** The sync graph is built from the saved routes, groups routes by
destination, links to provider details, and can be collapsed when you do not
want to see it. Live status, recent activity and issues remain visible while a
background run is in progress.

**Diagnostics.** Per-list results, row-level errors, failed and unresolved
samples, timings, and the last 25 detailed run records. A dry run previews
everything without writing.

**Connection health.** The Connections screen verifies every configured
provider with a read-only request, reports read/write capability and the last
check time, and offers the appropriate reconnect, edit, or retry action. The
dashboard shows whether the services required by each configured route are
ready before you start a run.

**Admin page.** Set `ADMIN_PASSWORD` to enable `/admin`: profile overview, queue
state, API request counters, anime cache repair and anime mapping refresh.

## How syncing decides

A sync is not a comparison of two lists. Comparing them tells you they differ;
it cannot tell you *which side moved*, and "missing on the destination" is
either something you just added on the source or something you just deleted on
the destination. Those want opposite actions.

So every route keeps a **baseline**: the state both sides were last confirmed to
agree on. Each run compares both current states against that baseline rather
than against each other, and builds a plan before writing anything.

    read → normalise → resolve identities → compare against the baseline
      → plan → safety check → write → record what actually landed

**What this means in practice**

* A route will not delete anything until it has one confirmed sync behind it.
  The first run of a new route — and the first run of every existing route after
  upgrading to this version — adds but never removes. The run after that behaves
  normally.
* A failed or incomplete provider read never causes a deletion. A timeout, an
  expired token, a rate limit and a half-read page all look identical to "the
  user deleted everything" if you only count what came back, so none of them is
  allowed to justify a removal.
* An item that was on the destination before the route existed, or that you
  added by hand, is not the route's to delete. Only `mirror` touches those.
* An item two routes both feed is only removed once *no* route still requires
  it, so SIMKL and MDBList both feeding Trakt do not fight over a title that
  left one of them.
* A run where some writes failed is recorded as partial. The next run retries
  what is still outstanding and does not repeat what landed.

**The safety guard.** A removal of more than 25 items, or more than 20% of the
destination, is paused rather than performed, and the preview says which
threshold stopped it. Small lists are exempt — emptying a three-item list is
100% and entirely ordinary. You can override a paused threshold from a preview;
an automatic run never can. Some blocks cannot be overridden at all: an
unreadable source or destination, a missing baseline, or a source returning
nothing while the destination holds plenty. You may decide a large deletion is
right; nobody can decide that a failed read really was empty.

**Preview.** A dry run shows exactly what would happen, grouped into what will
be added, updated and removed, plus conflicts, unresolved titles and warnings.
Every row carries its reason — "Removed from the source since the last sync",
"Destination item is unmanaged; keeping it". It is generated by the same planner
that performs the real sync, so it cannot disagree with what actually runs.

**Two-way routes** reconcile in a single pass rather than running one direction
after the other. If only one side changed since the baseline, that side wins,
whichever it is. If both changed and now agree, nothing happens. If both changed
and still disagree, it is reported as a conflict and neither side is touched —
letting run order pick a winner would silently discard one of your two edits.

**Watch history** is a union. An episode missing from one service is not
evidence to delete it elsewhere, because services expose different windows of
the same history. The same watch arriving twice is recognised three ways: by the
source's own event id, by this route's record of what it already carried, and by
matching timestamps within a tolerance window — services stamp the same viewing
minutes apart, so an exact match would make every hop look like a rewatch. A
genuine second viewing is still a second play, and only goes to services that
can store one.

**Cleaning up duplicate plays.** Earlier versions matched plays on the exact
second, so one watch relayed through several services could be recorded more
than once. That no longer happens, but plays already written stay where they
are — the engine will never remove them on its own, because to a union a
duplicate looks exactly like a rewatch it should preserve.

`POST /api/profile/history/duplicates` scans for them and reports what it found;
it changes nothing. To actually delete, repeat the call with
`{"confirm": true, "expected_redundant": N}` where N is the count the scan
returned — a stale page cannot delete more than you agreed to. The earliest play
of each group is kept, being the one closest to when you actually watched, and
a genuine rewatch weeks later is never touched. If PublicMetaDB returns only
part of your history the scan refuses to run at all, since half the history
looks like half the duplicates.

**Clearing the Library.** The Library page and the Danger Zone both offer
*Clear Library*, which empties SyncMeta's own copy of your titles, watch history
and resume points. Nothing is removed from SIMKL, Trakt, AniList, MDBList or
PublicMetaDB — it only clears the local store.

Routes that use the Library have their baselines dropped along with it, so they
start over: the next run may add, but will not remove anything until it has
completed once. Without that, a route reading *from* the Library would see its
source go from thousands of items to none and read it as a mass deletion.

**Route shapes.** The route editor warns about configurations that fight — two
one-way routes pointing at each other (better as one two-way route) and loops
(A → B → C → A, where no service is the authority). Neither is forbidden.

## Profiles and access

Each profile has its own credentials, list choices, history and schedule.
Credentials are encrypted at rest and are never sent back to the browser.

- Signing in needs the profile UUID and its password.
- Changing the password needs the current password, whether or not you are
  signed in. The UUID alone is not enough, because it is not a secret.
- A password change signs the profile out of every other browser.
- There is no password recovery. If both are lost, the profile is lost.
- `SITE_ACCESS_PASSWORD` optionally puts one shared password in front of the
  whole app before any of this.

## Scheduling

Defaults are deliberately gentle so SyncMeta does not crowd out other containers
on a small server.

| Sync | Default | Minimum |
|---|---:|---:|
| Each sync route | manual | 12h when automatic |

Automatic runs are staggered by a per-profile jitter so profiles do not all
start at once. Manual sync and dry run are always immediate.

## Environment variables

Only server-level settings belong in `.env`. API keys, list choices and sync
rules are per profile in the web UI.

Most of the variables below can also be edited in the admin panel
(`/admin`, needs `ADMIN_PASSWORD`), which is the easier route on Docker: the
`.env` file lives on the host, so there is nothing to edit from inside the
container. Panel edits are stored in `data/settings.json` — beside
`profiles.json`, on the mounted volume, so they survive a rebuild — and applied
over the environment at startup. Precedence is:

```
built-in default  <  environment (.env / compose)  <  admin panel
```

Each setting shows where its current value came from, and whether it needs a
restart to take effect. `SYNCMETA_MASTER_KEY`, `SYNCMETA_MASTER_KEY_FILE`,
`SYNCMETA_SESSION_SECRET` and `PROFILE_STORE_FILE` are shown but deliberately
not editable there — a typo in any of them is data loss, not a bad setting.

Worker counts are clamped to the maximum shown; values above it are capped
rather than rejected. Where the shipped `.env.example` sets something lower than
the code default, both are listed.

### Storage and encryption

Sync baselines live in `data/sync_state/<profile>.json`, beside the local
Library, one file per profile. They hold one entry per item per category per
route, which is far too much to keep in `profiles.json` — that file is rewritten
on every profile change and read on every dashboard poll. Deleting a baseline
file is safe: the affected routes re-enter their initialising state, so they add
but do not remove until they have completed a run again.


| Variable | Default | Description |
|---|---:|---|
| `PROFILE_STORE_FILE` | `/app/data/profiles.json` | Profile database. Mount `/app/data` or you lose everything on redeploy. |
| `SYNCMETA_MASTER_KEY` | generated | Fernet key encrypting stored credentials. Must stay stable across restarts. |
| `SYNCMETA_MASTER_KEY_FILE` | `/app/data/profiles.key` | File used when `SYNCMETA_MASTER_KEY` is empty. Never commit it. |
| `ANILIST_ROOT_CACHE_FILE` | `data/anilist_root_cache.json` | Anime prequel-chain cache location. |

### Access control

| Variable | Default | Description |
|---|---:|---|
| `ADMIN_PASSWORD` | empty | Enables `/admin` when set. |
| `SITE_ACCESS_PASSWORD` | empty | Shared password gate in front of the whole app. |
| `SYNCMETA_SESSION_SECRET` | master key | Signs browser session cookies. |
| `SYNCMETA_SESSION_TTL_SECONDS` | `2592000` | Session lifetime, 30 days. |
| `SYNCMETA_LOGIN_MAX_ATTEMPTS` | `10` | Failed profile sign-ins before a lockout. |
| `SYNCMETA_LOGIN_WINDOW_SECONDS` | `900` | Lockout window for sign-in and password changes. |
| `SYNCMETA_ACCESS_MAX_ATTEMPTS` | `10` | Same, for the `SITE_ACCESS_PASSWORD` gate. |
| `SYNCMETA_ACCESS_WINDOW_SECONDS` | `900` | Lockout window for the site gate. |

### Scheduler

| Variable | Default | Description |
|---|---:|---|
| `DISABLE_PROFILE_SCHEDULER` | `0` | `1` turns off all automatic background sync. |
| `SYNCMETA_SCHEDULER_POLL_SECONDS` | `5` | How often due profiles are checked. Minimum 5. |
| `SYNCMETA_MAX_CONCURRENT_SYNCS` | `1` | Profiles allowed to sync at the same time. |
| `SYNCMETA_SCHEDULER_STARTUP_GRACE_SECONDS` | `20` | Head start for the web tier before the first claim. `0` disables. |
| `SYNCMETA_SCHEDULER_CLAIM_BATCH` | max concurrent | Profiles claimed per poll. |
| `SYNCMETA_SCHEDULE_JITTER_SECONDS` | `900` | Maximum stagger applied to automatic runs. |
| `SYNCMETA_LIST_SYNC_JITTER_SECONDS` | schedule jitter | Overrides the above for list sync. |
| `SYNCMETA_HISTORY_SYNC_JITTER_SECONDS` | schedule jitter | Overrides the above for watch history. |
| `SYNCMETA_RESUME_SYNC_JITTER_SECONDS` | schedule jitter | Overrides the above for resume progress. |

### Sync workers

| Variable | Default | `.env.example` | Max | Description |
|---|---:|---:|---:|---|
| `SYNCMETA_SOURCE_SYNC_WORKERS` | `3` | `2` | 4 | Services fetched in parallel. |
| `SYNCMETA_SIMKL_FETCH_WORKERS` | `3` | `2` | 8 | SIMKL status requests in parallel. |
| `SYNCMETA_LIST_RESOLVE_WORKERS` | `2` | `2` | 6 | Id resolution for list rows. |
| `SYNCMETA_LIST_WRITE_WORKERS` | `2` | `1` | 4 | Writes into PublicMetaDB lists. |
| `SYNCMETA_ACTIVITY_SOURCE_WORKERS` | `2` | `2` | 3 | History and resume reads. |
| `SYNCMETA_ACTIVITY_WRITE_WORKERS` | `1` | `1` | 4 | History and resume writes. |
| `SYNCMETA_MAPPING_WRITE_WORKERS` | `1` | `1` | 4 | Mapping contribution writes. |
| `SYNCMETA_PREWARM_WORKERS` | `2` | `2` | 4 | Anime prewarm workers. |
| `SYNCMETA_ANILIST_PREWARM_LIMIT` | `100` | `50` | 200 | AniList root lookups prewarmed per run. `0` disables. |

### Sync behaviour

| Variable | Default | Meaning |
|---|---|---|
| `SYNCMETA_PLAY_MATCH_WINDOW` | `900` | Seconds within which two records of the same episode count as one viewing rather than two. Services timestamp the same play differently, so matching on the exact second turns one watch into a new play at every hop. Raise it if duplicate plays appear; lower it only if you rewatch things faster than this. |

### Network timeouts

Raise these on a slow or congested host, lower them to fail faster. Clamped to
2-180 seconds.

| Variable | Default | Description |
|---|---:|---|
| `SYNCMETA_TRAKT_READ_TIMEOUT` | `20` | Read timeout for Trakt. Was effectively 6s, which large watchlists exceeded. |
| `SYNCMETA_SIMKL_READ_TIMEOUT` | `20` | Read timeout for SIMKL. |
| `SYNCMETA_MDBLIST_READ_TIMEOUT` | `20` | Read timeout for MDBList. |
| `SYNCMETA_ANILIST_READ_TIMEOUT` | `20` | Read timeout for AniList. |
| `SYNCMETA_PMDB_READ_TIMEOUT` | `20` | Read timeout for PublicMetaDB. |

### Logging and serving

| Variable | Default | Description |
|---|---:|---|
| `SYNCMETA_PROFILE_LOG_LIMIT` | `500` | Log lines kept per profile. Minimum 100. |
| `SYNCMETA_GUNICORN_WORKERS` | `1` | Gunicorn workers. **Keep at 1** — see below. |
| `SYNCMETA_GUNICORN_THREADS` | `6` | Gunicorn threads. Raise this, not workers. |
| `SYNCMETA_GUNICORN_TIMEOUT` | `120` | Gunicorn request timeout in seconds. |

### Docker limits

Read by `docker-compose.yml`, not by the app.

| Variable | Default | Description |
|---|---:|---|
| `SYNCMETA_CPU_LIMIT` | `1.0` | CPU limit for the container. |
| `SYNCMETA_MEMORY_LIMIT` | `1536m` | Memory limit. |
| `SYNCMETA_MEMORY_RESERVATION` | `768m` | Memory reservation. |

### Two things to know


**Never run more than one Gunicorn worker.** The scheduler and the sync runner
live inside the web process, so a second worker is a second scheduler claiming
and running every sync a second time. Raise `SYNCMETA_GUNICORN_THREADS` instead
— threads share the one process and are what keep the dashboard answering while
a sync is blocked on a slow provider.

**Provider variables in `src/config.py` are dead.** `SIMKL_*`, `TRAKT_*`,
`ANILIST_*`, `MDBLIST_*`, `PMDB_API_KEY` and `SYNC_*` are left over from the
removed command-line entry point; nothing reads them. Configure providers in
the UI.

## Running on a small VPS

The shipped `docker-compose.yml` already limits SyncMeta to one concurrent sync,
one PublicMetaDB write worker, and one Gunicorn worker with two threads. Start
there. If it still competes with your other containers:

```env
SYNCMETA_CPU_LIMIT=0.5
SYNCMETA_MEMORY_LIMIT=1024m
SYNCMETA_ANILIST_PREWARM_LIMIT=0
```

Then raise the list sync interval in the UI.

## Health check

```bash
curl http://127.0.0.1:8080/healthz
```

```json
{"ok":true,"service":"syncmeta"}
```

## Development

```bash
pip install -r requirements.txt
python web.py                      # http://127.0.0.1:8080
python -m unittest discover -v     # full suite, no exclusions
```

If `cryptography` fails to import with a `pyo3_runtime.PanicException`, the
distro package is broken; reinstall the wheel:

```bash
pip install --ignore-installed "cryptography>=42,<44"
```

## Troubleshooting

- **High CPU.** Keep one concurrent sync, raise the sync intervals, set
  `SYNCMETA_ANILIST_PREWARM_LIMIT=0`, keep write workers at 1.
- **Wrong anime matches.** Use the unresolved mapping tools on the dashboard, or
  the anime cache repair action in `/admin`.
- **Stale anime data.** `/admin` -> Update Anime Lists. Refresh is ETag-aware and
  keeps the current data if it fails.
- **Expired tokens.** Reconnect that service in Settings -> Connections.
- **PublicMetaDB write errors.** Latest Sync Results -> Details, or Sync History
  -> Details, for the row-level error.
- **Empty local Library.** Add and run a route with **Library** as its target.
  A TMDB API key is optional and only enriches titles, posters and episodes.
- **A provider list is missing.** Check that the account is connected, refresh
  the Library tab, and confirm the credential can read private/account lists.

## License

MIT. See [LICENSE](LICENSE).
