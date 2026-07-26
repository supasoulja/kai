"""
Goal persistence tools — create and track multi-session tasks.

Goals are long-running tasks that span multiple conversations.
Kai holds the goal, tracks what's done, and picks up where it left off.
Active goals are injected into every context block so they're never forgotten.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime

from kai.tools.registry import registry


def _get_conn():
    from kai.store.db import get_conn

    return get_conn()


def _user_id() -> int:
    try:
        from kai.core._app_state import get_current_user_id

        return get_current_user_id() or 0
    except Exception:
        return 0


@registry.tool(
    name="goals.create",
    description=(
        "Create a new persistent goal — a multi-step task that spans multiple conversations. "
        "Use when the user wants to accomplish something that will take more than one session, "
        "like setting up a homelab service, reorganizing files, or a multi-phase project. "
        "Kai will track progress and pick up where it left off."
    ),
    parameters={
        "title": {
            "type": "string",
            "description": "Short goal name (e.g. 'Set up Nextcloud')",
            "required": True,
        },
        "description": {"type": "string", "description": "What this goal is and why it matters"},
        "steps": {
            "type": "string",
            "description": "Comma-separated ordered steps to accomplish the goal",
        },
    },
)
def create_goal(title: str, description: str = "", steps: str = "") -> str:
    """steps is a comma-separated string; split internally."""
    uid = _user_id()
    goal_id = secrets.token_hex(6)
    now = time.time()
    step_list = [s.strip() for s in steps.split(",") if s.strip()] if steps else []
    conn = _get_conn()
    conn.execute(
        "INSERT INTO goals (id, user_id, title, description, steps_json, current_step, "
        "status, notes, created_at, last_active, updated_at) VALUES (?, ?, ?, ?, ?, 0, 'active', '', ?, ?, ?)",
        (goal_id, uid, title, description or "", json.dumps(step_list), now, now, now),
    )
    conn.commit()
    step_text = ""
    if step_list:
        step_text = "\nSteps:\n" + "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(step_list))
    return f"Goal created: '{title}' (ID: {goal_id}){step_text}\nI'll track progress across our conversations."


@registry.tool(
    name="goals.list",
    description="List all active goals with their current progress.",
    parameters={},
)
def list_goals() -> str:
    uid = _user_id()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, title, description, steps_json, current_step, status, last_active "
        "FROM goals WHERE user_id = ? AND status = 'active' ORDER BY last_active DESC",
        (uid,),
    ).fetchall()
    if not rows:
        return "No active goals."
    lines = [f"Active goals ({len(rows)}):"]
    for r in rows:
        gid, title, desc, steps_json, current_step, status, last_active = r
        steps = json.loads(steps_json) if steps_json else []
        days_ago = int((time.time() - last_active) / 86400) if last_active else 0
        progress = f"Step {current_step + 1}/{len(steps)}" if steps else "in progress"
        last = f"{days_ago}d ago" if days_ago > 0 else "today"
        lines.append(f"\n[{gid}] {title}")
        lines.append(f"  Progress: {progress} — last active {last}")
        if steps and current_step < len(steps):
            lines.append(f"  Next: {steps[current_step]}")
    return "\n".join(lines)


@registry.tool(
    name="goals.update",
    description=(
        "Record progress on a goal — advance to the next step or add a note. "
        "Call this after completing a step or making meaningful progress."
    ),
    parameters={
        "goal_id": {"type": "string", "description": "Goal ID from goals.list", "required": True},
        "advance": {"type": "boolean", "description": "Move to the next step (default true)"},
        "notes": {"type": "string", "description": "Optional note about what was done"},
    },
)
def update_goal(goal_id: str, advance: bool = True, notes: str = "") -> str:
    uid = _user_id()
    conn = _get_conn()
    row = conn.execute(
        "SELECT title, steps_json, current_step, notes FROM goals "
        "WHERE id = ? AND user_id = ? AND status = 'active'",
        (goal_id, uid),
    ).fetchone()
    if not row:
        return f"Goal '{goal_id}' not found or not active."
    title, steps_json, current_step, existing_notes = row
    steps = json.loads(steps_json) if steps_json else []
    now = time.time()
    new_step = min(current_step + 1, len(steps)) if advance else current_step
    note_entry = ""
    if notes:
        ts = datetime.now().strftime("%m/%d")
        note_entry = f"[{ts}] {notes}"
        updated_notes = f"{existing_notes}\n{note_entry}".strip()
    else:
        updated_notes = existing_notes
    conn.execute(
        "UPDATE goals SET current_step = ?, notes = ?, last_active = ?, updated_at = ? WHERE id = ?",
        (new_step, updated_notes, now, now, goal_id),
    )
    conn.commit()
    if steps and new_step < len(steps):
        return f"Goal '{title}' updated. Next step: {steps[new_step]}"
    elif steps and new_step >= len(steps):
        return f"Goal '{title}' — all steps complete. Mark it done with goals.complete?"
    return f"Goal '{title}' progress noted."


@registry.tool(
    name="goals.complete",
    description="Mark a goal as done. Use when all steps have been completed.",
    parameters={
        "goal_id": {"type": "string", "description": "Goal ID from goals.list", "required": True},
    },
)
def complete_goal(goal_id: str) -> str:
    uid = _user_id()
    conn = _get_conn()
    row = conn.execute(
        "SELECT title FROM goals WHERE id = ? AND user_id = ?", (goal_id, uid)
    ).fetchone()
    if not row:
        return f"Goal '{goal_id}' not found."
    conn.execute(
        "UPDATE goals SET status = 'done', updated_at = ? WHERE id = ?",
        (time.time(), goal_id),
    )
    conn.commit()
    return f"Goal '{row[0]}' marked complete."


@registry.tool(
    name="goals.abandon",
    description="Abandon a goal — remove it from the active list.",
    parameters={
        "goal_id": {"type": "string", "description": "Goal ID from goals.list", "required": True},
        "reason": {"type": "string", "description": "Why the goal is being abandoned"},
    },
)
def abandon_goal(goal_id: str, reason: str = "") -> str:
    uid = _user_id()
    conn = _get_conn()
    row = conn.execute(
        "SELECT title FROM goals WHERE id = ? AND user_id = ?", (goal_id, uid)
    ).fetchone()
    if not row:
        return f"Goal '{goal_id}' not found."
    conn.execute(
        "UPDATE goals SET status = 'abandoned', updated_at = ? WHERE id = ?",
        (time.time(), goal_id),
    )
    conn.commit()
    return f"Goal '{row[0]}' abandoned." + (f" Reason: {reason}" if reason else "")
