"""
User management — name + PIN + machine certificate auth.
Stored in kai.db alongside sessions.

Auth layers:
  1. Name       — identifies the account (case-insensitive).
  2. PIN        — 4-8 digits, hashed with PBKDF2-HMAC-SHA256 (600 000 rounds,
                   random 16-byte salt). Stored as ``salt_hex$hash_hex``.
                   Legacy SHA-256-only hashes (no ``$``) are auto-upgraded on
                   next successful login.
  3. Machine key — 30-byte random value generated once per Kai installation
                   (see kai/device.py). Its SHA-256 hash is stored per user at
                   registration time. Login is rejected if the machine key on
                   the current PC doesn't match the one used at registration.
                   This means a copied database file is useless on another PC.

Kai's brain only ever receives the user's name. PINs and machine keys never
reach the AI layer.
"""
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime

from kai.db import get_conn

_PBKDF2_ROUNDS = 600_000  # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256


def _hash(value: str) -> str:
    """Hash *value* with PBKDF2-HMAC-SHA256 and a random 16-byte salt.

    Returns ``salt_hex$hash_hex``.
    """
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", value.strip().encode(), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}${dk.hex()}"


def _verify(value: str, stored: str) -> bool:
    """Verify *value* against a stored hash.

    Supports both new ``salt$hash`` format and legacy bare SHA-256 hashes.
    Uses constant-time comparison to prevent timing attacks.
    """
    if "$" in stored:
        # New PBKDF2 format: salt_hex$hash_hex
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", value.strip().encode(), salt, _PBKDF2_ROUNDS)
        return hmac.compare_digest(dk.hex(), hash_hex)
    else:
        # Legacy: bare SHA-256 (no salt) — accept but will be upgraded on success
        legacy = hashlib.sha256(value.strip().encode()).hexdigest()
        return hmac.compare_digest(legacy, stored)


_table_ensured = False

def _ensure_table() -> None:
    """Create users table if needed. Called lazily on first use."""
    global _table_ensured
    if _table_ensured:
        return
    conn = get_conn()
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
    conn.commit()
    _table_ensured = True


# ── Public API ────────────────────────────────────────────────────────────────

def list_users() -> list[str]:
    _ensure_table()
    conn = get_conn()
    rows = conn.execute("SELECT name FROM users ORDER BY name").fetchall()
    return [r[0] for r in rows]


def create_user(name: str, pin: str, machine_key_hash: str) -> dict | None:
    """
    Register a new user on this machine.
    machine_key_hash comes from kai.device.key_hash() — never from the client.
    Returns {"name": name} or None if the name is already taken.
    """
    _ensure_table()
    name = name.strip()
    if not name or not pin.strip():
        return None
    conn = get_conn()
    try:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO users (name, pin_hash, machine_hash, created_at) VALUES (?, ?, ?, ?)",
            (name, _hash(pin), machine_key_hash, now),
        )
        user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return {"name": name, "id": user_id}
    except sqlite3.IntegrityError:
        return None  # name already taken


def authenticate(name: str, pin: str, machine_key_hash: str) -> dict | None:
    """
    Verify name + PIN + machine.
    All three must match. Returns user dict on success, None on any failure.
    Deliberately gives the same error for wrong-PIN vs wrong-machine to avoid
    leaking which factor failed.
    """
    _ensure_table()
    name = name.strip()
    if not name or not pin.strip():
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT id, name, pin_hash, machine_hash FROM users WHERE name = ? COLLATE NOCASE",
        (name,),
    ).fetchone()
    if not row:
        return None
    user_id, stored_name, pin_hash, machine_hash = row
    # Both factors must pass — check both before returning to avoid timing leaks
    pin_ok     = _verify(pin, pin_hash)
    machine_ok = hmac.compare_digest(machine_hash or "", machine_key_hash or "")
    if not (pin_ok and machine_ok):
        return None
    now = datetime.now().isoformat()
    # Auto-upgrade legacy SHA-256 hashes to salted PBKDF2 on successful login
    if "$" not in pin_hash:
        conn.execute(
            "UPDATE users SET pin_hash = ?, last_seen = ? WHERE name = ?",
            (_hash(pin), now, stored_name),
        )
    else:
        conn.execute(
            "UPDATE users SET last_seen = ? WHERE name = ?", (now, stored_name)
        )
    conn.commit()
    return {"name": stored_name, "id": user_id, "last_seen": now}


def get_user_id(name: str) -> int | None:
    """Look up a user's integer ID by name. Returns None if not found."""
    _ensure_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM users WHERE name = ? COLLATE NOCASE", (name.strip(),)
    ).fetchone()
    return row[0] if row else None
