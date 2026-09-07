"""
notes.save / notes.search / notes.list / notes.commit — persistent note storage.

SQLite is the source of truth (search and list read it); every saved note is
also mirrored to disk as a .md + .txt pair by kai.store.note_files, so notes are
readable outside Kai and can be committed to a local git repo the user nominates
in Settings. A disk failure never loses the note — the DB write is what counts,
and the file write is reported as a warning alongside it.
"""

from datetime import datetime

from kai.core._app_state import get_current_user_id
from kai.store import note_files
from kai.store.db import get_conn, like_escape
from kai.tools.registry import registry
from kai.util.ids import short_id

_MAX_NOTE_CHARS = 10_000  # ~2 500 words; prevents multi-MB notes clogging the DB


@registry.tool(
    name="notes.save",
    description=(
        "Jot down a quick note: reminders, lists, snippets, things to look at later. "
        "NOT for lasting facts about the user themselves (their identity, hardware, "
        "health, preferences) — file those with tree.save instead."
    ),
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
        note_id = short_id(8)
        ts = datetime.now().isoformat()
        user_id = get_current_user_id()
        conn = get_conn()
        conn.execute(
            "INSERT INTO notes (id, user_id, timestamp, title, content) VALUES (?, ?, ?, ?, ?)",
            (note_id, user_id, ts, title or None, content),
        )
        conn.commit()
    except Exception as e:
        return f"Error saving note: {e}"

    # The note is safely in the DB from here on. Mirroring it to disk is a
    # best-effort extra: a full disk or a bad repo path degrades to a warning
    # rather than telling the user their note wasn't saved.
    saved = f"Saved note [{note_id}]: {title or content[:40]}"
    try:
        paths = note_files.write_note(note_id, ts, title, content, user_id=user_id)
        return f"{saved}\n  → {paths[0]}\n  → {paths[1]}"
    except Exception as e:
        return f"{saved}\n  (warning: could not write note files: {e})"


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
        escaped = like_escape(query)
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
            (user_id,),
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


@registry.tool(
    name="notes.commit",
    description=(
        "Commit saved note files to the local git repo the user configured in "
        "Settings → Notes & repo. Use when the user asks to commit, save, or "
        "check in their notes. Stages only the notes folder — never other work "
        "in that repo — and does not push."
    ),
    parameters={
        "message": {
            "type": "string",
            "description": "Optional commit message. Defaults to a summary of the files committed.",
        },
    },
    category="workspace_and_code",
    label="Committing notes",
    risk="caution",
)
def commit_notes(message: str = "") -> str:
    return note_files.commit_notes(message, user_id=get_current_user_id())


@registry.tool(
    name="notes.where",
    description=(
        "Show where notes are being written on disk and whether a git repo is "
        "configured for them. Use when the user asks where their notes are kept."
    ),
    parameters={},
    category="notes_and_memory",
    label="Checking where notes live",
    risk="safe",
)
def notes_where() -> str:
    info = note_files.repo_status(user_id=get_current_user_id())
    lines = [f"Notes folder: {info['notes_dir']} ({info['file_count']} note files)"]
    if info["repo"]:
        branch = f" on {info['branch']}" if info["branch"] else ""
        lines.append(f"Git repo: {info['repo']}{branch}")
        lines.append(
            f"Uncommitted note changes: {info['pending']}"
            if info["pending"]
            else "All notes are committed."
        )
    elif info["repo_missing"]:
        lines.append(
            f"Configured repo is missing or is no longer a git repo: {info['configured_path']}"
        )
    else:
        lines.append("No git repo configured (Settings → Notes & repo).")
    return "\n".join(lines)
