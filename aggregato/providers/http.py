"""The host's HTTP client (research.md , ).

This module lives in the providers tree because architecture.md puts it here, but it is **host
code**: it is
the one thing in this package allowed to import ``httpx``, and the import-linter contract in
pyproject.toml names that single exemption. Every actual provider package still cannot reach
``httpx``, which is the rule that matters — a provider able to construct its own client would be a
provider able to raise its own rate limit.

The politeness guarantees are therefore structural rather than reviewed. A provider is *handed* the
client in ``ProviderContext.http`` and has no way to obtain an unwrapped one:

* a token bucket at ``max(provider_declared, host_floor_for_acquisition_mode)`` — a ``max``, never a
  plugin-supplied value, so declaring a huge rate cannot lower the floor;
* one in-flight request per host for ``scrapes`` providers;
* retry on 5xx, 429, and transport errors, with jitter;
* ``Retry-After`` honoured when the platform sends it;
* ``ETag`` / ``If-Modified-Since`` passed through, so an unchanged page costs a 304;
* a User-Agent naming the project, version, and a contact URL, because an operator whose scraper
  misbehaves should be reachable rather than anonymous.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import cast
from weakref import WeakKeyDictionary

import httpx

from aggregato import __version__
from aggregato.domain.clock import SYSTEM_CLOCK
from aggregato.domain.enums import Acquisition
from aggregato.providers.errors import BlockedError, RateLimited, TransportError

#: Where to reach whoever is running this. Part of the User-Agent so a platform operator has
#: somewhere to complain other than a block list.
PROJECT_URL = "https://github.com/minipps/aggregato"

USER_AGENT = f"Aggregato/{__version__} (+{PROJECT_URL})"

#: Minimum seconds between requests, per acquisition mode. These are **floors**: the effective
#: interval is ``max(this, whatever the provider asked for)``. Scraping is slowest by an order of
#: magnitude because it is the surface with no rate-limit contract and the most capacity to annoy.
HOST_FLOOR_SECONDS: Mapping[Acquisition, float] = {
    Acquisition.API: 0.1,
    Acquisition.FEED: 1.0,
    Acquisition.EXPORT: 1.0,
    Acquisition.SCRAPE: 2.0,
}

#: Status codes worth trying again. 429 is included because the platform asked us to wait, not
#: because the request was wrong.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: In-run attempts before giving up and letting the cross-run retry ladder take over.
MAX_ATTEMPTS = 4

#: Cap on a single sleep. A platform sending `Retry-After: 86400` is telling us to come back
#: tomorrow, which is the scheduler's job, not something to block a worker on.
MAX_SLEEP_SECONDS = 60.0
MAX_REDIRECTS = 5
_ALLOWED_REQUEST_KWARGS = frozenset({"params", "headers", "json", "content", "data"})


class RateLimiter:
    """A single-rate token bucket, one per (provider, run).

    Simplest thing that enforces "no more than one request every N seconds": remember when the last
    request was permitted and sleep out the remainder. A real bucket with burst capacity would let a
    provider fire twenty requests at once after an idle minute, which is precisely the behaviour the
    floors exist to prevent.
    """

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        # Pacing uses the event loop's monotonic clock, not the injected wall clock: a wall clock
        # that steps backwards over an NTP correction would hand out a free burst of requests.
        self._lock = asyncio.Lock()
        self._next_allowed_monotonic = 0.0

    async def acquire(self) -> None:
        """Block until the next request is permitted. Serialized, so concurrent callers queue."""
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._next_allowed_monotonic - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = asyncio.get_running_loop().time()
            self._next_allowed_monotonic = now + self._min_interval

    def ensure_minimum(self, interval_seconds: float) -> None:
        """Raise a shared host limiter when another run declares a slower policy."""
        self._min_interval = max(self._min_interval, interval_seconds)


@dataclass
class _HostState:
    limiter: RateLimiter
    semaphore: asyncio.Semaphore


_HOST_STATES: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, int], _HostState]] = (
    WeakKeyDictionary()
)


@dataclass(frozen=True)
class PolitenessPolicy:
    """The resolved, un-overridable pacing for one provider's run.

    Built by the host from the provider's declared interval and its acquisition mode. A provider
    never constructs one of these, which is what makes ``effective_interval`` a floor rather than a
    suggestion.
    """

    acquisition: Acquisition
    declared_interval_seconds: float = 0.0

    @property
    def effective_interval_seconds(self) -> float:
        """``max(declared, floor)`` — the whole of  in one expression."""
        return max(self.declared_interval_seconds, HOST_FLOOR_SECONDS[self.acquisition])

    @property
    def max_concurrent_per_host(self) -> int:
        """Scraping providers get exactly one in-flight request per host."""
        return 1 if self.acquisition is Acquisition.SCRAPE else 4


class PoliteClient:
    """An ``httpx.AsyncClient`` wrapped in the host's pacing, retries, and identity.

    The provider calls ``get``/``post``/``request`` on this exactly as it would on the real client.
    It cannot reach the underlying client to bypass the wrapper, and it cannot change the policy the
    wrapper was built with.
    """

    def __init__(
        self,
        policy: PolitenessPolicy,
        *,
        client: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._limiter = RateLimiter(policy.effective_interval_seconds)
        # One semaphore per host, created on first sight. A single global semaphore would make a
        # slow platform throttle an unrelated one.
        self._host_locks: dict[str, asyncio.Semaphore] = {}
        self._host_limiters: dict[str, RateLimiter] = {}
        # Injected so retry jitter is reproducible in tests (testing guidance).
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        self._now = now or SYSTEM_CLOCK.now
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _host_lock(self, url: httpx.URL) -> asyncio.Semaphore:
        return self._host_state(url).semaphore

    def _host_state(self, url: httpx.URL) -> _HostState:
        host = url.host or ""
        key = (host, self._policy.max_concurrent_per_host)
        loop = asyncio.get_running_loop()
        states = _HOST_STATES.setdefault(loop, {})
        state = states.get(key)
        if state is None:
            state = _HostState(
                limiter=self._limiter,
                semaphore=asyncio.Semaphore(self._policy.max_concurrent_per_host),
            )
            states[key] = state
        else:
            state.limiter.ensure_minimum(self._policy.effective_interval_seconds)
        self._host_locks[host] = state.semaphore
        self._host_limiters[host] = state.limiter
        return state

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Perform one request under the host's pacing and retry rules.

        Args:
            method: HTTP method.
            url: Absolute URL.
            **kwargs: Passed through to ``httpx``. An ``ETag`` or ``If-Modified-Since`` header the
                provider sets is passed through untouched, so an unchanged page costs a 304.

        Returns:
            The response, including a 304 and including a non-retryable 4xx — deciding what a 404
            means is the provider's business, not the wrapper's.

        Raises:
            BlockedError: The platform answered 403 or 451. Retrying deepens a block, so this
                short-circuits the ladder entirely .
            RateLimited: Still 429 after ``MAX_ATTEMPTS``, carrying ``Retry-After`` when the
                platform sent one, so the scheduler can lengthen the interval for the whole
                session .
            httpx.TransportError: The transport failed on every attempt.
        """
        unexpected = set(kwargs) - _ALLOWED_REQUEST_KWARGS
        if unexpected:
            names = ", ".join(sorted(unexpected))
            raise TypeError(f"unsupported host HTTP option(s): {names}")
        raw_headers = kwargs.get("headers")
        if raw_headers is not None and not isinstance(raw_headers, Mapping):
            raise TypeError("headers must be a mapping")
        headers = httpx.Headers(cast(Mapping[str, str] | None, raw_headers))
        # A plugin may add conditional or authorization headers, but it cannot impersonate a
        # different client or remove the contact identity the host owns.
        headers["User-Agent"] = USER_AGENT
        request_kwargs = {key: value for key, value in kwargs.items() if key != "headers"}
        request_kwargs["headers"] = headers
        target = _validated_url(url)
        redirects = 0
        last_transport_error: httpx.TransportError | None = None

        while True:
            response: httpx.Response | None = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                state = self._host_state(target)
                async with state.semaphore:
                    await state.limiter.acquire()
                    try:
                        response = await self._client.request(
                            method,
                            target,
                            **request_kwargs,  # type: ignore[arg-type]
                        )
                    except httpx.TransportError as exc:
                        last_transport_error = exc
                    else:
                        last_transport_error = None
                if response is None:
                    if attempt == MAX_ATTEMPTS:
                        raise TransportError(
                            f"{target.host} could not be reached after "
                            f"{MAX_ATTEMPTS} attempts: {last_transport_error}"
                        ) from last_transport_error
                    await self._sleep_before_retry(attempt, None)
                    continue

                # 403/451 are not rate limits and not transient. Treating them as retryable is how
                # a soft block becomes a hard one.
                if response.status_code in (403, 451):
                    raise BlockedError(
                        f"{target.host} refused access with {response.status_code}; "
                        "not retrying, because retrying deepens a block"
                    )

                if response.status_code not in RETRYABLE_STATUS:
                    break

                retry_after = _parse_retry_after(response, now=self._now)
                if attempt == MAX_ATTEMPTS:
                    if response.status_code == 429:
                        raise RateLimited(
                            f"{target.host} still rate-limiting after {attempt} attempts",
                            retry_after=retry_after,
                        )
                    break
                await self._sleep_before_retry(attempt, retry_after)

            assert response is not None
            location = response.headers.get("Location")
            if response.status_code not in {301, 302, 303, 307, 308} or not location:
                return response
            redirects += 1
            if redirects > MAX_REDIRECTS:
                raise TransportError(f"{url} exceeded the {MAX_REDIRECTS}-redirect limit")
            target = _validated_url(str(target.join(location)))
            if response.status_code in {301, 302, 303} and method.upper() not in {"GET", "HEAD"}:
                method = "GET"
                request_kwargs = {
                    key: value
                    for key, value in request_kwargs.items()
                    if key not in {"json", "content", "data"}
                }

        # Unreachable: the loop either returns or raises on its final attempt. Kept explicit so a
        # future edit to the loop bounds fails loudly instead of returning None.
        raise AssertionError(f"retry loop fell through for {url}", last_transport_error)

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        """Honour ``Retry-After`` when given, otherwise exponential backoff with jitter.

        Jitter matters because several providers coming back from the same outage would otherwise
        retry in lockstep, and the platform sees a thundering herd rather than a recovery.
        """
        if retry_after is not None:
            delay = retry_after
        else:
            delay = (2 ** (attempt - 1)) * self._policy.effective_interval_seconds
            delay += self._rng.uniform(0, delay or 1.0)
        await asyncio.sleep(min(delay, MAX_SLEEP_SECONDS))


def _parse_retry_after(
    response: httpx.Response, *, now: Callable[[], datetime] | None = None
) -> float | None:
    """Parse ``Retry-After`` as delta-seconds or an HTTP-date."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = (now or SYSTEM_CLOCK.now)()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - current.astimezone(UTC)).total_seconds()
    return max(0.0, seconds)


def _validated_url(value: str) -> httpx.URL:
    """Allow only ordinary HTTP(S) URLs, including every manually followed redirect."""
    target = httpx.URL(value)
    if target.scheme not in {"http", "https"} or not target.host:
        raise TransportError("host HTTP requests require an absolute http(s) URL")
    return target
