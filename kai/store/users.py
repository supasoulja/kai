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

from kai.store.db import get_conn

_PBKDF2_ROUNDS = 600_000  # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256

# User-scoped tables wiped by a plain `WHERE user_id = ?` delete. Shared by
# delete_user() (account teardown) and export_user_data() (the data-export
# endpoint) so the two can never drift apart. Tables with FK children or vector
# mirrors (rag_*, episodic_*) keep their own special handling in
# delete_user; flow_log is handled separately because it's created lazily.
_USER_TABLES = (
    "episodic_transcripts",
    "episodic_entries",
    "semantic_facts",
    "procedural_rules",
    "session_messages",
    "sessions",
    "notes",
    "trace_log",
    "relationship_log",
    "session_tokens",
    "usage_patterns",
    "goals",
    "cerebellum_log",
    "pending_briefings",
    "study_library",
    "study_chunks",
)

# Excluded from the export — auth material, not user content.
_EXPORT_SKIP = frozenset({"session_tokens"})


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


def user_count() -> int:
    """Return the total number of registered users. Used to gate first-run registration."""
    _ensure_table()
    return get_conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_owner_id() -> int | None:
    """Return the user_id of the first registered user (lowest id). They are the owner."""
    _ensure_table()
    row = get_conn().execute("SELECT MIN(id) FROM users").fetchone()
    return row[0] if row and row[0] is not None else None


def create_user(name: str, pin: str, machine_key_hash: str) -> dict | None:
    """
    Register a new user on this machine.
    machine_key_hash comes from kai.system.device.key_hash() — never from the client.
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
    pin_ok = _verify(pin, pin_hash)
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
        conn.execute("UPDATE users SET last_seen = ? WHERE name = ?", (now, stored_name))
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


def delete_user(user_id: int) -> bool:
    """
    Permanently delete a user and ALL their data across every table.
    Returns True if the user existed and was deleted.

    This is the nuclear option — everything is gone. The user's conversations,
    memories, notes, documents, session tokens, and account are wiped.
    """
    if user_id <= 0:
        return False  # never delete the system user
    conn = get_conn()
    row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return False

    # All deletes run as one transaction: if any mandatory DELETE raises midway,
    # roll back so we don't leave a half-deleted user (and a dangling open
    # transaction on this thread-local connection for the next caller to inherit).
    try:
        _delete_user_rows(conn, user_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Per-user data lives outside kai.db too: three SQLite files (tree/state/
    # knowledge) and the downloaded study library on disk. Wipe them as well —
    # leaving them behind is the same privacy leak as the missed tables above.
    # Runs only after the DB delete has durably committed.
    _delete_user_files(user_id)
    return True


def _delete_user_rows(conn: sqlite3.Connection, user_id: int) -> None:
    """Delete every kai.db row owned by *user_id* (no commit — caller owns the txn)."""
    # Order matters: delete referencing rows before parent rows.
    # Episodic: delete vectors first (they reference rowids in episodic_entries)
    try:
        entry_rowids = [
            r[0]
            for r in conn.execute(
                "SELECT rowid FROM episodic_entries WHERE user_id = ?", (user_id,)
            ).fetchall()
        ]
        if entry_rowids:
            ph = ",".join("?" * len(entry_rowids))
            conn.execute(f"DELETE FROM episodic_vec WHERE rowid IN ({ph})", entry_rowids)
    except Exception:
        pass  # episodic_vec may not exist if sqlite-vec isn't available

    # RAG: delete vectors for user's document chunks
    try:
        chunk_rowids = [
            r[0]
            for r in conn.execute(
                "SELECT c.rowid FROM rag_chunks c "
                "JOIN rag_documents d ON c.doc_id = d.doc_id "
                "WHERE d.user_id = ?",
                (user_id,),
            ).fetchall()
        ]
        if chunk_rowids:
            ph = ",".join("?" * len(chunk_rowids))
            conn.execute(f"DELETE FROM rag_chunks_vec WHERE rowid IN ({ph})", chunk_rowids)
    except Exception:
        pass

    # Delete from every user-scoped table (single source of truth: _USER_TABLES,
    # also used by export_user_data so deletion and export stay in lockstep).
    for table in _USER_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    # flow_log is created lazily by kai.core.flow on first trace, so it may not
    # exist yet — guard the delete the same way the vec-table deletes are guarded.
    try:
        conn.execute("DELETE FROM flow_log WHERE user_id = ?", (user_id,))
    except sqlite3.OperationalError:
        pass  # no flow_log table — nothing to delete
    # provider_keys (cloud LLM API keys) is also created lazily by
    # kai.llm.keystore on first use — guard it the same way. Kept OUT of
    # _USER_TABLES on purpose so export_user_data never includes it (auth
    # material, like session_tokens).
    try:
        conn.execute("DELETE FROM provider_keys WHERE user_id = ?", (user_id,))
    except sqlite3.OperationalError:
        pass  # no provider_keys table — nothing to delete

    # RAG documents and chunks
    doc_ids = [
        r[0]
        for r in conn.execute(
            "SELECT doc_id FROM rag_documents WHERE user_id = ?", (user_id,)
        ).fetchall()
    ]
    if doc_ids:
        ph = ",".join("?" * len(doc_ids))
        conn.execute(f"DELETE FROM rag_chunks WHERE doc_id IN ({ph})", doc_ids)
    conn.execute("DELETE FROM rag_documents WHERE user_id = ?", (user_id,))

    # Finally, delete the user account itself
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def _delete_user_files(user_id: int) -> None:
    """Remove a user's per-user SQLite files and on-disk study library.

    Best-effort: each step swallows its own errors so a missing file or a
    Windows file-lock never aborts the rest of the deletion. Callers that hold
    open connections (e.g. a live Brain) should evict them first on Windows.
    """
    import shutil
    from pathlib import Path

    import kai.config as cfg
    from kai.memory import knowledge as _knowledge
    from kai.memory import state as _state
    from kai.memory import tree as _tree

    for mod in (_tree, _state, _knowledge):
        try:
            mod.delete_user_db(user_id)
        except Exception:
            pass

    try:
        lib_dir = Path(cfg.STUDY_LIBRARY_PATH) / str(user_id)
        shutil.rmtree(lib_dir, ignore_errors=True)
    except Exception:
        pass


# ── Data export ─────────────────────────────────────────────────────────────────


def _rows_as_dicts(conn, sql: str, params=()) -> list[dict]:
    """Run a SELECT and return rows as JSON-safe dicts (works regardless of the
    connection's row_factory). BLOB columns are base64-encoded so the result
    survives json.dumps."""
    import base64

    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        rec = {}
        for col, val in zip(cols, row, strict=True):
            if isinstance(val, (bytes, bytearray, memoryview)):
                val = {"__b64__": base64.b64encode(bytes(val)).decode("ascii")}
            rec[col] = val
        out.append(rec)
    return out


def export_user_data(user_id: int) -> dict:
    """Return a JSON-serializable dump of everything stored for this user in
    kai.db — the account row (secrets excluded) plus every user-scoped table.

    Read-only. Pairs with delete_user(): both walk _USER_TABLES, so an export
    proves exactly what a deletion will wipe. The three per-user .db files
    (tree/state/knowledge) and the study library live outside kai.db and are
    added to the export zip by the web layer, not here.
    """
    if user_id <= 0:
        return {}
    _ensure_table()
    conn = get_conn()
    out: dict = {"user_id": user_id, "account": [], "tables": {}}

    try:
        out["account"] = _rows_as_dicts(
            conn,
            "SELECT id, name, created_at, last_seen FROM users WHERE id = ?",
            (user_id,),
        )
    except Exception:
        pass

    tables = out["tables"]
    # Plain user-scoped tables (shared list with delete_user), plus the ones with
    # their own ownership column / lazy creation handled explicitly below.
    extra = ("rag_documents", "flow_log")
    for table in (*[t for t in _USER_TABLES if t not in _EXPORT_SKIP], *extra):
        try:
            tables[table] = _rows_as_dicts(
                conn, f"SELECT * FROM {table} WHERE user_id = ?", (user_id,)
            )
        except Exception:
            continue  # table may not exist in this DB — skip it
    return out
