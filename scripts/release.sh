#!/usr/bin/env bash
# Cut a Kai release.
#
# This script is the source of truth for what a release IS. CI calls it too
# (.github/workflows/release.yml) so a tag-triggered build and a build you run
# by hand do the same thing — there is no second, drifting definition of
# "released" living in a YAML file.
#
#   scripts/release.sh 0.8.0             # bump, verify, build, tag locally
#   scripts/release.sh 0.8.0 --push      # ...and push the git tag + image
#   scripts/release.sh 0.8.0 --ci        # CI mode: verify not bump, no git ops
#
# What it does NOT do: touch your running container. Upgrading is a separate,
# deliberate step (docker compose pull && up -d) so a release can never restart
# something out from under you.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# The image name is independent of the GitHub repo name (which is still
# "newb_agent" for historical reasons). Override to publish somewhere else.
REGISTRY="${KAI_REGISTRY:-ghcr.io/supasoulja}"
IMAGE="${KAI_IMAGE:-kai}"

PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

# ── Args ──────────────────────────────────────────────────────────────────────
VERSION="${1:-}"
PUSH=0
CI_MODE=0
for arg in "${@:2}"; do
    case "$arg" in
        --push) PUSH=1 ;;
        --ci)   CI_MODE=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

if [ -z "$VERSION" ]; then
    echo "usage: scripts/release.sh <version> [--push] [--ci]" >&2
    echo "       e.g. scripts/release.sh 0.8.0 --push" >&2
    exit 2
fi

# Semver, so the git tag and the image tag can never be something unsortable.
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$ ]]; then
    echo "[!] '$VERSION' is not a semver version (expected e.g. 0.8.0)" >&2
    exit 2
fi

TAG="v$VERSION"
step() { echo; echo "── $* ──────────────────────────────────────────"; }

# ── 1. Preflight ──────────────────────────────────────────────────────────────
step "Preflight"

command -v docker >/dev/null || { echo "[!] docker not on PATH" >&2; exit 1; }
"$PY" -c "import build" 2>/dev/null || { echo "[!] 'build' missing: $PY -m pip install build" >&2; exit 1; }

if [ "$CI_MODE" -eq 0 ]; then
    # A dirty tree means the tag would not describe what was built.
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        echo "[!] Working tree has uncommitted changes. Commit or stash first:" >&2
        git status --short --untracked-files=no >&2
        exit 1
    fi
    if git rev-parse "$TAG" >/dev/null 2>&1; then
        echo "[!] Tag $TAG already exists. Bump the version or delete the tag." >&2
        exit 1
    fi
fi
echo "  ok — releasing $VERSION as $REGISTRY/$IMAGE:$VERSION"

# ── 2. Set (or verify) the version ────────────────────────────────────────────
# kai/__init__.py is what pyproject reads; changelog.json is what Kai narrates
# on first run after an upgrade. tests/test_packaging.py fails if they drift.
#
# Locally this BUMPS both files. In CI it only VERIFIES them: the local run is
# what creates the commit and the tag, so if a v0.8.0 tag arrives while the
# committed code still says 0.7.0, someone tagged by hand and the build should
# fail rather than quietly publish a mislabelled image.
if [ "$CI_MODE" -eq 1 ]; then
    step "Verify version is $VERSION"
else
    step "Bump version -> $VERSION"
fi

"$PY" - "$VERSION" "$CI_MODE" <<'PYEOF'
import json
import re
import sys
from datetime import date
from pathlib import Path

version, ci_mode = sys.argv[1], sys.argv[2] == "1"

init = Path("kai/__init__.py")
clog_path = Path("kai/persona/changelog.json")
src = init.read_text()
clog = json.loads(clog_path.read_text())

m = re.search(r'^__version__ = "(.*)"$', src, flags=re.M)
if not m:
    sys.exit("could not find __version__ in kai/__init__.py")
previous = m.group(1)

if ci_mode:
    problems = []
    if previous != version:
        problems.append(f"kai/__init__.py says {previous}, tag says {version}")
    if clog.get("version") != version:
        problems.append(f"changelog.json says {clog.get('version')}, tag says {version}")
    if problems:
        print("  [!] version mismatch - was this tagged without scripts/release.sh?")
        for problem in problems:
            print(f"      {problem}")
        sys.exit(1)
    print(f"  ok - both files agree on {version}")
else:
    init.write_text(re.sub(r'^__version__ = ".*"$', f'__version__ = "{version}"', src, flags=re.M))
    clog["version"] = version
    clog["updated"] = date.today().isoformat()
    clog_path.write_text(json.dumps(clog, indent=4, ensure_ascii=False) + "\n")
    print(f"  kai/__init__.py    {previous} -> {version}")
    print(f"  changelog.json     {previous} -> {version} (updated {clog['updated']})")
    if previous == version:
        print("  [!] version unchanged - is this a re-run?")
PYEOF

# The release notes are Kai's own account of what changed; nobody but you can
# write them, so warn rather than silently ship last release's list.
if [ "$CI_MODE" -eq 0 ] && git diff --quiet HEAD -- kai/persona/changelog.json 2>/dev/null; then
    echo "  [!] changelog.json notes[] unchanged - users will see the previous release's notes."
fi

# ── 3. Verify ─────────────────────────────────────────────────────────────────
step "Verify"
"$PY" -m ruff check .
"$PY" -m ruff format --check .

if [ "$CI_MODE" -eq 1 ]; then
    # A CI runner has no Ollama and no 8 GB chat model. tests/test_security.py
    # calls setup_app() -> _init(), which hard-exits without them, so it is
    # excluded here. That is 56 of 431 tests; the other 375 are hermetic and
    # run in ~10s. Making those 56 hermetic too is tracked in docs/active/RELEASE.md.
    echo "  CI mode: running the Ollama-free subset"
    "$PY" -m pytest -q --ignore=tests/test_security.py
else
    "$PY" -m pytest -q
fi

# ── 4. Build the Python artifacts ─────────────────────────────────────────────
step "Build wheel + sdist"
rm -rf dist build ./*.egg-info
"$PY" -m build
ls -1 dist/

# ── 5. Build the image ────────────────────────────────────────────────────────
# Built from the same tree the wheel came from, so the image can never contain
# code the wheel does not.
step "Build image"
docker build \
    -f docker/Dockerfile \
    --build-arg "KAI_VERSION=$VERSION" \
    -t "$REGISTRY/$IMAGE:$VERSION" \
    -t "$REGISTRY/$IMAGE:latest" \
    .
docker image inspect "$REGISTRY/$IMAGE:$VERSION" --format '  built {{.RepoTags}}'

# ── 6. Commit + tag ───────────────────────────────────────────────────────────
if [ "$CI_MODE" -eq 1 ]; then
    step "CI mode - skipping git commit/tag (the tag is what triggered this)"
else
    step "Commit + tag $TAG"
    git add kai/__init__.py kai/persona/changelog.json
    git commit -m "release: $VERSION"
    git tag -a "$TAG" -m "Kai $VERSION"
    echo "  tagged $TAG"
fi

# ── 7. Push ───────────────────────────────────────────────────────────────────
if [ "$PUSH" -eq 1 ]; then
    step "Push"
    if [ "$CI_MODE" -eq 0 ]; then
        git push origin HEAD
        git push origin "$TAG"
    fi
    docker push "$REGISTRY/$IMAGE:$VERSION"
    docker push "$REGISTRY/$IMAGE:latest"
    echo "  pushed $REGISTRY/$IMAGE:$VERSION"
else
    step "Not pushed"
    cat <<EOF
  Nothing left your machine. To publish:
      git push origin HEAD && git push origin $TAG
      docker push $REGISTRY/$IMAGE:$VERSION
      docker push $REGISTRY/$IMAGE:latest
  (or re-run with --push)
EOF
fi

step "Done - Kai $VERSION"
