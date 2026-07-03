"""Watchdog device pairing, node command API, container control, and dev stats.

These routes don't use cookie/session auth the way the rest of the app does:
scanner scripts and remote agents authenticate via a join code then a
device_id/device_key pair (or the X-Device-Key header), so /register and /event
are public routes that check their own payload. The dashboard-facing routes
(join-code, cluster/nodes, containers, dev/stats) require a logged-in user and
return a JSONResponse 401 (not a raised HTTPException) to match their callers.
"""
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import kai.config as cfg
from kai.api.deps import get_user
from kai.api.models import NodeResultRequest, WatchdogEventRequest, WatchdogRegisterRequest

router = APIRouter()


# ── Watchdog — scanner-script device pairing + event intake ──────────────────
# Scanner scripts can't do cookie-based session auth, so /register and /event
# are public routes that authenticate via their own payload (join code, then
# device_id/device_key) instead. /join-code requires an existing logged-in
# session — minting a code is how a trusted user vouches for a new device.

@router.post("/api/watchdog/join-code")
async def watchdog_join_code(request: Request):
    """Mint a short-lived, single-use code for pairing a new device. Owner-only."""
    from kai.store import users as _users
    user = get_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    if user["user_id"] != _users.get_owner_id():
        return JSONResponse(status_code=403, content={"detail": "Only the owner can pair new devices."})
    from kai import watchdog_queue
    code = watchdog_queue.create_join_code()
    return {"join_code": code, "expires_in": watchdog_queue._JOIN_CODE_TTL}


@router.post("/api/watchdog/register")
async def watchdog_register(body: WatchdogRegisterRequest):
    """Redeem a join code for a unique device_id/device_key pair."""
    from kai import watchdog_queue
    result = watchdog_queue.register_device(body.join_code, body.label)
    if result is None:
        return JSONResponse(status_code=401, content={"detail": "Invalid, expired, or used join code"})
    device_id, device_key = result
    return {"device_id": device_id, "device_key": device_key}


@router.post("/api/watchdog/event")
async def watchdog_event(body: WatchdogEventRequest):
    """Intake for scanner-script reports. Authenticated by device_id/device_key."""
    from kai import watchdog_queue
    if not watchdog_queue.authenticate_device(body.device_id, body.device_key):
        return JSONResponse(status_code=401, content={"detail": "Unknown device or bad key"})
    try:
        watchdog_queue.report_event(
            body.device_id, body.script_id, body.severity, body.message, body.suggestion,
        )
        return {"ok": True}
    except Exception as e:
        return Response(status_code=500, content=str(e))


@router.get("/watchdog/download")
async def watchdog_download():
    """Serve the self-contained watchdog/ agent folder as a zip — lets a new
    machine grab the scanner scripts straight from Kai without git or models.
    Built fresh from disk on each request, so it always matches this server's
    protocol version."""
    import io
    import zipfile
    from pathlib import Path

    # Exclude per-machine state — a paired device's credentials must never leak
    # into the bundle, and bytecode cache is just clutter.
    _skip_names = {"watchdog_config.json"}
    _skip_dirs = {"__pycache__"}

    watchdog_dir = cfg.ROOT_DIR / "watchdog"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in watchdog_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(watchdog_dir)
            if path.name in _skip_names or _skip_dirs & set(rel.parts):
                continue
            zf.write(path, arcname=str(Path("watchdog") / rel))
    data = buf.getvalue()
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=watchdog-agent.zip",
            "Content-Length": str(len(data)),
            "Cache-Control": "no-cache",
        },
    )


# ── Node command API — bidirectional agent control ────────────────────────────
# These routes authenticate via X-Device-Key header (same credential issued at
# join time) — no cookie auth, so agents can call them from remote machines.

def _device_key_auth(request: Request) -> str | None:
    """Extract and validate device_id from path + X-Device-Key header. Returns device_id or None."""
    from kai import watchdog_queue
    device_id = request.path_params.get("device_id", "")
    device_key = request.headers.get("X-Device-Key", "")
    if not watchdog_queue.authenticate_device(device_id, device_key):
        return None
    return device_id


@router.get("/api/node/{device_id}/commands")
async def node_get_commands(device_id: str, request: Request):
    """Agent polls this to receive pending commands. Marks them running on delivery."""
    did = _device_key_auth(request)
    if did is None:
        return JSONResponse(status_code=401, content={"detail": "Unknown device or bad key"})
    from kai import watchdog_queue
    commands = watchdog_queue.get_pending_commands(did)
    return {"commands": commands}


@router.post("/api/node/{device_id}/result/{command_id}")
async def node_post_result(device_id: str, command_id: str, body: NodeResultRequest, request: Request):
    """Agent posts the result of a completed command."""
    did = _device_key_auth(request)
    if did is None:
        return JSONResponse(status_code=401, content={"detail": "Unknown device or bad key"})
    from kai import watchdog_queue
    watchdog_queue.complete_command(command_id, body.result, error=body.error)
    return {"ok": True}


@router.get("/api/cluster/nodes")
async def cluster_nodes(request: Request):
    """List all registered devices with status. Requires user login."""
    if not get_user(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    from kai import watchdog_queue
    devices = watchdog_queue.get_all_devices()
    now = time.time()
    for d in devices:
        d["online"] = d["last_seen"] is not None and (now - d["last_seen"]) < 60
    return {"nodes": devices}


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
