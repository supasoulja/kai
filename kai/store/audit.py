"""
Audit log — who did what to which account, on this machine.

Every privileged action writes one row: an account created, edited, granted, kicked, ownership
handed over, the server restarted or shut down. Refused attempts are written too, with ok=0 —
someone reaching past their level is exactly what this log is for.

Rows outlive the accounts they name. Deleting an account must not erase what was done to it or
by it, so this table is deliberately NOT in users._USER_TABLES, and names are copied into each
row rather than joined. docs/active/PERMISSIONS.md is the contract.
"""

import json
from datetime import datetime

from kai.store.db import get_conn
from kai.util import log


def _ensure_table() -> None:
    # Cheap enough to run on every call, and immune to a test swapping the database out
    # from under a module-level "already created" flag.
    get_conn().execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            actor_id    INTEGER,
            actor_name  TEXT,
            action      TEXT    NOT NULL,
            target_id   INTEGER,
            target_name TEXT,
            detail      TEXT,
            ok          INTEGER NOT NULL DEFAULT 1
        )
    """)


def record(
    action: str,
    *,
    actor_id: int | None = None,
    actor_name: str | None = None,
    target_id: int | None = None,
    target_name: str | None = None,
    detail: dict | None = None,
    ok: bool = True,
) -> None:
    """Write one entry. Never raises: a failed audit write is logged loudly, but it must not
    turn a restart or a sign-out into a crash."""
    try:
        _ensure_table()
        conn = get_conn()
        conn.execute(
            "INSERT INTO audit_log "
            "(ts, actor_id, actor_name, action, target_id, target_name, detail, ok) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                actor_id,
                actor_name,
                action,
                target_id,
                target_name,
                json.dumps(detail) if detail is not None else None,
                1 if ok else 0,
            ),
        )
        conn.commit()
    except Exception as exc:
        log.warn(f"[audit] could not record {action}: {exc}")


def list_entries(limit: int = 200, before_id: int | None = None) -> list[dict]:
    """Newest first. Page backwards with before_id (the smallest id already seen)."""
    _ensure_table()
    sql = (
        "SELECT id, ts, actor_id, actor_name, action, target_id, target_name, detail, ok "
        "FROM audit_log"
    )
    args: list = []
    if before_id is not None:
        sql += " WHERE id < ?"
        args.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    entries = []
    for row in get_conn().execute(sql, args).fetchall():
        try:
            detail = json.loads(row[7]) if row[7] else None
        except ValueError:
            detail = row[7]
        entries.append(
            {
                "id": row[0],
                "ts": row[1],
                "actor_id": row[2],
                "actor_name": row[3],
                "action": row[4],
                "target_id": row[5],
                "target_name": row[6],
                "detail": detail,
                "ok": bool(row[8]),
            }
        )
    return entries
