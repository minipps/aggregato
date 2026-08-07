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
import hashlib
import math
import os
import random
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, cast
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
_REDIRECT_CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "cookie2"}
)


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


class _FileLock:
    """A kernel-released exclusive lock held by one process or thread."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    def acquire(self, *, blocking: bool = True) -> None:
        """Open and lock the file, closing it again if acquisition fails."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            _lock_handle(handle, blocking=blocking)
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        """Release the kernel lock and close its descriptor."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock_handle(handle)
        finally:
            handle.close()


class ProcessHostState:
    """Coordinate request reservations and scraper exclusivity across child processes.

    The reservation clock is monotonic and the state is protected by a kernel file lock. A caller
    reserves a future slot without holding that lock during the request itself. Scrapers acquire a
    second per-host lock and hold its descriptor for the complete request, so a crashed process
    releases exclusivity when the kernel closes its descriptors.
    """

    def __init__(self, directory: Path, *, clock: Callable[[], float] | None = None) -> None:
        self._directory = directory
        self._clock = clock or time.monotonic

    def reserve(
        self,
        host: str,
        min_interval_seconds: float,
        *,
        now: float | None = None,
    ) -> float:
        """Reserve the next host slot and return how long the caller must wait.

        This synchronous method is intentionally small and deterministic when ``now`` is supplied;
        the async wrapper used by :class:`PoliteClient` runs it in a worker thread so file locking
        never blocks the event loop.
        """
        current = self._clock() if now is None else now
        lock = _FileLock(self._reservation_lock_path(host))
        lock.acquire()
        try:
            next_allowed = _read_next_allowed(self._state_path(host))
            reserved_at = max(current, next_allowed)
            _write_next_allowed(
                self._state_path(host), reserved_at + max(0.0, min_interval_seconds)
            )
        finally:
            lock.release()
        return max(0.0, reserved_at - current)

    async def reserve_async(self, host: str, min_interval_seconds: float) -> float:
        """Reserve a slot without blocking the provider event loop on filesystem I/O."""
        return await asyncio.to_thread(self.reserve, host, min_interval_seconds)

    def acquire_scrape_lock(self, host: str, *, blocking: bool = True) -> _FileLock:
        """Acquire the held per-host scraper lock.

        ``blocking=False`` is useful for deterministic tests; production callers use the async
        wrapper, which performs the blocking acquisition in a worker thread.
        """
        lock = _FileLock(self._scrape_lock_path(host))
        lock.acquire(blocking=blocking)
        return lock

    async def acquire_scrape(self, host: str) -> _FileLock:
        """Acquire scraper exclusivity without blocking the provider event loop."""
        return await asyncio.to_thread(self.acquire_scrape_lock, host)

    def _host_stem(self, host: str) -> str:
        return hashlib.sha256(host.encode("utf-8")).hexdigest()

    def _reservation_lock_path(self, host: str) -> Path:
        return self._directory / f"{self._host_stem(host)}.reservation.lock"

    def _scrape_lock_path(self, host: str) -> Path:
        return self._directory / f"{self._host_stem(host)}.scrape.lock"

    def _state_path(self, host: str) -> Path:
        return self._directory / f"{self._host_stem(host)}.state"


def _lock_handle(handle: BinaryIO, *, blocking: bool) -> None:
    """Take an exclusive advisory lock using the platform's kernel primitive."""
    if os.name == "posix":
        import fcntl

        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), operation)
        return

    # The supported deployment is Linux, but keep the helper usable on Windows without adding a
    # dependency. ``msvcrt`` locks one byte and releases it automatically when the descriptor
    # closes.
    msvcrt = __import__("msvcrt")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    operation = msvcrt.LK_NBLCK
    while True:
        try:
            msvcrt.locking(handle.fileno(), operation, 1)
        except OSError:
            if not blocking:
                raise
            time.sleep(0.01)
        else:
            return


def _unlock_handle(handle: BinaryIO) -> None:
    """Release a lock taken by :func:`_lock_handle`."""
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    msvcrt = __import__("msvcrt")
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _read_next_allowed(path: Path) -> float:
    """Read a reservation, treating a missing or interrupted state as empty."""
    try:
        value = float(path.read_text(encoding="ascii"))
    except (FileNotFoundError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _write_next_allowed(path: Path, value: float) -> None:
    """Atomically replace a reservation state while its per-host lock is held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{value:.9f}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)


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
        host_state_dir: Path | None = None,
        rng: random.Random | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._limiter = RateLimiter(policy.effective_interval_seconds)
        self._process_host_state = (
            ProcessHostState(host_state_dir) if host_state_dir is not None else None
        )
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
                    if self._process_host_state is not None:
                        wait = await self._process_host_state.reserve_async(
                            target.host or "", self._policy.effective_interval_seconds
                        )
                        if wait > 0:
                            await asyncio.sleep(wait)
                    scrape_lock: _FileLock | None = None
                    try:
                        if (
                            self._process_host_state is not None
                            and self._policy.acquisition is Acquisition.SCRAPE
                        ):
                            scrape_lock = await self._process_host_state.acquire_scrape(
                                target.host or ""
                            )
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
                    finally:
                        if scrape_lock is not None:
                            await asyncio.to_thread(scrape_lock.release)
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
            previous_target = target
            target = _validated_url(str(target.join(location)))
            if not _same_origin(previous_target, target):
                for header in _REDIRECT_CREDENTIAL_HEADERS:
                    headers.pop(header, None)
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


def _same_origin(left: httpx.URL, right: httpx.URL) -> bool:
    """Return whether redirect credentials remain on the same scheme, host, and port."""
    return left.scheme == right.scheme and left.host == right.host and left.port == right.port
