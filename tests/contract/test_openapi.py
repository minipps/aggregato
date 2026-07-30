"""The served schema against contracts/openapi.yaml (T045).

``contracts/openapi.yaml`` is the contract the web UI consumes and the only one (FR-031), so it is
normative rather than documentation. This harness compares what the app actually serves against it
and fails on drift in either direction:

* a path or method in the contract that the app does not serve — the UI would 404;
* a path the app serves that the contract does not describe — an undocumented endpoint, which is how
  "the UI uses only the public API" quietly stops being true.

Paths not yet implemented are listed in :data:`NOT_YET_IMPLEMENTED` with the task that adds each.
That list is the honest form of "partially built": it shrinks as tasks land, and a path missing from
both the app and the list is a failure rather than an omission.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from aggregato.config import load_config
from aggregato.main import API_PREFIX, create_app

CONTRACT = (
    Path(__file__).parent.parent.parent / "specs/001-media-log-aggregator/contracts/openapi.yaml"
)

#: Contract paths with no implementation yet, each with the task that adds it. Every entry is a
#: promise rather than an exemption — T144's final gate check expects this dict empty.
NOT_YET_IMPLEMENTED: dict[str, str] = {
    "/works": "T054",
    "/works/{id}": "T054",
    "/works/{id}/merge": "T105",
    "/entries": "T053",
    "/opinions": "T055",
    "/creators": "T074",
    "/creators/{id}": "T074",
    "/creators/{id}/merge": "T105",
    "/creators/{id}/split": "T105",
    "/providers": "T056",
    "/providers/{id}/config-schema": "T122",
    "/providers/{id}/runs": "T093",
    "/providers/{id}/sync": "T056",
    "/providers/{id}/check": "T056",
    "/providers/{id}/import": "T113",
    "/resolution-queue": "T106",
    "/resolution-queue/{id}/decide": "T106",
    "/merge-log/{id}/undo": "T104",
    "/ingest-failures": "T093",
    "/stats/summary": "T136",
    "/stats/top": "T136",
    "/media/image/{hash}": "T078",
    "/export": "T131",
}


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    """The normative contract document."""
    loaded: dict[str, Any] = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """The real app, without migrations — this suite inspects the schema, it does not query."""
    config = load_config({"AGGREGATO_TOKEN": "test-token", "AGGREGATO_DATA": "./data"})
    return create_app(config, run_migrations=False)


@pytest.fixture(scope="module")
def served(app: FastAPI) -> dict[str, Any]:
    """The schema FastAPI generates from the code."""
    schema: dict[str, Any] = app.openapi()
    return schema


def _contract_paths(contract: dict[str, Any]) -> dict[str, set[str]]:
    return {
        path: {m for m in spec if m in {"get", "post", "put", "patch", "delete"}}
        for path, spec in contract["paths"].items()
    }


def _served_paths(served: dict[str, Any]) -> dict[str, set[str]]:
    """Served paths, stripped of the ``/api/v1`` prefix the contract expresses as a server URL."""
    paths: dict[str, set[str]] = {}
    for path, spec in served["paths"].items():
        if not path.startswith(API_PREFIX):
            continue
        trimmed = path.removeprefix(API_PREFIX) or "/"
        methods = {m for m in spec if m in {"get", "post", "put", "patch", "delete"}}
        paths.setdefault(trimmed, set()).update(methods)
    return paths


# --- The contract document itself ---------------------------------------------------------------


def test_the_contract_is_valid_yaml_and_declares_its_version(contract: dict[str, Any]) -> None:
    assert contract["openapi"].startswith("3.")
    assert contract["info"]["title"] == "Aggregato API"


def test_the_contract_has_no_unauthenticated_endpoint(contract: dict[str, Any]) -> None:
    """FR-032 at the document level: a global ``security`` block, and nothing opting out to nothing.

    ``POST /auth/session`` narrows to bearer only — which is not an exemption, it is the one
    endpoint a session cookie cannot authenticate because it is what issues the cookie.
    """
    assert contract["security"], "the contract must declare global security"
    for path, spec in contract["paths"].items():
        for method, operation in spec.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            security = operation.get("security")
            if security is not None:
                assert security != [], f"{method.upper()} {path} opts out of authentication"


def test_every_contract_path_is_either_served_or_listed_as_pending(
    contract: dict[str, Any], served: dict[str, Any]
) -> None:
    """The honest-progress check: implemented, or explicitly owed to a named task."""
    contract_paths = set(_contract_paths(contract))
    served_paths = set(_served_paths(served))
    missing = contract_paths - served_paths - set(NOT_YET_IMPLEMENTED)
    assert missing == set(), (
        f"contract paths neither served nor listed in NOT_YET_IMPLEMENTED: {sorted(missing)}"
    )


def test_nothing_is_served_that_the_contract_does_not_describe(
    contract: dict[str, Any], served: dict[str, Any]
) -> None:
    """An undocumented endpoint is how "the UI consumes only the public API" stops being true."""
    undocumented = set(_served_paths(served)) - set(_contract_paths(contract))
    assert undocumented == set(), f"served but undocumented: {sorted(undocumented)}"


def test_the_pending_list_names_only_real_contract_paths() -> None:
    """A stale entry would hide a genuinely missing endpoint behind a promise."""
    declared = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["paths"]
    unknown = set(NOT_YET_IMPLEMENTED) - set(declared)
    assert unknown == set(), (
        f"NOT_YET_IMPLEMENTED names paths the contract does not have: {unknown}"
    )


def test_the_pending_list_does_not_claim_something_already_served(
    served: dict[str, Any],
) -> None:
    """When a task lands, its entry must come out — otherwise the list stops meaning anything."""
    stale = set(NOT_YET_IMPLEMENTED) & set(_served_paths(served))
    assert stale == set(), f"already served but still listed as pending: {sorted(stale)}"


# --- Method agreement on what IS implemented ---------------------------------------------------


def test_implemented_paths_serve_exactly_the_contract_methods(
    contract: dict[str, Any], served: dict[str, Any]
) -> None:
    contract_paths = _contract_paths(contract)
    served_paths = _served_paths(served)
    for path, methods in served_paths.items():
        expected = contract_paths[path]
        assert methods == expected, (
            f"{path}: serves {sorted(methods)}, contract says {sorted(expected)}"
        )


def test_health_and_auth_are_the_currently_implemented_surface(served: dict[str, Any]) -> None:
    """Pins what exists today, so an endpoint appearing without a contract entry is noticed."""
    assert set(_served_paths(served)) == {"/health", "/auth/session"}


# --- Conventions the contract states and the app must not contradict ----------------------------


def test_no_endpoint_offers_an_offset_parameter(contract: dict[str, Any]) -> None:
    """Pagination is keyset-only; ``offset`` is called a footgun by the spec itself (FR-030)."""
    offenders = []
    for path, spec in contract["paths"].items():
        for method, operation in spec.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("name") == "offset":
                    offenders.append(f"{method.upper()} {path}")
    assert offenders == [], f"offset parameters found: {offenders}"


def test_the_problem_schema_matches_rfc_9457(contract: dict[str, Any]) -> None:
    problem = contract["components"]["schemas"]["Problem"]
    assert set(problem["required"]) == {"type", "title", "status"}
    response = contract["components"]["responses"]["Problem"]
    assert "application/problem+json" in response["content"]


def test_the_enum_schemas_match_the_domain_vocabularies(contract: dict[str, Any]) -> None:
    """The contract and the code must not disagree about a closed vocabulary (FR-008).

    Drift here is the kind that surfaces as a client rejecting a value the server considers valid.
    """
    from aggregato.domain.enums import (
        Acquisition,
        EntryKind,
        ErrorClass,
        MediaFamily,
        MediaType,
        ProviderStatus,
        Role,
        RunStatus,
    )

    schemas = contract["components"]["schemas"]
    pairs = [
        ("MediaType", MediaType),
        ("MediaFamily", MediaFamily),
        ("EntryKind", EntryKind),
        ("Role", Role),
        ("ErrorClass", ErrorClass),
        ("RunStatus", RunStatus),
        ("ProviderStatus", ProviderStatus),
        ("Acquisition", Acquisition),
    ]
    for name, enum in pairs:
        assert set(schemas[name]["enum"]) == {m.value for m in enum}, name
