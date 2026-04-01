"""
Kai — web interface.

Usage:
    python web.py              # starts on port 7860, opens browser
    python web.py --port 8080
    python web.py --no-browser
"""
import argparse
import asyncio
import json
import logging
import re
import secrets
import sys
import time as _time
import threading
import webbrowser
from pathlib import Path

_log = logging.getLogger(__name__)

try:
    import uvicorn
except ModuleNotFoundError:
    print("ERROR: uvicorn is not installed in this Python environment.")
    print(f"  Python executable: {sys.executable}")
    print(f"  Fix:  {sys.executable} -m pip install uvicorn[standard]")
    sys.exit(1)
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import kai.config as cfg
from kai.brain import Brain, OllamaClient, _strip_thinking, _build_compress_messages
from kai.memory.manager import MemoryManager
from kai.memory.procedural import seed_defaults
from kai.memory import semantic as _semantic
from kai.identity import seed_founding_entry
from kai.tools import registry as tool_registry
from kai import sessions as _sessions
from kai import campaign as _campaign
from kai._app_state import set_embed_fn as _set_embed_fn


# ── Pydantic models (defined before routes that use them) ─────────────────────

class ChatRequest(BaseModel):
    message: str

class LoginRequest(BaseModel):
    name: str
    pin:  str

class FeedbackRequest(BaseModel):
    message_id: int
    value: int        # 1 = thumbs up, -1 = thumbs down
    snippet: str = "" # first ~300 chars of the response (for episodic memory)

class FactUpdateRequest(BaseModel):
    value: str

class ModeRequest(BaseModel):
    mode: str  # "short" | "long" | "chat" | "research"

class DmStartRequest(BaseModel):
    campaign_name: str = ""   # empty = resume active, non-empty = create new

class AddModelRequest(BaseModel):
    name: str
    ollama_id: str
    think: bool = False

# Maximum input length — prevents accidental context blowout
_MAX_INPUT_CHARS = 8000


# ── Session auth ──────────────────────────────────────────────────────────────
# In-memory token store with server-side expiry.

_SESSION_TTL = 86400 * 7  # 7 days — matches cookie max_age
_session_tokens: dict[str, dict] = {}   # token → {"name": str, "user_id": int, "created_at": float}
_session_tokens_lock = threading.Lock()


def _issue_token(user_info: dict) -> str:
    """Create, store, and return a new session token.
    Prunes expired tokens as a side effect to prevent unbounded growth."""
    token = secrets.token_urlsafe(32)
    now = _time.monotonic()
    with _session_tokens_lock:
        expired = [t for t, v in _session_tokens.items()
                   if now - v["created_at"] > _SESSION_TTL]
        for t in expired:
            del _session_tokens[t]
        _session_tokens[token] = {**user_info, "created_at": now}
    return token


def _get_session(token: str) -> dict | None:
    """Look up a session token; returns user dict or None if absent or expired."""
    now = _time.monotonic()
    with _session_tokens_lock:
        info = _session_tokens.get(token)
        if info is None:
            return None
        if now - info["created_at"] > _SESSION_TTL:
            del _session_tokens[token]
            return None
        return {"name": info["name"], "user_id": info["user_id"]}

# ── Login rate limiting ──────────────────────────────────────────────────────
# Limits login attempts per IP to prevent brute-forcing the 4-digit PIN.
# Window = 15 minutes, max 5 attempts. Resets after the window expires.

_LOGIN_WINDOW    = 900   # 15 minutes in seconds
_LOGIN_MAX_TRIES = 5
_login_attempts: dict[str, list[float]] = {}   # IP → list of timestamps
_login_lock = threading.Lock()


def _check_login_rate(ip: str) -> bool:
    """Return True if this IP is allowed to attempt login, False if rate-limited."""
    now = _time.monotonic()
    with _login_lock:
        attempts = _login_attempts.get(ip, [])
        # Prune old attempts outside the window
        attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
        _login_attempts[ip] = attempts
        if len(attempts) >= _LOGIN_MAX_TRIES:
            return False
        attempts.append(now)
        return True


class _AuthGuard:
    """
    Raw ASGI middleware — rejects unauthenticated requests to protected routes.

    Written as a raw ASGI app (not BaseHTTPMiddleware) so that streaming
    responses (SSE chat) are never buffered.
    """

    # Routes that never require auth (no cookie parsing needed)
    _PUBLIC = frozenset({
        "/login", "/users", "/users/login", "/users/register",
    })
    _PUBLIC_PREFIXES = ("/static/",)

    # Routes that parse the cookie but don't reject if missing
    _OPTIONAL_AUTH = frozenset({"/", "/dashboard/stats", "/users/logout"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in self._PUBLIC or any(path.startswith(p) for p in self._PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)

        # Parse kai_session from the Cookie header
        token = None
        for name, val in scope.get("headers", []):
            if name == b"cookie":
                for part in val.decode().split(";"):
                    part = part.strip()
                    if part.startswith("kai_session="):
                        token = part[len("kai_session="):]
                        break
                break

        user_info = _get_session(token) if token else None

        if not user_info and path not in self._OPTIONAL_AUTH:
            resp = JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
            )
            return await resp(scope, receive, send)

        # Inject user info into ASGI scope so routes can access it
        if user_info:
            scope.setdefault("state", {})
            scope["state"]["user"] = user_info
        return await self.app(scope, receive, send)


class _SecurityHeaders:
    """Raw ASGI middleware — injects security headers on every HTTP response."""

    _HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options",       b"DENY"),
        (b"referrer-policy",       b"strict-origin-when-cross-origin"),
        (b"permissions-policy",    b"camera=(), microphone=(), geolocation=()"),
        (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(self._HEADERS)
                message["headers"] = headers
            await send(message)

        return await self.app(scope, receive, send_with_headers)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_user(request: Request) -> dict | None:
    """Extract the authenticated user dict from the request (set by _AuthGuard).
    Returns {"name": str, "user_id": int} or None for public routes."""
    return getattr(request.state, "user", None)


# ── App state ──────────────────────────────────────────────────────────────────

app = FastAPI(title="Kai")

# Shared across all users (expensive to duplicate)
_ollama: OllamaClient | None = None
_shared_tool_index: dict[str, list[float]] = {}
_shared_domain_index: dict[str, list[float]] = {}

# Per-user Brain + MemoryManager instances
_user_brains: dict[int, Brain] = {}
_user_brains_lock = threading.Lock()

_STATIC_DIR = Path(__file__).parent / "kai" / "static"


def _get_or_create_brain(user_id: int) -> Brain:
    """Get (or lazily create) a per-user Brain instance. Thread-safe."""
    brain = _user_brains.get(user_id)
    if brain is not None:
        return brain
    with _user_brains_lock:
        # Double-check after acquiring lock
        brain = _user_brains.get(user_id)
        if brain is not None:
            return brain
        from kai.embed import embed as _fast_embed
        memory = MemoryManager(embed_fn=_fast_embed, user_id=user_id)
        # Copy shared indexes so we don't re-embed per user
        memory._domain_index = dict(_shared_domain_index)
        seed_defaults(user_id=user_id)
        brain = Brain(
            memory=memory,
            model=cfg.CHAT_MODEL,
            ollama=_ollama,
            tool_registry=tool_registry,
            think=True,
            user_id=user_id,
        )
        brain._tool_index = dict(_shared_tool_index)
        brain._tool_index_ready = bool(_shared_tool_index)
        brain._memory_router_ready = bool(_shared_domain_index)
        _user_brains[user_id] = brain
        return brain


def _brain_for(request: Request) -> Brain:
    """Get the Brain for the authenticated user. Raises 503 if Ollama not ready."""
    if not _ollama:
        raise HTTPException(status_code=503, detail="Not initialized")
    user = _get_user(request)
    uid = user["user_id"] if user else 0
    return _get_or_create_brain(uid)


def _uid_for(request: Request) -> int:
    """Get the user_id for the authenticated user. Returns 0 for public routes."""
    user = _get_user(request)
    return user["user_id"] if user else 0


# ── Routes ─────────────────────────────────────────────────────────────────────

# Cache static HTML at import time — these files don't change at runtime.
from functools import lru_cache

@lru_cache(maxsize=4)
def _read_html(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main app page (or redirect to login if not authenticated)."""
    user = _get_user(request)
    if not user:
        return HTMLResponse(content=_read_html("login.html"))
    return HTMLResponse(content=_read_html("app.html"))


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Serve the standalone login page."""
    return HTMLResponse(content=_read_html("login.html"))


_HIGHLIGHT_KEYS = {"user_name", "user_role", "location", "gaming"}
_HIGHLIGHT_LABELS = {
    "user_name": "name",
    "user_role": "role",
    "location":  "location",
    "gaming":    "games",
}

@app.get("/info")
async def info(request: Request):
    brain = _brain_for(request)
    memory = brain.memory
    facts   = memory.list_facts()
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

    return {
        "model":          brain.model,
        "facts":          len(facts),
        "context_window": cfg.CONTEXT_WINDOW,
        "last_seen":      recents[0].timestamp.strftime("%b %d") if recents else None,
        "highlights":     highlights,
    }


@app.get("/dashboard/stats")
async def dashboard_stats(request: Request):
    """Aggregated counts for the dashboard stat cards."""
    uid = _uid_for(request)
    # Use _get_or_create_brain (never raises 503) — stats only needs DB, not Ollama.
    brain = _get_or_create_brain(uid)
    memory = brain.memory
    from kai.db import get_conn
    conn = get_conn()
    facts_count = len(memory.list_facts())
    sessions_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)
    ).fetchone()[0]
    docs_count = conn.execute(
        "SELECT COUNT(DISTINCT doc_id) FROM rag_documents WHERE user_id = ? OR shared = 1",
        (uid,),
    ).fetchone()[0]
    notes_count = conn.execute(
        "SELECT COUNT(*) FROM notes WHERE user_id = ?", (uid,)
    ).fetchone()[0]
    return {
        "facts": facts_count,
        "sessions": sessions_count,
        "documents": docs_count,
        "notes": notes_count,
    }


@app.post("/clear")
async def clear(request: Request):
    brain = _brain_for(request)
    snapshot = brain.snapshot_history()
    brain.clear_history()
    if any(m.get("role") != "system" for m in snapshot):
        threading.Thread(
            target=brain.flush_history_snapshot,
            args=(snapshot,),
            daemon=True,
        ).start()
    return {"ok": True}


# ── Memory browser ─────────────────────────────────────────────────────────────

@app.get("/memory/facts")
async def get_memory_facts(request: Request):
    memory = _brain_for(request).memory
    facts = memory.list_facts()
    return [
        {
            "key":        f.key,
            "value":      f.value,
            "source":     f.source,
            "updated_at": f.updated_at.strftime("%b %d, %Y"),
        }
        for f in facts
    ]


@app.put("/memory/facts/{key}")
async def update_memory_fact(key: str, req: FactUpdateRequest, request: Request):
    memory = _brain_for(request).memory
    value = req.value.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Value cannot be empty")
    memory.set_fact(key, value, source="user_edit")
    return {"ok": True}


@app.delete("/memory/facts/{key}")
async def delete_memory_fact(key: str, request: Request):
    memory = _brain_for(request).memory
    memory.delete_fact(key)
    return {"ok": True}


@app.get("/memory/episodic")
async def get_memory_episodic(request: Request):
    """Return episodic summaries (compressed conversation memories)."""
    uid = _uid_for(request)
    from kai.memory import episodic as _episodic
    entries = _episodic.recent(limit=50, user_id=uid)
    return [
        {
            "id":         e.id,
            "content":    e.content,
            "timestamp":  e.timestamp.strftime("%b %d %H:%M"),
            "entry_type": e.entry_type,
        }
        for e in entries
    ]


# ── Session history ─────────────────────────────────────────────────────────────

@app.get("/sessions")
async def get_sessions(request: Request):
    uid = _uid_for(request)
    return _sessions.list_sessions(limit=50, user_id=uid)


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, request: Request):
    uid = _uid_for(request)
    return _sessions.get_messages(session_id, user_id=uid)


@app.post("/sessions/{session_id}/load")
async def load_session(session_id: str, request: Request):
    """Restore a past session into the brain's in-memory history."""
    brain = _brain_for(request)
    uid = _uid_for(request)
    msgs = _sessions.get_messages(session_id, user_id=uid)
    if not msgs:
        raise HTTPException(status_code=404, detail="Session not found or empty")
    loaded = brain.load_session(session_id, msgs)
    return {"ok": True, "loaded": loaded}


# ── Feedback ───────────────────────────────────────────────────────────────────

@app.post("/feedback")
async def post_feedback(req: FeedbackRequest, request: Request):
    if req.value not in (1, -1):
        raise HTTPException(status_code=400, detail="value must be 1 or -1")

    # Persist to DB
    _sessions.save_feedback(req.message_id, req.value)

    # Record in episodic memory so Kai can learn from it
    if req.snippet:
        memory = _brain_for(request).memory
        label = "positive" if req.value == 1 else "negative"
        entry = f"User gave {label} feedback on this response: {req.snippet[:300]}"
        memory.add_episode(entry, entry_type="event", metadata={"feedback": req.value})

    return {"ok": True}


# ── Response mode ──────────────────────────────────────────────────────────────

_MODE_LABELS = {
    "short":    "Short answers",
    "long":     "Long answers",
    "chat":     "Just chatting",
    "research": "Research",
    "dm":       "DM Mode",
}

_MODE_RULES = {
    "short":    "brief and direct. use bullets and short sentences. skip preamble and conclusions.",
    "long":     "thorough and detailed. explain reasoning, give examples, cover edge cases. don't truncate.",
    "chat":     "conversational and casual. no structure or bullet points needed. talk like a person.",
    "research": "comprehensive and well-structured. include context, comparisons, organize with headers where helpful.",
    "dm":       "narrative and immersive. you are the Dungeon Master — see [CAMPAIGN] block for full DM instructions.",
}


@app.get("/settings/mode")
async def get_mode(request: Request):
    memory = _brain_for(request).memory
    label = memory.get_fact("response_mode") or "Short answers"
    label_to_key = {v: k for k, v in _MODE_LABELS.items()}
    mode = label_to_key.get(label, "short")
    return {"mode": mode, "label": label}


@app.post("/settings/mode")
async def set_mode(req: ModeRequest, request: Request):
    if req.mode not in _MODE_RULES:
        raise HTTPException(status_code=400, detail=f"Invalid mode. Choose from: {list(_MODE_RULES)}")
    memory = _brain_for(request).memory
    from kai.memory import procedural as _proc
    _proc.set_rule("response_length", _MODE_RULES[req.mode], user_id=memory.user_id)
    memory.set_fact("response_mode", _MODE_LABELS[req.mode], source="user_setting")
    return {"ok": True, "mode": req.mode, "label": _MODE_LABELS[req.mode]}


# ── Think mode ─────────────────────────────────────────────────────────────────

@app.get("/settings/think")
async def get_think(request: Request):
    brain = _brain_for(request)
    return {"think": brain._think}


@app.post("/settings/think")
async def set_think(request: Request):
    brain = _brain_for(request)
    brain._think = not brain._think
    return {"think": brain._think}


# ── Model management ──────────────────────────────────────────────────────────

@app.get("/settings/models")
async def get_models(request: Request):
    """List all configured models + which one is active."""
    from kai import models as _models
    brain = _brain_for(request)
    all_models = _models.list_models()
    # Mark which one is currently active
    for m in all_models:
        m["active"] = (m["ollama_id"] == brain.model and m["think"] == brain._think)
    return {"models": all_models}


@app.get("/settings/models/available")
async def get_available_models():
    """List models installed in Ollama (for the 'add model' dropdown)."""
    if not _ollama:
        return {"models": [], "error": "Not initialized"}
    try:
        installed = _ollama.installed_models()
        return {"models": installed}
    except Exception:
        return {"models": [], "error": "Could not reach Ollama"}


@app.post("/settings/models")
async def add_model(req: AddModelRequest, request: Request):
    from kai import models as _models
    name = req.name.strip()
    ollama_id = req.ollama_id.strip()
    if not name or not ollama_id:
        raise HTTPException(status_code=400, detail="Name and model ID are required")
    if len(name) > 30:
        raise HTTPException(status_code=400, detail="Name must be 30 characters or fewer")
    try:
        entry = _models.add_model(name, ollama_id, req.think)
        return {"ok": True, "model": entry}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/settings/models/{name}")
async def delete_model(name: str, request: Request):
    from kai import models as _models
    try:
        removed = _models.remove_model(name)
        if not removed:
            raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/settings/models/active")
async def set_active_model(request: Request):
    """Switch the brain to a different configured model."""
    from kai import models as _models
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Model name is required")
    entry = _models.get_model(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    brain = _brain_for(request)
    brain.model = entry["ollama_id"]
    brain._think = entry.get("think", False)
    return {"ok": True, "model": entry["ollama_id"], "think": entry["think"]}


# ── User auth ──────────────────────────────────────────────────────────────────
# The machine key hash is added server-side to every auth call.
# The browser never sees the machine key — it only sends name + PIN.

@app.get("/users")
async def get_users():
    from kai import users as _users
    return {"names": _users.list_users()}


@app.post("/users/login")
async def login_user(req: LoginRequest, response: Response, request: Request = None):
    """
    Name + PIN login. Machine key is checked invisibly server-side.
    Same error message for wrong PIN vs wrong machine — don't leak which failed.
    Sets an httpOnly session cookie on success. Rate-limited per IP.
    """
    client_ip = request.client.host if request and request.client else "unknown"
    if not _check_login_rate(client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")
    from kai import users as _users
    from kai.device import key_hash
    user = _users.authenticate(req.name.strip(), req.pin.strip(), key_hash())
    if not user:
        raise HTTPException(status_code=401, detail="Invalid name or PIN")

    # Issue session token
    token = _issue_token({"name": user["name"], "user_id": user["id"]})
    response.set_cookie(
        key="kai_session",
        value=token,
        httponly=True,      # JS can't read it — XSS-safe
        samesite="strict",  # never sent cross-site — CSRF-safe
        secure=False,       # HTTP-only server; set True if TLS is added
        max_age=86400 * 7,  # 7 days (matches _SESSION_TTL)
    )

    # Eagerly create the user's Brain so it's warm when they start chatting
    brain = _get_or_create_brain(user["id"])
    brain.memory.set_fact("user_name", user["name"], source="login")
    return {"ok": True, "user": user}


@app.post("/users/register")
async def register_user(req: LoginRequest, response: Response):
    """
    Create a new account, binding it to this machine's key.
    The machine key hash is stored alongside the PIN hash — never the key itself.
    Sets an httpOnly session cookie on success (auto-login after registration).
    """
    from kai import users as _users
    from kai.device import key_hash
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if len(req.pin.strip()) < 4:
        raise HTTPException(status_code=400, detail="PIN must be at least 4 digits")
    user = _users.create_user(name, req.pin.strip(), key_hash())
    if not user:
        raise HTTPException(status_code=409, detail="That name is already taken")

    # Issue session token (auto-login)
    token = _issue_token({"name": name, "user_id": user["id"]})
    response.set_cookie(
        key="kai_session",
        value=token,
        httponly=True,
        samesite="strict",
        secure=False,       # HTTP-only server; set True if TLS is added
        max_age=86400 * 7,  # 7 days (matches _SESSION_TTL)
    )

    # Eagerly create the user's Brain
    brain = _get_or_create_brain(user["id"])
    brain.memory.set_fact("user_name", name, source="login")
    return {"ok": True, "user": user}


@app.post("/users/logout")
async def logout_user(request: Request, response: Response):
    """Destroy the session cookie and invalidate the server-side token."""
    token = request.cookies.get("kai_session")
    if token:
        with _session_tokens_lock:
            _session_tokens.pop(token, None)
    response.delete_cookie("kai_session")
    return {"ok": True}


# ── DM mode ────────────────────────────────────────────────────────────────────

@app.get("/dm/status")
async def dm_status(request: Request):
    """Return current DM mode state and active campaign info."""
    uid = _uid_for(request)
    brain = _brain_for(request)
    active = _campaign.get_active_campaign(user_id=uid)
    return {
        "dm_mode":  brain.dm_mode,
        "campaign": active,
        "campaigns": _campaign.list_campaigns(user_id=uid),
    }


@app.post("/dm/start")
async def dm_start(req: DmStartRequest, request: Request):
    """Enter DM mode. Creates a new campaign if name given, else resumes active."""
    uid = _uid_for(request)
    brain = _brain_for(request)
    if req.campaign_name.strip():
        _campaign.create_campaign(req.campaign_name.strip(), user_id=uid)
    else:
        # Resume: ensure there's an active campaign
        if not _campaign.get_active_campaign(user_id=uid):
            campaigns = _campaign.list_campaigns(user_id=uid)
            if campaigns:
                _campaign.set_active_campaign(campaigns[0]["id"], user_id=uid)
            else:
                raise HTTPException(
                    status_code=400,
                    detail="No active campaign. Provide a campaign_name to start one."
                )
    brain.dm_mode = True
    active = _campaign.get_active_campaign(user_id=uid)
    return {"ok": True, "dm_mode": True, "campaign": active}


@app.post("/dm/stop")
async def dm_stop(request: Request):
    """Exit DM mode (campaign data is preserved)."""
    brain = _brain_for(request)
    brain.dm_mode = False
    return {"ok": True, "dm_mode": False}


@app.post("/dm/campaigns/{campaign_id}/activate")
async def dm_activate_campaign(campaign_id: str, request: Request):
    """Switch to a different campaign."""
    uid = _uid_for(request)
    brain = _brain_for(request)
    if not _campaign.set_active_campaign(campaign_id, user_id=uid):
        raise HTTPException(status_code=404, detail="Campaign not found")
    brain.dm_mode = True
    active = _campaign.get_active_campaign(user_id=uid)
    return {"ok": True, "campaign": active}


# ── Document RAG ─────────────────────────────────────────────────────────────

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB max upload size


@app.post("/docs/upload")
async def upload_doc(file: UploadFile = File(...), request: Request = None):
    """Ingest an uploaded document: extract text, chunk, embed, store."""
    import shutil, tempfile
    from pathlib import Path
    from kai.memory import documents as _docs

    # Content-Length is untrusted (client-controlled) — use as a fast early-reject only.
    # The read loop below is the actual enforcement and cannot be bypassed.
    content_length = request.headers.get("content-length") if request else None
    if content_length and int(content_length) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum upload size is {_MAX_UPLOAD_BYTES // (1024*1024)} MB.",
        )

    uid = _uid_for(request)
    brain = _brain_for(request)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _docs.ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported type '{suffix}'. Allowed: {', '.join(sorted(_docs.ALLOWED_TYPES))}",
        )

    # Save stream to a temp file with size enforcement
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        bytes_written = 0
        while True:
            chunk = file.file.read(65536)
            if not chunk:
                break
            bytes_written += len(chunk)
            if bytes_written > _MAX_UPLOAD_BYTES:
                tmp_path = Path(tmp.name)
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum upload size is {_MAX_UPLOAD_BYTES // (1024*1024)} MB.",
                )
            tmp.write(chunk)
        tmp_path = Path(tmp.name)

    try:
        embed_fn = brain.get_embed_fn()
        meta = _docs.ingest(tmp_path, embed_fn=embed_fn, original_name=file.filename, user_id=uid)

        # Inject the upload as a message in the conversation history
        upload_note = (
            f"[Document uploaded: {file.filename} — "
            f"{meta.get('chunk_count', '?')} chunks, "
            f"{meta.get('char_count', '?')} chars]"
        )
        with brain._history_lock:
            brain._session_history.append(
                {"role": "user", "content": upload_note}
            )

        return {"ok": True, **meta}
    except ValueError as e:
        # ValueError is raised intentionally by _extract_text for known-bad input;
        # safe to surface the message (it's ours, not a library traceback).
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        # Log the real error server-side; never expose library tracebacks to client
        _log.exception("Document ingestion failed")
        raise HTTPException(status_code=500, detail="Document ingestion failed. Check the server log for details.")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/docs/list")
async def list_docs(request: Request):
    from kai.memory import documents as _docs
    uid = _uid_for(request)
    return _docs.list_documents(user_id=uid)


@app.delete("/docs/{doc_id}")
async def delete_doc(doc_id: str, request: Request):
    from kai.memory import documents as _docs
    uid = _uid_for(request)
    ok = _docs.delete_document(doc_id, user_id=uid)
    if not ok:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"ok": True, "deleted": doc_id}


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    brain = _brain_for(request)

    user_input = req.message.strip()
    if not user_input:
        async def empty():
            yield f'data: {json.dumps({"type":"done"})}\n\n'
        return StreamingResponse(empty(), media_type="text/event-stream")

    # Reject excessively long input to prevent context blowout
    if len(user_input) > _MAX_INPUT_CHARS:
        async def too_long():
            yield f'data: {json.dumps({"type":"error","text":f"Message too long ({len(user_input)} chars). Max is {_MAX_INPUT_CHARS}."})}\n\n'
            yield f'data: {json.dumps({"type":"done"})}\n\n'
        return StreamingResponse(too_long(), media_type="text/event-stream")

    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def run_brain() -> None:
        def on_status(text: str) -> None:
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "status", "text": text}), loop
            )

        try:
            for token, done, meta in brain.run_stream(user_input, on_status=on_status):
                if done:
                    event = {"type": "done"}
                    if meta.get("message_id"):
                        event["message_id"] = meta["message_id"]
                elif meta.get("think_step"):
                    event = {"type": "think_step", "text": meta["text"]}
                elif meta.get("think"):
                    event = {"type": "think", "text": meta["text"]}
                else:
                    event = {"type": "token", "text": token}
                asyncio.run_coroutine_threadsafe(q.put(event), loop)
        except Exception as exc:
            # Log real error server-side; send generic message to client
            _log.exception("Chat stream error")
            # Only surface connection errors (user-actionable); hide everything else
            if "connect" in str(exc).lower():
                safe_msg = "Could not reach Ollama. Is it running?"
            else:
                safe_msg = "Something went wrong. Check the server log for details."
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "error", "text": safe_msg}), loop
            )
            asyncio.run_coroutine_threadsafe(q.put({"type": "done"}), loop)
        finally:
            _first_reply_done.set()  # unblock deferred archive thread

    threading.Thread(target=run_brain, daemon=True).start()

    async def stream():
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=300)
            except asyncio.TimeoutError:
                yield f'data: {json.dumps({"type":"error","text":"Response timed out."})}\n\n'
                yield f'data: {json.dumps({"type":"done"})}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "done":
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Startup ────────────────────────────────────────────────────────────────────

# Gate: archive thread waits for first user message to finish before running.
# Without this, the archive's ollama.chat() call competes with the first
# message's embed + chat calls — causing 10-15 min hangs from model swaps.
_first_reply_done = threading.Event()


def _archive_pending_turns(ollama: OllamaClient) -> None:
    """
    Compress any raw episodic turns left over from the previous session
    into a searchable archive. Runs in a background thread.

    Waits for _first_reply_done so the archive's Ollama call never competes
    with the user's first message (which already has cold-start model loading).

    Raw turns (entry_type='turn') are saved by commit_turn after each exchange.
    They only become searchable as archives after history compression or clear-chat.
    If the server was restarted before either fired, those turns are orphaned.
    This recovers them.
    """
    # Wait up to 10 min for the first reply — if it never comes, archive anyway.
    _first_reply_done.wait(timeout=600)

    from kai.memory import episodic as _episodic
    from kai.embed import embed as _fast_embed
    from kai.db import get_conn

    # Collect all user IDs that have pending (uncompressed) turns
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM episodic_entries WHERE entry_type = 'turn'"
    ).fetchall()
    user_ids = [r[0] for r in rows] if rows else [0]

    for uid in user_ids:
        pending = _episodic.get_pending_turns_text(user_id=uid)
        if not pending:
            continue
        memory = MemoryManager(embed_fn=_fast_embed, user_id=uid)
        try:
            resp = ollama.chat(
                messages=_build_compress_messages(pending[:4000]),
                model=cfg.CHAT_MODEL,
                think=False,
                temperature=cfg.TEMPERATURE_TOOL,
            )
            summary = resp.get("message", {}).get("content", "").strip()
            _, summary = _strip_thinking(summary)
            if summary:
                memory.archive_history(summary)
                print(f"[✓] Archived {len(pending.splitlines())} lines for user {uid}")
        except Exception as exc:
            print(f"[!] Startup archive failed for user {uid} (non-critical): {exc}")


def _init() -> None:
    global _ollama, _shared_tool_index, _shared_domain_index

    _ollama = OllamaClient()
    if not _ollama.is_alive():
        print("[!] Ollama is not running. Start it with: ollama serve")
        sys.exit(1)

    # Only check chat model at startup — embed model is CPU-based now
    installed = _ollama.installed_models()
    base = cfg.CHAT_MODEL.split(":")[0]
    if cfg.CHAT_MODEL not in installed and base not in {x.split(":")[0] for x in installed}:
        print(f"[!] Model not found: {cfg.CHAT_MODEL}")
        print(f"    ollama pull {cfg.CHAT_MODEL}")
        sys.exit(1)

    # ── Fast CPU embedding — no VRAM, no model swaps ─────────────────────
    from kai.embed import embed as fast_embed, embed_batch as fast_embed_batch, warm_up as _warm_embed
    _warm_embed()  # pre-load ONNX model (~50 MB first-run download)

    # Shared embed function for campaign tools
    _set_embed_fn(fast_embed)

    # Run system-level migrations and seeding (user_id=0)
    _semantic.migrate()
    seed_defaults()
    seed_founding_entry()

    # ── Pre-warm: build shared indexes once so per-user brains skip this step ──
    # Memory router (7 domain embeddings) + tool index (10 category embeddings)
    from kai.memory import router as _router
    try:
        _shared_domain_index = _router.build_domain_index(fast_embed_batch)
    except Exception:
        _shared_domain_index = {}

    try:
        _shared_tool_index = tool_registry.build_category_index(fast_embed_batch)
    except Exception:
        _shared_tool_index = {}

    print(f"[+] Kai ready  —  model: {cfg.CHAT_MODEL}  think: ON")

    # Upgrade awareness — detect version changes and write an episodic memory entry
    from kai.upgrade import check_for_upgrade
    upgrade_msg = check_for_upgrade(embed_fn=fast_embed)
    if upgrade_msg:
        print(f"[+] Upgrade detected: {upgrade_msg[:80]}...")

    # Archive any raw turns left from the previous session so they're searchable
    threading.Thread(target=_archive_pending_turns, args=(_ollama,), daemon=True).start()

    # Register shutdown hook: shut down Brain thread pools + HQ re-embed
    import atexit
    def _on_shutdown():
        with _user_brains_lock:
            for brain in _user_brains.values():
                brain.shutdown()
        print("[~] Shutdown: running HQ re-embed with Qwen...")
        try:
            from kai.embed import shutdown_reembed
            shutdown_reembed()
        except Exception as exc:
            print(f"[!] HQ re-embed failed: {exc}")
    atexit.register(_on_shutdown)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kai web UI")
    parser.add_argument("--port",       type=int, default=7860)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # ── Security middleware ────────────────────────────────────────────────
    # Order matters: last-added = outermost.  CORS must wrap AuthGuard so
    # that preflight OPTIONS requests are answered before the auth check.
    # _SecurityHeaders is outermost so every response gets the headers.
    app.add_middleware(_AuthGuard)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{args.port}",
            f"http://127.0.0.1:{args.port}",
        ],
        allow_credentials=True,   # allow cookies to be sent
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    app.add_middleware(_SecurityHeaders)

    # Serve CSS / JS from kai/static/
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    _init()

    url = f"http://localhost:{args.port}"
    print(f"[✓] Serving at  {url}")
    print(f"[✓] CORS locked to {url}  •  Auth: session cookie")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
