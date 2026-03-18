"""
Campaign memory — tracks D&D campaigns, NPCs, quests, and events.

Separate from personal memory so NPC names and story details don't pollute
semantic facts. Uses sqlite-vec for NPC and event similarity search,
falling back to substring search if sqlite-vec is unavailable.
"""
import json
import uuid
from datetime import datetime
from typing import Callable

from kai.config import DEBUG
from kai.db import get_conn, sqlite_vec_available

EmbedFn = Callable[[str], list[float]]


# ── Campaign management ────────────────────────────────────────────────────────

def get_active_campaign() -> dict | None:
    """Return the currently active campaign dict, or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, name, created_at, last_active "
        "FROM campaigns WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "created_at": row[2], "last_active": row[3]}


def create_campaign(name: str) -> str:
    """Create a new campaign, deactivate any current one, return new ID."""
    campaign_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = get_conn()
    conn.execute("UPDATE campaigns SET is_active = 0")
    conn.execute(
        "INSERT INTO campaigns (id, name, is_active, created_at, last_active) "
        "VALUES (?, ?, 1, ?, ?)",
        (campaign_id, name, now, now),
    )
    conn.commit()
    return campaign_id


def set_active_campaign(campaign_id: str) -> bool:
    """Switch active campaign by ID. Returns True if found."""
    conn = get_conn()
    if not conn.execute(
        "SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone():
        return False
    conn.execute("UPDATE campaigns SET is_active = 0")
    conn.execute(
        "UPDATE campaigns SET is_active = 1, last_active = ? WHERE id = ?",
        (datetime.now().isoformat(), campaign_id),
    )
    conn.commit()
    return True


def list_campaigns() -> list[dict]:
    """Return all campaigns; active one first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, is_active, created_at, last_active "
        "FROM campaigns ORDER BY is_active DESC, last_active DESC"
    ).fetchall()
    return [
        {"id": r[0], "name": r[1], "is_active": bool(r[2]),
         "created_at": r[3], "last_active": r[4]}
        for r in rows
    ]


def end_campaign() -> None:
    """Deactivate the current campaign (exit DM mode)."""
    conn = get_conn()
    conn.execute("UPDATE campaigns SET is_active = 0")
    conn.commit()


# ── NPC management ─────────────────────────────────────────────────────────────

def upsert_npc(
    campaign_id: str,
    name: str,
    role: str = "",
    description: str = "",
    status: str = "alive",
    embed_fn: EmbedFn | None = None,
) -> str:
    """Create or update an NPC (matched by campaign + name). Returns NPC ID."""
    now = datetime.now().isoformat()
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM campaign_npcs "
        "WHERE campaign_id = ? AND LOWER(name) = LOWER(?)",
        (campaign_id, name),
    ).fetchone()

    if existing:
        npc_id = existing[0]
        conn.execute(
            "UPDATE campaign_npcs "
            "SET role=?, description=?, status=?, updated_at=? WHERE id=?",
            (role, description, status, now, npc_id),
        )
    else:
        npc_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO campaign_npcs "
            "(id, campaign_id, name, role, description, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (npc_id, campaign_id, name, role, description, status, now),
        )

    if embed_fn and sqlite_vec_available():
        try:
            import sqlite_vec
            embed_text = f"{name} ({role}): {description}"
            embedding = embed_fn(embed_text)
            rowid = conn.execute(
                "SELECT rowid FROM campaign_npcs WHERE id = ?", (npc_id,)
            ).fetchone()[0]
            conn.execute("DELETE FROM campaign_npc_vec WHERE rowid = ?", (rowid,))
            conn.execute(
                "INSERT INTO campaign_npc_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, sqlite_vec.serialize_float32(embedding)),
            )
        except Exception:
            if DEBUG:
                import traceback; traceback.print_exc()

    conn.commit()
    return npc_id


def search_npcs(
    campaign_id: str,
    query: str,
    embed_fn: EmbedFn | None = None,
    top_k: int = 5,
) -> list[dict]:
    """Search NPCs by semantic similarity, filtered to this campaign."""
    if embed_fn and sqlite_vec_available():
        import sqlite_vec
        embedding = embed_fn(query)
        conn = get_conn()
        knn_rows = conn.execute(
            "SELECT rowid FROM campaign_npc_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(embedding), int(top_k * 4)),
        ).fetchall()
        if not knn_rows:
            return []
        rowids = [r[0] for r in knn_rows]
        placeholders = ",".join("?" * len(rowids))
        rows = conn.execute(
            f"SELECT id, name, role, description, status, updated_at "
            f"FROM campaign_npcs "
            f"WHERE rowid IN ({placeholders}) AND campaign_id = ? "
            f"LIMIT ?",
            (*rowids, campaign_id, top_k),
        ).fetchall()
        return _npc_rows_to_dicts(rows)

    # Text fallback
    conn = get_conn()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT id, name, role, description, status, updated_at "
        "FROM campaign_npcs "
        "WHERE campaign_id = ? "
        "AND (name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR role LIKE ? ESCAPE '\\') "
        "ORDER BY updated_at DESC LIMIT ?",
        (campaign_id, f"%{escaped}%", f"%{escaped}%", f"%{escaped}%", top_k),
    ).fetchall()
    return _npc_rows_to_dicts(rows)


def list_npcs(campaign_id: str, status: str | None = None) -> list[dict]:
    """List all NPCs for a campaign, optionally filtered by status."""
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT id, name, role, description, status, updated_at "
            "FROM campaign_npcs WHERE campaign_id = ? AND status = ? ORDER BY name",
            (campaign_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, role, description, status, updated_at "
            "FROM campaign_npcs WHERE campaign_id = ? ORDER BY name",
            (campaign_id,),
        ).fetchall()
    return _npc_rows_to_dicts(rows)


def _npc_rows_to_dicts(rows: list) -> list[dict]:
    return [
        {"id": r[0], "name": r[1], "role": r[2],
         "description": r[3], "status": r[4], "updated_at": r[5]}
        for r in rows
    ]


# ── Quest management ───────────────────────────────────────────────────────────

def upsert_quest(
    campaign_id: str,
    name: str,
    description: str = "",
    status: str = "active",
) -> str:
    """Create or update a quest (matched by campaign + name). Returns quest ID."""
    now = datetime.now().isoformat()
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM campaign_quests "
        "WHERE campaign_id = ? AND LOWER(name) = LOWER(?)",
        (campaign_id, name),
    ).fetchone()
    if existing:
        quest_id = existing[0]
        conn.execute(
            "UPDATE campaign_quests SET description=?, status=?, updated_at=? WHERE id=?",
            (description, status, now, quest_id),
        )
    else:
        quest_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO campaign_quests "
            "(id, campaign_id, name, description, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (quest_id, campaign_id, name, description, status, now),
        )
    conn.commit()
    return quest_id


def list_quests(campaign_id: str, status: str | None = None) -> list[dict]:
    """List quests for a campaign, optionally filtered by status."""
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT id, name, description, status, updated_at "
            "FROM campaign_quests WHERE campaign_id = ? AND status = ? "
            "ORDER BY updated_at DESC",
            (campaign_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, description, status, updated_at "
            "FROM campaign_quests WHERE campaign_id = ? ORDER BY status, name",
            (campaign_id,),
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "description": r[2],
         "status": r[3], "updated_at": r[4]}
        for r in rows
    ]


# ── Event log ──────────────────────────────────────────────────────────────────

def log_event(
    campaign_id: str,
    content: str,
    embed_fn: EmbedFn | None = None,
    metadata: dict | None = None,
) -> str:
    """Log a story event (beat, decision, outcome). Returns event ID."""
    event_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    meta_json = json.dumps(metadata or {})

    conn = get_conn()
    conn.execute(
        "INSERT INTO campaign_events (id, campaign_id, content, timestamp, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, campaign_id, content, now, meta_json),
    )
    if embed_fn and sqlite_vec_available():
        try:
            import sqlite_vec
            embedding = embed_fn(content)
            rowid = conn.execute(
                "SELECT rowid FROM campaign_events WHERE id = ?", (event_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO campaign_event_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, sqlite_vec.serialize_float32(embedding)),
            )
        except Exception:
            if DEBUG:
                import traceback; traceback.print_exc()
    conn.commit()
    return event_id


def search_events(
    campaign_id: str,
    query: str,
    embed_fn: EmbedFn | None = None,
    top_k: int = 5,
) -> list[dict]:
    """Search campaign events by semantic similarity."""
    if embed_fn and sqlite_vec_available():
        import sqlite_vec
        embedding = embed_fn(query)
        conn = get_conn()
        knn_rows = conn.execute(
            "SELECT rowid FROM campaign_event_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(embedding), int(top_k * 4)),
        ).fetchall()
        if not knn_rows:
            return []
        rowids = [r[0] for r in knn_rows]
        placeholders = ",".join("?" * len(rowids))
        rows = conn.execute(
            f"SELECT id, content, timestamp, metadata "
            f"FROM campaign_events "
            f"WHERE rowid IN ({placeholders}) AND campaign_id = ? "
            f"ORDER BY timestamp DESC LIMIT ?",
            (*rowids, campaign_id, top_k),
        ).fetchall()
        return _event_rows_to_dicts(rows)

    # Text fallback
    conn = get_conn()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT id, content, timestamp, metadata "
        "FROM campaign_events WHERE campaign_id = ? AND content LIKE ? ESCAPE '\\' "
        "ORDER BY timestamp DESC LIMIT ?",
        (campaign_id, f"%{escaped}%", top_k),
    ).fetchall()
    return _event_rows_to_dicts(rows)


def recent_events(campaign_id: str, limit: int = 10) -> list[dict]:
    """Fetch the most recent events for a campaign (chronological order)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, content, timestamp, metadata "
        "FROM campaign_events WHERE campaign_id = ? "
        "ORDER BY timestamp DESC LIMIT ?",
        (campaign_id, limit),
    ).fetchall()
    return list(reversed(_event_rows_to_dicts(rows)))


def _event_rows_to_dicts(rows: list) -> list[dict]:
    return [
        {"id": r[0], "content": r[1], "timestamp": r[2],
         "metadata": json.loads(r[3])}
        for r in rows
    ]


# ── Context builder ────────────────────────────────────────────────────────────

def build_campaign_context(
    campaign_id: str,
    query: str = "",
    embed_fn: EmbedFn | None = None,
    npc_top_k: int = 5,
    event_top_k: int = 5,
) -> str:
    """
    Build the [CAMPAIGN] context block injected into the system prompt.
    Pulls only the most relevant NPCs and events via vector search.
    Returns empty string if campaign not found.
    """
    campaign = get_active_campaign()
    if not campaign or campaign["id"] != campaign_id:
        return ""

    lines = [
        f"Campaign: {campaign['name']}",
        # DM behavioral instructions — front-loaded so they weight heavily
        "You are the Dungeon Master. Narrate in second person ('you see...', 'ahead lies...'). "
        "Give each NPC a distinct voice when speaking. Describe scenes with atmosphere and tension. "
        "Proactively call campaign.npc_save when introducing any named character. "
        "Call campaign.event_log after significant story beats without being asked. "
        "Call campaign.quest_update when quests begin, change, or end.",
    ]

    # Active quests — always show (short + always relevant)
    quests = list_quests(campaign_id, status="active")
    if quests:
        q_lines = []
        for q in quests:
            q_lines.append(
                f"- {q['name']}: {q['description']}" if q["description"]
                else f"- {q['name']}"
            )
        lines.append("Active quests:\n" + "\n".join(q_lines))

    # NPCs — vector search if enough exist, else list all
    all_npcs = list_npcs(campaign_id)
    if not all_npcs:
        relevant_npcs = []
    elif len(all_npcs) <= npc_top_k:
        relevant_npcs = all_npcs
    elif query:
        relevant_npcs = search_npcs(campaign_id, query, embed_fn=embed_fn, top_k=npc_top_k)
    else:
        relevant_npcs = all_npcs[:npc_top_k]

    if relevant_npcs:
        npc_lines = []
        for npc in relevant_npcs:
            line = f"- {npc['name']} ({npc['role']}, {npc['status']})"
            if npc["description"]:
                line += f": {npc['description']}"
            npc_lines.append(line)
        lines.append("NPCs:\n" + "\n".join(npc_lines))

    # Events — recent chronological + semantic search merged
    recent = recent_events(campaign_id, limit=event_top_k)
    if query and all_npcs:
        semantic = search_events(campaign_id, query, embed_fn=embed_fn, top_k=event_top_k)
        seen = {e["id"] for e in recent}
        extra = [e for e in semantic if e["id"] not in seen]
        all_ev = recent + extra
    else:
        all_ev = recent

    if all_ev:
        ev_lines = [
            f"- [{e['timestamp'][:16]}] {e['content']}"
            for e in all_ev[-event_top_k:]
        ]
        lines.append("Recent events:\n" + "\n".join(ev_lines))

    return "\n\n".join(lines)
