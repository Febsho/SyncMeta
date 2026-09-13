<div align="center">

# SyncMeta

**Self-hosted synchronization for your watch data.**

Keep watchlists, collections, custom lists, watch history and playback progress synchronized across your media services.

[![Docker](https://github.com/Febsho/SyncMeta/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Febsho/SyncMeta/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

SIMKL · Trakt · AniList · MDBList · PublicMetaDB · Local Library

</div>

---

## What is SyncMeta?

SyncMeta is a self-hosted web application that synchronizes your media data between multiple tracking services.

Connect your accounts, create sync routes and let SyncMeta keep everything aligned automatically.

It supports:

* Watchlists
* Collections
* Custom lists
* Watch history
* Episode progress
* Resume progress
* Anime mapping
* One-way and two-way synchronization
* Automatic scheduled synchronization
* Local provider-neutral Library
* Dry-run previews and deletion protection

SyncMeta runs entirely on your own server using Docker.

---

## Supported services

| Service              | Read | Write |
| -------------------- | :--: | :---: |
| **SIMKL**            |   ✓  |   ✓   |
| **Trakt**            |   ✓  |   ✓   |
| **AniList**          |   ✓  |   ✓   |
| **MDBList**          |   ✓  |   ✓   |
| **PublicMetaDB**     |   ✓  |   ✓   |
| **SyncMeta Library** |   ✓  |   ✓   |

### SIMKL

Supports:

* Watch statuses
* Movies
* Shows
* Anime
* Watch history
* Resume progress
* Watchlist
* Collection

SIMKL statuses can also be mapped cleanly into Trakt.

For example:

```text
SIMKL Plan to Watch → Trakt Watchlist
SIMKL Watching      → Trakt "Watching" list
SIMKL Completed     → Trakt "Completed" list
SIMKL On Hold       → Trakt "On Hold" list
SIMKL Dropped       → Trakt "Dropped" list
```

### Trakt

Supports:

* Watchlist
* Collection
* History
* Resume progress
* Personal lists
* Liked lists

### AniList

Supports:

* Anime status lists
* Custom lists
* Episode progress
* Activity-derived watch history

SyncMeta also handles anime identity mapping between services.

### MDBList

Supports:

* Watchlist
* Collection
* History
* Account lists
* Public lists

Both API-key and OAuth based connections are supported.

### PublicMetaDB

Supports:

* Watchlist
* Picks
* Collection
* Custom lists
* History
* Resume progress

PublicMetaDB is treated like any other provider. It does not have to be the central synchronization service.

### Local Library

SyncMeta includes its own local media Library.

It can be used as both a source and a destination:

```text
SIMKL ───────┐
Trakt ───────┤
AniList ─────┼──→ SyncMeta Library
MDBList ─────┤
PublicMetaDB ┘
```

Other routes can then read from the Library without repeatedly querying remote providers.

---

# Quick Start

## Docker Compose

Clone the repository:

```bash
git clone https://github.com/Febsho/SyncMeta.git
cd SyncMeta
```

Optional:

```bash
cp .env.example .env
```

Start SyncMeta:

```bash
docker compose up -d
```

Open:

```text
http://localhost:8080
```

The `.env` file is optional. SyncMeta can start with the defaults included in `docker-compose.yml`.

---

# First Setup

### 1. Create a profile

Open SyncMeta and enter a password.

SyncMeta creates a unique profile UUID.

Keep both:

```text
Profile UUID
+
Password
```

They are required to access the profile again.

There is no password recovery.

### 2. Connect services

Open:

```text
Settings → Connections
```

Connect the services you want to use.

The local SyncMeta Library is always available.

### 3. Create a sync route

Open:

```text
Settings → Sync Routes
```

Choose:

```text
Source
↓
Content
↓
Destination
```

Example:

```text
SIMKL
  ↓
Watchlist + History
  ↓
Trakt
```

### 4. Preview

Run **Preview** before the first real synchronization.

The dry run shows:

* Items that will be added
* Items that will be updated
* Items that will be removed
* Conflicts
* Unresolved titles
* Safety warnings

Nothing is written during a preview.

### 5. Enable automatic sync

Routes are manual by default.

Automatic synchronization can be enabled individually for each route.

---

# Sync Routes

A route describes how data should move between two services.

```text
Source ───────────→ Destination
```

or:

```text
Service A ←──────→ Service B
```

Each route has its own:

* Source
* Destination
* Content types
* Synchronization direction
* Removal policy
* Schedule
* Baseline
* Sync history

Services communicate directly.

SyncMeta does not force everything through PublicMetaDB or the local Library.

---

# Safe Synchronization

SyncMeta is designed to avoid destructive synchronization mistakes.

Instead of simply comparing two lists, every route keeps a **baseline** containing the last state that both sides agreed on.

The synchronization pipeline looks roughly like this:

```text
Read
  ↓
Normalize
  ↓
Resolve identities
  ↓
Compare against baseline
  ↓
Create sync plan
  ↓
Safety checks
  ↓
Write changes
  ↓
Update baseline
```

This lets SyncMeta distinguish between:

```text
Added on source
Removed on source
Added on destination
Removed on destination
Changed on both sides
```

rather than treating every difference as the same thing.

---

## First-run protection

A new route will not immediately start deleting content.

The first successful synchronization establishes its baseline.

Until that baseline exists:

```text
Adds     → allowed
Removals → blocked
```

This also applies when a route's baseline is reset.

---

## Failed reads cannot cause deletions

A provider returning an incomplete response must never look like:

```text
"The user deleted everything"
```

Therefore an incomplete or failed provider read cannot justify removing content.

Examples include:

* API timeout
* Expired token
* Rate limit
* Partial pagination
* Provider outage

---

## Deletion guard

Large removals are paused for confirmation.

By default, SyncMeta stops a removal when it would affect:

```text
more than 25 items

or

more than 20% of the destination
```

The preview explains why the operation was stopped.

Automatic synchronization cannot override these safety checks.

---

# Two-Way Sync

Two-way routes reconcile both services in one operation.

```text
Trakt ←──────→ SIMKL
```

SyncMeta compares both sides against their previous baseline.

If only one side changed:

```text
Changed side wins
```

If both sides changed to the same state:

```text
No action required
```

If both sides changed differently:

```text
Conflict
```

Neither side is silently overwritten.

---

# Watch History

Watch history is treated as a set of viewing events rather than a simple watched/unwatched list.

SyncMeta attempts to recognize the same playback event when it appears on multiple services.

This prevents a single watch from becoming multiple plays when it travels through several providers.

A genuine rewatch remains a separate play.

---

# Resume Progress

Resume positions can currently be read from compatible providers such as:

```text
SIMKL
Trakt
PublicMetaDB
SyncMeta Library
```

Routes only expose combinations supported by both the source and destination.

SyncMeta also avoids moving playback progress backwards.

Very small progress values are treated as accidental opens while nearly completed titles are treated as completed instead of resumable.

---

# Anime Support

Anime synchronization is one of the areas where provider data differs the most.

SyncMeta resolves identities using data from sources such as:

```text
AniList
MyAnimeList
SIMKL
TMDB
IMDb
```

with additional mappings from the Fribb anime-lists project.

It can normalize:

```text
separate cours
sequel seasons
provider-specific season entries
```

into the same underlying series.

Low-confidence matches are not guessed automatically.

They are shown as unresolved so they can be mapped manually.

---

# SyncMeta Library

The built-in Library provides a provider-neutral representation of your media data.

It supports:

* Movies
* Shows
* Anime
* Anime movies
* Watchlist
* Collection
* History
* Resume progress
* Watched episodes
* Seasons
* Search
* Filtering
* Sorting
* Pagination

Series are stored as a single title with seasons underneath it.

This helps normalize differences such as:

```text
SIMKL season entries
AniList cours
Trakt series/seasons
```

into one consistent structure.

A TMDB API key can additionally provide richer metadata such as:

* Titles
* Posters
* Season information
* Episode names

The Library still works without TMDB enrichment.

---

# Dashboard

The dashboard provides an overview of your synchronization setup.

It displays:

* Saved routes
* Provider status
* Recent activity
* Sync progress
* Warnings
* Errors
* Route relationships

Routes are visually grouped by destination so more complex synchronization setups remain understandable.

---

# Diagnostics

Every synchronization records detailed information about what happened.

Diagnostics include:

* Added items
* Updated items
* Removed items
* Failed items
* Unresolved titles
* Provider errors
* Timings
* Warnings
* Partial runs

Detailed information for recent synchronization runs is available directly from the web interface.

---

# Connection Health

SyncMeta periodically verifies connected providers using read-only API requests.

The Connections page shows:

```text
Connection state
Read capability
Write capability
Last health check
Reconnect actions
```

Routes can therefore show whether their required providers are ready before synchronization begins.

---

# Profiles & Security

Every SyncMeta profile has its own:

* Connected accounts
* Credentials
* Routes
* Library
* History
* Schedules
* Settings

Provider credentials are encrypted at rest.

They are not returned to the browser after being stored.

A profile is accessed using:

```text
UUID + password
```

Changing the password invalidates existing sessions.

---

## Optional site password

A shared password can be placed in front of the complete application:

```env
SITE_ACCESS_PASSWORD=your-password
```

---

## Admin interface

Set:

```env
ADMIN_PASSWORD=your-password
```

to enable:

```text
/admin
```

The admin interface provides access to:

* Profile overview
* Queue state
* API counters
* Runtime configuration
* Anime cache maintenance
* Anime mapping refresh

Most server settings can be changed there without editing `.env`.

---

# Configuration

Copy the example configuration if you want to override the defaults:

```bash
cp .env.example .env
```

Most provider credentials and synchronization settings should be configured through the web interface.

The `.env` file is primarily for server-level configuration.

---

## Important variables

| Variable                        | Purpose                                   |
| ------------------------------- | ----------------------------------------- |
| `ADMIN_PASSWORD`                | Enables the admin interface               |
| `SITE_ACCESS_PASSWORD`          | Password protecting the whole application |
| `SYNCMETA_MASTER_KEY`           | Encryption key for stored credentials     |
| `PROFILE_STORE_FILE`            | Profile database location                 |
| `SYNCMETA_MAX_CONCURRENT_SYNCS` | Maximum simultaneous synchronization jobs |
| `SYNCMETA_CPU_LIMIT`            | Docker CPU limit                          |
| `SYNCMETA_MEMORY_LIMIT`         | Docker memory limit                       |
| `SYNCMETA_GUNICORN_THREADS`     | HTTP worker threads                       |
| `DISABLE_PROFILE_SCHEDULER`     | Disable automatic synchronization         |

See [.env.example](.env.example) for all supported options.

---

## Persistent storage

Docker mounts:

```text
./data → /app/data
```

Important persistent data includes:

```text
profiles
credentials
local library
sync baselines
settings
anime caches
```

Do not delete the data directory unless you intentionally want to reset SyncMeta.

---

# Important Docker Note

Keep:

```env
SYNCMETA_GUNICORN_WORKERS=1
```

The scheduler and synchronization runner live inside the application process.

Running multiple Gunicorn processes would therefore create multiple schedulers capable of running the same synchronization jobs.

Use additional threads instead:

```env
SYNCMETA_GUNICORN_THREADS=6
```

---

# Small VPS Configuration

SyncMeta is designed to run alongside other containers.

The included Docker Compose configuration already uses conservative defaults.

For smaller servers you can reduce its limits further:

```env
SYNCMETA_CPU_LIMIT=0.5
SYNCMETA_MEMORY_LIMIT=1024m
SYNCMETA_MAX_CONCURRENT_SYNCS=1
SYNCMETA_ANILIST_PREWARM_LIMIT=0
```

Longer automatic synchronization intervals can also substantially reduce resource usage.

---

# Health Check

Check whether SyncMeta is running:

```bash
curl http://127.0.0.1:8080/healthz
```

Expected response:

```json
{
  "ok": true,
  "service": "syncmeta"
}
```

Docker also includes a built-in container health check.

---

# Updating

Pull the newest container:

```bash
docker compose pull
```

Restart SyncMeta:

```bash
docker compose up -d
```

The persistent `data` directory remains untouched.

---

# Development

Clone the repository:

```bash
git clone https://github.com/Febsho/SyncMeta.git
cd SyncMeta
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the development server:

```bash
python web.py
```

Open:

```text
http://127.0.0.1:8080
```

Run the test suite:

```bash
python -m unittest discover -v
```

---

# Troubleshooting

### Provider connection stopped working

Reconnect the provider under:

```text
Settings → Connections
```

Expired OAuth tokens are a common cause.

### Anime was matched incorrectly

Use the unresolved mapping tools or the repair tools available from the admin interface.

### Provider list is missing

Verify that:

* The provider is connected
* The credential can access private lists
* The Library/provider view has been refreshed

### Local Library is empty

Create a route where:

```text
Destination → Library
```

and run it once.

### High CPU usage

Reduce concurrency and background work:

```env
SYNCMETA_MAX_CONCURRENT_SYNCS=1
SYNCMETA_ANILIST_PREWARM_LIMIT=0
```

and increase automatic synchronization intervals.

### Sync failed

Open the latest synchronization result and inspect its details.

SyncMeta stores row-level errors and provider failures to make failed items identifiable without guessing.

---

# Project Philosophy

SyncMeta is built around three principles:

### Provider neutral

No provider is required to be the central source of truth.

```text
SIMKL → Trakt

Trakt → MDBList

AniList → Library

Library → PublicMetaDB
```

are all valid setups.

### Safe by default

Synchronization should never turn an API outage into a mass deletion.

Baseline tracking, previews and deletion guards are built directly into the sync engine.

### Observable

SyncMeta should show what it plans to change, what it actually changed and why.

The same planner used for previews performs the real synchronization.

---

# License

SyncMeta is released under the [MIT License](LICENSE).

---

<div align="center">

**Sync your watch data without locking it to a single service.**

[Source](https://github.com/Febsho/SyncMeta) · [Issues](https://github.com/Febsho/SyncMeta/issues)

</div>
