"""
Opening a SQLite connection — one implementation, used by every database here.

There are several databases (the shared kai.db, the per-user tree/state/knowledge
files, the event log) and they all want the same four things: connect, WAL,
busy_timeout, optionally sqlite-vec, and a thread-local cache so the file isn't
reopened on every call. That was written out five times; the connection-leak fix
had to be applied five times too.

Two rules this module exists to hold:

  Resolve the path at CALL time. Callers pass a path they computed now, never a
  value frozen at import. Tests reassign cfg.DB_PATH / TREE_DIR / STATE_DIR after
  import, and a frozen path sends them straight at the production database.

  Cache, don't reopen. sqlite3's ``with conn:`` commits the transaction but never
  closes the connection, so the old ``with _connect(...)`` pattern leaked one
  connection per call. Connections are keyed by (thread, path) because sqlite3
  connections are not safe to share across threads. Evict with close_cached()
  before deleting a database file.
"""

import sqlite3
import threading
from pathlib import Path

_local = threading.local()

_SQLITE_VEC_AVAILABLE: bool | None = None


def sqlite_vec_available() -> bool:
    """Whether the sqlite-vec extension is importable. Checked once, cached."""
    global _SQLITE_VEC_AVAILABLE
    if _SQLITE_VEC_AVAILABLE is None:
        try:
            import sqlite_vec  # noqa: F401

            _SQLITE_VEC_AVAILABLE = True
        except ImportError:
            _SQLITE_VEC_AVAILABLE = False
    return _SQLITE_VEC_AVAILABLE


def open_db(
    path,
    *,
    vec: bool = False,
    row_factory=None,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open *path* (creating its directory) with WAL and a 5s busy timeout. No caching —
    see cached_conn.

    WAL so background daemon threads can commit while the main thread reads.
    busy_timeout so a locked file waits instead of failing. ``vec=True`` loads
    sqlite-vec when it is installed; a load failure raises rather than leaving a
    connection that silently can't do vector search.
    """
    # kai.config no longer mkdirs at import time, so every open has to guarantee its
    # own parent — honoring a test that repointed the path at a temp dir.
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if vec and sqlite_vec_available():
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


def cached_conn(
    path,
    *,
    vec: bool = False,
    row_factory=None,
    init=None,
) -> sqlite3.Connection:
    """Return this thread's connection for *path*, opening it on first use.

    ``init(conn)`` is the place for CREATE TABLE IF NOT EXISTS. It runs once per
    (connection, init function) — so two modules pointed at the same file each
    get their schema created, but neither re-runs it on every call. Pass a
    module-level function, not a lambda: a fresh lambda is a new key every time.
    The connection is cached before init runs, so an init that reaches for the
    connection itself doesn't recurse.

    ``vec`` and ``row_factory`` only apply when the connection is opened; a
    second caller for the same file inherits whatever the first asked for.
    """
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    key = str(Path(path))
    entry = cache.get(key)
    if entry is None:
        entry = cache[key] = (open_db(path, vec=vec, row_factory=row_factory), set())
    conn, inits_run = entry
    if init is not None and init not in inits_run:
        inits_run.add(init)
        init(conn)
    return conn


def close_cached(path) -> None:
    """Evict and close this thread's connection for *path*, if any.

    Call before unlinking a database file — Windows can't delete an open file,
    and a stale cached connection would otherwise be handed out again.
    """
    cache = getattr(_local, "conns", None)
    if not cache:
        return
    entry = cache.pop(str(Path(path)), None)
    if entry is not None:
        try:
            entry[0].close()
        except Exception:
            pass
