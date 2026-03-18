"""
Campaign tools — Kai uses these during DM (Dungeon Master) sessions.

These tools let Kai auto-track NPCs, quests, and story events while narrating.
They should be called proactively without the user asking:
  - Introduce a new NPC → call campaign.npc_save
  - Something story-significant happens → call campaign.event_log
  - A quest begins or ends → call campaign.quest_update

campaign.recall and campaign.status are used to answer player questions
like "who is the blacksmith again?" or "what quests do we have?"
"""
from kai.tools.registry import registry
from kai import campaign as _camp


def _get_embed_fn():
    """Lazy-import the embed function (set at startup in _app_state)."""
    try:
        from kai._app_state import get_embed_fn
        return get_embed_fn()
    except Exception:
        return None


# ── Tool: save / update an NPC ─────────────────────────────────────────────────

@registry.tool(
    name="campaign.npc_save",
    description=(
        "Save or update an NPC in the current campaign. "
        "Call this automatically whenever you introduce or describe a named NPC: "
        "a shopkeeper, guard captain, villain, quest giver, or any named character. "
        "Use status='alive' (default), 'dead', 'missing', or 'unknown'. "
        "Re-call with updated info if you learn more about them later."
    ),
    parameters={
        "name":        {"type": "string",  "description": "NPC's full name", "required": True},
        "role":        {"type": "string",  "description": "Their role or title (e.g. blacksmith, bandit lord, town mayor)", "required": True},
        "description": {"type": "string",  "description": "Brief description: appearance, personality, motivation"},
        "status":      {"type": "string",  "description": "Current status: alive, dead, missing, unknown (default: alive)"},
    },
)
def campaign_npc_save(name: str, role: str, description: str = "", status: str = "alive") -> dict:
    camp = _camp.get_active_campaign()
    if not camp:
        return {"success": False, "error": "No active campaign. Start one with campaign.status first."}
    npc_id = _camp.upsert_npc(
        campaign_id=camp["id"],
        name=name,
        role=role,
        description=description,
        status=status,
        embed_fn=_get_embed_fn(),
    )
    return {"success": True, "npc_id": npc_id, "name": name, "role": role, "status": status}


# ── Tool: log a story event ────────────────────────────────────────────────────

@registry.tool(
    name="campaign.event_log",
    description=(
        "Log a significant story event in the current campaign. "
        "Call this for major beats: combat outcomes, discoveries, deals made, "
        "betrayals, deaths, important decisions, or anything the players would "
        "want to recall later. Keep entries concise (1-2 sentences). "
        "Log proactively — don't wait for the user to ask."
    ),
    parameters={
        "content": {
            "type": "string",
            "description": "What happened (1-2 sentences, specific and factual)",
            "required": True,
        },
    },
)
def campaign_event_log(content: str) -> dict:
    camp = _camp.get_active_campaign()
    if not camp:
        return {"success": False, "error": "No active campaign."}
    event_id = _camp.log_event(camp["id"], content, embed_fn=_get_embed_fn())
    return {"success": True, "event_id": event_id, "logged": content}


# ── Tool: create / update a quest ──────────────────────────────────────────────

@registry.tool(
    name="campaign.quest_update",
    description=(
        "Create or update a quest in the current campaign. "
        "Use status='active' for ongoing quests, 'completed' when finished, "
        "'failed' if the party failed, 'abandoned' if dropped. "
        "Call this when a quest starts, when its goal changes, or when it ends."
    ),
    parameters={
        "name":        {"type": "string", "description": "Quest name (short, memorable)", "required": True},
        "description": {"type": "string", "description": "Goal or current objective"},
        "status":      {"type": "string", "description": "active, completed, failed, or abandoned"},
    },
)
def campaign_quest_update(name: str, description: str = "", status: str = "active") -> dict:
    camp = _camp.get_active_campaign()
    if not camp:
        return {"success": False, "error": "No active campaign."}
    quest_id = _camp.upsert_quest(camp["id"], name, description, status)
    return {"success": True, "quest_id": quest_id, "name": name, "status": status}


# ── Tool: recall story context ─────────────────────────────────────────────────

@registry.tool(
    name="campaign.recall",
    description=(
        "Search the campaign's memory for NPCs, events, or quests matching a query. "
        "Use this when the player asks 'who is X?', 'what happened with Y?', "
        "'do we have any quests about Z?', or any question about past story content. "
        "Returns matching NPCs, events, and quests."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "What to search for (NPC name, place, topic, event)",
            "required": True,
        },
    },
)
def campaign_recall(query: str) -> dict:
    camp = _camp.get_active_campaign()
    if not camp:
        return {"success": False, "error": "No active campaign."}
    cid      = camp["id"]
    embed_fn = _get_embed_fn()

    npcs   = _camp.search_npcs(cid, query, embed_fn=embed_fn, top_k=5)
    events = _camp.search_events(cid, query, embed_fn=embed_fn, top_k=5)

    # Quest text search (no vectors for quests — they're few and short)
    q_lower    = query.lower()
    all_quests = _camp.list_quests(cid)
    quests = [
        q for q in all_quests
        if q_lower in q["name"].lower() or q_lower in q["description"].lower()
    ][:5]

    return {
        "success":  True,
        "campaign": camp["name"],
        "npcs":     npcs,
        "events":   [{"timestamp": e["timestamp"][:16], "content": e["content"]} for e in events],
        "quests":   quests,
    }


# ── Tool: campaign status overview ────────────────────────────────────────────

@registry.tool(
    name="campaign.status",
    description=(
        "Show a full overview of the current campaign: name, all NPCs, "
        "active quests, and the 10 most recent events. "
        "Use this at the start of a session to recap, or when the player asks "
        "for a summary of where things stand."
    ),
    parameters={},
)
def campaign_status() -> dict:
    camp = _camp.get_active_campaign()
    if not camp:
        campaigns = _camp.list_campaigns()
        return {
            "success": False,
            "error": "No active campaign.",
            "available_campaigns": [c["name"] for c in campaigns],
        }
    cid = camp["id"]
    return {
        "success":       True,
        "campaign":      camp["name"],
        "npcs":          _camp.list_npcs(cid),
        "quests":        _camp.list_quests(cid),
        "recent_events": [
            {"timestamp": e["timestamp"][:16], "content": e["content"]}
            for e in _camp.recent_events(cid, limit=10)
        ],
    }
