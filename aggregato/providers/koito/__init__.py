"""The ``koito`` provider: listens from a self-hosted Koito server (https://github.com/gabehf/Koito).

``acquisition`` is ``api`` and no higher surface exists (FR-042, contract §6). Koito is
*ListenBrainz-compatible for submission only* — ``/apis/listenbrainz/1`` exposes ``submit-listens``
and ``validate-token`` and nothing that reads a history — so the ``listenbrainz`` provider aimed
at a Koito base URL would read nothing. The history lives on Koito's own web API,
``GET /apis/web/v1/listens``, which is why this is a separate provider rather than another
``base_url``. ``GET /apis/web/v1/export`` streams the whole archive in one unresumable response, so
it is a lower surface, not a higher one.

Paging walks **backwards** in time by the ``to`` query parameter, which Koito applies as an
inclusive ``listened_at BETWEEN from AND to``. A ``Checkpoint`` after each page carries the next
boundary, so a run that dies resumes there (FR-020). ``incremental`` moves ``from`` up to one second
past the newest listen already seen.

Two Koito details drive the request shape and are not obvious from the endpoint:

* ``from`` must always be sent. Koito's timeframe resolution only honours ``to`` when ``from`` is
  non-zero; ``to`` alone resolves to the empty range ``BETWEEN 0 AND 0`` and answers with nothing,
  which would look exactly like the end of the history.
* Authorization is ``Token {api_key}``, the same header ListenBrainz uses. With Koito's login gate
  disabled the endpoint is public and the header is ignored, so it is always sent.

A listen is a **track** and nothing else: the payload names no release, and Koito's own identifiers
for the track and its artists are integers scoped to that installation, filed under ``koito_track``
and ``koito_artist`` (FR-009).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

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

PAGE_SIZE = 100
"""Listens per request. Koito's own default, and its cap is 500 — a request over the cap is silently
reduced to 100 rather than rejected, so asking for more buys nothing. A smaller page also means a
crash loses less work, because the checkpoint boundary is the page (FR-020)."""

EPOCH_FLOOR = 1
"""The ``from`` value for a walk with no lower bound. Not ``0``: Koito treats a zero ``from`` as
"unset" and then ignores ``to`` as well, which turns a bounded page request into an empty answer."""


class KoitoConfig(BaseModel):
    """Settings for the Koito provider — and the settings form the UI renders (FR-039)."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(
        min_length=1,
        description="Base URL of the Koito server: scheme, host and optional port, for example "
        "http://koito.lan:4110. Stop before /apis — the API path is appended.",
    )
    api_key: SecretStr = Field(
        description="Koito API key, from Settings -> API keys on the server. Use a ${ENV_VAR} "
        "reference to keep it out of the config file. Required whenever Koito's login gate is on.",
    )

    @field_validator("base_url")
    @classmethod
    def _absolute_http_url(cls, value: str) -> str:
        """Require an absolute http(s) URL and strip a trailing slash, so joining is unambiguous."""
        normalized = value.strip().rstrip("/")
        parts = urlsplit(normalized)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(
                f"base_url must be an absolute http:// or https:// URL, got {value!r} — a host "
                "alone is not enough, because the port is part of it"
            )
        if parts.query or parts.fragment:
            raise ValueError("base_url must not carry a query string or fragment")
        if "/apis/" in f"{parts.path}/":
            raise ValueError(
                "base_url must stop before the API path; give the server root, for example "
                "http://koito.lan:4110"
            )
        return normalized


class KoitoProvider:
    """A provider that reads a user's listens from a self-hosted Koito server."""

    id: str = "koito"
    name: str = "Koito"
    # RUF012 wants these immutable, but contract/provider-plugin.md §1 publishes them as `set` and
    # `list`. One instance per provider class, so the shared-default risk is moot.
    media_types: set[MediaType] = {MediaType.TRACK}  # noqa: RUF012
    # No `has_ratings`, no `has_reviews`: a Koito listen carries neither. No `reports_deletes`: the
    # endpoint never mentions a deleted listen, and a listen that vanishes from a range is
    # indistinguishable from one an operator merged into another track.
    capabilities: set[Capability] = {  # noqa: RUF012
        Capability.POLL,
        Capability.BACKFILL,
        Capability.HAS_CREDITS,
    }
    acquisition: Acquisition = Acquisition.API
    # Annotated as the protocol declares them, not inferred: a Protocol's mutable attributes are
    # invariant, so `type[KoitoConfig]` would not satisfy `type[BaseModel]`.
    config_model: type[BaseModel] = KoitoConfig
    rating_scales: list[RatingScale] = []  # noqa: RUF012
    schema_version: int = 1
    # Koito is the operator's own server: its only rate limit is ten requests a minute on the login
    # route, which this provider never calls. So the interval is a courtesy to a small self-hosted
    # box rather than a quota division (FR-018) — five minutes keeps a listening session near-live
    # at under 300 requests a day against a host the operator owns.
    default_poll_interval: timedelta = timedelta(minutes=5)

    async def fetch(
        self, ctx: ProviderContext, cursor: Cursor | None, mode: FetchMode
    ) -> AsyncIterator[RawRecord | Checkpoint]:
        """Yield listens, page by page backwards in time, checkpointing after each page.

        Args:
            ctx: Host context. ``ctx.config`` must be a ``KoitoConfig``; ``ctx.http`` is the only
                client used (FR-043).
            cursor: ``{"newest_ts": int | None, "to_ts": int | None}`` from a previous checkpoint.
                ``to_ts`` is the inclusive upper bound the next page should start at, one second
                below the oldest listen already emitted. ``newest_ts`` is the high-water mark an
                ``incremental`` run raises ``from`` to.
            mode: ``incremental`` walks only listens newer than ``newest_ts``; anything else walks
                the whole history backwards. Only ``poll`` and ``backfill`` are declared, so
                ``import`` is never requested.

        Yields:
            ``RawRecord`` per listen, and a ``Checkpoint`` after each page — including a final one
            with ``to_ts`` cleared, which is what tells the next ``incremental`` run that the walk
            finished and it may start again from the top.

            Known ceiling: the boundary between pages is a whole second, because ``to`` is Koito's
            only ordering filter and it has one-second resolution. A page whose oldest second holds
            more listens than ``PAGE_SIZE`` would lose the remainder of that second — that needs 100
            listens stamped in the same second, which a listening history does not produce, and the
            upgrade path is Koito growing a keyset cursor on ``(listened_at, track_id)`` as its
            export endpoint already has internally.

        Raises:
            ProviderError: ``ctx.config`` is not a ``KoitoConfig``, or the server answered a status
                this provider cannot interpret.
            AuthError: HTTP 401 — the API key was rejected. No retry.
            RateLimited: HTTP 429.
            StructureChangedError: The response shape is wrong, or the server did not page backwards
                and a walk would never terminate.
        """
        config = _config(ctx)
        state = cursor.state if cursor is not None else {}
        newest_ts = _as_int(state.get("newest_ts"))
        to_ts = _as_int(state.get("to_ts"))
        # Inclusive lower bound, so the +1 is what keeps an incremental run from re-reading the
        # listen it stopped on. The host's writes are idempotent, but a page spent on a record it
        # already has is a page not spent on new ones.
        from_ts = newest_ts + 1 if mode is FetchMode.INCREMENTAL and newest_ts else EPOCH_FLOOR

        while True:
            listens = _listens(await self._page(ctx, config, from_ts=from_ts, to_ts=to_ts))
            if not listens:
                # The walk reached the end of what the server has. Clearing `to_ts` is the whole
                # difference between "resume mid-history" and "start from the top next time".
                yield Checkpoint(cursor=Cursor(state={"newest_ts": newest_ts, "to_ts": None}))
                return

            for listen in listens:
                yield RawRecord(native_id=_native_id(listen), payload=listen)

            stamps = [_listened_at(listen) for listen in listens]
            oldest = min(stamps)
            # `to` is inclusive, so a compliant server answers with nothing newer than it. A page
            # that does not move the boundary would otherwise loop forever, which is a structural
            # failure worth naming rather than a run to leave spinning.
            if to_ts is not None and oldest > to_ts:
                raise StructureChangedError(
                    f"{listens_url(config)} returned listens newer than to={to_ts}: the server is "
                    "not honouring the timeframe filter, so a walk would never terminate"
                )
            newest_ts = max([*stamps, newest_ts]) if newest_ts is not None else max(stamps)
            to_ts = oldest - 1
            # After the page, not before: the cursor names where to continue, so a crash here
            # re-emits nothing that was already ingested (FR-020).
            yield Checkpoint(cursor=Cursor(state={"newest_ts": newest_ts, "to_ts": to_ts}))

    def normalize(self, raw: RawRecord) -> NormalizedBatch:
        """Map one listen onto the host vocabulary. Pure: no clock, no I/O, no randomness.

        ``time`` is an RFC 3339 instant *in the payload*. Replay re-runs this over old payloads, and
        a clock read would rewrite history on the second pass (FR-002).

        Args:
            raw: A listen as ``fetch`` yielded it, or as replay read it back.

        Returns:
            A ``NormalizedBatch`` — identical on every call for the same input. Every identifier in
            the payload is extracted (FR-009) and ``role_raw`` keeps Koito's own field name verbatim
            (FR-016).

        Raises:
            StructureChangedError: The stored payload is not a listen — no parsable ``time``, or no
                ``track.title``.
            ValueError: The payload is a listen but a value does not validate.
        """
        listen = raw.payload
        track = _as_mapping(listen.get("track"))
        title = _as_text(track.get("title"))
        if title is None:
            raise StructureChangedError(
                f"listen {raw.native_id!r} has no track.title: a listen without a track is not a "
                "listen"
            )
        artists = [
            artist for artist in _as_list(track.get("artists")) if _as_text(artist.get("name"))
        ]

        return NormalizedBatch(
            work=NormalizedWork(media_type=MediaType.TRACK, title=title),
            entries=[
                NormalizedEntry(
                    kind=EntryKind.LISTEN,
                    # `_listened_at` returns Koito's own unix second, converted here rather than
                    # parsed twice, so the entry and the paging cursor cannot disagree.
                    logged_at=datetime.fromtimestamp(_listened_at(listen), tz=UTC),
                    # `exact`, and honestly so: Koito recorded the instant the scrobble was
                    # submitted for. Nothing here was widened or narrowed (FR-004).
                    logged_precision=LoggedPrecision.EXACT,
                    native_id=raw.native_id,
                )
            ],
            credits=[
                NormalizedCredit(
                    creator_name=_require_text(artist.get("name")),
                    # Koito does not say whether an artist is a person or a band, and guessing from
                    # the name is exactly the kind of invention FR-008 forbids.
                    creator_kind=CreatorKind.UNKNOWN,
                    role=Role.PERFORMER,
                    # Koito's own term for this field, verbatim (FR-016): it has no word for the
                    # relationship beyond the key it files the names under.
                    role_raw="artists",
                    position=position,
                )
                for position, artist in enumerate(artists)
            ],
            external_ids=_ids(track, "koito_track", "mbid_recording"),
            creator_external_ids=[
                NormalizedCreatorId(
                    creator_name=_require_text(artist.get("name")),
                    namespace=namespace,
                    value=value,
                    confidence=confidence,
                )
                for artist in artists
                for namespace, value, confidence in _ids_raw(artist, "koito_artist", "mbid_artist")
            ],
        )

    async def check(self, ctx: ProviderContext) -> CheckResult:
        """Verify the API key against the configured server, ingesting nothing.

        Args:
            ctx: Host context; ``ctx.config`` must be a ``KoitoConfig``.

        Returns:
            ``ok=True`` when the server answers a readable listens page, otherwise ``ok=False``
            carrying the ``ErrorClass`` the host would have classified the equivalent raised failure
            as — ``auth`` for a rejected key (HTTP 401).
        """
        try:
            config = _config(ctx)
            listens = _listens(await self._page(ctx, config, from_ts=EPOCH_FLOOR, to_ts=None))
        except ProviderError as exc:
            return CheckResult(ok=False, error_class=exc.error_class, detail=str(exc))
        except Exception as exc:
            # Deliberately broad, and it cannot be narrower here: naming `httpx.TransportError`
            # would mean importing httpx, which the FR-043 import contract forbids in a provider
            # package. `check` is a diagnostic the operator asked for (contract §1), so a DNS
            # failure has to come back as the answer rather than as a crash in the settings UI.
            return CheckResult(
                ok=False, error_class=ErrorClass.TRANSPORT, detail=f"{type(exc).__name__}: {exc}"
            )
        return CheckResult(
            ok=True,
            detail=f"{config.base_url} answered: {len(listens)} listen(s) in the first page",
        )

    async def _page(
        self,
        ctx: ProviderContext,
        config: KoitoConfig,
        *,
        from_ts: int,
        to_ts: int | None,
    ) -> object:
        """Fetch one listens page and classify its status (contract §4).

        Raises:
            AuthError: 401. No retry — retrying a rejected key risks locking the operator out.
            RateLimited: 429. ``retry_after`` is left to the host: ``PoliteClient`` already honours
                ``Retry-After``, and a second reader of that header would be a second policy.
            ProviderError: Any other non-200, including the 400 Koito answers for query parameters
                it cannot parse. ``internal`` sends it to the cross-run ladder, which is right for a
                misconfigured base URL's 404 as much as for a 500.
        """
        url = listens_url(config)
        params: dict[str, int] = {"limit": PAGE_SIZE, "from": from_ts}
        if to_ts is not None:
            params["to"] = to_ts

        response = await ctx.http.get(url, params=params, headers=_auth_headers(config))
        if response.status_code == 401:
            raise AuthError(f"{url} rejected the configured API key (HTTP 401)")
        if response.status_code == 429:
            raise RateLimited(f"{url} is rate-limiting (HTTP 429)")
        if response.status_code != 200:
            raise ProviderError(f"{url} answered HTTP {response.status_code}")
        return response.json()


def _config(ctx: ProviderContext) -> KoitoConfig:
    """Narrow ``ctx.config`` at the boundary rather than assuming the host got it right."""
    if not isinstance(ctx.config, KoitoConfig):
        raise ProviderError(f"koito provider received a {type(ctx.config).__name__} config")
    return ctx.config


def listens_url(config: KoitoConfig) -> str:
    """Build the listens endpoint URL for the configured server.

    Public because it is the one thing an operator debugging a base URL needs to be able to predict,
    and the one thing a test can assert exactly.
    """
    return f"{config.base_url}/apis/web/v1/listens"


def _auth_headers(config: KoitoConfig) -> dict[str, str]:
    """Koito's auth header. Always sent: it is ignored when the login gate is off."""
    key = config.api_key.get_secret_value()
    return {"Authorization": f"Token {key}"} if key else {}


def _listens(body: object) -> list[dict[str, Any]]:
    """The ``items`` list of Koito's paginated response, or a named structural failure.

    Only the envelope is checked here — a JSON object with an ``items`` list of objects, each with
    a parsable ``time``. Koito serializes an empty page as ``"items": []``.

    Raises:
        StructureChangedError: The response is not that shape. Never a silent empty result, because
            silence plus delete inference is how an archive gets erased (FR-024, FR-026).
    """
    if not isinstance(body, dict):
        raise StructureChangedError("listens response is not a JSON object")
    items = body.get("items")
    if not isinstance(items, list):
        raise StructureChangedError("listens response has no `items` list")
    for listen in items:
        if not isinstance(listen, dict):
            raise StructureChangedError("`items` holds something that is not a listen")
        _listened_at(listen)
    return items


def _listened_at(listen: Mapping[str, Any]) -> int:
    """The listen's unix second, from Koito's RFC 3339 ``time``.

    Truncated to the second deliberately: that is the resolution Koito stores, the resolution its
    ``from``/``to`` filter uses, and therefore the resolution the cursor has to speak.

    Raises:
        StructureChangedError: Absent or not an RFC 3339 instant. It is half the identity of a
            listen and the whole of the paging cursor, so guessing one would corrupt both.
    """
    value = listen.get("time")
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            moment = None
        if moment is not None:
            # A Koito response is UTC-stamped, but a naive value would make the unix second depend
            # on the reader's timezone — which is a clock read by another name (FR-002).
            return int(moment.replace(tzinfo=moment.tzinfo or UTC).timestamp())
    raise StructureChangedError(f"a listen carries no RFC 3339 `time`: {value!r}")


def _native_id(listen: Mapping[str, Any]) -> str:
    """A stable id for one listen.

    Koito identifies a listen by ``(user, track_id, listened_at)`` and gives it no id of its own, so
    the id is composed — and composed identically on every run, which is what makes it usable for
    idempotent writes. A track id is scoped to the installation, which is fine: so is the provider
    configuration that points at it.
    """
    track_id = _as_int(_as_mapping(listen.get("track")).get("id"))
    return f"{_listened_at(listen)}:{'' if track_id is None else track_id}"


def _ids(
    node: Mapping[str, Any], id_namespace: str, mbid_namespace: str
) -> list[NormalizedExternalId]:
    return [
        NormalizedExternalId(namespace=namespace, value=value, confidence=confidence)
        for namespace, value, confidence in _ids_raw(node, id_namespace, mbid_namespace)
    ]


def _ids_raw(
    node: Mapping[str, Any], id_namespace: str, mbid_namespace: str
) -> list[tuple[str, str, Confidence]]:
    """Every identifier Koito states about a track or an artist (FR-009).

    ``id`` is always present. ``musicbrainz_id`` is not — Koito omits it from the trimmed track and
    artist objects a listen carries today, and states it on the full ones. It is read here anyway,
    because an identifier that appears in a later Koito release should be stored the day it appears
    rather than the day someone notices.
    """
    found: list[tuple[str, str, Confidence]] = []
    native = _as_int(node.get("id"))
    if native is not None:
        found.append((id_namespace, str(native), Confidence.ASSERTED))
    mbid = _as_text(node.get("musicbrainz_id"))
    if mbid is not None:
        found.append((mbid_namespace, mbid, Confidence.ASSERTED))
    return found


def _as_mapping(value: object) -> Mapping[str, Any]:
    """A nested object, or an empty mapping — absence is checked where it matters, not here."""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Mapping[str, Any]]:
    """The object members of a list. Koito sends ``null`` for a track with no artists."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _as_text(value: object) -> str | None:
    """A non-empty string, or ``None``. An empty string in a payload means "not stated"."""
    return value.strip() or None if isinstance(value, str) else None


def _require_text(value: object) -> str:
    """``_as_text`` where the caller has already filtered out the absent case."""
    text = _as_text(value)
    if text is None:  # pragma: no cover - the comprehensions above filter this out
        raise StructureChangedError("expected a non-empty string")
    return text


def _as_int(value: object) -> int | None:
    """An integer, or ``None``. ``bool`` is excluded: ``True`` is not a track id."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


provider: Provider = KoitoProvider()
"""The registration point (aggregato/providers/registry.py convention 2).

Annotated as ``Provider`` so the type checker proves this class satisfies the protocol here, at the
one place that would otherwise be a runtime surprise. Constructing it does nothing — the convention
requires that.
"""
