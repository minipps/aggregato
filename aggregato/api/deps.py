"""Authentication for every request (, research.md , ).

There is no unauthenticated mode. A request carries either ``Authorization: Bearer <api.token>``
or the ``aggregato_session`` cookie; anything else is a 401 problem detail.  also forbids the
token appearing in a URL or in page source, which is why the browser gets a cookie instead of the
token itself.

Design decisions worth stating once:

* **Constant-time token comparison.** ``hmac.compare_digest`` on bytes. A ``==`` on a secret leaks
  length and prefix through timing, and this is the only credential in the system .
* **Sessions live in the database** (``sessions``), not in a self-contained signed cookie, so that
  rotating ``api.token`` can actually reach them: every row stores the ``token_fingerprint`` it was
  issued under, and a lookup requires it to equal the fingerprint of the *current* token. Rotation
  therefore invalidates every session, as  requires. Expiry is checked in SQL for the same
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
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Final, Literal

from fastapi import FastAPI
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.requests import Request

from aggregato.api.clock import now as request_now
from aggregato.api.errors import ProblemError, error_type
from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import sessions
from aggregato.domain.clock import SYSTEM_CLOCK, Clock

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
"""How long a cookie session lasts. One user, one token: there is no refresh flow ."""

MAX_ACTIVE_SESSIONS: Final = 100
"""Bound the number of live sessions a leaked token can mint before cleanup runs."""

_SESSION_ISSUANCE_LOCK_KEY: Final = 482901735
"""PostgreSQL transaction-lock key shared by every API process issuing sessions."""

AUTH_FAILURE_WINDOW_SECONDS: Final = 60.0
AUTH_FAILURE_LIMIT: Final = 20
_auth_failures: dict[str, deque[float]] = {}

_UNAUTHORIZED: Final = 401
_FORBIDDEN: Final = 403

# A bearer token is not attached automatically by a browser, so these need no CSRF token when
# presented with one; only cookie authentication does .
_UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_SESSION_PATHS: Final = frozenset({"/api/v1/auth/session", "/auth/session"})
"""Exact session-exchange paths for the mounted API and direct route assemblies."""

_PUBLIC_IMAGE_ROUTE: Final = re.compile(r"^(?:/api/v1)?/media/image/[0-9a-f]{64}$")


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


def register_auth(
    app: FastAPI, *, config: Config, engine: AsyncEngine, clock: Clock | None = None
) -> None:
    """Attach the config and engine that :func:`require_auth` and the routes read.

    ``main.py`` must additionally construct the app as
    ``FastAPI(dependencies=[Depends(require_auth)])`` so no route can forget the check .

    Args:
        app: The application to mutate.
        config: Resolved configuration; ``config.api.token`` is the only credential.
        engine: The async engine the ``sessions`` table is read and written through.
        clock: Optional request timestamp source; direct ``FastAPI`` test assemblies default to the
            system clock, while :func:`aggregato.main.create_app` supplies the app clock.
    """
    app.state.config = config
    app.state.engine = engine
    if clock is not None:
        app.state.clock = clock
    elif not hasattr(app.state, "clock"):
        app.state.clock = SYSTEM_CLOCK


async def require_auth(request: Request) -> AuthContext:
    """Authenticate any request: bearer token or session cookie, plus CSRF on cookie writes.

    Inputs: the request. Declared app-wide as a dependency, and returned so a route can tell how it
    was authenticated.

    Failure modes: :class:`~aggregato.api.errors.ProblemError` 401 when no credential is present,
    the bearer token does not match, or the session is unknown, expired, or was issued under a
    rotated token; 403 when a cookie-authenticated state-changing request has no valid CSRF token.
    Never FastAPI's default ``{"detail": ...}`` 401 shape.

    One exception: reading a cached image is unauthenticated, so a page can embed
    ``<img src=".../media/image/...">`` — a browser attaches neither a bearer header nor a
    cross-site cookie to an image load. The path carries a sha256, which cannot be enumerated, and
    the bytes are artwork the source platform already serves publicly. Nothing else is exempt.
    """
    if _is_public_image(request):
        return AuthContext(via="bearer", readonly=True)
    _check_auth_rate(request)

    presented = _bearer(request)
    if presented is not None:
        matched = _match_credential(request, presented)
        if matched is None:
            _record_auth_failure(request)
            raise _unauthorized("The bearer token is not valid.")
        _clear_auth_failures(request)
        _, readonly = matched
        if readonly and _is_write(request):
            raise _readonly_forbidden(request.method)
        return AuthContext(via="bearer", readonly=readonly)

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie is None:
        _record_auth_failure(request)
        raise _unauthorized(
            "This request needs an Authorization: Bearer header or a session cookie."
        )
    try:
        session_id, credential, readonly = await _valid_session(request, cookie)
    except ProblemError:
        _record_auth_failure(request)
        raise
    _clear_auth_failures(request)
    if readonly and _is_write(request):
        raise _readonly_forbidden(request.method)
    if request.method in _UNSAFE_METHODS:
        _check_csrf(request, session_id, credential)
    return AuthContext(via="cookie", session_id=session_id, readonly=readonly)


async def require_bearer(request: Request) -> None:
    """Require a bearer credential specifically — what ``POST /auth/session`` exchanges.

    Either the API token or ``api.readonly_token`` is accepted; the session issued carries whatever
    the presented credential grants.

    Failure modes: 401 if the header is absent or matches neither token. A session cookie is
    deliberately not accepted here: the endpoint's job is to turn a token into a cookie.
    """
    presented = _bearer(request)
    if presented is None or _match_credential(request, presented) is None:
        _record_auth_failure(request)
        raise _unauthorized("POST /auth/session requires the API token as a bearer credential.")
    _clear_auth_failures(request)


async def issue_session(request: Request) -> tuple[str, str]:
    """Create a session row and return ``(cookie_value, csrf_token)``.

    Inputs: the request, for the app's engine and the bearer credential it presented — a read-only
    token yields a read-only session. The session id is 256 bits from :mod:`secrets`; the cookie
    value is ``<id>.<HMAC(credential, id)>``, so a cookie that was not issued by this server is
    rejected before any database round trip, rotating a token invalidates the signature as well as
    the row, and which credential signed it is what marks the session read-only — no column and no
    migration for a flag the signature already carries.

    Failure modes: database errors propagate as a 500 problem detail. Callers must gate on
    :func:`require_bearer`, which is what guarantees a credential matched.
    """
    matched = _match_credential(request, _bearer(request) or "")
    if matched is None:  # pragma: no cover - require_bearer already rejected this request
        raise _unauthorized("POST /auth/session requires the API token as a bearer credential.")
    token, _ = matched
    session_id = secrets.token_urlsafe(32)
    now = request_now(request)
    async with transaction(_engine(request)) as conn:
        await _lock_session_issuance(conn)
        # Housekeeping on the one write path this table has, so expired rows cannot accumulate
        # forever without a separate sweeper job.
        await conn.execute(delete(sessions).where(sessions.c.expires_at <= now))
        active = list(
            await conn.execute(
                select(sessions.c.id)
                .where(sessions.c.expires_at > now)
                .order_by(sessions.c.created_at.desc(), sessions.c.id.desc())
                .with_for_update()
            )
        )
        # Keep one slot for the new session. This is deliberately cleanup rather than rejection:
        # repeated legitimate logins cannot turn into a denial-of-service against the operator.
        if len(active) >= MAX_ACTIVE_SESSIONS:
            evicted = [row.id for row in active[MAX_ACTIVE_SESSIONS - 1 :]]
            await conn.execute(delete(sessions).where(sessions.c.id.in_(evicted)))
        await conn.execute(
            sessions.insert().values(
                id=session_id,
                created_at=now,
                expires_at=now + SESSION_TTL,
                token_fingerprint=token_fingerprint(token),
            )
        )
    return f"{session_id}.{_sign(token, session_id)}", _csrf_token(token, session_id)


async def _lock_session_issuance(conn: AsyncConnection) -> None:
    """Serialize session-cap checks across API processes on PostgreSQL.

    SQLite obtains its database writer lock from the expiry cleanup ``DELETE`` below. PostgreSQL
    otherwise has no gap lock when the active-session set is empty or below the cap, so an
    application-wide transaction advisory lock closes that race without a schema sentinel row.
    """
    if conn.dialect.name == "postgresql":
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _SESSION_ISSUANCE_LOCK_KEY},
        )


def _token(request: Request) -> str:
    config: Config = request.app.state.config
    return config.api.token.get_secret_value()


def _readonly_token(request: Request) -> str | None:
    config: Config = request.app.state.config
    secret = config.api.readonly_token
    return None if secret is None else secret.get_secret_value()


def _credentials(request: Request) -> list[tuple[str, bool]]:
    """Every accepted bearer credential, most-privileged first, paired with its read-only flag."""
    readonly = _readonly_token(request)
    pairs = [(_token(request), False)]
    if readonly is not None:
        pairs.append((readonly, True))
    return pairs


def _match_credential(request: Request, presented: str) -> tuple[str, bool] | None:
    """The configured credential ``presented`` equals, or ``None``. Every candidate is compared in
    constant time, and the full token wins if both are somehow the same string."""
    for candidate in _credentials(request):
        if _matches(presented, candidate[0]):
            return candidate
    return None


def _is_write(request: Request) -> bool:
    """Whether this request changes state, for the purpose of refusing a read-only credential.

    ``POST /auth/session`` is excluded: it mints a session no stronger than the credential
    presented, so a read-only token exchanging itself for a read-only cookie escalates nothing —
    and without that exchange the SPA could only work by keeping the token in page source, which
     forbids.
    """
    return request.method in _UNSAFE_METHODS and request.url.path not in _SESSION_PATHS


def _is_public_image(request: Request) -> bool:
    """Whether this is a read of a cached image, the one route that needs no credential.

    Reads only: a ``POST`` to this path has no route anyway, and treating one as authenticated would
    hand an unauthenticated caller whatever a future write here does.
    """
    return (
        request.method in {"GET", "HEAD"}
        and _PUBLIC_IMAGE_ROUTE.fullmatch(request.url.path) is not None
    )


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


def _auth_client_key(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _check_auth_rate(request: Request) -> None:
    now = time.monotonic()
    key = _auth_client_key(request)
    failures = _auth_failures.get(key)
    if failures is None:
        return
    while failures and now - failures[0] >= AUTH_FAILURE_WINDOW_SECONDS:
        failures.popleft()
    if not failures:
        _auth_failures.pop(key, None)
    elif len(failures) >= AUTH_FAILURE_LIMIT:
        raise ProblemError(
            status=429,
            title="Too many authentication attempts",
            detail="wait before trying to authenticate again",
            type=error_type("auth-rate-limited"),
        )


def _record_auth_failure(request: Request) -> None:
    now = time.monotonic()
    key = _auth_client_key(request)
    failures = _auth_failures.setdefault(key, deque())
    while failures and now - failures[0] >= AUTH_FAILURE_WINDOW_SECONDS:
        failures.popleft()
    failures.append(now)
    if len(_auth_failures) > 1024:
        oldest = min(_auth_failures, key=lambda item: _auth_failures[item][0])
        _auth_failures.pop(oldest, None)


def _clear_auth_failures(request: Request) -> None:
    _auth_failures.pop(_auth_client_key(request), None)


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


async def _valid_session(request: Request, cookie: str) -> tuple[str, str, bool]:
    """Verify the cookie's signature, then the session row.

    Returns ``(session_id, signing_credential, readonly)``. Which credential's HMAC verifies is what
    identifies a read-only session, so the caller gets it back to derive the CSRF token from.

    Raises 401 on any failure.
    """
    session_id, _, signature = cookie.rpartition(".")
    matched = None
    if session_id:
        for candidate, readonly in _credentials(request):
            if _matches(signature, _sign(candidate, session_id)):
                matched = (candidate, readonly)
                break
    if matched is None:
        raise _unauthorized("The session cookie is not valid.")
    credential, readonly = matched
    # Expiry and fingerprint are both filters, not fetched values: SQLite returns naive datetimes,
    # and the fingerprint predicate is what makes token rotation invalidate the session .
    async with transaction(_engine(request)) as conn:
        found = await conn.scalar(
            select(sessions.c.id).where(
                sessions.c.id == session_id,
                sessions.c.expires_at > request_now(request),
                sessions.c.token_fingerprint == token_fingerprint(credential),
            )
        )
    if found is None:
        raise _unauthorized("The session has expired or was invalidated. Sign in again.")
    return session_id, credential, readonly


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
