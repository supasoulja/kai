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

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all tables once. No-op after the first call."""
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if _schema_initialized:
            return
        _maybe_migrate_fresh(conn)
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
        "campaigns", "campaign_npcs", "campaign_events", "campaign_quests",
    ]
    # Drop vector tables first (virtual tables)
    for vt in ["episodic_vec", "rag_chunks_vec", "campaign_npc_vec", "campaign_event_vec"]:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {vt}")
        except Exception:
            pass
    for table in tables_to_drop:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


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

        -- Campaigns (per-user ownership)
        CREATE TABLE IF NOT EXISTS campaigns (
            id          TEXT PRIMARY KEY,
            owner_id    INTEGER NOT NULL DEFAULT 0,
            name        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            last_active TEXT NOT NULL
        );

        -- Per-user active campaign (replaces global is_active flag)
        CREATE TABLE IF NOT EXISTS user_active_campaigns (
            user_id     INTEGER PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES campaigns(id)
        );

        -- Campaign access control (owner + invited players)
        CREATE TABLE IF NOT EXISTS campaign_access (
            campaign_id TEXT NOT NULL REFERENCES campaigns(id),
            user_id     INTEGER NOT NULL,
            role        TEXT NOT NULL DEFAULT 'player',
            PRIMARY KEY (campaign_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS campaign_npcs (
            id          TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            name        TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'alive',
            updated_at  TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS campaign_events (
            id          TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            content     TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            metadata    TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS campaign_quests (
            id          TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'active',
            updated_at  TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        -- Campaign characters (D&D Beyond linked)
        CREATE TABLE IF NOT EXISTS campaign_characters (
            id              TEXT PRIMARY KEY,
            campaign_id     TEXT NOT NULL REFERENCES campaigns(id),
            user_id         INTEGER NOT NULL DEFAULT 0,
            name            TEXT NOT NULL,
            dndbeyond_url   TEXT,
            character_data  TEXT NOT NULL DEFAULT '{}',
            updated_at      TEXT NOT NULL
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
        CREATE INDEX IF NOT EXISTS idx_campaigns_owner ON campaigns(owner_id);
    """)

    # Vector tables (require sqlite-vec extension)
    if _check_sqlite_vec():
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_vec
            USING vec0(embedding float[2560])
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_vec
            USING vec0(embedding float[2560])
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS campaign_npc_vec
            USING vec0(embedding float[768])
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS campaign_event_vec
            USING vec0(embedding float[768])
        """)

    conn.commit()
