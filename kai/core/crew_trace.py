"""
kai/core/crew_trace.py — always-on telemetry for the agent-crew path.

The crew (triage → Otto orchestration → coverage dispatch) is flag-gated and
low-volume, but its decisions were invisible in the data: `flow_rec.record` only
persists when FLOW_TRACE is on (off by default) and gets trimmed, so
coverage-dispatch behavior could never be debugged after the fact. This is a
small, dedicated, ALWAYS-ON log — one row per crew decision, keyed by the turn's
trace_id — recording what triage chose, which domains it expected covered (the
coverage set), and every specialist / coverage dispatch that actually ran.

Deliberately separate from flow_log (the verbose full-turn X-ray), mirroring how
cerebellum_log is its own table. Cheap: crew turns are rare and each writes only a
handful of rows. Recording must never break a turn — every path swallows its own
errors. Read with recent() / for_turn().
"""
from __future__ import annotations

import json
import time

from kai.core._app_state import get_current_user_id
from kai.store.db import get_conn

_MAX_FIELD = 4000
_schema_ready = False

# Retention cap — crew turns are low-volume, but never let the log grow unbounded.
_LOG_MAX = 5000
_TRIM_EVERY = 100
_writes_since_trim = 0


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crew_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id   TEXT NOT NULL,
            ts         REAL NOT NULL,
            kind       TEXT NOT NULL,
            data       TEXT NOT NULL,
            user_id    INTEGER NOT NULL DEFAULT 0,
            session_id TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crew_trace ON crew_log(trace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crew_ts ON crew_log(ts)")
    conn.commit()
    _schema_ready = True


def record(trace_id: str, kind: str, *, session_id: str | None = None, **payload) -> None:
    """Append one crew decision to the log. Always on; fire-and-forget.

    kind is one of: "triage" (the routing decision + coverage set + scores),
    "dispatch" (a specialist Otto chose), "coverage_dispatch" (a domain Otto
    skipped that coverage force-ran), "specialist_result" (findings/needs/blocked),
    "finish" (the turn's outcome + coverage accounting).
    """
    try:
        data = {}
        for k, v in payload.items():
            # JSON-native values round-trip as-is (lists/dicts stay structured for
            # querying); everything else is stringified and long strings truncated.
            if v is None or isinstance(v, (int, float, bool, list, dict)):
                data[k] = v
            else:
                s = str(v)
                if len(s) > _MAX_FIELD:
                    s = s[:_MAX_FIELD] + f"… [+{len(s) - _MAX_FIELD} chars]"
                data[k] = s
    except Exception:
        return
    try:
        conn = get_conn()
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO crew_log (trace_id, ts, kind, data, user_id, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trace_id, time.time(), kind, json.dumps(data),
             get_current_user_id(), session_id),
        )
        conn.commit()
        _maybe_trim(conn)
    except Exception:
        pass  # observability must never break a turn


def _maybe_trim(conn) -> None:
    global _writes_since_trim
    _writes_since_trim += 1
    if _writes_since_trim < _TRIM_EVERY:
        return
    _writes_since_trim = 0
    try:
        conn.execute(
            "DELETE FROM crew_log WHERE id <= (SELECT MAX(id) FROM crew_log) - ?",
            (_LOG_MAX,),
        )
        conn.commit()
    except Exception:
        pass


def for_turn(trace_id: str) -> list[dict]:
    """Every crew decision for one turn, in order."""
    try:
        conn = get_conn()
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT ts, kind, data FROM crew_log WHERE trace_id = ? ORDER BY id",
            (trace_id,),
        ).fetchall()
        return [{"ts": r[0], "kind": r[1], **json.loads(r[2])} for r in rows]
    except Exception:
        return []


def recent(limit: int = 50) -> list[dict]:
    """Most-recent crew decisions across all turns (newest first)."""
    try:
        conn = get_conn()
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT ts, trace_id, kind, data FROM crew_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"ts": r[0], "trace_id": r[1], "kind": r[2], **json.loads(r[3])}
                for r in rows]
    except Exception:
        return []
