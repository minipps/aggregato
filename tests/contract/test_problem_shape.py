"""Every failure returns the same RFC 9457 document (T040, Constitution III).

Driven through ``httpx.ASGITransport`` — in-process, no socket, so the autouse socket blocker in
``tests/conftest.py`` is satisfied. ``raise_app_exceptions=False`` is required for the 500 case:
Starlette's ``ServerErrorMiddleware`` sends the handler's response and *then* re-raises so the
traceback still reaches the server log, which is exactly the behaviour under test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aggregato.api.errors import (
    PROBLEM_MEDIA_TYPE,
    ProblemError,
    error_type,
    register_error_handlers,
)

SECRET_IN_TRACEBACK = "asparagus-in-the-connection-string"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """A throwaway app with one route per failure mode, plus the handlers under test."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/missing")
    async def missing() -> None:
        raise HTTPException(status_code=404, detail="No entry with that id.")

    @app.get("/bad-cursor")
    async def bad_cursor() -> None:
        raise ProblemError(
            status=400,
            title="Bad Request",
            detail="The cursor parameter is not a valid cursor.",
            type=error_type("invalid-cursor"),
        )

    @app.get("/validated")
    async def validated(limit: int) -> None:  # missing/non-integer limit -> 422
        return None

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(SECRET_IN_TRACEBACK)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def _assert_problem(response: httpx.Response, status: int) -> dict[str, object]:
    """Assert the response is a well-formed problem detail for ``status`` and return its body."""
    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = response.json()
    for field in ("type", "title", "status", "detail", "instance"):
        assert field in body, f"missing {field}: {body}"
    assert body["status"] == status  # the body must not disagree with the status line
    assert isinstance(body["title"], str) and body["title"]
    assert isinstance(body["type"], str) and body["type"]
    return body


async def test_404_is_problem_json(client: httpx.AsyncClient) -> None:
    body = _assert_problem(await client.get("/missing"), 404)
    assert body["type"] == "about:blank"  # a plain status error, per the module's convention
    assert body["title"] == "Not Found"
    assert body["instance"] == "/missing"


async def test_unrouted_path_is_problem_json_too(client: httpx.AsyncClient) -> None:
    # FastAPI's own 404, not one a route raised: the default {"detail": ...} must not survive.
    _assert_problem(await client.get("/nope"), 404)


async def test_400_is_problem_json_with_a_domain_type(client: httpx.AsyncClient) -> None:
    body = _assert_problem(await client.get("/bad-cursor"), 400)
    assert body["type"] == "urn:aggregato:error:invalid-cursor"
    assert body["detail"] == "The cursor parameter is not a valid cursor."


async def test_validation_error_is_problem_json_not_fastapis_422_envelope(
    client: httpx.AsyncClient,
) -> None:
    body = _assert_problem(await client.get("/validated", params={"limit": "abc"}), 422)
    assert "detail" in body and isinstance(body["detail"], str)  # a string, not FastAPI's list
    assert "limit" in body["detail"]


async def test_missing_required_query_param_is_problem_json(client: httpx.AsyncClient) -> None:
    _assert_problem(await client.get("/validated"), 422)


async def test_unhandled_exception_is_a_500_problem(client: httpx.AsyncClient) -> None:
    body = _assert_problem(await client.get("/boom"), 500)
    assert body["title"] == "Internal Server Error"


async def test_unhandled_exception_leaks_neither_message_nor_traceback(
    client: httpx.AsyncClient,
) -> None:
    raw = (await client.get("/boom")).text
    assert SECRET_IN_TRACEBACK not in raw
    assert "RuntimeError" not in raw
    assert "Traceback" not in raw
    assert "test_problem_shape" not in raw  # no file paths from the stack either
