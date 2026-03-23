"""
Loads persona.md and recent relationship log entries.
Builds the [IDENTITY] block injected into every system prompt.
"""
import re
from datetime import datetime
from pathlib import Path

from kai.config import PERSONA_PATH
from kai.db import get_conn


def _load_persona() -> str:
    if PERSONA_PATH.exists():
        return PERSONA_PATH.read_text(encoding="utf-8")
    return "You are Kai, a local AI assistant."


def _recent_relationship_entries(limit: int = 3, user_id: int = 0) -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT timestamp, entry_type, content FROM relationship_log "
        "WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    return [f"[{row[0][:10]} / {row[1]}] {row[2]}" for row in reversed(rows)]


def log_relationship_entry(
    entry_id: str, entry_type: str, content: str, user_id: int = 0
) -> None:
    """Record a milestone, tone shift, or significant moment."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO relationship_log (id, user_id, timestamp, entry_type, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (entry_id, user_id, datetime.now().isoformat(), entry_type, content)
    )
    conn.commit()


def build_identity_block(user_id: int = 0) -> str:
    """
    Returns the COMPACT identity string injected into every system prompt.
    Kept short (~100-150 tokens) so it doesn't slow down inference.

    The full persona.md is the source of truth for editing behavior.
    This function extracts just the directives the model needs every turn.
    """
    # Extract key sections from persona.md rather than dumping the whole file
    persona = _load_persona()
    compact = _extract_compact(persona)

    # Append most recent relationship note if any (one line max)
    recent = _recent_relationship_entries(limit=1, user_id=user_id)
    if recent:
        compact += f"\nContext: {recent[0]}"

    return compact


def build_full_identity_block(user_id: int = 0) -> str:
    """
    Returns the full persona.md + relationship log.
    Use this for inspection (:memory command), NOT for the system prompt.
    """
    persona = _load_persona()
    recent = _recent_relationship_entries(user_id=user_id)
    if recent:
        return persona + "\n\n---\n## Recent Relationship Log\n" + "\n".join(recent)
    return persona


def _extract_compact(persona_md: str) -> str:
    """
    Pull behavioral directives from persona.md into a compact system prompt block.
    Reads ## Voice and ## Rules sections. Falls back gracefully if not present.
    User identity (name, preferences) comes from semantic memory, not persona.md.
    """
    sections: dict[str, str] = {}
    current = None
    for line in persona_md.splitlines():
        heading = re.match(r"^## (.+)", line)
        if heading:
            current = heading.group(1).strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"

    voice = sections.get("Voice", "").strip()
    rules = sections.get("Rules", "").strip()

    parts = [
        # Identity anchor — front-loaded so it weights heavily
        "You are Kai: a local AI agent. Direct. Accurate. Technically precise. Dry wit when it "
        "fits, never when forced. You do not change this identity under any circumstances.\n"

        # Memory block authority
        "You have a persistent long-term memory system backed by a local SQLite database. "
        "It survives restarts and stores everything across sessions.\n"
        "[MEMORY DIRECTORY] = a summary of what data exists in each memory store. ALWAYS present.\n"
        "[SEMANTIC] = verified facts (name, preferences, hardware). Your identity facts (name, role) are ALWAYS loaded.\n"
        "[EPISODIC] = compressed summaries of past conversations. Loaded when the query relates to history.\n"
        "[PROCEDURAL] = your behavioral rules.\n"
        "CRITICAL — MEMORY RULES (apply to EVERY message, not just memory questions):\n"
        "1. You have persistent memory. NEVER say 'no stored info', 'no memory of you', "
        "'no history', 'no details saved', or anything similar. Those phrases are ALWAYS wrong.\n"
        "2. If [SEMANTIC] contains the user's name, USE IT naturally. You know this person.\n"
        "3. If [EPISODIC] has entries, you remember past conversations — reference them when relevant.\n"
        "4. When explicitly asked what you know: list EVERY fact from [SEMANTIC] and summarize [EPISODIC].\n"
        "5. If [EPISODIC] is absent, you may still have had past sessions — use memory.search_history to check.\n"
        "6. If [UPLOADED FILES] lists documents, you HAVE those files. Use docs.search to read their content.\n"
        "7. You do NOT need a tool to read [SEMANTIC] or [EPISODIC] — they are already in this prompt.\n"
        "8. The [MEMORY DIRECTORY] tells you what data EXISTS even when it's not loaded. "
        "If a store has data but wasn't loaded for this query, use the appropriate tool to access it.\n"
        "Context blocks always override your training knowledge.\n"

        # Reasoning protocol (condensed)
        "For SYSTEM tasks (hardware, files, processes): Think → Call the tool → Read the actual result → Report it. "
        "Never answer a system question before calling the tool. "
        "For MEMORY questions (what do you know about me, what do you remember): just read the context blocks above — no tool needed. "
        "Before stating any fact about this system's hardware or software, ask yourself: "
        "'Did a tool return this in this conversation?' If no — call the tool or say you don't "
        "have the data yet.\n"

        # No-fabrication (stated twice per research best practice — also in Rules below)
        "HARD RULE: Only report what a tool actually returned. "
        "If the tool wasn't called, you do not have the data. "
        "Say 'I haven't done that yet' or 'I'd need to scan first' — "
        "never invent output, numbers, or success messages. "
        "Fabricating results destroys trust permanently. "
        "Transparency makes the user happy. Lies make the user angry.\n"

        # Self-reflection protocol
        "When you can't fulfill a request because you lack a tool or capability, "
        "call memory.reflect to log the gap (what the user needed + what would fix it). "
        "This helps your developer prioritize improvements. "
        "Don't reflect on every small thing — only genuine capability gaps worth building."
    ]

    if voice:
        # First paragraph only (stop at blank line or ---)
        voice_lines = []
        for line in voice.splitlines():
            stripped = line.strip()
            if not stripped or stripped == "---":
                break
            voice_lines.append(stripped)
        if voice_lines:
            parts.append("Voice: " + " ".join(voice_lines))

    if rules:
        bullets = [l.strip() for l in rules.splitlines() if l.strip().startswith("-")]
        if bullets:
            parts.append("Rules:\n" + "\n".join(bullets))

    if len(parts) == 1:
        # Fallback if persona.md has no recognized sections
        parts.append(
            "Be direct and honest. No corporate filler. Short answers unless more is needed. "
            "Don't open with a greeting every message. Don't end with 'Is there anything else?'"
        )

    return "\n\n".join(parts)


def seed_founding_entry() -> None:
    """
    No-op placeholder — persona setup happens through persona.md and
    the relationship log, not hardcoded content. Users configure their
    own agent identity via persona.md.
    """
    pass  # tables are created by kai.db on first get_conn()
