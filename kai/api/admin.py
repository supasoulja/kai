"""
Server controls — clean shutdown, restart, and the console mirror.

Mounted by web.py via include_router. Every route needs the server.control capability, which
the owner and admins hold (kai/core/permissions.py). The _AuthGuard middleware already
requires a valid session for /api/admin/*, so anyone else signed in gets 403 here. Shutdown
and restart are written to the audit log.

The heavy lifting (drain → welcome-back note → HQ re-embed, then terminate /
re-exec) lives in kai.core.lifecycle. These endpoints just authorize and kick
it off; the work runs on a worker thread so the HTTP response returns before the
process goes away. The dashboard polls /shutdown-status to show progress.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from kai.api.deps import require_capability
from kai.core import lifecycle
from kai.store import audit

router = APIRouter(prefix="/api/admin", tags=["admin"])


class RestartRequest(BaseModel):
    mode: str = "hard"  # "soft" = rebuild in place; "hard" = re-exec the process


def _require_control(request: Request) -> dict:
    """The owner or an admin (capability server.control). Raises 403 otherwise."""
    return require_capability(request, "server.control")


@router.post("/shutdown")
async def shutdown(request: Request):
    """Cleanly shut Kai down — finishes the end-of-session embedding, then exits."""
    user = _require_control(request)
    if lifecycle.is_shutting_down():
        return {"ok": True, "action": "shutdown", "already": True}
    audit.record("server.shutdown", actor_id=user["user_id"], actor_name=user.get("name"))
    lifecycle.request_shutdown()
    return {"ok": True, "action": "shutdown"}


@router.post("/restart")
async def restart(req: RestartRequest, request: Request):
    """Restart Kai. mode='soft' rebuilds brains in place; 'hard' re-execs."""
    user = _require_control(request)
    mode = "soft" if req.mode == "soft" else "hard"
    if lifecycle.is_shutting_down():
        return {"ok": True, "action": "restart", "mode": mode, "already": True}
    audit.record(
        "server.restart",
        actor_id=user["user_id"],
        actor_name=user.get("name"),
        detail={"mode": mode},
    )
    lifecycle.request_restart(mode)
    return {"ok": True, "action": "restart", "mode": mode}


@router.get("/shutdown-status")
async def shutdown_status(request: Request):
    """Progress of an in-flight shutdown/restart (for the dashboard overlay)."""
    _require_control(request)
    return lifecycle.get_progress()


@router.get("/logs")
async def logs(request: Request, after: int = 0):
    """Console output mirror for the dashboard's Server Console panel.

    Pass ?after=<seq> to fetch only lines newer than the last one seen, so the
    panel can poll cheaply. Owner and admins only — these are raw server logs.
    """
    _require_control(request)
    from kai.util import logbuf

    return logbuf.snapshot(after_seq=after)
