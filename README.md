# Aggregato


<img width="2524" height="1336" alt="Aggregato landing page" src="https://github.com/user-attachments/assets/4863cba9-3996-4b32-99a4-e6049eaa6d0a" />

Aggregato pulls your media logs — films, series, books, comics, albums, games — out of the
platforms you already log them on and keeps them in one local database you own, browsable through one
web UI.

It is single-user and self-hosted. It does not replace the platforms you log on, and it does not
enrich anything from third-party metadata sources: what you see is what your platforms said, unified
and handed back to you.

**A fresh install makes exactly zero outbound requests.** Provider requests start after you enable
a platform or explicitly request a credential check or import.

## What it gives you

- **One log across every platform.** A film you rated on Letterboxd and an album you scrobbled sit in
  the same timeline, the same search, and the same statistics.
- **Your ratings kept honest.** 4 of 5 stars and 8 of 10 are stored as what each platform actually
  said, with its scale, rather than flattened into one invented number.
- **No fabricated precision.** An entry logged as "May 2019" stays a month, not a guessed day.
- **Archive-first.** Retained raw payloads let normalization fixes replay offline. A failed sync
  preserves stored valid facts; reported deletions and opt-in inferred deletions are tombstoned and
  hidden by default.
- **Local storage.** SQLite is the default database, kept in the data directory or Compose volume
  you configure. PostgreSQL is also supported.

## Platforms

| Platform | What it collects | What you need | How it syncs |
|---|---|---|---|
| **Koito** | listens (tracks) | server URL + API key | its own API, every 5 minutes |
| **ListenBrainz** | listens (tracks) | username + user token | its API, every 15 minutes. Also works against a compatible server such as Maloja via `base_url` |
| **AniList** | anime + manga, with ratings | username, plus an OAuth token for private lists | its API, hourly |
| **Letterboxd** | films + series, with ratings and reviews | your username or public RSS URL | public feed, every 6 hours |
| **Spotify** | recently played (tracks) | app client ID + secret, plus a refresh token with `user-read-recently-played` ([setup](docs/provider-acquisition-roadmap.md#spotify)) | its Web API, every 15 minutes |
| **Goodreads** | books, ratings, and reviews in its rolling feed | public profile/RSS URL | [public RSS feed](docs/goodreads.md), daily |

Anything else is a plugin away: see [docs/writing-a-provider.md](docs/writing-a-provider.md).

## Quickstart

Docker and Docker Compose are all you need.

```bash
git clone https://github.com/minipps/aggregato && cd aggregato
export AGGREGATO_TOKEN=$(openssl rand -hex 32)   # keep this — it is your login
docker compose -f docker/compose.yml up -d
```

That pulls the published backend and frontend `0.2.5` images (amd64 and arm64); pin them explicitly
with `AGGREGATO_VERSION=0.2.5`, or set `AGGREGATO_VERSION` to a newer release when upgrading. To
upgrade, run:

```bash
docker compose -f docker/compose.yml pull
docker compose -f docker/compose.yml up -d
```

To build from your checkout, run `docker compose -f docker/compose.yml build`.

Open <http://localhost:8000> and sign in with that token. The frontend container serves the SPA and
proxies `/api` to the backend container, which runs the API and scheduler. There is one named volume;
no database server, message broker, or other service is needed. Set `AGGREGATO_FRONTEND_PORT` if the
host's port 8000 is already in use.

Then, on the **Providers** screen:

1. Pick a platform, press **Configure provider**, fill the form in and **Save configuration**. The
   form is generated from what that platform actually needs, and every field says what it is for.
   Saved credentials are never shown again.
2. Press **Check provider** to check the saved credentials. This does not start a history sync.
3. Press **Enable**, then **Sync now** for the first pass. After that the scheduler polls on its own
   at the platform's own interval, and you can leave it alone.

Generic providers may expose **Import personal export** when their local export is a distinct,
host-supported acquisition surface. The upload stays local and is processed by the sync worker;
Goodreads and Letterboxd use their public RSS feeds as their sole bundled acquisition paths.

Your log fills in as syncs land. **Log** is the timeline, **Stats** the summaries, **Sync history**
what ran and when, and **Settings** holds retention and storage usage.

### Configuring platforms in a file instead

Provider settings can also live in YAML, which is handy for keeping credentials in environment
variables. Point `AGGREGATO_CONFIG` at the file:

```yaml
providers:
  koito:
    base_url: http://koito.lan:4110
    api_key: ${KOITO_API_KEY}
  listenbrainz:
    username: your-name
    token: ${LISTENBRAINZ_TOKEN}
  spotify:
    client_id: your-app-client-id
    client_secret: ${SPOTIFY_CLIENT_SECRET}
    refresh_token: ${SPOTIFY_REFRESH_TOKEN}
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
configuration error is a missing `AGGREGATO_TOKEN`, which remains required for protected access.

### Letting other people look

`AGGREGATO_READONLY_TOKEN` is an optional second token. Set it to a value different from
`AGGREGATO_TOKEN` to allow read-only access. The server rejects writes from this token with `403`,
including requests sent directly to the API. It cannot change settings, trigger syncs, or resolve
identities. Leave it unset to disable token-based read-only access.

For public read access, set `AGGREGATO_ALLOW_UNAUTHENTICATED_READONLY=true`. Public GET and HEAD
routes then work without credentials; writes, WebSocket connections, exports, and raw diagnostics
still require operator authentication. `AGGREGATO_TOKEN` remains required by the server.

### Where your data lives

With the default SQLite setup, the database, retained payloads, and image cache live under
`AGGREGATO_DATA`. Compose mounts that directory at `/data` on the named `aggregato-data` volume, so
it survives restarts and image upgrades. `docker compose down --volumes` deletes the volume.
PostgreSQL is also supported through `AGGREGATO_DATABASE_URL`; its database lives outside that
volume.

For on-disk SQLite, **Settings → Download archive** creates a portable ZIP. It is unavailable for
PostgreSQL, which uses database-native backup tools. The ZIP contains personal history and retained
provider data, so keep it private. See the [operations guide](docs/operations.md#portable-archive-and-restore)
for its contents and restore steps.

When something looks wrong — a platform reporting `structure_changed`, storage growing faster than
you expected — [docs/operations.md](docs/operations.md) is written for exactly those moments.

## Contributing

Use Python 3.13 and [uv](https://docs.astral.sh/uv/); install Node 22 only when changing the
frontend. [CONTRIBUTING.md](CONTRIBUTING.md) covers provider policy and releases. The development
commands and validation checks are in [docs/validation.md](docs/validation.md).

```bash
uv sync --all-extras --dev
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data
```

The API and scheduler run as separate processes. To develop the frontend in Docker, start the normal
service with the development overlay; Vite and the API reload local changes.

```bash
docker compose -f docker/compose.yml -f docker/compose.dev.yml up
```

Migrations run automatically at startup. Run them separately with `uv run alembic upgrade head`.

### Adding a platform

Every platform is a plugin implementing one three-method contract; the core knows none of their
names. Start with [docs/writing-a-provider.md](docs/writing-a-provider.md), then read
[CONTRIBUTING.md](CONTRIBUTING.md) — the acquisition hierarchy and the scraping policy are review
gates, not suggestions.

## Documentation

Aggregato is licensed under [AGPLv3](LICENSE). Report vulnerabilities privately using the
contact in [SECURITY.md](SECURITY.md).

| Document | What it is |
|---|---|
| [docs/operations.md](docs/operations.md) | Running it: failure states, retention, storage, Docker specifics |
| [docs/writing-a-provider.md](docs/writing-a-provider.md) | Adding a platform of your own |
| [docs/provider-acquisition-roadmap.md](docs/provider-acquisition-roadmap.md) | Why each platform reads the surface it does, and how that will change |
| [docs/roadmap.md](docs/roadmap.md) | Pending release checks and development work |
| [docs/accessibility.md](docs/accessibility.md) | UI accessibility conventions and manual checks |
| [AGENTS.md](AGENTS.md) | Engineering principles, architecture guardrails, and contributor workflow |
| [docs/architecture.md](docs/architecture.md) | Architecture and stack |
| [docs/data-model.md](docs/data-model.md) | Tables, constraints, state transitions, and migrations |
| [docs/contracts/provider-plugin.md](docs/contracts/provider-plugin.md) | The plugin contract — normative |
| [docs/contracts/openapi.yaml](docs/contracts/openapi.yaml) | The HTTP contract the UI consumes, and the only one |
| [docs/validation.md](docs/validation.md) | Local development and validation commands |
