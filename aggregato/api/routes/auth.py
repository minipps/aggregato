"""``POST /auth/session`` — exchange the API token for a browser session (T041, R12).

The token itself must never reach page source or a URL (FR-032), so the SPA calls this once with
the operator's token and works from cookies afterwards.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from starlette.requests import Request

from aggregato.api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_TTL,
    issue_session,
    require_bearer,
)

router = APIRouter(tags=["operations"])


@router.post("/auth/session", status_code=204, dependencies=[Depends(require_bearer)])
async def create_session(request: Request, response: Response) -> Response:
    """Establish a session: set the cookies and return 204, per the contract.

    Inputs: an ``Authorization: Bearer <api.token>`` header. Outputs: an ``HttpOnly``,
    ``SameSite=Lax`` session cookie and the readable ``aggregato_csrf`` cookie whose value a
    cookie-authenticated write must echo in ``X-CSRF-Token``.

    Failure modes: 401 problem+json when the bearer token is missing or wrong.
    """
    cookie_value, csrf = await issue_session(request)
    # Secure only over TLS: forcing it would silently break the plain-HTTP LAN install, which is a
    # supported deployment (R12). Uvicorn's --proxy-headers makes this correct behind a TLS proxy.
    secure = request.url.scheme == "https"
    max_age = int(SESSION_TTL.total_seconds())
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    # Readable on purpose — the frontend has to copy it into the header. It is not a credential:
    # the server recomputes the expected value from the signed session id.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )
    response.status_code = 204
    return response
