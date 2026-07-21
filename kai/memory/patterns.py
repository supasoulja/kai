"""
Usage pattern tracking and proactive suggestions.

Tracks what tool calls happen at what times of day / days of week.
After PATTERN_MIN_SAMPLES observations for a (user, tool, hour) cluster,
the pattern is surfaced as a one-line proactive note injected into context.

Example: user runs system.temps every evening around 7 PM →
  "[PATTERN] You usually check temps around this time — want a quick scan?"

All logging is async (fire-and-forget). Pattern detection is a fast DB
aggregate query, never an LLM call.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import kai.config as cfg
from kai.memory.privacy import patterns_enabled

_bg = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pattern-log")

# Human-readable labels for proactive suggestions
_TOOL_SUGGEST: dict[str, str] = {
    "system.temps":           "check temps",
    "system.info":            "run a system check",
    "pc.network_info":        "check your network",
    "files.disk_usage":       "check disk space",
    "files.find_large":       "scan for large files",
    "pc.event_logs":          "scan event logs",
    "search.web":             "search the web",
}


def log_tool_call(tool_name: str, user_id: int = 0, topic: str = "") -> None:
    """Fire-and-forget: record this tool call for pattern analysis."""
    if not patterns_enabled(user_id):
        return
    _bg.submit(_write_pattern, tool_name, user_id, topic)


def _write_pattern(tool_name: str, user_id: int, topic: str) -> None:
    try:
        from kai.store.db import get_conn
        now = datetime.now()
        conn = get_conn()
        conn.execute(
            "INSERT INTO usage_patterns (user_id, tool_name, topic, hour_of_day, day_of_week, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, tool_name, topic or None, now.hour, now.weekday(), time.time()),
        )
        conn.commit()
    except Exception:
        pass


def get_proactive_suggestion(user_id: int = 0) -> str:
    """
    Check whether the current time matches a usage pattern.
    Returns a one-line suggestion string, or empty string.

    Called by context.py on session open — fast DB aggregate, no LLM.
    """
    if not patterns_enabled(user_id):
        return ""
    try:
        return _check_patterns(user_id)
    except Exception:
        return ""


def _check_patterns(user_id: int) -> str:
    from kai.store.db import get_conn
    now = datetime.now()
    hour = now.hour
    dow = now.weekday()
    window = cfg.PATTERN_SUGGEST_WINDOW // 60  # hours around current time

    conn = get_conn()
    # Find tools called at this hour (±window) on this day of week, enough times
    rows = conn.execute(
        """
        SELECT tool_name, COUNT(*) as cnt
        FROM usage_patterns
        WHERE user_id = ?
          AND day_of_week = ?
          AND hour_of_day BETWEEN ? AND ?
        GROUP BY tool_name
        HAVING cnt >= ?
        ORDER BY cnt DESC
        LIMIT 1
        """,
        (user_id, dow, max(0, hour - window), min(23, hour + window),
         cfg.PATTERN_MIN_SAMPLES),
    ).fetchall()

    if not rows:
        return ""

    tool_name = rows[0][0]
    label = _TOOL_SUGGEST.get(tool_name)
    if not label:
        return ""

    return f"[PATTERN] You often {label} around this time — want me to run it?"
