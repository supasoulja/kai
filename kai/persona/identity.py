"""
Loads persona.md and recent relationship log entries.
Builds the [IDENTITY] block injected into every system prompt.
"""

import re
from datetime import datetime

from kai.config import PERSONA_PATH
from kai.store.db import get_conn
from kai.system.platform import IS_MAC as _IS_MAC
from kai.system.platform import IS_WINDOWS as _IS_WINDOWS

_OS_NAME = "Windows" if _IS_WINDOWS else ("macOS" if _IS_MAC else "Linux")
_PATH_STYLE = (
    "drive letters like C:\\ and D:\\."
    if _IS_WINDOWS
    else "POSIX paths like /, /home, /mnt. There are no C:\\ drive letters here."
)


def _load_persona() -> str:
    if PERSONA_PATH.exists():
        return PERSONA_PATH.read_text(encoding="utf-8")
    return "You are Kai, a local AI assistant."


def _recent_relationship_entries(limit: int = 3, user_id: int = 0) -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT timestamp, entry_type, content FROM relationship_log "
        "WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [f"[{row[0][:10]} / {row[1]}] {row[2]}" for row in reversed(rows)]


def log_relationship_entry(entry_id: str, entry_type: str, content: str, user_id: int = 0) -> None:
    """Record a milestone, tone shift, or significant moment."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO relationship_log (id, user_id, timestamp, entry_type, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (entry_id, user_id, datetime.now().isoformat(), entry_type, content),
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
    Build the system-prompt identity block from persona.md.

    persona.md is AUTHORITATIVE — it is injected near-verbatim, so editing that
    file (or Kai editing it via self.apply_persona_update) actually changes
    behavior. The only thing added in code is the PLATFORM line, which depends on
    runtime OS detection and so cannot live in a static file.

    HTML comments (<!-- ... -->) are stripped so a file can carry editor notes
    without leaking them into the prompt. Falls back to a minimal directive if
    persona.md is missing or empty.
    """
    persona = re.sub(r"<!--.*?-->", "", persona_md, flags=re.DOTALL).strip()

    if not persona:
        persona = (
            "You are Kai: a local AI agent. Direct, accurate, technically precise. "
            "You have persistent memory and a roster of tools. Never fabricate tool "
            "results. Be brief by default. Don't open with a greeting or close with "
            "'Is there anything else?'"
        )

    # The one runtime-dynamic directive — real OS so paths/commands match
    # (no C:\ probing on Linux). Everything else is the persona file's job.
    platform = (
        f"PLATFORM: This machine runs {_OS_NAME}. "
        f"Use {_OS_NAME}-appropriate paths and commands — {_PATH_STYLE}"
    )

    return f"{persona}\n\n{platform}"
