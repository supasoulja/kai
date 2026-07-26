"""
kai/memory/state.py — Three state stores: user, kai, relationship.

The dynamic between two people is not reducible to either person alone.
That's why relationship state is its own store, not a derived value.

These three stores combine into a single context_modifier scalar that scales
every node score in the tree. Low relationship depth compresses scores toward
neutral (Kai is less certain, asserts less). High depth lets scores spread out.
"""

import json  # for serializing state to/from the database
import sqlite3  # the database driver
import threading  # thread-local connection cache (see _conn)
import time  # for timestamps
from dataclasses import asdict, dataclass, field, fields  # asdict turns a dataclass into a dict
from pathlib import Path  # clean path handling

# ─── Paths ────────────────────────────────────────────────────────────────────
from kai.config import STATE_DIR as _STATE_DIR  # runtime data: var/state/{user_id}.db

# ─── State dataclasses ────────────────────────────────────────────────────────
# Each one captures a different "who" — the person, Kai herself, or the bond
# between them. None of them can be derived from the others.


@dataclass
class UserState:
    """What's going on with the person right now. Recomputed often — this drifts fast."""

    emotional_register: str = "neutral"  # "stressed" | "focused" | "venting" | "casual" | "neutral"
    session_intent: str = "unknown"  # "troubleshooting" | "task" | "casual" | "unknown"
    terseness: float = 0.5  # 0=verbose, 1=one-word answers — shifts how Kai reads tone
    recent_override_rate: float = 0.0  # fraction of Kai's recent suggestions they overrode
    last_updated: float = field(default_factory=time.time)


@dataclass
class KaiState:
    """Kai's own internal read on herself this session. The variable that makes her a personality."""

    self_confidence: float = 0.7  # drops when corrected, recovers slowly over good turns
    certainty_in_user_read: float = 0.5  # how well Kai feels she actually knows this person
    correction_count_session: int = 0  # how many times the user has corrected Kai today
    intuition_active: bool = False  # True when something feels off but isn't fully formed yet
    last_updated: float = field(default_factory=time.time)


@dataclass
class RelationshipState:
    """
    The dynamic between them — exists in neither person alone.
    Persistent. Accumulates slowly. Changes here should be rare and meaningful.
    """

    trust_trajectory: str = "building"  # "building" | "stable" | "declining"
    relationship_depth: float = 0.0  # 0=stranger, 1=years of context — grows slowly
    session_count: int = 0  # total number of sessions together
    validation_count: int = 0  # times the user confirmed Kai's read was right
    correction_count: int = 0  # times the user corrected Kai's read
    shorthand_level: float = 0.0  # how much Kai can assume without spelling it out
    override_by_domain: dict = field(  # e.g. {"hardware": 1, "tone": 6}
        default_factory=dict  # default_factory=dict avoids the mutable-default bug
    )
    persistent_disagreements: list = field(
        default_factory=list  # topics they keep landing on differently
    )
    last_updated: float = field(default_factory=time.time)


# ─── DB helpers ───────────────────────────────────────────────────────────────


def _db_path(user_id: str) -> Path:
    """Path to this user's state database."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR / f"{user_id}.db"


def delete_user_db(user_id) -> None:
    """Permanently remove a user's state database (file + WAL/SHM sidecars).

    Called by store.users.delete_user. Best-effort: missing files are ignored.
    """
    _close(user_id)  # evict the cached connection so the file can be unlinked
    base = _STATE_DIR / f"{user_id}.db"
    for p in (base, base.with_suffix(".db-wal"), base.with_suffix(".db-shm")):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _connect(user_id: str) -> sqlite3.Connection:
    """Open this user's state database, creating the table on first use."""
    conn = sqlite3.connect(_db_path(user_id))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s if the file is locked
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key          TEXT PRIMARY KEY,   -- "user" | "kai" | "relationship"
            data         TEXT NOT NULL,      -- JSON-serialized dataclass
            last_updated REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


# Per-(thread, user_id) connection cache — see kai/memory/tree.py for the
# rationale. Avoids reopening + re-running CREATE TABLE on every load/save, and
# stops the per-call connection leak from the old `with _connect(...)` pattern.
_local = threading.local()


def _conn(user_id: str) -> sqlite3.Connection:
    """Return this thread's cached connection for the user, opening it once.

    Keyed by the resolved DB path so a STATE_DIR change (e.g. tests pointing it
    at a tmp dir) never reuses a stale connection from the old location."""
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
    file so the OS can unlink it)."""
    cache = getattr(_local, "conns", None)
    if not cache:
        return
    conn = cache.pop(str(_db_path(user_id)), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _save(user_id: str, key: str, obj) -> None:
    """Serialize a dataclass instance to JSON and upsert it under its key."""
    data = json.dumps(asdict(obj))  # asdict() converts dataclass → dict; dumps → JSON string
    with _conn(user_id) as conn:
        conn.execute(
            """
            INSERT INTO state VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET   -- upsert: overwrite if this key already exists
                data = excluded.data,
                last_updated = excluded.last_updated
        """,
            (key, data, time.time()),
        )


def _load(user_id: str, key: str, cls):
    """Load and deserialize a state object, or return a fresh default instance."""
    with _conn(user_id) as conn:
        row = conn.execute("SELECT data FROM state WHERE key = ?", (key,)).fetchone()
    if row is None:  # no record yet — first time talking to this user
        return cls()  # cls() calls the dataclass constructor with all defaults
    # Be defensive: a corrupt blob or a row written before a dataclass field was
    # added/removed must not crash the read. Keep only keys the dataclass knows,
    # and fall back to defaults if the JSON itself is unparseable.
    try:
        raw = json.loads(row["data"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})
    except Exception:
        return cls()


# ─── Public load/save API ─────────────────────────────────────────────────────


def load_user_state(user_id: str) -> UserState:
    return _load(user_id, "user", UserState)


def save_user_state(user_id: str, state: UserState) -> None:
    state.last_updated = time.time()
    _save(user_id, "user", state)


def load_kai_state(user_id: str) -> KaiState:
    return _load(user_id, "kai", KaiState)


def save_kai_state(user_id: str, state: KaiState) -> None:
    state.last_updated = time.time()
    _save(user_id, "kai", state)


def load_relationship_state(user_id: str) -> RelationshipState:
    return _load(user_id, "relationship", RelationshipState)


def save_relationship_state(user_id: str, state: RelationshipState) -> None:
    state.last_updated = time.time()
    _save(user_id, "relationship", state)


# ─── Update helpers — small, meaningful mutations called by the memory model ──


def record_validation(user_id: str) -> None:
    """User confirmed Kai's read was right. Strengthens trust and depth slowly."""
    rel = load_relationship_state(user_id)
    rel.validation_count += 1
    # depth grows slowly and asymptotically — each validation matters less as trust builds
    rel.relationship_depth = min(1.0, rel.relationship_depth + 0.02)
    if rel.trust_trajectory == "declining":  # a validation can interrupt a decline
        rel.trust_trajectory = "stable"
    save_relationship_state(user_id, rel)


def record_correction(user_id: str, domain: str = "") -> None:
    """User corrected Kai. Updates both her self-confidence and the relationship record."""
    kai = load_kai_state(user_id)
    kai.correction_count_session += 1
    # self-confidence dips with each correction but never collapses to zero
    kai.self_confidence = max(0.3, kai.self_confidence - 0.05)
    save_kai_state(user_id, kai)

    rel = load_relationship_state(user_id)
    rel.correction_count += 1
    if domain:  # track which domains generate friction
        rel.override_by_domain[domain] = rel.override_by_domain.get(domain, 0) + 1
    # repeated corrections without validation in between suggest declining trust
    if rel.correction_count > rel.validation_count * 2 and rel.correction_count > 3:
        rel.trust_trajectory = "declining"
    save_relationship_state(user_id, rel)


def record_session_start(user_id: str) -> None:
    """Called once per session. Increments session count, nudges depth, resets session-scoped state."""
    rel = load_relationship_state(user_id)
    rel.session_count += 1
    # depth grows from raw session count too, separate from validation —
    # showing up consistently builds familiarity on its own
    rel.relationship_depth = min(1.0, rel.relationship_depth + 0.01)
    save_relationship_state(user_id, rel)

    kai = load_kai_state(user_id)
    kai.correction_count_session = 0  # session-scoped counter resets
    kai.self_confidence = min(1.0, kai.self_confidence + 0.05)  # small recovery between sessions
    save_kai_state(user_id, kai)


# ─── Context modifier — the bridge into the scoring equation ──────────────────

_TRUST_FACTOR = {
    "building": 1.0,  # neutral — still establishing the baseline
    "stable": 1.0,  # neutral — relationship is healthy
    "declining": 0.8,  # dampen scores — be more tentative when trust is shaky
}


def compute_context_modifier(
    user_id: str, rel: "RelationshipState | None" = None, kai: "KaiState | None" = None
) -> float:
    """
    Combine all three state stores into the single scalar the scorer multiplies in.

    - relationship_depth widens the spread: well-known users get more confident scoring
    - trust_trajectory dampens everything when declining
    - kai's own self_confidence this session pulls the ceiling down when she's been wrong a lot

    Range is roughly 0.3–1.0. Never zero — Kai always has *some* basis to act on.

    Pass already-loaded `rel`/`kai` to avoid reloading them — gather_context
    loads both for rendering, so it hands them through instead of re-reading.
    """
    if rel is None:
        rel = load_relationship_state(user_id)
    if kai is None:
        kai = load_kai_state(user_id)

    # Low depth compresses toward 0.5 (cautious); high depth allows full range up to 1.0
    depth_factor = 0.5 + (rel.relationship_depth * 0.5)  # ranges 0.5–1.0

    # Declining trust dampens every score — Kai surfaces things more tentatively
    trust_factor = _TRUST_FACTOR.get(rel.trust_trajectory, 1.0)  # 0.8 or 1.0

    # Kai's own self-confidence this session sets a soft ceiling
    confidence_factor = 0.7 + (kai.self_confidence * 0.3)  # ranges 0.91–1.0 at typical values

    return depth_factor * trust_factor * confidence_factor


def explain_context_modifier(user_id: str) -> dict:
    """Breakdown of the context modifier for debugging — same pattern as scorer.explain_score."""
    rel = load_relationship_state(user_id)
    kai = load_kai_state(user_id)

    depth_factor = 0.5 + (rel.relationship_depth * 0.5)
    trust_factor = _TRUST_FACTOR.get(rel.trust_trajectory, 1.0)
    confidence_factor = 0.7 + (kai.self_confidence * 0.3)

    return {
        "final_modifier": round(depth_factor * trust_factor * confidence_factor, 4),
        "factors": {
            "relationship_depth": round(rel.relationship_depth, 4),
            "depth_factor": round(depth_factor, 4),
            "trust_trajectory": rel.trust_trajectory,
            "trust_factor": trust_factor,
            "kai_self_confidence": round(kai.self_confidence, 4),
            "confidence_factor": round(confidence_factor, 4),
        },
    }
