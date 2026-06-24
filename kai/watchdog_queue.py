"""
Kai Watchdog Queue — device registry, event intake, and bidirectional command queue.

Architecture (events, one-way):
    watchdog/*.py (any PC)  →  POST /api/watchdog/event  →  SQLite queue  →  surfaced in chat

Architecture (commands, bidirectional):
    Kai Brain  →  queue_command()  →  SQLite  →  GET /api/node/{id}/commands  →  agent.py
    agent.py   →  POST /api/node/{id}/result  →  complete_command()  →  cluster tools

A device must first register (via a Kai-issued, single-use join code) to get a
unique device_id + device_key pair. Every subsequent request is authenticated
against that pair — Kai always knows exactly which machine is talking to her,
and a single compromised key only affects one device, not the whole network.
"""

import hashlib
import json
import secrets
import sqlite3
import threading
import time

from kai.config import WATCHDOG_DB


def _hash_device_key(key: str) -> str:
    """SHA-256 of a device key — what gets stored in the DB, never the raw key."""
    return hashlib.sha256(key.encode()).hexdigest()

_db_lock = threading.Lock()
_db_conn = None

_JOIN_CODE_TTL = 600  # seconds — join codes are short-lived, single-use


def _get_db():
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(str(WATCHDOG_DB), check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS watchdog_devices (
                device_id     TEXT PRIMARY KEY,
                device_key    TEXT NOT NULL,
                key_hashed    INTEGER NOT NULL DEFAULT 0,
                label         TEXT NOT NULL,
                registered_at REAL NOT NULL,
                last_seen     REAL,
                status        TEXT NOT NULL DEFAULT 'active'
            )
        """)
        # Migration: add key_hashed column to tables created before this change
        cols = {r[1] for r in _db_conn.execute(
            "PRAGMA table_info(watchdog_devices)"
        ).fetchall()}
        if "key_hashed" not in cols:
            _db_conn.execute(
                "ALTER TABLE watchdog_devices ADD COLUMN key_hashed INTEGER NOT NULL DEFAULT 0"
            )
        # Upgrade any existing plaintext keys: SHA-256 hash length is 64 hex chars;
        # token_urlsafe(32) is ~43 chars — detect and hash all plaintext entries.
        plain_rows = _db_conn.execute(
            "SELECT device_id, device_key FROM watchdog_devices WHERE key_hashed = 0"
        ).fetchall()
        for did, dkey in plain_rows:
            _db_conn.execute(
                "UPDATE watchdog_devices SET device_key = ?, key_hashed = 1 WHERE device_id = ?",
                (_hash_device_key(dkey), did),
            )
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS join_codes (
                code       TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0
            )
        """)
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS watchdog_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id  TEXT NOT NULL,
                script_id  TEXT NOT NULL,
                ts         REAL NOT NULL,
                severity   TEXT NOT NULL,
                message    TEXT NOT NULL,
                suggestion TEXT NOT NULL,
                delivered  INTEGER NOT NULL DEFAULT 0
            )
        """)
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS node_commands (
                id                  TEXT PRIMARY KEY,
                device_id           TEXT NOT NULL,
                command             TEXT NOT NULL,
                args_json           TEXT NOT NULL DEFAULT '{}',
                status              TEXT NOT NULL DEFAULT 'queued',
                result_json         TEXT,
                queued_at           REAL NOT NULL,
                completed_at        REAL,
                requester_user_id   INTEGER
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_node_cmds_device ON node_commands (device_id, status)"
        )
        _db_conn.commit()
    return _db_conn


# ── Join codes — minted by an already-trusted session, redeemed by a new device ─

def create_join_code(ttl_seconds: int = _JOIN_CODE_TTL) -> str:
    """Mint a short-lived, single-use code a new device can redeem to register."""
    code = secrets.token_urlsafe(16)
    now = time.time()
    with _db_lock:
        conn = _get_db()
        conn.execute(
            "INSERT INTO join_codes (code, created_at, expires_at, used) VALUES (?, ?, ?, 0)",
            (code, now, now + ttl_seconds),
        )
        conn.commit()
    return code


def register_device(join_code: str, label: str) -> tuple[str, str] | None:
    """
    Redeem a join code for a fresh (device_id, device_key) pair.

    Returns None if the code is missing, expired, or already used — the caller
    should respond 401 in that case. Consumes the code on success so it can't
    be replayed.
    """
    now = time.time()
    with _db_lock:
        conn = _get_db()
        row = conn.execute(
            "SELECT expires_at, used FROM join_codes WHERE code = ?", (join_code,)
        ).fetchone()
        if row is None or row[1] or row[0] < now:
            return None

        device_id = secrets.token_urlsafe(16)
        device_key = secrets.token_urlsafe(32)
        conn.execute("UPDATE join_codes SET used = 1 WHERE code = ?", (join_code,))
        conn.execute(
            "INSERT INTO watchdog_devices "
            "(device_id, device_key, key_hashed, label, registered_at, last_seen, status) "
            "VALUES (?, ?, 1, ?, ?, ?, 'active')",
            (device_id, _hash_device_key(device_key), label or "unnamed device", now, now),
        )
        conn.commit()
    return device_id, device_key


def authenticate_device(device_id: str, device_key: str) -> bool:
    """Check a device's credentials against the registry; bumps last_seen on success."""
    with _db_lock:
        conn = _get_db()
        row = conn.execute(
            "SELECT device_key, key_hashed, status FROM watchdog_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if row is None or row[2] != "active":
            return False
        stored_key, key_hashed, _ = row
        provided = device_key or ""
        if key_hashed:
            # Normal path: compare SHA-256 hashes
            if not secrets.compare_digest(stored_key, _hash_device_key(provided)):
                return False
        else:
            # Legacy plaintext (pre-migration): compare directly then upgrade in place
            if not secrets.compare_digest(stored_key, provided):
                return False
            conn.execute(
                "UPDATE watchdog_devices SET device_key = ?, key_hashed = 1 WHERE device_id = ?",
                (_hash_device_key(provided), device_id),
            )
        conn.execute(
            "UPDATE watchdog_devices SET last_seen = ? WHERE device_id = ?",
            (time.time(), device_id),
        )
        conn.commit()
    return True


# ── Event queue — populated by authenticated devices, drained by the chat loop ──

def report_event(device_id: str, script_id: str, severity: str, message: str, suggestion: str) -> int:
    """Queue an incoming watchdog event. Returns the new row id."""
    with _db_lock:
        conn = _get_db()
        cur = conn.execute(
            "INSERT INTO watchdog_events "
            "(device_id, script_id, ts, severity, message, suggestion, delivered) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (device_id, script_id, time.time(), severity, message, suggestion),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_events(limit: int = 5) -> list[dict]:
    """Fetch undelivered events, oldest first, joined with the device's label."""
    with _db_lock:
        conn = _get_db()
        rows = conn.execute(
            "SELECT e.id, e.device_id, d.label, e.script_id, e.ts, "
            "       e.severity, e.message, e.suggestion "
            "FROM watchdog_events e "
            "LEFT JOIN watchdog_devices d ON d.device_id = e.device_id "
            "WHERE e.delivered = 0 ORDER BY e.ts LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r[0], "device_id": r[1], "label": r[2] or r[1],
            "script_id": r[3], "ts": r[4],
            "severity": r[5], "message": r[6], "suggestion": r[7],
        }
        for r in rows
    ]


def mark_delivered(ids: list[int]) -> None:
    """Mark queued events as delivered — call only after a successful response."""
    if not ids:
        return
    with _db_lock:
        conn = _get_db()
        conn.executemany(
            "UPDATE watchdog_events SET delivered = 1 WHERE id = ?",
            [(i,) for i in ids],
        )
        conn.commit()


# ── Node command queue — Kai dispatches work, agents fetch and execute it ─────

def get_all_devices() -> list[dict]:
    """Return all registered devices with their current status."""
    with _db_lock:
        conn = _get_db()
        rows = conn.execute(
            "SELECT device_id, label, registered_at, last_seen, status "
            "FROM watchdog_devices ORDER BY label"
        ).fetchall()
    return [
        {"device_id": r[0], "label": r[1], "registered_at": r[2],
         "last_seen": r[3], "status": r[4]}
        for r in rows
    ]


def queue_command(device_id: str, command: str, args: dict | None = None, user_id: int | None = None) -> str:
    """Queue a command for a specific device. Returns the command_id."""
    cmd_id = secrets.token_urlsafe(12)
    now = time.time()
    with _db_lock:
        conn = _get_db()
        conn.execute(
            "INSERT INTO node_commands "
            "(id, device_id, command, args_json, status, queued_at, requester_user_id) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (cmd_id, device_id, command, json.dumps(args or {}), now, user_id),
        )
        conn.commit()
    return cmd_id


def queue_broadcast(command: str, args: dict | None = None, user_id: int | None = None) -> list[str]:
    """Queue a command on every active device. Returns list of command_ids."""
    devices = get_all_devices()
    active = [d["device_id"] for d in devices if d["status"] == "active"]
    return [queue_command(did, command, args, user_id) for did in active]


def get_pending_commands(device_id: str) -> list[dict]:
    """
    Fetch queued commands for a device and atomically mark them 'running'.
    Called by the remote agent on each poll — marking running prevents double-delivery.
    """
    with _db_lock:
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, command, args_json FROM node_commands "
            "WHERE device_id = ? AND status = 'queued' ORDER BY queued_at",
            (device_id,),
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE node_commands SET status = 'running' WHERE id = ?",
                [(r[0],) for r in rows],
            )
            conn.commit()
    return [{"id": r[0], "command": r[1], "args": json.loads(r[2])} for r in rows]


def complete_command(command_id: str, result: dict, error: bool = False) -> None:
    """Mark a command done (or error) and store its result JSON."""
    status = "error" if error else "done"
    with _db_lock:
        conn = _get_db()
        conn.execute(
            "UPDATE node_commands SET status = ?, result_json = ?, completed_at = ? WHERE id = ?",
            (status, json.dumps(result), time.time(), command_id),
        )
        conn.commit()


def get_command_results(command_ids: list[str]) -> dict[str, dict | None]:
    """
    Return a mapping of command_id → result dict for completed commands.
    Incomplete commands map to None. Callers poll this until all are non-None
    or a timeout is reached.
    """
    if not command_ids:
        return {}
    placeholders = ",".join("?" * len(command_ids))
    with _db_lock:
        conn = _get_db()
        rows = conn.execute(
            f"SELECT id, status, result_json FROM node_commands WHERE id IN ({placeholders})",
            command_ids,
        ).fetchall()
    out: dict[str, dict | None] = {cid: None for cid in command_ids}
    for row_id, status, result_json in rows:
        if status in ("done", "error"):
            result = json.loads(result_json) if result_json else {}
            result["_status"] = status
            out[row_id] = result
    return out
