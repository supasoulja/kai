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
# Step grammar is NOT redefined here — it lives in the executor (_parse_steps),
# and _validate_steps below borrows it so the builder and the runtime can't drift.


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


def _validate_steps(steps: list[str]) -> None:
    """Validate a recipe's steps using the executor's own grammar.

    Borrows kai.skills.registry._parse_steps (same package) instead of
    re-implementing the grammar, so what the builder accepts is exactly what the
    runtime can run. That parser understands the optional ``id = tool args``
    prefix and ``{{ref}}`` data-flow, and raises on a malformed tool name, a
    duplicate step id, a self-reference, or a reference to an unknown step.

    On top of it we require every tool to actually be registered — the executor
    only checks the *shape* of a tool name, not that the tool exists.
    Raises ValueError with a user-facing message.
    """
    from kai.skills.registry import _parse_steps
    from kai.tools.registry import registry

    parsed = _parse_steps(steps)          # structural problems raise ValueError
    known = set(registry.list_tools())
    for p in parsed:
        if p.tool_name not in known:
            raise ValueError(f"Step “{p.tool_name}” is not a known tool (e.g. system.info).")


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
    _validate_steps(steps)
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
