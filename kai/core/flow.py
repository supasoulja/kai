"""
kai/flow.py — turn-flow recorder: the X-ray for a single conversation turn.

The events table holds what the UI shows (stream tokens, tool labels).
This log holds what actually happened INSIDE the turn — every model request
and raw response, thinking, tool calls with args and outputs, injected
context blocks, discarded text, fallbacks. One row per step, keyed by the
turn's trace_id, so a whole turn can be replayed in order.

Viewers: `:flow` in the CLI, GET /debug/flow in the web API.
Toggle with FLOW_TRACE in config.py. Recording must never break a turn —
every entry point swallows its own errors.
"""
import json
import time

import kai.config as cfg
from kai.core._app_state import get_current_user_id
from kai.store.db import get_conn

_MAX_FIELD = 6000   # truncate giant payload values (full file dumps etc.)
_schema_ready = False

# Retention: trim the oldest rows past cfg.FLOW_LOG_MAX so the debug log can't
# grow without bound. Checked every _TRIM_EVERY inserts rather than per-row.
_TRIM_EVERY = 100
_writes_since_trim = 0

# Live taps — callbacks invoked synchronously for every recorded step, so the
# CLI (:flowlive) and the web debug page can watch a turn AS it happens.
# Tap errors never propagate; a broken viewer must not break a turn.
_taps: list = []


def subscribe(fn) -> None:
    """Register fn(trace_id, kind, data_dict) to receive every step live."""
    if fn not in _taps:
        _taps.append(fn)


def unsubscribe(fn) -> None:
    try:
        _taps.remove(fn)
    except ValueError:
        pass


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flow_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            ts       REAL NOT NULL,
            kind     TEXT NOT NULL,
            data     TEXT NOT NULL,
            user_id  INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migrate existing DBs created before flow rows were user-scoped.
    try:
        conn.execute("ALTER TABLE flow_log ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flow_trace ON flow_log(trace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flow_user ON flow_log(user_id)")
    conn.commit()
    _schema_ready = True


def record(trace_id: str, kind: str, **payload) -> None:
    """Append one step to the turn's flow. Fire-and-forget."""
    if not getattr(cfg, "FLOW_TRACE", False):
        return
    try:
        data = {}
        for k, v in payload.items():
            if not (v is None or isinstance(v, (int, float, bool))):
                v = str(v)
                if len(v) > _MAX_FIELD:
                    v = v[:_MAX_FIELD] + f"… [+{len(v) - _MAX_FIELD} chars]"
            data[k] = v
    except Exception:
        return
    # Live viewers first — they shouldn't depend on the DB write succeeding.
    for fn in list(_taps):
        try:
            fn(trace_id, kind, data)
        except Exception:
            pass
    try:
        conn = get_conn()
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO flow_log (trace_id, ts, kind, data, user_id) VALUES (?, ?, ?, ?, ?)",
            (trace_id, time.time(), kind, json.dumps(data), get_current_user_id()),
        )
        conn.commit()
        _maybe_trim(conn)
    except Exception:
        pass  # observability must never break a turn


def _maybe_trim(conn) -> None:
    """Drop the oldest flow_log rows past the retention cap. Cheap & amortized:
    only runs once every _TRIM_EVERY inserts, not on every row."""
    global _writes_since_trim
    _writes_since_trim += 1
    if _writes_since_trim < _TRIM_EVERY:
        return
    _writes_since_trim = 0
    cap = getattr(cfg, "FLOW_LOG_MAX", 5000)
    try:
        conn.execute(
            "DELETE FROM flow_log WHERE id <= "
            "(SELECT MAX(id) FROM flow_log) - ?",
            (cap,),
        )
        conn.commit()
    except Exception:
        pass


def get_flow(trace_id: str, user_id: int | None = None) -> list[dict]:
    """Every step of one turn, in order. Pass user_id to scope the read to one
    user (web routes do this so a turn id can't leak another user's steps);
    None means no filter (local CLI viewer)."""
    try:
        conn = get_conn()
        _ensure_schema(conn)
        sql = "SELECT ts, kind, data FROM flow_log WHERE trace_id = ?"
        params: list = [trace_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
        return [{"ts": r[0], "kind": r[1], **json.loads(r[2])} for r in rows]
    except Exception:
        return []


def recent_turns(limit: int = 10, user_id: int | None = None) -> list[dict]:
    """The last N turns: trace_id, start time, step count, and the user input.
    Pass user_id to scope to one user (None = no filter, for the local CLI)."""
    try:
        conn = get_conn()
        _ensure_schema(conn)
        where = "" if user_id is None else "WHERE user_id = ?"
        scope: list = [] if user_id is None else [user_id]
        rows = conn.execute(f"""
            SELECT trace_id, MIN(ts) AS started, COUNT(*) AS steps FROM flow_log
            {where}
            GROUP BY trace_id ORDER BY started DESC LIMIT ?
        """, (*scope, limit)).fetchall()
        out = []
        for tid, ts, steps in rows:
            row = conn.execute(
                "SELECT data FROM flow_log WHERE trace_id = ? AND kind = 'route' LIMIT 1",
                (tid,),
            ).fetchone()
            user_input = ""
            if row:
                user_input = str(json.loads(row[0]).get("input", ""))[:80]
            out.append({"trace_id": tid, "ts": ts, "steps": steps, "input": user_input})
        return out
    except Exception:
        return []
