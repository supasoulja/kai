"""
Semantic memory — key/value facts about the user and world.
Examples: name=Alex, preferred_language=Python, timezone=EST
"""
from datetime import datetime

from kai.db import get_conn
from kai.schema import SemanticFact


def set_fact(key: str, value: str, source: str = "conversation", confidence: float = 1.0) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO semantic_facts (key, value, source, confidence, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source, "
        "confidence=excluded.confidence, updated_at=excluded.updated_at",
        (key, value, source, confidence, datetime.now().isoformat())
    )
    conn.commit()


def get_fact(key: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM semantic_facts WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def delete_fact(key: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM semantic_facts WHERE key = ?", (key,))
    conn.commit()


def migrate() -> None:
    """
    One-time cleanup: remove volatile sys_* keys saved by older code.
    These are runtime stats (CPU%, temps, etc.) that don't belong in long-term memory.
    Safe to call on every startup — no-ops if keys don't exist.
    """
    from kai.memory.extractor import VOLATILE_DB_KEYS
    conn = get_conn()
    for key in VOLATILE_DB_KEYS:
        conn.execute("DELETE FROM semantic_facts WHERE key = ?", (key,))
    conn.commit()


def list_facts() -> list[SemanticFact]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT key, value, source, confidence, updated_at "
        "FROM semantic_facts ORDER BY key"
    ).fetchall()
    return [
        SemanticFact(
            key=row[0], value=row[1], source=row[2],
            confidence=row[3], updated_at=datetime.fromisoformat(row[4])
        )
        for row in rows
    ]
