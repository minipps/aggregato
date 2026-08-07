"""The politeness guarantees are un-overridable .

The point of these tests is not that the limiter works — it is that a *provider* cannot make it not
work.  says the floors are not negotiable, and a rule enforced only by review is the kind that
eventually leaks, so each guarantee below is asserted from the outside, the way a misbehaving plugin
would try to break it.

Every test drives a `MockTransport`, so nothing here opens a socket (`conftest.py` blocks them
anyway) and no test waits on a real clock.
"""

from __future__ import annotations

import asyncio
import random

import httpx
import pytest

from aggregato.domain.enums import Acquisition
from aggregato.providers.errors import BlockedError, RateLimited
from aggregato.providers.http import (
    HOST_FLOOR_SECONDS,
    MAX_ATTEMPTS,
    USER_AGENT,
    PoliteClient,
    PolitenessPolicy,
)

# Deterministic jitter: testing guidance makes randomness injectable rather than ambient.
FIXED_RNG = 1234


def client_for(
    policy: PolitenessPolicy,
    handler: object,
) -> PoliteClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    inner = httpx.AsyncClient(transport=transport, headers={"User-Agent": USER_AGENT})
    return PoliteClient(policy, client=inner, rng=random.Random(FIXED_RNG))


# --- The floor is a max(), never a provider-supplied value ------------------------------------


@pytest.mark.parametrize("acquisition", list(Acquisition))
def test_a_provider_cannot_lower_the_floor(acquisition: Acquisition) -> None:
    """Declaring an absurdly fast interval must not beat the host's floor."""
    greedy = PolitenessPolicy(acquisition=acquisition, declared_interval_seconds=0.0)
    assert greedy.effective_interval_seconds == HOST_FLOOR_SECONDS[acquisition]


@pytest.mark.parametrize("acquisition", list(Acquisition))
def test_a_provider_cannot_lower_the_floor_with_a_negative_interval(
    acquisition: Acquisition,
) -> None:
    # max() makes this structurally impossible rather than validated-against, which is the point.
    sneaky = PolitenessPolicy(acquisition=acquisition, declared_interval_seconds=-9999.0)
    assert sneaky.effective_interval_seconds == HOST_FLOOR_SECONDS[acquisition]


def test_a_provider_may_ask_to_be_slower_than_the_floor() -> None:
    """The floor is a minimum, not a target: a platform asking for 10s gets 10s."""
    polite = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=10.0)
    assert polite.effective_interval_seconds == 10.0


def test_scraping_is_the_slowest_floor() -> None:
    # Scraping has no rate-limit contract and the most capacity to annoy, so it must never be paced
    # faster than a documented API.
    assert HOST_FLOOR_SECONDS[Acquisition.SCRAPE] > HOST_FLOOR_SECONDS[Acquisition.API]


# --- One in-flight request per host, for scrapers ----------------------------------------------


def test_scraping_providers_get_exactly_one_in_flight_request_per_host() -> None:
    assert PolitenessPolicy(acquisition=Acquisition.SCRAPE).max_concurrent_per_host == 1


def test_api_providers_may_have_more_than_one_in_flight() -> None:
    assert PolitenessPolicy(acquisition=Acquisition.API).max_concurrent_per_host > 1


async def test_a_scraper_cannot_exceed_one_concurrent_request_per_host() -> None:
    """Fire ten requests at once and prove they never overlap.

    A provider that gathers its own coroutines is the realistic way this would be broken, so that is
    exactly what the test does.
    """
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Yield control, so any missing mutual exclusion actually manifests.
        await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(200, text="ok")

    # Zero pacing delay, so the semaphore is the only thing preventing overlap.
    policy = PolitenessPolicy(acquisition=Acquisition.SCRAPE, declared_interval_seconds=0.0)
    client = client_for(policy, handler)
    object.__setattr__(client._limiter, "_min_interval", 0.0)

    async with client:
        await asyncio.gather(*(client.get("https://example.test/p") for _ in range(10)))

    assert peak == 1, f"a scraper ran {peak} concurrent requests against one host"


async def test_separate_hosts_do_not_block_each_other() -> None:
    """A slow platform must not throttle an unrelated one — hence a semaphore per host."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200)

    policy = PolitenessPolicy(acquisition=Acquisition.SCRAPE, declared_interval_seconds=0.0)
    client = client_for(policy, handler)
    object.__setattr__(client._limiter, "_min_interval", 0.0)

    async with client:
        await client.get("https://a.test/x")
        await client.get("https://b.test/x")

    assert seen == ["a.test", "b.test"]
    assert len(client._host_locks) == 2


# --- Identity ---------------------------------------------------------------------------------


async def test_requests_carry_an_identifying_user_agent() -> None:
    """An operator whose scraper misbehaves should be reachable, not anonymous."""
    captured: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers["User-Agent"])
        return httpx.Response(200)

    async with client_for(PolitenessPolicy(acquisition=Acquisition.API), handler) as client:
        await client.get("https://example.test/")

    assert captured == [USER_AGENT]
    assert "Aggregato/" in USER_AGENT
    assert "http" in USER_AGENT, "the User-Agent must carry a contact URL"


# --- Blocking is never retried ----------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 451])
async def test_a_block_raises_immediately_and_is_never_retried(status: int) -> None:
    """Retrying a block deepens it, so this must short-circuit rather than climb the ladder."""
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    async with client_for(PolitenessPolicy(acquisition=Acquisition.SCRAPE), handler) as client:
        with pytest.raises(BlockedError, match="not retrying"):
            await client.get("https://example.test/")

    assert attempts == 1, f"a block was retried {attempts} times"


# --- Retries, Retry-After, and what is left alone ---------------------------------------------


async def test_transient_server_errors_are_retried_then_succeed() -> None:
    responses = [httpx.Response(503), httpx.Response(502), httpx.Response(200, text="finally")]

    async def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        response = await client.get("https://example.test/")

    assert response.status_code == 200
    assert response.text == "finally"


async def test_retry_after_is_honoured_over_the_backoff() -> None:
    """A platform that names a delay gets that delay, not our guess."""
    slept: list[float] = []
    original = asyncio.sleep

    async def record_sleep(delay: float) -> None:
        slept.append(delay)
        await original(0)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", record_sleep)
            with pytest.raises(RateLimited) as caught:
                await client.get("https://example.test/")

    assert caught.value.retry_after == 7.0
    assert 7.0 in slept, f"Retry-After was ignored; slept {slept}"


async def test_a_rate_limit_without_retry_after_still_reports_the_class() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            with pytest.raises(RateLimited) as caught:
                await client.get("https://example.test/")

    # None means "the platform did not say" — the host falls back to its ladder rather than
    # inventing a number.
    assert caught.value.retry_after is None


async def test_an_http_date_retry_after_is_parsed_without_sleeping_forever() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            with pytest.raises(RateLimited) as caught:
                await client.get("https://example.test/")

    assert caught.value.retry_after is not None
    assert caught.value.retry_after > 0


async def test_retries_are_bounded() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            response = await client.get("https://example.test/")

    assert attempts == MAX_ATTEMPTS
    # The in-run retries are exhausted, not escalated: the cross-run ladder takes over from here.
    assert response.status_code == 503


async def test_transport_errors_are_retried_then_classified() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("no route to host")

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _no_sleep)
            from aggregato.providers.errors import TransportError

            with pytest.raises(TransportError, match="no route to host"):
                await client.get("https://example.test/")

    assert attempts == MAX_ATTEMPTS


@pytest.mark.parametrize("status", [200, 301, 304, 400, 404, 410, 422])
async def test_non_retryable_responses_come_straight_back(status: int) -> None:
    """Deciding what a 404 means is the provider's business, not the wrapper's.

    304 is in this list on purpose: an unchanged page is a successful, cheap answer, and a wrapper
    that retried it would defeat the ETag pass-through entirely.
    """
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    async with client_for(PolitenessPolicy(acquisition=Acquisition.API), handler) as client:
        response = await client.get("https://example.test/")

    assert response.status_code == status
    assert attempts == 1


async def test_conditional_request_headers_pass_through_untouched() -> None:
    """ETag / If-Modified-Since belong to the provider; the host must not strip or rewrite them."""
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(304)

    async with client_for(PolitenessPolicy(acquisition=Acquisition.FEED), handler) as client:
        response = await client.get(
            "https://example.test/feed",
            headers={
                "If-None-Match": '"abc123"',
                "If-Modified-Since": "Wed, 21 Oct 2026 07:28:00 GMT",
            },
        )

    assert response.status_code == 304
    assert captured["if-none-match"] == '"abc123"'
    assert captured["if-modified-since"] == "Wed, 21 Oct 2026 07:28:00 GMT"


# --- Pacing actually paces --------------------------------------------------------------------


async def test_the_limiter_spaces_requests_by_the_effective_interval() -> None:
    """Asserted on the delays requested, not on wall-clock time — no test may sleep for real."""
    slept: list[float] = []
    original = asyncio.sleep

    async def record_sleep(delay: float) -> None:
        slept.append(delay)
        await original(0)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    policy = PolitenessPolicy(acquisition=Acquisition.SCRAPE)
    async with client_for(policy, handler) as client:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", record_sleep)
            await client.get("https://example.test/a")
            await client.get("https://example.test/b")

    paced = [d for d in slept if d > 0]
    assert paced, "the second request was not paced at all"
    assert max(paced) <= policy.effective_interval_seconds + 0.01


async def _no_sleep(delay: float) -> None:
    """Drop-in for ``asyncio.sleep`` so retry tests exercise the ladder without waiting on it."""
    return None
