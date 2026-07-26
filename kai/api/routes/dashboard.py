"""Dashboard basics — sidebar info, stat-card counts, and clear-conversation."""

import re
import threading

from fastapi import APIRouter, Request

import kai.config as cfg
from kai.api.deps import uid_for
from kai.api.state import brain_for, get_or_create_brain
from kai.store.db import get_conn

router = APIRouter()

_HIGHLIGHT_KEYS = {"user_name", "user_role", "location", "gaming"}
_HIGHLIGHT_LABELS = {
    "user_name": "name",
    "user_role": "role",
    "location": "location",
    "gaming": "games",
}


@router.get("/info")
async def info(request: Request):
    brain = brain_for(request)
    memory = brain.memory
    facts = memory.list_facts()
    recents = memory.recent_episodes(limit=1)

    # Build memory highlights: stable user facts worth showing in sidebar
    highlights = []
    for f in facts:
        base_key = re.sub(r"_\d+$", "", f.key)  # strip _1, _2 suffixes
        if base_key in _HIGHLIGHT_KEYS or base_key in ("note", "preference"):
            label = _HIGHLIGHT_LABELS.get(base_key, base_key.replace("_", " "))
            highlights.append({"key": label, "value": f.value[:24]})
        if len(highlights) >= 4:
            break

    from kai.store import users as _users

    uid = uid_for(request)

    return {
        "model": brain.model,
        "facts": len(facts),
        "context_window": cfg.CONTEXT_WINDOW,
        "last_seen": recents[0].timestamp.strftime("%b %d") if recents else None,
        "highlights": highlights,
        "is_owner": uid != 0 and uid == _users.get_owner_id(),
    }


@router.get("/dashboard/stats")
async def dashboard_stats(request: Request):
    """Aggregated counts for the dashboard stat cards."""
    uid = uid_for(request)
    # Use get_or_create_brain (never raises 503) — stats only needs DB, not Ollama.
    brain = get_or_create_brain(uid)
    memory = brain.memory
    conn = get_conn()
    facts_count = len(memory.list_facts())
    sessions_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)
    ).fetchone()[0]
    docs_count = conn.execute(
        "SELECT COUNT(DISTINCT doc_id) FROM rag_documents WHERE user_id = ? OR shared = 1",
        (uid,),
    ).fetchone()[0]
    notes_count = conn.execute("SELECT COUNT(*) FROM notes WHERE user_id = ?", (uid,)).fetchone()[0]
    return {
        "facts": facts_count,
        "sessions": sessions_count,
        "documents": docs_count,
        "notes": notes_count,
    }


@router.post("/clear")
async def clear(request: Request):
    brain = brain_for(request)
    snapshot = brain.snapshot_history()
    brain.clear_history()
    if any(m.get("role") != "system" for m in snapshot):
        threading.Thread(
            target=brain.flush_history_snapshot,
            args=(snapshot,),
            daemon=True,
        ).start()
    return {"ok": True}
