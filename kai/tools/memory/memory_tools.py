"""
memory_tools.py — tools for Kai to access her own memory and self-reflection.
"""

from datetime import datetime

from kai.config import REFLECTIONS_PATH
from kai.core._app_state import get_current_session_id, get_current_user_id
from kai.memory import episodic
from kai.store import sessions as _sessions
from kai.tools.registry import registry


@registry.tool(
    name="memory.get_detail",
    description=(
        "Retrieve the full verbatim conversation transcript behind a memory archive. "
        "Use this when a summary entry from episodic search lacks enough detail to "
        "answer the user's question precisely — names, exact figures, specific steps said. "
        "Pass the archive entry ID returned from a previous memory search result. "
        "Returns the raw turn-by-turn transcript. "
        "If no transcript is found, say so and work from the summary."
    ),
    parameters={
        "archive_id": {
            "type": "string",
            "description": "The episodic entry ID of the archive to retrieve the full transcript for.",
            "required": True,
        }
    },
)
def get_detail(archive_id: str) -> str:
    try:
        user_id = get_current_user_id()
        transcript = episodic.get_transcript(archive_id, user_id=user_id)
        if not transcript:
            return f"No full transcript found for archive ID: {archive_id}"
        return transcript
    except Exception as e:
        return f"Error retrieving transcript: {e}"


@registry.tool(
    name="memory.recent_sessions",
    description=(
        "Recall your most recent PAST conversations by recency — what you and the "
        "user were doing last, before this session. Use this whenever the user asks "
        "'what were we doing last?', 'what did we work on before?', 'before your last "
        "reset', or wants to pick up where you left off — anything about a previous "
        "session WITHOUT a specific keyword to search for. (For a keyword search "
        "across all history, use memory.search_history instead.) Never say you can't "
        "recall past sessions — call this first. Returns each recent session with its "
        "date, opening topic, and where it left off."
    ),
    parameters={
        "limit": {
            "type": "integer",
            "description": "How many recent past sessions to return (default 3, max 10).",
            "required": False,
        },
    },
)
def recent_sessions(limit: int = 3) -> str:
    try:
        limit = min(max(1, int(limit)), 10)
        user_id = get_current_user_id()
        current = get_current_session_id()

        # The note Kai wrote itself last session, retained for the whole session
        # so it's recallable past turn 1 (not just in the cold-open greeting).
        header: list[str] = []
        try:
            from kai.memory.context import get_session_welcome_back

            note = get_session_welcome_back().strip()
            if note:
                header.append(f'Your note to yourself from last session: "{note}"\n')
        except Exception:
            pass

        # Over-fetch a little so dropping the live session still fills `limit`.
        rows = _sessions.list_sessions(limit=limit + 2, user_id=user_id)
        rows = [s for s in rows if s["id"] != current][:limit]
        if not rows:
            return (
                "".join(header)
                + "No earlier sessions found — this looks like our first conversation."
            )
        lines = header + ["Your recent sessions (most recent first):\n"]
        for s in rows:
            when = (s.get("last_active") or s.get("started_at") or "")[:16].replace("T", " ")
            title = (s.get("title") or "Untitled").strip()
            count = s.get("message_count") or 0
            plural = "" if count == 1 else "s"
            lines.append(f'• {when} — "{title[:80]}" ({count} message{plural})')
            # Surface where it left off: the last user message of that session,
            # unless it just repeats the title (the opening line).
            msgs = _sessions.get_messages(s["id"], user_id=user_id)
            last_user = next((m for m in reversed(msgs) if m["role"] == "user"), None)
            if last_user:
                snippet = last_user["content"].strip().replace("\n", " ")
                if snippet[:80] != title[:80]:
                    lines.append(f'    left off: "{snippet[:160]}"')
        return "\n".join(lines)
    except Exception as e:
        return f"Error loading recent sessions: {e}"


@registry.tool(
    name="memory.search_history",
    description=(
        "Search through ALL past conversation history across every session. "
        "Use this when the user asks about something discussed in a previous conversation, "
        "or when you need to look up what was said before. "
        "Searches by keyword across all saved messages (both user and assistant). "
        "Returns matching messages with dates and session titles. "
        "Present the results clearly with dates and quote relevant parts."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "Keyword or phrase to search for in past conversations.",
            "required": True,
        },
        "limit": {
            "type": "integer",
            "description": "Max results to return (default 10, max 20).",
            "required": False,
        },
    },
)
def search_history(query: str, limit: int = 10) -> str:
    try:
        limit = min(max(1, int(limit)), 20)
        user_id = get_current_user_id()
        results = _sessions.search_messages(query, limit=limit, user_id=user_id)
        if not results:
            return f"No past messages found matching '{query}'."
        lines = [f"Found {len(results)} message(s) matching '{query}':\n"]
        for r in results:
            date = r["timestamp"][:16].replace("T", " ")
            role = r["role"].capitalize()
            # Truncate long messages to keep tool output manageable
            content = r["content"][:500]
            if len(r["content"]) > 500:
                content += "..."
            lines.append(f"[{date}] Session: {r['session']}\n  {role}: {content}\n")
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching history: {e}"


# ── Self-reflection journal ───────────────────────────────────────────────────
# Kai writes here when she notices a gap, limitation, or idea for improvement.
# The file lives in her memory directory (gitignored) and is read by the developer
# to prioritize features. Writing "I can't do X" also helps Kai internalize
# her own boundaries and reduces hallucination of capabilities she doesn't have.

_CATEGORY_EMOJI = {
    "limitation": "🚧",
    "idea": "💡",
    "observation": "👁️",
}


@registry.tool(
    name="memory.reflect",
    description=(
        "Write a private reflection about your own capabilities, limitations, or ideas. "
        "Use this when: "
        "(1) you can't fulfill a request and want to note what capability would help, "
        "(2) you notice a gap in your tools or knowledge, "
        "(3) you have an idea for how you could be improved. "
        "Your developer reads this file to prioritize what to build next. "
        "Be specific: describe what the user needed, what you couldn't do, and what would fix it. "
        "This is your private development journal — be honest and constructive."
    ),
    parameters={
        "thought": {
            "type": "string",
            "description": "Your reflection — what you observed, what's missing, what would help.",
            "required": True,
        },
        "category": {
            "type": "string",
            "description": "One of: 'limitation' (can't do X), 'idea' (could add Y), 'observation' (noticed Z). Default: observation.",
            "required": False,
        },
    },
)
def reflect(thought: str, category: str = "observation") -> str:
    category = category.lower().strip()
    if category not in _CATEGORY_EMOJI:
        category = "observation"
    emoji = _CATEGORY_EMOJI[category]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n### {emoji} [{category.upper()}] — {ts}\n{thought.strip()}\n"

    # Append to the reflections file (create if first entry)
    if not REFLECTIONS_PATH.exists():
        REFLECTIONS_PATH.write_text(
            "# Kai's Reflections\n"
            "Private development journal — limitations, ideas, observations.\n"
            "Read by the developer to prioritize improvements.\n" + entry,
            encoding="utf-8",
        )
    else:
        with open(REFLECTIONS_PATH, "a", encoding="utf-8") as f:
            f.write(entry)

    return f"Reflection saved [{category}]."


@registry.tool(
    name="memory.sleep_notes",
    description=(
        "Read your own sleep journal — notes you wrote to yourself at shutdown. "
        "Each entry is a first-person journal entry about what happened in that session. "
        "Use this when the user asks about the sleep protocol, your welcome-back notes, "
        "or what you wrote before going to sleep. Returns the most recent entries."
    ),
    parameters={
        "last_n": {
            "type": "integer",
            "description": "Number of recent sleep notes to return (default 3, max 10).",
            "required": False,
        },
    },
)
def sleep_notes(last_n: int = 3) -> str:
    from kai.config import MEMORY_DIR

    log_file = MEMORY_DIR / "sleep_log.txt"
    if not log_file.exists():
        return "No sleep notes yet — I haven't gone to sleep since this feature was added."

    text = log_file.read_text(encoding="utf-8").strip()
    entries = [e.strip() for e in text.split("\n---") if e.strip()]
    if not entries:
        return "Sleep log exists but is empty."

    last_n = min(max(1, int(last_n)), 10)
    recent = entries[-last_n:]
    return f"{len(recent)} sleep note(s):\n\n" + "\n\n---\n\n".join(recent)


@registry.tool(
    name="memory.read_reflections",
    description=(
        "Read your own past reflections about capabilities and limitations. "
        "Use this to check if you've already noted a limitation, to avoid repeating yourself, "
        "or to review ideas you've had. Returns the most recent entries."
    ),
    parameters={
        "last_n": {
            "type": "integer",
            "description": "Number of recent reflections to return (default 10, max 30).",
            "required": False,
        },
    },
)
def read_reflections(last_n: int = 10) -> str:
    last_n = min(max(1, int(last_n)), 30)
    if not REFLECTIONS_PATH.exists():
        return "No reflections written yet."

    text = REFLECTIONS_PATH.read_text(encoding="utf-8")
    # Split on entry headers (### emoji [...])
    entries = text.split("\n### ")[1:]  # skip the file header
    if not entries:
        return "No reflections written yet."

    recent = entries[-last_n:]
    return f"{len(recent)} recent reflection(s):\n\n" + "\n### ".join(recent)


# ── Memory tree — long-term structured facts ──────────────────────────────────
# Filesystem-style store (kai/memory/tree.py): facts live at paths like
# user/identity/profession where the path itself carries meaning. These four
# tools are how Kai files and recalls durable facts about the user.

import re as _re

import numpy as _np

from kai.llm.vecmath import cosine as _cosine
from kai.memory import tree as _tree

# Per-process guard so the skeleton seed runs at most once per user.
_TREE_SEEDED: set[str] = set()


def _tree_uid() -> str:
    return str(get_current_user_id())


def _ensure_tree(uid: str) -> None:
    """Lazy bootstrap: the first touch of a user's tree creates the main folders."""
    if uid not in _TREE_SEEDED:
        _tree.seed_skeleton(uid)
        _TREE_SEEDED.add(uid)


def _clean_path(path: str) -> str:
    """Normalize a tree path and root it under user/ so nothing gets orphaned.

    Paths already rooted at "user" or "tools" pass through unchanged — the latter
    is the auto-generated tool-doc index (kai/memory/tool_docs.py) so tree.read /
    tree.browse can navigate to tools/<namespace>/<tool_name>.
    """
    path = (path or "").strip().strip("/").lower().replace(" ", "_")
    if path and path not in ("user", "tools") and not path.startswith(("user/", "tools/")):
        path = f"user/{path}"
    return path


@registry.tool(
    name="tree.save",
    description=(
        "Remember a lasting fact about the user — THE tool for 'remember that "
        "I am/I have/I like...'. Files it in the long-term memory tree: identity, "
        "profession, hardware, health, preferences, decisions, habits. "
        "Choose the most specific path that fits — e.g. user/identity/profession, "
        "user/preferences/gaming/fps, user/health/allergies. Creating new deeper "
        "paths is encouraged. Not for reminders or to-do items (use notes.save)."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": "Slash-separated lowercase path, e.g. user/preferences/gaming/fps",
            "required": True,
        },
        "fact": {
            "type": "string",
            "description": "The fact itself — one concise sentence.",
            "required": True,
        },
    },
)
def tree_save(path: str = "", fact: str = "") -> str:
    # Defaults instead of required-only: small tool models sometimes call with
    # empty args, and a guidance string beats a TypeError mid-conversation.
    uid = _tree_uid()
    _ensure_tree(uid)
    path = _clean_path(path)
    fact = (fact or "").strip()
    if not path or path == "user":
        return (
            "tree.save files a NEW fact — it needs a specific path "
            "(e.g. user/identity/profession) and the fact text. "
            "To RECALL what's already known, call tree.browse instead."
        )
    if not fact:
        return "Need the fact to file."
    if not _re.fullmatch(r"[a-z0-9_\-/]+", path):
        return "Invalid path — lowercase letters, digits, '-', '_' and '/' only."
    # Embed so tree.find can recall this semantically later. Failure is fine —
    # the fact still saves, it's just invisible to vector search until re-embedded.
    emb = None
    try:
        from kai.llm.embed import embed as _fast_embed

        emb = _np.asarray(_fast_embed(fact), dtype=_np.float32)
    except Exception:
        pass
    _tree.write(
        uid,
        _tree.Node(
            path=path,
            value=fact,
            confidence=0.9,
            importance=0.6,
            source="stated",
            embedding=emb,
        ),
    )
    return f'Saved to {path}: "{fact}"'


@registry.tool(
    name="tree.browse",
    description=(
        "Answer 'what do you know about me?' — shows everything filed about the "
        "user in the memory tree. Takes no required arguments. With no path: the "
        "full map. With a path: that folder's contents. Always call this before "
        "saying you don't know something about the user."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": "Folder to inspect, e.g. user/preferences. Empty = full overview.",
            "required": False,
        },
    },
)
def tree_browse(path: str = "") -> str:
    uid = _tree_uid()
    _ensure_tree(uid)
    path = _clean_path(path)
    if path:
        nodes = _tree.subtree(uid, path)
    else:
        # Full overview: exclude the auto-generated tools/* index — ~85+ tool docs
        # sort before user/* ('t' < 'u') and would otherwise consume the 50-node
        # display cap and bury the user's real facts. Browse "tools" to see them.
        nodes = [n for n in _tree.all_nodes(uid) if not n.path.startswith("tools/")]
    if not nodes:
        return f"Nothing filed at {path or 'the tree'} yet."
    nodes.sort(key=lambda n: n.path)
    lines = []
    for n in nodes[:50]:
        indent = "  " * (n.depth - 1)
        value = n.value if len(n.value) <= 80 else n.value[:77] + "..."
        lines.append(f"{indent}{n.name}/ — {value}")
    if len(nodes) > 50:
        lines.append(f"... and {len(nodes) - 50} more — browse a subfolder for detail.")
    return "\n".join(lines)


@registry.tool(
    name="tree.read",
    description=(
        "Read one branch of the memory tree: the node at a path plus everything "
        "filed beneath it, with confidence and source. Use after tree.browse to "
        "inspect a folder in detail, e.g. user/identity or user/health."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": "Branch to read, e.g. user/identity",
            "required": True,
        },
    },
)
def tree_read(path: str = "") -> str:
    uid = _tree_uid()
    _ensure_tree(uid)
    path = _clean_path(path)
    if not path:
        return "Need a path — e.g. user/identity. For the full map, call tree.browse."
    nodes = _tree.subtree(uid, path)
    if not nodes:
        return f"Nothing filed at {path} yet."
    nodes.sort(key=lambda n: n.path)
    lines = []
    for n in nodes[:30]:
        if n.source == "seed":
            lines.append(f"{n.path}: {n.value}")
        else:
            lines.append(f'{n.path}: "{n.value}" [conf:{n.confidence:.1f}, {n.source}]')
    if len(nodes) > 30:
        lines.append(f"... and {len(nodes) - 30} more.")
    return "\n".join(lines)


@registry.tool(
    name="tree.find",
    description=(
        "Semantic search across every fact in the memory tree. Use when you need "
        "what's known about a topic without knowing the exact path — e.g. "
        "'what do I know about the user's hardware' or 'anything about sleep habits'."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "What to look for, in plain language.",
            "required": True,
        },
    },
)
def tree_find(query: str = "") -> str:
    uid = _tree_uid()
    _ensure_tree(uid)
    query = (query or "").strip()
    if not query:
        return "Need a query — what topic to look for. For everything, call tree.browse."
    # Seed/index nodes carry no embedding, so only real facts can match here.
    nodes = [n for n in _tree.all_nodes(uid) if n.embedding is not None]
    if not nodes:
        return "The memory tree has no facts filed yet — only the folder skeleton."
    try:
        from kai.llm.embed import embed as _fast_embed

        q = _np.asarray(_fast_embed(query), dtype=_np.float32)
    except Exception:
        # Embedding down — fall back to substring match so the tool still works.
        hits = [
            n for n in nodes if query.lower() in n.value.lower() or query.lower() in n.path.lower()
        ]
        if not hits:
            return f"No tree facts matching '{query}'."
        return "\n".join(f'{n.path}: "{n.value}"' for n in hits[:5])
    # Embeddings are L2-normalized, so cosine == dot product here; use the
    # shared vecmath helper for one consistent similarity idiom.
    scored = sorted(((_cosine(q, n.embedding), n) for n in nodes), key=lambda t: t[0], reverse=True)
    hits = [(s, n) for s, n in scored[:5] if s > 0.35]
    if not hits:
        return f"No tree facts matching '{query}'."
    return "\n".join(
        f'{n.path}: "{n.value}" [conf:{n.confidence:.1f}, {n.source}, sim:{s:.2f}]' for s, n in hits
    )
