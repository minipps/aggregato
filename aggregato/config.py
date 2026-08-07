"""Configuration resolution: defaults ← YAML file ← ``${ENV}`` ← explicit caller overlay.

The precedence and every rule below come from research.md :

* ``api.token`` is the only fatal configuration error — there is no unauthenticated mode
  , so a missing token raises :class:`MissingTokenError` and startup stops.
* An invalid *provider* block disables that provider and records the error. It never stops
  startup and never touches another provider . That is the important behaviour here.
* Secrets are read from the environment at read time and never written back to disk or returned
  by the API: :meth:`Config.public_dict` emits the ``${VAR}`` reference, never its value.
* Anything set in the YAML file is reported in :attr:`Config.file_pinned` so the UI can explain
  why an edit does not stick.

``pydantic-settings`` is deliberately not used: implementing the four-layer precedence explicitly
is a handful of lines and keeps :func:`load_config` a pure function of its arguments — nothing is
read from the environment at import time.
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

__all__ = [
    "Config",
    "ConfigError",
    "MissingTokenError",
    "ProviderConfig",
    "load_config",
]


class ConfigError(Exception):
    """Configuration that cannot yield a usable :class:`Config`."""


class MissingTokenError(ConfigError):
    """``api.token`` is unset. The only fatal configuration error ."""


# The operator-facing environment variables (.env.example is the contract) mapped onto the dotted
# setting paths they override.
ENV_SETTINGS: Mapping[str, str] = {
    "AGGREGATO_TOKEN": "api.token",
    "AGGREGATO_READONLY_TOKEN": "api.readonly_token",
    "AGGREGATO_CORS_ORIGINS": "api.cors_origins",
    "AGGREGATO_IMPORT_QUOTA_BYTES": "api.import_quota_bytes",
    "AGGREGATO_IMPORT_TOTAL_QUOTA_BYTES": "api.import_total_quota_bytes",
    "AGGREGATO_HOST": "api.host",
    "AGGREGATO_PORT": "api.port",
    "AGGREGATO_DATA": "data_dir",
    "AGGREGATO_DATABASE_URL": "database_url",
    "AGGREGATO_STATIC_DIR": "static_dir",
    "AGGREGATO_PROVIDER_DIR": "provider_dir",
}

DEFAULTS: Mapping[str, Any] = {
    # Loopback, not 0.0.0.0: the container publishes the port itself and docker/entrypoint.sh
    # already passes `--host ${AGGREGATO_HOST:-0.0.0.0}`, so binding every interface stays an
    # explicit deployment choice rather than the default a bare `uvicorn` run gets.
    "api": {"host": "127.0.0.1", "port": 8000},
    "data_dir": "./data",
}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ProviderConfig(BaseModel):
    """One provider's opaque configuration block.

    The core never interprets ``settings``; the provider validates its own schema. What matters
    here is that a block this module *cannot* resolve arrives with ``enabled=False`` and an
    ``error`` instead of raising .
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    settings: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    error: str | None = None


class ApiSettings(BaseModel):
    """The HTTP surface's settings. ``token`` is required ."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: SecretStr
    #: Optional second bearer credential granting reads only: every state-changing request is
    #: refused. For exposing the archive in a public or shared environment without handing over
    #: the credential that can edit settings or trigger syncs.
    readonly_token: SecretStr | None = None
    #: Browser origins allowed to call the API cross-origin, as a comma-separated list.
    #:
    #: Empty — the default — mounts no CORS middleware at all, which is what the supported
    #: deployments want: the API serves the SPA itself and Vite proxies ``/api`` in development, so
    #: neither is ever cross-origin and neither ever preflights. Set it only for a browser client
    #: served from somewhere else.
    cors_origins: tuple[str, ...] = ()
    #: Maximum retained import-file bytes per provider. The upload itself is included in the
    #: admission check, so this is a storage quota rather than just a pre-existing-usage warning.
    import_quota_bytes: int = Field(default=512 * 1024 * 1024, ge=0)
    #: Maximum retained import-file bytes across all providers. The per-provider quota still applies
    #: independently, so either limit may reject an upload.
    import_total_quota_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=0)
    host: str = "127.0.0.1"
    port: int = 8000

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _origins(cls, value: object) -> object:
        """Split the environment's comma-separated string, and refuse a wildcard.

        ``*`` is rejected rather than passed through: this API authenticates with a session cookie,
        the middleware is mounted with credentials enabled, and "any origin may send the browser's
        cookie" is a different thing from "the archive is public". The CORS specification forbids
        the combination outright, so a wildcard here would silently produce a policy that rejects
        every credentialed request anyway — an unexplainable failure instead of a refused setting.
        """
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list | tuple) and "*" in value:
            raise ValueError(
                "api.cors_origins does not accept '*': the API sends a session cookie, and CORS "
                "forbids a wildcard origin with credentials. List the origins explicitly."
            )
        return value


class Config(BaseModel):
    """A fully resolved configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api: ApiSettings
    data_dir: Path
    database_url: str
    #: Where the built SPA lives. Set by the Docker image; absent in a development checkout, where
    #: Vite serves the frontend itself and proxies /api. Absent therefore means "API only", not an
    #: error.
    static_dir: Path | None = None
    #: Optional directory of operator-installed provider packages.  These are discovered as
    #: unreviewed drop-ins; no in-application installation mechanism exists .
    provider_dir: Path | None = None
    #: Disable remote image retrieval while preserving stable local image URLs .
    image_cache_enabled: bool = True
    config_file: Path | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    file_pinned: frozenset[str] = frozenset()
    # Dotted path (in the shape of `public_dict`) → the `${VAR}` text it came from. Kept so the
    # resolved secret can be swapped back out for its reference on the way to the API .
    env_refs: dict[str, str] = Field(default_factory=dict)

    def is_file_pinned(self, path: str) -> bool:
        """Report whether ``path`` (e.g. ``"api.port"``) was set in the YAML file.

        A pinned setting is not editable in the UI, and the UI needs to say so .
        """
        return path in self.file_pinned

    def public_dict(self) -> dict[str, Any]:
        """The API/serialization view: no secret values anywhere.

        ``api.token`` is dropped outright and every value that came from ``${VAR}`` interpolation
        is replaced by that reference, so a resolved credential can neither reach an API response
        nor be written back to a config file .
        """
        data: dict[str, Any] = self.model_dump(mode="json", exclude={"env_refs"})
        data["api"].pop("token", None)
        data["api"].pop("readonly_token", None)
        for path, ref in self.env_refs.items():
            _set_path(data, path, ref)
        return data


def load_config(
    env: Mapping[str, str] | None = None,
    config_file: str | Path | None = None,
    db_overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Resolve configuration from the startup layers plus an explicit caller overlay.

    Args:
        env: The environment to read. Defaults to ``os.environ``; passed explicitly by tests so
            no global state needs monkeypatching. An empty value counts as unset, because
            copying ``.env.example`` leaves ``AGGREGATO_TOKEN=``.
        config_file: The YAML file to read. Defaults to ``$AGGREGATO_CONFIG``; absent means no
            file layer.
        db_overrides: Optional dotted setting paths supplied by the caller, addressing the raw
            setting tree (``"api.port"``, ``"providers.listenbrainz.token"``). This function does
            not read the database; the worker reads mutable provider settings separately from
            ``providers.config`` immediately before spawning a child.

    Returns:
        A frozen :class:`Config`. Providers whose blocks were unusable are present but disabled
        with their error recorded.

    Raises:
        MissingTokenError: ``api.token``/``AGGREGATO_TOKEN`` is unset .
        ConfigError: The YAML file is unreadable or malformed, ``providers`` is not a mapping, or
            a non-provider setting references an unset ``${VAR}``. These are file-structure
            failures, not the per-setting errors  wants tolerated — ignoring them would run
            the service on a configuration the operator never asked for.
    """
    env = os.environ if env is None else env
    path = _resolve_config_path(env, config_file)
    file_layer = _read_yaml(path) if path is not None else {}
    # Computed before the layer is consumed: pinning is about what the *file* said .
    pinned = frozenset(_dotted_paths(file_layer))

    raw_providers = file_layer.pop("providers", {})
    tree: dict[str, Any] = _merge(copy.deepcopy(dict(DEFAULTS)), file_layer)
    for var, setting in ENV_SETTINGS.items():
        value = env.get(var)
        if value:
            _set_path(tree, setting, value)
    for setting, value in (db_overrides or {}).items():
        _set_path(tree, setting, value)

    # A provider block is resolved in isolation so one broken block cannot take out the process
    # or its neighbours . Everything else interpolates eagerly and fails loudly.
    provider_tree = tree.pop("providers", None)
    refs: dict[str, str] = {}
    tree = _interpolate(tree, env, "", refs)
    providers = _load_providers(_merge_providers(raw_providers, provider_tree), env, refs)

    if not tree.get("api", {}).get("token"):
        raise MissingTokenError(
            "api.token is unset: set AGGREGATO_TOKEN. There is no unauthenticated mode "
        )
    api = tree["api"]
    if api.get("readonly_token") and api["readonly_token"] == api["token"]:
        # Otherwise the read-only token would silently grant full access: the matcher takes the
        # first credential that compares equal, and that is always the real token.
        raise ConfigError("api.readonly_token must differ from api.token, or it grants full access")
    tree.setdefault("database_url", f"sqlite+aiosqlite:///{tree['data_dir']}/aggregato.db")

    try:
        return Config(
            **tree,
            config_file=path,
            providers=providers,
            file_pinned=pinned,
            env_refs=refs,
        )
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc


def _resolve_config_path(env: Mapping[str, str], config_file: str | Path | None) -> Path | None:
    value = config_file if config_file is not None else env.get("AGGREGATO_CONFIG") or None
    return Path(value) if value is not None else None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"config file {path} must contain a mapping, got {type(loaded).__name__}")
    return loaded


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively overlay ``overlay`` onto ``base``, mutating and returning ``base``."""
    for key, value in overlay.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value)
        else:
            base[key] = value
    return base


def _merge_providers(from_file: Any, from_overrides: Any) -> Mapping[str, Any]:
    if from_overrides is None:
        raw = from_file
    elif isinstance(from_file, dict) and isinstance(from_overrides, dict):
        raw = _merge(copy.deepcopy(from_file), from_overrides)
    else:
        raw = from_overrides
    if not isinstance(raw, dict):
        raise ConfigError(f"`providers` must be a mapping of provider id to config, got {raw!r}")
    return raw


def _load_providers(
    raw: Mapping[str, Any], env: Mapping[str, str], refs: dict[str, str]
) -> dict[str, ProviderConfig]:
    providers: dict[str, ProviderConfig] = {}
    for pid, block in raw.items():
        provider_id = str(pid)
        local: dict[str, str] = {}
        try:
            if not isinstance(block, dict):
                raise ConfigError(f"config must be a mapping, got {type(block).__name__}")
            settings = _interpolate(block, env, f"providers.{provider_id}.settings", local)
            providers[provider_id] = ProviderConfig(id=provider_id, settings=settings)
        except (ConfigError, ValidationError) as exc:
            # Disabled, recorded, startup unaffected, neighbours unaffected .
            providers[provider_id] = ProviderConfig(id=provider_id, enabled=False, error=str(exc))
            continue
        refs.update(local)
    return providers


def _interpolate(value: Any, env: Mapping[str, str], path: str, refs: dict[str, str]) -> Any:
    """Replace ``${VAR}`` in every string, recording each reference under its dotted path.

    Raises:
        ConfigError: A referenced variable is unset. Leaking a literal ``${VAR}`` downstream
            turns a missing credential into a confusing auth failure much later .
    """
    if isinstance(value, str):
        if not _VAR.search(value):
            return value
        refs[path] = value
        return _VAR.sub(lambda m: _lookup(m.group(1), env, path), value)
    if isinstance(value, dict):
        return {k: _interpolate(v, env, _join(path, str(k)), refs) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, env, f"{path}[{i}]", refs) for i, v in enumerate(value)]
    return value


def _lookup(name: str, env: Mapping[str, str], path: str) -> str:
    value = env.get(name)
    if not value:
        raise ConfigError(f"{path or 'config'} references ${{{name}}}, which is unset")
    return value


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _set_path(tree: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    node = tree
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[leaf] = value


def _dotted_paths(tree: Mapping[str, Any], prefix: str = "") -> Iterator[str]:
    """Every leaf path in a mapping, plus each intermediate one, as dotted strings."""
    for key, value in tree.items():
        path = _join(prefix, str(key))
        yield path
        if isinstance(value, dict):
            yield from _dotted_paths(value, path)
