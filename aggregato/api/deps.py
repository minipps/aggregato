"""Authentication for every request (T041, research.md R12, FR-032).

There is no unauthenticated mode. A request carries either ``Authorization: Bearer <api.token>``
or the ``aggregato_session`` cookie; anything else is a 401 problem detail. FR-032 also forbids the
token appearing in a URL or in page source, which is why the browser gets a cookie instead of the
token itself.

Design decisions worth stating once:

* **Constant-time token comparison.** ``hmac.compare_digest`` on bytes. A ``==`` on a secret leaks
  length and prefix through timing, and this is the only credential in the system (FR-006).
* **Sessions live in the database** (``sessions``), not in a self-contained signed cookie, so that
  rotating ``api.token`` can actually reach them: every row stores the ``token_fingerprint`` it was
  issued under, and a lookup requires it to equal the fingerprint of the *current* token. Rotation
  therefore invalidates every session, as R12 requires. Expiry is checked in SQL for the same
  reason it is stored in SQL — and because SQLite hands back naive datetimes.
* **CSRF: double-submit, with the token derived by HMAC rather than stored.**
  ``POST /auth/session`` sets a second, JS-readable cookie holding
  ``HMAC(api.token, "csrf:" + session_id)``; a cookie-authenticated write must echo it in the
  ``X-CSRF-Token`` header. The server *recomputes* the expected value from the session id in the
  HttpOnly cookie, so the readable cookie is only transport — an attacker who can set cookies still
  cannot forge a header that matches, and there is no extra table, no extra secret, and no state to
  expire. Bearer requests are exempt: a browser never attaches a bearer token by itself, so there
  is no cross-site request to forge.
* **The optional read-only token is bearer-only and method-gated.** ``api.readonly_token``, when
  set, authenticates reads and refuses every ``POST``/``PUT``/``PATCH``/``DELETE`` with a 403 — so
  it cannot change settings, trigger a sync, or exchange itself for a session cookie. Every
  mutating endpoint is an unsafe method, so the gate is the method rather than a per-route list.
* **``Secure`` only over TLS.** Setting it unconditionally would break a plain-HTTP LAN install,
  which is a supported deployment.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Final, Literal

from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from aggregato.api.errors import ProblemError, error_type
from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import sessions

__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "SESSION_TTL",
    "AuthContext",
    "issue_session",
    "register_auth",
    "require_auth",
    "require_bearer",
    "token_fingerprint",
]

SESSION_COOKIE: Final = "aggregato_session"
"""Cookie name fixed by ``contracts/openapi.yaml``'s ``sessionCookie`` scheme."""

CSRF_COOKIE: Final = "aggregato_csrf"
"""JS-readable half of the double-submit pair. Never trusted — see the module docstring."""

CSRF_HEADER: Final = "X-CSRF-Token"

SESSION_TTL: Final = timedelta(days=14)
"""How long a cookie session lasts. One user, one token: there is no refresh flow (FR-006)."""

_UNAUTHORIZED: Final = 401
_FORBIDDEN: Final = 403

# A bearer token is not attached automatically by a browser, so these need no CSRF token when
# presented with one; only cookie authentication does (R12).
_UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class AuthContext:
    """How the current request authenticated.

    Attributes:
        via: ``"bearer"`` or ``"cookie"``.
        session_id: The session row's id for cookie auth, ``None`` for bearer.
        readonly: The request presented ``api.readonly_token``, so it may only read.
    """

    via: Literal["bearer", "cookie"]
    session_id: str | None = None
    readonly: bool = False


def token_fingerprint(token: str) -> str:
    """Fingerprint of the API token, as stored in ``sessions.token_fingerprint``.

    Inputs: the raw token. Returns its SHA-256 hex digest — 64 chars, matching the column width.
    A hash rather than the token so a database dump does not hand over the credential.
    """
    return sha256(token.encode()).hexdigest()


def register_auth(app: FastAPI, *, config: Config, engine: AsyncEngine) -> None:
    """Attach the config and engine that :func:`require_auth` and the routes read.

    ``main.py`` must additionally construct the app as
    ``FastAPI(dependencies=[Depends(require_auth)])`` so no route can forget the check (FR-032).

    Args:
        app: The application to mutate.
        config: Resolved configuration; ``config.api.token`` is the only credential.
        engine: The async engine the ``sessions`` table is read and written through.
    """
    app.state.config = config
    app.state.engine = engine


async def require_auth(request: Request) -> AuthContext:
    """Authenticate any request: bearer token or session cookie, plus CSRF on cookie writes.

    Inputs: the request. Declared app-wide as a dependency, and returned so a route can tell how it
    was authenticated.

    Failure modes: :class:`~aggregato.api.errors.ProblemError` 401 when no credential is present,
    the bearer token does not match, or the session is unknown, expired, or was issued under a
    rotated token; 403 when a cookie-authenticated state-changing request has no valid CSRF token.
    Never FastAPI's default ``{"detail": ...}`` 401 shape.
    """
    token = _token(request)
    presented = _bearer(request)
    if presented is not None:
        if _matches(presented, token):
            return AuthContext(via="bearer")
        readonly = _readonly_token(request)
        if readonly is not None and _matches(presented, readonly):
            if request.method in _UNSAFE_METHODS:
                raise _readonly_forbidden(request.method)
            return AuthContext(via="bearer", readonly=True)
        raise _unauthorized("The bearer token is not valid.")

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie is None:
        raise _unauthorized(
            "This request needs an Authorization: Bearer header or a session cookie."
        )
    session_id = await _valid_session_id(request, cookie, token)
    if request.method in _UNSAFE_METHODS:
        _check_csrf(request, session_id, token)
    return AuthContext(via="cookie", session_id=session_id)


async def require_bearer(request: Request) -> None:
    """Require the bearer token specifically — the credential ``POST /auth/session`` exchanges.

    Failure modes: 401 if the header is absent or the token does not match. A session cookie is
    deliberately not accepted here: the endpoint's job is to turn the token into a cookie.
    """
    presented = _bearer(request)
    if presented is None or not _matches(presented, _token(request)):
        raise _unauthorized("POST /auth/session requires the API token as a bearer credential.")


async def issue_session(request: Request) -> tuple[str, str]:
    """Create a session row and return ``(cookie_value, csrf_token)``.

    Inputs: the request, for the app's engine and token. The session id is 256 bits from
    :mod:`secrets`; the cookie value is ``<id>.<HMAC(api.token, id)>``, so a cookie that was not
    issued by this server is rejected before any database round trip, and rotating the token
    invalidates the signature as well as the row.

    Failure modes: database errors propagate as a 500 problem detail.
    """
    token = _token(request)
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    async with transaction(_engine(request)) as conn:
        # Housekeeping on the one write path this table has, so expired rows cannot accumulate
        # forever without a separate sweeper job.
        await conn.execute(delete(sessions).where(sessions.c.expires_at <= now))
        await conn.execute(
            sessions.insert().values(
                id=session_id,
                created_at=now,
                expires_at=now + SESSION_TTL,
                token_fingerprint=token_fingerprint(token),
            )
        )
    return f"{session_id}.{_sign(token, session_id)}", _csrf_token(token, session_id)


def _token(request: Request) -> str:
    config: Config = request.app.state.config
    return config.api.token.get_secret_value()


def _readonly_token(request: Request) -> str | None:
    config: Config = request.app.state.config
    secret = config.api.readonly_token
    return None if secret is None else secret.get_secret_value()


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


def _bearer(request: Request) -> str | None:
    """The token from an ``Authorization: Bearer`` header, or ``None`` if there is no such."""
    header = request.headers.get("Authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip()


def _matches(presented: str, expected: str) -> bool:
    """Constant-time secret comparison. Bytes, because ``compare_digest`` rejects non-ASCII str."""
    return hmac.compare_digest(presented.encode(), expected.encode())


def _sign(token: str, payload: str) -> str:
    """HMAC-SHA256 of ``payload`` under the API token."""
    return hmac.new(token.encode(), payload.encode(), sha256).hexdigest()


def _csrf_token(token: str, session_id: str) -> str:
    """The CSRF token bound to one session. Domain-separated from the cookie signature."""
    return _sign(token, f"csrf:{session_id}")


async def _valid_session_id(request: Request, cookie: str, token: str) -> str:
    """Verify the cookie's signature, then the session row. Raises 401 on any failure."""
    session_id, _, signature = cookie.rpartition(".")
    if not session_id or not _matches(signature, _sign(token, session_id)):
        raise _unauthorized("The session cookie is not valid.")
    # Expiry and fingerprint are both filters, not fetched values: SQLite returns naive datetimes,
    # and the fingerprint predicate is what makes token rotation invalidate the session (R12).
    async with transaction(_engine(request)) as conn:
        found = await conn.scalar(
            select(sessions.c.id).where(
                sessions.c.id == session_id,
                sessions.c.expires_at > datetime.now(UTC),
                sessions.c.token_fingerprint == token_fingerprint(token),
            )
        )
    if found is None:
        raise _unauthorized("The session has expired or was invalidated. Sign in again.")
    return session_id


def _check_csrf(request: Request, session_id: str, token: str) -> None:
    """Enforce the double-submit CSRF token on a cookie-authenticated write. Raises 403."""
    presented = request.headers.get(CSRF_HEADER)
    if presented is None or not _matches(presented, _csrf_token(token, session_id)):
        raise ProblemError(
            status=_FORBIDDEN,
            title="Forbidden",
            detail=f"A cookie-authenticated {request.method} requires the {CSRF_HEADER} header.",
            type=error_type("csrf-required"),
        )


def _readonly_forbidden(method: str) -> ProblemError:
    return ProblemError(
        status=_FORBIDDEN,
        title="Forbidden",
        detail=f"The read-only token cannot perform a {method}.",
        type=error_type("readonly-token"),
    )


def _unauthorized(detail: str) -> ProblemError:
    return ProblemError(
        status=_UNAUTHORIZED,
        title="Unauthorized",
        detail=detail,
        type=error_type("unauthorized"),
    )
