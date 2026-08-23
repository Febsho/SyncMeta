# SyncMeta

[![Deploy to Docker](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Self-hosted web app that keeps your watchlists, watch history and resume
progress in [PublicMetaDB](https://publicmetadb.com) up to date from SIMKL,
AniList, Trakt and MDBList.

You run it in Docker, open it in a browser, connect your accounts, and it syncs
in the background.

## Quick start

```bash
git clone https://github.com/Febsho/SyncMeta-for-PublicMetaDB
cd SyncMeta-for-PublicMetaDB
cp .env.example .env
docker compose up -d syncmeta
```

Open `http://127.0.0.1:8080` and:

1. Enter a password and click **Save Profile**. You get a profile UUID back —
   copy it and keep it with the password. Together they are the only way back
   into the profile.
2. In **Settings -> Connections**, paste your PublicMetaDB API key, then connect
   SIMKL, Trakt, AniList or MDBList.
3. In **Sync**, add a **pair**: pick a source service, a target service, and what
   to copy. The built-in PublicMetaDB pipeline is on the same screen below it.
4. On the **Dashboard**, click **Dry Run** to preview, then run it.

After that it runs on its own every 12 hours.

## What it does

**Sync pairs.** The main way to sync: a pair copies items between any two
services. It is one-way or two-way, chooses what happens when an item disappears
from the source (never remove, remove only what this pair added, or mirror the
source exactly), and can run on its own schedule with a minimum interval of 12
hours. Where the target can create lists — PublicMetaDB and Trakt — a pair also
chooses whether the lists it creates are public or private; a list that already
exists keeps whatever you set on the service itself.

For SIMKL → Trakt, Plan to Watch maps to Trakt's native watchlist. Watching,
Completed, On Hold and Dropped are kept in separate Trakt lists that SyncMeta
creates or reuses, rather than being flattened into Trakt Collection.

| Service | Reads | Writes |
|---|---|---|
| SIMKL | Watch statuses for shows, movies and anime; watch history; resume | Watchlist, history and collection |
| Trakt | Watchlist, collection, history, your lists, liked lists, resume | Watchlist, history, collection and personal lists |
| AniList | Lists by status | only with an access token |
| MDBList | Your lists and public lists, watchlist, collection, watch history | yes, with an API key or OAuth |
| PublicMetaDB | Watchlist, lists, collection, watch history and resume | the same |
| Library (local) | All titles, watchlist, collection, watch history and resume | the same, always |

**Watch history and resume progress.** Both are normal pair categories. Resume
can be read from SIMKL, Trakt, PublicMetaDB or Library, and written to
PublicMetaDB or Library. The pair editor only offers directions that both ends
actually support.

**MDBList OAuth.** An API key is enough to read. To sync *into* MDBList — its
watchlist, collection, watch history, or one of your static lists — create an app
at [mdblist.com/developer](https://mdblist.com/developer/), paste the client id
and secret in Connections, set the shown Redirect URL on your MDBList app, then
press Connect. MDBList marks its sync API as beta, so preview with a dry run
before trusting a real run.

**The PublicMetaDB pipeline.** The original built-in sync, still running and
unchanged: every connected service feeds PublicMetaDB, and for each one you pick
which lists or statuses to send, whether entries may be removed again, and
whether they are public or private. A pair can express the same thing, so new
setups are better served by one — but nothing about an existing pipeline
changed, and it sits below the pairs on the Sync screen.

**Anime matching.** Anime is matched across AniList, MAL, SIMKL, TMDB and IMDB
using the Fribb anime-lists data, with sequel seasons resolved back to the root
series. Anything it cannot match with confidence is listed as unresolved for you
to map by hand rather than guessed at.

**Library.** SyncMeta's own local store, and a sync target like any other
service — point every service at it once and any other pair can read from it
without touching a remote API again. It holds **one entry per series with the
seasons inside it**, the shape Trakt and TVDB use, which is what makes SIMKL's
per-season anime entries and AniList's per-cour entries land on the same row
instead of three. Filter by **movies, shows, anime and anime films** (anime is
tracked as a flag on the TMDB type, so an anime film is a film), by watchlist /
collection / watched, or search by title. Click a title to see its seasons and
exactly which episodes are watched. Posters, titles and episode names need a
free TMDB API key; without one it still works and shows ids and episode numbers.
The PublicMetaDB browser is still there on its own tab.

**Diagnostics.** Per-list results, row-level errors, failed and unresolved
samples, timings, and the last 25 detailed run records. A dry run previews
everything without writing.

**Connection health.** The Connections screen verifies every configured
provider with a read-only request, reports read/write capability and the last
check time, and offers the appropriate reconnect, edit, or retry action. The
dashboard shows whether PublicMetaDB, a readable source, and configured sync
pairs are ready before you start a run.

**Admin page.** Set `ADMIN_PASSWORD` to enable `/admin`: profile overview, queue
state, API request counters, anime cache repair and anime mapping refresh.

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
| Lists | automatic, every 12h | 6h |
| Each sync pair | manual | 12h when automatic |
| Watch history | manual | 24h |
| Resume progress | manual | 24h |

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
- **Empty Library.** Add a TMDB API key in Settings -> Connections for titles and
  posters.

## License

MIT. See [LICENSE](LICENSE).
