"""
Daily briefing generator.

Runs at BRIEFING_TIME each day, assembles a data-driven summary from:
  - Pending watchdog alerts (node events since last delivery)
  - Registered cluster nodes and their last-seen status
  - Active goals that have gone stale (no progress in GOAL_STALE_DAYS)

The briefing is stored as a pending_briefings row and surfaced in context.py
when the user opens their next chat session — same delivery mechanism as the
welcome-back note, but generated proactively rather than at shutdown.

Design note: briefing generation is intentionally LLM-free. It assembles
structured facts from the DB. This means it works even when Ollama isn't
loaded, adds zero VRAM pressure, and runs in under 100ms.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta

import kai.config as cfg


def generate_and_store(user_id: int = 0) -> None:
    """
    Build today's briefing for user_id and write it to pending_briefings.
    Called by the scheduler — runs in a background thread.
    """
    if not cfg.BRIEFING_ENABLED:
        return

    sections: list[str] = []
    today = datetime.now().strftime("%A, %B %-d")

    # ── 1. Watchdog alerts ────────────────────────────────────────────────────
    try:
        from kai import watchdog_queue
        events = watchdog_queue.get_pending_events(limit=20)
        if events:
            crit = [e for e in events if e["severity"] in ("critical", "warning")]
            if crit:
                lines = [f"[ALERTS — {len(crit)} pending]"]
                for e in crit[:5]:
                    when = datetime.fromtimestamp(e["ts"]).strftime("%H:%M")
                    lines.append(f"  • {e['label']}: {e['message']} ({when})")
                if len(crit) > 5:
                    lines.append(f"  … and {len(crit) - 5} more")
                sections.append("\n".join(lines))
    except Exception:
        pass

    # ── 2. Cluster node status ─────────────────────────────────────────────────
    try:
        from kai import watchdog_queue
        devices = watchdog_queue.get_all_devices()
        active = [d for d in devices if d["status"] == "active"]
        if active:
            now = time.time()
            online = [d for d in active if d["last_seen"] and (now - d["last_seen"]) < 300]
            offline = [d for d in active if d not in online]
            status_parts = []
            if online:
                status_parts.append(f"{len(online)} online")
            if offline:
                status_parts.append(f"{len(offline)} offline")
            node_line = f"[CLUSTER — {', '.join(status_parts)}]"
            if offline and cfg.BRIEFING_DEPTH == "full":
                node_line += "\n" + "\n".join(f"  • {d['label']} — offline" for d in offline[:3])
            sections.append(node_line)
    except Exception:
        pass

    # ── 3. Stale goals ─────────────────────────────────────────────────────────
    try:
        stale = _get_stale_goals(user_id)
        if stale:
            lines = [f"[GOALS — {len(stale)} stalled]"]
            for g in stale[:3]:
                days_ago = int((time.time() - g["last_active"]) / 86400)
                lines.append(f"  • {g['title']} — no progress for {days_ago}d")
            sections.append("\n".join(lines))
    except Exception:
        pass

    if not sections:
        return  # nothing worth reporting

    header = f"[MORNING BRIEFING — {today}]"
    content = header + "\n" + "\n\n".join(sections)

    _store(content, user_id)


def _get_stale_goals(user_id: int) -> list[dict]:
    """Return active goals with no progress in GOAL_STALE_DAYS."""
    try:
        from kai.store.db import get_conn
        cutoff = time.time() - (cfg.GOAL_STALE_DAYS * 86400)
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, title, last_active FROM goals "
            "WHERE user_id = ? AND status = 'active' AND last_active < ? "
            "ORDER BY last_active ASC LIMIT 5",
            (user_id, cutoff),
        ).fetchall()
        return [{"id": r[0], "title": r[1], "last_active": r[2]} for r in rows]
    except Exception:
        return []


def _store(content: str, user_id: int) -> None:
    """Write briefing to pending_briefings table."""
    try:
        from kai.store.db import get_conn
        conn = get_conn()
        conn.execute(
            "INSERT INTO pending_briefings (id, user_id, generated_at, content, delivered) "
            "VALUES (?, ?, ?, ?, 0)",
            (secrets.token_hex(8), user_id, time.time(), content),
        )
        conn.commit()
    except Exception:
        pass


def get_pending(user_id: int = 0) -> str:
    """
    Return the most recent undelivered briefing for user_id, or empty string.
    Called by context.py on session open.
    """
    try:
        from kai.store.db import get_conn
        conn = get_conn()
        row = conn.execute(
            "SELECT id, content FROM pending_briefings "
            "WHERE user_id = ? AND delivered = 0 "
            "ORDER BY generated_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            return row[1]
    except Exception:
        pass
    return ""


def mark_delivered(user_id: int = 0) -> None:
    """Mark all pending briefings for user_id as delivered."""
    try:
        from kai.store.db import get_conn
        conn = get_conn()
        conn.execute(
            "UPDATE pending_briefings SET delivered = 1 "
            "WHERE user_id = ? AND delivered = 0",
            (user_id,),
        )
        conn.commit()
    except Exception:
        pass
