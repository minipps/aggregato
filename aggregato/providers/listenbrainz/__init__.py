"""The ``listenbrainz`` provider: listens from ListenBrainz, or any API-compatible server.

``acquisition`` is ``api`` and no lower surface was evaluated as sufficient (, contract §6):
ListenBrainz publishes a documented REST API for a user's listens, so a feed, an export or a scrape
would each carry strictly less than ``GET /1/user/{name}/listens`` already gives.

**The base URL is configurable on purpose.** The wire surface is not ListenBrainz's alone — Maloja
exposes a compatible endpoint under a path prefix (``http://maloja.lan:42010/apis/listenbrainz``) —
so every request is built by joining ``config.base_url`` with the API path. No host is hardcoded and
nothing assumes the path starts at ``/1/`` on the origin, because a prefix that does not survive URL
construction is a provider that only works against one server.

Paging walks **backwards** in time by ``max_ts``. A ``Checkpoint`` after each page carries the
oldest ``listened_at`` seen, so a run that dies resumes at that boundary without duplicates or gaps
. ``incremental`` pins ``min_ts`` to the newest prior listen and walks only what is new.

A listen is a **track** and nothing else. The payload names a release, but emitting an album entry
per listen would fabricate a listening event the operator never had ; the release name rides
along in ``work.metadata`` instead.

Tolerance is deliberate and asymmetric. A compatible server that omits fields ListenBrainz always
sends — ``mbid_mapping`` is absent for any listen ListenBrainz has not matched, ``additional_info``
is absent for a bare scrobble — is handled as the normal case. ``StructureChangedError`` is reserved
for a response whose *shape* is wrong: not a JSON object, no ``payload``, ``listens`` not a list, a
listen with no ``listened_at`` or no ``track_name``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from aggregato.domain.enums import (
    Acquisition,
    Capability,
    Confidence,
    CreatorKind,
    EntryKind,
    ErrorClass,
    FetchMode,
    LoggedPrecision,
    MediaType,
    Role,
)
from aggregato.domain.models import (
    Checkpoint,
    CheckResult,
    Cursor,
    NormalizedBatch,
    NormalizedCreatorId,
    NormalizedCredit,
    NormalizedEntry,
    NormalizedExternalId,
    NormalizedWork,
    RawRecord,
)
from aggregato.domain.ratings import RatingScale
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.errors import (
    AuthError,
    ProviderError,
    RateLimited,
    StructureChangedError,
)

DEFAULT_BASE_URL = "https://api.listenbrainz.org"
"""ListenBrainz's own API root. Overridden per-installation for a compatible third-party server."""

PAGE_SIZE = 100
"""Listens per request. The endpoint permits up to 1000; a smaller page means a crash loses less
work, because the checkpoint boundary is the page ."""

_ARTIST_ID_KEYS = frozenset({"artist_mbids", "artist_msid"})
"""Identifier keys that belong to a *creator*, not to the work — filed as creator ids instead."""

_ID_KEY_SUFFIXES = ("_mbid", "_mbids", "_msid", "_msids", "_id", "_ids")
"""What an identifier key looks like in this payload. A suffix rule rather than a fixed list, so an
identifier a future ListenBrainz release adds is extracted the day it appears ."""

_ID_NAMESPACES: Mapping[str, str] = {
    "recording_mbid": "mbid_recording",
    "release_mbid": "mbid_release",
    "release_group_mbid": "mbid_release_group",
    "caa_release_mbid": "mbid_caa_release",
    "work_mbids": "mbid_work",
    "track_mbid": "mbid_track",
    "recording_msid": "msid_recording",
    "release_msid": "msid_release",
}
"""Payload key → namespace, for the identifiers ListenBrainz documents. Anything else keeps its own
key as the namespace: an unrecognised identifier is worth storing under a guessable name, and is not
worth dropping ."""


class ListenBrainzConfig(BaseModel):
    """Settings for the ListenBrainz provider — and the settings form the UI renders .

    Flat scalars with descriptions, because the description is the label an operator reads.
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        min_length=1,
        description="ListenBrainz username whose listens are read. Case-sensitive, as the platform "
        "stores it.",
    )
    token: SecretStr = Field(
        min_length=1,
        description="ListenBrainz user token, from Settings on the server. Use a ${ENV_VAR} "
        "reference to keep it out of the config file.",
    )
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="Base URL of the ListenBrainz-compatible API: scheme, host, optional port and "
        "optional path prefix. Leave it at https://api.listenbrainz.org for ListenBrainz itself, "
        "or point it at a compatible server — Maloja is the tested case, at "
        "http://maloja.lan:42010/apis/listenbrainz.",
    )

    @field_validator("base_url")
    @classmethod
    def _absolute_http_url(cls, value: str) -> str:
        """Require an absolute http(s) URL and strip a trailing slash, so joining is unambiguous."""
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(
                f"base_url must be an absolute http:// or https:// URL, got {value!r} — a host "
                "alone is not enough, because the port and any path prefix are part of it"
            )
        if parts.query or parts.fragment:
            raise ValueError("base_url must not carry a query string or fragment")
        normalized = value.strip().rstrip("/")
        if urlsplit(normalized).path.endswith("/1"):
            raise ValueError(
                "base_url must stop before the API version /1; use the server's "
                "ListenBrainz API prefix (for example, .../apis/listenbrainz)"
            )
        return normalized


class ListenBrainzProvider:
    """A provider that reads a user's listens from ListenBrainz or a compatible server."""

    id: str = "listenbrainz"
    name: str = "ListenBrainz"
    # RUF012 wants these immutable, but contract/provider-plugin.md §1 publishes them as `set` and
    # `list`, and a provider's declared shape should match the contract a third party reads rather
    # than a lint preference. One instance per provider class, so the shared-default risk is moot.
    media_types: set[MediaType] = {MediaType.TRACK}  # noqa: RUF012
    # No `has_ratings` and no `has_reviews`: a listen carries neither, so declaring them would fail
    # conformance group 2 — and would send an operator debugging an empty ratings view to the wrong
    # provider. `reports_deletes` is equally absent: the endpoint never mentions a deleted listen.
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.BACKFILL,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.API
    # Annotated as the protocol declares them, not inferred: a Protocol's mutable attributes are
    # invariant, so `type[ListenBrainzConfig]` would not satisfy `type[BaseModel]`.
    config_model: type[BaseModel] = ListenBrainzConfig
    rating_scales: list[RatingScale] = []  # noqa: RUF012
    schema_version: int = 1
    # ListenBrainz documents its rate limit as a per-window budget reported in the X-RateLimit-*
    # headers (a few hundred requests per ten-second window), with no daily quota. The platform's
    # own limit constrains a *burst*, not a schedule, and the polling interval is a courtesy figure
    # rather than a quota division . Fifteen minutes keeps a listening session near-live at
    # ~96 requests a day, which is nothing next to that window budget.
    default_poll_interval: timedelta = timedelta(minutes=15)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Yield listens, page by page backwards in time, checkpointing after each page.

        Args:
            ctx: Host context. ``ctx.config`` must be a ``ListenBrainzConfig``; ``ctx.http`` is the
                only client used .
            cursor: ``{"newest_ts": int | None, "max_ts": int | None}`` from a previous checkpoint.
                ``max_ts`` is the oldest ``listened_at`` already emitted. A resumed run continues
                there rather than restarting . ``newest_ts`` is the high-water mark an
                ``incremental`` run pins ``min_ts`` to.
            mode: ``incremental`` walks only listens newer than ``newest_ts``; anything else walks
                the whole history backwards. Only ``poll`` and ``backfill`` are declared, so
                ``import`` is never requested.

        Yields:
            ``RawRecord`` per listen, and a ``Checkpoint`` after each page — including a final one
            with ``max_ts`` cleared, which is what tells the next ``incremental`` run that the walk
            finished and it may start again from the top.

        Raises:
            ProviderError: ``ctx.config`` is not a ``ListenBrainzConfig``, or the server answered
                a status this provider cannot interpret.
            AuthError: HTTP 401 — the token was rejected. No retry.
            RateLimited: HTTP 429.
            StructureChangedError: The response shape is wrong, or the server did not page backwards
                and a walk would never terminate.
        """
        config = _config(ctx)
        state = cursor.state if cursor is not None else {}
        newest_ts = _as_int(state.get("newest_ts"))
        max_ts = _as_int(state.get("max_ts"))
        min_ts = newest_ts if mode is FetchMode.INCREMENTAL else None

        while True:
            listens = _listens(await self._page(ctx, config, max_ts=max_ts, min_ts=min_ts))
            if not listens:
                # The walk reached the end of what the server has. Clearing `max_ts` is the whole
                # difference between "resume mid-history" and "start from the top next time".
                yield Checkpoint(cursor=Cursor(state={"newest_ts": newest_ts, "max_ts": None}))
                return

            for listen in listens:
                yield RawRecord(native_id=_native_id(listen), payload=listen)

            stamps = [_listened_at(listen) for listen in listens]
            oldest = min(stamps)
            # `max_ts` is exclusive, so a compliant server always answers with something strictly
            # older. A page that does not move the boundary would otherwise loop forever, which is a
            # structural failure worth naming rather than a run to leave spinning.
            if max_ts is not None and oldest >= max_ts:
                raise StructureChangedError(
                    f"{config.base_url} returned listens at or after max_ts={max_ts}: the server "
                    "is not paging backwards, so a walk would never terminate"
                )
            newest_ts = max([*stamps, newest_ts]) if newest_ts is not None else max(stamps)
            max_ts = oldest
            # After the page, not before: the cursor names where to continue, so a crash here
            # re-emits nothing that was already ingested .
            yield Checkpoint(cursor=Cursor(state={"newest_ts": newest_ts, "max_ts": max_ts}))

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        """Map one listen onto the host vocabulary. Pure: no clock, no I/O, no randomness.

        ``listened_at`` is a unix timestamp *in the payload*, converted with
        ``datetime.fromtimestamp(ts, tz=UTC)`` — never a wall clock. Replay re-runs this over old
        payloads, and a clock read would rewrite history on the second pass .

        Args:
            raw: A listen as ``fetch`` yielded it, or as replay read it back.

        Returns:
            A ``NormalizedBatch`` — identical on every call for the same input. Every identifier in
            the payload is extracted  and ``role_raw`` keeps the platform's own field name
            verbatim .

        Raises:
            StructureChangedError: The stored payload is not a listen — no ``listened_at``, or no
                ``track_metadata.track_name``.
            ValueError: The payload is a listen but a value does not validate.
        """
        listen = raw.payload
        meta = _as_mapping(listen.get("track_metadata"))
        # Absent for a listen ListenBrainz has not matched to MusicBrainz, and absent from most
        # compatible servers entirely. Normal, not a structure change.
        additional = _as_mapping(meta.get("additional_info"))
        mapping = _as_mapping(meta.get("mbid_mapping"))

        track_name = _as_text(meta.get("track_name"))
        if track_name is None:
            raise StructureChangedError(
                f"listen {raw.native_id!r} has no track_metadata.track_name: a listen without a "
                "track is not a listen"
            )
        listened_at = _listened_at(listen)
        release_name = _as_text(meta.get("release_name"))

        artist_names, artist_field = _artist_names(meta, additional)
        artist_mbids = _artist_mbids(additional, mapping)

        return NormalizedBatch(
            work=NormalizedWork(
                media_type=MediaType.TRACK,
                title=track_name,
                sequence_number=_track_number(additional),
                # The release is context for the track, not a work of its own: emitting an album
                # entry per listen would invent an event the operator never logged .
                metadata={"release_name": release_name} if release_name else {},
            ),
            entries=[
                NormalizedEntry(
                    kind=EntryKind.LISTEN,
                    logged_at=datetime.fromtimestamp(listened_at, tz=UTC),
                    # `exact`, and honestly so: the platform recorded a unix second at submission
                    # time. It is not a date the provider widened or narrowed, so there is nothing
                    # here for 's precision to protect against.
                    logged_precision=LoggedPrecision.EXACT,
                    native_id=raw.native_id,
                )
            ],
            credits=[
                NormalizedCredit(
                    creator_name=name,
                    # ListenBrainz does not say whether an artist is a person or a band, and
                    # guessing from the name is exactly the kind of invention  forbids.
                    creator_kind=CreatorKind.UNKNOWN,
                    role=Role.PERFORMER,
                    # The platform's own term for this field, verbatim : ListenBrainz has no
                    # word for the relationship beyond the key it files the name under.
                    role_raw=artist_field,
                    position=position,
                )
                for position, name in enumerate(artist_names)
            ],
            external_ids=[
                NormalizedExternalId(namespace=namespace, value=value, confidence=confidence)
                for namespace, value, confidence in _work_identifiers(listen, additional, mapping)
            ],
            creator_external_ids=_creator_ids(artist_names, artist_mbids),
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        """Verify the token against the configured base URL, ingesting nothing.

        Args:
            ctx: Host context; ``ctx.config`` must be a ``ListenBrainzConfig``.

        Returns:
            ``ok=True`` when the configured server answers a readable listens page, otherwise
            ``ok=False`` carrying the ``ErrorClass`` the host would have classified the equivalent
            raised failure as — ``auth`` for a rejected token (HTTP 401).
        """
        try:
            config = _config(ctx)
            listens = _listens(await self._page(ctx, config, max_ts=None, min_ts=None))
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=exc.error_class, detail=str(exc))
        except Exception as exc:
            # Deliberately broad, and it cannot be narrower here: naming `httpx.TransportError`
            # would mean importing httpx, which the  import contract forbids in a provider
            # package. `check` is a diagnostic the operator asked for (contract §1), so a DNS
            # failure has to come back as the answer rather than as a crash in the settings UI.
            return CheckResult(
                ok=False,
                error_class=ErrorClass.TRANSPORT,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return CheckResult(
            ok=True,
            detail=f"{config.base_url} answered for user {config.username}: "
            f"{len(listens)} listen(s) in the first page",
        )

    async def _page(
        self,
        ctx: ProviderContext,
        config: ListenBrainzConfig,
        *,
        max_ts: int | None,
        min_ts: int | None,
    ) -> object:
        """Fetch one listens page and classify its status (contract §4).

        Raises:
            AuthError: 401. No retry — retrying a rejected token risks locking the operator out.
            RateLimited: 429. ``retry_after`` is left to the host: ``PoliteClient`` already honours
                ``Retry-After``, and a second reader of that header would be a second policy.
            ProviderError: Any other non-200. ``internal`` sends it to the cross-run ladder; that
                is right for a mistyped username's 404 as much as for a 500.
        """
        url = listens_url(config)
        params: dict[str, int] = {"count": PAGE_SIZE}
        if max_ts is not None:
            params["max_ts"] = max_ts
        if min_ts is not None:
            params["min_ts"] = min_ts

        response = await ctx.http.get(url, params=params, headers=_auth_headers(config))
        if response.status_code == 401:
            raise AuthError(
                f"{url} rejected the configured token (HTTP 401); check the token for user "
                f"{config.username}"
            )
        if response.status_code == 429:
            raise RateLimited(f"{url} is rate-limiting (HTTP 429)")
        if response.status_code != 200:
            raise ProviderError(f"{url} answered HTTP {response.status_code}")
        return response.json()


def _config(ctx: ProviderContext) -> ListenBrainzConfig:
    """Narrow ``ctx.config`` at the boundary rather than assuming the host got it right."""
    if not isinstance(ctx.config, ListenBrainzConfig):
        raise ProviderError(f"listenbrainz provider received a {type(ctx.config).__name__} config")
    return ctx.config


def listens_url(config: ListenBrainzConfig) -> str:
    """Build the listens endpoint URL for the configured server.

    Public because it is the one thing an operator pointing this at Maloja needs to be able to
    predict, and the one thing a test can assert exactly.

    Args:
        config: Validated settings; ``base_url`` is already trailing-slash free.

    Returns:
        ``{base_url}/1/user/{username}/listens`` — so a base carrying a port and a path prefix
        (``http://maloja.lan:42010/apis/listenbrainz``) keeps both.
    """
    return f"{config.base_url}/1/user/{quote(config.username, safe='')}/listens"


def _auth_headers(config: ListenBrainzConfig) -> dict[str, str]:
    """The ListenBrainz auth header. Omitted when no token is set, since listens are public."""
    token = config.token.get_secret_value()
    return {"Authorization": f"Token {token}"} if token else {}


def _listens(body: object) -> list[dict[str, Any]]:
    """The ``payload.listens`` list, or a named structural failure.

    Only the envelope is checked here — a JSON object, a ``payload`` object, a ``listens`` list of
    objects each carrying a ``listened_at``. Missing optional content (``mbid_mapping``,
    ``additional_info``, ``release_name``) is the normal case and is not a structure change.

    Raises:
        StructureChangedError: The response is not that shape. Never a silent empty result, because
            silence plus delete inference is how an archive gets erased .
    """
    if not isinstance(body, dict):
        raise StructureChangedError("listens response is not a JSON object")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise StructureChangedError("listens response has no `payload` object")
    listens = payload.get("listens")
    if not isinstance(listens, list):
        raise StructureChangedError("`payload.listens` is not a list")
    for listen in listens:
        if not isinstance(listen, dict):
            raise StructureChangedError("`payload.listens` holds something that is not a listen")
        _listened_at(listen)
    return listens


def _listened_at(listen: Mapping[str, Any]) -> int:
    """The listen's unix second.

    Raises:
        StructureChangedError: Absent or not an integer. It is the primary key of a listen and the
            paging cursor, so guessing one would corrupt both.
    """
    value = _as_int(listen.get("listened_at"))
    if value is None:
        raise StructureChangedError("a listen carries no integer `listened_at`")
    return value


def _native_id(listen: Mapping[str, Any]) -> str:
    """A stable id for one listen.

    ListenBrainz identifies a listen by ``(user, listened_at, recording_msid)`` and gives it no id
    of its own, so the id is composed — and composed identically on every run, which is what makes
    it usable for idempotent writes. A server that sends no ``recording_msid`` falls back to the
    artist and track names, which is the only other thing that distinguishes two listens in the
    same second.
    """
    stamp = _listened_at(listen)
    msid = _as_text(listen.get("recording_msid"))
    if msid is not None:
        return f"{stamp}:{msid}"
    meta = _as_mapping(listen.get("track_metadata"))
    return f"{stamp}:{_as_text(meta.get('artist_name')) or ''}:{_as_text(meta.get('track_name'))}"


def _work_identifiers(
    listen: Mapping[str, Any],
    additional: Mapping[str, Any],
    mapping: Mapping[str, Any],
) -> list[tuple[str, str, Confidence]]:
    """Every work-level identifier in the payload, deduplicated and sorted .

    Confidence is the real distinction between the two sources: ``additional_info`` is what the
    submitting client **asserted**, while ``mbid_mapping`` is ListenBrainz's own fuzzy match of the
    listen against MusicBrainz — ``matched``, not asserted. ``setdefault`` keeps the asserted value
    when both name the same identifier.

    Returns:
        ``(namespace, value, confidence)`` triples, sorted so the output is order-stable across
        calls: purity has to survive JSON dict ordering .
    """
    seen: dict[tuple[str, str], Confidence] = {}
    sources = (
        (listen, Confidence.ASSERTED),
        (additional, Confidence.ASSERTED),
        (mapping, Confidence.MATCHED),
    )
    for source, confidence in sources:
        for key, value in source.items():
            if key in _ARTIST_ID_KEYS or not key.endswith(_ID_KEY_SUFFIXES):
                continue
            namespace = _ID_NAMESPACES.get(key, key)
            for scalar in _scalars(value):
                seen.setdefault((namespace, scalar), confidence)
    return sorted((namespace, value, seen[namespace, value]) for namespace, value in seen)


def _artist_names(meta: Mapping[str, Any], additional: Mapping[str, Any]) -> tuple[list[str], str]:
    """The credited artists, and the payload key they came from (which becomes ``role_raw``).

    ``additional_info.artist_names`` is the only place the payload splits an artist credit into
    separate artists; ``artist_name`` is a single string that may well read "A & B" and must not be
    split on a guess.
    """
    names = [name for name in _scalars(additional.get("artist_names")) if name]
    if names:
        return names, "artist_names"
    single = _as_text(meta.get("artist_name"))
    return ([single] if single else []), "artist_name"


def _artist_mbids(
    additional: Mapping[str, Any], mapping: Mapping[str, Any]
) -> list[tuple[str, Confidence]]:
    """Artist MBIDs in payload order, asserted ones first, deduplicated across both sources."""
    seen: dict[str, Confidence] = {}
    for source, confidence in ((additional, Confidence.ASSERTED), (mapping, Confidence.MATCHED)):
        for mbid in _scalars(source.get("artist_mbids")):
            seen.setdefault(mbid, confidence)
    return list(seen.items())


def _creator_ids(
    names: list[str], mbids: list[tuple[str, Confidence]]
) -> list[NormalizedCreatorId]:
    """Attach each artist MBID to the artist it belongs to, by payload position.

    Known ceiling: ``artist_name`` is one string for a whole artist credit while ``artist_mbids``
    lists every artist in it, so the counts often disagree. Where they do, every MBID attaches to
    the credited line —  says keep the identifier, and nothing in the payload says which name
    each one belongs to, so inventing a pairing would be worse than a coarse one.
    """
    if not names:
        return []
    pairs = (
        list(zip(names, mbids, strict=True))
        if len(names) == len(mbids)
        else [(names[0], mbid) for mbid in mbids]
    )
    return [
        NormalizedCreatorId(
            creator_name=name,
            namespace="mbid_artist",
            value=value,
            confidence=confidence,
        )
        for name, (value, confidence) in pairs
    ]


def _track_number(additional: Mapping[str, Any]) -> int | None:
    """The track number as an integer, or ``None``.

    Tag-derived, so it arrives as ``5``, ``"5"`` or ``"5/12"``. Anything else is left as ``None``
    rather than coerced: a wrong sequence number reorders an album silently.
    """
    value = additional.get("tracknumber", additional.get("track_number"))
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        head = value.split("/")[0].strip()
        if head.isdigit():
            return int(head)
    return None


def _as_mapping(value: object) -> Mapping[str, Any]:
    """A nested object, or an empty mapping — absence is normal for every optional block here."""
    return value if isinstance(value, dict) else {}


def _as_text(value: object) -> str | None:
    """A non-empty string, or ``None``. An empty string in a payload means "not stated"."""
    return value.strip() or None if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    """An integer, or ``None``. ``bool`` is excluded: ``True`` is not a timestamp."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _scalars(value: object) -> Iterator[str]:
    """Identifier-shaped scalars in a payload value, flattening a list of them.

    Yields strings, because an identifier is a string once stored — ``caa_id: 12345`` and
    ``caa_id: "12345"`` are the same identifier and must not become two.
    """
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
    elif isinstance(value, int) and not isinstance(value, bool):
        yield str(value)
    elif isinstance(value, list):
        for item in value:
            yield from _scalars(item)


provider: Provider = ListenBrainzProvider()
"""The registration point (aggregato/providers/registry.py convention 2).

Annotated as ``Provider`` so the type checker proves this class satisfies the protocol here, at the
one place that would otherwise be a runtime surprise. Constructing it does nothing — the convention
requires that.
"""
