"""Discovery of the providers that are installed — and nothing more.

This is a function that lists what is on disk. It is not a plugin manager: it holds no registry
state, fires no hooks, and never decides anything. Enablement, scheduling and credentials belong to
the host, which reads this list and acts on it .

**Discovery is inert.** Listing a provider must not fetch, authenticate, open a socket, write a
file, or schedule anything — a fresh install makes exactly zero outbound requests , and
discovery is where that is either true or false. It follows that importing a provider module must be
side-effect free: module bodies define classes and construct the provider object, and that is all.
Anything a provider needs to do to start working, it does in ``fetch`` or ``check``, when the host
calls it because the operator enabled it.

The convention, which third parties will follow, so it is deliberately boring:

1. A provider is a **package** directly under ``aggregato/providers/`` — a directory with an
   ``__init__.py``. Plain modules in this tree are host code (``base``, ``errors``, ``http``,
   ``registry``) and are never treated as providers.
2. The package's ``__init__.py`` exposes a **module-level object named ``provider``** implementing
   the ``Provider`` protocol. One attribute, no factory, no decorator: a factory would only exist to
   defer work, and no work is permitted at import time anyway.
3. ``provider.id`` equals the package name. The slug is the directory, so the same name identifies
   the provider in the tree, in the database, in the API and in ``tests/fixtures/<id>/``.
4. Constructing the provider object is free of side effects: no I/O, no network, no clock.

Bundled providers are ``reviewed=True`` because they ship with the core and went through review.
Drop-in providers are read from the explicitly configured operator directory using a required
static ``manifest.json``, marked ``reviewed=False``, and imported only inside the selected child
process. A drop-in is trusted code with the child process's filesystem/network/resource limits;
discovery is metadata parsing, not a sandbox.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import pkgutil
import sys
import warnings
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from .base import Provider
from .manifest import BUNDLED_MANIFESTS

#: This module's own directory, which is the provider tree. Derived from __file__ rather than by
#: importing the package into itself, which is a cycle waiting to bite during interpreter startup.
_PROVIDERS_DIR = Path(__file__).parent

PROVIDER_ATTR = "provider"
"""The module-level attribute every provider package exposes (convention 2 above)."""


@dataclass(frozen=True)
class ProviderInfo:
    """What is installed, without asking the provider to do anything.

    Declared metadata only — everything here is a constant the provider states about itself, so
    building it cannot cause work. The host uses it to render the provider list and to decide
    scheduling for the ones the operator enabled.
    """

    id: str
    name: str
    module: str
    """Dotted import path of the provider package, for ``load_provider``."""
    media_types: frozenset[str]
    capabilities: frozenset[str]
    acquisition: str
    schema_version: int
    default_poll_interval: timedelta
    reviewed: bool
    """``True`` for bundled providers; ``False`` for drop-ins, which the UI labels as unreviewed
    and warns about on the enable action ."""
    api_visible: bool
    """Whether the provider should be advertised by the operator-facing API."""
    config_schema: dict[str, Any]
    """Static settings schema; reading it never imports provider code."""
    rating_scales: tuple[Any, ...]
    """Host-owned rating declarations used by parent-side validation and writing."""
    import_inference: str | None
    """Name of a host-owned import metadata parser, never a provider callback."""


def discover_providers(drop_in_dir: Path | None = None) -> list[ProviderInfo]:
    """List installed providers using static manifests only.

    A provider package is extension code, not metadata. Discovery therefore reads host-owned
    bundled manifests or a drop-in's manifest.json and never imports __init__.py. A drop-in without
    a manifest is warned about and skipped, and a malformed drop-in is quarantined on its own, so
    unrelated extension files cannot hide bundled or otherwise valid providers.

    Returns:
        One ``ProviderInfo`` per bundled provider, sorted by ``id`` so the UI order is stable.

    Raises:
        RuntimeError: A package in this tree does not follow the convention — no ``provider``
            attribute, or an ``id`` that disagrees with its directory name. A merge gate, not a
            runtime condition: it means a bundled provider is malformed.
    """
    infos = [_info(name, reviewed=True) for name in _bundled_package_names()]
    drop_ins = _discover_drop_ins(drop_in_dir)
    duplicate_ids = {info.id for info in infos} & {info.id for info in drop_ins}
    if duplicate_ids:
        raise RuntimeError(
            "drop-in provider ids conflict with bundled providers: "
            + ", ".join(sorted(duplicate_ids))
        )
    infos.extend(drop_ins)
    return sorted(infos, key=lambda info: info.id)


def load_provider(provider_id: str, drop_in_dir: Path | None = None) -> Provider:
    """Return the provider object for ``provider_id``.

    Args:
        provider_id: The slug, equal to the package name.

    Returns:
        The module-level ``provider`` object. It has not been asked to do anything yet.

    Raises:
        LookupError: No installed provider has that id.
        RuntimeError: The package exists but breaks the convention (see ``discover_providers``).
    """
    for info in discover_providers(drop_in_dir):
        if info.id == provider_id:
            if info.module.startswith(f"{_DROPIN_NAMESPACE}."):
                if drop_in_dir is None:
                    raise LookupError(f"no provider with id {provider_id!r} is installed")
                _drop_in_module_path(drop_in_dir / provider_id)
            return _provider_object(info.module, provider_id)
    raise LookupError(f"no provider with id {provider_id!r} is installed")


def _bundled_package_names() -> list[str]:
    """Provider package names in this tree: directories only, no private names.

    Restricting discovery to packages is what keeps host modules in this tree (``base``, ``http``,
    ``registry``) from being mistaken for providers, without maintaining a deny-list that a new host
    module would silently fall off.
    """
    return [
        name
        for _finder, name, is_package in pkgutil.iter_modules([str(_PROVIDERS_DIR)])
        if is_package and not name.startswith("_")
    ]


def _drop_in_package_paths(drop_in_dir: Path | None) -> list[Path]:
    """Packages immediately inside the operator-owned directory, if it exists.

    A missing directory is equivalent to no drop-ins: it is normal on a fresh install.  A present
    non-directory is a configuration error, because silently ignoring a path the operator supplied
    would make a provider appear installed while never being loadable.
    """
    if drop_in_dir is None:
        return []
    if not drop_in_dir.exists():
        return []
    if not drop_in_dir.is_dir():
        raise RuntimeError(f"provider_dir is not a directory: {drop_in_dir}")
    return sorted(
        (
            path
            for path in drop_in_dir.iterdir()
            if path.is_dir() and (path / "__init__.py").is_file()
        ),
        key=lambda path: path.name,
    )


def _discover_drop_ins(drop_in_dir: Path | None) -> list[ProviderInfo]:
    """Parse each candidate independently, quarantining only malformed metadata.

    Directory validation remains outside this helper: a configured provider directory that is not a
    directory is still an error, and only immediate package directories with ``__init__.py`` are
    candidates. The duplicate-id check also remains in :func:`discover_providers`, after all valid
    metadata has been collected, so a valid shadowing drop-in cannot be hidden by this quarantine.
    """
    infos: list[ProviderInfo] = []
    for path in _drop_in_package_paths(drop_in_dir):
        try:
            infos.append(_drop_in_info(path))
        except (RuntimeError, UnicodeError) as exc:
            warnings.warn(
                f"skipping drop-in provider {path.name!r}: {exc}",
                UserWarning,
                stacklevel=2,
            )
    return infos


def _drop_in_info(path: Path) -> ProviderInfo:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"manifest.json is required: {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid drop-in provider manifest: {manifest_path}") from exc
    return _manifest_info(
        raw,
        provider_id=path.name,
        module=_drop_in_module_name(path),
        reviewed=False,
    )


def _drop_in_module_name(path: Path) -> str:
    """Return a drop-in module name without importing the package."""
    return f"{_DROPIN_NAMESPACE}.{path.name}"


def _drop_in_module_path(path: Path) -> str:
    """Load a package under a host-owned namespace without putting its directory on ``sys.path``."""
    module_path = _drop_in_module_name(path)
    if module_path in sys.modules:
        return module_path
    namespace = sys.modules.get(_DROPIN_NAMESPACE)
    if namespace is None:
        namespace = ModuleType(_DROPIN_NAMESPACE)
        namespace.__path__ = []  # namespace-package marker
        sys.modules[_DROPIN_NAMESPACE] = namespace
    spec = importlib.util.spec_from_file_location(
        module_path, path / "__init__.py", submodule_search_locations=[str(path)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load drop-in provider package at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_path] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_path, None)
        raise
    return module_path


def _info(package_name: str, *, reviewed: bool) -> ProviderInfo:
    module_path = f"{__package__}.{package_name}"
    try:
        raw = BUNDLED_MANIFESTS[package_name]
    except KeyError as exc:
        raise RuntimeError(
            f"bundled provider {package_name!r} has no static manifest; add it to "
            "aggregato/providers/manifest.py"
        ) from exc
    return _manifest_info(raw, provider_id=package_name, module=module_path, reviewed=reviewed)


def _manifest_info(raw: object, *, provider_id: str, module: str, reviewed: bool) -> ProviderInfo:
    """Validate and convert a JSON/static manifest without executing extension code."""
    if not isinstance(raw, dict):
        raise RuntimeError(f"provider manifest for {provider_id!r} must be an object")
    required = {
        "name",
        "media_types",
        "capabilities",
        "acquisition",
        "schema_version",
        "default_poll_interval_seconds",
        "config_schema",
    }
    if not required <= raw.keys():
        missing = ", ".join(sorted(required - raw.keys()))
        raise RuntimeError(f"provider manifest for {provider_id!r} is missing: {missing}")
    if not isinstance(raw["name"], str) or not raw["name"]:
        raise RuntimeError(f"provider manifest for {provider_id!r} has an invalid name")
    if not isinstance(raw["media_types"], (list, tuple)) or not all(
        isinstance(value, str) for value in raw["media_types"]
    ):
        raise RuntimeError(f"provider manifest for {provider_id!r} has invalid media_types")
    if not isinstance(raw["capabilities"], (list, tuple)) or not all(
        isinstance(value, str) for value in raw["capabilities"]
    ):
        raise RuntimeError(f"provider manifest for {provider_id!r} has invalid capabilities")
    if not isinstance(raw["config_schema"], dict):
        raise RuntimeError(f"provider manifest for {provider_id!r} has an invalid config_schema")
    interval = raw["default_poll_interval_seconds"]
    version = raw["schema_version"]
    if (
        not isinstance(interval, int)
        or interval <= 0
        or not isinstance(version, int)
        or version < 1
    ):
        raise RuntimeError(f"provider manifest for {provider_id!r} has invalid scheduling metadata")
    return ProviderInfo(
        id=provider_id,
        name=raw["name"],
        module=module,
        media_types=frozenset(raw["media_types"]),
        capabilities=frozenset(raw["capabilities"]),
        acquisition=str(raw["acquisition"]),
        schema_version=version,
        default_poll_interval=timedelta(seconds=interval),
        reviewed=reviewed,
        api_visible=bool(raw.get("api_visible", True)),
        config_schema=copy.deepcopy(raw["config_schema"]),
        rating_scales=tuple(raw.get("rating_scales", ())),
        import_inference=(
            raw.get("import_inference") if isinstance(raw.get("import_inference"), str) else None
        ),
    )


def _provider_object(module_path: str, expected_id: str) -> Provider:
    module = importlib.import_module(module_path)
    provider = getattr(module, PROVIDER_ATTR, None)
    if provider is None:
        raise RuntimeError(
            f"{module_path} exposes no `{PROVIDER_ATTR}` attribute; a provider package must define "
            f"a module-level `{PROVIDER_ATTR}` object (see aggregato/providers/registry.py)"
        )
    if not isinstance(provider, Provider):
        raise RuntimeError(
            f"{module_path}.{PROVIDER_ATTR} does not implement the Provider protocol"
        )
    if provider.id != expected_id:
        raise RuntimeError(
            f"{module_path}.{PROVIDER_ATTR}.id is {provider.id!r} but the package is named "
            f"{expected_id!r}; the slug and the directory must agree"
        )
    return provider


PROVIDER_API_VERSION = 1
"""The extension API version implemented by this host (contract provider API 1.0)."""

_DROPIN_NAMESPACE = "aggregato.dropins"
