"""Turn-flow debug — the X-ray of what happened inside a turn.

Auth uses the shared ``require_user(request)`` guard (M3) — called at the top of
each handler, matching this codebase's style (cf. admin.py's ``_require_owner``) —
rather than a hand-rolled ``if not get_user(...): 401`` copied into each one.
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

import kai.config as cfg
from kai.api.deps import require_user, uid_for
from kai.store.db import get_conn

router = APIRouter()


@router.get("/debug/flow")
async def flow_recent(request: Request):
    """Recent turns with trace ids — open /debug/flow/{trace_id} for the detail."""
    require_user(request)
    from kai.core import flow as _flow

    uid = uid_for(request)
    return {"enabled": cfg.FLOW_TRACE, "turns": _flow.recent_turns(limit=20, user_id=uid)}


@router.get("/debug/flow/live")
async def flow_live(request: Request):
    """SSE firehose of flow steps AS they happen — feeds the /flow page.

    Polls the flow_log table from the current end, so connecting means
    "watch from now on". Survives turns from any session (single-host debug)."""
    require_user(request)
    uid = uid_for(request)

    async def stream():
        try:
            row = get_conn().execute("SELECT COALESCE(MAX(id), 0) FROM flow_log").fetchone()
            cursor = row[0] if row else 0
        except Exception:
            cursor = 0  # table doesn't exist yet — first recorded step creates it
        yield f"data: {json.dumps({'kind': 'hello', 'live': True})}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                rows = (
                    get_conn()
                    .execute(
                        "SELECT id, trace_id, ts, kind, data FROM flow_log "
                        "WHERE id > ? AND user_id = ? ORDER BY id LIMIT 200",
                        (cursor, uid),
                    )
                    .fetchall()
                )
                for rid, tid, ts, kind, data in rows:
                    cursor = rid
                    evt = {"trace_id": tid, "ts": ts, "kind": kind, **json.loads(data)}
                    yield f"data: {json.dumps(evt)}\n\n"
            except Exception:
                pass
            await asyncio.sleep(0.4)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/debug/flow/{trace_id}")
async def flow_detail(trace_id: str, request: Request):
    """Every step of one turn: model requests, raw responses, thinking, tools."""
    require_user(request)
    from kai.core import flow as _flow

    uid = uid_for(request)
    return {"trace_id": trace_id, "steps": _flow.get_flow(trace_id, user_id=uid)}
