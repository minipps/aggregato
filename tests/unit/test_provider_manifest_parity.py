"""The static API metadata stays aligned with each bundled provider declaration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from difflib import unified_diff
from typing import Any

from aggregato.providers.manifest import BUNDLED_MANIFESTS
from aggregato.providers.registry import discover_providers, load_provider

_VALIDATION_KEYS = (
    "type",
    "format",
    "enum",
    "const",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
)


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _scale(scale: Any) -> dict[str, Any]:
    return {
        "id": scale.id,
        "kind": _value(scale.kind),
        "min_value": str(scale.min_value),
        "max_value": str(scale.max_value),
        "step": str(scale.step),
        "labels": scale.labels,
    }


def _scalar_schema(field: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
    alternatives = field.get("anyOf")
    if alternatives is None:
        return field, False
    non_null = [
        alternative
        for alternative in alternatives
        if not isinstance(alternative, Mapping) or alternative.get("type") != "null"
    ]
    if len(non_null) != 1 or not isinstance(non_null[0], Mapping):
        raise AssertionError(f"configuration field is not a scalar or nullable scalar: {field}")
    return non_null[0], True


def _field_schema(name: str, field: Mapping[str, Any], required: set[str]) -> dict[str, Any]:
    scalar, nullable = _scalar_schema(field)
    write_only = bool(field.get("writeOnly", scalar.get("writeOnly", False)))
    public = field.get(
        "x-aggregato-public",
        scalar.get("x-aggregato-public", not write_only),
    )
    description = field.get("description")
    if description is None:
        description = scalar.get("description")
    constraints = {key: scalar[key] for key in _VALIDATION_KEYS if key in scalar}
    default = {
        "present": "default" in field,
        "value": field.get("default"),
    }
    return {
        "required": name in required,
        "nullable": nullable,
        "default": default,
        "public": bool(public),
        "writeOnly": write_only,
        "description": description,
        "constraints": constraints,
    }


def _config_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise AssertionError(f"configuration schema has no flat properties: {schema}")
    required_value = schema.get("required", ())
    if not isinstance(required_value, (list, tuple)):
        raise AssertionError(f"configuration schema has invalid required fields: {schema}")
    required = {str(name) for name in required_value}
    canonical_properties: dict[str, Any] = {}
    for name, field in sorted(properties.items()):
        if not isinstance(name, str) or not isinstance(field, Mapping):
            raise AssertionError(f"configuration schema has a non-scalar property: {schema}")
        canonical_properties[name] = _field_schema(name, field, required)
    return {
        "type": schema.get("type"),
        "additionalProperties": schema.get("additionalProperties"),
        "required": sorted(required),
        "properties": canonical_properties,
    }


def _metadata(provider: Any) -> dict[str, Any]:
    return {
        "id": provider.id,
        "name": provider.name,
        "media_types": sorted(_value(value) for value in provider.media_types),
        "capabilities": sorted(_value(value) for value in provider.capabilities),
        "acquisition": _value(provider.acquisition),
        "schema_version": provider.schema_version,
        "default_poll_interval_seconds": int(provider.default_poll_interval.total_seconds()),
        "rating_scales": [_scale(scale) for scale in provider.rating_scales],
        "config_schema": _config_schema(provider.config_model.model_json_schema()),
    }


def _expected_metadata(provider_id: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": provider_id,
        "name": manifest["name"],
        "media_types": sorted(manifest["media_types"]),
        "capabilities": sorted(manifest["capabilities"]),
        "acquisition": manifest["acquisition"],
        "schema_version": manifest["schema_version"],
        "default_poll_interval_seconds": manifest["default_poll_interval_seconds"],
        "rating_scales": [_scale(scale) for scale in manifest.get("rating_scales", ())],
        "config_schema": _config_schema(manifest["config_schema"]),
    }


def _diff(provider_id: str, actual: Mapping[str, Any], expected: Mapping[str, Any]) -> str:
    actual_text = json.dumps(actual, indent=2, sort_keys=True).splitlines()
    expected_text = json.dumps(expected, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        unified_diff(
            expected_text,
            actual_text,
            fromfile=f"{provider_id} manifest",
            tofile=f"{provider_id} provider",
            lineterm="",
        )
    )


def test_bundled_provider_declarations_match_static_manifests() -> None:
    discovered = {info.id for info in discover_providers() if info.reviewed}
    assert discovered == set(BUNDLED_MANIFESTS)

    mismatches: list[str] = []
    for provider_id in sorted(BUNDLED_MANIFESTS):
        provider = load_provider(provider_id)
        actual = _metadata(provider)
        expected = _expected_metadata(provider_id, BUNDLED_MANIFESTS[provider_id])
        if actual != expected:
            mismatches.append(_diff(provider_id, actual, expected))

    assert not mismatches, "bundled provider metadata drift:\n\n" + "\n\n".join(mismatches)
