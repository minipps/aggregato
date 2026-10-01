"""Host-side parsing and validation for provider setting schemas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class _SettingField:
    """One scalar field from the API's deliberately small provider-schema contract."""

    schema: dict[str, Any]
    nullable: bool
    public: bool


class UnsupportedProviderSchema(ValueError):
    """A provider schema uses features the API does not implement."""


_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})
_ROOT_SCHEMA_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "x-aggregato-public",
    }
)
_FIELD_SCHEMA_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "format",
        "writeOnly",
        "x-aggregato-public",
        "enum",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "anyOf",
    }
)


def _setting_fields(schema: object) -> tuple[dict[str, _SettingField], frozenset[str]]:
    """Read the supported flat schema without resolving or traversing JSON Schema."""
    if not isinstance(schema, dict):
        raise UnsupportedProviderSchema("the schema must be an object")
    _check_schema_keys(schema, _ROOT_SCHEMA_KEYS, "settings")
    if schema.get("type") != "object":
        raise UnsupportedProviderSchema("settings must be a top-level object")
    if schema.get("additionalProperties") is not False:
        raise UnsupportedProviderSchema(
            "settings must set additionalProperties to false for a flat schema"
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise UnsupportedProviderSchema("settings.properties must be an object")

    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
        raise UnsupportedProviderSchema("settings.required must be a list of field names")
    required_names = frozenset(required)
    unknown_required = sorted(required_names - set(properties))
    if unknown_required:
        raise UnsupportedProviderSchema(
            f"settings.required names unknown field {unknown_required[0]!r}"
        )

    fields: dict[str, _SettingField] = {}
    for name, node in properties.items():
        if not isinstance(name, str):
            raise UnsupportedProviderSchema("settings.properties keys must be strings")
        fields[name] = _setting_field(node, f"settings.{name}")
    return fields, required_names


def _setting_field(node: object, path: str) -> _SettingField:
    if not isinstance(node, dict):
        raise UnsupportedProviderSchema(f"{path} must be a schema object")

    if "anyOf" not in node:
        _validate_scalar_schema(node, path)
        _validate_default(node, node, nullable=False, path=path)
        return _SettingField(node, False, _is_public_field(node, node))

    nullable_keys = {"anyOf", "title", "description", "default", "writeOnly", "x-aggregato-public"}
    _check_schema_keys(node, frozenset(nullable_keys), path)
    choices = node["anyOf"]
    if not isinstance(choices, list) or len(choices) != 2:
        raise UnsupportedProviderSchema(f"{path} supports only anyOf [scalar, {{'type': 'null'}}]")
    scalar_choices = [
        choice for choice in choices if isinstance(choice, dict) and choice.get("type") != "null"
    ]
    null_choices = [
        choice for choice in choices if isinstance(choice, dict) and choice.get("type") == "null"
    ]
    if len(scalar_choices) != 1 or len(null_choices) != 1:
        raise UnsupportedProviderSchema(f"{path} supports only anyOf [scalar, {{'type': 'null'}}]")
    if set(null_choices[0]) != {"type"}:
        raise UnsupportedProviderSchema(
            f"{path} nullable anyOf null branch must be {{'type': 'null'}}"
        )
    scalar = scalar_choices[0]
    _validate_scalar_schema(scalar, path)
    _validate_default(node, scalar, nullable=True, path=path)
    return _SettingField(scalar, True, _is_public_field(node, scalar))


def _validate_scalar_schema(node: dict[str, Any], path: str) -> None:
    kind = node.get("type")
    if kind in {"object", "array"}:
        raise UnsupportedProviderSchema(
            f"{path} uses unsupported type {kind!r}; nested objects and arrays are not supported"
        )
    _check_schema_keys(node, _FIELD_SCHEMA_KEYS - {"anyOf"}, path)
    if not isinstance(kind, str) or kind not in _SCALAR_TYPES:
        raise UnsupportedProviderSchema(
            f"{path} must declare one scalar type: string, number, integer, or boolean"
        )

    _validate_field_metadata(node, path)
    enum = node.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise UnsupportedProviderSchema(f"{path}.enum must be a non-empty list")
        if not all(_matches_scalar_type(value, kind) for value in enum):
            raise UnsupportedProviderSchema(f"{path}.enum contains a value with the wrong type")

    constraints = {
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    }
    if kind == "string":
        for keyword in ("minLength", "maxLength"):
            value = node.get(keyword)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise UnsupportedProviderSchema(f"{path}.{keyword} must be a non-negative integer")
        pattern = node.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise UnsupportedProviderSchema(f"{path}.pattern must be a string")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise UnsupportedProviderSchema(
                    f"{path}.pattern is not a valid regular expression: {exc}"
                ) from exc
        invalid = constraints - {"minLength", "maxLength", "pattern"}
    elif kind in {"number", "integer"}:
        for keyword in constraints - {"minLength", "maxLength", "pattern"}:
            value = node.get(keyword)
            if value is not None and (
                not isinstance(value, int | float) or isinstance(value, bool)
            ):
                raise UnsupportedProviderSchema(f"{path}.{keyword} must be a number")
        multiple = node.get("multipleOf")
        if multiple is not None and multiple <= 0:
            raise UnsupportedProviderSchema(f"{path}.multipleOf must be greater than zero")
        invalid = {"minLength", "maxLength", "pattern"}
    else:
        invalid = constraints
    if any(keyword in node for keyword in invalid):
        raise UnsupportedProviderSchema(
            f"{path} declares a constraint that does not apply to {kind!r}"
        )


def _validate_field_metadata(node: dict[str, Any], path: str) -> None:
    for keyword in ("title", "description", "format"):
        value = node.get(keyword)
        if value is not None and not isinstance(value, str):
            raise UnsupportedProviderSchema(f"{path}.{keyword} must be a string")
    for keyword in ("writeOnly", "x-aggregato-public"):
        value = node.get(keyword)
        if value is not None and not isinstance(value, bool):
            raise UnsupportedProviderSchema(f"{path}.{keyword} must be a boolean")


def _validate_default(
    wrapper: dict[str, Any], scalar: dict[str, Any], *, nullable: bool, path: str
) -> None:
    if "default" not in wrapper:
        return
    value = wrapper["default"]
    if value is None and nullable:
        return
    if not _matches_scalar_type(value, scalar.get("type")):
        raise UnsupportedProviderSchema(f"{path}.default has the wrong type")


def _is_public_field(wrapper: dict[str, Any], scalar: dict[str, Any]) -> bool:
    """Use only manifest metadata as the redaction policy; absence is private by default."""
    nodes = (wrapper, scalar)
    if any(
        node.get("writeOnly") is True
        or node.get("format") == "password"
        or node.get("x-aggregato-public") is False
        for node in nodes
    ):
        return False
    return any(node.get("x-aggregato-public") is True for node in nodes)


def _check_schema_keys(node: dict[str, Any], allowed: frozenset[str], path: str) -> None:
    unsupported = sorted(key for key in node if key not in allowed)
    if unsupported:
        raise UnsupportedProviderSchema(
            f"{path} uses unsupported schema keyword {unsupported[0]!r}"
        )


def validate_settings(schema: dict[str, Any], value: object) -> list[str]:
    """Validate flat provider settings without importing provider code or resolving refs."""
    try:
        fields, required = _setting_fields(schema)
    except UnsupportedProviderSchema as exc:
        return [f"provider configuration schema is unsupported: {exc}"]
    if not isinstance(value, dict):
        return ["settings must be an object"]

    errors = [f"settings.{key} is required" for key in sorted(required) if key not in value]
    errors.extend(
        f"settings.{key} is not a recognized setting" for key in value if key not in fields
    )
    for key, setting in value.items():
        if isinstance(key, str) and (field := fields.get(key)) is not None:
            errors.extend(_validate_field_value(setting, field, f"settings.{key}"))
    return errors


def _validate_field_value(value: object, field: _SettingField, path: str) -> list[str]:
    if value is None:
        return [] if field.nullable else [f"{path} must be {field.schema['type']}"]
    kind = field.schema["type"]
    enum = field.schema.get("enum")
    if kind == "string":
        if not isinstance(value, str):
            return [f"{path} must be a string"]
        if isinstance(enum, list) and not any(value == option for option in enum):
            return [f"{path} must be one of {enum!r}"]
        minimum = field.schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return [f"{path} must contain at least {minimum} characters"]
        maximum = field.schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            return [f"{path} must contain at most {maximum} characters"]
        pattern = field.schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            return [f"{path} does not match the provider pattern"]
    elif kind in {"number", "integer"}:
        if not isinstance(value, int | float) or isinstance(value, bool):
            message = "an integer" if kind == "integer" else "a number"
            return [f"{path} must be {message}"]
        if kind == "integer" and not isinstance(value, int):
            return [f"{path} must be an integer"]
        if isinstance(enum, list) and not any(value == option for option in enum):
            return [f"{path} must be one of {enum!r}"]
        minimum = field.schema.get("minimum")
        if isinstance(minimum, int | float) and not isinstance(minimum, bool) and value < minimum:
            return [f"{path} must be at least {minimum}"]
        maximum = field.schema.get("maximum")
        if isinstance(maximum, int | float) and not isinstance(maximum, bool) and value > maximum:
            return [f"{path} must be at most {maximum}"]
        exclusive_minimum = field.schema.get("exclusiveMinimum")
        if (
            isinstance(exclusive_minimum, int | float)
            and not isinstance(exclusive_minimum, bool)
            and value <= exclusive_minimum
        ):
            return [f"{path} must be greater than {exclusive_minimum}"]
        exclusive_maximum = field.schema.get("exclusiveMaximum")
        if (
            isinstance(exclusive_maximum, int | float)
            and not isinstance(exclusive_maximum, bool)
            and value >= exclusive_maximum
        ):
            return [f"{path} must be less than {exclusive_maximum}"]
        multiple = field.schema.get("multipleOf")
        if isinstance(multiple, int | float) and not isinstance(multiple, bool):
            quotient = value / multiple
            if abs(quotient - round(quotient)) > 1e-9:
                return [f"{path} must be a multiple of {multiple}"]
    elif kind == "boolean":
        if not isinstance(value, bool):
            return [f"{path} must be a boolean"]
        if isinstance(enum, list) and not any(value == option for option in enum):
            return [f"{path} must be one of {enum!r}"]
    else:
        return [f"{path} has unsupported type {kind!r}"]
    return []


def _matches_scalar_type(value: object, kind: object) -> bool:
    return (
        (kind == "string" and isinstance(value, str))
        or (kind == "number" and isinstance(value, int | float) and not isinstance(value, bool))
        or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (kind == "boolean" and isinstance(value, bool))
    )


def public_settings(schema: object, settings: object) -> dict[str, Any]:
    """Return valid configured values explicitly marked safe for display or backup."""
    fields, _ = _setting_fields(schema)
    if not isinstance(settings, dict):
        return {}
    return {
        key: value
        for key, value in settings.items()
        if isinstance(key, str)
        and (field := fields.get(key)) is not None
        and field.public
        and not _validate_field_value(value, field, f"settings.{key}")
    }
