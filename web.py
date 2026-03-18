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
import re
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import kai.config as cfg
from kai.brain import Brain, OllamaClient, COMPRESS_PROMPT, _strip_thinking, _build_compress_messages
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

class LoadSessionRequest(BaseModel):
    pass  # no body needed — session_id is a path param

# Maximum input length — prevents accidental context blowout
_MAX_INPUT_CHARS = 8000


# ── Session auth ──────────────────────────────────────────────────────────────
# In-memory token store. Tokens survive until server restart or explicit logout.

_session_tokens: dict[str, dict] = {}   # token → {"name": str, "user_id": int}


class _AuthGuard:
    """
    Raw ASGI middleware — rejects unauthenticated requests to protected routes.

    Written as a raw ASGI app (not BaseHTTPMiddleware) so that streaming
    responses (SSE chat) are never buffered.
    """

    # Routes that never require auth (no cookie parsing needed)
    _PUBLIC = frozenset({
        "/login", "/users", "/users/login", "/users/register", "/users/logout",
    })
    _PUBLIC_PREFIXES = ("/static/",)

    # Routes that parse the cookie but don't reject if missing
    _OPTIONAL_AUTH = frozenset({"/", "/dashboard/stats"})

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

        user_info = _session_tokens.get(token) if token else None

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
        memory = MemoryManager(embed_fn=_ollama.embed, user_id=user_id)
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

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main app page (or redirect to login if not authenticated)."""
    user = _get_user(request)
    if not user:
        # Not authenticated — serve login page
        return HTMLResponse(content=(_STATIC_DIR / "login.html").read_text(encoding="utf-8"))
    return HTMLResponse(content=(_STATIC_DIR / "app.html").read_text(encoding="utf-8"))


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Serve the standalone login page."""
    return HTMLResponse(content=(_STATIC_DIR / "login.html").read_text(encoding="utf-8"))


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
    brain = _brain_for(request)
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
async def get_session_messages(session_id: str):
    return _sessions.get_messages(session_id)


@app.post("/sessions/{session_id}/load")
async def load_session(session_id: str, request: Request):
    """Restore a past session into the brain's in-memory history."""
    brain = _brain_for(request)
    msgs = _sessions.get_messages(session_id)
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


# ── User auth ──────────────────────────────────────────────────────────────────
# The machine key hash is added server-side to every auth call.
# The browser never sees the machine key — it only sends name + PIN.

@app.get("/users")
async def get_users():
    from kai import users as _users
    return {"names": _users.list_users()}


@app.post("/users/login")
async def login_user(req: LoginRequest, response: Response):
    """
    Name + PIN login. Machine key is checked invisibly server-side.
    Same error message for wrong PIN vs wrong machine — don't leak which failed.
    Sets an httpOnly session cookie on success.
    """
    from kai import users as _users
    from kai.device import key_hash
    user = _users.authenticate(req.name.strip(), req.pin.strip(), key_hash())
    if not user:
        raise HTTPException(status_code=401, detail="Invalid name or PIN")

    # Issue session token
    token = secrets.token_urlsafe(32)
    _session_tokens[token] = {"name": user["name"], "user_id": user["id"]}
    response.set_cookie(
        key="kai_session",
        value=token,
        httponly=True,      # JS can't read it — XSS-safe
        samesite="strict",  # never sent cross-site — CSRF-safe
        secure=False,       # localhost is HTTP, not HTTPS
        max_age=86400 * 7,  # 7 days
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
    token = secrets.token_urlsafe(32)
    _session_tokens[token] = {"name": name, "user_id": user["id"]}
    response.set_cookie(
        key="kai_session",
        value=token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=86400 * 7,
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

@app.post("/docs/upload")
async def upload_doc(file: UploadFile = File(...), request: Request = None):
    """Ingest an uploaded document: extract text, chunk, embed, store."""
    import shutil, tempfile
    from pathlib import Path
    from kai.memory import documents as _docs

    uid = _uid_for(request)
    brain = _brain_for(request)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _docs.ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported type '{suffix}'. Allowed: {', '.join(sorted(_docs.ALLOWED_TYPES))}",
        )

    # Save stream to a temp file; pass original_name so ingest uses the right suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
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
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")
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
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "error", "text": str(exc)}), loop
            )
            asyncio.run_coroutine_threadsafe(q.put({"type": "done"}), loop)
        finally:
            _first_reply_done.set()  # unblock deferred archive thread

    threading.Thread(target=run_brain, daemon=True).start()

    async def stream():
        while True:
            event = await q.get()
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
    # Archive turns for user_id=0 (legacy/default user)
    pending = _episodic.get_pending_turns_text(user_id=0)
    if not pending:
        return
    # Use a temporary MemoryManager for archiving
    memory = MemoryManager(embed_fn=ollama.embed, user_id=0)
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
            print(f"[✓] Archived {len(pending.splitlines())} lines from previous session")
    except Exception as exc:
        print(f"[!] Startup archive failed (non-critical): {exc}")


def _init() -> None:
    global _ollama, _shared_tool_index, _shared_domain_index

    _ollama = OllamaClient()
    if not _ollama.is_alive():
        print("[!] Ollama is not running. Start it with: ollama serve")
        sys.exit(1)

    installed = _ollama.installed_models()
    for m in [cfg.CHAT_MODEL, cfg.EMBED_MODEL]:
        base = m.split(":")[0]
        if m not in installed and base not in {x.split(":")[0] for x in installed}:
            print(f"[!] Model not found: {m}")
            print(f"    ollama pull {m}")
            sys.exit(1)

    # Shared embed function for campaign tools
    _set_embed_fn(lambda text: _ollama.embed(text))

    # Run system-level migrations and seeding (user_id=0)
    _semantic.migrate()
    seed_defaults()
    seed_founding_entry()

    # ── Pre-warm: build shared indexes once so per-user brains skip this step ──
    # Memory router (7 domain embeddings) + tool index (10 category embeddings)
    from kai.memory import router as _router
    try:
        _shared_domain_index = _router.build_domain_index(_ollama.embed_batch)
    except Exception:
        _shared_domain_index = {}

    try:
        _shared_tool_index = tool_registry.build_category_index(_ollama.embed_batch)
    except Exception:
        _shared_tool_index = {}

    print(f"[✓] Kai ready  —  model: {cfg.CHAT_MODEL}  think: ON")

    # Upgrade awareness — detect version changes and write an episodic memory entry
    from kai.upgrade import check_for_upgrade
    upgrade_msg = check_for_upgrade(embed_fn=_ollama.embed)
    if upgrade_msg:
        print(f"[✓] Upgrade detected: {upgrade_msg[:80]}...")

    # Archive any raw turns left from the previous session so they're searchable
    threading.Thread(target=_archive_pending_turns, args=(_ollama,), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kai web UI")
    parser.add_argument("--port",       type=int, default=7860)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # ── Security middleware ────────────────────────────────────────────────
    # Order matters: last-added = outermost.  CORS must wrap AuthGuard so
    # that preflight OPTIONS requests are answered before the auth check.
    app.add_middleware(_AuthGuard)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{args.port}",
            f"http://127.0.0.1:{args.port}",
        ],
        allow_credentials=True,   # allow cookies to be sent
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
