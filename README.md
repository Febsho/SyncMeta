# SyncMeta for PublicMetaDB

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
3. In **Sync**, choose which lists each service should send to PublicMetaDB.
4. On the **Dashboard**, click **Dry Run** to preview, then **Sync Lists**.

After that it runs on its own every 12 hours.

## What it does

**Sync into PublicMetaDB.** Every connected service feeds PublicMetaDB. For each
one you pick which lists or statuses to send, whether entries may be removed
again, and whether they are public or private in PublicMetaDB.

| Service | Reads | Writes |
|---|---|---|
| SIMKL | Watch statuses for shows, movies and anime; watch history | yes |
| Trakt | Watchlist, collection, history, your lists, liked lists, resume | yes |
| AniList | Lists by status | only with an access token |
| MDBList | Your lists and public lists | no, source only |
| PublicMetaDB | Watchlist and lists | yes |

**Watch history and resume progress.** Optional and off by default. Watch
history can come from SIMKL or Trakt; resume progress is Trakt only. History is
manual by default, resume can run automatically.

**Sync pairs.** Beyond the built-in service-to-PublicMetaDB pipelines, you can
copy a list from any service to any other. A pair is one-way or two-way, and
chooses what happens when an item disappears from the source: never remove,
remove only what this pair added, or mirror the source exactly. Every pair can
also run on its own automatic schedule, with a minimum interval of 12 hours.

For SIMKL → Trakt, Plan to Watch maps to Trakt's native watchlist. Watching,
Completed, On Hold, and Dropped are kept separate in private Trakt lists that
SyncMeta creates or reuses; they are not flattened into Trakt Collection.

**Anime matching.** Anime is matched across AniList, MAL, SIMKL, TMDB and IMDB
using the Fribb anime-lists data, with sequel seasons resolved back to the root
series. Anything it cannot match with confidence is listed as unresolved for you
to map by hand rather than guessed at.

**Library.** Browse what SyncMeta has put in PublicMetaDB, with posters and
titles if you add a free TMDB API key. Without one it still works and shows ids.

**Diagnostics.** Per-list results, row-level errors, failed and unresolved
samples, timings, and the last 25 detailed run records. A dry run previews
everything without writing.

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
