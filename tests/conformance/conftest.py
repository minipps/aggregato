"""Registration and shared fixtures for the provider conformance suite (contract §5).

This suite is a **merge gate** (Constitution II and V) and the third-party compliance proof
(FR-046). It is therefore parametrized over a registration list rather than written against any one
provider: adding a provider means adding a ``Registration`` below, not writing new tests.

A provider registers by naming its id, the recorded fixture ``fetch`` should read, the fixture that
must make ``check`` fail, and how to build its config from a fixture path. Everything else is
derived: the provider object comes from ``load_provider``, and the records come out of the
provider's own ``fetch`` rather than from a second reader of the wire format written here — a
conformance suite that parsed the fixtures itself would be asserting against its own parser.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from aggregato.domain.enums import Acquisition, Capability, FetchMode
from aggregato.domain.models import Checkpoint, Cursor, RawRecord
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.registry import load_provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from httpx import AsyncClient


@dataclass(frozen=True)
class Registration:
    """One provider entered into the gate.

    Attributes:
        provider_id: The slug, which is also the package name and the fixture directory name.
        records: Fixture under ``tests/fixtures/<id>/`` holding the recorded records, spanning at
            least two pages so group 6 has a checkpoint to resume from.
        invalid: Fixture that stands in for an invalid credential set, so group 7 can assert
            ``check`` reports rather than raises. For a file-based provider this is an unreadable
            file; for an API provider it is a rejected-credentials response.
        config: Builds the ``config_model`` kwargs from the fixture path being exercised. Providers
            that do not read a path ignore the argument.
        secrets: What the host would resolve from the environment. Placeholders only — a real
            credential in this tree is a review failure (tests/fixtures/README.md).
        structure_changed: A fixture whose structure no longer matches the reader. Required for
            scraping providers (group 9), meaningless for others.
    """

    provider_id: str
    records: str
    invalid: str
    config: Callable[[Path], dict[str, Any]] = lambda _path: {}
    secrets: dict[str, str] = field(default_factory=dict)
    structure_changed: str | None = None


REGISTERED = [
    Registration(
        provider_id="fixture",
        records="log-two-pages.jsonl",
        invalid="unreadable-not-json.jsonl",
        config=lambda path: {"path": path},
    ),
]
"""Every bundled provider. ``test_every_bundled_provider_is_registered`` fails if one is missing, so
shipping a provider without entering it into the gate is not possible (contract §5)."""


class _InertHTTP:
    """Stands in for ``ctx.http``.

    ``ProviderContext.http`` is typed ``AsyncClient`` and the type must not be weakened (FR-043
    depends on the host owning the client), so this is cast at the one construction site below.
    Handing over an object that raises on **any** attribute access is safe here because a
    conformance test that touches the network is a failure, not a slow test (contract §5): the
    autouse socket blocker in tests/conftest.py already makes it one, and this makes the same
    mistake fail at the call rather than at the connect.
    """

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"a conformance test reached ctx.http.{name}: conformance runs offline with no "
            "credentials, so a provider needing HTTP must be exercised against a recorded fixture"
        )


@pytest.fixture(params=REGISTERED, ids=[r.provider_id for r in REGISTERED])
def registration(request: pytest.FixtureRequest) -> Registration:
    return cast("Registration", request.param)


@pytest.fixture
def provider(registration: Registration) -> Provider:
    """The provider object, loaded exactly as the host loads it — no test-only construction."""
    return load_provider(registration.provider_id)


@pytest.fixture
def provider_fixtures(registration: Registration, fixtures_dir: Path) -> Path:
    """``tests/fixtures/<id>/``, which the README makes part of the contract."""
    return fixtures_dir / registration.provider_id


@pytest.fixture
def records_path(registration: Registration, provider_fixtures: Path) -> Path:
    return provider_fixtures / registration.records


def build_ctx(registration: Registration, path: Path, **overrides: Any) -> ProviderContext:
    """A context holding only what contract §2 says the host hands over."""
    return ProviderContext(
        http=cast("AsyncClient", _InertHTTP()),
        config=load_provider(registration.provider_id).config_model(**registration.config(path)),
        secrets=dict(registration.secrets),
        log=logging.getLogger(f"conformance.{registration.provider_id}"),
        state={},
        **overrides,
    )


@pytest.fixture
def ctx(registration: Registration, records_path: Path) -> ProviderContext:
    return build_ctx(registration, records_path)


async def fetch_all(
    provider: Provider, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
) -> list[RawRecord | Checkpoint]:
    """Drain ``fetch`` into a list, records and checkpoints interleaved as yielded."""
    return [item async for item in provider.fetch(ctx, cursor, mode)]


def only_records(items: list[RawRecord | Checkpoint]) -> list[RawRecord]:
    return [item for item in items if isinstance(item, RawRecord)]


@pytest.fixture
async def raw_records(provider: Provider, ctx: ProviderContext) -> list[RawRecord]:
    """The provider's recorded records, obtained through its own ``fetch``.

    Groups 3, 4 and 5 run over these, so what they assert about is exactly what a real run would
    have handed to ``normalize`` and stored for replay (FR-002).
    """
    records = only_records(await fetch_all(provider, ctx, None, FetchMode.FULL))
    assert records, "a registered provider's records fixture must contain at least one record"
    return records


def scrapes(provider: Provider) -> bool:
    """Whether group 9 applies: either declaration is enough to be a scraper for the contract."""
    return Capability.SCRAPES in provider.capabilities or provider.acquisition is Acquisition.SCRAPE
