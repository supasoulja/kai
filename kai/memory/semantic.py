"""
Semantic memory — key/value facts about the user and world.
Examples: name=<user's name>, preferred_language=Python, timezone=EST
"""

from datetime import datetime

from kai.store.db import get_conn
from kai.store.schema import SemanticFact


def set_fact(
    key: str, value: str, source: str = "conversation", confidence: float = 1.0, user_id: int = 0
) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO semantic_facts (user_id, key, value, source, confidence, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, source=excluded.source, "
        "confidence=excluded.confidence, updated_at=excluded.updated_at",
        (user_id, key, value, source, confidence, datetime.now().isoformat()),
    )
    conn.commit()


def get_fact(key: str, user_id: int = 0) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM semantic_facts WHERE user_id = ? AND key = ?", (user_id, key)
    ).fetchone()
    return row[0] if row else None


def delete_fact(key: str, user_id: int = 0) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM semantic_facts WHERE user_id = ? AND key = ?", (user_id, key))
    conn.commit()


def migrate(user_id: int = 0) -> None:
    """
    One-time cleanup: remove volatile sys_* keys saved by older code.
    These are runtime stats (CPU%, temps, etc.) that don't belong in long-term memory.
    Safe to call on every startup — no-ops if keys don't exist.
    """
    from kai.memory.extractor import VOLATILE_DB_KEYS

    conn = get_conn()
    for key in VOLATILE_DB_KEYS:
        conn.execute("DELETE FROM semantic_facts WHERE user_id = ? AND key = ?", (user_id, key))
    conn.commit()


def list_facts(user_id: int = 0) -> list[SemanticFact]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT key, value, source, confidence, updated_at "
        "FROM semantic_facts WHERE user_id = ? ORDER BY key",
        (user_id,),
    ).fetchall()
    return [
        SemanticFact(
            key=row[0],
            value=row[1],
            source=row[2],
            confidence=row[3],
            updated_at=datetime.fromisoformat(row[4]),
        )
        for row in rows
    ]


def _cap_numbered(conn, user_id: int, base_key: str, cap: int) -> int:
    """Keep only the `cap` most-recently-updated `base_key_N` facts; delete the
    rest. Returns how many were deleted."""
    rows = conn.execute(
        "SELECT key FROM semantic_facts "
        "WHERE user_id = ? AND key LIKE ? ESCAPE '\\' "
        "ORDER BY updated_at DESC",
        (user_id, base_key + "\\_%"),
    ).fetchall()
    stale = [r[0] for r in rows[cap:]]
    for key in stale:
        conn.execute("DELETE FROM semantic_facts WHERE user_id = ? AND key = ?", (user_id, key))
    return len(stale)


def review_facts(
    user_id: int = 0,
    decay: float = 0.1,
    purge_below: float = 0.3,
    pref_cap: int = 20,
    stale_days: int = 30,
) -> dict:
    """Sleep-time fact maintenance — 'use it or lose it' for low-trust facts.

    - Decay: facts below confidence 1.0 (the inferred regex guesses; explicit
      and observed facts sit at 1.0 and are left alone) that haven't been
      re-confirmed in `stale_days` lose `decay` confidence. Re-stating a fact
      refreshes its updated_at and restores the pattern confidence, so active
      facts never decay.
    - Purge: once a decayed fact drops below `purge_below`, it's deleted.
    - Cap: accumulated preference_N is trimmed to the `pref_cap` most recent.

    Returns {"decayed": n, "purged": n}.
    """
    conn = get_conn()
    now = datetime.now()
    decayed = purged = 0
    for f in list_facts(user_id=user_id):
        if f.confidence >= 1.0:
            continue  # explicit / observed facts are permanent
        if (now - f.updated_at).days < stale_days:
            continue  # recently confirmed — leave it
        new_conf = round(f.confidence - decay, 4)
        if new_conf < purge_below:
            conn.execute(
                "DELETE FROM semantic_facts WHERE user_id = ? AND key = ?", (user_id, f.key)
            )
            purged += 1
        else:
            conn.execute(
                "UPDATE semantic_facts SET confidence = ? WHERE user_id = ? AND key = ?",
                (new_conf, user_id, f.key),
            )
            decayed += 1
    purged += _cap_numbered(conn, user_id, "preference", pref_cap)
    conn.commit()
    return {"decayed": decayed, "purged": purged}
