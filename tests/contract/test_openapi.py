"""Compare served and documented paths, methods, and selected response shapes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from aggregato.config import load_config
from aggregato.main import API_PREFIX, create_app

CONTRACT = Path(__file__).parent.parent.parent / "docs/contracts/openapi.yaml"


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


def _assert_schema_shape_matches(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    assert set(actual["required"]) == set(expected["required"])
    assert set(actual["properties"]) == set(expected["properties"])

    def shape(schema: dict[str, Any]) -> tuple[str | None, bool]:
        variants = schema.get("anyOf", [schema])
        types = {variant.get("type") for variant in variants}
        nullable = schema.get("nullable", False) or "null" in types
        types.discard("null")
        assert len(types) <= 1
        return next(iter(types), None), nullable

    for name, expected_property in expected["properties"].items():
        actual_property = actual["properties"][name]
        assert shape(actual_property) == shape(expected_property), name
        assert actual_property.get("$ref") == expected_property.get("$ref"), name
        if expected_property.get("type") == "array":
            assert actual_property["items"].get("$ref") == expected_property["items"].get("$ref"), (
                name
            )


# --- The contract document itself ---------------------------------------------------------------


def test_the_contract_is_valid_yaml_and_declares_its_version(contract: dict[str, Any]) -> None:
    assert contract["openapi"].startswith("3.")
    assert contract["info"]["title"] == "Aggregato API"


def test_the_contract_has_no_unauthenticated_endpoint(contract: dict[str, Any]) -> None:
    """At the document level, auth is the default and no operation opts out unconditionally.

    ``POST /auth/session`` narrows to bearer only — which is not an exemption, it is the one
    endpoint a session cookie cannot authenticate because it is what issues the cookie.

    ``GET /media/image/{hash}`` is the single real exemption, listed here so adding a second one has
    to be an argued edit rather than a passing test: an ``<img>`` tag sends no credential, and the
    path carries an unguessable sha256 of artwork the source platform serves publicly .
    """
    unauthenticated = {("get", "/media/image/{hash}")}
    assert contract["security"], "the contract must declare global security"
    for path, spec in contract["paths"].items():
        for method, operation in spec.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            security = operation.get("security")
            if security is not None and (method, path) not in unauthenticated:
                assert security != [], f"{method.upper()} {path} opts out of authentication"


def test_served_paths_and_methods_match_the_contract(
    contract: dict[str, Any], served: dict[str, Any]
) -> None:
    contract_paths = _contract_paths(contract)
    served_paths = _served_paths(served)
    assert set(served_paths) == set(contract_paths), (
        f"path mismatch: missing={sorted(set(contract_paths) - set(served_paths))}, "
        f"undocumented={sorted(set(served_paths) - set(contract_paths))}"
    )
    for path, methods in contract_paths.items():
        assert served_paths[path] == methods, (
            f"{path}: serves {sorted(served_paths[path])}, contract says {sorted(methods)}"
        )


def test_phase_three_paths_are_present(served: dict[str, Any]) -> None:
    """Pins what exists today, so an endpoint appearing without a contract entry is noticed."""
    assert {
        "/health",
        "/auth/session",
        "/entries",
        "/opinions",
        "/works",
        "/works/{id}",
        "/providers",
        "/providers/{id}/sync",
        "/providers/{id}/last-run",
    } <= set(_served_paths(served))


def test_import_contract_documents_all_admission_failure_classes(
    contract: dict[str, Any],
) -> None:
    operation = contract["paths"]["/providers/{id}/import"]["post"]
    responses = operation["responses"]

    assert {"404", "409", "413", "422", "503"} <= set(responses)
    assert "415" not in responses
    assert "configured total and per-provider import storage quotas" in operation["description"]


def test_replay_contract_documents_queued_worker_semantics(contract: dict[str, Any]) -> None:
    operation = contract["paths"]["/ingest-failures/{id}/replay"]["post"]
    responses = operation["responses"]

    assert "202" in responses
    assert "200" not in responses
    assert "worker" in responses["202"]["description"]
    schema = responses["202"]["content"]["application/json"]["schema"]
    assert set(schema["required"]) == {"replayed", "queued"}


def test_failure_contract_exposes_replay_identity(contract: dict[str, Any]) -> None:
    properties = contract["components"]["schemas"]["IngestFailure"]["properties"]
    assert properties["native_id"]["nullable"] is True


def test_settings_response_matches_its_contract_shape(
    contract: dict[str, Any], served: dict[str, Any]
) -> None:
    schemas = contract["components"]["schemas"]
    served_schemas = served["components"]["schemas"]
    _assert_schema_shape_matches(served_schemas["SettingsView"], schemas["ArchiveSettings"])
    _assert_schema_shape_matches(served_schemas["StorageUsage"], schemas["StorageUsage"])


def test_run_modes_match_the_contract(contract: dict[str, Any], served: dict[str, Any]) -> None:
    expected = contract["components"]["schemas"]["SyncRun"]["properties"]["mode"]["enum"]
    assert set(served["components"]["schemas"]["FetchMode"]["enum"]) == set(expected)


def test_diagnostic_response_metadata_matches_its_contract_shape(
    contract: dict[str, Any], served: dict[str, Any]
) -> None:
    schemas = contract["components"]["schemas"]
    served_schemas = served["components"]["schemas"]
    _assert_schema_shape_matches(
        served_schemas["SyncRunDiagnostics"], schemas["SyncRunDiagnostics"]
    )
    actual_metadata = served_schemas["RunHttpResponseMetadata"]
    expected_metadata = schemas["RunHttpResponseMetadata"]
    _assert_schema_shape_matches(actual_metadata, expected_metadata)
    safe_fields = {"method", "url", "status"}
    assert set(actual_metadata["properties"]) == safe_fields
    assert set(actual_metadata["required"]) == safe_fields
    assert set(expected_metadata["properties"]) == safe_fields
    assert set(expected_metadata["required"]) == safe_fields
    assert actual_metadata["additionalProperties"] is False
    assert expected_metadata["additionalProperties"] is False


# --- Conventions the contract states and the app must not contradict ----------------------------


def test_no_endpoint_offers_an_offset_parameter(contract: dict[str, Any]) -> None:
    """Pagination is keyset-only; ``offset`` is called a footgun by the spec itself ."""
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
    """The contract and the code must not disagree about a closed vocabulary .

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
