"""Keep executable CI code separate from publishing credentials."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.unit.test_release_script import git, make_release_fixture

ROOT = Path(__file__).parents[2]


def test_workflows_keep_permissions_and_actions_restricted() -> None:
    workflows = {
        path.stem: yaml.safe_load(path.read_text())
        for path in (ROOT / ".github/workflows").glob("*.yml")
    }
    assert set(workflows) == {"ci", "release", "pages"}
    for workflow in workflows.values():
        assert workflow["permissions"] == {"contents": "read"}
        assert not set(workflow.get("on", workflow.get(True, {}))) & {
            "pull_request_target",
            "workflow_run",
            "issue_comment",
        }
        for job in workflow["jobs"].values():
            assert job.get("runs-on", "ubuntu-latest") == "ubuntu-latest"
            for step in job.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", step["uses"])
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False

    for job in workflows["ci"]["jobs"].values():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}
    release = workflows["release"]["jobs"]
    assert release["preflight"].get("permissions", {"contents": "read"}) == {"contents": "read"}
    assert release["gates"]["permissions"] == {"contents": "read"}
    assert release["gates"]["needs"] == "preflight"
    assert release["release"]["needs"] == "gates"
    assert release["image"]["needs"] == ["gates", "release"]
    assert release["release"]["permissions"] == {"contents": "write"}
    assert release["image"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert release["release"]["environment"] == release["image"]["environment"] == "release"
    assert workflows["pages"]["jobs"]["deploy"]["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert (ROOT / ".github/CODEOWNERS").read_text().strip() == "* @minipps"


@pytest.mark.parametrize(
    "on_main,tag,allowed",
    [(True, "v0.1.0", True), (False, "v0.1.0", False), (True, "vgarbage", False)],
)
def test_release_preflight_rejects_unreviewed_commits_and_invalid_tags(
    tmp_path: Path, on_main: bool, tag: str, allowed: bool
) -> None:
    repo, _, _ = make_release_fixture(tmp_path)
    if not on_main:
        git(repo, "switch", "-c", "unreviewed")
        git(
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "unreviewed",
        )
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    guard = workflow["jobs"]["preflight"]["steps"][1]["run"]

    result = subprocess.run(  # noqa: S603
        ["bash", "-e", "-c", guard],  # noqa: S607
        cwd=repo,
        env={**os.environ, "GITHUB_SHA": git(repo, "rev-parse", "HEAD"), "GITHUB_REF_NAME": tag},
        capture_output=True,
        text=True,
    )

    assert (result.returncode == 0) is allowed
