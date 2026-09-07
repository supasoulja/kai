"""Packaging invariants.

These guard the properties that let Kai ship as a wheel, a clone, or a container
without any of those three becoming the privileged one. They are cheap and
import-only — no Ollama, no DB, no models.

The one that matters most is test_no_container_detection: the moment code starts
asking "am I in Docker?", the container stops being an optional consumer of the
packaging and becomes a thing the app has to know about.
"""

import json
import os
import subprocess
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest

import kai
import kai.config as cfg

REPO_ROOT = Path(__file__).parent.parent


def test_version_matches_changelog():
    """pyproject reads __version__; upgrade.py reads changelog.json. Keep them equal."""
    changelog = json.loads((cfg.PKG_DIR / "persona" / "changelog.json").read_text())
    assert changelog["version"] == kai.__version__, (
        f"kai.__version__ is {kai.__version__} but changelog.json says "
        f"{changelog['version']} — bump both."
    )


def test_declared_entry_points_are_importable():
    """Every console script in pyproject must resolve to a real callable."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert scripts, "no console scripts declared"

    import importlib

    for name, target in scripts.items():
        module_path, _, func = target.partition(":")
        mod = importlib.import_module(module_path)
        assert callable(getattr(mod, func, None)), f"{name} -> {target} is not callable"


def test_package_assets_resolve():
    """Read-only assets must resolve from the package, not the repo root — this is
    what makes them survive being installed into site-packages."""
    assert cfg.PERSONA_PATH.is_file()
    assert cfg.CHANGELOG_PATH.is_file()
    assert cfg.STATIC_DIR.is_dir()
    assert list(cfg.CREW_PROMPTS_DIR.glob("*.md")), "no crew prompts found"

    for p in (cfg.PERSONA_PATH, cfg.CHANGELOG_PATH, cfg.STATIC_DIR, cfg.CREW_PROMPTS_DIR):
        assert cfg.PKG_DIR in p.parents or p == cfg.PKG_DIR, f"{p} is not package-relative"


def test_package_data_globs_cover_shipped_assets():
    """Every non-.py file under kai/ that the runtime reads must match a
    package-data glob, or it will be missing from the wheel."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    globs = pyproject["tool"]["setuptools"]["package-data"]["kai"]

    required = [
        cfg.PERSONA_PATH,
        cfg.CHANGELOG_PATH,
        *cfg.CREW_PROMPTS_DIR.glob("*.md"),
    ]
    for path in required:
        rel = path.relative_to(cfg.PKG_DIR)
        assert any(fnmatch(rel.as_posix(), g) for g in globs), (
            f"{rel} is read at runtime but matches no package-data glob {globs}"
        )


def test_config_import_has_no_side_effects():
    """Importing kai.config must not create directories.

    It used to mkdir at import time, which meant `--help`, a wheel build, and any
    read-only package layer all wrote to disk as a side effect of an import.
    """
    probe = REPO_ROOT / "var" / "_packaging_probe_should_not_exist"
    env = {**os.environ, "KAI_VAR_DIR": str(probe)}
    subprocess.run(
        [sys.executable, "-c", "import kai.config"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    assert not probe.exists(), f"importing kai.config created {probe}"


@pytest.mark.parametrize(
    "env_var,attr",
    [
        ("KAI_VAR_DIR", "VAR_DIR"),
        ("KAI_MODELS_DIR", "AUDIO_MODELS_DIR"),
        ("KAI_SKILLS_DIR", "SKILLS_DIR"),
        ("KAI_WORKSPACE_DIR", "WORKSPACE_DIR"),
    ],
)
def test_path_env_overrides(env_var, attr, tmp_path):
    """Every user-data path takes an env override.

    This is the whole reason a container needs no Kai-specific code: it sets
    these, exactly as someone moving data to a second disk would.
    """
    target = tmp_path / env_var.lower()
    out = subprocess.run(
        [sys.executable, "-c", f"import kai.config as c; print(c.{attr})"],
        cwd=REPO_ROOT,
        env={**os.environ, env_var: str(target)},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert out == str(target), f"{env_var} did not override cfg.{attr} (got {out})"


def test_ollama_url_env_override():
    out = subprocess.run(
        [sys.executable, "-c", "import kai.config as c; print(c.OLLAMA_BASE_URL)"],
        cwd=REPO_ROOT,
        env={**os.environ, "KAI_OLLAMA_URL": "http://gpu-box.lan:11434/"},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert out == "http://gpu-box.lan:11434", "trailing slash should be normalised off"


def test_no_container_detection():
    """No module under kai/ may branch on being containerised.

    Docker is a consumer of the packaging, not a mode the app knows about. If
    this test starts failing, the container has become load-bearing — fix the
    packaging instead of adding the branch.
    """
    needles = ("/.dockerenv", "dockerenv", "IS_DOCKER", "IN_DOCKER", "IS_CONTAINER")
    offenders = []
    for py in cfg.PKG_DIR.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for needle in needles:
            if needle in text:
                offenders.append(f"{py.relative_to(REPO_ROOT)}: {needle}")
    assert not offenders, "container detection leaked into the app:\n" + "\n".join(offenders)


def test_docker_layer_is_deletable():
    """The container files must live in docker/ (plus .dockerignore) and nowhere
    else, so `rm -rf docker/ .dockerignore` fully removes them."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    stray = [
        f
        for f in tracked
        if ("docker" in f.lower() or "compose" in f.lower())
        and not f.startswith("docker/")
        and f != ".dockerignore"
    ]
    assert not stray, f"container files outside docker/: {stray}"


def test_launchers_do_not_hardcode_model_names():
    """scripts/start.sh and start.bat must ask config.py which models to pull.

    They used to hardcode `qwen3.5:9b` and `qwen3:8b` — models Kai stopped
    loading when CHAT_MODEL moved to gemma4. A fresh install pulled ~14 GB of
    weights nothing uses and still had no chat model, so first run failed. The
    launchers are an install path like any other; they get the same single
    source of truth.
    """
    import re

    # Any `family:tag` that looks like an Ollama model reference.
    model_ref = re.compile(r"\b(?:gemma|qwen|llama|mistral|phi)[\w.]*:\w+", re.I)

    for name in ("start.sh", "start.bat"):
        script = REPO_ROOT / "scripts" / name
        if not script.is_file():
            continue
        for i, line in enumerate(script.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("::"):
                continue  # comments may name models for explanation
            assert not model_ref.search(line), (
                f"scripts/{name}:{i} hardcodes a model name — read it from "
                f"kai.config instead:\n    {stripped}"
            )
