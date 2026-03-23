"""
Trace log — records what happened each turn: context size, tool calls, timing.
Lightweight. Stored in SQLite. Accessible via :trace CLI command.
"""
import json
from dataclasses import dataclass
from datetime import datetime

from kai.db import get_conn


@dataclass
class TraceEntry:
    trace_id:    str
    timestamp:   str
    user_input:  str
    model:       str
    context_len: int           # characters in system prompt
    tool_calls:  list[str]     # tool names called
    elapsed_ms:  int           # wall time for the full turn
    response_len: int          # characters in final response
    user_id:     int = 0


def record(entry: TraceEntry) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO trace_log "
            "(trace_id, user_id, timestamp, user_input, model, context_len, tool_calls, elapsed_ms, response_len) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                entry.trace_id,
                entry.user_id,
                entry.timestamp,
                entry.user_input[:200],
                entry.model,
                entry.context_len,
                json.dumps(entry.tool_calls),
                entry.elapsed_ms,
                entry.response_len,
            ),
        )
        conn.commit()
    except Exception:
        # Trace failure never breaks a conversation, but log in debug mode
        from kai.config import DEBUG
        if DEBUG:
            import traceback; traceback.print_exc()


def recent(limit: int = 10, user_id: int | None = None) -> list[TraceEntry]:
    conn = get_conn()
    if user_id is not None:
        rows = conn.execute(
            "SELECT trace_id, timestamp, user_input, model, context_len, "
            "tool_calls, elapsed_ms, response_len, user_id "
            "FROM trace_log WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT trace_id, timestamp, user_input, model, context_len, "
            "tool_calls, elapsed_ms, response_len, user_id "
            "FROM trace_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        TraceEntry(
            trace_id    = r[0],
            timestamp   = r[1],
            user_input  = r[2],
            model       = r[3],
            context_len = r[4],
            tool_calls  = json.loads(r[5]),
            elapsed_ms  = r[6],
            response_len = r[7],
            user_id     = r[8],
        )
        for r in rows
    ]
