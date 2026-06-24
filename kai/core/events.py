"""
Kai Event Bus — one-way event stream from Brain to UI.

Architecture:
    brain.py  →  emit()  →  SQLite log  →  WebSocket subscribers

Events are:
  - Written to SQLite for persistence (replay on reconnect)
  - Broadcast to connected WebSocket clients in real time
  - Keyed by session_id so each thread has its own event stream

The Brain never reads events — this is a pure downstream projection.
"""

import json
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any

# ── Event types ──────────────────────────────────────────────────────────────
# Keep in sync with the frontend event handler.

EVENT_TOOL_START    = "tool.start"      # tool call initiated
EVENT_TOOL_END      = "tool.end"        # tool call completed (success or error)
EVENT_THINK         = "think"           # reasoning/thinking chunk
EVENT_STATUS        = "status"          # status label ("Thinking...", "Responding...")
EVENT_STREAM_TOKEN  = "stream.token"    # response token (batched for efficiency)
EVENT_STREAM_END    = "stream.end"      # response complete
EVENT_MEMORY_READ   = "memory.read"     # memory was consulted
EVENT_MEMORY_WRITE  = "memory.write"    # knowledge extracted / memory updated
EVENT_MODEL_SWITCH  = "model.switch"    # model changed (thinking on/off)
EVENT_FACE          = "face"            # face expression change
EVENT_ERROR         = "error"           # something went wrong


@dataclass
class Event:
    type: str
    session_id: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    event_id: int = 0  # set by the store after insert

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


# ── In-memory subscriber registry ───────────────────────────────────────────

_subscribers: dict[str, list] = defaultdict(list)  # session_id → [callback, ...]
_sub_lock = threading.Lock()


def subscribe(session_id: str, callback) -> None:
    """Register a callback that receives Event objects for this session."""
    with _sub_lock:
        _subscribers[session_id].append(callback)


def unsubscribe(session_id: str, callback) -> None:
    """Remove a previously registered callback."""
    with _sub_lock:
        try:
            _subscribers[session_id].remove(callback)
        except ValueError:
            pass
        if not _subscribers[session_id]:
            del _subscribers[session_id]


def _notify(event: Event) -> None:
    """Send event to all subscribers for this session."""
    with _sub_lock:
        cbs = list(_subscribers.get(event.session_id, []))
    for cb in cbs:
        try:
            cb(event)
        except Exception:
            pass  # don't let a bad subscriber break the pipeline


# ── SQLite persistence ───────────────────────────────────────────────────────

_db_lock = threading.Lock()
_db_conn = None


def _get_db():
    global _db_conn
    if _db_conn is None:
        import sqlite3
        from kai.config import MEMORY_DIR
        db_path = MEMORY_DIR / "events.db"
        _db_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                type       TEXT NOT NULL,
                session_id TEXT NOT NULL,
                ts         REAL NOT NULL,
                data       TEXT NOT NULL
            )
        """)
        _db_conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_session
            ON events(session_id, ts)
        """)
        _db_conn.commit()
    return _db_conn


def _store(event: Event) -> int:
    """Persist event to SQLite. Returns the event_id."""
    with _db_lock:
        conn = _get_db()
        cur = conn.execute(
            "INSERT INTO events (type, session_id, ts, data) VALUES (?, ?, ?, ?)",
            (event.type, event.session_id, event.ts, json.dumps(event.data, default=str)),
        )
        conn.commit()
        return cur.lastrowid


# ── Public API ───────────────────────────────────────────────────────────────

def emit(event_type: str, session_id: str, **data) -> Event:
    """
    Create, persist, and broadcast an event.

    Usage:
        from kai.core.events import emit, EVENT_TOOL_START
        emit(EVENT_TOOL_START, session_id, name="weather.current", args={"location": "Tokyo"})
    """
    event = Event(type=event_type, session_id=session_id, data=data)
    # Persist (skip high-frequency token events to avoid DB bloat)
    if event_type != EVENT_STREAM_TOKEN:
        event.event_id = _store(event)
    # Broadcast
    _notify(event)
    return event


def get_events(session_id: str, since_ts: float = 0, limit: int = 500) -> list[dict]:
    """Fetch persisted events for a session, optionally filtered by timestamp."""
    with _db_lock:
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, type, session_id, ts, data FROM events "
            "WHERE session_id = ? AND ts > ? ORDER BY ts LIMIT ?",
            (session_id, since_ts, limit),
        ).fetchall()
    return [
        {"event_id": r[0], "type": r[1], "session_id": r[2], "ts": r[3],
         "data": json.loads(r[4])}
        for r in rows
    ]


def get_session_ids() -> list[str]:
    """Return all session IDs that have events."""
    with _db_lock:
        conn = _get_db()
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM events ORDER BY session_id"
        ).fetchall()
    return [r[0] for r in rows]
