"""
Centralized SQLite connection management.

Provides thread-local connection reuse, WAL mode, and one-time table
initialization. Every module that touches the database should use get_conn()
instead of sqlite3.connect(DB_PATH) directly.

Why thread-local?
  SQLite connections are not safe to share across threads, but creating a new
  connection (+ loading sqlite-vec) on every operation is expensive. Thread-local
  storage gives each thread a long-lived connection that is reused across calls.

Why WAL?
  Default journal mode blocks readers during writes. WAL (Write-Ahead Logging)
  allows concurrent reads and writes — critical since background daemon threads
  commit memory while the main thread reads context.

Storage conventions across the codebase (there are three — know which to use):

  1. Central shared DB (this module — get_conn()).
     The default. One file (kai.db), thread-local connections, WAL +
     busy_timeout, sqlite-vec loaded, schema created once. Use this for any new
     table that is global or scoped by a user_id column. Most tables live here.

  2. Per-user file databases (kai/memory/{tree,state,knowledge}).
     The brain's memory tree is partitioned into one SQLite file per user
     (kai/memory/tree/<user_id>.db, etc.) rather than a user_id column, so a
     user's whole tree can be dropped/exported as a file. These open their own
     connection per call via a local _connect()/_conn() (also WAL); they do NOT
     go through get_conn(). Only the memory tree/state/knowledge use this.

  3. Long-lived module-global connections (kai/events.py, kai/watchdog_queue.py).
     The event bus (events.db) and the watchdog queue (watchdog.db) are separate
     databases with their own process-wide connection (check_same_thread=False).
     They are independent subsystems, not part of the main schema.

  New code should default to (1). Reach for (2) only for per-user file
  partitioning, and (3) only for a genuinely separate subsystem database.
"""
import sqlite3
import threading

from kai.config import DB_PATH

_local = threading.local()
_schema_initialized = False
_schema_lock = threading.Lock()

# sqlite-vec availability (checked once, cached)
_SQLITE_VEC_AVAILABLE: bool | None = None


def _check_sqlite_vec() -> bool:
    global _SQLITE_VEC_AVAILABLE
    if _SQLITE_VEC_AVAILABLE is not None:
        return _SQLITE_VEC_AVAILABLE
    try:
        import sqlite_vec  # noqa: F401
        _SQLITE_VEC_AVAILABLE = True
    except ImportError:
        _SQLITE_VEC_AVAILABLE = False
    return _SQLITE_VEC_AVAILABLE


def sqlite_vec_available() -> bool:
    """Public check — whether sqlite-vec is importable."""
    return _check_sqlite_vec()


def _reset_for_tests() -> None:
    """Reset module state so tests using different temp DBs get fresh schemas."""
    global _schema_initialized
    _schema_initialized = False
    _local.__dict__.pop("conn", None)


def get_conn() -> sqlite3.Connection:
    """
    Return a thread-local SQLite connection with WAL mode and sqlite-vec loaded.

    The connection is created once per thread and reused for all subsequent calls.
    Tables are initialized on the first call from any thread.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s if DB is locked

    if _check_sqlite_vec():
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    _local.conn = conn
    _ensure_schema(conn)
    return conn


# ── Search helpers ───────────────────────────────────────────────────────────
# Shared by every full-text / vector search path so the tricky bits (LIKE-escape
# correctness, the vec0 two-step KNN dance) live in exactly one tested place.

def like_escape(s: str) -> str:
    r"""Escape %, _ and \ so user input is matched literally under a
    ``LIKE ? ESCAPE '\'`` clause. Without this, a user typing ``%`` or ``_``
    would inject SQL wildcards into their own search."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def vec_knn(conn: sqlite3.Connection, vec_table: str, embedding, k: int
            ) -> list[tuple[int, float]]:
    """Pure sqlite-vec vec0 KNN: return up to ``k`` ``(rowid, distance)`` pairs,
    nearest-first.

    vec0 ``MATCH`` cannot be JOINed, so callers do a two-step: get ordered
    rowids here, fetch the real rows by ``rowid IN (...)`` (which loses order),
    then restore order with :func:`resort_by_rowid_order`. ``vec_table`` is
    always a hard-coded table name, never user input.
    """
    import sqlite_vec
    return conn.execute(
        f"SELECT rowid, distance FROM {vec_table} "
        f"WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (sqlite_vec.serialize_float32(embedding), int(k)),
    ).fetchall()


def resort_by_rowid_order(conn: sqlite3.Connection, table: str, entries: list,
                          rowids: list[int], id_attr: str = "id") -> list:
    """Re-sort ``entries`` (fetched via ``rowid IN (...)``, arbitrary order) back
    into the KNN distance order given by ``rowids``. Sorts in place and returns
    the same list. Entries must expose their primary key via ``id_attr``."""
    if not rowids:
        return entries
    placeholders = ",".join("?" * len(rowids))
    rowid_by_id = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT {id_attr}, rowid FROM {table} WHERE rowid IN ({placeholders})",
            rowids,
        ).fetchall()
    }
    rank = {rid: i for i, rid in enumerate(rowids)}
    entries.sort(
        key=lambda e: rank.get(rowid_by_id.get(getattr(e, id_attr), -1), 999)
    )
    return entries


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all tables once. No-op after the first call."""
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if _schema_initialized:
            return
        _maybe_migrate_fresh(conn)
        _maybe_migrate_embed_dim(conn)
        _create_all_tables(conn)
        _schema_initialized = True


def _maybe_migrate_fresh(conn: sqlite3.Connection) -> None:
    """
    Detect old schema (no user_id columns) and do a fresh-start migration.
    Drops all data tables EXCEPT users. Called before _create_all_tables
    so the new schema is created cleanly.
    """
    # Check if semantic_facts exists and has user_id
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_facts)").fetchall()}
    except Exception:
        return  # table doesn't exist yet — first run, nothing to migrate

    if not cols or "user_id" in cols:
        return  # already migrated or brand new DB

    # Old schema detected — drop data tables (preserve users table)
    tables_to_drop = [
        "semantic_facts", "procedural_rules",
        "episodic_entries", "episodic_transcripts",
        "sessions", "session_messages",
        "notes", "rag_documents", "rag_chunks",
        "tool_aliases", "trace_log", "relationship_log",
    ]
    # Drop vector tables first (virtual tables)
    for vt in ["episodic_vec", "rag_chunks_vec"]:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {vt}")
        except Exception:
            pass
    for table in tables_to_drop:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def _maybe_migrate_embed_dim(conn: sqlite3.Connection) -> None:
    """
    Detect old 2560-dim / 768-dim vector tables and drop them so they can
    be recreated at the new FAST_EMBED_DIM (384).

    Text data (episodic_entries, rag_chunks) is preserved —
    only the vec0 virtual tables (which store vectors + rowids) are dropped.
    New 384-dim vectors are created on-demand as content is accessed.
    """
    if not _check_sqlite_vec():
        return

    # Quick check: if episodic_vec exists and is already the right dimension,
    # skip. We detect old dim by trying to insert a FAST_EMBED_DIM zero vector.
    from kai.config import FAST_EMBED_DIM
    try:
        # If the table doesn't exist, nothing to migrate
        conn.execute("SELECT rowid FROM episodic_vec LIMIT 0")
    except Exception:
        return  # table doesn't exist yet — first run

    import sqlite_vec
    test_vec = [0.0] * FAST_EMBED_DIM
    try:
        # Try inserting a 384-dim vector inside a savepoint so nothing is
        # permanently written — even if the process crashes mid-probe.
        conn.execute("SAVEPOINT dim_check")
        conn.execute(
            "INSERT INTO episodic_vec (rowid, embedding) VALUES (-999, ?)",
            (sqlite_vec.serialize_float32(test_vec),),
        )
        # Success — dimension matches.  Roll back the test row.
        conn.execute("ROLLBACK TO dim_check")
        conn.execute("RELEASE dim_check")
        return  # already correct dimension
    except Exception:
        try:
            conn.execute("ROLLBACK TO dim_check")
            conn.execute("RELEASE dim_check")
        except Exception:
            conn.rollback()

    # Old dimension detected — drop all vec tables so they'll be recreated
    print("[~] Migrating vector tables to 384-dim (CPU fast embed)...")
    for vt in ["episodic_vec", "rag_chunks_vec"]:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {vt}")
        except Exception:
            pass
    conn.commit()
    print("[+] Old vector tables dropped — new ones will be created at 384-dim")


def _create_all_tables(conn: sqlite3.Connection) -> None:
    """Idempotent schema creation for every table in the project."""
    conn.executescript("""
        -- Semantic memory (per-user)
        CREATE TABLE IF NOT EXISTS semantic_facts (
            user_id     INTEGER NOT NULL DEFAULT 0,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'conversation',
            confidence  REAL NOT NULL DEFAULT 1.0,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );

        -- Procedural memory (per-user)
        CREATE TABLE IF NOT EXISTS procedural_rules (
            user_id     INTEGER NOT NULL DEFAULT 0,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );

        -- Episodic memory (per-user)
        CREATE TABLE IF NOT EXISTS episodic_entries (
            id          TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL DEFAULT 0,
            content     TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            entry_type  TEXT NOT NULL DEFAULT 'turn',
            metadata    TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS episodic_transcripts (
            archive_id  TEXT NOT NULL,
            user_id     INTEGER NOT NULL DEFAULT 0,
            content     TEXT NOT NULL,
            timestamp   TEXT NOT NULL
        );

        -- Sessions (per-user)
        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            user_id       INTEGER NOT NULL DEFAULT 0,
            title         TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            last_active   TEXT NOT NULL,
            message_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS session_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT    NOT NULL REFERENCES sessions(id),
            user_id     INTEGER NOT NULL DEFAULT 0,
            role        TEXT    NOT NULL,
            content     TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL,
            turn_order  INTEGER NOT NULL,
            feedback    INTEGER DEFAULT NULL
        );

        -- Tool aliases (global — shared across users)
        CREATE TABLE IF NOT EXISTS tool_aliases (
            alias       TEXT PRIMARY KEY,
            target      TEXT NOT NULL,
            similarity  REAL NOT NULL,
            seen_count  INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL
        );

        -- Trace log (per-user)
        CREATE TABLE IF NOT EXISTS trace_log (
            trace_id     TEXT PRIMARY KEY,
            user_id      INTEGER NOT NULL DEFAULT 0,
            timestamp    TEXT NOT NULL,
            user_input   TEXT,
            model        TEXT,
            context_len  INTEGER,
            tool_calls   TEXT,
            elapsed_ms   INTEGER,
            response_len INTEGER
        );

        -- Relationship log (per-user)
        CREATE TABLE IF NOT EXISTS relationship_log (
            id          TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL DEFAULT 0,
            timestamp   TEXT NOT NULL,
            entry_type  TEXT NOT NULL,
            content     TEXT NOT NULL
        );

        -- Auth session tokens (survive server restarts)
        CREATE TABLE IF NOT EXISTS session_tokens (
            token       TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            user_name   TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        );

        -- Notes (per-user)
        CREATE TABLE IF NOT EXISTS notes (
            id          TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL DEFAULT 0,
            timestamp   TEXT NOT NULL,
            title       TEXT,
            content     TEXT NOT NULL
        );

        -- RAG documents (per-user with optional sharing)
        CREATE TABLE IF NOT EXISTS rag_documents (
            doc_id      TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL DEFAULT 0,
            shared      INTEGER NOT NULL DEFAULT 0,
            filename    TEXT NOT NULL,
            file_type   TEXT NOT NULL,
            char_count  INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            uploaded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rag_chunks (
            chunk_id    TEXT PRIMARY KEY,
            doc_id      TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content     TEXT NOT NULL
        );

        -- Indexes for user_id lookups
        CREATE INDEX IF NOT EXISTS idx_semantic_user ON semantic_facts(user_id);
        CREATE INDEX IF NOT EXISTS idx_episodic_user ON episodic_entries(user_id);
        CREATE INDEX IF NOT EXISTS idx_episodic_type_user ON episodic_entries(user_id, entry_type);
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);
        CREATE INDEX IF NOT EXISTS idx_rag_docs_user ON rag_documents(user_id);
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc ON rag_chunks(doc_id);
        CREATE INDEX IF NOT EXISTS idx_session_tokens_expires ON session_tokens(expires_at);
        CREATE INDEX IF NOT EXISTS idx_transcripts_archive ON episodic_transcripts(archive_id);
        -- Message history is loaded per session, ordered by turn — avoid full scans.
        CREATE INDEX IF NOT EXISTS idx_session_messages_session_turn ON session_messages(session_id, turn_order);

        -- Login rate-limiting (persists across restarts so a restart can't reset the counter)
        CREATE TABLE IF NOT EXISTS login_attempts (
            id  INTEGER PRIMARY KEY AUTOINCREMENT,
            ip  TEXT NOT NULL,
            ts  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, ts);

        -- Handoff routing patterns (shared, no user data)
        CREATE TABLE IF NOT EXISTS handoff_patterns (
            id          TEXT PRIMARY KEY,
            pattern     TEXT NOT NULL,
            target_mode TEXT NOT NULL,
            confidence  REAL NOT NULL DEFAULT 1.0,
            use_count   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_handoff_mode ON handoff_patterns(target_mode);

        -- Cerebellum validation log
        -- Every pre/post tool-call check is logged here for pattern learning.
        -- Verdict: 'clear' | 'flag' | 'stop'. Score: cosine distance from intent.
        CREATE TABLE IF NOT EXISTS cerebellum_log (
            id              TEXT PRIMARY KEY,
            user_id         INTEGER NOT NULL DEFAULT 0,
            ts              REAL NOT NULL,
            tool_name       TEXT NOT NULL,
            phase           TEXT NOT NULL,
            verdict         TEXT NOT NULL,
            score           REAL NOT NULL DEFAULT 0.0,
            reason          TEXT NOT NULL,
            tool_args       TEXT NOT NULL DEFAULT '{}',
            output_snippet  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cerebellum_verdict ON cerebellum_log(verdict, ts);
        CREATE INDEX IF NOT EXISTS idx_cerebellum_tool ON cerebellum_log(tool_name, verdict);

        -- Daily briefings (generated on schedule, consumed on next chat open)
        CREATE TABLE IF NOT EXISTS pending_briefings (
            id              TEXT PRIMARY KEY,
            user_id         INTEGER NOT NULL DEFAULT 0,
            generated_at    REAL NOT NULL,
            content         TEXT NOT NULL,
            delivered       INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_briefings_user ON pending_briefings(user_id, delivered);

        -- Usage patterns (tool call tracking for proactive suggestions)
        CREATE TABLE IF NOT EXISTS usage_patterns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL DEFAULT 0,
            tool_name       TEXT NOT NULL,
            topic           TEXT,
            hour_of_day     INTEGER NOT NULL,
            day_of_week     INTEGER NOT NULL,
            ts              REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_patterns_user ON usage_patterns(user_id, tool_name);
        CREATE INDEX IF NOT EXISTS idx_patterns_time ON usage_patterns(user_id, hour_of_day, day_of_week);

        -- Goals (persistent cross-session tasks)
        CREATE TABLE IF NOT EXISTS goals (
            id              TEXT PRIMARY KEY,
            user_id         INTEGER NOT NULL DEFAULT 0,
            title           TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            steps_json      TEXT NOT NULL DEFAULT '[]',
            current_step    INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'active',
            notes           TEXT NOT NULL DEFAULT '',
            created_at      REAL NOT NULL,
            last_active     REAL NOT NULL,
            updated_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_goals_user_status ON goals(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_goals_stale ON goals(status, last_active);

        -- Study library (downloaded open-access content per user)
        CREATE TABLE IF NOT EXISTS study_library (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL DEFAULT 0,
            title       TEXT NOT NULL DEFAULT '',
            author      TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT '',
            original_url TEXT NOT NULL DEFAULT '',
            format      TEXT NOT NULL DEFAULT 'pdf',
            path        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_study_library_user ON study_library(user_id);

        -- Study library full-text chunks (for RAG over saved epubs/pdfs)
        CREATE TABLE IF NOT EXISTS study_chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER NOT NULL REFERENCES study_library(id) ON DELETE CASCADE,
            user_id     INTEGER NOT NULL DEFAULT 0,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            content     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_study_chunks_item ON study_chunks(item_id);
        CREATE INDEX IF NOT EXISTS idx_study_chunks_user ON study_chunks(user_id);
    """)

    # Vector tables (require sqlite-vec extension)
    # Live tables use FAST_EMBED_DIM (384) for CPU-based fastembed.
    # HQ shadow tables at HQ_EMBED_DIM (2560) are created by shutdown_reembed().
    if _check_sqlite_vec():
        from kai.config import FAST_EMBED_DIM
        dim = FAST_EMBED_DIM
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_vec
            USING vec0(embedding float[{dim}])
        """)
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_vec
            USING vec0(embedding float[{dim}])
        """)
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS handoff_vec
            USING vec0(embedding float[{dim}])
        """)

    conn.commit()
