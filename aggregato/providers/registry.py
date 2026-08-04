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

Bundled providers are ``reviewed=True`` because they ship with the core and went through review
. Drop-in providers, discovered from an operator directory, are ``reviewed=False`` and the
UI labels them — that directory scan is a later task  and deliberately absent here.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
import warnings
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import ModuleType

from .base import Provider

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


def discover_providers(drop_in_dir: Path | None = None) -> list[ProviderInfo]:
    """List the provider packages bundled in this tree.

    Imports each provider package to read its declared metadata — which is safe precisely because
    convention 4 forbids import-time work. Nothing is fetched, nothing is enabled and no network
    connection is opened .

    Returns:
        One ``ProviderInfo`` per bundled provider, sorted by ``id`` so the UI order is stable.

    Raises:
        RuntimeError: A package in this tree does not follow the convention — no ``provider``
            attribute, or an ``id`` that disagrees with its directory name. A merge gate, not a
            runtime condition: it means a bundled provider is malformed.
    """
    infos = [_info(name, reviewed=True) for name in _bundled_package_names()]
    drop_ins = [_drop_in_info(path) for path in _drop_in_package_paths(drop_in_dir)]
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


def _drop_in_info(path: Path) -> ProviderInfo:
    provider = _provider_object(_drop_in_module_path(path), path.name)
    declared_version = getattr(sys.modules[_drop_in_module_path(path)], "provider_api_version", 1)
    if declared_version != PROVIDER_API_VERSION:
        warnings.warn(
            f"drop-in provider {provider.id!r} declares provider API {declared_version!r}; "
            f"this host implements {PROVIDER_API_VERSION}. It may be incompatible.",
            stacklevel=2,
        )
    return ProviderInfo(
        id=provider.id,
        name=provider.name,
        module=_drop_in_module_path(path),
        media_types=frozenset(str(m) for m in provider.media_types),
        capabilities=frozenset(str(c) for c in provider.capabilities),
        acquisition=str(provider.acquisition),
        schema_version=provider.schema_version,
        default_poll_interval=provider.default_poll_interval,
        reviewed=False,
    )


def _drop_in_module_path(path: Path) -> str:
    """Load a package under a host-owned namespace without putting its directory on ``sys.path``."""
    module_path = f"{_DROPIN_NAMESPACE}.{path.name}"
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
    provider = _provider_object(module_path, package_name)
    return ProviderInfo(
        id=provider.id,
        name=provider.name,
        module=module_path,
        # Enum members stringify to their values (StrEnum), and ProviderInfo is pure declared
        # metadata that the API serializes — so it carries the wire strings, not the enum types.
        media_types=frozenset(str(m) for m in provider.media_types),
        capabilities=frozenset(str(c) for c in provider.capabilities),
        acquisition=str(provider.acquisition),
        schema_version=provider.schema_version,
        default_poll_interval=provider.default_poll_interval,
        reviewed=reviewed,
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
