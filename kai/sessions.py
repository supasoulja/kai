"""
Session history — persists conversations across server restarts.

Tables (in kai.db):
  sessions         — one row per conversation
  session_messages — one row per message (user or assistant)
"""
import uuid
from datetime import datetime

from kai.db import get_conn


# ── Write ────────────────────────────────────────────────────────────────────

def new_session(title: str, user_id: int = 0) -> str:
    """Create a new session row and return its UUID."""
    sid  = str(uuid.uuid4())
    now  = datetime.now().isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, started_at, last_active, message_count) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (sid, user_id, title[:60], now, now),
    )
    conn.commit()
    return sid


def append_message(
    session_id: str,
    role: str,
    content: str,
    turn_order: int,
    user_id: int = 0,
) -> int:
    """Persist one message and return its row id."""
    now = datetime.now().isoformat()
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO session_messages (session_id, user_id, role, content, timestamp, turn_order) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, user_id, role, content, now, turn_order),
    )
    conn.execute(
        "UPDATE sessions SET message_count = message_count + 1, last_active = ? "
        "WHERE id = ?",
        (now, session_id),
    )
    conn.commit()
    return cur.lastrowid


def save_feedback(message_id: int, value: int) -> None:
    """Store thumbs up (1) or thumbs down (-1) on an assistant message."""
    conn = get_conn()
    conn.execute(
        "UPDATE session_messages SET feedback = ? WHERE id = ? AND role = 'assistant'",
        (value, message_id),
    )
    conn.commit()


# ── Read ─────────────────────────────────────────────────────────────────────

def list_sessions(limit: int = 50, user_id: int = 0) -> list[dict]:
    """Return sessions for this user, sorted by most recently active."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, started_at, last_active, message_count "
        "FROM sessions WHERE user_id = ? ORDER BY last_active DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [
        {
            "id":            r[0],
            "title":         r[1],
            "started_at":    r[2],
            "last_active":   r[3],
            "message_count": r[4],
        }
        for r in rows
    ]


def get_messages(session_id: str, user_id: int = 0) -> list[dict]:
    """Return all messages for a session in turn order.

    Only returns results when the session belongs to *user_id*, preventing
    one user from reading another user's conversation history.
    """
    conn = get_conn()
    # Verify session ownership via JOIN — returns nothing if user_id doesn't match
    rows = conn.execute(
        "SELECT sm.role, sm.content, sm.timestamp "
        "FROM session_messages sm "
        "JOIN sessions s ON sm.session_id = s.id "
        "WHERE sm.session_id = ? AND s.user_id = ? "
        "ORDER BY sm.turn_order, sm.id",
        (session_id, user_id),
    ).fetchall()
    return [{"role": r[0], "content": r[1], "timestamp": r[2]} for r in rows]


def search_messages(query: str, limit: int = 10, user_id: int = 0) -> list[dict]:
    """
    Search across past session messages by keyword, scoped to this user.
    Returns matching messages with their session title and timestamp.
    """
    conn = get_conn()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT sm.role, sm.content, sm.timestamp, s.title, s.id "
        "FROM session_messages sm "
        "JOIN sessions s ON sm.session_id = s.id "
        "WHERE s.user_id = ? AND sm.content LIKE ? ESCAPE '\\' "
        "ORDER BY sm.timestamp DESC LIMIT ?",
        (user_id, f"%{escaped}%", limit),
    ).fetchall()
    return [
        {
            "role":       r[0],
            "content":    r[1],
            "timestamp":  r[2],
            "session":    r[3],
            "session_id": r[4],
        }
        for r in rows
    ]


def update_last_active(session_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE sessions SET last_active = ? WHERE id = ?",
        (datetime.now().isoformat(), session_id),
    )
    conn.commit()
