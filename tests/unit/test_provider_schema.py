"""The API supports only the provider settings schema the form can render."""

from __future__ import annotations

import pytest

from aggregato.api.errors import ProblemError
from aggregato.api.routes.providers import _public_settings
from aggregato.providers.settings import validate_settings as _validate_settings


def _schema(field: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"setting": field},
    }


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ({"type": "object", "properties": {}}, "nested objects"),
        ({"type": "array", "items": {"type": "string"}}, "arrays"),
        ({"type": "string", "oneOf": [{"type": "string"}]}, "oneOf"),
        ({"type": "string", "allOf": [{"type": "string"}]}, "allOf"),
        ({"$ref": "#/$defs/Setting"}, "$ref"),
    ],
)
def test_unsupported_provider_schema_is_rejected_clearly(
    field: dict[str, object], message: str
) -> None:
    errors = _validate_settings(_schema(field), {"setting": "value"})

    assert len(errors) == 1
    assert "unsupported" in errors[0]
    assert message in errors[0]


def test_projection_rejects_unsupported_schema_instead_of_partial_interpretation() -> None:
    with pytest.raises(ProblemError, match="unsupported"):
        _public_settings(_schema({"type": "array", "items": {"type": "string"}}), {})


def test_projection_uses_public_and_write_only_metadata_for_redaction() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "visible": {"type": "string", "x-aggregato-public": True},
            "secret_with_an_unusual_name": {
                "type": "string",
                "x-aggregato-public": True,
                "writeOnly": True,
            },
            "unmarked": {"type": "string"},
            "password_format": {
                "type": "string",
                "x-aggregato-public": True,
                "format": "password",
            },
            "nullable_secret": {
                "anyOf": [
                    {"type": "string", "x-aggregato-public": False},
                    {"type": "null"},
                ],
                "default": None,
                "writeOnly": True,
            },
        },
    }

    projected = _public_settings(
        schema,
        {
            "visible": "shown",
            "secret_with_an_unusual_name": "hidden",
            "unmarked": "hidden",
            "password_format": "hidden",
            "nullable_secret": "hidden",
        },
    )

    assert projected == {"visible": "shown"}


def test_nullable_scalar_and_enum_constraints_are_validated() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "state": {
                "type": "string",
                "enum": ["new", "done"],
                "x-aggregato-public": True,
            },
            "limit": {"type": "number", "minimum": 1, "x-aggregato-public": True},
            "note": {
                "anyOf": [
                    {"type": "string", "minLength": 2, "x-aggregato-public": True},
                    {"type": "null"},
                ],
                "default": None,
            },
        },
        "required": ["state", "limit"],
    }

    assert _validate_settings(schema, {"state": "done", "limit": 2, "note": None}) == []
    errors = _validate_settings(schema, {"state": "other", "limit": 0, "note": ""})
    assert "settings.state must be one of ['new', 'done']" in errors
    assert "settings.limit must be at least 1" in errors
    assert "settings.note must contain at least 2 characters" in errors
