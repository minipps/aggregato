"""The API process: app factory, static assets, and the startup migration hook.

Serves the public HTTP contract under ``/api/v1`` and the built SPA at everything else. The SPA
consumes only that contract (FR-031) — which an SPA makes structural rather than disciplinary, since
it physically cannot read the database or reach a private endpoint.

This process does **not** run syncs. The scheduler is a separate process (``python -m
aggregato.worker``), so a provider that hangs or crashes cannot affect browsing (FR-025). The
import-linter contract ``aggregato.api -> aggregato.sync`` keeps this module out of the scheduler's
business.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aggregato.api.deps import register_auth, require_auth
from aggregato.api.errors import register_error_handlers
from aggregato.api.routes import auth, health
from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine
from aggregato.db.migrate import upgrade_to_head
from aggregato.logging import configure_logging

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def create_app(config: Config | None = None, *, run_migrations: bool = True) -> FastAPI:
    """Build the application.

    Args:
        config: Loaded configuration, or ``None`` to load it from the environment. Injectable so a
            test builds an app without touching the real environment.
        run_migrations: Whether to apply migrations on startup (FR-049). Tests that build their
            own schema pass ``False``.

    Returns:
        A configured ``FastAPI`` app.

    Raises:
        aggregato.config.MissingTokenError: ``api.token`` is unset. Startup fails rather than
            serving, because there is no unauthenticated mode (FR-032). This is the ONLY fatal
            configuration error — a broken *provider* leaves the service running.
    """
    settings = config or load_config()
    engine = create_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if run_migrations:
            # In a THREAD, not inline. Alembic's env.py drives its own `asyncio.run`, and lifespan
            # already runs inside a loop — calling it directly raises "asyncio.run() cannot be
            # called from a running event loop", which is how this was broken until a smoke test
            # started the real app. The thread has no loop of its own, so env.py works unchanged.
            #
            # Still before anything is served, which is what keeps the pre-migration backup honest:
            # no writer is running yet (FR-049).
            await asyncio.to_thread(upgrade_to_head, settings.database_url)
        yield
        await engine.dispose()

    app = FastAPI(
        title="Aggregato",
        version="1.0.0",
        lifespan=lifespan,
        # App-wide, not per route. No endpoint can become reachable by someone forgetting to
        # declare a dependency (FR-032).
        dependencies=[Depends(require_auth)],
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )

    register_error_handlers(app)
    register_auth(app, config=settings, engine=engine)

    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(health.router, prefix=API_PREFIX)

    _mount_frontend(app, settings)
    return app


def _mount_frontend(app: FastAPI, settings: Config) -> None:
    """Serve the built SPA, if it was built.

    Absent in a development checkout, where Vite serves the frontend on its own port and proxies
    ``/api`` here — so a missing directory is normal rather than an error.

    Client-side routes need the index page: a browser reloading ``/works/abc`` asks the server for
    a path only the SPA router knows, so anything that is not under ``/api`` and not a real file
    falls back to ``index.html``.
    """
    static_dir = settings.static_dir
    if static_dir is None or not static_dir.is_dir():
        log.info("no built frontend at %s; serving the API only", static_dir)
        return

    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = static_dir / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        candidate = static_dir / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


def __getattr__(name: str) -> object:
    """Build ``app`` on first access, for ``uvicorn aggregato.main:app``.

    Lazy (PEP 562) rather than a module-level ``app = create_app()``, because that would load
    configuration merely by importing this module — so a test importing :func:`create_app` would
    need ``AGGREGATO_TOKEN`` set, and a missing token would surface as an import error in places it
    has no business surfacing.

    Accessing ``app`` DOES load configuration and will fail if ``api.token`` is unset, which is
    correct: uvicorn then reports a startup failure rather than serving and 500-ing per request.
    """
    if name == "app":
        configure_logging()
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
