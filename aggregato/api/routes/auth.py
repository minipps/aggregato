"""``/auth/session`` — exchange the API token for a browser session, and describe it .

The token itself must never reach page source or a URL , so the SPA calls this once with
the operator's token and works from cookies afterwards. ``GET`` reports back what the current
credential is, which is how the SPA knows to render itself read-only.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from starlette.requests import Request

from aggregato.api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_TTL,
    AuthContext,
    issue_session,
    require_auth,
    require_bearer,
)

router = APIRouter(tags=["operations"])


class SessionView(BaseModel):
    """How the caller is authenticated, exactly the object ``contracts/openapi.yaml`` declares."""

    via: Literal["bearer", "cookie"]
    readonly: bool


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
    # supported deployment . Uvicorn's --proxy-headers makes this correct behind a TLS proxy.
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


@router.get("/auth/session", response_model=SessionView)
async def read_session(ctx: Annotated[AuthContext, Depends(require_auth)]) -> SessionView:
    """Report the current credential, so the SPA can hide what it is not allowed to do.

    Inputs: none beyond authentication — the app-wide dependency has already run, and FastAPI hands
    back its cached result rather than authenticating twice.

    Failure modes: 401 problem+json without credentials, like every other route.
    """
    return SessionView(via=ctx.via, readonly=ctx.readonly)
