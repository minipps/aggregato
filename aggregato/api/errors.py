"""One problem-detail response shape for API errors.

Every response body below is the RFC 9457 problem detail from ``contracts/openapi.yaml`` —
``type``, ``title``, ``status``, ``detail``, ``instance`` — served as
``application/problem+json``. FastAPI's default ``{"detail": ...}`` and its default 422
validation envelope are both replaced, because "uniform" means one shape, not two.

``type`` convention, chosen once here so it stays stable across the API:

* ``about:blank`` for a plain HTTP status error carrying no extra semantics. RFC 9457 §4.2.1
  makes this the explicit default, and inventing a URN per status would give clients nothing
  the ``status`` field does not already say.
* ``urn:aggregato:error:<slug>`` for a domain error a client can branch on — for example
  ``urn:aggregato:error:invalid-cursor``. A URN rather than an ``https://`` URL because a URL
  promises a document at a host this self-hosted project does not own.

``instance`` is the request path, which is the only per-occurrence identifier available without
inventing a correlation id nobody asked for.
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Plain ints rather than ``starlette.status`` constants: Starlette is mid-rename on 422
# (``HTTP_422_UNPROCESSABLE_ENTITY`` now warns) and ``filterwarnings = ["error"]`` turns that
# deprecation into a collection failure.
_UNPROCESSABLE: Final = 422
_INTERNAL: Final = 500

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
"""RFC 9457 media type. Not ``application/json`` — the contract names this one."""

BLANK_TYPE: Final = "about:blank"

ERROR_TYPE_PREFIX: Final = "urn:aggregato:error:"
"""Namespace for domain error types. See the module docstring for why it is a URN."""


def error_type(slug: str) -> str:
    """Build the ``type`` URI for a domain error.

    Args:
        slug: Kebab-case name of the error kind, e.g. ``invalid-cursor``.

    Returns:
        The namespaced URN, e.g. ``urn:aggregato:error:invalid-cursor``.
    """
    return f"{ERROR_TYPE_PREFIX}{slug}"


class ProblemError(Exception):
    """A failure that already knows how it should look on the wire.

    Route and service code raises this instead of assembling a response, so the ``type`` URI and
    the detail text live next to the condition that produced them.

    Args:
        status: HTTP status code to return.
        title: Short, human-readable summary of the error kind. Stable for a given ``type``.
        detail: What went wrong with *this* occurrence. Must not contain anything the caller is
            not allowed to see — it is echoed verbatim.
        type: The ``type`` URI. Defaults to ``about:blank``; pass ``error_type("...")`` for a
            domain error clients are expected to branch on.
    """

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        # ``type`` shadows the builtin on purpose: it is the RFC 9457 member name.
        type: str = BLANK_TYPE,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type


def problem_response(
    request: Request,
    status: int,
    title: str,
    detail: str | None = None,
    # ``type`` shadows the builtin on purpose: it is the RFC 9457 member name.
    type: str = BLANK_TYPE,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Render one problem detail document.

    Args:
        request: The request being answered; supplies ``instance``.
        status: HTTP status code, mirrored into the body's ``status`` member.
        title: Short summary of the error kind.
        detail: Occurrence-specific explanation, omitted from the body when ``None``.
        type: The ``type`` URI.
        headers: Extra response headers to preserve, e.g. ``WWW-Authenticate`` off a 401.

    Returns:
        A ``JSONResponse`` with media type ``application/problem+json``.
    """
    body: dict[str, Any] = {
        "type": type,
        "title": title,
        "status": status,
        "instance": request.url.path,
    }
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


async def problem_error_handler(request: Request, exc: Exception) -> Response:
    """Turn a :class:`ProblemError` into its response."""
    assert isinstance(exc, ProblemError)  # Starlette types handlers as taking bare Exception.
    return problem_response(request, exc.status, exc.title, exc.detail, exc.type)


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """Turn any Starlette/FastAPI ``HTTPException`` into problem+json.

    This covers the framework's own 404s and 405s as well as ``raise HTTPException(...)`` in route
    code, so no path can return the default ``{"detail": ...}`` body.
    """
    assert isinstance(exc, HTTPException)
    return problem_response(
        request,
        exc.status_code,
        _http_title(exc.status_code),
        exc.detail,
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """Turn a request validation failure into problem+json rather than FastAPI's 422 envelope.

    The per-field errors are flattened into ``detail`` because the contract's ``Problem`` schema
    has no place for a structured list, and adding an extension member here would mean two shapes.
    """
    assert isinstance(exc, RequestValidationError)
    detail = "; ".join(_describe_validation_error(e) for e in exc.errors()) or "Invalid request."
    return problem_response(
        request,
        _UNPROCESSABLE,
        "Unprocessable Entity",
        detail,
        error_type("validation-failed"),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Answer an unexpected exception with a 500 problem detail that leaks nothing.

    The exception type, message and traceback are deliberately absent from the body: a stack trace
    on the wire is an information disclosure, and this is a self-hosted server whose logs the
    operator can already read. Starlette re-raises after this handler runs, so the traceback still
    reaches the server log.
    """
    del exc  # Intentionally unused; see above.
    return problem_response(
        request,
        _INTERNAL,
        "Internal Server Error",
        "The server encountered an unexpected condition. Check the server log for details.",
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install every handler in this module on ``app``.

    One call so ``main.py`` cannot install three of the four and reintroduce a second error shape.

    Args:
        app: The application to register on. Mutated in place.
    """
    app.add_exception_handler(ProblemError, problem_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


def _http_title(status: int) -> str:
    """Reason phrase for a status code, falling back to a generic class name."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Client Error" if status < _INTERNAL else "Server Error"


def _describe_validation_error(err: Any) -> str:
    """Render one pydantic error as ``location: message``."""
    location = ".".join(str(part) for part in err.get("loc", ()))
    message = str(err.get("msg", "invalid"))
    return f"{location}: {message}" if location else message
