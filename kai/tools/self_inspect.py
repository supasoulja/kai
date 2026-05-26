"""
self_inspect.py — lets Kai read her own source code and propose persona updates.
"""
import re
from pathlib import Path

from kai.tools.registry import registry
from kai.config import ROOT_DIR, PERSONA_PATH

_PROJECT_ROOT = ROOT_DIR

_EXCLUDED_DIRS = {"__pycache__", ".git", "node_modules", "data", "KaiFiles",
                  "kai's memory", ".mypy_cache", ".pytest_cache", ".venv",
                  "venv", "env", ".env", "site-packages", "dist-info"}
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
        return f"Access denied — path must be within the project directory."

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

    selected = lines[s - 1:e]
    numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start=s)]
    header = f"{clean} ({total} lines)"
    if s != 1 or e != total:
        header += f" — showing lines {s}-{e}"

    return header + "\n\n" + "\n".join(numbered)


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
        "Check if your persona.md is up to date with your actual capabilities. "
        "Compares registered tools, features, and sections against what's documented. "
        "Use this when you notice something about yourself that doesn't match your persona, "
        "or periodically to check for gaps. "
        "Returns a list of undocumented tools or features that might need persona updates."
    ),
    parameters={},
)
def check_persona() -> str:
    persona = _get_persona_text()
    if not persona:
        return "persona.md not found."

    tools = _get_registered_tools()
    persona_lower = persona.lower()

    # Check which tool namespaces are mentioned
    namespaces: dict[str, list[str]] = {}
    for t in tools:
        ns = t.split(".")[0]
        namespaces.setdefault(ns, []).append(t)

    undocumented_ns = []
    for ns, tool_list in namespaces.items():
        # Check if the namespace or any of its tools are mentioned
        mentioned = (ns in persona_lower or
                     any(t in persona_lower for t in tool_list))
        if not mentioned:
            undocumented_ns.append((ns, tool_list))

    # Check for sections that might be missing
    section_headings = re.findall(r"^## (.+)", persona, re.MULTILINE)

    lines = []
    if undocumented_ns:
        lines.append(f"Found {len(undocumented_ns)} tool namespace(s) not mentioned in persona.md:\n")
        for ns, tool_list in undocumented_ns:
            lines.append(f"  {ns}: {', '.join(tool_list)}")
        lines.append("")
        lines.append("Consider proposing a persona update to document these capabilities.")
        lines.append("Use self.propose_persona_update to draft a change and show it to the user.")
    else:
        lines.append("All tool namespaces are referenced in persona.md.")

    lines.append(f"\nCurrent persona sections: {', '.join(section_headings)}")
    lines.append(f"Total tools registered: {len(tools)}")

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
def apply_persona_update(section: str, content: str) -> str:
    if not PERSONA_PATH.exists():
        return "Error: persona.md not found."

    persona = _get_persona_text()
    section = section.strip().lstrip("#").strip()
    new_section_text = f"## {section}\n\n{content.strip()}\n"

    # Check if section exists
    pattern = re.compile(
        rf"^## {re.escape(section)}\s*\n(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(persona)

    if match:
        # Replace existing section
        updated = persona[:match.start()] + new_section_text + "\n---\n\n" + persona[match.end():]
    else:
        # Append before the last section (Face) to keep it at the end
        face_match = re.search(r"^---\s*\n\n## Face", persona, re.MULTILINE)
        if face_match:
            insert_point = face_match.start()
            updated = (
                persona[:insert_point]
                + "---\n\n" + new_section_text + "\n"
                + persona[insert_point:]
            )
        else:
            # Fallback: append at end
            updated = persona.rstrip() + "\n\n---\n\n" + new_section_text

    PERSONA_PATH.write_text(updated, encoding="utf-8")
    return f"Updated persona.md — section '## {section}' has been {'replaced' if match else 'added'}."
