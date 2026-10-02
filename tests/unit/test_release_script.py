from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
GIT = shutil.which("git")
if GIT is None:
    raise RuntimeError("git is required for the release script tests")


def run_command(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)  # noqa: S603


def git(repo: Path, *args: str) -> str:
    result = run_command(
        [GIT, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_release_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    (repo / "frontend").mkdir()
    (repo / "scripts/release.sh").write_bytes((ROOT / "scripts/release.sh").read_bytes())
    (repo / "scripts/release.sh").chmod(0o755)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "aggregato"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (repo / "frontend/package.json").write_text(
        '{\n  "name": "aggregato-frontend",\n  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )
    (repo / "frontend/package-lock.json").write_text(
        '{\n  "version": "0.1.0",\n  "packages": {\n    "": {"version": "0.1.0"}\n  }\n}\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "aggregato"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "That pulls the published backend and frontend `0.1.0` images.\n"
        "Pin it with `AGGREGATO_VERSION=0.1.0`.\n",
        encoding="utf-8",
    )
    (repo / "docker").mkdir()
    (repo / "docker/compose.yml").write_text(
        "# pin with AGGREGATO_VERSION=0.1.0\n"
        "image: ghcr.io/minipps/aggregato:${AGGREGATO_VERSION:-0.1.0}\n"
        "image: ghcr.io/minipps/aggregato-frontend:${AGGREGATO_VERSION:-0.1.0}\n",
        encoding="utf-8",
    )

    identity = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
    }
    run_command([GIT, "init", "-q", "-b", "main"], cwd=repo, check=True, env=identity)
    run_command([GIT, "add", "."], cwd=repo, check=True, env=identity)
    run_command([GIT, "commit", "-q", "-m", "initial"], cwd=repo, check=True, env=identity)
    origin = tmp_path / "origin.git"
    run_command([GIT, "init", "-q", "--bare", str(origin)], check=True, env=identity)
    run_command([GIT, "remote", "add", "origin", str(origin)], cwd=repo, check=True)
    run_command([GIT, "push", "-q", "-u", "origin", "main"], cwd=repo, check=True, env=identity)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    (fake_bin / "git").write_text(
        "#!/bin/sh\n"
        'if [ "${RELEASE_FAIL_PUSH:-}" = 1 ] && [ "$1" = push ] && [ "$2" = --quiet ] '
        " ; then\n"
        '    echo "simulated push failure" >&2\n'
        "    exit 42\n"
        "fi\n"
        f'exec {shlex.quote(real_git)} "$@"\n',
        encoding="utf-8",
    )
    (fake_bin / "git").chmod(0o755)
    (fake_bin / "npm").write_text(
        "#!/bin/sh\n"
        'new="$2"\n'
        'sed -i "s/\\"version\\": \\"0.1.0\\"/\\"version\\": \\"$new\\"/g" '
        "package.json package-lock.json\n",
        encoding="utf-8",
    )
    (fake_bin / "npm").chmod(0o755)
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'new=$(sed -n \'s/^version = "\\([^" ]*\\)"$/\\1/p\' pyproject.toml | head -n 1)\n'
        'sed -i "s/^version = \\"0.1.0\\"$/version = \\"$new\\"/" uv.lock\n',
        encoding="utf-8",
    )
    (fake_bin / "uv").chmod(0o755)
    (fake_bin / "gh").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$RELEASE_PR_ARGS"\n[ "${RELEASE_FAIL_PR:-}" != 1 ]\n',
        encoding="utf-8",
    )
    (fake_bin / "gh").chmod(0o755)

    tracked = {
        path: (repo / path).read_bytes()
        for path in (
            "README.md",
            "docker/compose.yml",
            "pyproject.toml",
            "frontend/package.json",
            "frontend/package-lock.json",
            "uv.lock",
        )
    }
    return repo, fake_bin, tracked


def run_release(
    repo: Path,
    fake_bin: Path,
    *,
    fail_push: bool = False,
    fail_pr: bool = False,
    tag: bool = False,
    version: str = "0.1.1",
    reply: str = "y\n",
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
        "RELEASE_PR_ARGS": str(fake_bin / "pr-args.txt"),
    }
    if fail_push:
        env["RELEASE_FAIL_PUSH"] = "1"
    if fail_pr:
        env["RELEASE_FAIL_PR"] = "1"
    return run_command(
        [str(repo / "scripts/release.sh"), *(["--tag"] if tag else []), version],
        cwd=repo,
        env=env,
        input=reply,
        capture_output=True,
        text=True,
    )


def test_release_updates_both_documented_image_tags(tmp_path: Path) -> None:
    repo, fake_bin, _ = make_release_fixture(tmp_path)
    base_commit = git(repo, "rev-parse", "main")

    result = run_release(repo, fake_bin)

    assert result.returncode == 0, result.stderr
    assert "published backend and frontend `0.1.1` images" in (repo / "README.md").read_text()
    compose = (repo / "docker/compose.yml").read_text()
    assert compose.count("${AGGREGATO_VERSION:-0.1.1}") == 2
    assert json.loads((repo / "frontend/package.json").read_text())["version"] == "0.1.1"
    assert 'version = "0.1.1"' in (repo / "pyproject.toml").read_text()
    assert 'version = "0.1.1"' in (repo / "uv.lock").read_text()
    assert git(repo, "branch", "--show-current") == "release/v0.1.1"
    assert git(repo, "rev-parse", "main") == base_commit
    assert git(repo, "ls-remote", "origin", "refs/heads/main").startswith(base_commit)
    assert not git(repo, "ls-remote", "--tags", "origin")
    args = (fake_bin / "pr-args.txt").read_text().splitlines()
    assert args[:6] == ["pr", "create", "--base", "main", "--head", "release/v0.1.1"]


def test_release_rolls_back_version_files_when_push_fails(tmp_path: Path) -> None:
    repo, fake_bin, original = make_release_fixture(tmp_path)
    base_commit = git(repo, "rev-parse", "HEAD")

    result = run_release(repo, fake_bin, fail_push=True)

    assert result.returncode != 0
    assert "release: failed, release changes rolled back" in result.stderr
    assert git(repo, "rev-parse", "HEAD") == base_commit
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--show-current") == "main"
    assert not git(repo, "tag", "--list")
    for path, content in original.items():
        assert (repo / path).read_bytes() == content


def test_release_restores_main_when_pr_creation_fails(tmp_path: Path) -> None:
    repo, fake_bin, original = make_release_fixture(tmp_path)
    base_commit = git(repo, "rev-parse", "HEAD")

    result = run_release(repo, fake_bin, fail_pr=True)

    assert result.returncode != 0
    assert "remote branch was not rolled back" in result.stderr
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == base_commit
    assert git(repo, "status", "--porcelain") == ""
    assert not git(repo, "ls-remote", "--tags", "origin")
    for path, content in original.items():
        assert (repo / path).read_bytes() == content


def test_release_only_tags_a_merged_version_bump(tmp_path: Path) -> None:
    repo, fake_bin, _ = make_release_fixture(tmp_path)
    prepared = run_release(repo, fake_bin)
    assert prepared.returncode == 0, prepared.stderr
    git(repo, "switch", "main")
    git(repo, "merge", "--ff-only", "release/v0.1.1")
    git(repo, "push", "origin", "main")
    merged_commit = git(repo, "rev-parse", "HEAD")

    result = run_release(repo, fake_bin, tag=True)

    assert result.returncode == 0, result.stderr
    assert git(repo, "rev-parse", "v0.1.1") == merged_commit
    assert git(repo, "ls-remote", "--tags", "origin").startswith(merged_commit)
    assert git(repo, "status", "--porcelain") == ""


def test_release_rejects_tag_with_unmerged_version(tmp_path: Path) -> None:
    repo, fake_bin, _ = make_release_fixture(tmp_path)

    result = run_release(repo, fake_bin, tag=True)

    assert result.returncode != 0
    assert "disagrees" in result.stderr
    assert not git(repo, "tag", "--list")
    assert not git(repo, "ls-remote", "--tags", "origin")
    assert git(repo, "status", "--porcelain") == ""


def test_release_removes_local_tag_when_tag_push_fails(tmp_path: Path) -> None:
    repo, fake_bin, _ = make_release_fixture(tmp_path)

    result = run_release(repo, fake_bin, tag=True, version="0.1.0", fail_push=True)

    assert result.returncode != 0
    assert not git(repo, "tag", "--list")
    assert git(repo, "status", "--porcelain") == ""
