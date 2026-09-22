#!/usr/bin/env bash
# ── Kai — stand up a development machine (code + tests) ───────────────
#
# For a second machine that edits code and runs the test suite. It does NOT
# set Kai up to run: no Ollama, no chat/embedding models, no machine
# certificate. Use scripts/start.sh for that, on a box that runs Kai.
#
# First time on a new machine:
#   git clone https://github.com/supasoulja/kai-private.git newB2_kai
#   cd newB2_kai && scripts/bootstrap.sh
#
# Re-running is safe — every step checks before it acts.
#
# Flags:
#   --skip-tests      set up, but don't run the suite at the end
#   --with-browser    also fetch Playwright's Chromium (design + browser tools)

set -euo pipefail

SKIP_TESTS=0
WITH_BROWSER=0
for arg in "$@"; do
    case "$arg" in
        --skip-tests)   SKIP_TESTS=1 ;;
        --with-browser) WITH_BROWSER=1 ;;
        *) echo "  [!] Unknown flag: $arg"; exit 1 ;;
    esac
done

echo ""
echo "  +--------------------------------------+"
echo "  |   Kai - dev machine bootstrap        |"
echo "  +--------------------------------------+"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # repo root — this script lives in scripts/
cd "$ROOT_DIR"

# ── Step 1: Python ───────────────────────────────────────────────────
# pyproject.toml says requires-python = ">=3.11". Checking here rather than
# letting pip fail halfway through a 1 GB install.
echo "[1/6] Checking Python..."
if command -v python3 &>/dev/null; then
    PY="python3"
else
    echo ""
    echo "  [!] python3 is not installed or not in PATH."
    echo "      Ubuntu/Debian:  sudo apt install python3 python3-venv python3-pip"
    echo "      Fedora:         sudo dnf install python3 python3-pip"
    echo "      Arch:           sudo pacman -S python python-pip"
    echo ""
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo ""
    echo "  [!] Kai needs Python 3.11 or newer. Found: $("$PY" --version 2>&1)"
    echo ""
    exit 1
fi
echo "      $("$PY" --version 2>&1) found."

# ── Step 2: Private context repo ─────────────────────────────────────
# docs/, audits/, CLAUDE.md and friends are gitignored here — they hold
# personal context and this repo has a public remote. They live in the
# private kai-docs repo and are symlinked in, so a fresh clone that skips
# this step has the source but none of the project's memory.
echo ""
echo "[2/6] Checking private context (kai-docs)..."
CTX_DIR="$(cd "$ROOT_DIR/.." && pwd)/kai-docs"
if [ -d "$CTX_DIR/.git" ]; then
    echo "      Present — pulling any updates..."
    git -C "$CTX_DIR" pull --ff-only --quiet 2>/dev/null \
        && echo "      Up to date." \
        || echo "      [!] Could not fast-forward. Sort it out in $CTX_DIR."
else
    echo "      Cloning into $CTX_DIR ..."
    if git clone --quiet https://github.com/supasoulja/kai-docs.git "$CTX_DIR"; then
        echo "      Cloned."
    else
        echo ""
        echo "  [!] Could not clone kai-docs. It is a PRIVATE repo — check your"
        echo "      GitHub auth (gh auth login, or an SSH key / PAT)."
        echo "      Setup will continue; you'll have code but no project context."
        echo ""
    fi
fi

# ── Step 3: Symlink the context into place ───────────────────────────
# Tools and editors expect these at the repo root. Relative targets, so the
# pair of directories can live anywhere as long as they stay siblings.
echo ""
echo "[3/6] Linking context into the working copy..."
if [ -d "$CTX_DIR" ]; then
    for p in docs audits experiments CLAUDE.md CODE_MAP.md AUDIT_PLAN.md; do
        if [ -e "$ROOT_DIR/$p" ] || [ -L "$ROOT_DIR/$p" ]; then
            continue    # already linked, or a real file the user put there
        fi
        [ -e "$CTX_DIR/$p" ] || continue
        ln -s "../kai-docs/$p" "$ROOT_DIR/$p"
        echo "      linked $p"
    done
    echo "      Context in place."
else
    echo "      Skipped — no kai-docs checkout."
fi

# ── Step 4: Virtual environment ──────────────────────────────────────
# --system-site-packages is required, not a preference: app.py needs the
# apt-only gi/PyGObject, which lives outside the venv. pyvenv.cfg is
# gitignored, so a git sync can never repair a venv built without it.
echo ""
echo "[4/6] Setting up the virtual environment..."
if [ -d "$ROOT_DIR/.venv" ]; then
    echo "      .venv already exists."
    grep -q 'include-system-site-packages = true' "$ROOT_DIR/.venv/pyvenv.cfg" 2>/dev/null \
        || echo "      [!] This venv lacks --system-site-packages; app.py won't open a window."
else
    "$PY" -m venv --system-site-packages "$ROOT_DIR/.venv"
    echo "      Created .venv (--system-site-packages)"
fi
VPY="$ROOT_DIR/.venv/bin/python"

echo "      Installing dependencies (this pulls ~1 GB — onnxruntime,"
echo "      faster-whisper, playwright — so give it a few minutes)..."
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r "$ROOT_DIR/requirements.txt"
"$VPY" -m pip install --quiet -r "$ROOT_DIR/requirements-dev.txt"
echo "      Dependencies installed."

# --system-site-packages means a host package can satisfy a requirement and
# never get installed here. That is how this box silently ran the host's
# CVE-carrying cryptography 41.x. Check the one that bit us.
CRYPTO_PATH="$("$VPY" -c 'import cryptography; print(cryptography.__file__)' 2>/dev/null || echo "")"
case "$CRYPTO_PATH" in
    "$ROOT_DIR/.venv"/*) echo "      cryptography: in the venv, good." ;;
    "")                  echo "      [!] cryptography does not import at all." ;;
    *)                   echo "      [!] cryptography resolves OUTSIDE the venv:"
                         echo "          $CRYPTO_PATH"
                         echo "          Force it in:  .venv/bin/pip install --ignore-installed 'cryptography>=46.0.7'" ;;
esac

if [ "$WITH_BROWSER" -eq 1 ]; then
    echo "      Fetching Playwright Chromium..."
    "$VPY" -m playwright install chromium
fi

# ── Step 5: Pre-commit hooks ─────────────────────────────────────────
# Same ruff version as CI, so a commit here fails the same way it would there.
echo ""
echo "[5/6] Installing pre-commit hooks..."
if "$VPY" -m pre_commit install &>/dev/null; then
    echo "      Hooks installed (ruff check + ruff format)."
else
    echo "      [!] pre-commit install failed — run it by hand later."
fi

# ── Step 6: Verify ───────────────────────────────────────────────────
echo ""
# Setup is done by now, so a failure here is a report, not a crash: say what
# broke, keep going, and exit non-zero at the end so CI still notices.
echo "[6/6] Verifying..."
FAILED=0
if "$VPY" -m ruff check .; then
    echo "      ruff: clean."
else
    echo "      [!] ruff found problems (above)."
    FAILED=1
fi
# tests/test_security.py calls setup_app() -> _init(), which needs a live
# Ollama and the 8 GB chat model. This machine has neither, so it is ignored
# here for the same reason release.sh ignores it under --ci. The rest of the
# suite is hermetic.
if [ "$SKIP_TESTS" -eq 1 ]; then
    echo "      Tests skipped (--skip-tests)."
elif "$VPY" -m pytest -q --ignore=tests/test_security.py; then
    echo "      Tests: green (test_security.py needs a live Ollama; not run here)."
else
    echo "      [!] Test suite failed (above)."
    FAILED=1
fi

echo ""
echo "  ========================================"
if [ "$FAILED" -eq 0 ]; then
    echo "   Ready to code."
else
    echo "   Set up, but verification failed — see above."
fi
echo "  ========================================"
echo ""
echo "  Verify:   .venv/bin/python -m pytest -q"
echo "  Lint:     .venv/bin/python -m ruff check ."
echo ""
echo "  Not set up on this machine: Ollama, the chat and embedding models,"
echo "  and Kai's machine certificate. This box edits and tests code; it does"
echo "  not run Kai. Run scripts/start.sh on a machine that should."
echo ""
exit "$FAILED"
