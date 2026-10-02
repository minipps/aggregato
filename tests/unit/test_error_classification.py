"""Contract §4 is a table; this is that table, executable."""

from __future__ import annotations

import httpx2
import pytest

from aggregato.domain.enums import ErrorClass
from aggregato.providers.errors import (
    AuthError,
    BlockedError,
    ProviderError,
    RateLimited,
    StructureChangedError,
)
from aggregato.sync.errors import NEVER_RETRY, classify, schedules_retry


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AuthError("token rejected"), ErrorClass.AUTH),
        (BlockedError("captcha"), ErrorClass.BLOCKED),
        (StructureChangedError("no such field"), ErrorClass.STRUCTURE_CHANGED),
        (RateLimited("slow down", retry_after=30), ErrorClass.RATE_LIMIT),
        (ProviderError("something the plugin did not name"), ErrorClass.INTERNAL),
    ],
)
def test_provider_exceptions_map_to_their_documented_class(
    exc: ProviderError, expected: ErrorClass
) -> None:
    assert classify(exc) is expected


@pytest.mark.parametrize(
    "exc",
    [
        httpx2.ConnectError("no route"),
        httpx2.ReadTimeout("too slow"),
        httpx2.ConnectTimeout("too slow"),
        httpx2.RemoteProtocolError("truncated"),
    ],
)
def test_httpx2_transport_errors_are_transport(exc: httpx2.TransportError) -> None:
    assert classify(exc) is ErrorClass.TRANSPORT


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_http_5xx_is_transport(status: int) -> None:
    request = httpx2.Request("GET", "https://example.invalid/list")
    exc = httpx2.HTTPStatusError(
        "server error", request=request, response=httpx2.Response(status, request=request)
    )
    assert classify(exc) is ErrorClass.TRANSPORT


def test_http_4xx_a_provider_did_not_classify_is_internal() -> None:
    # A 4xx should have been raised as AuthError or BlockedError by the provider. Leaving it
    # `internal` keeps the provider bug visible instead of dressing it up as a network blip.
    request = httpx2.Request("GET", "https://example.invalid/list")
    exc = httpx2.HTTPStatusError(
        "teapot", request=request, response=httpx2.Response(418, request=request)
    )
    assert classify(exc) is ErrorClass.INTERNAL


def test_unknown_exception_is_internal() -> None:
    assert classify(ZeroDivisionError("host bug")) is ErrorClass.INTERNAL


def test_never_retry_set_is_exactly_the_three_documented_classes() -> None:
    assert set(NEVER_RETRY) == {
        ErrorClass.AUTH,
        ErrorClass.BLOCKED,
        ErrorClass.STRUCTURE_CHANGED,
    }


@pytest.mark.parametrize(
    "error_class", [ErrorClass.AUTH, ErrorClass.BLOCKED, ErrorClass.STRUCTURE_CHANGED]
)
def test_auth_blocked_and_structure_changed_never_schedule_a_retry(
    error_class: ErrorClass,
) -> None:
    assert not schedules_retry(error_class), (
        f"{error_class.value} was made retryable. Retrying it does active harm: an `auth` retry "
        f"can lock the operator out of their own account, a `blocked` retry deepens the block, and "
        f"a `structure_changed` retry hammers a platform over code that only a release can fix "
        f"(, contract §4). These three go straight to `degraded` with no next_run_at."
    )


@pytest.mark.parametrize(
    "error_class",
    [ErrorClass.RATE_LIMIT, ErrorClass.TRANSPORT, ErrorClass.PARSE, ErrorClass.INTERNAL],
)
def test_recoverable_classes_do_schedule_a_retry(error_class: ErrorClass) -> None:
    assert schedules_retry(error_class)


def test_every_error_class_has_a_retry_answer() -> None:
    # Total by construction: a newly added ErrorClass cannot slip through unclassified.
    assert all(isinstance(schedules_retry(member), bool) for member in ErrorClass)
