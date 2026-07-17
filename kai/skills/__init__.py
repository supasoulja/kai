"""
Skill system — reusable multi-step workflows that chain tools together.

Usage:
    from kai.skills import SkillRegistry, Skill, SkillResult

    reg = SkillRegistry(tool_registry=my_tool_registry)
    reg.discover(extra_dirs=[Path("user_skills")])
    result = reg.run("pc-health-check")
"""
from pathlib import Path

from kai.skills.base import Skill, SkillResult
from kai.skills.registry import SkillRegistry

__all__ = ["Skill", "SkillResult", "SkillRegistry", "build_skill_registry"]


def build_skill_registry(tool_registry, extra_dirs: "list[Path] | None" = None):
    """Construct a SkillRegistry and discover built-in + user skills.

    `extra_dirs` defaults to [ROOT_DIR/user_skills] — where SKILL.md recipes and
    first-party Python skills live. Skills are optional: any failure degrades to
    None (no skills) rather than blocking Brain/app startup.
    """
    import kai.config as cfg
    try:
        sr = SkillRegistry(tool_registry=tool_registry)
        dirs = extra_dirs if extra_dirs is not None else [cfg.ROOT_DIR / "user_skills"]
        sr.discover(extra_dirs=dirs)
        return sr
    except Exception:
        if getattr(cfg, "DEBUG", False):
            import traceback
            traceback.print_exc()
        return None
