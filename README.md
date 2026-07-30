# Aggregato

Aggregato pulls your media logs — films, series, books, comics, albums, games, podcasts — out of the
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
| **Goodreads** | books, with ratings and reviews | your Library Export CSV | manual file import — Goodreads has no supported read API |

Anything else is a plugin away: see [docs/writing-a-provider.md](docs/writing-a-provider.md).

## Quickstart

Docker and Docker Compose are all you need.

```bash
git clone https://github.com/minipps/aggregato && cd aggregato
export AGGREGATO_TOKEN=$(openssl rand -hex 32)   # keep this — it is your login
docker compose -f docker/compose.yml up -d
```

Open <http://localhost:8000> and sign in with that token. One image, one volume, two processes (the
API and the scheduler). No database server, no message broker, no other services.

Then, on the **Providers** screen:

1. Pick a platform, press **Configure provider**, fill the form in and **Save configuration**. The
   form is generated from what that platform actually needs, and every field says what it is for.
   Saved credentials are never shown again.
2. Press **Check credentials**. It reads one page and reports back what it found.
3. Press **Enable**, then **Sync now** for the first pass. After that the scheduler polls on its own
   at the platform's own interval, and you can leave it alone.

For a file-based platform — Goodreads, or Letterboxd history older than its feed — enable it first,
then use **Import personal export** on the same card to upload the CSV or `.rss`. The file stays
local and is processed by the sync worker.

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
```

A `${VAR}` reference is resolved when it is read and never written back to disk; the API never
returns a secret's value. Settings that come from the file are **file-pinned** — the UI shows them but
will not edit them, because the file is the source of truth for those.

A platform whose block is broken (a bad shape, an unset `${VAR}`) is marked `misconfigured` and
disabled on its own. Startup is unaffected and so is every other platform. The one fatal
configuration error is a missing `AGGREGATO_TOKEN`: there is no unauthenticated mode.

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
open <http://localhost:5173>; `frontend/` is bind-mounted into Vite, so edits reload immediately.

```bash
docker compose -f docker/compose.yml -f docker/compose.dev.yml up
```

Migrations run automatically at startup; `uv run alembic upgrade head` if you want them separately.

### Quality gates

All four must be clean before anything merges. Warnings are errors.

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run lint-imports          # the decoupling contract, a merge gate rather than a convention
uv run pytest                # no test touches the network or needs credentials
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
| [specs/001-media-log-aggregator/spec.md](specs/001-media-log-aggregator/spec.md) | What the product does, in capability terms |
| [.../plan.md](specs/001-media-log-aggregator/plan.md) | Architecture, stack, and the declared performance budgets |
| [.../contracts/provider-plugin.md](specs/001-media-log-aggregator/contracts/provider-plugin.md) | The plugin contract — normative |
| [.../contracts/openapi.yaml](specs/001-media-log-aggregator/contracts/openapi.yaml) | The HTTP contract the UI consumes, and the only one |
| [.../quickstart.md](specs/001-media-log-aggregator/quickstart.md) | How to prove each user story works |
| [.specify/memory/constitution.md](.specify/memory/constitution.md) | The rules reviews are held to |
