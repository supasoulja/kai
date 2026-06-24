"""
Owner-only server controls — clean shutdown and restart.

Mounted by web.py via include_router. Every route is gated to the owner (the
first registered user, kai.store.users.get_owner_id); the _AuthGuard middleware
already requires a valid session for /api/admin/*, so a non-owner authenticated
user is rejected with 403 here.

The heavy lifting (drain → welcome-back note → HQ re-embed, then terminate /
re-exec) lives in kai.core.lifecycle. These endpoints just authorize and kick
it off; the work runs on a worker thread so the HTTP response returns before the
process goes away. The dashboard polls /shutdown-status to show progress.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from kai.api.deps import uid_for
from kai.core import lifecycle
from kai.store import users

router = APIRouter(prefix="/api/admin", tags=["admin"])


class RestartRequest(BaseModel):
    mode: str = "hard"   # "soft" = rebuild in place; "hard" = re-exec the process


def _require_owner(request: Request) -> int:
    """Allow only the owner (first registered user). Raise 403 otherwise."""
    uid = uid_for(request)
    owner = users.get_owner_id()
    if uid == 0 or owner is None or uid != owner:
        raise HTTPException(status_code=403, detail="Owner only")
    return uid


@router.post("/shutdown")
async def shutdown(request: Request):
    """Cleanly shut Kai down — finishes the end-of-session embedding, then exits."""
    _require_owner(request)
    if lifecycle.is_shutting_down():
        return {"ok": True, "action": "shutdown", "already": True}
    lifecycle.request_shutdown()
    return {"ok": True, "action": "shutdown"}


@router.post("/restart")
async def restart(req: RestartRequest, request: Request):
    """Restart Kai. mode='soft' rebuilds brains in place; 'hard' re-execs."""
    _require_owner(request)
    mode = "soft" if req.mode == "soft" else "hard"
    if lifecycle.is_shutting_down():
        return {"ok": True, "action": "restart", "mode": mode, "already": True}
    lifecycle.request_restart(mode)
    return {"ok": True, "action": "restart", "mode": mode}


@router.get("/shutdown-status")
async def shutdown_status(request: Request):
    """Progress of an in-flight shutdown/restart (for the dashboard overlay)."""
    _require_owner(request)
    return lifecycle.get_progress()
