"""
notes.save / notes.search / notes.list — persistent note storage in SQLite.
"""
import uuid
from datetime import datetime

from kai.db import get_conn
from kai.tools.registry import registry


@registry.tool(
    name="notes.save",
    description="Save a note. Use this when Alex asks you to remember something specific, jot something down, or save information for later.",
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
    note_id = str(uuid.uuid4())[:8]
    ts = datetime.now().isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO notes (id, timestamp, title, content) VALUES (?, ?, ?, ?)",
        (note_id, ts, title or None, content),
    )
    conn.commit()
    return f"Saved note [{note_id}]: {title or content[:40]}"


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
    conn = get_conn()
    # Escape LIKE wildcards so user input is treated literally
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT id, timestamp, title, content FROM notes "
        "WHERE content LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' "
        "ORDER BY timestamp DESC LIMIT 5",
        (f"%{escaped}%", f"%{escaped}%"),
    ).fetchall()

    if not rows:
        return f"No notes found matching '{query}'."

    results = []
    for row_id, ts, title, content in rows:
        header = f"[{row_id}] {ts[:10]}" + (f" — {title}" if title else "")
        results.append(f"{header}\n{content}")
    return "\n\n".join(results)


@registry.tool(
    name="notes.list",
    description="List the most recent saved notes (titles and dates).",
)
def list_notes() -> str:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, timestamp, title, content FROM notes "
        "ORDER BY timestamp DESC LIMIT 10"
    ).fetchall()

    if not rows:
        return "No notes saved yet."

    lines = []
    for row_id, ts, title, content in rows:
        label = title or content[:50]
        lines.append(f"[{row_id}] {ts[:10]} — {label}")
    return "\n".join(lines)
