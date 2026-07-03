"""Session history — list past sessions, fetch their messages, restore one."""
from fastapi import APIRouter, HTTPException, Request

from kai.api.deps import uid_for
from kai.api.state import brain_for
from kai.store import sessions as _sessions

router = APIRouter()


@router.get("/sessions")
async def get_sessions(request: Request):
    uid = uid_for(request)
    return _sessions.list_sessions(limit=50, user_id=uid)


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, request: Request):
    uid = uid_for(request)
    return _sessions.get_messages(session_id, user_id=uid)


@router.post("/sessions/{session_id}/load")
async def load_session(session_id: str, request: Request):
    """Restore a past session into the brain's in-memory history."""
    brain = brain_for(request)
    uid = uid_for(request)
    msgs = _sessions.get_messages(session_id, user_id=uid)
    if not msgs:
        raise HTTPException(status_code=404, detail="Session not found or empty")
    loaded = brain.load_session(session_id, msgs)
    return {"ok": True, "loaded": loaded}
