"""
User management — name + PIN + machine certificate auth.
Stored in kai.db alongside sessions.

Auth layers:
  1. Name       — identifies the account (case-insensitive).
  2. PIN        — 4-8 digits, stored only as SHA-256 hash. Never in plain text.
  3. Machine key — 30-byte random value generated once per Kai installation
                   (see kai/device.py). Its SHA-256 hash is stored per user at
                   registration time. Login is rejected if the machine key on
                   the current PC doesn't match the one used at registration.
                   This means a copied database file is useless on another PC.

Kai's brain only ever receives the user's name. PINs and machine keys never
reach the AI layer.
"""
import hashlib
import sqlite3
from datetime import datetime

from kai.config import DB_PATH


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _ensure_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                pin_hash        TEXT    NOT NULL,
                machine_hash    TEXT    NOT NULL,
                created_at      TEXT    NOT NULL,
                last_seen       TEXT
            )
        """)
        # Migration: add machine_hash column to any existing table that lacks it
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "machine_hash" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN machine_hash TEXT")
            # Drop user_key column artifacts from earlier iterations if present
        if "user_key" in cols:
            # SQLite can't DROP COLUMN before 3.35 — just leave it, it does no harm
            pass


_ensure_table()


# ── Public API ────────────────────────────────────────────────────────────────

def list_users() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT name FROM users ORDER BY name").fetchall()
    return [r[0] for r in rows]


def create_user(name: str, pin: str, machine_key_hash: str) -> dict | None:
    """
    Register a new user on this machine.
    machine_key_hash comes from kai.device.key_hash() — never from the client.
    Returns {"name": name} or None if the name is already taken.
    """
    name = name.strip()
    if not name or not pin.strip():
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO users (name, pin_hash, machine_hash, created_at) VALUES (?, ?, ?, ?)",
                (name, _hash(pin), machine_key_hash, now),
            )
        return {"name": name}
    except sqlite3.IntegrityError:
        return None  # name already taken


def authenticate(name: str, pin: str, machine_key_hash: str) -> dict | None:
    """
    Verify name + PIN + machine.
    All three must match. Returns user dict on success, None on any failure.
    Deliberately gives the same error for wrong-PIN vs wrong-machine to avoid
    leaking which factor failed.
    """
    name = name.strip()
    if not name or not pin.strip():
        return None
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT name, pin_hash, machine_hash FROM users WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if not row:
            return None
        stored_name, pin_hash, machine_hash = row
        # Both factors must pass — check both before returning to avoid timing leaks
        pin_ok     = (pin_hash     == _hash(pin))
        machine_ok = (machine_hash == machine_key_hash)
        if not (pin_ok and machine_ok):
            return None
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE users SET last_seen = ? WHERE name = ?", (now, stored_name)
        )
    return {"name": stored_name, "last_seen": now}
