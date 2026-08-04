"""No endpoint is reachable without credentials (, , research.md ).

The app assembled here is the one ``main.py`` builds: the app-level ``require_auth`` dependency,
the real error handlers, the real ``POST /auth/session``, and two throwaway routes standing in for
every other endpoint. Driven in-process through ``httpx.ASGITransport``, over a temp-file SQLite
database, so the autouse socket blocker in ``tests/conftest.py`` is satisfied.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.api.deps import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthContext,
    register_auth,
    require_auth,
)
from aggregato.api.errors import PROBLEM_MEDIA_TYPE, register_error_handlers
from aggregato.api.routes import auth
from aggregato.config import Config, load_config
from aggregato.db.engine import create_engine, transaction
from aggregato.db.schema import metadata, sessions

TOKEN = "correct-horse-battery-staple"
AUTH_DEP = Depends(require_auth)  # module-level: ruff B008 forbids the call in a default


def _config(token: str, data_dir: Path) -> Config:
    return load_config(env={"AGGREGATO_TOKEN": token, "AGGREGATO_DATA": str(data_dir)})


@pytest.fixture
async def engine(data_dir: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(f"sqlite+aiosqlite:///{data_dir}/aggregato.db")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def app(engine: AsyncEngine, data_dir: Path) -> FastAPI:
    """The real assembly: auth enforced app-wide, so a route cannot opt out."""
    application = FastAPI(dependencies=[Depends(require_auth)])
    register_error_handlers(application)
    register_auth(application, config=_config(TOKEN, data_dir), engine=engine)
    application.include_router(auth.router)

    @application.get("/probe")
    async def probe(ctx: AuthContext = AUTH_DEP) -> dict[str, str]:
        return {"via": ctx.via}

    @application.post("/probe")
    async def write_probe() -> dict[str, bool]:
        return {"written": True}

    @application.get("/media/image/{hash}")
    async def image_probe(hash: str) -> dict[str, str]:
        return {"hash": hash}

    @application.post("/media/image/{hash}")
    async def image_write_probe(hash: str) -> dict[str, bool]:
        return {"written": True}

    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def _assert_problem(response: httpx.Response, status: int) -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = response.json()
    assert body["status"] == status
    assert body["title"]


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/probe"), ("POST", "/probe"), ("POST", "/auth/session")],
)
async def test_no_endpoint_is_reachable_unauthenticated(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    """: there is no unauthenticated mode, and no 401 escapes as FastAPI's default shape."""
    _assert_problem(await client.request(method, path), 401)


async def test_cached_image_reads_without_credentials(client: httpx.AsyncClient) -> None:
    """Covers are embeddable in an <img>, which carries no bearer header ."""
    response = await client.get(f"/media/image/{'a' * 64}")
    assert response.status_code == 200


async def test_the_image_exemption_is_reads_only(client: httpx.AsyncClient) -> None:
    _assert_problem(await client.post(f"/media/image/{'a' * 64}"), 401)


async def test_valid_bearer_authenticates(client: httpx.AsyncClient) -> None:
    response = await client.get("/probe", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"via": "bearer"}


async def test_wrong_bearer_is_rejected(client: httpx.AsyncClient) -> None:
    _assert_problem(await client.get("/probe", headers={"Authorization": "Bearer nope"}), 401)


async def test_bearer_differing_only_in_length_is_rejected(client: httpx.AsyncClient) -> None:
    """A prefix of the real token must not pass — the comparison is constant-time, not a prefix."""
    for candidate in (TOKEN[:-1], TOKEN + "x"):
        _assert_problem(
            await client.get("/probe", headers={"Authorization": f"Bearer {candidate}"}), 401
        )


async def test_session_exchange_sets_httponly_cookie(client: httpx.AsyncClient) -> None:
    response = await client.post("/auth/session", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 204
    session_cookie = next(
        value for key, value in response.headers.multi_items() if key.lower() == "set-cookie"
    )
    assert session_cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    # Plain HTTP is a supported LAN deployment, so Secure must not be forced on .
    assert "Secure" not in session_cookie
    assert client.cookies[CSRF_COOKIE]


async def test_cookie_authenticates_a_get(client: httpx.AsyncClient) -> None:
    await client.post("/auth/session", headers={"Authorization": f"Bearer {TOKEN}"})
    response = await client.get("/probe")
    assert response.status_code == 200
    assert response.json() == {"via": "cookie"}


async def test_cookie_write_without_csrf_is_rejected(client: httpx.AsyncClient) -> None:
    await client.post("/auth/session", headers={"Authorization": f"Bearer {TOKEN}"})
    _assert_problem(await client.post("/probe"), 403)


async def test_cookie_write_with_csrf_succeeds(client: httpx.AsyncClient) -> None:
    await client.post("/auth/session", headers={"Authorization": f"Bearer {TOKEN}"})
    response = await client.post("/probe", headers={CSRF_HEADER: client.cookies[CSRF_COOKIE]})
    assert response.status_code == 200


async def test_cookie_write_with_wrong_csrf_is_rejected(client: httpx.AsyncClient) -> None:
    await client.post("/auth/session", headers={"Authorization": f"Bearer {TOKEN}"})
    _assert_problem(await client.post("/probe", headers={CSRF_HEADER: "forged"}), 403)


async def test_bearer_write_needs_no_csrf(client: httpx.AsyncClient) -> None:
    """A browser never attaches a bearer token by itself, so there is nothing to forge ."""
    response = await client.post("/probe", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


async def test_expired_session_is_rejected(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    session_id = "expired-session-id"
    past = datetime.now(UTC) - timedelta(days=1)
    async with transaction(engine) as conn:
        await conn.execute(
            sessions.insert().values(
                id=session_id,
                created_at=past - timedelta(days=14),
                expires_at=past,
                token_fingerprint=sha256(TOKEN.encode()).hexdigest(),
            )
        )
    signature = hmac.new(TOKEN.encode(), session_id.encode(), sha256).hexdigest()
    client.cookies.set(SESSION_COOKIE, f"{session_id}.{signature}")
    _assert_problem(await client.get("/probe"), 401)


async def test_rotating_the_token_invalidates_an_existing_session(
    client: httpx.AsyncClient, app: FastAPI, data_dir: Path
) -> None:
    """The whole reason sessions live in the database and carry a token fingerprint ."""
    await client.post("/auth/session", headers={"Authorization": f"Bearer {TOKEN}"})
    assert (await client.get("/probe")).status_code == 200

    rotated = "a-freshly-rotated-token"
    app.state.config = _config(rotated, data_dir)

    _assert_problem(await client.get("/probe"), 401)
    _assert_problem(await client.get("/probe", headers={"Authorization": f"Bearer {TOKEN}"}), 401)

    # Re-sign the surviving session id under the *new* token, so the signature check passes and the
    # only thing left to reject it is the stored token_fingerprint.
    session_id = client.cookies[SESSION_COOKIE].rpartition(".")[0]
    signature = hmac.new(rotated.encode(), session_id.encode(), sha256).hexdigest()
    client.cookies.set(SESSION_COOKIE, f"{session_id}.{signature}")
    _assert_problem(await client.get("/probe"), 401)


# --- The optional read-only token ------------------------------------------------------------

READONLY = "read-only-horse-battery-staple"


@pytest.fixture
async def readonly_app(engine: AsyncEngine, app: FastAPI, data_dir: Path) -> FastAPI:
    """The same app, with ``api.readonly_token`` configured."""
    register_auth(
        app,
        config=load_config(
            env={
                "AGGREGATO_TOKEN": TOKEN,
                "AGGREGATO_READONLY_TOKEN": READONLY,
                "AGGREGATO_DATA": str(data_dir),
            }
        ),
        engine=engine,
    )
    return app


@pytest.fixture
async def readonly_client(readonly_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=readonly_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_readonly_token_reads(readonly_client: httpx.AsyncClient) -> None:
    response = await readonly_client.get("/probe", headers={"Authorization": f"Bearer {READONLY}"})
    assert response.status_code == 200
    assert response.json() == {"via": "bearer"}


async def test_readonly_token_cannot_write(readonly_client: httpx.AsyncClient) -> None:
    _assert_problem(
        await readonly_client.post("/probe", headers={"Authorization": f"Bearer {READONLY}"}), 403
    )


async def test_readonly_token_reports_itself(readonly_client: httpx.AsyncClient) -> None:
    """What the SPA reads to render itself read-only."""
    response = await readonly_client.get(
        "/auth/session", headers={"Authorization": f"Bearer {READONLY}"}
    )
    assert response.json() == {"via": "bearer", "readonly": True}


async def test_full_token_is_not_readonly(client: httpx.AsyncClient) -> None:
    response = await client.get("/auth/session", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.json() == {"via": "bearer", "readonly": False}


async def test_readonly_session_cookie_reads_but_cannot_write(
    readonly_client: httpx.AsyncClient,
) -> None:
    """The SPA path: the read-only token buys a cookie, and the cookie is read-only too (
    forbids keeping the token in page source, so the exchange has to be allowed)."""
    exchange = await readonly_client.post(
        "/auth/session", headers={"Authorization": f"Bearer {READONLY}"}
    )
    assert exchange.status_code == 204
    assert readonly_client.cookies[SESSION_COOKIE]

    session = await readonly_client.get("/auth/session")
    assert session.json() == {"via": "cookie", "readonly": True}
    # A valid CSRF token is attached by the client fixture's cookie jar, so the 403 is the read-only
    # refusal and not the CSRF one.
    _assert_problem(
        await readonly_client.post(
            "/probe", headers={CSRF_HEADER: readonly_client.cookies[CSRF_COOKIE]}
        ),
        403,
    )


async def test_full_session_cookie_still_writes(readonly_client: httpx.AsyncClient) -> None:
    """Configuring a read-only token must not restrict the real one's session."""
    await readonly_client.post("/auth/session", headers={"Authorization": f"Bearer {TOKEN}"})
    session = await readonly_client.get("/auth/session")
    assert session.json() == {"via": "cookie", "readonly": False}
    response = await readonly_client.post(
        "/probe", headers={CSRF_HEADER: readonly_client.cookies[CSRF_COOKIE]}
    )
    assert response.status_code == 200


async def test_readonly_token_is_not_accepted_when_unconfigured(
    client: httpx.AsyncClient,
) -> None:
    """The default config has no read-only token, so that value is just a wrong token."""
    _assert_problem(
        await client.get("/probe", headers={"Authorization": f"Bearer {READONLY}"}), 401
    )
