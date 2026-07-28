"""Developer Mode stats.

Dashboard-facing and requires a logged-in user. Returns a JSONResponse 401 (not a
raised HTTPException) to match its caller, which checks the status code rather
than catching an exception.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from kai.api.deps import get_user

router = APIRouter()


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
