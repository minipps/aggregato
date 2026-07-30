# Aggregato

A single-user, self-hosted service that pulls your media logs — films, series, books, comics,
albums, games, podcasts — out of the platforms you already log them on and keeps them in one local
database you own, browsable through one web UI.

It does not replace the platforms you log on, and it does not enrich anything from third-party
metadata sources. It aggregates, unifies, and hands the result back to you.

**A fresh install makes exactly zero outbound requests.** Nothing is fetched until you enable a
platform yourself.

## Run it

```bash
export AGGREGATO_TOKEN=$(openssl rand -hex 32)
docker compose -f docker/compose.yml up
```

Then open <http://localhost:8000> and sign in with that token. One image, one volume, two processes
(the API and the scheduler). No database server, no broker, no other services.

`AGGREGATO_TOKEN` is the only fatal configuration error — there is no unauthenticated mode. A
missing or invalid *platform* configuration never blocks startup: that platform is marked
`misconfigured` and everything else keeps running.

## Develop it

Python 3.13 and [uv](https://docs.astral.sh/uv/); Node 22+ only if you are changing the frontend.

```bash
uv sync --all-extras --dev
export AGGREGATO_TOKEN=dev-token AGGREGATO_DATA=./data

uv run uvicorn aggregato.main:app --reload   # API process
uv run python -m aggregato.worker            # scheduler process, separate on purpose

cd frontend && npm install && npm run dev    # dev server proxies /api to :8000
```

To develop the frontend entirely in Docker, start the normal service plus the development overlay:

```bash
docker compose -f docker/compose.yml -f docker/compose.dev.yml up
```

Open <http://localhost:5173>. The `frontend/` directory is bind-mounted into Vite, so source edits
reload immediately; API calls proxy to the `aggregato` Compose service. The production compose file
is unchanged and continues to serve the built frontend at port 8000.

Migrations run automatically at startup; `uv run alembic upgrade head` if you want them separately.

### Quality gates

All four must be clean before anything merges. Warnings are errors.

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run lint-imports          # the decoupling contract, a merge gate rather than a convention
uv run pytest                # no test touches the network or needs credentials
cd frontend && npm run type-check && npm run test:unit
```

## Adding a platform

Every platform is a plugin implementing one three-method contract; the core knows none of their
names. Start with [docs/writing-a-provider.md](docs/writing-a-provider.md), then read
[CONTRIBUTING.md](CONTRIBUTING.md) — the acquisition hierarchy and the scraping policy are
review gates, not suggestions.

## Documentation

| Document | What it is |
|---|---|
| [specs/001-media-log-aggregator/spec.md](specs/001-media-log-aggregator/spec.md) | What the product does, in capability terms |
| [.../plan.md](specs/001-media-log-aggregator/plan.md) | Architecture, stack, and the declared performance budgets |
| [.../contracts/provider-plugin.md](specs/001-media-log-aggregator/contracts/provider-plugin.md) | The plugin contract — normative |
| [.../contracts/openapi.yaml](specs/001-media-log-aggregator/contracts/openapi.yaml) | The HTTP contract the UI consumes, and the only one |
| [.../quickstart.md](specs/001-media-log-aggregator/quickstart.md) | How to prove each user story works |
| [docs/provider-acquisition-roadmap.md](docs/provider-acquisition-roadmap.md) | How file imports evolve into supported automatic provider syncs |
| [.specify/memory/constitution.md](.specify/memory/constitution.md) | The rules reviews are held to |
