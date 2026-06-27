"""
kai/memory/tree.py — Filesystem-style hierarchical memory store.

Nodes live at paths like "user/identity/profession" or "user/preferences/gaming/fps".
The path carries meaning before you read the file — navigating to a subtree is the
coarse filter, vector search within it is the fine filter.

Empty folders cost nothing. Missing folders cost judgment.
"""

import sqlite3                           # standard library — Python's built-in database driver
import threading                         # thread-local connection cache (see _conn)
import time                              # for unix timestamps on node creation/update
import numpy as np                       # numerical arrays — used to store and compare embeddings
from pathlib import Path                 # cleaner file path handling than raw strings
from dataclasses import dataclass, field # dataclass auto-generates __init__, __repr__, etc.
from typing import Optional              # type hint for values that might be None


# ─── Paths ────────────────────────────────────────────────────────────────────

# Runtime data lives outside the package — see kai/config.py (var/tree/{user_id}.db).
from kai.config import TREE_DIR as _TREE_DIR

# These path prefixes bypass the scoring equation entirely — always surface first.
# A node is hardcoded if its path starts with any of these strings.
HARDCODED_PREFIXES = {
    "user/health",          # medical conditions, allergies — never let recency bury these
    "user/identity/hardware",  # GPU/CPU/RAM — rarely changes, always relevant to system tools
    "user/identity/profession", # stuntman vs office worker changes how Kai reads everything
    "user/identity/critical",   # catch-all for anything the user explicitly pins
}


# ─── Node dataclass ───────────────────────────────────────────────────────────

@dataclass                               # @dataclass auto-writes __init__ from the fields below
class Node:
    """One memory node. Conceptually a file in the tree. Stored as a DB row."""

    path: str                            # full address: "user/preferences/gaming/fps"
    value: str                           # the fact itself: "144hz minimum, competitive"

    confidence: float = 0.5             # 0.0 = pure guess, 1.0 = user stated explicitly
    importance: float = 0.5             # 0.0 = trivial, 1.0 = critical to judgment
    specificity: float = 0.5            # 0.0 = broad claim, 1.0 = precise detail
    source: str = "inferred"            # how we learned it: "stated" | "inferred" | "pattern"
    frequency: int = 1                  # times this node has been confirmed or queried
    decays: bool = True                 # False = recency never penalizes (hardware, medical)
    last_updated: float = field(        # field() lets us set a dynamic default
        default_factory=time.time       # default_factory is called at creation time, not import time
    )
    domain: str = ""                    # comma-separated tags: "gaming,hardware" or ""
    hardcoded_type: bool = False        # True = skip scoring, always surface at the top
    embedding: Optional[np.ndarray] = None  # 384-dim vector; None until embed runs

    @property                           # @property turns a method into a readable attribute
    def name(self) -> str:
        """The leaf name — last segment of the path."""
        return self.path.split("/")[-1] # split on slash, take the last piece

    @property
    def parent_path(self) -> str:
        """The path one level up. Empty string if this is a root-level node."""
        parts = self.path.split("/")            # ["user", "preferences", "gaming", "fps"]
        return "/".join(parts[:-1]) if len(parts) > 1 else ""  # drop the last segment

    @property
    def depth(self) -> int:
        """How many levels deep. "user/identity/profession" is depth 3."""
        return len(self.path.split("/"))        # count the slash-separated segments


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db_path(user_id: str) -> Path:
    """Return the path to this user's tree database file."""
    _TREE_DIR.mkdir(parents=True, exist_ok=True)  # create the directory if it doesn't exist
    return _TREE_DIR / f"{user_id}.db"


def delete_user_db(user_id) -> None:
    """Permanently remove a user's tree database (file + WAL/SHM sidecars).

    Used by store.users.delete_user so account deletion wipes per-user files,
    not just the main DB rows. Best-effort: missing files are ignored.
    """
    _close(user_id)          # evict the cached connection so the file can be unlinked
    base = _TREE_DIR / f"{user_id}.db"
    for p in (base, base.with_suffix(".db-wal"), base.with_suffix(".db-shm")):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass             # e.g. kai/memory/tree/1.db


def _connect(user_id: str) -> sqlite3.Connection:
    """Open (or create) the database for this user and return a connection."""
    conn = sqlite3.connect(_db_path(user_id))  # opens the file; creates it if missing
    conn.row_factory = sqlite3.Row             # makes rows dict-like: row["path"] instead of row[0]
    conn.execute("PRAGMA journal_mode=WAL")    # WAL mode: reads don't block writes
    conn.execute("PRAGMA busy_timeout=5000")   # wait up to 5s if another process holds the lock
    _init(conn)                                # create the table and index on first run
    return conn


# Connections are cached per (thread, user_id). sqlite3 connections can't be
# shared across threads, so we key by thread via threading.local(). This avoids
# reopening the file + re-running CREATE TABLE on every operation — and avoids
# leaking connections (sqlite3's `with conn:` commits the transaction but never
# closes the connection, so the old "with _connect(...)" pattern leaked one per
# call). Callers keep `with _conn(...)` for transaction (commit/rollback) scope.
_local = threading.local()


def _conn(user_id: str) -> sqlite3.Connection:
    """Return this thread's cached connection for the user, opening it once.

    Keyed by the resolved DB path (not just user_id) so that if TREE_DIR changes
    — e.g. tests monkeypatch it to a tmp dir — a stale connection from the old
    location is never reused."""
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    key = str(_db_path(user_id))
    conn = cache.get(key)
    if conn is None:
        conn = cache[key] = _connect(user_id)
    return conn


def _close(user_id) -> None:
    """Evict and close this thread's cached connection (call before deleting the
    file so the OS — notably Windows — can unlink it)."""
    cache = getattr(_local, "conns", None)
    if not cache:
        return
    conn = cache.pop(str(_db_path(user_id)), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _init(conn: sqlite3.Connection) -> None:
    """Create the nodes table and index if they don't already exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            path           TEXT PRIMARY KEY,    -- unique tree address; PRIMARY KEY = auto-indexed
            value          TEXT NOT NULL,       -- the fact; NOT NULL = must always have a value
            confidence     REAL DEFAULT 0.5,    -- REAL = floating point number
            importance     REAL DEFAULT 0.5,
            specificity    REAL DEFAULT 0.5,
            source         TEXT DEFAULT 'inferred',
            frequency      INTEGER DEFAULT 1,   -- INTEGER = whole number
            decays         INTEGER DEFAULT 1,   -- SQLite has no bool; 1=True, 0=False
            last_updated   REAL NOT NULL,       -- unix timestamp (seconds since 1970)
            domain         TEXT DEFAULT '',
            hardcoded_type INTEGER DEFAULT 0,
            embedding      BLOB                 -- BLOB = raw bytes; stores numpy array
        )
    """)
    # A second index on path lets SQLite do fast prefix scans for subtree queries
    conn.execute("CREATE INDEX IF NOT EXISTS idx_path ON nodes(path)")
    conn.commit()   # flush the schema changes to disk


def _row_to_node(row: sqlite3.Row) -> Node:
    """Convert a raw database row back into a typed Node object."""
    emb = None
    if row["embedding"]:                                        # embedding column may be NULL
        emb = np.frombuffer(row["embedding"], dtype=np.float32)  # raw bytes → 384-dim float array
    return Node(
        path=row["path"],
        value=row["value"],
        confidence=row["confidence"],
        importance=row["importance"],
        specificity=row["specificity"],
        source=row["source"],
        frequency=row["frequency"],
        decays=bool(row["decays"]),               # SQLite INTEGER → Python bool
        last_updated=row["last_updated"],
        domain=row["domain"] or "",               # or "" handles NULL domain
        hardcoded_type=bool(row["hardcoded_type"]),
        embedding=emb,
    )


def _is_hardcoded(path: str) -> bool:
    """Check if a path falls under any hardcoded prefix."""
    return any(path == p or path.startswith(p + "/") for p in HARDCODED_PREFIXES)
    # e.g. "user/health/condition/diabetes" starts with "user/health" → True


# ─── Write ────────────────────────────────────────────────────────────────────

def write(user_id: str, node: Node) -> None:
    """
    Insert a new node or replace an existing one at the same path.
    Automatically flags hardcoded_type based on path prefix.
    """
    node.hardcoded_type = _is_hardcoded(node.path)  # set flag from path, not caller
    node.last_updated = time.time()                  # always stamp the write time

    # Convert numpy array to raw bytes for SQLite BLOB storage
    emb_bytes = node.embedding.tobytes() if node.embedding is not None else None

    with _conn(user_id) as conn:  # "with" = context manager; auto-commits on exit
        conn.execute("""
            INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET   -- upsert: update every field if path exists
                value          = excluded.value,       -- "excluded" = the row we tried to insert
                confidence     = excluded.confidence,
                importance     = excluded.importance,
                specificity    = excluded.specificity,
                source         = excluded.source,
                frequency      = excluded.frequency,
                decays         = excluded.decays,
                last_updated   = excluded.last_updated,
                domain         = excluded.domain,
                hardcoded_type = excluded.hardcoded_type,
                embedding      = excluded.embedding
        """, (
            node.path, node.value, node.confidence, node.importance,
            node.specificity, node.source, node.frequency,
            int(node.decays),           # bool → INTEGER for SQLite
            node.last_updated,
            node.domain,
            int(node.hardcoded_type),   # bool → INTEGER for SQLite
            emb_bytes,
        ))


def increment_frequency(user_id: str, path: str) -> None:
    """Bump the frequency counter when a node is confirmed or successfully queried."""
    with _conn(user_id) as conn:
        conn.execute(
            "UPDATE nodes SET frequency = frequency + 1 WHERE path = ?", (path,)
        )   # SQL UPDATE modifies in-place; no need to read-then-write


def update_embedding(user_id: str, path: str, embedding: np.ndarray) -> None:
    """Swap in a new embedding for an existing node. Called after high-quality re-embed."""
    with _conn(user_id) as conn:
        conn.execute(
            "UPDATE nodes SET embedding = ? WHERE path = ?",
            (embedding.tobytes(), path)     # tobytes() = numpy array → raw bytes
        )


# ─── Read ─────────────────────────────────────────────────────────────────────

def read(user_id: str, path: str) -> Optional[Node]:
    """Fetch one node by exact path. Returns None if the path doesn't exist."""
    with _conn(user_id) as conn:
        row = conn.execute(
            "SELECT * FROM nodes WHERE path = ?", (path,)  # the (path,) is a tuple — SQL params
        ).fetchone()    # fetchone() returns one row or None
    return _row_to_node(row) if row else None   # row if row: convert it; else return None


def children(user_id: str, path: str) -> list[Node]:
    """
    Immediate children of a path — one level down only.
    e.g. children("user/preferences") returns nodes at "user/preferences/gaming",
    "user/preferences/airflow", etc. — not "user/preferences/gaming/fps".
    """
    prefix = path + "/"      # children start with this prefix
    prefix_len = len(prefix)  # used to check depth after the prefix

    with _conn(user_id) as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE path LIKE ?", (prefix + "%",)
            # LIKE with % wildcard: matches anything starting with prefix
        ).fetchall()

    # Filter to direct children only: no additional "/" after the prefix means one level down
    return [
        _row_to_node(r) for r in rows
        if "/" not in r["path"][prefix_len:]   # slice off the prefix, check the remainder
    ]


def subtree(user_id: str, path: str) -> list[Node]:
    """All descendants at any depth, plus the node itself."""
    with _conn(user_id) as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE path = ? OR path LIKE ?",
            (path, path + "/%")   # exact match OR any descendant
        ).fetchall()
    return [_row_to_node(r) for r in rows]


def all_nodes(user_id: str) -> list[Node]:
    """Every node for this user. Used by the memory model for full scans."""
    with _conn(user_id) as conn:
        rows = conn.execute("SELECT * FROM nodes").fetchall()
    return [_row_to_node(r) for r in rows]


def count_facts(user_id: str) -> int:
    """How many real (non-seed) facts this user's tree holds.

    Cheap existence check: the brain uses it to decide whether the
    [MEMORY CONTEXT] block is worth rendering at all this turn.
    """
    with _conn(user_id) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE source != 'seed'"
        ).fetchone()[0]


def hardcoded_nodes(user_id: str) -> list[Node]:
    """All nodes that bypass scoring. Always surface these before scored nodes."""
    with _conn(user_id) as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE hardcoded_type = 1"
        ).fetchall()
    return [_row_to_node(r) for r in rows]


def domain_nodes(user_id: str, domain: str) -> list[Node]:
    """All nodes tagged with a specific domain. Used for domain-scoped searches."""
    with _conn(user_id) as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE domain LIKE ?", (f"%{domain}%",)
            # LIKE with % on both sides: matches "gaming" inside "gaming,hardware"
        ).fetchall()
    return [_row_to_node(r) for r in rows]


# ─── Delete ───────────────────────────────────────────────────────────────────

def delete(user_id: str, path: str) -> None:
    """Remove one node. Does not touch its children."""
    with _conn(user_id) as conn:
        conn.execute("DELETE FROM nodes WHERE path = ?", (path,))


def delete_subtree(user_id: str, path: str) -> None:
    """Remove a node and all descendants. Called by the merge pass."""
    with _conn(user_id) as conn:
        conn.execute(
            "DELETE FROM nodes WHERE path = ? OR path LIKE ?",
            (path, path + "/%")   # same pattern as subtree() query
        )


# ─── Bootstrap seed ───────────────────────────────────────────────────────────
# The main folders every user's tree starts with (BRAIN_DESIGN: "an empty
# folder costs nothing, a missing folder costs judgment"). Folder nodes are
# plain index entries — source='seed', no embedding — so browsing shows the
# layout but semantic search never surfaces them as facts. Filing a real fact
# at one of these paths simply overwrites the index entry.

SKELETON: list[tuple[str, str]] = [
    ("user",                     "(folder) everything known about this user"),
    ("user/identity",            "(folder) who they are — name, age, profession, hardware"),
    ("user/identity/profession", "(folder) what they do for work"),
    ("user/identity/hardware",   "(folder) their machine — CPU, GPU, RAM, OS, peripherals"),
    ("user/identity/critical",   "(folder) facts the user explicitly said to never forget"),
    ("user/health",              "(folder) medical conditions, allergies, accessibility needs"),
    ("user/preferences",         "(folder) how they like things — defaults, styles, tastes"),
    ("user/preferences/gaming",  "(folder) games, genres, performance expectations"),
    ("user/patterns",            "(folder) recurring behaviors, habits, stress signals"),
    ("user/knowledge",           "(folder) what the user knows well — their expertise"),
    ("user/history",             "(folder) notable events and decisions worth recalling"),
    ("user/history/decisions",   "(folder) choices made and why"),
    ("user/history/events",      "(folder) things that happened — upgrades, incidents, wins"),
]


def seed_skeleton(user_id: str) -> int:
    """Create the main folders for a new user's tree.

    Idempotent: paths that already exist are never touched, so calling this
    on every startup or tool use costs almost nothing. Returns how many
    folder nodes were actually created (0 on an already-seeded tree).
    """
    created = 0
    for path, label in SKELETON:
        if read(user_id, path) is None:
            write(user_id, Node(
                path=path, value=label,
                confidence=1.0, importance=0.2, specificity=0.0,
                source="seed", decays=False,
            ))
            created += 1
    return created


# ─── Self-organization ────────────────────────────────────────────────────────

def split_node(user_id: str, old_path: str, new_nodes: list[Node]) -> None:
    """
    Replace one coarse node with multiple specific children.
    Called by the memory model when a node's queries return noisy mixed results.
    Example: "user/preferences/temperature" → ["user/preferences/temperature/default",
                                               "user/preferences/temperature/sleep"]
    """
    delete(user_id, old_path)       # remove the coarse parent
    for node in new_nodes:          # write each fine-grained replacement
        write(user_id, node)


def merge_nodes(user_id: str, paths: list[str], into: Node) -> None:
    """
    Collapse multiple nodes into one combined parent.
    Called by the memory model's periodic redundancy pass when nodes are
    almost always co-queried and never need to be retrieved independently.
    """
    for path in paths:              # remove each node being collapsed
        delete(user_id, path)
    write(user_id, into)            # write the single combined node
