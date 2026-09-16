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
import json
import os
import sqlite3
from datetime import datetime

from kai.store.db import get_conn

_PBKDF2_ROUNDS = 600_000  # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256

# Account levels, highest first. docs/active/PERMISSIONS.md is the contract and
# kai/core/permissions.py says what each level may do; this module only stores them.
LEVELS = ("owner", "admin", "member", "guest")

# User-scoped tables wiped by a plain `WHERE user_id = ?` delete. Shared by
# delete_user() (account teardown) and export_user_data() (the data-export
# endpoint) so the two can never drift apart. Tables with FK children or vector
# mirrors (rag_*, episodic_*) keep their own special handling in
# delete_user; flow_log and crew_log are handled separately because they're
# created lazily, so a plain loop over this tuple would raise on a fresh DB.
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
            last_seen       TEXT,
            level           TEXT    NOT NULL DEFAULT 'member',
            grants          TEXT    NOT NULL DEFAULT '[]',
            created_by      INTEGER
        )
    """)
    # Migration: add machine_hash column to any existing table that lacks it
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "machine_hash" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN machine_hash TEXT")
    # Migration: levels. The account that has always been the owner — the lowest id —
    # stays the owner, so adding levels changes nobody's powers.
    if "level" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN level TEXT NOT NULL DEFAULT 'member'")
        conn.execute("UPDATE users SET level = 'owner' WHERE id = (SELECT MIN(id) FROM users)")
    if "grants" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN grants TEXT NOT NULL DEFAULT '[]'")
    if "created_by" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN created_by INTEGER")  # NULL: nobody on record
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


def valid_pin(pin: str) -> bool:
    """4–8 digits: what every PIN form asks for."""
    pin = (pin or "").strip()
    return pin.isdigit() and 4 <= len(pin) <= 8


def get_owner_id() -> int | None:
    """The owner's user_id: the account holding the owner level. If none does — the owner
    deleted their own account — the lowest id stands in, as it always has, so a Kai is never
    left with nobody able to run it."""
    _ensure_table()
    conn = get_conn()
    row = conn.execute("SELECT id FROM users WHERE level = 'owner' ORDER BY id LIMIT 1").fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT MIN(id) FROM users").fetchone()
    return row[0] if row and row[0] is not None else None


def create_user(
    name: str,
    pin: str,
    machine_key_hash: str,
    level: str = "member",
    created_by: int | None = None,
) -> dict | None:
    """
    Register a new user on this machine.
    machine_key_hash comes from kai.system.device.key_hash() — never from the client.

    The first account on a machine is its owner, whatever *level* says. Nobody after it is
    ever created as one: ownership moves only by transfer_ownership(). Who may create which
    level is kai/core/permissions.py's question, asked by the routes before they call this.

    Returns {"name", "id", "level"}, or None if the name is taken or the level isn't one.
    """
    _ensure_table()
    name = name.strip()
    if not name or not pin.strip():
        return None
    conn = get_conn()
    first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    if first:
        level, created_by = "owner", None
    elif level not in LEVELS or level == "owner":
        return None
    try:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO users (name, pin_hash, machine_hash, created_at, level, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, _hash(pin), machine_key_hash, now, level, created_by),
        )
        user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return {"name": name, "id": user_id, "level": level}
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


def get_level(user_id: int) -> str | None:
    """The account's level, or None for an id with no account (the terminal's scratch
    identity, user 0). Read live, so a demotion applies to the very next request."""
    if not user_id:
        return None
    _ensure_table()
    row = get_conn().execute("SELECT level FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return None
    if user_id == get_owner_id():
        return "owner"
    return row[0] if row[0] in LEVELS and row[0] != "owner" else "member"


_ACCOUNT_COLS = "id, name, level, grants, created_by, created_at, last_seen"


def _account(row) -> dict:
    try:
        grants = [g for g in json.loads(row[3] or "[]") if isinstance(g, str)]
    except (TypeError, ValueError):
        grants = []
    return {
        "id": row[0],
        "name": row[1],
        "level": row[2],
        "grants": grants,
        "created_by": row[4],
        "created_at": row[5],
        "last_seen": row[6],
    }


def get_account(user_id: int) -> dict | None:
    """One account, secrets excluded, with its effective level."""
    _ensure_table()
    row = (
        get_conn().execute(f"SELECT {_ACCOUNT_COLS} FROM users WHERE id = ?", (user_id,)).fetchone()
    )
    if not row:
        return None
    account = _account(row)
    account["level"] = get_level(row[0])
    return account


def list_accounts() -> list[dict]:
    """Every account, oldest first, secrets excluded, with effective levels."""
    _ensure_table()
    owner = get_owner_id()
    accounts = []
    for row in get_conn().execute(f"SELECT {_ACCOUNT_COLS} FROM users ORDER BY id").fetchall():
        account = _account(row)
        if row[0] == owner:
            account["level"] = "owner"
        elif account["level"] not in LEVELS or account["level"] == "owner":
            account["level"] = "member"
        accounts.append(account)
    return accounts


def update_account(
    user_id: int, *, name: str | None = None, pin: str | None = None, level: str | None = None
) -> bool | None:
    """Change an account's name, PIN or level. Returns True on success, None if the new name
    is taken, False if there is no such account or the change touches ownership — which only
    transfer_ownership() moves."""
    _ensure_table()
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        return False
    if level is not None and (level not in LEVELS or level == "owner" or user_id == get_owner_id()):
        return False
    sets, args = [], []
    if name is not None:
        sets.append("name = ?")
        args.append(name.strip())
    if pin is not None:
        sets.append("pin_hash = ?")
        args.append(_hash(pin))
    if level is not None:
        sets.append("level = ?")
        args.append(level)
    if not sets:
        return True
    try:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*args, user_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return None  # name already taken
    return True


def set_grants(user_id: int, grants: list[str]) -> bool:
    """Replace an account's grants. Which grants fit which level is permissions.GRANTS."""
    _ensure_table()
    conn = get_conn()
    cur = conn.execute(
        "UPDATE users SET grants = ? WHERE id = ?", (json.dumps(sorted(set(grants))), user_id)
    )
    conn.commit()
    return cur.rowcount > 0


def transfer_ownership(to_id: int) -> bool:
    """Make *to_id* the owner. The old owner becomes an admin — handing over the machine
    shouldn't lock its former owner out of running it. False if there is no such account."""
    _ensure_table()
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (to_id,)).fetchone():
        return False
    old = get_owner_id()
    try:
        if old is not None and old != to_id:
            conn.execute("UPDATE users SET level = 'admin', grants = '[]' WHERE id = ?", (old,))
        conn.execute("UPDATE users SET level = 'admin' WHERE level = 'owner' AND id != ?", (to_id,))
        conn.execute("UPDATE users SET level = 'owner', grants = '[]' WHERE id = ?", (to_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def revoke_sessions(user_id: int) -> int:
    """Sign an account out everywhere. Sessions are looked up in the database on every
    request, so this applies to their next click. Returns how many sessions ended."""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM session_tokens WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount
    except sqlite3.OperationalError:
        return 0  # no session_tokens table yet: nobody has ever signed in


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
    # crew_log is created lazily by kai.core.crew_trace on the first crew turn.
    # It holds per-user crew decisions and specialist findings, so it is user
    # content and must go with the account — it only escaped _USER_TABLES
    # because of the lazy creation.
    try:
        conn.execute("DELETE FROM crew_log WHERE user_id = ?", (user_id,))
    except sqlite3.OperationalError:
        pass  # no crew_log table — nothing to delete
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
    extra = ("rag_documents", "flow_log", "crew_log")
    for table in (*[t for t in _USER_TABLES if t not in _EXPORT_SKIP], *extra):
        try:
            tables[table] = _rows_as_dicts(
                conn, f"SELECT * FROM {table} WHERE user_id = ?", (user_id,)
            )
        except Exception:
            continue  # table may not exist in this DB — skip it
    return out
