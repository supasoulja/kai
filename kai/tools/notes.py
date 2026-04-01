"""
notes.save / notes.search / notes.list — persistent note storage in SQLite.
"""
import secrets
from datetime import datetime

_MAX_NOTE_CHARS = 10_000  # ~2 500 words; prevents multi-MB notes clogging the DB

from kai.db import get_conn
from kai._app_state import get_current_user_id
from kai.tools.registry import registry


@registry.tool(
    name="notes.save",
    description="Save a note. Use this when the user asks you to remember something specific, jot something down, or save information for later.",
    parameters={
        "content": {
            "type": "string",
            "description": "The note content to save.",
            "required": True,
        },
        "title": {
            "type": "string",
            "description": "Optional short title for the note.",
        },
    },
)
def save_note(content: str, title: str = "") -> str:
    if len(content) > _MAX_NOTE_CHARS:
        return f"Note too long ({len(content):,} chars). Maximum is {_MAX_NOTE_CHARS:,}."
    try:
        note_id = secrets.token_hex(4)
        ts = datetime.now().isoformat()
        user_id = get_current_user_id()
        conn = get_conn()
        conn.execute(
            "INSERT INTO notes (id, user_id, timestamp, title, content) VALUES (?, ?, ?, ?, ?)",
            (note_id, user_id, ts, title or None, content),
        )
        conn.commit()
        return f"Saved note [{note_id}]: {title or content[:40]}"
    except Exception as e:
        return f"Error saving note: {e}"


@registry.tool(
    name="notes.search",
    description="Search saved notes by keyword. Returns matching notes with their content.",
    parameters={
        "query": {
            "type": "string",
            "description": "The keyword or phrase to search for.",
            "required": True,
        },
    },
)
def search_notes(query: str) -> str:
    try:
        user_id = get_current_user_id()
        conn = get_conn()
        # Escape LIKE wildcards so user input is treated literally
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "SELECT id, timestamp, title, content FROM notes "
            "WHERE user_id = ? AND (content LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\') "
            "ORDER BY timestamp DESC LIMIT 5",
            (user_id, f"%{escaped}%", f"%{escaped}%"),
        ).fetchall()

        if not rows:
            return f"No notes found matching '{query}'."

        results = []
        for row_id, ts, title, content in rows:
            header = f"[{row_id}] {ts[:10]}" + (f" — {title}" if title else "")
            results.append(f"{header}\n{content}")
        return "\n\n".join(results)
    except Exception as e:
        return f"Error searching notes: {e}"


@registry.tool(
    name="notes.list",
    description="List the most recent saved notes (titles and dates).",
)
def list_notes() -> str:
    try:
        user_id = get_current_user_id()
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, timestamp, title, content FROM notes "
            "WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10",
            (user_id,)
        ).fetchall()

        if not rows:
            return "No notes saved yet."

        lines = []
        for row_id, ts, title, content in rows:
            label = title or content[:50]
            lines.append(f"[{row_id}] {ts[:10]} — {label}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing notes: {e}"
