"""The provider extension contract (contracts/provider-plugin.md §1 and §2).

The ``Protocol`` below **is** the contract. There is no base class to inherit, no registration
decorator and no hook system: a provider is any object with these attributes and these three
methods, which is what makes a third-party provider possible without importing anything of ours
beyond the domain models.

Hard rules, restated here because this is the file a provider author reads first (contract §1):

* ``normalize`` is **pure** — no network, no clock, no randomness, no storage. It is what makes
  normalization replay  and credential-free offline tests  possible, and the
  conformance suite calls it twice on one fixture and demands identical output.
* A provider **never receives a database handle**, another provider's config, or another provider's
  secrets . This is enforced physically: provider code runs in a child process that has no
  engine and no other provider imported (research.md ). ``ProviderContext`` is the whole of what
  the host hands over.
* A provider **never constructs its own HTTP client** . ``ctx.http`` is the only client,
  because the politeness floors must be un-overridable; an import-linter contract forbids importing
  ``httpx`` anywhere under ``aggregato.providers`` — including this module, which takes the type
  under ``TYPE_CHECKING`` only.
* A provider **may not invent** a ``media_type``, ``role``, or ``subject_ref`` key . The
  vocabularies are closed enums; violations become ``ingest_failures`` at the parent-side boundary
  rather than writes.
* A provider **extracts every identifier** present in a payload, including ones Aggregato has no use
  for  — under the no-enrichment rule this is the largest single lever on match quality.
* **No CAPTCHA or anti-bot circumvention, in any form** . Raise ``BlockedError``; the run
  stops and the provider goes to ``degraded`` immediately.

A provider that finds nothing returns an empty iterator. It must not report success on a structural
failure — raise ``StructureChangedError``, because silence plus delete inference is how an archive
gets erased .
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

from aggregato.domain.enums import Acquisition, Capability, FetchMode, MediaType
from aggregato.domain.models import (
    Checkpoint,
    CheckResult,
    Cursor,
    NormalizedBatch,
    NowPlayingItem,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale

if TYPE_CHECKING:  # pragma: no cover - typing only
    import logging
    from pathlib import Path

    # Type-only import: an import-linter contract forbids `aggregato.providers -> httpx` and this
    # module has no exemption (pyproject.toml). The host constructs the client; we only name it.
    from httpx import AsyncClient


@dataclass(frozen=True)
class ProviderContext:
    """Everything the host hands a provider for one run — and nothing else (contract §2).

    Frozen, because a provider reassigning ``http`` would be exactly the escape from the politeness
    floors  forbids. The guarantee attached to each field is contract text, not decoration:
    a provider may rely on it, and may not reimplement it.

    The host owns, and a provider must not reimplement: scheduling, jitter, the retry ladder, rate
    limiting, cursor persistence, idempotency, identity resolution, storage, migrations, and image
    caching .
    """

    http: AsyncClient
    """The only HTTP client a provider may use (, research.md ).

    Guaranteed: rate limiting at ``max(declared, host_floor)``, one in-flight request per host for
    ``scrapes`` providers, retry on 5xx/429/transport with jitter, ``Retry-After`` compliance, ETag
    pass-through, and the project User-Agent with a contact URL.
    """

    config: BaseModel
    """The validated ``config_model`` instance for **this** provider only .

    Typed as ``BaseModel`` because the context is generic; a provider narrows it at the boundary
    and treats a mismatch as a host bug rather than assuming.
    """

    secrets: Mapping[str, str]
    """This provider's credentials only, resolved from the environment at read time .

    Read-only: secrets are never written back to disk and never returned by the API.
    """

    log: logging.Logger
    """A logger already bound to ``provider_id``, ``run_id``, and ``lineage_id`` .

    A provider logs to it and does not configure logging.
    """

    state: MutableMapping[str, str]
    """A small opaque key/value store, this provider's own, persisted in ``provider_state.kv``.

    For things that are not pagination state — a discovered account id, a last-seen etag. Cursor
    persistence is separate and host-owned : yield a ``Checkpoint`` for that.
    """

    import_path: Path | None = None
    """Set only in ``import`` mode, pointing at the operator-supplied export file.

    ``None`` in every other mode; a provider that needs it and finds ``None`` has been called
    wrongly and should raise rather than guess a path.
    """


@runtime_checkable
class Provider(Protocol):
    """What a provider is (contract §1).

    Implementations declare the attributes as class-level annotations typed exactly as below —
    ``config_model: type[BaseModel] = MyConfig`` rather than a bare assignment — because a
    ``Protocol``'s mutable attributes are invariant.
    """

    id: str
    """Stable slug, e.g. ``"letterboxd"``. It never changes, ever: it is the join key for every
    row the provider has ever written, and it survives a migration to a better acquisition surface
    ."""

    name: str
    """Display name, shown in the UI."""

    media_types: set[MediaType]
    """Every ``MediaType`` this provider can produce. From the closed enum — a provider cannot
    invent one ."""

    capabilities: set[Capability]
    """What the provider can do. Checked against observed behaviour by the conformance suite: a
    provider declaring ``has_ratings`` must produce at least one opinion from its fixtures."""

    acquisition: Acquisition
    """Which surface the data comes from. A provider must use the highest surface available
    , and its pull request states which higher surfaces were evaluated and rejected."""

    config_model: type[BaseModel]
    """Validation **and** the generated settings form (, research.md ).

    Flat scalars, enums and secrets only: the form renderer reads the JSON Schema directly, secrets
    are marked ``writeOnly``, and every field carries a description because the description is the
    label the operator reads."""

    rating_scales: list[RatingScale]
    """Every scale this provider's opinions can cite. A ``rating_raw`` whose ``rating_scale_id`` is
    not declared here is uninterpretable and rejected ."""

    schema_version: int
    """``>= 1``. Bumping it triggers normalization replay over stored raw payloads (,
    research.md ) — the mechanism for shipping a ``normalize`` fix without re-syncing."""

    default_poll_interval: timedelta
    """Derived from the platform's own rate limits, not from a global default . The
    scheduler treats it as a floor it may lengthen, never shorten."""

    def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Stream raw records from the platform, resumably.

        Declared as a plain ``def`` returning an ``AsyncIterator`` rather than ``async def``: that
        is the type of an async generator function, which is how every real implementation is
        written.

        Args:
            ctx: The host-provided context. ``ctx.http`` is the only permitted client.
            cursor: Where to resume from, as last checkpointed; ``None`` starts from the beginning.
            mode: ``incremental``, ``full``, or ``import``. In ``import`` mode ``ctx.import_path``
                is set and no network call should happen at all.

        Yields:
            ``RawRecord`` per platform record, and a ``Checkpoint`` between records wherever
            resumption is meaningful — the host persists the enclosed cursor so a failed run
            resumes there instead of restarting . A provider whose pagination cannot
            support mid-fetch resumption declares only ``full`` and accepts full resyncs.

        Raises:
            AuthError: Credentials missing, wrong, or no longer accepted. No retry.
            BlockedError: A block page, CAPTCHA, or anti-bot challenge. No retry, no circumvention
                .
            StructureChangedError: The payload no longer looks like what this provider expects.
                Raised instead of returning empty .
            RateLimited: The platform asked us to slow down; carries ``retry_after`` when stated.
        """
        ...

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        """Turn one raw record into the host's vocabulary. **Pure** — and synchronous, which is the
        cheapest way to make that credible.

        No network, no clock, no randomness, no storage: replay re-runs this over stored payloads
        , so two calls on the same record must produce byte-identical output. Every
        timestamp, and every value that looks like "now", comes out of ``raw.payload``.

        Args:
            raw: A record exactly as ``fetch`` yielded it, possibly read back from storage months
                later.

        Returns:
            A ``NormalizedBatch``. ``logged_precision`` is required on every entry — there is no
            default, because a default fabricates exactness . Every identifier present in
            the payload appears in ``external_ids`` or ``creator_external_ids`` , and
            ``role_raw`` carries the platform's own word verbatim even when ``role`` maps cleanly
            .

        Raises:
            StructureChangedError: The stored payload is not the shape this provider was written
                against.
            ValueError: A single record is invalid — including Pydantic ``ValidationError``. The
                host records it in ``ingest_failures`` and the run continues (contract §4).
        """
        ...

    async def check(self, ctx: ProviderContext) -> CheckResult:
        """Self-test credentials and reachability for the operator.

        Returns rather than raises, because ``check`` is a diagnostic the operator asked for: its
        failure is the answer, not an error.

        Args:
            ctx: The same context a run would get.

        Returns:
            ``CheckResult(ok=True)``, or ``ok=False`` with the ``ErrorClass`` and a detail message
            written for an operator reading it in the UI.
        """
        ...


@runtime_checkable
class NowPlayingProvider(Protocol):
    """Optional provider capability for reporting the work currently being played."""

    async def now_playing(self, ctx: ProviderContext) -> NowPlayingItem | None:
        """Return the current normalized item, or ``None`` when playback is idle."""
        ...
