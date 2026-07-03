"""Memory browser — facts, episodic entries, goals, briefings, capabilities.

Read/edit surface for the dashboard's memory panel. Search and goals hit the DB
directly (read-only projections for the UI); fact edits go through MemoryManager.
"""
import json

from fastapi import APIRouter, HTTPException, Request

from kai.api.deps import uid_for
from kai.api.models import FactUpdateRequest
from kai.api.state import brain_for
from kai.store.db import get_conn

router = APIRouter()


@router.get("/briefing/latest")
async def briefing_latest(request: Request):
    """Return the most recent pending daily briefing for the dashboard."""
    uid = uid_for(request)
    from kai.memory.briefing import get_pending
    content = get_pending(user_id=uid)
    return {"content": content}


@router.get("/api/capabilities/new")
async def capabilities_new(request: Request):
    """Tools added to the registry since the user last acknowledged — for the
    awareness bubble. Descriptions come straight from the registry schema, so the
    bubble can't describe a capability that doesn't exist."""
    uid = uid_for(request)
    from kai.memory.capabilities import new_capabilities
    return {"groups": new_capabilities(uid)}


@router.post("/api/capabilities/ack")
async def capabilities_ack(request: Request):
    """Mark the current toolset as seen — dismisses the awareness bubble."""
    uid = uid_for(request)
    from kai.memory.capabilities import acknowledge
    acknowledge(uid)
    return {"ok": True}


@router.get("/goals/active")
async def goals_active(request: Request):
    """Active goals with step progress — for dashboard and chat banner."""
    uid = uid_for(request)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, steps_json, current_step, last_active "
        "FROM goals WHERE user_id = ? AND status = 'active' ORDER BY last_active DESC LIMIT 10",
        (uid,),
    ).fetchall()
    results = []
    for gid, title, steps_json, current_step, last_active in rows:
        steps = json.loads(steps_json) if steps_json else []
        results.append({
            "id": gid,
            "title": title,
            "current_step": current_step,
            "total_steps": len(steps),
            "next_step": steps[current_step] if steps and current_step < len(steps) else None,
            "last_active": last_active,
        })
    return results


@router.get("/goals/all")
async def goals_all(request: Request):
    """All goals grouped by status — for memory browser."""
    uid = uid_for(request)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, description, steps_json, current_step, status, notes, created_at, last_active "
        "FROM goals WHERE user_id = ? ORDER BY last_active DESC",
        (uid,),
    ).fetchall()
    results = []
    for gid, title, desc, steps_json, current_step, status, notes, created_at, last_active in rows:
        steps = json.loads(steps_json) if steps_json else []
        results.append({
            "id": gid,
            "title": title,
            "description": desc,
            "steps": steps,
            "current_step": current_step,
            "total_steps": len(steps),
            "status": status,
            "notes": notes,
            "created_at": created_at,
            "last_active": last_active,
        })
    return results


@router.get("/memory/search")
async def memory_search(q: str, request: Request):
    """Full-text search across semantic facts + episodic entries."""
    uid = uid_for(request)
    q = (q or "").strip().lower()
    if not q:
        return {"facts": [], "episodes": []}

    conn = get_conn()

    # Facts: simple substring match on key + value
    fact_rows = conn.execute(
        "SELECT key, value, source, updated_at FROM semantic_facts "
        "WHERE user_id = ? AND (LOWER(key) LIKE ? OR LOWER(value) LIKE ?) LIMIT 20",
        (uid, f"%{q}%", f"%{q}%"),
    ).fetchall()
    facts = [{"key": r[0], "value": r[1], "source": r[2], "updated_at": r[3]} for r in fact_rows]

    # Episodes: substring match on content
    ep_rows = conn.execute(
        "SELECT id, content, timestamp, entry_type FROM episodic_entries "
        "WHERE user_id = ? AND LOWER(content) LIKE ? ORDER BY timestamp DESC LIMIT 20",
        (uid, f"%{q}%"),
    ).fetchall()
    episodes = [{"id": r[0], "content": r[1], "timestamp": r[2], "entry_type": r[3]} for r in ep_rows]

    return {"facts": facts, "episodes": episodes}


@router.get("/memory/facts")
async def get_memory_facts(request: Request):
    memory = brain_for(request).memory
    facts = memory.list_facts()
    return [
        {
            "key":        f.key,
            "value":      f.value,
            "source":     f.source,
            "updated_at": f.updated_at.strftime("%b %d, %Y"),
        }
        for f in facts
    ]


@router.put("/memory/facts/{key}")
async def update_memory_fact(key: str, req: FactUpdateRequest, request: Request):
    memory = brain_for(request).memory
    value = req.value.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Value cannot be empty")
    memory.set_fact(key, value, source="user_edit")
    return {"ok": True}


@router.delete("/memory/facts/{key}")
async def delete_memory_fact(key: str, request: Request):
    memory = brain_for(request).memory
    memory.delete_fact(key)
    return {"ok": True}


@router.get("/memory/episodic")
async def get_memory_episodic(request: Request):
    """Return episodic summaries (compressed conversation memories)."""
    uid = uid_for(request)
    from kai.memory import episodic as _episodic
    entries = _episodic.recent(limit=50, user_id=uid)
    return [
        {
            "id":         e.id,
            "content":    e.content,
            "timestamp":  e.timestamp.strftime("%b %d %H:%M"),
            "entry_type": e.entry_type,
        }
        for e in entries
    ]
