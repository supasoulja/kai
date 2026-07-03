"""Static HTML pages — the SPA shell, login, computer, and flow console.

Owns the asset-stamping helpers (``STATIC_DIR`` is re-imported by web.py for the
/static mount). Stamping every /static reference with the file mtime forces the
embedded desktop webview to re-fetch changed app.js/CSS instead of serving stale
cached bundles.
"""
import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kai.api.deps import get_user

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

_ASSET_REF_RE = re.compile(r'((?:src|href)=["\'])(/static/[^"\'?]+)(["\'])')


def _stamp_assets(html: str) -> str:
    def _sub(m: "re.Match[str]") -> str:
        url = m.group(2)
        fp = STATIC_DIR / url[len("/static/"):]
        try:
            ver = int(fp.stat().st_mtime)
        except OSError:
            return m.group(0)
        return f"{m.group(1)}{url}?v={ver}{m.group(3)}"
    return _ASSET_REF_RE.sub(_sub, html)


@lru_cache(maxsize=4)
def read_html(name: str) -> str:
    return _stamp_assets((STATIC_DIR / name).read_text(encoding="utf-8"))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main app page (or redirect to login if not authenticated)."""
    user = get_user(request)
    if not user:
        return HTMLResponse(content=read_html("login.html"))
    return HTMLResponse(content=read_html("app.html"))


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    """Serve the standalone login page."""
    return HTMLResponse(content=read_html("login.html"))


@router.get("/computer", response_class=HTMLResponse)
async def computer_page():
    """Serve Kai's Computer — simulated Ubuntu desktop."""
    return HTMLResponse(content=read_html("computer.html"))


@router.get("/flow")
async def flow_console():
    """Live debug console — watch Kai's internal flow as it happens."""
    return HTMLResponse(content=read_html("flow.html"))
