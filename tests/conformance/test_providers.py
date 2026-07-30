"""The provider conformance suite — contract §5, groups 1 to 9.

Every bundled provider is registered in ``conftest.py`` and must pass all of this; it is also what a
third-party provider runs to prove compliance (FR-046). Each test names the group it implements, and
cites the requirement the group exists for — a group whose reason is unclear gets weakened by the
first person it inconveniences.

Nothing here touches the network or reads a credential. The autouse socket blocker in
tests/conftest.py makes an attempt a hard failure, which is the contract's last line: a network call
in a conformance test is a failure, not a slow test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

from aggregato.domain.enums import (
    Acquisition,
    Capability,
    FetchMode,
    MediaType,
    Role,
)
from aggregato.domain.models import Checkpoint, CheckResult, RawRecord
from aggregato.domain.subject_ref import SUBJECT_REF_KEYS, validate_subject_ref
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.errors import StructureChangedError
from aggregato.providers.registry import discover_providers
from tests.conformance.conftest import (
    REGISTERED,
    Registration,
    build_ctx,
    fetch_all,
    only_records,
    scrapes,
)
from tests.conftest import FrozenClock

_SLUG = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")

_SECRET_NAME = re.compile(r"secret|token|password|passwd|cookie|api_key|apikey|session")
"""Field names whose value is a credential, so group 8 can require ``writeOnly`` without every
provider having to opt in to being checked."""

_ID_KEY = re.compile(
    r"^(ids|identifiers|external_ids|id|guid|uuid|isbn|isbn10|isbn13|asin|mbid|ean|gtin)$"
    r"|_id(s)?$|_?isbn(10|13)?$",
)
"""Keys whose contents are identifiers, for the group 5 payload walk. See the note there."""

_SOLVERS = (
    "twocaptcha",
    "anticaptchaofficial",
    "capsolver",
    "undetected_chromedriver",
    "selenium",
    "playwright",
    "seleniumbase",
    "cloudscraper",
    "flaresolverr",
)
"""Packages that exist to defeat anti-bot measures. FR-044 is a hard line, not a default."""


# --- group 1: identity and scheduling declarations -----------------------------------------------


def test_group1_identity_declarations(provider: Provider) -> None:
    """Group 1: ``id`` is a stable slug, ``schema_version`` >= 1, ``default_poll_interval`` is set.

    ``id`` is the join key for every row the provider has ever written and must survive a migration
    to a better acquisition surface (FR-047), so it is restricted to a slug: anything needing
    quoting, casing rules or normalization somewhere downstream is not stable. ``schema_version``
    drives normalization replay (FR-002) and ``default_poll_interval`` must come from the platform's
    own rate limits rather than a global default (FR-018), so a zero here is a missing decision.
    """
    assert _SLUG.match(provider.id), f"{provider.id!r} is not a stable slug"
    assert provider.name
    assert provider.schema_version >= 1
    assert provider.default_poll_interval.total_seconds() > 0


def test_every_bundled_provider_is_registered() -> None:
    """Not one of the nine: the gate only gates what is registered (contract §5).

    Bundled providers ship with the core (FR-041), so shipping one that never runs through this
    suite has to be impossible rather than discouraged.
    """
    registered = {r.provider_id for r in REGISTERED}
    bundled = {info.id for info in discover_providers()}
    assert bundled <= registered, (
        f"bundled providers missing from the conformance gate: {sorted(bundled - registered)}; "
        "add a Registration in tests/conformance/conftest.py"
    )


# --- group 2: declarations match observed behaviour ----------------------------------------------


async def test_group2_declarations_match_observed_behaviour(
    provider: Provider,
    registration: Registration,
    ctx: ProviderContext,
    raw_records: list[RawRecord],
    records_path: Path,
) -> None:
    """Group 2: declared ``media_types``, ``capabilities`` and ``acquisition`` match what the
    fixtures actually produce.

    A declaration nobody checks is a wish. The UI, the scheduler and the settings form all read
    these, so ``has_ratings`` on a provider that never emits a rating means an operator debugging an
    empty ratings view starts in the wrong place.
    """
    batches = [provider.normalize(raw) for raw in raw_records]

    emitted = {batch.work.media_type for batch in batches}
    assert emitted <= provider.media_types, (
        f"undeclared media types emitted: {sorted(str(m) for m in emitted - provider.media_types)}"
    )

    opinions = [opinion for batch in batches for opinion in batch.opinions]
    credits = [credit for batch in batches for credit in batch.credits]

    if Capability.HAS_RATINGS in provider.capabilities:
        assert any(o.rating_raw is not None for o in opinions), (
            "declares has_ratings but no fixture record produces an opinion carrying a rating"
        )
    if Capability.HAS_REVIEWS in provider.capabilities:
        assert any(o.review_text for o in opinions), (
            "declares has_reviews but no fixture record produces a review"
        )
    if Capability.HAS_CREDITS in provider.capabilities:
        assert credits, "declares has_credits but no fixture record produces credits"

    # A rating_raw citing an undeclared scale is uninterpretable (FR-003, contract §3): 4 of 5 and
    # 4 of 10 are not the same opinion, and the declaration is the only thing that says which.
    declared_scales = {scale.id for scale in provider.rating_scales}
    cited = {o.rating_scale_id for o in opinions if o.rating_scale_id is not None}
    assert cited <= declared_scales, f"opinions cite undeclared rating scales: {sorted(cited)}"

    # file_import / export is a claim about a code path, so exercise it: import mode must read
    # ctx.import_path and produce the same records rather than quietly falling back (contract §1).
    if (
        Capability.FILE_IMPORT in provider.capabilities
        or provider.acquisition is Acquisition.EXPORT
    ):
        import_ctx = build_ctx(registration, records_path, import_path=records_path)
        imported = only_records(await fetch_all(provider, import_ctx, None, FetchMode.IMPORT))
        assert [r.native_id for r in imported] == [r.native_id for r in raw_records]

    # Scraping is the lowest surface (FR-042) and carries the politeness and FR-044 obligations, so
    # the two ways of declaring it must agree — a scraper that forgets Capability.SCRAPES would not
    # get the one-in-flight-request-per-host floor the host applies on that basis (contract §2).
    assert (Capability.SCRAPES in provider.capabilities) == (
        provider.acquisition is Acquisition.SCRAPE
    ), "Capability.SCRAPES and Acquisition.SCRAPE must be declared together or not at all"


# --- group 3: normalize is pure ------------------------------------------------------------------


def test_group3_normalize_is_pure(
    provider: Provider,
    raw_records: list[RawRecord],
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Group 3: two calls on the same record produce identical output; sockets blocked; clock
    frozen; no writes.

    This is what makes replay (FR-002) and credential-free offline tests (FR-036) possible: replay
    re-runs ``normalize`` over payloads stored months ago, so a value that came from the clock, the
    network or a random source would silently rewrite history on the second pass.

    Serialized output is compared, not object identity — identity would pass trivially and equality
    on Pydantic models would miss ordering differences that a JSON line does carry.
    """
    # The frozen clock is requested rather than merely available so that a provider reading a clock
    # the host injected sees a time that cannot move between the two calls; the socket blocker is
    # autouse (tests/conftest.py). Both together turn "pure" into something observable.
    assert clock.now() == clock.now()

    # Writes: run from an empty directory and require it to stay empty. Ceiling — this catches a
    # relative-path write, not an absolute one. An absolute-path write is caught by the child
    # process having no writable state of its own (research.md R2) rather than here.
    monkeypatch.chdir(tmp_path)

    for raw in raw_records:
        first = provider.normalize(raw)
        second = provider.normalize(raw)
        assert first.model_dump_json() == second.model_dump_json(), (
            f"normalize is not pure for record {raw.native_id!r}"
        )

    assert list(tmp_path.iterdir()) == [], "normalize wrote to the working directory"


# --- group 4: closed vocabularies ----------------------------------------------------------------


def test_group4_closed_vocabulary(provider: Provider, raw_records: list[RawRecord]) -> None:
    """Group 4: every ``media_type``, ``role`` and ``subject_ref`` key produced is in the closed
    vocabulary (FR-008).

    Asserted on the **serialized** form, because that is what crosses the process boundary and what
    the parent-side ingest boundary validates: a provider that bypassed the Pydantic models and
    emitted a JSON line by hand would still be caught here. ``subject_ref`` goes through
    ``validate_subject_ref``, the same function the ingest boundary uses, so the suite cannot drift
    into a second, more permissive rule.
    """
    media_values = {m.value for m in MediaType}
    role_values = {r.value for r in Role}

    for raw in raw_records:
        wire: dict[str, Any] = provider.normalize(raw).model_dump(mode="json")
        assert wire["work"]["media_type"] in media_values

        for credit in wire["credits"]:
            assert credit["role"] in role_values, f"invented role {credit['role']!r}"
            # role_raw is the platform's own word and is deliberately open (FR-016), but it must be
            # present even when `role` maps cleanly — that is where the distinction survives.
            assert credit["role_raw"]

        for holder in [*wire["entries"], *wire["opinions"]]:
            ref = holder.get("subject_ref")
            validated = validate_subject_ref(ref)
            if validated is not None:
                assert set(validated.as_dict()) <= set(SUBJECT_REF_KEYS)


# --- group 5: every identifier in the fixture reaches the output ---------------------------------


def _identifiers(node: object, *, under_id_key: bool = False) -> set[str]:
    """Identifier-looking scalars in a raw payload.

    The rule: a scalar counts when its own key looks like an identifier key, or when it sits
    anywhere beneath one (``ids: {imdb: ...}`` — the namespace names cannot be enumerated in
    advance, which is the whole point of FR-009).
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found |= _identifiers(value, under_id_key=under_id_key or bool(_ID_KEY.search(key)))
    elif isinstance(node, list):
        for item in node:
            found |= _identifiers(item, under_id_key=under_id_key)
    elif under_id_key and isinstance(node, str | int) and not isinstance(node, bool):
        found.add(str(node))
    return found


def test_group5_every_identifier_reaches_the_output(
    provider: Provider, raw_records: list[RawRecord]
) -> None:
    """Group 5: every identifier visible in the fixture appears in the output (FR-009).

    Under the no-enrichment rule this is the single largest lever on match quality, so the test
    walks the raw payload and demands each identifier-looking value come out in ``external_ids``,
    ``creator_external_ids``, or as the record's own native id.

    Known ceilings, stated rather than hidden — a weak version of this test is worse than none:

    * Identification is by **key name** (``_ID_KEY``). An identifier stored under a key that looks
      like nothing — ``"slug"``, ``"permalink"``, a bare numeric ``"ref"`` — is not caught. Widening
      the pattern is the upgrade path; loosening to "every scalar" would flag titles and years.
    * The walk starts at ``RawRecord.payload``, so an identifier the provider drops while *building*
      the payload is invisible here. Contract §3 requires the payload be retained verbatim, and
      tests/fixtures/README.md forbids trimming a recording, which is what keeps that honest.
    * Values are compared as strings, so ``"9001"`` and ``9001`` are one identifier. Namespaces are
      not checked: this asserts the value survived, not that it was filed correctly.
    """
    for raw in raw_records:
        batch = provider.normalize(raw)
        expected = _identifiers(raw.payload)
        emitted = {external.value for external in batch.external_ids}
        emitted |= {creator.value for creator in batch.creator_external_ids}
        # The platform's id for the record itself is an entry native_id, not an external id.
        emitted |= {raw.native_id}
        emitted |= {entry.native_id for entry in batch.entries if entry.native_id}

        missing = {value for value in expected if value not in emitted}
        assert not missing, (
            f"record {raw.native_id!r} drops identifiers present in its payload: {sorted(missing)} "
            "(FR-009: every identifier is extracted, including ones Aggregato has no use for)"
        )


# --- group 6: cursors round-trip -----------------------------------------------------------------


async def test_group6_cursor_round_trip(
    provider: Provider, ctx: ProviderContext, raw_records: list[RawRecord]
) -> None:
    """Group 6: fetch to a checkpoint, resume from its cursor, and get no duplicate and no gap.

    A resumed run is the normal case, not the exception: FR-020 exists because a long backfill dies
    partway. A duplicate means the operator's history grows on every crash; a gap means it silently
    loses records, which is the failure FR-024 and FR-026 are about.
    """
    full = [record.native_id for record in raw_records]

    consumed: list[str] = []
    resume_from = None
    async for item in provider.fetch(ctx, None, FetchMode.FULL):
        if isinstance(item, Checkpoint):
            resume_from = item.cursor
            break
        consumed.append(item.native_id)

    if resume_from is None:
        pytest.skip(
            "yields no Checkpoint: its pagination cannot support mid-fetch resumption, so it "
            "accepts full resyncs (contract §1)"
        )

    resumed = [
        r.native_id
        for r in only_records(await fetch_all(provider, ctx, resume_from, FetchMode.FULL))
    ]

    assert consumed + resumed == full, (
        "resuming from a checkpoint did not reproduce the uninterrupted fetch: "
        f"{consumed + resumed} != {full}"
    )
    assert len(set(full)) == len(full), "the uninterrupted fetch itself yields duplicate native ids"


# --- group 7: check reports, for valid and invalid credentials ------------------------------------


async def test_group7_check_reports_both_outcomes(
    provider: Provider,
    registration: Registration,
    ctx: ProviderContext,
    provider_fixtures: Path,
) -> None:
    """Group 7: ``check`` returns a ``CheckResult`` for a valid and for an invalid credential set.

    ``check`` is a diagnostic the operator asked for, so its failure is the answer, not an error
    (contract §1): raising here would surface in the UI as a crash instead of as the sentence that
    tells the operator what to fix. The invalid case must also name an ``error_class``, because that
    is what the host records on ``providers.last_error``.
    """
    ok = await provider.check(ctx)
    assert isinstance(ok, CheckResult)
    assert ok.ok, f"check failed against the valid fixture: {ok.detail}"

    invalid_path = provider_fixtures / registration.invalid
    assert invalid_path.exists(), (
        f"registered invalid-credentials fixture is missing: {invalid_path}"
    )
    bad = await provider.check(build_ctx(registration, invalid_path))
    assert isinstance(bad, CheckResult)
    assert not bad.ok, "check reported ok against the invalid fixture"
    assert bad.error_class is not None, "a failed check must name the error_class (contract §4)"
    assert bad.detail, "a failed check must say what an operator should do about it"


# --- group 8: the settings form can render config_model -------------------------------------------


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow one ``$ref``: an optional enum field is a ``$ref`` nested inside ``anyOf``."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        resolved: dict[str, Any] = defs.get(ref.rsplit("/", 1)[-1], {})
        return resolved
    return schema


def test_group8_config_schema_is_renderable(provider: Provider) -> None:
    """Group 8: ``config_model`` produces a JSON Schema the settings form can render (FR-039).

    The form renderer reads the JSON Schema directly (research.md R11), so the schema is the form:
    a nested object has no widget, a secret without ``writeOnly`` gets echoed back into the page,
    and a field without a description has no label but its own variable name.
    """
    schema = provider.config_model.model_json_schema()
    defs: dict[str, Any] = schema.get("$defs", {})
    properties: dict[str, Any] = schema.get("properties", {})

    for name, raw_field in properties.items():
        variants = [_resolve(v, defs) for v in raw_field.get("anyOf", [raw_field])]
        for variant in variants:
            kind = variant.get("type")
            # Enums resolve to a scalar with `enum`, which the form renders as a select. An object
            # or an array does not have a widget, and inventing one is out of scope for M1.
            assert kind not in {"object", "array"}, (
                f"{provider.id}.config_model.{name} is not a flat field ({kind}): the settings "
                "form renders scalars, enums and secrets only"
            )

        assert raw_field.get("description"), (
            f"{provider.id}.config_model.{name} has no description; the description is the label "
            "the operator reads (FR-039)"
        )

        if _SECRET_NAME.search(name):
            assert raw_field.get("writeOnly") is True, (
                f"{provider.id}.config_model.{name} looks like a credential but is not marked "
                "writeOnly; secrets are never returned by the API"
            )


# --- group 9: scraping providers additionally -----------------------------------------------------


async def test_group9_scraping_obligations(
    provider: Provider, registration: Registration, provider_fixtures: Path
) -> None:
    """Group 9: scrapers only — recorded HTML fixtures, ``StructureChangedError`` on a changed
    structure, and no solver in the import graph.

    Skips for every non-scraping provider, and activates automatically on either declaration, so a
    provider becomes subject to it by declaring ``Capability.SCRAPES`` or ``Acquisition.SCRAPE``
    rather than by anyone remembering to add it here. FR-044 is the reason the last assertion
    exists: a solver dependency is circumvention regardless of whether it is called today.
    """
    if not scrapes(provider):
        pytest.skip("not a scraping provider: group 9 does not apply (contract §5)")

    html = list(provider_fixtures.glob("*.html"))  # noqa: ASYNC240 - local fixture dir, no I/O wait
    assert html, f"{provider.id} scrapes but ships no recorded HTML fixtures in {provider_fixtures}"

    changed = registration.structure_changed
    assert changed, (
        f"{provider.id} scrapes, so its Registration must name a structure_changed fixture: a page "
        "whose shape moved must raise rather than return empty (FR-024, FR-026)"
    )
    changed_path = provider_fixtures / changed
    assert changed_path.exists(), f"missing structure-changed fixture: {changed_path}"

    with pytest.raises(StructureChangedError):
        # Either surface may notice first — fetch while parsing the page, or normalize while
        # reading a stored payload. What is forbidden is a silent empty result from either.
        records = only_records(
            await fetch_all(provider, build_ctx(registration, changed_path), None, FetchMode.FULL)
        )
        for raw in records:
            provider.normalize(raw)
        assert records, "a structurally-changed page returned empty instead of raising"

    # Reachability, not spelling: the provider package has been imported by now, so a solver it
    # depends on at module scope is in sys.modules. Ceiling — a solver imported lazily inside a
    # function body is not caught here; the review checklist (contract §6) is the backstop.
    loaded = {name.split(".")[0] for name in sys.modules}
    assert not loaded.intersection(_SOLVERS), (
        f"{provider.id}'s import graph reaches an anti-bot solver: "
        f"{sorted(loaded.intersection(_SOLVERS))} (FR-044 is a hard line, not a default)"
    )
