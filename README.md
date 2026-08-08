# Aggregato


<img width="2524" height="1336" alt="Aggregato landing page" src="https://github.com/user-attachments/assets/4863cba9-3996-4b32-99a4-e6049eaa6d0a" />

Aggregato pulls your media logs — films, series, books, comics, albums, games — out of the
platforms you already log them on and keeps them in one local database you own, browsable through one
web UI.

It is single-user and self-hosted. It does not replace the platforms you log on, and it does not
enrich anything from third-party metadata sources: what you see is what your platforms said, unified
and handed back to you.

**A fresh install makes exactly zero outbound requests.** Nothing is fetched until you enable a
platform yourself.

## What it gives you

- **One log across every platform.** A film you rated on Letterboxd and an album you scrobbled sit in
  the same timeline, the same search, and the same statistics.
- **Your ratings kept honest.** 4 of 5 stars and 8 of 10 are stored as what each platform actually
  said, with its scale, rather than flattened into one invented number.
- **No fabricated precision.** An entry logged as "May 2019" stays a month, not a guessed day.
- **Archive-first.** Raw payloads are retained so a normalization fix can be replayed offline, and a
  platform that changes shape or goes away does not take your history with it.
- **Yours to take.** Download a full archive from Settings at any time; the database is a plain file
  in a directory you chose.

## Platforms

| Platform | What it collects | What you need | How it syncs |
|---|---|---|---|
| **Koito** | listens (tracks) | server URL + API key | its own API, every 5 minutes |
| **ListenBrainz** | listens (tracks) | username + user token | its API, every 15 minutes. Also works against a compatible server such as Maloja via `base_url` |
| **AniList** | anime + manga, with ratings | username, plus an OAuth token for private lists | its API, hourly |
| **Letterboxd** | films + series, with ratings and reviews | your username (public RSS), or an exported `.rss`/`.xml` | public feed, every 6 hours |
| **Goodreads** | books, with ratings and reviews | public profile/RSS URL; CSV export for a complete archive | [public RSS feed](docs/goodreads.md), daily; CSV import remains available |

Anything else is a plugin away: see [docs/writing-a-provider.md](docs/writing-a-provider.md).

### Candidates

None of these exist yet — this is the shortlist and what each would cost. "Surface" is the
acquisition ladder every provider declares, `api` > `feed` > `export` > `scrape`, best to worst
: a platform with an API is mostly mapping work, while one without gets the Goodreads
treatment — a file the operator downloads, imported by hand.

| Platform | What it would add | Surface |
|---|---|---|
| **MyAnimeList** | anime + manga, with scores | `api` — official v2 API, OAuth2 |
| **Kitsu** (kitsu.app) | anime + manga, with ratings | `api` — public JSON:API |
| **Last.fm** | scrobbles (tracks) + loved tracks | `api` — documented, API key. A third listens provider beside ListenBrainz and Koito |
| **AOTY** | album ratings + reviews | unconfirmed — no official public API found; likely `export` or `scrape` |
| **RateYourMusic** | album ratings + reviews | **no API** — `export` at best |
| **Hardcover** | books, with ratings and reviews | `api` — public GraphQL, token |
| **The StoryGraph** | books, with ratings and reviews | **no API** — `export`, the same shape Goodreads already uses |

RateYourMusic and The StoryGraph have no API to read, so they would need either an operator-supplied
export or a carefully fenced scraper. See [docs/provider-acquisition-roadmap.md](docs/provider-acquisition-roadmap.md)
for how a provider keeps one id while its surface improves.

What a platform offers changes. Re-check the surface before starting work rather than trusting this
table — it records what was true when it was written, not what is true today.

## Quickstart

Docker and Docker Compose are all you need.

```bash
git clone https://github.com/minipps/aggregato && cd aggregato
export AGGREGATO_TOKEN=$(openssl rand -hex 32)   # keep this — it is your login
docker compose -f docker/compose.yml up -d
```

That pulls the published backend and frontend `0.2.0` images (amd64 and arm64); pin them explicitly with
`AGGREGATO_VERSION=0.2.0`, or set `AGGREGATO_VERSION` to a newer release when upgrading. Use
`docker compose -f docker/compose.yml pull && docker compose
-f docker/compose.yml up -d`, and if you would rather build from your checkout, use `docker compose
-f docker/compose.yml build`.

Open <http://localhost:8000> and sign in with that token. The frontend container serves the SPA and
proxies `/api` to the backend container, which runs the API and scheduler. There is one named volume;
no database server, message broker, or other service is needed. Set `AGGREGATO_FRONTEND_PORT` if the
host's port 8000 is already in use.

Then, on the **Providers** screen:

1. Pick a platform, press **Configure provider**, fill the form in and **Save configuration**. The
   form is generated from what that platform actually needs, and every field says what it is for.
   Saved credentials are never shown again.
2. Press **Show latest run** to see the most recent completed provider run. This reports recorded
   state; it does not make an immediate request to the platform.
3. Press **Enable**, then **Sync now** for the first pass. After that the scheduler polls on its own
   at the platform's own interval, and you can leave it alone.

For a local archive — a Goodreads CSV, or Letterboxd history older than its feed — enable it first,
then use **Import personal export** on the same card to upload the CSV or `.rss`. The file stays
local and is processed by the sync worker. Goodreads can also sync a configured bookshelf URL.

Your log fills in as syncs land. **Log** is the timeline, **Stats** the summaries, **Sync history**
what ran and when, and **Settings** holds retention, storage usage, and the archive download.

### Configuring platforms in a file instead

Anything you can set in the UI can be set in YAML, which is handy for keeping credentials in
environment variables. Point `AGGREGATO_CONFIG` at the file:

```yaml
providers:
  koito:
    base_url: http://koito.lan:4110
    api_key: ${KOITO_API_KEY}
  listenbrainz:
    username: your-name
    token: ${LISTENBRAINZ_TOKEN}
  letterboxd:
    username: your-name
  goodreads:
    profile_url: https://www.goodreads.com/user/show/155188990-mini
```

A `${VAR}` reference is resolved when it is read and never written back to disk; the API never
returns a secret's value. Settings that come from the file are **file-pinned** — the UI shows them but
will not edit them, because the file is the source of truth for those.

A platform whose block is broken (a bad shape, an unset `${VAR}`) is marked `misconfigured` and
disabled on its own. Startup is unaffected and so is every other platform. The one fatal
configuration error is a missing `AGGREGATO_TOKEN`: there is no unauthenticated mode.

### Letting other people look

`AGGREGATO_READONLY_TOKEN` is a second, optional token that can only read. Set it to something
different from `AGGREGATO_TOKEN`, hand it out, and whoever holds it signs in on the same screen and
browses the whole archive — log, statistics, creators, providers — with every control that would
change something simply absent. The refusal is enforced by the server, not by the hidden buttons:
any write it attempts comes back `403`, whether it goes through the web UI or straight at the API.
It cannot change settings, trigger a sync, resolve identities, or upgrade itself into the real
token. Leave it unset and no read-only access exists.

### Where your data lives

Everything is under the container's `/data`, which Compose keeps on the named `aggregato-data`
volume: the SQLite database, the retained raw payloads, and the image cache. It survives restarts and
image upgrades. `docker compose down --volumes` deletes it — that is the one command to be careful
with. Past roughly a million entries, point `AGGREGATO_DATABASE_URL` at Postgres instead.

When something looks wrong — a platform reporting `structure_changed`, storage growing faster than
you expected — [docs/operations.md](docs/operations.md) is written for exactly those moments.

## Contributing

Python 3.13 and [uv](https://docs.astral.sh/uv/); Node 22+ only if you are changing the frontend.

```bash
uv sync --all-extras --dev
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data

uv run uvicorn aggregato.main:app --reload   # API process
uv run python -m aggregato.worker            # scheduler process, separate on purpose

cd frontend && npm install && npm run dev    # dev server proxies /api to :8000
```

To develop the frontend entirely in Docker, start the normal service plus the development overlay and
open <http://localhost:8000>; the overlay replaces the production frontend image with Vite and
bind-mounts `frontend/`, so edits hot-reload immediately. Backend Python edits reload the API too.

```bash
docker compose -f docker/compose.yml -f docker/compose.dev.yml up
```

Migrations run automatically at startup; `uv run alembic upgrade head` if you want them separately.

### Quality gates

All four must be clean before anything merges. Warnings are errors.

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run lint-imports          # the decoupling contract, a merge gate rather than a convention
uv run pytest -m "not bench" # merge-gate suite; run `uv run pytest -m bench tests/bench` separately
cd frontend && npm run type-check && npm run test:unit
```

### Adding a platform

Every platform is a plugin implementing one three-method contract; the core knows none of their
names. Start with [docs/writing-a-provider.md](docs/writing-a-provider.md), then read
[CONTRIBUTING.md](CONTRIBUTING.md) — the acquisition hierarchy and the scraping policy are review
gates, not suggestions.

## Documentation

| Document | What it is |
|---|---|
| [docs/operations.md](docs/operations.md) | Running it: failure states, retention, storage, Docker specifics |
| [docs/writing-a-provider.md](docs/writing-a-provider.md) | Adding a platform of your own |
| [docs/provider-acquisition-roadmap.md](docs/provider-acquisition-roadmap.md) | Why each platform reads the surface it does, and how that will change |
| [docs/accessibility.md](docs/accessibility.md) | The accessibility commitments the UI is held to |
| [AGENTS.md](AGENTS.md) | Engineering principles, architecture guardrails, and contributor workflow |
| [docs/requirements.md](docs/requirements.md) | What the product does, in capability terms |
| [docs/architecture.md](docs/architecture.md) | Architecture, stack, and declared performance budgets |
| [docs/contracts/provider-plugin.md](docs/contracts/provider-plugin.md) | The plugin contract — normative |
| [docs/contracts/openapi.yaml](docs/contracts/openapi.yaml) | The HTTP contract the UI consumes, and the only one |
| [docs/validation.md](docs/validation.md) | How to validate the product journeys |
