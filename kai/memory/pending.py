"""
kai/memory/pending.py — durable queue for background knowledge extraction.

Replaces the old in-memory ``deque(maxlen=12)`` on the Brain, which evaporated
on process exit: a short session that ended without a clean shutdown learned
nothing (proven root cause of the Aug-2026 "memory stopped writing" regression).

Rows are enqueued after each eligible turn and drained by the Brain when no user
turn is in flight — and again best-effort on Brain construction, so a hard kill
or a short session that never reached idle re-drains next startup. The table
itself lives in kai/store/db.py (_create_all_tables); this module is just the
read/write API. Every path swallows its own errors: queueing must never break a
turn.
"""

from __future__ import annotations

import time

from kai.store.db import get_conn


def enqueue(
    user_id: int,
    user_text: str,
    assistant_text: str,
    session_id: str | None = None,
) -> None:
    """Queue one exchange for later knowledge extraction."""
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO pending_learn (user_id, session_id, user_text, assistant_text, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, session_id, user_text, assistant_text, time.time()),
        )
        conn.commit()
    except Exception:
        pass


def peek(user_id: int | None = None, limit: int = 5) -> list[tuple[int, str, str]]:
    """Oldest-first pending rows. Pass user_id=None to drain every user's queue.

    Returns (row_id, user_text, assistant_text) tuples. Rows are NOT removed —
    the caller deletes each with delete() only after processing it, so a crash
    mid-extraction re-processes rather than loses the exchange.
    """
    try:
        conn = get_conn()
        if user_id is None:
            rows = conn.execute(
                "SELECT id, user_text, assistant_text FROM pending_learn ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, user_text, assistant_text FROM pending_learn "
                "WHERE user_id = ? ORDER BY id LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]
    except Exception:
        return []


def delete(row_id: int) -> None:
    """Drop one row once it's been processed."""
    try:
        conn = get_conn()
        conn.execute("DELETE FROM pending_learn WHERE id = ?", (row_id,))
        conn.commit()
    except Exception:
        pass


def count(user_id: int | None = None) -> int:
    """How many exchanges are still queued (for diagnostics / tests)."""
    try:
        conn = get_conn()
        if user_id is None:
            return conn.execute("SELECT COUNT(*) FROM pending_learn").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM pending_learn WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
    except Exception:
        return 0
