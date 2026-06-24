"""
Knowledge system — two-layer learned intelligence:

  HandoffRouter  — shared, no user data. Embeds routing patterns and
                   classifies each user turn to decide which mode
                   (chat / reasoning / tool / researcher) should handle it.
                   Starts from seed patterns; grows dynamically via learn().

  KnowledgeStore — per-user, fully isolated. Stores facts the researcher
                   discovers, searchable by vector similarity. Each user
                   gets their own SQLite file; deleting it removes all their
                   learned data with zero cross-user risk.
"""
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from kai.config import FAST_EMBED_DIM, HANDOFF_THRESHOLD

EmbedFn = Callable[[str], list[float]]

# ── Paths ──────────────────────────────────────────────────────────────────────

def _user_db_path(user_id: int) -> Path:
    from kai.config import KNOWLEDGE_DIR
    user_dir = KNOWLEDGE_DIR / "users"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{user_id}.db"


def delete_user_db(user_id: int) -> None:
    """Permanently remove a user's knowledge database (file + WAL/SHM sidecars).

    Called by store.users.delete_user so account deletion wipes the per-user
    learned-knowledge file. Best-effort: missing files are ignored. On Linux,
    unlinking an open SQLite file is safe; on Windows the caller should evict
    the user's Brain first so cached connections are released.
    """
    base = _user_db_path(user_id)
    for p in (base, base.with_suffix(".db-wal"), base.with_suffix(".db-shm")):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


# ── Seed patterns ──────────────────────────────────────────────────────────────
# Bootstrap routing before the system has learned from real usage.
# Written in natural user-query language — same phrasing real users will use.

_SEED_PATTERNS: list[tuple[str, str]] = [
    ("debug this code, find the bug, fix this error, something is broken", "tool"),
    ("run this command, execute this, check my system, show me my hardware", "tool"),
    ("create a goal for me, show my goals, update my goal progress, mark the goal complete", "tool"),
    ("write code for, create a function, build a script, implement this feature", "tool"),
    ("search for, look this up, research this topic, find information about", "researcher"),
    ("what is, who is, when did, how does, tell me about, explain this concept", "researcher"),
    ("think through this carefully, reason step by step, complex analysis, explain why", "reasoning"),
    ("summarize this in depth, analyze this thoroughly, give me a detailed breakdown", "reasoning"),
    ("what did I tell you, do you remember, recall our conversation, what do you know about me", "chat"),
]


# ── HandoffRouter ──────────────────────────────────────────────────────────────

class HandoffRouter:
    """
    Classifies each user turn to decide which mode handles it.
    Patterns live in the main kai.db (handoff_patterns + handoff_vec tables).
    Uses the FAST embed (384-dim CPU) — always on, zero VRAM cost.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False

    def init(self, embed_fn: EmbedFn) -> None:
        """Call once at Brain startup. Seeds patterns if the table is empty."""
        with self._lock:
            if self._initialized:
                return
            self._initialized = True
        self._seed_if_empty(embed_fn)

    def route(
        self,
        user_message: str,
        embed_fn: EmbedFn,
        query_emb: list[float] | None = None,
    ) -> tuple[str, float]:
        """
        Returns (target_mode, confidence) where confidence is cosine similarity.
        target_mode: 'chat' | 'reasoning' | 'tool' | 'researcher'
        Falls back to 'chat' if nothing matches above HANDOFF_THRESHOLD.

        Pass query_emb when the message is already embedded (the brain embeds
        each turn once and shares the vector) — embed_fn is only the fallback.
        """
        try:
            from kai.store.db import get_conn, sqlite_vec_available
            if not sqlite_vec_available():
                return "chat", 0.0

            import sqlite_vec

            vec = query_emb if query_emb is not None else embed_fn(user_message)
            query_bytes = sqlite_vec.serialize_float32(vec)
            conn = get_conn()

            # Idiom note: vec_distance_cosine inline JOIN, *not* the vec0 MATCH
            # two-step (db.vec_knn). vec0 MATCH forbids JOINs, but here we need
            # hp.target_mode from the sibling table in one shot — so the inline
            # distance function is the right tool. See db.vec_knn for the other idiom.
            row = conn.execute("""
                SELECT hp.rowid, hp.target_mode,
                       vec_distance_cosine(hv.embedding, ?) AS dist
                FROM handoff_vec hv
                JOIN handoff_patterns hp ON hp.rowid = hv.rowid
                ORDER BY dist ASC
                LIMIT 1
            """, (query_bytes,)).fetchone()

            if not row:
                return "chat", 0.0

            rowid, mode, dist = row
            # cosine distance: 0=identical, 2=opposite → similarity = 1 - dist
            similarity = round(1.0 - dist, 3)

            if dist > HANDOFF_THRESHOLD:
                return "chat", similarity

            conn.execute(
                "UPDATE handoff_patterns SET use_count = use_count + 1 WHERE rowid = ?",
                (rowid,)
            )
            conn.commit()
            return mode, similarity

        except Exception:
            return "chat", 0.0

    def learn(self, pattern: str, target_mode: str, embed_fn: EmbedFn) -> None:
        """
        Add a new routing pattern. Call this when a routing decision proves
        useful — e.g. after the researcher successfully finds something, save
        the user message that triggered it as a new researcher pattern.
        """
        try:
            from kai.store.db import get_conn, sqlite_vec_available
            if not sqlite_vec_available():
                return

            import sqlite_vec

            conn = get_conn()
            pattern_id = str(uuid.uuid4())
            ts = datetime.now().isoformat()

            conn.execute(
                "INSERT INTO handoff_patterns "
                "(id, pattern, target_mode, confidence, use_count, created_at) "
                "VALUES (?, ?, ?, 1.0, 0, ?)",
                (pattern_id, pattern, target_mode, ts)
            )
            conn.commit()

            rowid = conn.execute(
                "SELECT rowid FROM handoff_patterns WHERE id = ?", (pattern_id,)
            ).fetchone()[0]

            conn.execute(
                "INSERT INTO handoff_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, sqlite_vec.serialize_float32(embed_fn(pattern)))
            )
            conn.commit()

        except Exception:
            pass

    def list_patterns(self) -> list[dict]:
        """Return all patterns sorted by use count — useful for inspection/debugging."""
        try:
            from kai.store.db import get_conn
            conn = get_conn()
            rows = conn.execute(
                "SELECT pattern, target_mode, confidence, use_count, created_at "
                "FROM handoff_patterns ORDER BY use_count DESC"
            ).fetchall()
            return [
                {"pattern": r[0], "mode": r[1], "confidence": r[2],
                 "use_count": r[3], "created_at": r[4]}
                for r in rows
            ]
        except Exception:
            return []

    def _seed_if_empty(self, embed_fn: EmbedFn) -> None:
        try:
            from kai.store.db import get_conn, sqlite_vec_available
            if not sqlite_vec_available():
                return

            import sqlite_vec

            conn = get_conn()
            count = conn.execute("SELECT COUNT(*) FROM handoff_patterns").fetchone()[0]
            if count > 0:
                return

            for pattern, mode in _SEED_PATTERNS:
                pid = str(uuid.uuid4())
                ts = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO handoff_patterns "
                    "(id, pattern, target_mode, confidence, use_count, created_at) "
                    "VALUES (?, ?, ?, 1.0, 0, ?)",
                    (pid, pattern, mode, ts)
                )
                conn.commit()
                rowid = conn.execute(
                    "SELECT rowid FROM handoff_patterns WHERE id = ?", (pid,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO handoff_vec (rowid, embedding) VALUES (?, ?)",
                    (rowid, sqlite_vec.serialize_float32(embed_fn(pattern)))
                )
                conn.commit()

        except Exception:
            pass


# ── KnowledgeStore ─────────────────────────────────────────────────────────────

class KnowledgeStore:
    """
    Per-user knowledge store. Each user gets their own SQLite file at
    kai/memory/knowledge/users/{user_id}.db

    Written by the researcher model when it discovers something worth keeping.
    Searched by all models when they need external context beyond conversation history.
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._db_path = _user_db_path(user_id)
        self._local = threading.local()
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            pass

        self._local.conn = conn
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        with self._schema_lock:
            if self._schema_ready:
                return
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge (
                    id          TEXT PRIMARY KEY,
                    content     TEXT NOT NULL,
                    source      TEXT NOT NULL DEFAULT 'researcher',
                    topic       TEXT,
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_topic   ON knowledge(topic);
                CREATE INDEX IF NOT EXISTS idx_knowledge_created ON knowledge(created_at);
            """)
            try:
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec
                    USING vec0(embedding float[{FAST_EMBED_DIM}])
                """)
            except Exception:
                pass
            conn.commit()
            self._schema_ready = True

    def learn(
        self,
        content: str,
        embed_fn: EmbedFn,
        source: str = "researcher",
        topic: str | None = None,
    ) -> None:
        """Save a new piece of learned knowledge for this user."""
        try:
            import sqlite_vec
            conn = self._conn()
            entry_id = str(uuid.uuid4())
            ts = datetime.now().isoformat()

            conn.execute(
                "INSERT INTO knowledge (id, content, source, topic, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (entry_id, content, source, topic, ts)
            )
            conn.commit()

            rowid = conn.execute(
                "SELECT rowid FROM knowledge WHERE id = ?", (entry_id,)
            ).fetchone()[0]

            conn.execute(
                "INSERT INTO knowledge_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, sqlite_vec.serialize_float32(embed_fn(content)))
            )
            conn.commit()
        except Exception:
            pass

    def search(
        self,
        query: str,
        embed_fn: EmbedFn,
        top_k: int = 5,
        threshold: float = 0.6,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        """
        Vector search for relevant knowledge.
        Returns list of {content, source, topic, similarity} dicts,
        filtered to entries with similarity >= threshold.

        Pass `query_embedding` (same FAST_EMBED space) to reuse an embedding the
        caller already computed this turn and skip the redundant embed_fn call.
        """
        try:
            import sqlite_vec
            conn = self._conn()
            vec = query_embedding if query_embedding is not None else embed_fn(query)
            query_bytes = sqlite_vec.serialize_float32(vec)

            # Idiom note: vec_distance_cosine inline JOIN, *not* db.vec_knn's vec0
            # MATCH two-step — we need k.content/source/topic alongside the score
            # and vec0 MATCH can't JOIN, so distance is computed inline.
            rows = conn.execute("""
                SELECT k.content, k.source, k.topic,
                       1.0 - vec_distance_cosine(kv.embedding, ?) AS similarity
                FROM knowledge_vec kv
                JOIN knowledge k ON k.rowid = kv.rowid
                ORDER BY vec_distance_cosine(kv.embedding, ?) ASC
                LIMIT ?
            """, (query_bytes, query_bytes, top_k)).fetchall()

            return [
                {
                    "content": r[0],
                    "source": r[1],
                    "topic": r[2],
                    "similarity": round(r[3], 3),
                }
                for r in rows
                if r[3] >= threshold
            ]
        except Exception:
            return []

    def count(self) -> int:
        """How many knowledge entries this user has."""
        try:
            return self._conn().execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        except Exception:
            return 0

    def recent(self, limit: int = 10) -> list[dict]:
        """Return the most recently added entries — useful for inspection."""
        try:
            rows = self._conn().execute(
                "SELECT content, source, topic, created_at FROM knowledge "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [
                {"content": r[0], "source": r[1], "topic": r[2], "created_at": r[3]}
                for r in rows
            ]
        except Exception:
            return []
