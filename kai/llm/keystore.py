"""
Per-user API-key store for cloud LLM providers.

Why this exists: connecting a cloud brain means Kai has to hold a provider API
key. Keys are REVERSIBLE secrets (unlike PINs, we must send the real value to
the provider), so they're encrypted at rest with a key derived from the machine
key (kai.system.device.get_key()). A stolen kai.db is useless without the
device.key file from the same machine — the same security model the machine
certificate already gives the login flow.

Guarantees:
  • plaintext keys live only in memory for the duration of a provider call;
  • they are never sent to the browser and never logged;
  • storage is user-scoped (provider_keys table in kai.db), so delete_user()
    wipes a user's keys and export_user_data() excludes them (auth material,
    not user content) — mirroring how session_tokens are handled.

The table is created lazily (like flow_log), so importing this module never
forces schema work.
"""
import base64
import hashlib
from datetime import datetime

from kai.store.db import get_conn
from kai.system import device

_schema_ready = False
_cipher_cache = None


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_keys (
            user_id    INTEGER NOT NULL,
            conn_id    TEXT    NOT NULL,
            provider   TEXT    NOT NULL,
            base_url   TEXT    NOT NULL DEFAULT '',
            secret     BLOB    NOT NULL,
            created_at TEXT    NOT NULL,
            PRIMARY KEY (user_id, conn_id)
        )
        """
    )
    conn.commit()
    _schema_ready = True


def _cipher():
    """Fernet built from a key derived from the machine key. Cached per process."""
    global _cipher_cache
    if _cipher_cache is None:
        from cryptography.fernet import Fernet
        material = hashlib.sha256(device.get_key() + b"kai-llm-keystore-v1").digest()
        _cipher_cache = Fernet(base64.urlsafe_b64encode(material))
    return _cipher_cache


def set_key(user_id: int, conn_id: str, secret: str,
            provider: str, base_url: str = "") -> None:
    """Store (or replace) an encrypted API key for one connection.

    conn_id is a stable per-user label for the connection (e.g. "openrouter").
    provider / base_url are non-secret metadata stored alongside so the UI can
    list connections without ever touching the secret.
    """
    if not secret:
        raise ValueError("secret must not be empty")
    conn = get_conn()
    _ensure_schema(conn)
    token = _cipher().encrypt(secret.encode("utf-8"))
    conn.execute(
        "INSERT INTO provider_keys (user_id, conn_id, provider, base_url, secret, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, conn_id) DO UPDATE SET "
        "provider = excluded.provider, base_url = excluded.base_url, secret = excluded.secret",
        (user_id, conn_id, provider, base_url, token, datetime.now().isoformat()),
    )
    conn.commit()


def get_secret(user_id: int, conn_id: str) -> str | None:
    """Decrypt and return the plaintext key, or None if missing/undecryptable.

    Undecryptable (wrong machine key / corrupt) is treated as missing rather
    than raising — a moved kai.db should fail closed, not crash a turn.
    """
    conn = get_conn()
    _ensure_schema(conn)
    row = conn.execute(
        "SELECT secret FROM provider_keys WHERE user_id = ? AND conn_id = ?",
        (user_id, conn_id),
    ).fetchone()
    if not row:
        return None
    try:
        return _cipher().decrypt(row[0]).decode("utf-8")
    except Exception:
        return None


def has_key(user_id: int, conn_id: str) -> bool:
    return get_secret(user_id, conn_id) is not None


def list_connections(user_id: int) -> list[dict]:
    """All of a user's connections — metadata only, never the secret."""
    conn = get_conn()
    _ensure_schema(conn)
    rows = conn.execute(
        "SELECT conn_id, provider, base_url, created_at FROM provider_keys "
        "WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ).fetchall()
    return [
        {"conn_id": r[0], "provider": r[1], "base_url": r[2], "created_at": r[3]}
        for r in rows
    ]


def delete_key(user_id: int, conn_id: str) -> bool:
    """Remove one connection's key. Returns True if a row was deleted."""
    conn = get_conn()
    _ensure_schema(conn)
    cur = conn.execute(
        "DELETE FROM provider_keys WHERE user_id = ? AND conn_id = ?",
        (user_id, conn_id),
    )
    conn.commit()
    return cur.rowcount > 0
