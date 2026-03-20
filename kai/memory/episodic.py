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
    user_id: int = 0,
) -> str:
    """Store an episodic entry. Returns the entry ID."""
    entry_id = str(uuid.uuid4())
    ts = datetime.now().isoformat()
    meta_json = json.dumps(metadata or {})

    conn = get_conn()
    conn.execute(
        "INSERT INTO episodic_entries (id, user_id, content, timestamp, entry_type, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (entry_id, user_id, content, ts, entry_type, meta_json)
    )
    conn.commit()
    rowid = conn.execute(
        "SELECT rowid FROM episodic_entries WHERE id = ?", (entry_id,)
    ).fetchone()[0]

    # Embedding is best-effort — a failure here never loses the text entry above.
    # Skip embedding for raw turns — they are temporary staging deleted after
    # compression. Embedding them wastes an Ollama round-trip and adds queue
    # pressure that delays the next user turn.
    if embed_fn and sqlite_vec_available() and entry_type != "turn":
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
    query_embedding: list[float] | None = None,
    user_id: int = 0,
) -> list[EpisodicEntry]:
    """
    Search episodic memory. Uses vector similarity if available,
    falls back to substring search.
    """
    if (query_embedding or embed_fn) and sqlite_vec_available():
        return _vector_search(query, embed_fn, top_k, query_embedding, user_id)
    return _text_search(query, top_k, user_id)


def _vector_search(
    query: str, embed_fn: EmbedFn, top_k: int,
    query_embedding: list[float] | None = None,
    user_id: int = 0,
) -> list[EpisodicEntry]:
    import sqlite_vec
    embedding = query_embedding or embed_fn(query)
    conn = get_conn()

    # sqlite-vec vec0 requires a pure KNN query (no JOINs with MATCH).
    # Step 1: get matching rowids from the vector table alone.
    knn_rows = conn.execute(
        "SELECT rowid FROM episodic_vec "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (sqlite_vec.serialize_float32(embedding), int(top_k * 2))
    ).fetchall()
    if not knn_rows:
        return []
    # Step 2: fetch the actual entries by rowid, filtered by user_id.
    # Preserve KNN distance ordering via the rowid list order.
    rowids = [r[0] for r in knn_rows]
    placeholders = ",".join("?" * len(rowids))
    rows = conn.execute(
        f"SELECT id, content, timestamp, entry_type, metadata "
        f"FROM episodic_entries WHERE rowid IN ({placeholders}) AND user_id = ? "
        f"LIMIT ?",
        (*rowids, user_id, top_k)
    ).fetchall()

    # Re-sort to match KNN distance order (IN clause returns arbitrary order)
    entries = _rows_to_entries(rows)
    row_id_by_entry_id = {}
    for r in conn.execute(
        f"SELECT id, rowid FROM episodic_entries WHERE rowid IN ({placeholders})",
        rowids,
    ).fetchall():
        row_id_by_entry_id[r[0]] = r[1]
    rowid_rank = {rid: i for i, rid in enumerate(rowids)}
    entries.sort(key=lambda e: rowid_rank.get(row_id_by_entry_id.get(e.id, -1), 999))
    return entries


def _text_search(query: str, top_k: int, user_id: int = 0) -> list[EpisodicEntry]:
    conn = get_conn()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT id, content, timestamp, entry_type, metadata "
        "FROM episodic_entries "
        "WHERE user_id = ? AND content LIKE ? ESCAPE '\\' "
        "ORDER BY timestamp DESC LIMIT ?",
        (user_id, f"%{escaped}%", top_k)
    ).fetchall()
    return _rows_to_entries(rows)


def search_non_turns(
    query: str,
    embed_fn: EmbedFn | None = None,
    top_k: int = EPISODIC_TOP_K,
    query_embedding: list[float] | None = None,
    user_id: int = 0,
) -> list[EpisodicEntry]:
    """
    Like search(), but only returns summaries and milestone entries.
    Raw 'turn' entries are excluded — they are temporary staging; only archives are injected.
    """
    if (query_embedding or embed_fn) and sqlite_vec_available():
        import sqlite_vec
        embedding = query_embedding or embed_fn(query)
        conn = get_conn()
        # Step 1: pure KNN query — no extra WHERE conditions, only MATCH + LIMIT
        # Turns are no longer embedded (skipped in add_entry), so the vec table
        # contains only archives/learned entries — no need to over-fetch.
        knn_rows = conn.execute(
            "SELECT rowid FROM episodic_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(embedding), int(top_k))
        ).fetchall()
        if not knn_rows:
            return []
        # Step 2: fetch entries and filter by user_id + entry_type
        rowids = [r[0] for r in knn_rows]
        placeholders = ",".join("?" * len(rowids))
        rows = conn.execute(
            f"SELECT id, content, timestamp, entry_type, metadata "
            f"FROM episodic_entries "
            f"WHERE rowid IN ({placeholders}) AND user_id = ? AND entry_type != 'turn' "
            f"LIMIT ?",
            (*rowids, user_id, top_k)
        ).fetchall()

        # Re-sort to match KNN distance order (IN clause returns arbitrary order)
        entries = _rows_to_entries(rows)
        row_id_by_entry_id = {}
        for r in conn.execute(
            f"SELECT id, rowid FROM episodic_entries WHERE rowid IN ({placeholders})",
            rowids,
        ).fetchall():
            row_id_by_entry_id[r[0]] = r[1]
        rowid_rank = {rid: i for i, rid in enumerate(rowids)}
        entries.sort(key=lambda e: rowid_rank.get(row_id_by_entry_id.get(e.id, -1), 999))
        return entries

    # Text fallback — exclude raw turns
    conn = get_conn()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT id, content, timestamp, entry_type, metadata "
        "FROM episodic_entries "
        "WHERE user_id = ? AND entry_type != 'turn' AND content LIKE ? ESCAPE '\\' "
        "ORDER BY timestamp DESC LIMIT ?",
        (user_id, f"%{escaped}%", top_k)
    ).fetchall()
    return _rows_to_entries(rows)


def recent(limit: int = 5, user_id: int = 0) -> list[EpisodicEntry]:
    """Fetch the most recent entries regardless of query."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, content, timestamp, entry_type, metadata "
        "FROM episodic_entries WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    return list(reversed(_rows_to_entries(rows)))


def get_pending_turns_text(user_id: int = 0) -> str:
    """
    Return all raw 'turn' entries concatenated as a single transcript string.
    Call this BEFORE delete_turns() to capture the full text.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT content FROM episodic_entries "
        "WHERE user_id = ? AND entry_type = 'turn' ORDER BY timestamp ASC",
        (user_id,)
    ).fetchall()
    return "\n\n".join(r[0] for r in rows)


def save_transcript(archive_id: str, content: str, user_id: int = 0) -> None:
    """Save the full verbatim transcript linked to an archive entry."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO episodic_transcripts (archive_id, user_id, content, timestamp) "
        "VALUES (?, ?, ?, ?)",
        (archive_id, user_id, content, datetime.now().isoformat())
    )
    conn.commit()


def get_transcript(archive_id: str, user_id: int = 0) -> str | None:
    """Retrieve the full transcript for a given archive entry ID. Returns None if not found."""
    conn = get_conn()
    row = conn.execute(
        "SELECT content FROM episodic_transcripts WHERE archive_id = ? AND user_id = ?",
        (archive_id, user_id)
    ).fetchone()
    return row[0] if row else None


def delete_turns(user_id: int = 0) -> None:
    """
    Delete all raw 'turn' entries from episodic_entries AND their vectors.
    Called after Brain compresses history into an archive — turns have been captured
    in the summary so removing them keeps the DB lean.
    """
    conn = get_conn()

    # Collect rowids BEFORE deleting entries — needed to clean up episodic_vec.
    turn_rowids = [
        r[0] for r in conn.execute(
            "SELECT rowid FROM episodic_entries WHERE user_id = ? AND entry_type = 'turn'",
            (user_id,)
        ).fetchall()
    ]

    conn.execute(
        "DELETE FROM episodic_entries WHERE user_id = ? AND entry_type = 'turn'",
        (user_id,)
    )

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
