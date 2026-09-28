"""Dashboard basics — sidebar info, stat-card counts, and clear-conversation."""

import re
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Request

import kai.config as cfg
from kai.api.deps import get_user, require_user, uid_for
from kai.api.state import brain_for, get_or_create_brain
from kai.llm import roles
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
    facts = memory.list_facts(include_settings=False)
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

    from kai.core import permissions as _permissions
    from kai.store import users as _users

    uid = uid_for(request)

    return {
        "model": brain.model,
        "facts": len(facts),
        "context_window": cfg.CONTEXT_WINDOW,
        "last_seen": recents[0].timestamp.strftime("%b %d") if recents else None,
        "highlights": highlights,
        "is_owner": uid != 0 and uid == _users.get_owner_id(),
        "level": _permissions.level_of(uid) if uid else None,
        "capabilities": _permissions.capabilities_of(uid) if uid else [],
        "user": {"id": uid, "name": (get_user(request) or {}).get("name", "")},
        "max_input_chars": cfg.MAX_INPUT_CHARS,
    }


@router.get("/dashboard/stats")
async def dashboard_stats(request: Request):
    """Aggregated counts for the dashboard stat cards."""
    uid = uid_for(request)
    # Use get_or_create_brain (never raises 503) — stats only needs DB, not Ollama.
    brain = get_or_create_brain(uid)
    memory = brain.memory
    conn = get_conn()
    facts_count = len(memory.list_facts(include_settings=False))
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


def _model_state(model: str, installed: dict | None, loaded: dict | None) -> dict:
    if installed is None:
        return {"state": "unknown", "bytes": 0}
    if loaded and model in loaded:
        return {"state": "loaded", **loaded[model]}
    if model in installed:
        return {"state": "installed", "bytes": installed[model]}
    return {"state": "missing", "bytes": 0}


@router.get("/dashboard/models")
def dashboard_models(request: Request):
    """Which model runs each role, whether it's loaded, and — for non-guests — this
    machine's GPU and RAM fill. Every number comes from Ollama, sysfs or psutil."""
    from kai.core import permissions
    from kai.system import devstats

    uid = require_user(request)["user_id"]
    brain = get_or_create_brain(uid)
    alive = brain.ollama is not None and brain.ollama.is_alive()
    installed = brain.ollama.model_sizes() if alive else None
    loaded = brain.ollama.running() if alive else None

    tool_model, _ = brain._engine._resolve_tool_model()
    rows = [("chat", brain.model), ("tools", tool_model or brain.model)]
    if cfg.CREW_ENABLED:
        rows.append(("crew", roles.model_for("crew")))
    rows.append(("search_deep", cfg.HQ_EMBED_MODEL))

    out = []
    for role, model in rows:
        if role == "chat" and brain._chat_client is not brain.ollama:
            state = {"state": "cloud", "bytes": 0}
        else:
            state = _model_state(model, installed, loaded)
        out.append({"role": role, "model": model, **state})
    out.append({"role": "search_fast", "model": cfg.FAST_EMBED_MODEL, "state": "cpu", "bytes": 0})

    machine = None
    if permissions.level_of(uid) != "guest":
        machine = {
            "gpus": devstats.collect_gpus(),
            "ram": devstats.collect_ram(),
            "loaded": loaded,
        }
    return {"roles": out, "machine": machine}


_ROUTINE_DAYS = 30
_ROUTINE_MIN_DAYS = 7


@router.get("/dashboard/routine")
def dashboard_routine(request: Request):
    """The caller's own messages by hour of day over the last 30 days. `hours` is None
    until messages span enough distinct days to mean anything."""
    uid = require_user(request)["user_id"]
    since = (datetime.now() - timedelta(days=_ROUTINE_DAYS)).isoformat()
    rows = get_conn().execute(
        "SELECT timestamp FROM session_messages WHERE user_id = ? AND role = 'user' "
        "AND timestamp >= ?",
        (uid, since),
    )
    stamps = []
    for (ts,) in rows:
        try:
            stamps.append(datetime.fromisoformat(ts))
        except (TypeError, ValueError):
            continue
    days_seen = len({s.date() for s in stamps})
    hours = None
    if days_seen >= _ROUTINE_MIN_DAYS:
        hours = [0] * 24
        for s in stamps:
            hours[s.hour] += 1
    return {
        "days": _ROUTINE_DAYS,
        "min_days": _ROUTINE_MIN_DAYS,
        "days_seen": days_seen,
        "messages": len(stamps),
        "hours": hours,
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
