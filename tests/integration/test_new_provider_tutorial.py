"""The authoring guide's smallest useful promise: a provider is a drop-in, not a core edit."""

from __future__ import annotations

from pathlib import Path

from aggregato.providers.registry import PROVIDER_API_VERSION, discover_providers, load_provider


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
        f"provider_api_version = {PROVIDER_API_VERSION}\n"
    )

    info = next(info for info in discover_providers(tmp_path) if info.id == "tutorial_provider")
    assert info.reviewed is False
    assert load_provider("tutorial_provider", tmp_path).id == "tutorial_provider"
    assert {path: path.stat().st_mtime_ns for path in core_root.rglob("*.py")} == before
