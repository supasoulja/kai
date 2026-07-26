"""
Kai — web interface.

Usage:
    python web.py              # starts on port 7860, opens browser
    python web.py --port 8080
    python web.py --no-browser
"""

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import secrets
import socket as _socket
import sys
import threading
import time as _time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

_log = logging.getLogger(__name__)

try:
    import uvicorn
except ModuleNotFoundError:
    print("ERROR: uvicorn is not installed in this Python environment.")
    print(f"  Python executable: {sys.executable}")
    print(f"  Fix:  {sys.executable} -m pip install uvicorn[standard]")
    sys.exit(1)
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import kai.config as cfg

# ── Pydantic models ───────────────────────────────────────────────────────────
# Request shapes live in kai/api/models.py so routers and web.py share them.
from kai.api.models import ChatRequest, GreetingRequest, LoginRequest
from kai.core import bootstrap, lifecycle
from kai.core import events as _events
from kai.core._app_state import set_embed_fn as _set_embed_fn
from kai.core.brain import _build_compress_messages, _strip_thinking
from kai.llm.ollama import OllamaClient
from kai.memory.manager import MemoryManager
from kai.tools import registry as tool_registry
from kai.util import log as _klog

# Maximum input length — prevents accidental context blowout
_MAX_INPUT_CHARS = 8000


# ── Session auth ──────────────────────────────────────────────────────────────
# DB-backed session tokens — survive server restarts.
# The cookie holds the raw token; the DB stores only SHA-256(token) so that
# reading the DB file cannot be used to forge a valid session cookie.

_SESSION_TTL = 86400 * 7  # 7 days — matches cookie max_age

# Set by main() before server starts. Controls cookie `secure` flag.
_tls_active = False

import hashlib as _hashlib


def _hash_token(token: str) -> str:
    """SHA-256 of a session token — what is stored in the DB, never the raw value."""
    return _hashlib.sha256(token.encode()).hexdigest()


def _issue_token(user_info: dict) -> str:
    """Create, persist (as a hash), and return a new session token.
    Prunes expired tokens as a side effect to prevent unbounded table growth."""
    from kai.store.db import get_conn

    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat()
    expires = (datetime.now() + timedelta(seconds=_SESSION_TTL)).isoformat()
    conn = get_conn()
    conn.execute("DELETE FROM session_tokens WHERE expires_at < ?", (now,))
    conn.execute(
        "INSERT INTO session_tokens (token, user_id, user_name, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (_hash_token(token), user_info["user_id"], user_info["name"], now, expires),
    )
    conn.commit()
    return token  # raw token goes to the cookie; only its hash lives in the DB


def _get_session(token: str) -> dict | None:
    """Look up a session token from the DB; returns user dict or None if absent/expired."""
    if not token:
        return None
    from kai.store.db import get_conn

    now = datetime.now().isoformat()
    conn = get_conn()
    row = conn.execute(
        "SELECT user_id, user_name FROM session_tokens WHERE token = ? AND expires_at > ?",
        (_hash_token(token), now),
    ).fetchone()
    if not row:
        return None
    return {"user_id": row[0], "name": row[1]}


def _revoke_token(token: str) -> None:
    """Delete a session token from the DB."""
    from kai.store.db import get_conn

    conn = get_conn()
    conn.execute("DELETE FROM session_tokens WHERE token = ?", (_hash_token(token),))
    conn.commit()


def _migrate_session_tokens() -> None:
    """Hash any plaintext session tokens left over from before this change.
    Existing browser cookies still work — the client sends the plaintext token,
    we hash it on lookup, which now matches the upgraded DB value."""
    from kai.store.db import get_conn

    conn = get_conn()
    rows = conn.execute("SELECT token FROM session_tokens").fetchall()
    for (tok,) in rows:
        if len(tok) != 64:  # SHA-256 hex is always 64 chars; anything else is plaintext
            conn.execute(
                "UPDATE session_tokens SET token = ? WHERE token = ?",
                (_hash_token(tok), tok),
            )
    conn.commit()


# ── Login rate limiting ──────────────────────────────────────────────────────
# Limits login attempts per IP to prevent brute-forcing the 4-digit PIN.
# Window = 15 minutes, max 5 attempts. Persisted in the DB so a server restart
# can't reset the counter.

_LOGIN_WINDOW = 900  # 15 minutes in seconds
_LOGIN_MAX_TRIES = 5


def _check_login_rate(ip: str) -> bool:
    """Return True if this IP is allowed to attempt login, False if rate-limited.
    Writes an attempt record and prunes expired ones on each call."""
    from kai.store.db import get_conn

    now = _time.time()
    window_start = now - _LOGIN_WINDOW
    conn = get_conn()
    # Prune expired attempts to keep the table small
    conn.execute("DELETE FROM login_attempts WHERE ts < ?", (window_start,))
    count = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND ts > ?",
        (ip, window_start),
    ).fetchone()[0]
    if count >= _LOGIN_MAX_TRIES:
        conn.commit()
        return False
    conn.execute("INSERT INTO login_attempts (ip, ts) VALUES (?, ?)", (ip, now))
    conn.commit()
    return True


class _AuthGuard:
    """
    Raw ASGI middleware — rejects unauthenticated requests to protected routes.

    Written as a raw ASGI app (not BaseHTTPMiddleware) so that streaming
    responses (SSE chat) are never buffered.
    """

    # Routes that never require auth (no cookie parsing needed)
    _PUBLIC = frozenset(
        {
            "/login",
            "/users",
            "/users/login",
            "/users/register",
            "/api/show-window",  # single-instance lock (desktop app)
            "/voice/test",  # audio pipeline test — no auth, no kokoro
        }
    )
    _PUBLIC_PREFIXES = ("/static/", "/ws/", "/computer")

    # Routes that parse the cookie but don't reject if missing
    # /dashboard/stats is NOT here — it requires auth to prevent user_id=0 data leaks.
    _OPTIONAL_AUTH = frozenset({"/", "/users/logout"})

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
                        token = part[len("kai_session=") :]
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

    _BASE = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    ]

    def __init__(self, app):
        self.app = app
        # CSP: 'unsafe-inline' is required for scripts because app.html carries
        # two inline <script> blocks (theme snippet + sidebar helpers). Everything
        # else is self-hosted (Tailwind pre-built, marked/dompurify vendored,
        # fonts vendored) — no external origins remain.
        #
        # 'unsafe-eval' is added ONLY for the desktop app: pywebview builds its
        # JS↔Python bridge with `new Function(...)` (webview/js/api.js), which a
        # strict CSP blocks — throwing an EvalError that aborts page scripts and
        # leaves the window dead. The browser served by web.py has no such bridge,
        # so it stays strict. app.py sets KAI_ENTRYPOINT="app" before setup_app.
        script_src = b"script-src 'self' 'unsafe-inline'"
        if os.environ.get("KAI_ENTRYPOINT") == "app":
            script_src += b" 'unsafe-eval'"
        csp = (
            b"default-src 'self'; " + script_src + b"; "
            b"style-src 'self' 'unsafe-inline'; "
            b"img-src 'self' data:; "
            b"connect-src 'self'; "
            b"font-src 'self'; "
            b"object-src 'none'; "
            b"base-uri 'self'; "
            b"form-action 'self'"
        )
        self._headers = self._BASE + [(b"content-security-policy", csp)]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(self._headers)
                # Stop the embedded desktop webview from pinning to a stale
                # app.js/CSS bundle (it has no hard-refresh) — always re-fetch
                # the frontend. Skip routes that set their own (SSE = no-cache).
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        return await self.app(scope, receive, send_with_headers)


# ── Helpers ────────────────────────────────────────────────────────────────────
# get_user / uid_for live in kai/api/deps.py so routers share them. Kept under
# their original private names here for the routes still defined in this module.
from kai.api.deps import get_user as _get_user

# ── App state ──────────────────────────────────────────────────────────────────

app = FastAPI(title="Kai")

# Domain routers extracted from this module (see kai/api/). Self-contained:
# voice depends only on kai.audio; study depends only on kai.api.deps + kai.store.db.
from kai.api import admin as _admin_router
from kai.api import study as _study_router
from kai.api import voice as _voice_router
from kai.api.routes import (
    dashboard as _dashboard_router,
)
from kai.api.routes import (
    debug as _debug_router,
)
from kai.api.routes import (
    documents as _documents_router,
)
from kai.api.routes import (
    feedback as _feedback_router,
)
from kai.api.routes import (
    memory as _memory_router,
)
from kai.api.routes import (
    pages as _pages_router,
)
from kai.api.routes import (
    sessions as _sessions_router,
)
from kai.api.routes import (
    settings as _settings_router,
)
from kai.api.routes import (
    system as _system_router,
)

for _r in (
    _voice_router,
    _study_router,
    _admin_router,
    _pages_router,
    _dashboard_router,
    _memory_router,
    _sessions_router,
    _feedback_router,
    _settings_router,
    _debug_router,
    _documents_router,
    _system_router,
):
    app.include_router(_r.router)

# Brain registry + shared singletons now live in kai/api/state.py so route
# modules can reach them without importing this entrypoint. _init() below
# populates the shared singletons (_state.ollama + prebuilt indexes). The
# private aliases keep the existing route call-sites unchanged. user_brains /
# user_brains_lock are stable objects (mutated in place), so aliasing is safe;
# _state.ollama is reassigned at init, so it is always read as _state.ollama.
from kai.api import state as _state

_user_brains = _state.user_brains
_user_brains_lock = _state.user_brains_lock
_get_or_create_brain = _state.get_or_create_brain
_brain_for = _state.brain_for

# ── Routes ─────────────────────────────────────────────────────────────────────
# Static HTML pages + asset stamping now live in kai/api/routes/pages.py;
# STATIC_DIR is re-imported here for the /static file mount in _init().
# Dashboard / memory / sessions / feedback / settings / debug / documents routes
# were extracted to kai/api/routes/ (mounted above via include_router).
from kai.api.routes.pages import STATIC_DIR as _STATIC_DIR

# ── User auth ──────────────────────────────────────────────────────────────────
# The machine key hash is added server-side to every auth call.
# The browser never sees the machine key — it only sends name + PIN.


@app.get("/users")
async def get_users():
    from kai.store import users as _users

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
        raise HTTPException(
            status_code=429, detail="Too many login attempts. Try again in 15 minutes."
        )
    from kai.store import users as _users
    from kai.system.device import key_hash

    user = _users.authenticate(req.name.strip(), req.pin.strip(), key_hash())
    if not user:
        raise HTTPException(status_code=401, detail="Invalid name or PIN")

    # Issue session token
    token = _issue_token({"name": user["name"], "user_id": user["id"]})
    response.set_cookie(
        key="kai_session",
        value=token,
        httponly=True,
        samesite="strict",
        secure=_tls_active,
        max_age=86400 * 7,
    )

    # Eagerly create the user's Brain so it's warm when they start chatting
    brain = _get_or_create_brain(user["id"])
    brain.memory.set_fact("user_name", user["name"], source="login")
    return {"ok": True, "user": user}


@app.post("/users/register")
async def register_user(req: LoginRequest, response: Response):
    """
    Create a new account, binding it to this machine's key.
    Only allowed when no users exist yet (first-run setup). After the first account
    is created, new users must be added by the owner via the UI — not via this endpoint.
    """
    from kai.store import users as _users
    from kai.system.device import key_hash

    if _users.user_count() > 0:
        raise HTTPException(
            status_code=403,
            detail="Registration is closed. Ask the owner of this Kai to add you.",
        )
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
        secure=_tls_active,
        max_age=86400 * 7,
    )

    # Eagerly create the user's Brain
    brain = _get_or_create_brain(user["id"])
    brain.memory.set_fact("user_name", name, source="login")
    return {"ok": True, "user": user}


@app.post("/users/add")
async def add_user(req: LoginRequest, request: Request):
    """
    Owner-only: create a new account on this machine for someone else.
    Unlike /users/register, this doesn't touch the caller's session — the
    owner stays logged in as themselves and the new user logs in separately.
    """
    from kai.store import users as _users
    from kai.system.device import key_hash

    user = _get_user(request)
    if not user or user["user_id"] != _users.get_owner_id():
        raise HTTPException(status_code=403, detail="Only the owner can add users.")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if len(req.pin.strip()) < 4:
        raise HTTPException(status_code=400, detail="PIN must be at least 4 digits")
    new_user = _users.create_user(name, req.pin.strip(), key_hash())
    if not new_user:
        raise HTTPException(status_code=409, detail="That name is already taken")
    return {"ok": True, "user": new_user}


@app.post("/users/logout")
async def logout_user(request: Request, response: Response):
    """Destroy the session cookie and invalidate the server-side token."""
    token = request.cookies.get("kai_session")
    if token:
        _revoke_token(token)
    response.delete_cookie("kai_session")
    return {"ok": True}


@app.get("/users/account/export")
async def export_account(request: Request):
    """
    Download a zip of everything stored for the authenticated user: a data.json
    dump of their kai.db rows plus the three per-user .db files (tree/state/
    knowledge) and their study library. Makes account deletion verifiable —
    the export shows exactly what delete_user() wipes.
    """
    user = _get_user(request)
    if not user or user.get("user_id", 0) == 0:
        raise HTTPException(status_code=401, detail="Authentication required")
    uid = user["user_id"]

    import io
    import zipfile
    from pathlib import Path

    from kai.memory import knowledge as _knowledge
    from kai.memory import state as _state
    from kai.memory import tree as _tree
    from kai.store import users as _users

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("data.json", json.dumps(_users.export_user_data(uid), indent=2))
            # Per-user SQLite files that live outside kai.db.
            db_files = {
                "tree.db": _tree._db_path(uid),
                "state.db": _state._db_path(uid),
                "knowledge.db": _knowledge._user_db_path(uid),
            }
            for arc_name, path in db_files.items():
                try:
                    p = Path(path)
                    if p.exists():
                        zf.write(p, f"databases/{arc_name}")
                except Exception:
                    pass
            # Downloaded study library (on-disk files).
            try:
                lib_dir = Path(cfg.STUDY_LIBRARY_PATH) / str(uid)
                if lib_dir.is_dir():
                    for f in lib_dir.rglob("*"):
                        if f.is_file():
                            zf.write(f, f"study_library/{f.relative_to(lib_dir)}")
            except Exception:
                pass
        return buf.getvalue()

    payload = await asyncio.to_thread(_build_zip)  # DB reads + file I/O off the loop
    headers = {"Content-Disposition": f'attachment; filename="kai-export-user{uid}.zip"'}
    return Response(content=payload, media_type="application/zip", headers=headers)


@app.delete("/users/account")
async def delete_account(request: Request, response: Response):
    """
    Permanently delete the authenticated user's account and ALL their data.
    Conversations, memories, notes, documents — everything is wiped.
    This cannot be undone.
    """
    user = _get_user(request)
    if not user or user.get("user_id", 0) == 0:
        raise HTTPException(status_code=401, detail="Authentication required")
    uid = user["user_id"]

    # Tear down the in-memory Brain if one exists
    with _user_brains_lock:
        brain = _user_brains.pop(uid, None)
        if brain:
            brain.shutdown()

    from kai.store import users as _users

    deleted = _users.delete_user(uid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Account not found")

    # Clear the session cookie
    token = request.cookies.get("kai_session")
    if token:
        _revoke_token(token)
    response.delete_cookie("kai_session")
    return {"ok": True, "deleted": True}


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    if lifecycle.is_shutting_down():
        raise HTTPException(status_code=503, detail="Kai is shutting down — saving the session.")
    brain = _brain_for(request)

    user_input = req.message.strip()
    if not user_input:

        async def empty():
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(empty(), media_type="text/event-stream")

    # Reject excessively long input to prevent context blowout
    if len(user_input) > _MAX_INPUT_CHARS:

        async def too_long():
            yield f"data: {json.dumps({'type': 'error', 'text': f'Message too long ({len(user_input)} chars). Max is {_MAX_INPUT_CHARS}.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(too_long(), media_type="text/event-stream")

    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def run_brain() -> None:
        def on_status(text: str) -> None:
            asyncio.run_coroutine_threadsafe(q.put({"type": "status", "text": text}), loop)

        for attempt in range(2):
            emitted = False
            try:
                for token, done, meta in brain.run_stream(user_input, on_status=on_status):
                    emitted = True
                    if done:
                        event = {"type": "done"}
                        if meta.get("message_id"):
                            event["message_id"] = meta["message_id"]
                        if meta.get("latency_ms") is not None:
                            event["latency_ms"] = meta["latency_ms"]
                    elif meta.get("confirm_tool"):
                        event = {
                            "type": "confirm_tool",
                            "name": meta["name"],
                            "label": meta["label"],
                        }
                        if meta.get("diff"):
                            event["diff"] = meta["diff"]
                    elif meta.get("think_step"):
                        event = {"type": "think_step", "text": meta["text"]}
                    elif meta.get("think_token"):
                        event = {"type": "think_token", "text": meta["text"]}
                    elif meta.get("think"):
                        event = {"type": "think", "text": meta["text"]}
                    else:
                        event = {"type": "token", "text": token}
                    asyncio.run_coroutine_threadsafe(q.put(event), loop)
                break
            except Exception as exc:
                # Log real error server-side; send generic message to client
                _log.exception("Chat stream error")
                # If Ollama crashed mid-session and nothing streamed yet,
                # try bringing it back up and retry once before giving up.
                if (
                    not emitted
                    and attempt == 0
                    and "connect" in str(exc).lower()
                    and bootstrap.ensure_ollama_running(_state.ollama)
                ):
                    asyncio.run_coroutine_threadsafe(
                        q.put({"type": "status", "text": "Ollama restarted — retrying..."}), loop
                    )
                    continue
                # Only surface connection errors (user-actionable); hide everything else
                if "connect" in str(exc).lower():
                    safe_msg = "Could not reach Ollama. Is it running?"
                else:
                    safe_msg = "Something went wrong. Check the server log for details."
                asyncio.run_coroutine_threadsafe(q.put({"type": "error", "text": safe_msg}), loop)
                asyncio.run_coroutine_threadsafe(q.put({"type": "done"}), loop)
                break
        _first_reply_done.set()  # unblock deferred archive thread

    threading.Thread(target=run_brain, daemon=True).start()

    async def stream():
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=300)
            except TimeoutError:
                yield f"data: {json.dumps({'type': 'error', 'text': 'Response timed out.'})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "done":
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/chat/stop")
async def chat_stop(request: Request):
    """Stop the in-flight turn — aborts tool-call loops and streaming. Keeps partial output."""
    brain = _brain_for(request)
    brain.request_stop()
    return {"ok": True}


@app.post("/chat/greeting")
async def chat_greeting(req: GreetingRequest, request: Request):
    """Kai opens the conversation with a greeting of her own (SSE, like /chat)."""
    if lifecycle.is_shutting_down():
        raise HTTPException(status_code=503, detail="Kai is shutting down — saving the session.")
    brain = _brain_for(request)
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def run_brain() -> None:
        try:
            for token, done, _meta in brain.generate_greeting(fresh=req.fresh):
                event = {"type": "done"} if done else {"type": "token", "text": token}
                asyncio.run_coroutine_threadsafe(q.put(event), loop)
        except Exception:
            _log.exception("Greeting stream error")
            # A failed greeting should be silent — just end the stream.
            asyncio.run_coroutine_threadsafe(q.put({"type": "done"}), loop)

    threading.Thread(target=run_brain, daemon=True).start()

    async def stream():
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=120)
            except TimeoutError:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "done":
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Activity event endpoints ──────────────────────────────────────────────────


@app.websocket("/ws/activity/{session_id}")
async def ws_activity(ws: WebSocket, session_id: str):
    """
    Real-time activity stream for a session (powers Kai's Computer).
    Events are pushed as JSON lines.
    """
    # Cookies are available in the HTTP upgrade request before accept().
    # Reject unauthenticated connections: accept then immediately close so the
    # client receives a proper WebSocket close frame (code 1008 = Policy Violation).
    token = ws.cookies.get("kai_session")
    user = _get_session(token) if token else None
    if not user:
        await ws.accept()
        await ws.close(code=1008)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def _on_event(event: _events.Event) -> None:
        asyncio.run_coroutine_threadsafe(q.put(event.to_json()), loop)

    _events.subscribe(session_id, _on_event)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=30)
                await ws.send_text(payload)
            except TimeoutError:
                # Send keepalive ping to detect dead connections
                await ws.send_text('{"type":"ping"}')
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _events.unsubscribe(session_id, _on_event)


@app.get("/api/events/{session_id}")
async def get_events(session_id: str, request: Request, since: float = 0, limit: int = 500):
    """Fetch persisted events for replay / history. Requires an active session."""
    if not _get_user(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return _events.get_events(session_id, since_ts=since, limit=limit)


@app.get("/api/events")
async def list_event_sessions():
    """List all session IDs that have events."""
    return _events.get_session_ids()


# Voice endpoints moved to kai/api/voice.py (mounted via include_router).


# Container control and dev-stats moved to kai/api/routes/system.py
# (mounted via include_router).
# Study mode endpoints moved to kai/api/study.py (mounted via include_router).


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

    from kai.llm.embed import embed as _fast_embed
    from kai.memory import episodic as _episodic
    from kai.store.db import get_conn

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
    _state.ollama = OllamaClient()
    if not bootstrap.ensure_ollama_running(_state.ollama):
        print("[!] Could not reach or start Ollama.")
        print("    Install it from: https://ollama.com/download")
        sys.exit(1)

    # Only check chat model at startup — embed model is CPU-based now
    if not bootstrap.is_model_installed(_state.ollama, cfg.CHAT_MODEL):
        print(f"[!] Model not found: {cfg.CHAT_MODEL}")
        print(f"    ollama pull {cfg.CHAT_MODEL}")
        sys.exit(1)

    # ── Fast CPU embedding — no VRAM, no model swaps ─────────────────────
    from kai.llm.embed import embed as fast_embed
    from kai.llm.embed import embed_batch as fast_embed_batch
    from kai.llm.embed import warm_up as _warm_embed

    _warm_embed()  # pre-load ONNX model (~50 MB first-run download)

    # Shared embed function for tools that need embeddings (e.g. RAG)
    _set_embed_fn(fast_embed)

    # Run system-level migrations and seeding (user_id=0)
    bootstrap.run_migrations_and_seed()
    _migrate_session_tokens()

    # ── Pre-warm: build shared indexes once so per-user brains skip this step ──
    # Memory router (one embedding per domain) + tool index (one per category)
    from kai.memory import router as _router

    try:
        _state.shared_domain_index = _router.build_domain_index(fast_embed_batch)
    except Exception:
        _state.shared_domain_index = {}

    try:
        _state.shared_tool_index = tool_registry.build_category_index(fast_embed_batch)
    except Exception:
        _state.shared_tool_index = {}

    # Warn loudly if any tool is missing its UI label / routing category, so new
    # tools don't silently degrade (no status label, never picked by the router).
    _meta_audit = tool_registry.audit_metadata()
    if _meta_audit["missing_label"]:
        _klog.warn(f"Tools missing a UI label: {_meta_audit['missing_label']}")
    if _meta_audit["uncategorized"]:
        _klog.warn(f"Tools missing a routing category: {_meta_audit['uncategorized']}")
    if _meta_audit.get("stale_risk"):
        _klog.warn(f"Risk tiers naming unknown tools: {_meta_audit['stale_risk']}")

    # ── Pre-warm: load the chat model into VRAM so the first reply isn't slow ──
    # Runs in the background so it doesn't block server startup: a cold load of a
    # large model can take minutes, and the desktop app (app.py) gives up waiting
    # for the server after 30s. The web UI handles an unwarmed model fine — the
    # first request just loads it on demand.
    def _prewarm_chat_model() -> None:
        print(f"[~] Loading {cfg.CHAT_MODEL} into VRAM (background)...")
        t_warm = _time.monotonic()
        try:
            import urllib.request

            warm_payload = json.dumps(
                {
                    "model": cfg.CHAT_MODEL,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "think": False,
                    "keep_alive": "10m",
                    "options": {"num_predict": 1},
                }
            ).encode("utf-8")
            warm_req = urllib.request.Request(
                f"{_state.ollama.base_url}/api/chat",
                data=warm_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(warm_req, timeout=300) as _resp:
                _resp.read()
            print(f"[+] Model loaded in {_time.monotonic() - t_warm:.1f}s")
        except Exception as exc:
            print(f"[!] Model pre-warm failed (non-critical): {exc}")

    threading.Thread(target=_prewarm_chat_model, daemon=True).start()

    # ── Autonomous scheduler ──────────────────────────────────────────────────
    from kai.memory.briefing import generate_and_store as _gen_briefing
    from kai.memory.scheduler import get_scheduler

    sched = get_scheduler()
    sched.add_daily(cfg.BRIEFING_TIME, lambda: _gen_briefing(user_id=0), name="morning-briefing")
    sched.start()
    print(f"[+] Scheduler started — daily briefing at {cfg.BRIEFING_TIME}")

    print(f"[+] Kai ready  —  model: {cfg.CHAT_MODEL}  think: ON")

    # Upgrade awareness — detect version changes and write an episodic memory entry
    from kai.system.upgrade import check_for_upgrade

    upgrade_msg = check_for_upgrade(embed_fn=fast_embed)
    if upgrade_msg:
        print(f"[+] Upgrade detected: {upgrade_msg[:80]}...")

    # Pre-warm STT + TTS sequentially — both use onnxruntime and hang if loaded
    # simultaneously. Loading one after the other avoids ONNX contention.
    def _warm_audio():
        try:
            from kai.audio import _get_whisper

            _get_whisper()
            print("[+] Whisper STT ready")
        except Exception as exc:
            print(f"[!] Whisper pre-warm failed: {exc}")
        try:
            from kai.audio import _get_kokoro

            _get_kokoro()
            print("[+] Kokoro TTS ready")
        except Exception as exc:
            print(f"[!] Kokoro pre-warm failed: {exc}")

    threading.Thread(target=_warm_audio, daemon=True).start()

    # Archive any raw turns left from the previous session so they're searchable
    threading.Thread(target=_archive_pending_turns, args=(_state.ollama,), daemon=True).start()

    # Register shutdown hook: sleep cycle + HQ re-embed for every per-user brain
    import atexit

    def _on_shutdown():
        with _user_brains_lock:
            brains = list(_user_brains.values())
        bootstrap.run_shutdown(_state.ollama, brains, call_brain_shutdown=True)

    atexit.register(_on_shutdown)


def _get_lan_ip() -> str:
    """Return this machine's primary LAN IP without making a real network call."""
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _generate_self_signed_cert(cert_dir: Path, lan_ip: str | None = None) -> tuple[str, str]:
    """Generate a self-signed TLS cert + key. Returns (cert_path, key_path).
    When lan_ip is given, includes it as a SAN so phone browsers accept the cert."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[!] TLS requires the 'cryptography' package.")
        print(f"    Fix: {sys.executable} -m pip install cryptography")
        sys.exit(1)

    cert_dir.mkdir(parents=True, exist_ok=True)
    # Use a separate cert file for LAN mode so it doesn't collide with the localhost cert.
    cert_name = "kai-lan.crt" if lan_ip else "kai.crt"
    key_name = "kai-lan.key" if lan_ip else "kai.key"
    cert_path = cert_dir / cert_name
    key_path = cert_dir / key_name

    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Kai Local")])

    san_entries = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    if lan_ip and lan_ip != "127.0.0.1":
        san_entries.append(x509.IPAddress(ipaddress.ip_address(lan_ip)))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now())
        .not_valid_after(datetime.now() + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"[+] Generated self-signed TLS cert: {cert_path}")
    return str(cert_path), str(key_path)


def setup_app(
    host: str = "127.0.0.1",
    port: int = 7860,
    scheme: str = "http",
    extra_origins: list[str] | None = None,
) -> None:
    """Set up middleware, mount static files, and initialise Kai.

    Call once before starting uvicorn.  Extracted from main() so that
    both the browser-based launcher and the desktop app shell can share
    the same init path.
    """
    # Mirror console output into the in-memory ring buffer so the dashboard's
    # Server Console panel shows what the launching terminal sees. Install first
    # so init/model-load logs below are captured too.
    from kai.util import logbuf

    logbuf.install()

    origins = [
        f"{scheme}://localhost:{port}",
        f"{scheme}://127.0.0.1:{port}",
    ] + (extra_origins or [])

    app.add_middleware(_AuthGuard)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    app.add_middleware(_SecurityHeaders)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    _init()


def main() -> None:
    global _tls_active

    # Record the entry point so a hard restart knows how to relaunch (re-exec).
    os.environ.setdefault("KAI_ENTRYPOINT", "web")

    parser = argparse.ArgumentParser(description="Kai web UI")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--tls", action="store_true", help="Enable HTTPS with an auto-generated self-signed cert"
    )
    parser.add_argument("--cert", help="Path to TLS certificate file")
    parser.add_argument("--key", help="Path to TLS private key file")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="Bind to 0.0.0.0 with auto-TLS so phones on the same network can connect",
    )
    args = parser.parse_args()

    # Certs live under var/ (honors KAI_VAR_DIR), not inside the source package.
    cert_dir = cfg.TLS_DIR
    _old_cert_dir = Path(__file__).parent / "kai" / "memory" / "kai's memory" / "tls"
    if _old_cert_dir.is_dir() and not cert_dir.exists():
        try:
            cert_dir.parent.mkdir(parents=True, exist_ok=True)
            _old_cert_dir.replace(cert_dir)  # one-time migration of existing certs
        except Exception:
            pass

    # ── LAN mode — binds to 0.0.0.0 and auto-generates a cert with LAN IP SAN ──
    lan_ip = None
    ssl_certfile = None
    ssl_keyfile = None

    if args.lan:
        args.host = "0.0.0.0"
        lan_ip = _get_lan_ip()
        ssl_certfile, ssl_keyfile = _generate_self_signed_cert(cert_dir, lan_ip=lan_ip)
        _tls_active = True
    elif args.cert and args.key:
        ssl_certfile, ssl_keyfile = args.cert, args.key
        _tls_active = True
    elif args.tls:
        ssl_certfile, ssl_keyfile = _generate_self_signed_cert(cert_dir)
        _tls_active = True

    # ── Host binding safety ───────────────────────────────────────────────
    is_local = args.host in ("127.0.0.1", "localhost", "::1")
    if not is_local and not _tls_active:
        print("[!] DANGER: binding to a non-localhost address without TLS.")
        print("    Session cookies will be sent in plaintext over the network.")
        print("    Add --lan to auto-generate a LAN cert, or use --cert/--key.")
        sys.exit(1)

    scheme = "https" if _tls_active else "http"

    # ── Middleware + init ─────────────────────────────────────────────────
    extra_origins = [f"https://{lan_ip}:{args.port}"] if lan_ip else []
    setup_app(host=args.host, port=args.port, scheme=scheme, extra_origins=extra_origins)

    url = f"{scheme}://localhost:{args.port}"
    print(f"[✓] Serving at  {url}")
    tls_label = "TLS" if _tls_active else "HTTP"
    print(f"[✓] CORS locked to {url}  •  Auth: session cookie ({tls_label})")

    if lan_ip:
        phone_url = f"https://{lan_ip}:{args.port}"
        print(f"[✓] Phone URL:  {phone_url}")
        print("    → Open on phone, tap Advanced → Proceed (one-time cert trust)")
        print("    → After trusting: Add to Home Screen for app-like access")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


if __name__ == "__main__":
    main()
