"""Recipe files — user-authored SKILL.md workflows in user_skills/.

A "recipe" is the low-code unit behind Settings → Tools → New recipe: a named,
trigger-keyed sequence of *existing* tool calls, stored as a SKILL.md file that
the (now-active) SkillRegistry auto-discovers and exposes as skill.<name>.

No code runs here and none is authored — a recipe can only chain tools that are
already registered. That keeps recipes safe to create and share without the
sandbox that untrusted third-party *packs* will require. This module is the
create / list / delete backend; kai.api.routes.settings drives it over HTTP.
"""
from __future__ import annotations
import re
from pathlib import Path

import kai.config as cfg

# Recipe (and skill) names: lowercase, filename-safe. Mirrors kai.skills.registry.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# A step must start with a namespaced tool call (blocks bare shell commands).
_TOOL_RE = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+$")


def recipes_dir() -> Path:
    """The folder recipes live in (created on demand)."""
    d = cfg.ROOT_DIR / "user_skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_for(name: str) -> Path:
    return recipes_dir() / f"{name}.md"


def _parse(path: Path) -> dict | None:
    """Parse one SKILL.md into a recipe dict (name/description/triggers/steps)."""
    from kai.skills.registry import _parse_md_skill
    skill = _parse_md_skill(path)
    if not skill:
        return None
    return {
        "name": skill.name,
        "description": skill.description,
        "triggers": skill.triggers,
        "steps": list(getattr(skill, "_steps", [])),
        "filename": path.name,
    }


def list_recipes() -> list[dict]:
    """Every recipe in user_skills/, sorted by name."""
    out = [_parse(p) for p in sorted(recipes_dir().glob("*.md"))]
    return [r for r in out if r]


def _tool_of(step: str) -> str:
    return step.strip().split(maxsplit=1)[0] if step.strip() else ""


def _valid_step(step: str) -> bool:
    tool = _tool_of(step)
    if not _TOOL_RE.match(tool):
        return False
    from kai.tools.registry import registry
    return tool in registry.list_tools()


def create_recipe(name: str, description: str, triggers: list[str],
                  steps: list[str]) -> dict:
    """Create (or replace) a recipe. Validates the name and every step's tool.

    Raises ValueError with a user-facing message on bad input. Returns the
    stored recipe dict. The caller reloads the skill registry so it goes live.
    """
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise ValueError("Name must be lowercase letters, numbers, dots or hyphens (max 64 chars).")
    steps = [s.strip() for s in (steps or []) if s and s.strip()]
    if not steps:
        raise ValueError("A recipe needs at least one step.")
    for s in steps:
        if not _valid_step(s):
            raise ValueError(f"Step “{s}” must start with a known tool (e.g. system.info).")
    triggers = [t.strip() for t in (triggers or []) if t and t.strip()]
    _path_for(name).write_text(_render(name, description or "", triggers, steps), encoding="utf-8")
    return {"name": name, "description": description or "", "triggers": triggers,
            "steps": steps, "filename": f"{name}.md"}


def delete_recipe(name: str) -> bool:
    """Delete a recipe by name. Returns True if a file was removed."""
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        return False
    p = _path_for(name)
    # Traversal guard (belt-and-suspenders on top of the name regex).
    if not p.resolve().is_relative_to(recipes_dir().resolve()):
        return False
    if p.exists():
        p.unlink()
        return True
    return False


def _render(name: str, description: str, triggers: list[str], steps: list[str]) -> str:
    lines = ["---", f"name: {name}", f"description: {description}",
             f"triggers: {', '.join(triggers)}", "---", "## Steps"]
    lines += [f"- {s}" for s in steps]
    return "\n".join(lines) + "\n"
