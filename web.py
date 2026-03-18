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

_session_tokens: dict[str, str] = {}   # token → username


class _AuthGuard:
    """
    Raw ASGI middleware — rejects unauthenticated requests to protected routes.

    Written as a raw ASGI app (not BaseHTTPMiddleware) so that streaming
    responses (SSE chat) are never buffered.
    """

    _PUBLIC = frozenset({
        "/", "/users", "/users/login", "/users/register", "/users/logout",
    })

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        if scope.get("path", "") in self._PUBLIC:
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

        if not token or token not in _session_tokens:
            resp = JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
            )
            return await resp(scope, receive, send)

        return await self.app(scope, receive, send)


# ── App state ──────────────────────────────────────────────────────────────────

app    = FastAPI(title="Kai")
_brain:  Brain | None        = None
_memory: MemoryManager | None = None

_HTML = Path(__file__).parent / "kai" / "static" / "index.html"


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=_HTML.read_text(encoding="utf-8"))


_HIGHLIGHT_KEYS = {"user_name", "user_role", "location", "gaming"}
_HIGHLIGHT_LABELS = {
    "user_name": "name",
    "user_role": "role",
    "location":  "location",
    "gaming":    "games",
}

@app.get("/info")
async def info():
    facts   = _memory.list_facts()   if _memory else []
    recents = _memory.recent_episodes(limit=1) if _memory else []

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
        "model":          _brain.model if _brain else "unknown",
        "facts":          len(facts),
        "context_window": cfg.CONTEXT_WINDOW,
        "last_seen":      recents[0].timestamp.strftime("%b %d") if recents else None,
        "highlights":     highlights,
    }


@app.post("/clear")
async def clear():
    if _brain:
        brain = _brain  # capture non-None ref for closure
        # Thread-safe snapshot then clear immediately so new messages start fresh.
        # The snapshot is archived in the background — nothing is lost.
        snapshot = brain.snapshot_history()
        _brain.clear_history()
        if any(m.get("role") != "system" for m in snapshot):
            threading.Thread(
                target=brain.flush_history_snapshot,
                args=(snapshot,),
                daemon=True,
            ).start()
    return {"ok": True}


# ── Memory browser ─────────────────────────────────────────────────────────────

@app.get("/memory/facts")
async def get_memory_facts():
    if not _memory:
        return []
    facts = _memory.list_facts()
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
async def update_memory_fact(key: str, req: FactUpdateRequest):
    if not _memory:
        raise HTTPException(status_code=503, detail="Memory not initialized")
    value = req.value.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Value cannot be empty")
    _memory.set_fact(key, value, source="user_edit")
    return {"ok": True}


@app.delete("/memory/facts/{key}")
async def delete_memory_fact(key: str):
    if not _memory:
        raise HTTPException(status_code=503, detail="Memory not initialized")
    _memory.delete_fact(key)
    return {"ok": True}


@app.get("/memory/episodic")
async def get_memory_episodic():
    """Return episodic summaries (compressed conversation memories)."""
    if not _memory:
        return []
    from kai.memory import episodic as _episodic
    entries = _episodic.recent(limit=50)
    # Return all non-turn entries (summaries + milestones) plus raw turns
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
async def get_sessions():
    return _sessions.list_sessions(limit=50)


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    return _sessions.get_messages(session_id)


@app.post("/sessions/{session_id}/load")
async def load_session(session_id: str):
    """Restore a past session into the brain's in-memory history."""
    if not _brain:
        raise HTTPException(status_code=503, detail="Brain not initialized")
    msgs = _sessions.get_messages(session_id)
    if not msgs:
        raise HTTPException(status_code=404, detail="Session not found or empty")
    loaded = _brain.load_session(session_id, msgs)
    return {"ok": True, "loaded": loaded}


# ── Feedback ───────────────────────────────────────────────────────────────────

@app.post("/feedback")
async def post_feedback(req: FeedbackRequest):
    if req.value not in (1, -1):
        raise HTTPException(status_code=400, detail="value must be 1 or -1")

    # Persist to DB
    _sessions.save_feedback(req.message_id, req.value)

    # Record in episodic memory so Kai can learn from it
    if _memory and req.snippet:
        label = "positive" if req.value == 1 else "negative"
        entry = f"User gave {label} feedback on this response: {req.snippet[:300]}"
        _memory.add_episode(entry, entry_type="event", metadata={"feedback": req.value})

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
async def get_mode():
    label = _memory.get_fact("response_mode") if _memory else None
    label = label or "Short answers"
    label_to_key = {v: k for k, v in _MODE_LABELS.items()}
    mode = label_to_key.get(label, "short")
    return {"mode": mode, "label": label}


@app.post("/settings/mode")
async def set_mode(req: ModeRequest):
    if req.mode not in _MODE_RULES:
        raise HTTPException(status_code=400, detail=f"Invalid mode. Choose from: {list(_MODE_RULES)}")
    from kai.memory import procedural as _proc
    _proc.set_rule("response_length", _MODE_RULES[req.mode])
    if _memory:
        _memory.set_fact("response_mode", _MODE_LABELS[req.mode], source="user_setting")
    return {"ok": True, "mode": req.mode, "label": _MODE_LABELS[req.mode]}


# ── Think mode ─────────────────────────────────────────────────────────────────

@app.get("/settings/think")
async def get_think():
    return {"think": _brain._think if _brain else True}


@app.post("/settings/think")
async def set_think():
    if _brain:
        _brain._think = not _brain._think
    return {"think": _brain._think if _brain else True}


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
    _session_tokens[token] = user["name"]
    response.set_cookie(
        key="kai_session",
        value=token,
        httponly=True,      # JS can't read it — XSS-safe
        samesite="strict",  # never sent cross-site — CSRF-safe
        secure=False,       # localhost is HTTP, not HTTPS
        max_age=86400 * 7,  # 7 days
    )

    if _memory:
        _memory.set_fact("user_name", user["name"], source="login")
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
    _session_tokens[token] = name
    response.set_cookie(
        key="kai_session",
        value=token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=86400 * 7,
    )

    if _memory:
        _memory.set_fact("user_name", name, source="login")
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
async def dm_status():
    """Return current DM mode state and active campaign info."""
    active = _campaign.get_active_campaign()
    return {
        "dm_mode":  _brain.dm_mode if _brain else False,
        "campaign": active,
        "campaigns": _campaign.list_campaigns(),
    }


@app.post("/dm/start")
async def dm_start(req: DmStartRequest):
    """Enter DM mode. Creates a new campaign if name given, else resumes active."""
    if not _brain:
        raise HTTPException(status_code=503, detail="Brain not initialized")
    if req.campaign_name.strip():
        _campaign.create_campaign(req.campaign_name.strip())
    else:
        # Resume: ensure there's an active campaign
        if not _campaign.get_active_campaign():
            campaigns = _campaign.list_campaigns()
            if campaigns:
                _campaign.set_active_campaign(campaigns[0]["id"])
            else:
                raise HTTPException(
                    status_code=400,
                    detail="No active campaign. Provide a campaign_name to start one."
                )
    _brain.dm_mode = True
    active = _campaign.get_active_campaign()
    return {"ok": True, "dm_mode": True, "campaign": active}


@app.post("/dm/stop")
async def dm_stop():
    """Exit DM mode (campaign data is preserved)."""
    if _brain:
        _brain.dm_mode = False
    return {"ok": True, "dm_mode": False}


@app.post("/dm/campaigns/{campaign_id}/activate")
async def dm_activate_campaign(campaign_id: str):
    """Switch to a different campaign."""
    if not _campaign.set_active_campaign(campaign_id):
        raise HTTPException(status_code=404, detail="Campaign not found")
    if _brain:
        _brain.dm_mode = True
    active = _campaign.get_active_campaign()
    return {"ok": True, "campaign": active}


# ── Document RAG ─────────────────────────────────────────────────────────────

@app.post("/docs/upload")
async def upload_doc(file: UploadFile = File(...)):
    """Ingest an uploaded document: extract text, chunk, embed, store."""
    import shutil, tempfile
    from pathlib import Path
    from kai.memory import documents as _docs

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
        # Use the brain's embed_fn if available, else fall back to text-only storage
        embed_fn = _brain.get_embed_fn() if _brain else None
        meta = _docs.ingest(tmp_path, embed_fn=embed_fn, original_name=file.filename)

        # Inject the upload as a message in the conversation history so Kai
        # sees it in the thread (not just the sidebar).  This makes documents
        # part of the dialogue — the model will know a file was shared.
        if _brain:
            upload_note = (
                f"[Document uploaded: {file.filename} — "
                f"{meta.get('chunk_count', '?')} chunks, "
                f"{meta.get('char_count', '?')} chars]"
            )
            with _brain._history_lock:
                _brain._session_history.append(
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
async def list_docs():
    from kai.memory import documents as _docs
    return _docs.list_documents()


@app.delete("/docs/{doc_id}")
async def delete_doc(doc_id: str):
    from kai.memory import documents as _docs
    ok = _docs.delete_document(doc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"ok": True, "deleted": doc_id}


@app.post("/chat")
async def chat(req: ChatRequest):
    if not _brain:
        async def err_stream():
            yield f'data: {json.dumps({"type":"error","text":"Brain not initialized"})}\n\n'
            yield f'data: {json.dumps({"type":"done"})}\n\n'
        return StreamingResponse(err_stream(), media_type="text/event-stream")

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

    brain = _brain  # capture non-None ref for closure
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
    pending = _episodic.get_pending_turns_text()
    if not pending or not _memory:
        return
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
            _memory.archive_history(summary)
            print(f"[✓] Archived {len(pending.splitlines())} lines from previous session")
    except Exception as exc:
        print(f"[!] Startup archive failed (non-critical): {exc}")


def _init() -> None:
    global _brain, _memory

    ollama = OllamaClient()
    if not ollama.is_alive():
        print("[!] Ollama is not running. Start it with: ollama serve")
        sys.exit(1)

    installed = ollama.installed_models()
    for m in [cfg.CHAT_MODEL, cfg.EMBED_MODEL]:
        base = m.split(":")[0]
        if m not in installed and base not in {x.split(":")[0] for x in installed}:
            print(f"[!] Model not found: {m}")
            print(f"    ollama pull {m}")
            sys.exit(1)

    _memory = MemoryManager(embed_fn=ollama.embed)
    _semantic.migrate()   # remove stale volatile sys_* keys from previous sessions
    seed_defaults()
    seed_founding_entry()

    _brain = Brain(
        memory=_memory,
        model=cfg.CHAT_MODEL,
        ollama=ollama,
        tool_registry=tool_registry,
        think=True,
    )
    # Share embed function with campaign tools (avoids circular imports via _app_state)
    _set_embed_fn(lambda text: ollama.embed(text))

    # ── Pre-warm: build indexes now so the first message has zero cold-start ──
    # The boot screen animation runs ~2s on the frontend — plenty of time.
    # Memory router (7 domain embeddings) + tool index (10 category embeddings)
    # are done in two batch calls while the user is still watching the boot text.
    _brain._ensure_memory_router()
    _brain._ensure_tool_index()
    print(f"[✓] Kai ready  —  model: {cfg.CHAT_MODEL}  think: ON")

    # Upgrade awareness — detect version changes and write an episodic memory entry
    from kai.upgrade import check_for_upgrade
    upgrade_msg = check_for_upgrade(embed_fn=ollama.embed)
    if upgrade_msg:
        print(f"[✓] Upgrade detected: {upgrade_msg[:80]}...")

    # Archive any raw turns left from the previous session so they're searchable
    threading.Thread(target=_archive_pending_turns, args=(ollama,), daemon=True).start()


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

    _init()

    url = f"http://localhost:{args.port}"
    print(f"[✓] Serving at  {url}")
    print(f"[✓] CORS locked to {url}  •  Auth: session cookie")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
