"""Local container control and Developer Mode stats.

Both surfaces are dashboard-facing and require a logged-in user. They return a
JSONResponse 401 (not a raised HTTPException) to match their callers, which
check the status code rather than catching an exception.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from kai.api.deps import get_user

router = APIRouter()


@router.get("/api/containers")
async def list_containers(request: Request):
    """List local LXD/Incus containers and VMs for the Network Hub. Requires login.

    Returns {"available": bool, "instances": [...]}. `available` is False when no
    container client is installed, so the UI can show an install hint instead of
    an empty list.
    """
    if not get_user(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    from kai.tools import lxc
    return {
        "available": lxc.client_available(),
        "instances": lxc.list_instances_data(),
    }


@router.post("/api/containers/action")
async def container_action(request: Request):
    """Start, stop, or delete a local LXD/Incus instance from the dashboard.

    Body: {"name": "...", "action": "start"|"stop"|"delete"}. Requires login.
    The lxc tools return a human-readable message (not a status flag), so the UI
    re-polls /api/containers after the call to show ground truth. Delete is
    destructive — the UI confirms first, and we force-stop a running instance so
    the call doesn't fail mid-action.
    """
    if not get_user(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    body = await request.json()
    name = (body.get("name") or "").strip()
    action = (body.get("action") or "").strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Missing container name")
    from kai.tools import lxc
    if action == "start":
        message = lxc.start_instance(name)
    elif action == "stop":
        message = lxc.stop_instance(name)
    elif action == "delete":
        message = lxc.delete_instance(name, force=True)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action!r}")
    return {"message": message}


@router.get("/api/dev/stats")
async def dev_stats(request: Request):
    """Live system stats for Developer Mode — temperatures, network, disk usage.

    Reuses the diagnostic tools, which shell out and take a few seconds each, so
    the UI fetches this on demand (never on a poll loop). The three collectors
    run concurrently in worker threads to keep the event loop free. Requires login.
    """
    if not get_user(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    import asyncio as _asyncio

    from kai.tools import file_tools as _ft
    from kai.tools import pc_tools as _pc
    from kai.tools import temps as _temps

    loop = _asyncio.get_event_loop()

    async def _safe(fn):
        try:
            return await loop.run_in_executor(None, fn)
        except Exception as exc:  # one failing collector shouldn't sink the panel
            return f"Unavailable: {exc}"

    temps_txt, net_txt, disk_txt = await _asyncio.gather(
        _safe(_temps.get_temps),
        _safe(_pc.get_network_info),
        _safe(_ft.get_disk_usage),
    )
    return {"temps": temps_txt, "network": net_txt, "disk": disk_txt}
