"""Protocol and runtime context for provider plugins.

See ``docs/contracts/provider-plugin.md`` for the public contract. Providers must normalize
deterministically, use the host HTTP client, return only closed vocabulary values, extract all
identifiers, and raise ``StructureChangedError`` when a source's structure changes. CAPTCHA or
anti-bot challenges must raise ``BlockedError``; providers must not try to bypass them.
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

    # Type-only import: an import-linter contract forbids `aggregato.providers -> httpx2` and this
    # module has no exemption (pyproject.toml). The host constructs the client; we only name it.
    from httpx2 import AsyncClient


@dataclass(frozen=True)
class ProviderContext:
    """Selected provider inputs for one child operation.

    Frozen prevents rebinding context fields; it does not sandbox Python. Normal worker runs use a
    child process without a database engine. Bundled providers are checked by conformance and
    import rules; drop-ins require source review.
    """

    http: AsyncClient
    """Host-managed client enforcing acquisition rate limits and the retry policy."""

    config: BaseModel
    """Validated instance of the selected provider's ``config_model``."""

    secrets: Mapping[str, str]
    """Credentials for this provider, resolved before the child starts."""

    log: logging.Logger
    """Logger bound to this provider, run, and lineage."""

    state: MutableMapping[str, str]
    """Mutable mapping for this child operation; the child protocol does not return mutations."""

    import_path: Path | None = None
    """Set only in ``import`` mode, pointing at the operator-supplied export file.

    ``None`` in every other mode; a provider that needs it and finds ``None`` has been called
    wrongly and should raise rather than guess a path.
    """


@runtime_checkable
class Provider(Protocol):
    """Structural interface implemented by a package's module-level ``provider`` object.

    Implementations declare the attributes as class-level annotations typed exactly as below —
    ``config_model: type[BaseModel] = MyConfig`` rather than a bare assignment — because a
    ``Protocol``'s mutable attributes are invariant.
    """

    id: str
    """Stable slug used to associate this provider with its stored rows.

    Keep it stable across source changes.
    """

    name: str
    """Display name, shown in the UI."""

    media_types: set[MediaType]
    """Media types this provider can produce, from the closed ``MediaType`` enum."""

    capabilities: set[Capability]
    """Declared provider features, checked against observed behavior by conformance tests."""

    acquisition: Acquisition
    """Source category. Follow the acquisition hierarchy in ``CONTRIBUTING.md``."""

    config_model: type[BaseModel]
    """Pydantic model for runtime configuration; the static manifest supplies the UI schema."""

    rating_scales: list[RatingScale]
    """Rating scales this provider may use; undeclared scale IDs are rejected during ingest."""

    schema_version: int
    """Version of this provider's normalization schema; a bump replays stored raw payloads."""

    default_poll_interval: timedelta
    """Default poll interval based on the source's rate limits; the scheduler may lengthen it."""

    def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Stream raw records, with checkpoints where the source supports resumption.

        Declared as a plain ``def`` returning an ``AsyncIterator`` because it is an async
        generator.

        Args:
            ctx: The host-provided context. ``ctx.http`` is the only permitted client.
            cursor: Where to resume from, as last checkpointed; ``None`` starts from the beginning.
            mode: ``incremental``, ``full``, or ``import``. In ``import`` mode ``ctx.import_path``
                is set and the provider must not make network requests.

        Yields:
            ``RawRecord`` per platform record and ``Checkpoint`` values where resumption is
            meaningful. The host persists each checkpoint's cursor.

        Raises:
            AuthError: Credentials missing, wrong, or no longer accepted. No retry.
            BlockedError: A block page, CAPTCHA, or anti-bot challenge. Do not retry or
                circumvent it.
            StructureChangedError: The payload no longer has the expected structure. Raise this
                instead of returning an empty result.
            RateLimited: The platform asked us to slow down; carries ``retry_after`` when stated.
        """
        ...

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        """Convert one raw record to the host vocabulary.

        This method must be pure and synchronous.

        Do not use the network, clock, randomness, or storage. Replay runs this method on stored
        payloads, so the same record must produce the same output. Timestamps come from
        ``raw.payload``.

        Args:
            raw: A record exactly as ``fetch`` yielded it, possibly read back from storage months
                later.

        Returns:
            A ``NormalizedBatch``. Every entry requires ``logged_precision``. Include all payload
            identifiers in ``external_ids`` or ``creator_external_ids``. Preserve the platform's
            role text verbatim in ``role_raw``, even when ``role`` maps to a known value.

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
