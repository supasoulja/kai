"""
Skill system — reusable multi-step workflows that chain tools together.

Usage:
    from kai.skills import SkillRegistry, Skill, SkillResult

    reg = SkillRegistry(tool_registry=my_tool_registry)
    reg.discover(extra_dirs=[Path("skills")])
    result = reg.run("pc-health-check")
"""
from kai.skills.base import Skill, SkillResult
from kai.skills.registry import SkillRegistry

__all__ = ["Skill", "SkillResult", "SkillRegistry"]
