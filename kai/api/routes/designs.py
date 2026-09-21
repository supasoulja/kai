"""Serves what design.render and design.spreadsheet made, to the account that made them.

Only that account's own folder, only the finished file types. Never the rest of the
workspace, which holds other people's notes too.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

import kai.config as cfg
from kai.api.deps import require_user

router = APIRouter()

_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@router.get("/api/designs/{name}")
def get_design(name: str, request: Request):
    user = require_user(request)
    folder = (Path(cfg.DESIGNS_DIR) / str(user["user_id"])).resolve()
    path = (folder / name).resolve()
    kind = _TYPES.get(path.suffix.lower())
    if path.parent != folder or not kind or not path.is_file():
        raise HTTPException(status_code=404, detail="No such design")
    return FileResponse(
        path,
        media_type=kind,
        filename=path.name,
        content_disposition_type="attachment" if path.suffix.lower() == ".xlsx" else "inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )
