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
import os
import random
import signal
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path

import httpx2
import pytest

from aggregato import __version__
from aggregato.domain.enums import Acquisition
from aggregato.providers.errors import BlockedError, RateLimited
from aggregato.providers.http import (
    HOST_FLOOR_SECONDS,
    MAX_ATTEMPTS,
    USER_AGENT,
    PoliteClient,
    PolitenessPolicy,
    ProcessHostState,
)

# Deterministic jitter: testing guidance makes randomness injectable rather than ambient.
FIXED_RNG = 1234


def client_for(
    policy: PolitenessPolicy,
    handler: object,
) -> PoliteClient:
    transport = httpx2.MockTransport(handler)  # type: ignore[arg-type]
    inner = httpx2.AsyncClient(transport=transport, headers={"User-Agent": USER_AGENT})
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

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Yield control, so any missing mutual exclusion actually manifests.
        await asyncio.sleep(0)
        in_flight -= 1
        return httpx2.Response(200, text="ok")

    # Zero pacing delay, so the semaphore is the only thing preventing overlap.
    policy = PolitenessPolicy(acquisition=Acquisition.SCRAPE, declared_interval_seconds=0.0)
    client = client_for(policy, handler)
    object.__setattr__(client._limiter, "_min_interval", 0.0)

    async with client:
        await asyncio.gather(*(client.get("https://example.test/p") for _ in range(10)))

    assert peak == 1, f"a scraper ran {peak} concurrent requests against one host"


def test_process_shared_reservations_queue_across_coordinators(tmp_path: Path) -> None:
    """Separate coordinator instances reserve one monotonic host timeline."""
    first = ProcessHostState(tmp_path, clock=lambda: 100.0)
    second = ProcessHostState(tmp_path, clock=lambda: 100.0)

    assert first.reserve("example.test", 2.0, now=100.0) == 0.0
    assert second.reserve("example.test", 2.0, now=100.0) == 2.0
    assert first.reserve("example.test", 5.0, now=100.0) == 4.0
    assert second.reserve("example.test", 2.0, now=100.0) == 9.0
    assert first.reserve("other.test", 2.0, now=100.0) == 0.0


def test_process_shared_reservation_discards_stale_state(tmp_path: Path) -> None:
    """A reservation in the past never delays the next request."""
    coordinator = ProcessHostState(tmp_path)
    state_path = coordinator._state_path("example.test")
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text("98\n", encoding="ascii")

    assert coordinator.reserve("example.test", 2.0, now=100.0) == 0.0


@pytest.mark.skipif(os.name != "posix", reason="process-group file-lock test requires POSIX")
def test_process_shared_scrape_lock_is_held_across_processes(tmp_path: Path) -> None:
    """A separate process cannot enter a scraper host until the owner releases it."""
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(ready_read)
        os.close(release_write)
        try:
            held = ProcessHostState(tmp_path).acquire_scrape_lock("example.test")
            os.write(ready_write, b"1")
            os.read(release_read, 1)
            held.release()
        except BaseException:
            os.write(ready_write, b"0")
            os._exit(1)
        os._exit(0)

    os.close(ready_write)
    os.close(release_read)
    waited = False
    try:
        assert os.read(ready_read, 1) == b"1"
        second = ProcessHostState(tmp_path)
        with pytest.raises(BlockingIOError):
            second.acquire_scrape_lock("example.test", blocking=False)
        os.write(release_write, b"1")
        _, status = os.waitpid(child_pid, 0)
        waited = True
        assert os.WIFEXITED(status)
    finally:
        for descriptor in (ready_read, release_write):
            with suppress(OSError):
                os.close(descriptor)
        if not waited:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)

    released = ProcessHostState(tmp_path).acquire_scrape_lock("example.test", blocking=False)
    released.release()


async def test_separate_hosts_do_not_block_each_other() -> None:
    """A request held by one host must not block an unrelated host."""
    started = {host: asyncio.Event() for host in ("a.test", "b.test")}
    release_a = asyncio.Event()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        assert host is not None
        started[host].set()
        if host == "a.test":
            await release_a.wait()
        return httpx2.Response(200)

    policy = PolitenessPolicy(acquisition=Acquisition.SCRAPE, declared_interval_seconds=0.0)
    client = client_for(policy, handler)
    object.__setattr__(client._limiter, "_min_interval", 0.0)

    async with client:
        first = asyncio.create_task(client.get("https://a.test/x"))
        await started["a.test"].wait()
        second = asyncio.create_task(client.get("https://b.test/x"))
        try:
            await asyncio.sleep(0)
            assert started["b.test"].is_set()
        finally:
            release_a.set()
            await asyncio.gather(first, second)


# --- Identity ---------------------------------------------------------------------------------


async def test_requests_carry_an_identifying_user_agent() -> None:
    """An operator whose scraper misbehaves should be reachable, not anonymous."""
    captured: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request.headers["User-Agent"])
        return httpx2.Response(200)

    async with client_for(PolitenessPolicy(acquisition=Acquisition.API), handler) as client:
        await client.get("https://example.test/")

    assert captured == [USER_AGENT]
    assert __version__ == version("aggregato")
    assert USER_AGENT.startswith(f"Aggregato/{version('aggregato')} ")
    assert "Aggregato/" in USER_AGENT
    assert "http" in USER_AGENT, "the User-Agent must carry a contact URL"


# --- Blocking is never retried ----------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 451])
async def test_a_block_raises_immediately_and_is_never_retried(status: int) -> None:
    """Retrying a block deepens it, so this must short-circuit rather than climb the ladder."""
    attempts = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(status)

    async with client_for(PolitenessPolicy(acquisition=Acquisition.SCRAPE), handler) as client:
        with pytest.raises(BlockedError, match="not retrying"):
            await client.get("https://example.test/")

    assert attempts == 1, f"a block was retried {attempts} times"


# --- Retries, Retry-After, and what is left alone ---------------------------------------------


async def test_transient_server_errors_are_retried_then_succeed() -> None:
    responses = [httpx2.Response(503), httpx2.Response(502), httpx2.Response(200, text="finally")]

    async def handler(request: httpx2.Request) -> httpx2.Response:
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

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "7"}, text="slow down")

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", record_sleep)
            with pytest.raises(RateLimited) as caught:
                await client.get("https://example.test/")

    assert caught.value.retry_after == 7.0
    assert 7.0 in slept, f"Retry-After was ignored; slept {slept}"


async def test_a_rate_limit_without_retry_after_still_reports_the_class() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429)

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
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})

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

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(503)

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

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        raise httpx2.ConnectError("no route to host")

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

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(status)

    async with client_for(PolitenessPolicy(acquisition=Acquisition.API), handler) as client:
        response = await client.get("https://example.test/")

    assert response.status_code == status
    assert attempts == 1


async def test_conditional_request_headers_pass_through_untouched() -> None:
    """ETag / If-Modified-Since belong to the provider; the host must not strip or rewrite them."""
    captured: dict[str, str] = {}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured.update(request.headers)
        return httpx2.Response(304)

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


async def test_cross_origin_redirect_drops_credential_headers() -> None:
    """A redirect must not send provider credentials to a different origin."""
    captured: list[httpx2.Headers] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request.headers)
        if len(captured) == 1:
            return httpx2.Response(302, headers={"Location": "https://other.test/landing"})
        return httpx2.Response(200)

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        object.__setattr__(client._limiter, "_min_interval", 0.0)
        response = await client.get(
            "https://example.test/start",
            headers={
                "Authorization": "Bearer secret",
                "Proxy-Authorization": "Basic secret",
                "Cookie": "session=secret",
                "If-None-Match": '"abc123"',
            },
        )

    assert response.status_code == 200
    assert captured[0]["authorization"] == "Bearer secret"
    assert captured[0]["proxy-authorization"] == "Basic secret"
    assert captured[0]["cookie"] == "session=secret"
    assert "authorization" not in captured[1]
    assert "proxy-authorization" not in captured[1]
    assert "cookie" not in captured[1]
    assert captured[1]["if-none-match"] == '"abc123"'
    assert captured[1]["user-agent"] == USER_AGENT


async def test_same_origin_redirect_keeps_credential_headers() -> None:
    """Credentials remain available when the redirect stays on the original origin."""
    captured: list[httpx2.Headers] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request.headers)
        if len(captured) == 1:
            return httpx2.Response(302, headers={"Location": "/landing"})
        return httpx2.Response(200)

    policy = PolitenessPolicy(acquisition=Acquisition.API, declared_interval_seconds=0.0)
    async with client_for(policy, handler) as client:
        object.__setattr__(client._limiter, "_min_interval", 0.0)
        response = await client.get(
            "https://example.test/start",
            headers={"Authorization": "Bearer secret", "Cookie": "session=secret"},
        )

    assert response.status_code == 200
    assert captured[1]["authorization"] == "Bearer secret"
    assert captured[1]["cookie"] == "session=secret"


# --- Pacing actually paces --------------------------------------------------------------------


async def test_the_limiter_spaces_requests_by_the_effective_interval() -> None:
    """Asserted on the delays requested, not on wall-clock time — no test may sleep for real."""
    slept: list[float] = []
    original = asyncio.sleep

    async def record_sleep(delay: float) -> None:
        slept.append(delay)
        await original(0)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200)

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
