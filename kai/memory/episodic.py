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
from kai.store.db import (
    get_conn, sqlite_vec_available, like_escape, vec_knn, resort_by_rowid_order,
)
from kai.store.schema import EpisodicEntry

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
    embedding = query_embedding or embed_fn(query)
    conn = get_conn()

    # Step 1: pure vec0 KNN — over-fetch since the user_id filter comes later.
    rowids = [r[0] for r in vec_knn(conn, "episodic_vec", embedding, top_k * 2)]
    if not rowids:
        return []
    # Step 2: fetch the actual entries by rowid, filtered by user_id.
    placeholders = ",".join("?" * len(rowids))
    rows = conn.execute(
        f"SELECT id, content, timestamp, entry_type, metadata "
        f"FROM episodic_entries WHERE rowid IN ({placeholders}) AND user_id = ? "
        f"LIMIT ?",
        (*rowids, user_id, top_k)
    ).fetchall()

    # Step 3: IN-clause order is arbitrary — restore KNN distance order.
    entries = _rows_to_entries(rows)
    return resort_by_rowid_order(conn, "episodic_entries", entries, rowids)


def _text_search(query: str, top_k: int, user_id: int = 0) -> list[EpisodicEntry]:
    conn = get_conn()
    escaped = like_escape(query)
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
        embedding = query_embedding or embed_fn(query)
        conn = get_conn()
        # Step 1: pure KNN. Turns are no longer embedded (skipped in add_entry),
        # so the vec table holds only archives/learned entries — no over-fetch.
        rowids = [r[0] for r in vec_knn(conn, "episodic_vec", embedding, top_k)]
        if not rowids:
            return []
        # Step 2: fetch entries and filter by user_id + entry_type
        placeholders = ",".join("?" * len(rowids))
        rows = conn.execute(
            f"SELECT id, content, timestamp, entry_type, metadata "
            f"FROM episodic_entries "
            f"WHERE rowid IN ({placeholders}) AND user_id = ? AND entry_type != 'turn' "
            f"LIMIT ?",
            (*rowids, user_id, top_k)
        ).fetchall()

        # Step 3: IN-clause order is arbitrary — restore KNN distance order.
        entries = _rows_to_entries(rows)
        return resort_by_rowid_order(conn, "episodic_entries", entries, rowids)

    # Text fallback — exclude raw turns
    conn = get_conn()
    escaped = like_escape(query)
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


def archive_and_clear_turns(
    summary_text: str,
    embed_fn: EmbedFn | None = None,
    user_id: int = 0,
) -> str:
    """Atomically archive pending raw turns into a summary entry and delete them.

    Replaces the old three-call dance (add_entry → save_transcript → delete_turns),
    each of which committed separately. Those three text writes now happen in a
    single transaction, so a crash can never leave the raw turns *and* their archive
    both present, or strand a transcript without its turns. Returns the archive's
    entry ID.

    The summary embedding is best-effort and runs *after* the durable commit — a
    failure there never undoes the archive (same contract as add_entry).
    """
    conn = get_conn()
    entry_id = str(uuid.uuid4())
    ts = datetime.now().isoformat()

    # Capture the verbatim transcript and the turn rowids BEFORE anything is
    # deleted — both are needed inside the transaction below.
    transcript = get_pending_turns_text(user_id=user_id)
    turn_rowids = [
        r[0] for r in conn.execute(
            "SELECT rowid FROM episodic_entries WHERE user_id = ? AND entry_type = 'turn'",
            (user_id,)
        ).fetchall()
    ]

    try:
        conn.execute(
            "INSERT INTO episodic_entries (id, user_id, content, timestamp, entry_type, metadata) "
            "VALUES (?, ?, ?, ?, 'archive', '{}')",
            (entry_id, user_id, summary_text, ts),
        )
        if transcript:
            conn.execute(
                "INSERT INTO episodic_transcripts (archive_id, user_id, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (entry_id, user_id, transcript, ts),
            )
        conn.execute(
            "DELETE FROM episodic_entries WHERE user_id = ? AND entry_type = 'turn'",
            (user_id,)
        )
        if turn_rowids and sqlite_vec_available():
            try:
                placeholders = ",".join("?" * len(turn_rowids))
                conn.execute(
                    f"DELETE FROM episodic_vec WHERE rowid IN ({placeholders})",
                    turn_rowids,
                )
            except Exception:
                pass  # best-effort vec cleanup — text rows still go atomically
        conn.commit()
    except Exception:
        conn.rollback()  # leave the turns intact rather than half-archive them
        raise

    # Embed the summary so it's retrievable by similarity — best-effort, post-commit.
    if embed_fn and sqlite_vec_available():
        try:
            import sqlite_vec
            rowid = conn.execute(
                "SELECT rowid FROM episodic_entries WHERE id = ?", (entry_id,)
            ).fetchone()[0]
            embedding = embed_fn(summary_text)
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
    entries = []
    for row in rows:
        try:
            metadata = json.loads(row[4]) if row[4] else {}
        except Exception:
            metadata = {}  # corrupt metadata blob — don't let one bad row sink the read
        entries.append(EpisodicEntry(
            id=row[0],
            content=row[1],
            timestamp=datetime.fromisoformat(row[2]),
            entry_type=row[3],
            metadata=metadata,
        ))
    return entries
