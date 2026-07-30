"""Discovery is inert (SC-013) and the bundled provider follows the documented convention.

The point of these tests is not that discovery works — it is that discovery does *nothing*. A fresh
install makes zero outbound requests, and the only way that stays true is if listing providers, and
importing them, cannot reach out. ``conftest.block_sockets`` is autouse, so the network half is
covered for free; the file-I/O half is trapped explicitly below.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from aggregato.domain.enums import Acquisition, Capability, FetchMode, MediaType
from aggregato.domain.models import Checkpoint, Cursor, RawRecord
from aggregato.providers.base import Provider, ProviderContext
from aggregato.providers.registry import (
    PROVIDER_ATTR,
    ProviderInfo,
    discover_providers,
    load_provider,
)


def _info(provider_id: str) -> ProviderInfo:
    matches = [info for info in discover_providers() if info.id == provider_id]
    assert matches, f"{provider_id} was not discovered"
    return matches[0]


def test_discovery_finds_the_fixture_provider() -> None:
    info = _info("fixture")
    assert info.module == "aggregato.providers.fixture"
    assert info.acquisition == Acquisition.EXPORT
    assert info.schema_version >= 1
    assert info.default_poll_interval > timedelta(0)
    assert MediaType.FILM in info.media_types
    assert Capability.POLL in info.capabilities


def test_bundled_providers_are_reviewed() -> None:
    # FR-041: bundled providers ship with the core and went through review; drop-ins (T124) will
    # arrive with reviewed=False and a UI label.
    assert [info.id for info in discover_providers() if not info.reviewed] == []


def test_discovery_is_sorted_and_lists_only_packages() -> None:
    ids = [info.id for info in discover_providers()]
    assert ids == sorted(ids)
    # base.py, errors.py and registry.py live in the provider tree but are host code.
    assert not {"base", "errors", "registry", "http"} & set(ids)


def test_conventions_hold_for_the_bundled_provider() -> None:
    provider = load_provider("fixture")
    module = sys.modules["aggregato.providers.fixture"]
    assert getattr(module, PROVIDER_ATTR) is provider  # convention 2: module-level `provider`
    assert provider.id == "fixture"  # convention 3: slug == package name
    assert isinstance(provider, Provider)
    assert provider.rating_scales, "a provider that declares has_ratings needs a scale"
    assert provider.config_model.model_json_schema()["properties"], "config_model renders a form"


def test_unknown_provider_id_is_a_lookup_error() -> None:
    with pytest.raises(LookupError):
        load_provider("not-a-provider")


def test_importing_a_provider_module_has_no_side_effects() -> None:
    """A discovered-but-not-enabled provider does no work: no sockets, no files, no clock.

    Re-imports the provider package with ``open`` and ``Path.read_text`` trapped, so import-time or
    construction-time I/O fails loudly instead of being invisible until a fresh install phones home.
    """

    def trap(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("provider discovery performed I/O")

    with pytest.MonkeyPatch.context() as patch:
        for name in [m for m in sys.modules if m.startswith("aggregato.providers.fixture")]:
            patch.delitem(sys.modules, name)
        patch.setattr(builtins, "open", trap)
        patch.setattr(Path, "read_text", trap)
        patch.setattr(Path, "open", trap)
        info = _info("fixture")

    assert info.id == "fixture"


# --- The fixture provider is the thing discovery finds, so its two non-trivial paths are checked
# --- here too: cursor resumption and normalize purity (contract §5 groups 3 and 6).


@pytest.fixture
def ctx(fixtures_dir: Path) -> Iterator[ProviderContext]:
    from aggregato.providers.fixture import FixtureConfig

    yield ProviderContext(
        http=None,  # type: ignore[arg-type]  # no code path in this provider touches HTTP
        config=FixtureConfig(path=fixtures_dir / "fixture" / "log-two-pages.jsonl"),
        secrets={},
        log=__import__("logging").getLogger("test"),
        state={},
    )


async def _drain(
    provider: Provider, ctx: ProviderContext, cursor: Cursor | None
) -> tuple[list[RawRecord], list[Cursor]]:
    records: list[RawRecord] = []
    cursors: list[Cursor] = []
    async for item in provider.fetch(ctx, cursor, FetchMode.INCREMENTAL):
        if isinstance(item, Checkpoint):
            cursors.append(item.cursor)
        else:
            records.append(item)
    return records, cursors


async def test_fetch_checkpoints_between_records_and_resumes(ctx: ProviderContext) -> None:
    provider = load_provider("fixture")
    records, cursors = await _drain(provider, ctx, None)
    assert len(cursors) == 2, "one checkpoint per page, or resumption is untested"
    assert [r.native_id for r in records] == [f"rec-000{n}" for n in range(1, 6)]

    resumed, _ = await _drain(provider, ctx, cursors[0])
    # No duplicate and no gap: resuming at the first checkpoint replays exactly the second page.
    assert [r.native_id for r in resumed] == ["rec-0004", "rec-0005"]


async def test_normalize_is_pure_and_extracts_everything(ctx: ProviderContext) -> None:
    provider = load_provider("fixture")
    records, _ = await _drain(provider, ctx, None)
    by_id = {r.native_id: r for r in records}

    batch = provider.normalize(by_id["rec-0001"])
    assert provider.normalize(by_id["rec-0001"]) == batch  # called twice, identical output
    assert {e.namespace for e in batch.external_ids} == set(by_id["rec-0001"].payload["ids"])
    assert [c.role_raw for c in batch.credits] == ["Directed by", "Screenplay"]
    assert batch.opinions[0].rating_scale_id in {s.id for s in provider.rating_scales}

    assert provider.normalize(by_id["rec-0002"]).entries[0].subject_ref is not None
    assert provider.normalize(by_id["rec-0003"]).entries[0].logged_precision == "month"


async def test_check_reports_an_unreadable_file_rather_than_success(
    ctx: ProviderContext, fixtures_dir: Path
) -> None:
    from aggregato.providers.fixture import FixtureConfig

    provider = load_provider("fixture")
    assert (await provider.check(ctx)).ok

    broken = ProviderContext(
        http=ctx.http,
        config=FixtureConfig(path=fixtures_dir / "fixture" / "unreadable-not-json.jsonl"),
        secrets={},
        log=ctx.log,
        state={},
    )
    result = await provider.check(broken)
    assert not result.ok and result.error_class is not None
