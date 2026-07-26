"""
self_inspect.py — lets Kai read her own source code, see recent changes, and propose persona updates.
"""

import difflib
import re
from pathlib import Path

from kai.config import PERSONA_PATH, ROOT_DIR
from kai.tools.registry import registry

_PROJECT_ROOT = ROOT_DIR

_EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    "node_modules",
    "data",
    "KaiFiles",
    "kai's memory",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    "env",
    ".env",
    "site-packages",
    "dist-info",
}
_EXCLUDED_EXTENSIONS = {".db", ".bak", ".pyc", ".pyo", ".egg-info"}
_MAX_FILE_SIZE = 100_000  # 100 KB — skip binary/huge files


def _build_tree(root: Path, prefix: str = "", max_depth: int = 4, depth: int = 0) -> list[str]:
    if depth >= max_depth:
        return [f"{prefix}..."]

    lines = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return [f"{prefix}[permission denied]"]

    dirs = [e for e in entries if e.is_dir() and e.name not in _EXCLUDED_DIRS]
    files = [e for e in entries if e.is_file() and e.suffix not in _EXCLUDED_EXTENSIONS]

    for d in dirs:
        lines.append(f"{prefix}{d.name}/")
        lines.extend(_build_tree(d, prefix + "  ", max_depth, depth + 1))
    for f in files:
        lines.append(f"{prefix}{f.name}")

    return lines


@registry.tool(
    name="self.inspect",
    description=(
        "Read your own source code. Use this when you want to understand how you work, "
        "check your own implementation, explain your internals to the user, or verify "
        "what a tool or module actually does. "
        "With no file specified: returns the project file tree. "
        "With a file path: returns the file contents with line numbers. "
        "Paths are relative to the project root (e.g. 'kai/brain.py', 'kai/tools/search.py'). "
        "You can also pass a directory path to see just that subtree."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": (
                "Relative path to a file or directory. "
                "Examples: 'kai/brain.py', 'kai/tools/', 'web.py', 'kai/config.py'. "
                "Leave empty to see the full project tree."
            ),
            "required": False,
        },
        "start_line": {
            "type": "integer",
            "description": "Line number to start reading from (1-indexed). Useful for large files.",
            "required": False,
        },
        "end_line": {
            "type": "integer",
            "description": "Line number to stop reading at (inclusive). Useful for large files.",
            "required": False,
        },
    },
)
def inspect_source(path: str = "", start_line: int = 0, end_line: int = 0) -> str:
    if not path or path.strip() in ("", ".", "/"):
        tree = _build_tree(_PROJECT_ROOT)
        return f"Project root: {_PROJECT_ROOT.name}/\n\n" + "\n".join(tree)

    clean = path.strip().replace("\\", "/").lstrip("/")
    target = _PROJECT_ROOT / clean

    # Prevent path traversal
    try:
        target.resolve().relative_to(_PROJECT_ROOT.resolve())
    except ValueError:
        return "Access denied — path must be within the project directory."

    if not target.exists():
        return f"Not found: {clean}"

    if target.is_dir():
        tree = _build_tree(target)
        return f"{clean}/\n\n" + "\n".join(tree)

    # It's a file — read it
    if target.stat().st_size > _MAX_FILE_SIZE:
        return f"{clean} is too large ({target.stat().st_size:,} bytes). Use start_line/end_line to read a section."

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{clean} appears to be a binary file — cannot display."

    lines = content.splitlines()
    total = len(lines)

    # Apply line range if specified
    s = max(1, int(start_line)) if start_line else 1
    e = min(total, int(end_line)) if end_line else total

    if s > total:
        return f"{clean} has {total} lines — start_line {s} is past the end."

    selected = lines[s - 1 : e]
    numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start=s)]
    header = f"{clean} ({total} lines)"
    if s != 1 or e != total:
        header += f" — showing lines {s}-{e}"

    return header + "\n\n" + "\n".join(numbered)


# ── Recent changes ──────────────────────────────────────────────────────────


@registry.tool(
    name="self.recent_changes",
    description=(
        "Show recent changes to your own codebase via git log. "
        "Use this proactively when you notice a context gap — unfamiliar behavior, "
        "a capability that didn't exist before, or the user mentioning restarts or updates. "
        "Returns commit history with file change summaries so you can reason about what changed and why."
    ),
    parameters={
        "limit": {
            "type": "integer",
            "description": "Number of recent commits to show (default 10).",
            "required": False,
        },
        "file": {
            "type": "string",
            "description": "Optional: filter commits that touched a specific file (e.g. 'kai/brain.py').",
            "required": False,
        },
    },
)
def recent_changes(limit: int = 10, file: str = "") -> str:
    import subprocess

    limit = max(1, min(int(limit), 50))
    cmd = [
        "git",
        "-C",
        str(_PROJECT_ROOT),
        "log",
        f"-{limit}",
        "--pretty=format:%h %ad %s",
        "--date=short",
        "--stat",
    ]
    if file and file.strip():
        cmd += ["--", file.strip()]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return f"git log failed: {result.stderr.strip()}"
        output = result.stdout.strip()
        return output if output else "No commits found."
    except Exception as e:
        return f"Error running git log: {e}"


# ── Live tool inventory ───────────────────────────────────────────────────────


@registry.tool(
    name="self.list_tools",
    description=(
        "List your real, currently-registered tools — the live registry, not your "
        "memory of it. Use this whenever you're about to tell the user what you can "
        "do, or before claiming a tool exists, so you never invent or misname one. "
        "Returns every tool grouped by namespace with its short label."
    ),
    parameters={
        "namespace": {
            "type": "string",
            "description": (
                "Optional: show only tools in this namespace (e.g. 'system', 'files', "
                "'memory'). Leave empty to list everything."
            ),
            "required": False,
        },
    },
)
def list_tools(namespace: str = "") -> str:
    """Enumerate the registry so Kai answers from reality, not hallucination."""
    names = sorted(registry.list_tools())
    ns_filter = namespace.strip().rstrip(".").lower()

    grouped: dict[str, list[str]] = {}
    for name in names:
        ns = name.split(".")[0]
        if ns_filter and ns != ns_filter:
            continue
        grouped.setdefault(ns, []).append(name)

    if not grouped:
        if ns_filter:
            available = sorted({n.split(".")[0] for n in names})
            return f"No tools in namespace '{ns_filter}'. Namespaces: {', '.join(available)}"
        return "No tools registered."

    total = sum(len(v) for v in grouped.values())
    lines = [f"{total} tools registered across {len(grouped)} namespace(s):\n"]
    for ns in sorted(grouped):
        lines.append(f"{ns}.*")
        for name in grouped[ns]:
            lines.append(f"  {name} — {registry.label_for(name)}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ── Persona gap detection ────────────────────────────────────────────────────


def _get_persona_text() -> str:
    if PERSONA_PATH.exists():
        return PERSONA_PATH.read_text(encoding="utf-8")
    return ""


def _get_registered_tools() -> list[str]:
    try:
        from kai.tools.registry import registry as _reg

        return sorted(_reg.list_tools())
    except Exception:
        return []


@registry.tool(
    name="self.check_persona",
    description=(
        "Review your self-knowledge: the identity sections in your persona.md plus a "
        "grounded summary of your live toolbox. persona.md is your identity and voice — "
        "NOT a tool catalog. Your tools live in the registry (use self.list_tools) and "
        "are auto-documented in the memory tree (the [TOOLS] block), so this never asks "
        "you to copy tools into persona.md. Use it to sanity-check that your identity "
        "description still fits who you are, not to 'document capabilities'."
    ),
    parameters={},
)
def check_persona() -> str:
    """Ground 'what do I know about myself?' in the real sources.

    persona.md is identity/voice; the live toolbox is the registry + the memory
    tree ([TOOLS] block, tools/<ns>/<tool> nodes). Earlier this diffed the registry
    against persona.md and flagged every tool 'missing' from persona as a gap to
    fix — a meaningless comparison that nagged Kai to stuff tools into the wrong
    file. It now just reports both, and points capability questions at the registry.
    """
    tools = _get_registered_tools()
    namespaces: dict[str, list[str]] = {}
    for t in tools:
        namespaces.setdefault(t.split(".")[0], []).append(t)

    lines = []
    persona = _get_persona_text()
    if persona:
        section_headings = re.findall(r"^## (.+)", persona, re.MULTILINE)
        lines.append(f"persona.md (your identity/voice) sections: {', '.join(section_headings)}")
    else:
        lines.append("persona.md not found.")

    lines.append("")
    lines.append(
        f"Live toolbox: {len(tools)} tools across {len(namespaces)} namespaces — "
        f"{', '.join(sorted(namespaces))}."
    )
    lines.append(
        "Your tools are NOT listed in persona.md by design — that's identity, not a "
        "catalog. To answer what you can do, call self.list_tools (the live registry, "
        "never your memory of it) or read a tool's full contract with "
        "tree.read tools/<namespace>/<tool>. Propose a persona update only when your "
        "character or principles are out of date — never to document tools."
    )
    return "\n".join(lines)


@registry.tool(
    name="self.propose_persona_update",
    description=(
        "Propose a change to your own persona.md. This does NOT apply the change — "
        "it formats the proposal so the user can review the EXACT text before approving. "
        "ALWAYS show the proposal to the user and wait for explicit approval before "
        "calling self.apply_persona_update. Never apply without asking first."
    ),
    parameters={
        "section": {
            "type": "string",
            "description": (
                "The ## section heading to add or update. "
                "If the section exists, show what would change. "
                "If it's new, show the new section."
            ),
            "required": True,
        },
        "content": {
            "type": "string",
            "description": "The proposed content for this section (markdown formatted).",
            "required": True,
        },
    },
)
def propose_persona_update(section: str, content: str) -> str:
    persona = _get_persona_text()
    section = section.strip().lstrip("#").strip()

    # Check if section already exists
    pattern = re.compile(
        rf"^## {re.escape(section)}\s*\n(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(persona)

    proposal_lines = [f"PROPOSED PERSONA UPDATE — section: ## {section}\n"]

    if match:
        old_content = match.group(1).strip()
        proposal_lines.append("EXISTING content (will be replaced):")
        proposal_lines.append(f"```\n{old_content}\n```\n")
        proposal_lines.append("NEW content:")
        proposal_lines.append(f"```\n{content.strip()}\n```\n")
    else:
        proposal_lines.append("NEW section (will be added):")
        proposal_lines.append(f"```\n## {section}\n\n{content.strip()}\n```\n")

    proposal_lines.append(
        "Show this to the user and ask for approval. "
        "If they approve, call self.apply_persona_update with the same section and content."
    )

    return "\n".join(proposal_lines)


@registry.tool(
    name="self.apply_persona_update",
    description=(
        "Apply a previously proposed and USER-APPROVED change to persona.md. "
        "ONLY call this after: (1) you showed the exact change via self.propose_persona_update, "
        "AND (2) the user explicitly approved it in the conversation. "
        "Never call this without prior user approval."
    ),
    parameters={
        "section": {
            "type": "string",
            "description": "The ## section heading to add or update.",
            "required": True,
        },
        "content": {
            "type": "string",
            "description": "The approved content for this section.",
            "required": True,
        },
    },
)
def _compute_persona_update(section: str, content: str) -> tuple[str, str, bool]:
    """Return (old_persona, new_persona, replaced) without writing anything.

    Shared by apply_persona_update (which writes new_persona) and
    persona_update_diff (which diffs old vs new for the confirm modal), so the
    preview a user approves is always exactly what gets applied.
    """
    persona = _get_persona_text()
    section = section.strip().lstrip("#").strip()
    new_section_text = f"## {section}\n\n{content.strip()}\n"

    pattern = re.compile(
        rf"^## {re.escape(section)}\s*\n(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(persona)

    if match:
        # Replace existing section
        updated = persona[: match.start()] + new_section_text + "\n---\n\n" + persona[match.end() :]
    else:
        # New section — append at the end
        updated = persona.rstrip() + "\n\n---\n\n" + new_section_text

    return persona, updated, bool(match)


def persona_update_diff(section: str, content: str) -> str:
    """A unified diff of persona.md before/after this update — for the confirm UI.

    Returns '' if persona.md is missing or the update is a no-op.
    """
    if not PERSONA_PATH.exists():
        return ""
    old, new, _ = _compute_persona_update(section, content)
    if old == new:
        return ""
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile="persona.md (current)",
        tofile="persona.md (proposed)",
        lineterm="",
    )
    return "\n".join(diff)


def apply_persona_update(section: str, content: str) -> str:
    if not PERSONA_PATH.exists():
        return "Error: persona.md not found."

    section_name = section.strip().lstrip("#").strip()
    _, updated, replaced = _compute_persona_update(section, content)
    PERSONA_PATH.write_text(updated, encoding="utf-8")
    return f"Updated persona.md — section '## {section_name}' has been {'replaced' if replaced else 'added'}."
