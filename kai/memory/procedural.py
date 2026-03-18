"""
Procedural memory — behavioral rules and style preferences.
Examples: tone=direct, response_length=brief, swearing=contextual_ok
"""
from datetime import datetime

from kai.db import get_conn
from kai.schema import ProceduralRule


def set_rule(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO procedural_rules (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, datetime.now().isoformat())
    )
    conn.commit()


def get_rule(key: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM procedural_rules WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def list_rules() -> list[ProceduralRule]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT key, value, updated_at FROM procedural_rules ORDER BY key"
    ).fetchall()
    return [
        ProceduralRule(key=row[0], value=row[1], updated_at=datetime.fromisoformat(row[2]))
        for row in rows
    ]


def seed_defaults() -> None:
    """Set sensible defaults on first run. Won't overwrite existing rules."""
    defaults = {
        "tone":            "direct, honest, a bit of edge — no corporate polish",
        "response_length": "brief by default, detailed when the task needs it",
        "language":        "plain english, contextual slang ok, no slurs",
        "initiative":      "suggest things proactively, don't wait to be asked",
        "system_actions":  "report first, act second, always confirm before changing anything",
    }
    conn = get_conn()
    for key, value in defaults.items():
        existing = conn.execute(
            "SELECT 1 FROM procedural_rules WHERE key = ?", (key,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO procedural_rules (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, datetime.now().isoformat())
            )
    conn.commit()
