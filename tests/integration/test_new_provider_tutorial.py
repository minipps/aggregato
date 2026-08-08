"""The authoring guide's smallest useful promise: a provider is a drop-in, not a core edit."""

from __future__ import annotations

import json
from pathlib import Path

from aggregato.providers.registry import discover_providers, load_provider


def test_throwaway_provider_from_the_guide_needs_no_core_file(tmp_path: Path) -> None:
    """Create the documented package externally and prove discovery loads it as unreviewed."""
    core_root = Path(__file__).parents[2] / "aggregato"
    before = {path: path.stat().st_mtime_ns for path in core_root.rglob("*.py")}

    package = tmp_path / "tutorial_provider"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from aggregato.providers.fixture import FixtureProvider\n"
        "provider = FixtureProvider()\n"
        "provider.id = 'tutorial_provider'\n"
    )
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Tutorial provider",
                "media_types": [],
                "capabilities": [],
                "acquisition": "export",
                "schema_version": 1,
                "default_poll_interval_seconds": 3600,
                "config_schema": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )

    info = next(info for info in discover_providers(tmp_path) if info.id == "tutorial_provider")
    assert info.reviewed is False
    assert load_provider("tutorial_provider", tmp_path).id == "tutorial_provider"
    assert {path: path.stat().st_mtime_ns for path in core_root.rglob("*.py")} == before
