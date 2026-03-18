"""
Episodic memory — timestamped events stored as embeddings.
Uses sqlite-vec for cosine similarity search.
Falls back to substring search if sqlite-vec is not installed.
"""
import json
import uuid
from datetime import datetime
from typing import Callable

from kai.config import EPISODIC_TOP_K
from kai.db import get_conn, sqlite_vec_available
from kai.schema import EpisodicEntry

EmbedFn = Callable[[str], list[float]]


def add_entry(
    content: str,
    embed_fn: EmbedFn | None = None,
    entry_type: str = "turn",
    metadata: dict | None = None,
) -> str:
    """Store an episodic entry. Returns the entry ID."""
    entry_id = str(uuid.uuid4())
    ts = datetime.now().isoformat()
    meta_json = json.dumps(metadata or {})

    conn = get_conn()
    conn.execute(
        "INSERT INTO episodic_entries (id, content, timestamp, entry_type, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (entry_id, content, ts, entry_type, meta_json)
    )
    conn.commit()
    rowid = conn.execute(
        "SELECT rowid FROM episodic_entries WHERE id = ?", (entry_id,)
    ).fetchone()[0]

    # Embedding is best-effort — a failure here never loses the text entry above.
    if embed_fn and sqlite_vec_available():
        try:
            import sqlite_vec
            embedding = embed_fn(content)
            conn.execute(
                "INSERT INTO episodic_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, sqlite_vec.serialize_float32(embedding))
            )
            conn.commit()
        except Exception:
            from kai.config import DEBUG
            if DEBUG:
                import traceback; traceback.print_exc()

    return entry_id


def search(
    query: str,
    embed_fn: EmbedFn | None = None,
    top_k: int = EPISODIC_TOP_K,
) -> list[EpisodicEntry]:
    """
    Search episodic memory. Uses vector similarity if available,
    falls back to substring search.
    """
    if embed_fn and sqlite_vec_available():
        return _vector_search(query, embed_fn, top_k)
    return _text_search(query, top_k)


def _vector_search(query: str, embed_fn: EmbedFn, top_k: int) -> list[EpisodicEntry]:
    import sqlite_vec
    embedding = embed_fn(query)
    conn = get_conn()

    # sqlite-vec vec0 requires a pure KNN query (no JOINs with MATCH).
    # Step 1: get matching rowids from the vector table alone.
    knn_rows = conn.execute(
        "SELECT rowid FROM episodic_vec "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (sqlite_vec.serialize_float32(embedding), int(top_k))
    ).fetchall()
    if not knn_rows:
        return []
    # Step 2: fetch the actual entries by rowid.
    rowids = [r[0] for r in knn_rows]
    placeholders = ",".join("?" * len(rowids))
    rows = conn.execute(
        f"SELECT id, content, timestamp, entry_type, metadata "
        f"FROM episodic_entries WHERE rowid IN ({placeholders})",
        rowids
    ).fetchall()

    return _rows_to_entries(rows)


def _text_search(query: str, top_k: int) -> list[EpisodicEntry]:
    conn = get_conn()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT id, content, timestamp, entry_type, metadata "
        "FROM episodic_entries "
        "WHERE content LIKE ? ESCAPE '\\' "
        "ORDER BY timestamp DESC LIMIT ?",
        (f"%{escaped}%", top_k)
    ).fetchall()
    return _rows_to_entries(rows)


def search_non_turns(
    query: str,
    embed_fn: EmbedFn | None = None,
    top_k: int = EPISODIC_TOP_K,
) -> list[EpisodicEntry]:
    """
    Like search(), but only returns summaries and milestone entries.
    Raw 'turn' entries are excluded — they are temporary staging; only archives are injected.
    """
    if embed_fn and sqlite_vec_available():
        import sqlite_vec
        embedding = embed_fn(query)
        conn = get_conn()
        # Step 1: pure KNN query — no extra WHERE conditions, only MATCH + LIMIT
        # (sqlite-vec fails if any non-vec condition is mixed into the WHERE clause)
        knn_rows = conn.execute(
            "SELECT rowid FROM episodic_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(embedding), int(top_k * 4))
        ).fetchall()
        if not knn_rows:
            return []
        # Step 2: fetch entries and filter entry_type separately
        rowids = [r[0] for r in knn_rows]
        placeholders = ",".join("?" * len(rowids))
        rows = conn.execute(
            f"SELECT id, content, timestamp, entry_type, metadata "
            f"FROM episodic_entries "
            f"WHERE rowid IN ({placeholders}) AND entry_type != 'turn' "
            f"LIMIT ?",
            (*rowids, top_k)
        ).fetchall()
        return _rows_to_entries(rows)

    # Text fallback — exclude raw turns
    conn = get_conn()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT id, content, timestamp, entry_type, metadata "
        "FROM episodic_entries "
        "WHERE entry_type != 'turn' AND content LIKE ? ESCAPE '\\' "
        "ORDER BY timestamp DESC LIMIT ?",
        (f"%{escaped}%", top_k)
    ).fetchall()
    return _rows_to_entries(rows)


def recent(limit: int = 5) -> list[EpisodicEntry]:
    """Fetch the most recent entries regardless of query."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, content, timestamp, entry_type, metadata "
        "FROM episodic_entries ORDER BY timestamp DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return list(reversed(_rows_to_entries(rows)))


def get_pending_turns_text() -> str:
    """
    Return all raw 'turn' entries concatenated as a single transcript string.
    Call this BEFORE delete_turns() to capture the full text.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT content FROM episodic_entries "
        "WHERE entry_type = 'turn' ORDER BY timestamp ASC"
    ).fetchall()
    return "\n\n".join(r[0] for r in rows)


def save_transcript(archive_id: str, content: str) -> None:
    """Save the full verbatim transcript linked to an archive entry."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO episodic_transcripts (archive_id, content, timestamp) VALUES (?, ?, ?)",
        (archive_id, content, datetime.now().isoformat())
    )
    conn.commit()


def get_transcript(archive_id: str) -> str | None:
    """Retrieve the full transcript for a given archive entry ID. Returns None if not found."""
    conn = get_conn()
    row = conn.execute(
        "SELECT content FROM episodic_transcripts WHERE archive_id = ?",
        (archive_id,)
    ).fetchone()
    return row[0] if row else None


def delete_turns() -> None:
    """
    Delete all raw 'turn' entries from episodic_entries AND their vectors.
    Called after Brain compresses history into an archive — turns have been captured
    in the summary so removing them keeps the DB lean.
    """
    conn = get_conn()

    # Collect rowids BEFORE deleting entries — needed to clean up episodic_vec.
    turn_rowids = [
        r[0] for r in conn.execute(
            "SELECT rowid FROM episodic_entries WHERE entry_type = 'turn'"
        ).fetchall()
    ]

    conn.execute("DELETE FROM episodic_entries WHERE entry_type = 'turn'")

    # Remove orphaned vectors (same pattern as documents.py:delete_document)
    if turn_rowids and sqlite_vec_available():
        try:
            placeholders = ",".join("?" * len(turn_rowids))
            conn.execute(
                f"DELETE FROM episodic_vec WHERE rowid IN ({placeholders})",
                turn_rowids,
            )
        except Exception:
            pass  # best-effort — text entries are already gone

    conn.commit()


def _rows_to_entries(rows: list) -> list[EpisodicEntry]:
    return [
        EpisodicEntry(
            id=row[0],
            content=row[1],
            timestamp=datetime.fromisoformat(row[2]),
            entry_type=row[3],
            metadata=json.loads(row[4]),
        )
        for row in rows
    ]
