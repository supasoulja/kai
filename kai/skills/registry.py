"""
Skill registry — discovers, loads, and runs skills.

Skills can be:
  1. Python modules in kai/skills/ that subclass Skill
  2. Python files in a user-facing skills/ directory (alongside the project root)
  3. SKILL.md markdown files with structured frontmatter (name, description, triggers, steps)

The registry scans all sources at startup, deduplicates by name, and provides
lookup by exact name or trigger-keyword matching.
"""
from __future__ import annotations
import importlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import kai.config as cfg
from kai.skills.base import Skill, SkillResult

# Skill names must be safe identifiers: lowercase alphanumeric, hyphens, dots.
# No path separators, no whitespace, no special characters.
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


# ── Markdown skill loader ────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FIELD_RE = re.compile(r"^(\w+)\s*:\s*(.+)$", re.MULTILINE)
_LIST_ITEM_RE = re.compile(r"^\s*-\s*(.+)$", re.MULTILINE)


def _parse_md_skill(path: Path) -> Skill | None:
    """
    Parse a SKILL.md file into a MarkdownSkill instance.

    Expected format:
    ---
    name: quick-cleanup
    description: Clear temp files and run disk cleanup
    triggers: cleanup, free space, clear temp
    ---
    ## Steps
    - system.clear_temp_files
    - system.run_disk_cleanup
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return None

    frontmatter = fm_match.group(1)
    fields: dict[str, str] = {}
    for m in _FIELD_RE.finditer(frontmatter):
        fields[m.group(1).lower()] = m.group(2).strip()

    name = fields.get("name", "")
    if not name:
        return None

    description = fields.get("description", "")
    triggers_raw = fields.get("triggers", "")
    triggers = [t.strip() for t in triggers_raw.split(",") if t.strip()]

    # Extract steps — tool calls listed as "- tool.name arg1=val1 arg2=val2"
    body = text[fm_match.end():]
    steps: list[str] = _LIST_ITEM_RE.findall(body)

    skill = _MarkdownSkill()
    skill.name = name
    skill.description = description
    skill.triggers = triggers
    skill._steps = steps
    return skill


class _MarkdownSkill(Skill):
    """A skill loaded from a SKILL.md file. Steps are tool calls run in order."""

    def __init__(self) -> None:
        super().__init__()
        self._steps: list[str] = []

    def execute(self, args: dict) -> SkillResult:
        tools_called: list[str] = []
        outputs: list[str] = []

        for step in self._steps:
            parts = step.strip().split(maxsplit=1)
            tool_name = parts[0]

            # Validate tool name — must be namespaced (contain a dot) and use safe chars only.
            # This blocks bare commands like "rm" and path-like strings.
            if not re.fullmatch(r"[a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+", tool_name):
                outputs.append(f"[{tool_name}] ERROR: invalid tool name")
                return SkillResult(
                    success=False,
                    output="\n".join(outputs),
                    tool_calls=tools_called,
                )

            tool_args = _parse_inline_args(parts[1]) if len(parts) > 1 else {}
            # Merge caller args (lower priority) with inline args (higher priority)
            merged = {**args, **tool_args}
            tools_called.append(tool_name)
            try:
                result = self.call_tool(tool_name, merged)
                outputs.append(f"[{tool_name}] {result}")
            except Exception as exc:
                outputs.append(f"[{tool_name}] ERROR: {exc}")
                return SkillResult(
                    success=False,
                    output="\n".join(outputs),
                    tool_calls=tools_called,
                )

        return SkillResult(
            success=True,
            output="\n".join(outputs),
            tool_calls=tools_called,
        )


def _parse_inline_args(raw: str) -> dict[str, str]:
    """Parse 'key=value key2=value2' into a dict."""
    args: dict[str, str] = {}
    for token in raw.split():
        if "=" in token:
            k, v = token.split("=", 1)
            args[k.strip()] = v.strip()
    return args


# ── Skill Registry ───────────────────────────────────────────────────────────

class SkillRegistry:
    """
    Central registry for all skills. Scans built-in and user skill directories,
    provides lookup by name or trigger keyword, and handles execution.
    """

    def __init__(self, tool_registry: Any | None = None):
        self._skills: dict[str, Skill] = {}       # name → Skill instance
        self._tool_registry = tool_registry

    @property
    def tool_registry(self) -> Any | None:
        return self._tool_registry

    @tool_registry.setter
    def tool_registry(self, value: Any) -> None:
        self._tool_registry = value

    def register(self, skill: Skill) -> None:
        """Register a single skill instance. Name must be a safe identifier."""
        if not skill.name:
            raise ValueError(f"Skill {type(skill).__name__} has no name")
        if not _SAFE_NAME_RE.match(skill.name):
            raise ValueError(
                f"Skill name {skill.name!r} is invalid — must be lowercase "
                f"alphanumeric with hyphens/dots only (max 64 chars)"
            )
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        """Look up a skill by exact name."""
        return self._skills.get(name)

    def match(self, text: str) -> Skill | None:
        """
        Find the best-matching skill for a user query by checking trigger keywords.
        Returns the skill with the most trigger hits, or None if nothing matches.
        """
        text_lower = text.lower()
        best_skill: Skill | None = None
        best_hits = 0

        for skill in self._skills.values():
            hits = sum(1 for t in skill.triggers if t.lower() in text_lower)
            if hits > best_hits:
                best_hits = hits
                best_skill = skill

        return best_skill

    def run(self, name: str, args: dict | None = None) -> SkillResult:
        """Execute a skill by name. Raises KeyError if not found."""
        skill = self._skills.get(name)
        if not skill:
            raise KeyError(f"Unknown skill: {name!r}")
        skill.bind(self._tool_registry)
        return skill.execute(args or {})

    def list_skills(self) -> list[dict[str, Any]]:
        """Return a summary list of all registered skills."""
        return [
            {
                "name": s.name,
                "description": s.description,
                "triggers": s.triggers,
            }
            for s in self._skills.values()
        ]

    # ── Discovery ────────────────────────────────────────────────────────────

    def discover(self, extra_dirs: list[Path] | None = None) -> int:
        """
        Scan skill directories and register everything found.
        Returns the number of skills loaded.

        Scan order:
          1. kai/skills/ — built-in Python skill modules
          2. extra_dirs — user-facing directories (e.g. ROOT_DIR / "skills")
        """
        count = 0

        # 1. Built-in Python skills in this package
        builtin_dir = Path(__file__).parent
        count += self._scan_python_dir(builtin_dir, package="kai.skills")

        # 2. Extra directories (user skills, SKILL.md files)
        for d in (extra_dirs or []):
            if not d.is_dir():
                continue
            count += self._scan_python_dir(d)
            count += self._scan_md_dir(d)

        return count

    def _scan_python_dir(self, directory: Path, package: str | None = None) -> int:
        """Import .py files that contain Skill subclasses."""
        count = 0
        real_dir = directory.resolve()
        for py_file in directory.glob("*.py"):
            if py_file.name.startswith("_") or py_file.name in ("base.py", "registry.py"):
                continue
            # Symlink / path-traversal guard: file must resolve inside the directory
            if not py_file.resolve().is_relative_to(real_dir):
                if cfg.DEBUG:
                    print(f"[skills] skipping {py_file} — resolves outside {real_dir}")
                continue
            try:
                mod = self._import_file(py_file, package)
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, Skill)
                        and obj is not Skill
                        and getattr(obj, "name", "")
                    ):
                        instance = obj()
                        self.register(instance)
                        count += 1
            except Exception:
                if cfg.DEBUG:
                    import traceback; traceback.print_exc()
        return count

    def _scan_md_dir(self, directory: Path) -> int:
        """Load SKILL.md files from a directory."""
        count = 0
        real_dir = directory.resolve()
        for md_file in directory.glob("*.md"):
            # Symlink / path-traversal guard
            if not md_file.resolve().is_relative_to(real_dir):
                if cfg.DEBUG:
                    print(f"[skills] skipping {md_file} — resolves outside {real_dir}")
                continue
            skill = _parse_md_skill(md_file)
            if skill:
                try:
                    self.register(skill)
                    count += 1
                except ValueError:
                    if cfg.DEBUG:
                        import traceback; traceback.print_exc()
        return count

    @staticmethod
    def _import_file(path: Path, package: str | None = None) -> Any:
        """Import a Python file as a module."""
        stem = path.stem
        if package:
            module_name = f"{package}.{stem}"
        else:
            module_name = f"kai_skill_{stem}"

        # Return cached module if already imported
        if module_name in sys.modules:
            return sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, path)
        if not spec or not spec.loader:
            raise ImportError(f"Cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
