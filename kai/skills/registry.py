"""
Skill registry — discovers, loads, and runs skills.

Skills can be:
  1. Python modules in kai/skills/ that subclass Skill
  2. Python files in a user-facing user_skills/ directory (alongside the project root)
  3. SKILL.md markdown files with structured frontmatter (name, description, triggers, steps)

The registry scans all sources at startup, deduplicates by name, and provides
lookup by exact name or trigger-keyword matching.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from dataclasses import field as _dc_field
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

# A step may be named — "disk = files.disk_usage path=/" — so later steps can
# reference its output. The id is a dot-free identifier before "="; a tool name
# always contains a dot, so a bare "tool.name arg=val" step never false-matches.
_STEP_ID_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.+)", re.DOTALL)
# A namespaced tool call: blocks bare shell commands and path-like strings.
_TOOL_NAME_RE = re.compile(r"[a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+")
# Reference to another step's output inside an arg value: {{disk}} or {{disk.field}}.
# Phase 1 substitutes the whole raw output; the optional .field is Phase 2.
_REF_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)(?:\.[a-zA-Z0-9_]+)?\s*\}\}")

# Cap concurrency so a wide fan-out recipe can't spawn an unbounded thread pool.
_MAX_RECIPE_WORKERS = 8


@dataclass
class _RecipeStep:
    """One parsed step of a recipe: a tool call plus its data dependencies."""

    idx: int  # declaration order (drives deterministic output)
    step_id: str  # unique handle other steps reference
    tool_name: str
    args: dict[str, str]  # inline args; values may hold {{ref}} tokens
    deps: set[str] = _dc_field(default_factory=set)  # step ids this one waits on
    label: str = ""  # how to name this step in a user-facing error

    def __post_init__(self) -> None:
        # Unnamed steps get an internal id (_s3) that would be meaningless in a
        # validation message shown in Settings — describe them by position instead.
        if not self.label:
            self.label = (
                f"'{self.step_id}'"
                if not self.step_id.startswith("_s")
                else f"step {self.idx + 1} ({self.tool_name})"
            )


def _parse_steps(raw_steps: list[str]) -> list[_RecipeStep]:
    """Parse and validate every step up front so a malformed recipe runs nothing.

    Raises ValueError on the first structural problem (bad tool name, duplicate
    id, self-reference, or a reference to an unknown step). Dependencies are
    inferred from {{id}} tokens in the arg values — no dep means the step is
    free to run in parallel with its siblings.
    """
    steps: list[_RecipeStep] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(raw_steps):
        line = (raw or "").strip()
        if not line:
            continue
        m = _STEP_ID_RE.match(line)
        if m and not m.group(1).count("."):
            step_id, body = m.group(1), m.group(2).strip()
        else:
            step_id, body = f"_s{idx}", line

        parts = body.split(maxsplit=1)
        tool_name = parts[0] if parts else ""
        if not _TOOL_NAME_RE.fullmatch(tool_name):
            raise ValueError(f"invalid tool name: {tool_name!r}")
        if step_id in seen_ids:
            raise ValueError(f"duplicate step id: {step_id!r}")
        seen_ids.add(step_id)

        args = _parse_inline_args(parts[1]) if len(parts) > 1 else {}
        deps = {mm.group(1) for v in args.values() for mm in _REF_RE.finditer(v)}
        step = _RecipeStep(idx, step_id, tool_name, args, deps)
        if step_id in deps:
            raise ValueError(f"{step.label} references itself")
        steps.append(step)

    if not steps:
        raise ValueError("recipe has no steps")

    ids = {s.step_id for s in steps}
    for s in steps:
        unknown = s.deps - ids
        if unknown:
            raise ValueError(f"{s.label} references unknown step {sorted(unknown)[0]!r}")
    return steps


def _resolve_refs(value: str, completed: dict[str, tuple[bool, str]]) -> str:
    """Substitute {{id}} / {{id.field}} tokens with a completed step's output.

    Phase 1 injects the whole raw output string; field-level access ({{id.field}}
    over structured output) is Phase 2. Resolution happens in the main thread
    before a step is submitted, so the shared `completed` map is never read and
    written concurrently.
    """
    return _REF_RE.sub(lambda m: completed.get(m.group(1), (False, ""))[1], value)


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
    body = text[fm_match.end() :]
    steps: list[str] = _LIST_ITEM_RE.findall(body)

    skill = _MarkdownSkill()
    skill.name = name
    skill.description = description
    skill.triggers = triggers
    skill._steps = steps
    return skill


class _MarkdownSkill(Skill):
    """A skill loaded from a SKILL.md file.

    Steps run as a dependency graph, not a fixed sequence: steps that don't
    reference another step's output are independent and execute concurrently
    (the fan-out that replaces hand-threaded "scan everything" tools), while a
    step referencing {{other}} waits for that step to finish and receives its
    output. Output is always assembled in declaration order, so a recipe reads
    the same way every run regardless of which step finished first.
    """

    def __init__(self) -> None:
        super().__init__()
        self._steps: list[str] = []

    def execute(self, args: dict) -> SkillResult:
        caller_args = args or {}

        # Parse/validate everything first: a malformed recipe runs no tools at all.
        try:
            steps = _parse_steps(self._steps)
        except ValueError as exc:
            return SkillResult(success=False, output=f"[recipe] ERROR: {exc}", tool_calls=[])

        # user_id/session_id live in threading.local(), which does NOT reach
        # worker threads — capture them here and re-establish inside each worker
        # so per-user DB scoping still holds for tools run in parallel.
        from kai.core._app_state import (
            get_current_session_id,
            get_current_user_id,
            set_current_session_id,
            set_current_user_id,
        )

        uid, sid = get_current_user_id(), get_current_session_id()

        def _run(tool_name: str, merged: dict) -> str:
            set_current_user_id(uid)
            set_current_session_id(sid)
            return str(self.call_tool(tool_name, merged))

        completed: dict[str, tuple[bool, str]] = {}  # step_id → (ok, output)
        ran: set[str] = set()  # steps we actually invoked
        remaining = list(steps)

        while remaining:
            ready = [s for s in remaining if s.deps.issubset(completed)]
            if not ready:
                # Nothing can advance but steps remain → cycle or unreachable dep.
                for s in remaining:
                    completed[s.step_id] = (
                        False,
                        "ERROR: unresolved dependency (cycle or unknown step)",
                    )
                break

            # A step whose dependency failed can't run meaningfully — skip it and
            # say why, rather than calling the tool with an empty substitution.
            prepared: list[tuple[_RecipeStep, dict]] = []
            for s in ready:
                failed = sorted(d for d in s.deps if not completed[d][0])
                if failed:
                    completed[s.step_id] = (
                        False,
                        f"ERROR: skipped — dependency '{failed[0]}' failed",
                    )
                    continue
                resolved = {k: _resolve_refs(v, completed) for k, v in s.args.items()}
                # Caller args are the low-priority base; inline args win.
                prepared.append((s, {**caller_args, **resolved}))

            # Run this wave concurrently. Args were resolved above in this thread,
            # so `completed` is only ever mutated here — no lock needed.
            if prepared:
                workers = min(len(prepared), _MAX_RECIPE_WORKERS)
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_run, s.tool_name, m): s for s, m in prepared}
                    for fut in concurrent.futures.as_completed(futures):
                        s = futures[fut]
                        ran.add(s.step_id)
                        try:
                            completed[s.step_id] = (True, fut.result())
                        except Exception as exc:
                            completed[s.step_id] = (False, f"ERROR: {exc}")

            remaining = [s for s in remaining if s.step_id not in completed]

        # Declaration order, not completion order — a fan-out recipe must read
        # identically every run. A failed step reports inline and keeps its slot,
        # so a partial scan still returns everything that did work.
        outputs = [f"[{s.tool_name}] {completed[s.step_id][1]}" for s in steps]
        return SkillResult(
            success=all(ok for ok, _ in completed.values()),
            output="\n".join(outputs),
            tool_calls=[s.tool_name for s in steps if s.step_id in ran],
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
        self._skills: dict[str, Skill] = {}  # name → Skill instance
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
          2. extra_dirs — user-facing directories (e.g. ROOT_DIR / "user_skills")
        """
        count = 0

        # 1. Built-in Python skills in this package
        builtin_dir = Path(__file__).parent
        count += self._scan_python_dir(builtin_dir, package="kai.skills")

        # 2. Extra directories (user skills, SKILL.md files)
        for d in extra_dirs or []:
            if not d.is_dir():
                continue
            count += self._scan_python_dir(d)
            count += self._scan_md_dir(d)

        return count

    def reload(self, extra_dirs: list[Path] | None = None) -> int:
        """Clear all registered skills and re-run discovery.

        Used after a recipe (SKILL.md) is created or deleted so the live set
        matches what's on disk. Mutates this same instance in place, so every
        Brain holding a reference sees the change on the next turn. Returns the
        number of skills now registered.
        """
        self._skills.clear()
        return self.discover(extra_dirs=extra_dirs)

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
                    import traceback

                    traceback.print_exc()
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
                        import traceback

                        traceback.print_exc()
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
