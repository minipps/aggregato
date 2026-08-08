#!/bin/sh
# Cut a release: bump the version files, verify release examples, commit, tag, push.
#
# The tag is the release procedure (.github/workflows/release.yml) — pushing it runs the CI gates,
# publishes a source archive and a GitHub release, and pushes multi-arch images to ghcr tagged
# `x.y.z`, `x.y` and `latest`. What this script adds is the part that is easy to get wrong by hand:
# the workflow refuses a tag whose version disagrees with pyproject.toml, frontend/package.json,
# uv.lock, or the documented image markers, and that refusal happens *after* the tag is public, so it
# has to be caught here instead.
#
# Everything before the push is local and reversible. The guards are the point: a release cut from a
# stale main, or with an unrelated edit swept into the bump commit, ships something nobody reviewed.
set -eu

usage() {
    echo "usage: scripts/release.sh <version>    e.g. scripts/release.sh 0.1.1" >&2
    exit 2
}

[ $# -eq 1 ] || usage
version=$1
# The image tags are derived by docker/metadata-action's semver patterns, which need three parts.
echo "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || {
    echo "release: '$version' is not a three-part version (x.y.z)" >&2
    exit 2
}
tag="v$version"

cd "$(dirname "$0")/.."

# --- Refuse to start from a state that would release the wrong thing ---------------------------

[ -z "$(git status --porcelain)" ] || {
    echo "release: working tree is not clean; commit or stash first" >&2
    echo "         (the bump commit would otherwise carry unrelated changes into the release)" >&2
    exit 1
}

branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = main ] || {
    echo "release: on '$branch', not main" >&2
    exit 1
}

git fetch --quiet origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || {
    echo "release: main and origin/main disagree; pull or push first" >&2
    echo "         (a tag cut from a stale main releases code that is not what CI tested)" >&2
    exit 1
}

# Locally and on the remote: a tag that exists either place cannot be moved without rewriting a
# published release, so this is a hard stop rather than a prompt.
if git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null ||
    [ -n "$(git ls-remote --tags origin "refs/tags/$tag")" ]; then
    echo "release: tag $tag already exists" >&2
    exit 1
fi

# The preflight above is deliberately outside this transaction: it has not changed anything. From
# here on, every failure must leave the worktree as it was before the bump, including failures after
# the commit or local tag have been created.
base_commit=$(git rev-parse HEAD)
bump_started=1
release_commit_created=0
main_pushed=0
tag_created=0
release_complete=0
abort_requested=0

rollback() {
    status=$?
    trap - 0

    if [ "$release_complete" -eq 0 ] && [ "$bump_started" -eq 1 ]; then
        # The script refuses a dirty worktree before this point, so restoring the base commit cannot
        # discard anything belonging to the caller. A hard reset is needed once commit has run;
        # checkout alone would leave a failed release commit behind.
        if [ "$release_commit_created" -eq 1 ] || [ "$(git rev-parse HEAD)" != "$base_commit" ]; then
            if ! git reset --hard "$base_commit"; then
                echo "release: could not restore the pre-release commit" >&2
            fi
        elif ! git checkout -- README.md docker/compose.yml pyproject.toml \
            frontend/package.json frontend/package-lock.json uv.lock; then
            echo "release: could not restore the version files" >&2
        fi

        if [ "$tag_created" -eq 1 ]; then
            git tag --delete "$tag" >/dev/null 2>&1 ||
                echo "release: could not remove local tag $tag" >&2
        fi

        if [ "$main_pushed" -eq 1 ]; then
            echo "release: main was pushed before the failure; the remote commit was not rolled back" >&2
        fi
        if [ "$abort_requested" -eq 1 ]; then
            echo "release: aborted, version files restored" >&2
        else
            echo "release: failed, release changes rolled back" >&2
        fi
    fi

    exit "$status"
}
trap rollback 0

# --- Bump ---------------------------------------------------------------------------------------

# Anchored to the line-start `version` key, which only the [project] table has, and asserting
# exactly one substitution: a silent zero-match bump would tag a version nobody declared.
python3 - "$version" <<'PY'
import pathlib
import re
import sys

new = sys.argv[1]
path = pathlib.Path("pyproject.toml")
updated, count = re.subn(
    r'(?m)^version = "[^"]+"$', f'version = "{new}"', path.read_text(), count=1
)
if count != 1:
    sys.exit("release: found no top-level version line in pyproject.toml")
path.write_text(updated)
PY

# npm's own tool rather than a second sed: it updates package-lock.json too, in both the places the
# root package appears. The CI gate only reads package.json, so a hand-edited bump passes review
# while leaving the lockfile claiming the old version.
(cd frontend && npm version "$version" --no-git-tag-version >/dev/null)

# uv.lock pins the root project's own version alongside every dependency, and nothing else in this
# script would touch it — which is how v0.1.1 shipped with a lockfile still claiming 0.1.0. Left
# stale it dirties the working tree for whoever next runs `uv sync`, and `--frozen` in the Dockerfile
# means the image build will not notice. No `--upgrade`: this re-locks the version key, and a release
# is not the moment to move a dependency nobody reviewed.
uv lock --quiet

# Keep the release examples in step with the image that the release will publish. Each pattern is
# deliberately narrow and must match the expected number of times: a missing match means an example
# drifted, while an unexpected extra match means the script would be leaving another marker behind.
python3 - "$version" <<'PY'
import pathlib
import re
import sys

new = sys.argv[1]
semver = r"[0-9]+\.[0-9]+\.[0-9]+"
updates = {
    pathlib.Path("README.md"): (
        (
            "published image version",
            rf"published backend and frontend `{semver}` images",
            f"published backend and frontend `{new}` images",
        ),
        (
            "README image pin",
            rf"AGGREGATO_VERSION={semver}",
            f"AGGREGATO_VERSION={new}",
        ),
    ),
    pathlib.Path("docker/compose.yml"): (
        (
            "Compose image pin comment",
            rf"AGGREGATO_VERSION={semver}",
            f"AGGREGATO_VERSION={new}",
        ),
        (
            "Compose default image tag",
            rf"\$\{{AGGREGATO_VERSION:-{semver}\}}",
            f"${{AGGREGATO_VERSION:-{new}}}",
            2,
        ),
    ),
}

changed: dict[pathlib.Path, str] = {}
for path, rules in updates.items():
    text = path.read_text(encoding="utf-8")
    for rule in rules:
        expected = rule[3] if len(rule) == 4 else 1
        label, pattern, replacement = rule[:3]
        text, count = re.subn(pattern, replacement, text)
        if count != expected:
            sys.exit(f"release: expected {expected} {label} in {path}, found {count}")
    changed[path] = text

for path, text in changed.items():
    path.write_text(text, encoding="utf-8")
PY

# The release workflow's version gate, run here so a disagreement fails before the tag is public.
python3 - "$version" <<'PY'
import json
import pathlib
import sys
import tomllib

tag = sys.argv[1]
project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"]
frontend = json.loads(pathlib.Path("frontend/package.json").read_text())["version"]
packages = tomllib.loads(pathlib.Path("uv.lock").read_text())["package"]
locked = next(p["version"] for p in packages if p["name"] == "aggregato")
if {project, frontend, locked} != {tag}:
    sys.exit(
        f"release: v{tag} disagrees: pyproject {project}, frontend {frontend}, uv.lock {locked}"
    )
expected_examples = {
    pathlib.Path("README.md"): f"AGGREGATO_VERSION={tag}",
    pathlib.Path("docker/compose.yml"): f"AGGREGATO_VERSION={tag}",
}
missing = [
    str(path) for path, marker in expected_examples.items() if marker not in path.read_text()
]
if missing:
    sys.exit(f"release: examples do not name v{tag}: {', '.join(missing)}")
PY

# --- Confirm, then publish ----------------------------------------------------------------------

previous=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
echo "Releasing $tag${previous:+, $(git rev-list --count "$previous"..HEAD) commits since $previous}:"
echo
git --no-pager diff --stat
echo
[ -n "$previous" ] && git --no-pager log --oneline --no-merges "$previous"..HEAD | sed 's/^/  /'
echo
echo "Pushing the tag publishes a GitHub release and ghcr images tagged $version, ${version%.*} and latest."
printf 'Continue? [y/N] '
read -r reply
case "$reply" in
    y | Y) ;;
    *)
        abort_requested=1
        exit 1
        ;;
esac

git commit --quiet -am "chore: release $version"
release_commit_created=1
# main before the tag: if the branch is protected, this fails while the tag is still local and the
# only cleanup needed is `git reset --hard origin/main`.
git push --quiet origin main
main_pushed=1
git tag "$tag"
tag_created=1
git push --quiet origin "$tag"
release_complete=1

echo "release: pushed $tag — https://github.com/minipps/aggregato/actions"
